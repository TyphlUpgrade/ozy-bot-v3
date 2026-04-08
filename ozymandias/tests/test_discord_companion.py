"""
Unit tests for Phase 23 — Discord Companion command parsing and signal file helpers.

Tests exercise the companion's command dispatch and file I/O without requiring
discord.py or a live Discord connection. The companion is imported directly
since handle_command accepts a raw string (not a discord.Message).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# The companion lives outside ozymandias/ — add tools/ to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from discord_companion import (
    _is_informational,
    _read_json,
    _remove,
    _touch,
    _write_task,
    handle_command,
)
import discord_companion


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """Redirect STATE_DIR and SIGNALS_DIR to temp directory."""
    state_dir = tmp_path / "state"
    signals_dir = state_dir / "signals"
    state_dir.mkdir()
    signals_dir.mkdir()
    monkeypatch.setattr(discord_companion, "STATE_DIR", state_dir)
    monkeypatch.setattr(discord_companion, "SIGNALS_DIR", signals_dir)
    return state_dir, signals_dir


# ---------------------------------------------------------------------------
# Command parsing tests
# ---------------------------------------------------------------------------


class TestPauseCommand:
    async def test_creates_signal_file(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        result = await handle_command("!pause")
        assert "paused" in result.lower()
        assert (state_dir / "PAUSE_ENTRIES").exists()


class TestResumeCommand:
    async def test_removes_signal_file(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        (state_dir / "PAUSE_ENTRIES").touch()
        result = await handle_command("!resume")
        assert "resumed" in result.lower()
        assert not (state_dir / "PAUSE_ENTRIES").exists()

    async def test_when_not_paused(self, _isolated_dirs):
        result = await handle_command("!resume")
        assert "not paused" in result.lower()


class TestStatusCommand:
    async def test_reads_signal(self, _isolated_dirs):
        _, signals_dir = _isolated_dirs
        status = {
            "type": "status",
            "ts": "2026-04-08T14:00:00Z",
            "equity": 100000.0,
            "position_count": 2,
            "open_order_count": 1,
            "loop_health": {"broker_available": True},
        }
        (signals_dir / "status.json").write_text(json.dumps(status))
        result = await handle_command("!status")
        assert "$100,000.00" in result
        assert "Positions:** 2" in result

    async def test_missing(self, _isolated_dirs):
        result = await handle_command("!status")
        assert "not be running" in result.lower()


class TestExitCommand:
    async def test_creates_signal(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        result = await handle_command("!exit")
        assert "EMERGENCY EXIT" in result
        assert (state_dir / "EMERGENCY_EXIT").exists()


class TestForceReasoningCommand:
    async def test_creates_signal(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        result = await handle_command("!force-reasoning")
        assert "FORCE_REASONING" in result
        assert (state_dir / "FORCE_REASONING").exists()


class TestFixCommand:
    async def test_writes_task(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        result = await handle_command("!fix the broker adapter is timing out")
        assert "Task written" in result
        tasks_dir = state_dir / "agent_tasks"
        task_files = list(tasks_dir.glob("*.json"))
        assert len(task_files) == 1
        data = json.loads(task_files[0].read_text())
        assert data["description"] == "the broker adapter is timing out"
        assert data["source"] == "human"

    async def test_no_args(self, _isolated_dirs):
        result = await handle_command("!fix")
        assert "Usage" in result


# ---------------------------------------------------------------------------
# Intent filter tests
# ---------------------------------------------------------------------------


class TestIntentFilter:
    async def test_informational_filtered(self, _isolated_dirs):
        result = await handle_command("what is the current status")
        assert result is None

    async def test_command_not_filtered(self, _isolated_dirs):
        result = await handle_command("!pause")
        assert result is not None

    async def test_unknown_command_ignored(self, _isolated_dirs):
        result = await handle_command("!nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Signal file helper tests
# ---------------------------------------------------------------------------


class TestTouchHelper:
    def test_creates_file(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        target = state_dir / "subdir" / "test_file"
        _touch(target)
        assert target.exists()


class TestRemoveHelper:
    def test_existing(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        target = state_dir / "to_delete"
        target.touch()
        assert _remove(target) is True
        assert not target.exists()

    def test_missing(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        assert _remove(state_dir / "does_not_exist") is False


class TestReadJsonHelper:
    def test_valid(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        target = state_dir / "test.json"
        target.write_text('{"key": "value"}')
        data = _read_json(target)
        assert data == {"key": "value"}

    def test_missing(self, _isolated_dirs):
        state_dir, _ = _isolated_dirs
        assert _read_json(state_dir / "nope.json") is None
