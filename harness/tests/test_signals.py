"""Tests for harness.lib.signals — schema validation, read/write round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.lib.signals import (
    EscalationRequest,
    SignalReader,
    TaskSignal,
    write_signal,
)


class TestSignalSchemas:
    def test_escalation_request_defaults(self):
        esc = EscalationRequest(
            task_id="t1", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        assert esc.retry_count == 0
        assert esc.ts  # auto-generated

    def test_task_signal_defaults(self):
        task = TaskSignal(task_id="t1", description="Fix the bug")
        assert task.source == "discord"
        assert task.priority == "normal"


class TestWriteSignal:
    def test_atomic_write(self, tmp_path):
        esc = EscalationRequest(
            task_id="t1", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="?", options=["a"], context="ctx",
        )
        path = write_signal(tmp_path / "escalation", "t1.json", esc)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["task_id"] == "t1"
        assert data["category"] == "design_choice"
        # No .tmp file left behind
        assert not path.with_suffix(".tmp").exists()

    def test_creates_directory(self, tmp_path):
        task = TaskSignal(task_id="t2", description="Add feature")
        path = write_signal(tmp_path / "new" / "deep", "t2.json", task)
        assert path.exists()


class TestSignalReader:
    @pytest.mark.asyncio
    async def test_next_task_reads_oldest(self, tmp_dir):
        task_dir = tmp_dir / "agent_tasks"
        # Write two tasks
        (task_dir / "task-001.json").write_text(
            json.dumps({"task_id": "task-001", "description": "First"})
        )
        (task_dir / "task-002.json").write_text(
            json.dumps({"task_id": "task-002", "description": "Second"})
        )
        reader = SignalReader(tmp_dir / "signals")
        first = await reader.next_task(task_dir)
        assert first is not None
        assert first.task_id == "task-001"
        second = await reader.next_task(task_dir)
        assert second is not None
        assert second.task_id == "task-002"
        # No more tasks
        third = await reader.next_task(task_dir)
        assert third is None

    @pytest.mark.asyncio
    async def test_next_task_skips_bad_json(self, tmp_dir):
        task_dir = tmp_dir / "agent_tasks"
        (task_dir / "bad.json").write_text("not json{{{")
        (task_dir / "good.json").write_text(
            json.dumps({"task_id": "good", "description": "OK"})
        )
        reader = SignalReader(tmp_dir / "signals")
        task = await reader.next_task(task_dir)
        assert task is not None
        assert task.task_id == "good"

    @pytest.mark.asyncio
    async def test_read_escalation(self, tmp_dir):
        esc_dir = tmp_dir / "signals" / "escalation"
        esc = EscalationRequest(
            task_id="t1", agent="executor", stage="executor",
            severity="blocking", category="security_concern",
            question="Is this safe?", options=["yes", "no"], context="ctx",
        )
        write_signal(esc_dir, "t1.json", esc)
        reader = SignalReader(tmp_dir / "signals")
        result = await reader.read_escalation("t1")
        assert result is not None
        assert result.category == "security_concern"

    @pytest.mark.asyncio
    async def test_read_escalation_missing(self, tmp_dir):
        reader = SignalReader(tmp_dir / "signals")
        result = await reader.read_escalation("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_check_stage_complete(self, tmp_dir):
        # Write architect completion signal
        arch_dir = tmp_dir / "signals" / "architect" / "t1"
        arch_dir.mkdir(parents=True)
        (arch_dir / "plan.json").write_text(json.dumps({"plan": "do the thing"}))
        reader = SignalReader(tmp_dir / "signals")
        result = await reader.check_stage_complete("architect", "t1")
        assert result is not None
        assert result["plan"] == "do the thing"

    @pytest.mark.asyncio
    async def test_check_stage_complete_missing(self, tmp_dir):
        reader = SignalReader(tmp_dir / "signals")
        result = await reader.check_stage_complete("architect", "missing")
        assert result is None

    def test_clear_escalation(self, tmp_dir):
        esc_dir = tmp_dir / "signals" / "escalation"
        res_dir = tmp_dir / "signals" / "escalation_resolution"
        (esc_dir / "t1.json").write_text('{"task_id": "t1"}')
        (res_dir / "t1.json").write_text('{"task_id": "t1"}')
        reader = SignalReader(tmp_dir / "signals")
        reader.clear_escalation("t1")
        assert not (esc_dir / "t1.json").exists()
        assert not (res_dir / "t1.json").exists()

    def test_clear_escalation_missing_files_is_noop(self, tmp_dir):
        reader = SignalReader(tmp_dir / "signals")
        reader.clear_escalation("nonexistent")  # should not raise

    def test_archive(self, tmp_dir):
        esc_dir = tmp_dir / "signals" / "escalation"
        (esc_dir / "t1.json").write_text('{"task_id": "t1"}')
        archive = tmp_dir / "archive"
        reader = SignalReader(tmp_dir / "signals")
        reader.archive("t1", archive)
        assert not (esc_dir / "t1.json").exists()
        assert (archive / "escalation" / "t1.json").exists()
