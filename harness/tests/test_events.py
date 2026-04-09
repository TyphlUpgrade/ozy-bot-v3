"""Tests for harness.lib.events — JSONL event log."""

from __future__ import annotations

import json

import pytest

from harness.lib.events import EventLog


class TestEventLog:
    @pytest.mark.asyncio
    async def test_record_creates_file(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        await log.record("task_activated", {"task": "t1"})
        assert log.log_path.exists()

    @pytest.mark.asyncio
    async def test_record_appends_jsonl(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        await log.record("task_activated", {"task": "t1"})
        await log.record("stage_advanced", {"task": "t1", "from": "classify", "to": "architect"})
        lines = log.log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        event1 = json.loads(lines[0])
        assert event1["event"] == "task_activated"
        assert event1["task"] == "t1"
        assert "ts" in event1
        event2 = json.loads(lines[1])
        assert event2["event"] == "stage_advanced"

    @pytest.mark.asyncio
    async def test_record_no_data(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        await log.record("session_shutdown")
        line = json.loads(log.log_path.read_text().strip())
        assert line["event"] == "session_shutdown"
        assert "ts" in line

    @pytest.mark.asyncio
    async def test_record_creates_parent_dirs(self, tmp_path):
        log = EventLog(tmp_path / "deep" / "nested" / "events.jsonl")
        await log.record("test_event")
        assert log.log_path.exists()

    @pytest.mark.asyncio
    async def test_record_handles_write_error(self, tmp_path):
        """EventLog gracefully handles write errors (logs warning, doesn't crash)."""
        log = EventLog(tmp_path / "events.jsonl")
        # Make directory read-only to force write error
        log.log_path.parent.chmod(0o444)
        try:
            await log.record("test_event")  # should not raise
        finally:
            log.log_path.parent.chmod(0o755)
