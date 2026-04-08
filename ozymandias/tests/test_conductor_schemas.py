"""
Unit tests for Phase 24 — Conductor Wrapper schemas and Discord companion extensions.

Tests cover:
- Judgment call JSON schemas (classify_task, assemble_context, diagnose_failure)
- Task packet schema validation
- Zone file schema validation
- Discord companion conductor commands (!restart-conductor, !shutdown-conductor)
- Conductor bash script syntax
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# -- Schema validation helpers -----------------------------------------------

def _validate_keys(data: dict, required_keys: set, name: str) -> None:
    """Assert all required keys are present."""
    missing = required_keys - set(data.keys())
    assert not missing, f"{name} missing keys: {missing}"


# ---------------------------------------------------------------------------
# Judgment call schema tests
# ---------------------------------------------------------------------------


class TestClassifyTaskSchema:
    def test_input_schema(self):
        """classify_task input has all required fields."""
        input_json = {
            "judgment": "classify_task",
            "task_file": {"task_id": "test", "sections": {"TASK": "fix bug"}},
            "active_tasks": [],
            "orchestrator_state_summary": {"active_count": 0, "last_merge": None},
        }
        _validate_keys(input_json, {"judgment", "task_file", "active_tasks", "orchestrator_state_summary"}, "classify_task input")
        assert input_json["judgment"] == "classify_task"

    def test_output_schema_accept(self):
        """classify_task accept output has required fields."""
        output_json = {
            "action": "accept",
            "priority": "bug",
            "reason": "Valid bug report with reproducer",
        }
        _validate_keys(output_json, {"action", "priority", "reason"}, "classify_task output")
        assert output_json["action"] in ("accept", "defer", "reject")
        assert output_json["priority"] in ("human", "bug", "strategy_analysis", "backlog")

    def test_output_schema_reject(self):
        """classify_task reject output includes reject_reason."""
        output_json = {
            "action": "reject",
            "priority": "backlog",
            "reason": "Duplicate of active task",
            "reject_reason": "duplicate_task",
        }
        assert output_json["action"] == "reject"
        assert "reject_reason" in output_json


class TestAssembleContextSchema:
    def test_input_schema(self):
        input_json = {
            "judgment": "assemble_context",
            "task": {"task_id": "test", "sections": {}},
            "zone_files": ["core/orchestrator.py"],
            "recent_drift_log": "last 20 lines...",
        }
        _validate_keys(input_json, {"judgment", "task", "zone_files", "recent_drift_log"}, "assemble_context input")

    def test_output_schema(self):
        output_json = {
            "relevant_files": ["ozymandias/core/orchestrator.py", "ozymandias/core/signals.py"],
            "domain_context": "The orchestrator runs three async loops...",
            "known_concerns": ["RVOL filter oscillation noted in NOTES.md"],
        }
        _validate_keys(output_json, {"relevant_files", "domain_context", "known_concerns"}, "assemble_context output")
        assert isinstance(output_json["relevant_files"], list)
        assert isinstance(output_json["known_concerns"], list)


class TestDiagnoseFailureSchema:
    def test_input_schema(self):
        input_json = {
            "judgment": "diagnose_failure",
            "task_id": "fix-rvol",
            "zone_file": {"task_id": "fix-rvol", "units_completed": [1]},
            "failure_history": [
                {"attempt": 1, "error": "test_rvol_persistence failed", "wall_clock_s": 420},
            ],
            "last_agent_log_tail": "... AssertionError: 0.7 != 1.2 ...",
        }
        _validate_keys(input_json, {"judgment", "task_id", "zone_file", "failure_history", "last_agent_log_tail"}, "diagnose_failure input")

    def test_output_schema(self):
        output_json = {
            "decision": "replan",
            "notes": "The executor is modifying the wrong file",
            "architect_hint": "Persistence should be in state_manager, not orchestrator",
        }
        _validate_keys(output_json, {"decision", "notes", "architect_hint"}, "diagnose_failure output")
        assert output_json["decision"] in ("replan", "escalate", "retry_simpler")


# ---------------------------------------------------------------------------
# Task packet schema tests
# ---------------------------------------------------------------------------


class TestTaskPacketSchema:
    def test_has_all_sections(self):
        packet = {
            "task_id": "2026-04-08-fix-rvol",
            "sections": {
                "TASK": "Fix RVOL filter oscillation",
                "EXPECTED_OUTCOME": "min_rvol persists across reasoning calls",
                "MUST_DO": ["Persist filter_adjustments"],
                "MUST_NOT_DO": ["Modify the risk manager"],
                "CONTEXT": "See DRIFT_LOG",
                "ACCEPTANCE_TESTS": ["test_rvol_persistence"],
            },
            "source": "strategy_analyst",
            "priority": "backlog",
            "model_override": None,
            "zone": "core/orchestrator.py",
            "checkpoint_units": [2],
        }
        _validate_keys(packet, {"task_id", "sections", "source", "priority", "zone"}, "task packet")
        _validate_keys(
            packet["sections"],
            {"TASK", "EXPECTED_OUTCOME", "MUST_DO", "MUST_NOT_DO", "CONTEXT", "ACCEPTANCE_TESTS"},
            "task sections",
        )
        assert isinstance(packet["sections"]["MUST_DO"], list)
        assert isinstance(packet["sections"]["MUST_NOT_DO"], list)


# ---------------------------------------------------------------------------
# Zone file schema tests
# ---------------------------------------------------------------------------


class TestZoneFileSchema:
    def test_has_history_array(self):
        zone = {
            "task_id": "fix-rvol",
            "units_completed": [1, 2],
            "unit_in_progress": 3,
            "units_remaining": [4, 5],
            "test_status": "passing",
            "branch": "feature/fix-rvol",
            "worktree_path": ".worktrees/fix-rvol",
            "wall_clock_seconds": 1830,
            "last_updated": "2026-04-08T14:00:00Z",
            "history": [
                {"ts": "2026-04-08T14:00:00Z", "transition": "started", "unit": 1},
                {"ts": "2026-04-08T14:12:30Z", "transition": "completed", "unit": 1},
            ],
        }
        _validate_keys(
            zone,
            {"task_id", "units_completed", "unit_in_progress", "units_remaining",
             "test_status", "branch", "worktree_path", "wall_clock_seconds",
             "last_updated", "history"},
            "zone file",
        )
        assert isinstance(zone["history"], list)
        assert all("ts" in h and "transition" in h for h in zone["history"])


# ---------------------------------------------------------------------------
# Bash script syntax tests
# ---------------------------------------------------------------------------


class TestBashSyntax:
    def test_conductor_sh_syntax(self):
        result = subprocess.run(
            ["bash", "-n", "tools/conductor.sh"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"conductor.sh syntax error: {result.stderr}"

    def test_start_conductor_sh_syntax(self):
        result = subprocess.run(
            ["bash", "-n", "tools/start_conductor.sh"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"start_conductor.sh syntax error: {result.stderr}"


# ---------------------------------------------------------------------------
# Discord companion conductor command tests
# ---------------------------------------------------------------------------

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from discord_companion import handle_command
import discord_companion


@pytest.fixture
def _companion_dirs(tmp_path, monkeypatch):
    """Redirect companion dirs to temp."""
    state_dir = tmp_path / "state"
    signals_dir = state_dir / "signals"
    state_dir.mkdir()
    signals_dir.mkdir()
    monkeypatch.setattr(discord_companion, "STATE_DIR", state_dir)
    monkeypatch.setattr(discord_companion, "SIGNALS_DIR", signals_dir)
    return state_dir, signals_dir


class TestRestartConductor:
    async def test_creates_signal(self, _companion_dirs):
        _, signals_dir = _companion_dirs
        result = await handle_command("!restart-conductor")
        assert "restart" in result.lower()
        signal_file = signals_dir / "conductor" / "restart.json"
        assert signal_file.exists()
        data = json.loads(signal_file.read_text())
        assert data["action"] == "restart"


class TestShutdownConductor:
    async def test_creates_signal(self, _companion_dirs):
        _, signals_dir = _companion_dirs
        result = await handle_command("!shutdown-conductor")
        assert "shutdown" in result.lower()
        signal_file = signals_dir / "conductor" / "shutdown.json"
        assert signal_file.exists()
        data = json.loads(signal_file.read_text())
        assert data["action"] == "shutdown"
