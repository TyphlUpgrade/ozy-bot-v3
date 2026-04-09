"""Shared fixtures for harness tests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harness.lib.pipeline import (
    AgentDef,
    CavemanConfig,
    PipelineState,
    ProjectConfig,
)


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory for signal files and state."""
    (tmp_path / "signals" / "architect").mkdir(parents=True)
    (tmp_path / "signals" / "executor").mkdir(parents=True)
    (tmp_path / "signals" / "reviewer").mkdir(parents=True)
    (tmp_path / "signals" / "escalation").mkdir(parents=True)
    (tmp_path / "signals" / "escalation_resolution").mkdir(parents=True)
    (tmp_path / "agent_tasks").mkdir(parents=True)
    (tmp_path / "sessions").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def config(tmp_dir):
    """Build a ProjectConfig pointing at the temp directory."""
    agents_dir = tmp_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    for name in ("architect", "executor", "reviewer"):
        (agents_dir / f"{name}.md").write_text(f"# {name} role stub")
    return ProjectConfig(
        project_root=tmp_dir,
        signal_dir=tmp_dir / "signals",
        task_dir=tmp_dir / "agent_tasks",
        state_file=tmp_dir / "pipeline_state.json",
        session_dir=tmp_dir / "sessions",
        worktree_base=tmp_dir / "worktrees",
        poll_interval=0.1,
        max_retries=3,
        escalation_timeout=14400,
        agents={
            "architect": AgentDef(
                name="architect", model="opus", mode="read-only",
                lifecycle="persistent", role_file=agents_dir / "architect.md",
            ),
            "executor": AgentDef(
                name="executor", model="sonnet", mode="full",
                lifecycle="per-task", auto_start=False,
                role_file=agents_dir / "executor.md",
            ),
            "reviewer": AgentDef(
                name="reviewer", model="sonnet", mode="read-only",
                lifecycle="persistent", role_file=agents_dir / "reviewer.md",
            ),
        },
        caveman=CavemanConfig(
            default_level="full",
            agents={"architect": "off", "executor": "full", "reviewer": "lite"},
            orchestrator={"classify": "ultra", "summarize": "ultra", "wiki": "off"},
        ),
        timeouts={"classify": 5, "summarize": 5, "reformulate": 5, "wiki": 10},
    )


@pytest.fixture
def pipeline_state():
    """Fresh pipeline state."""
    return PipelineState()
