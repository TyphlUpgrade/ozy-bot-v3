"""Tests for harness.lib.pipeline — state transitions, config, caveman."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.lib.pipeline import (
    AgentDef,
    CavemanConfig,
    PipelineState,
    ProjectConfig,
    VALID_CAVEMAN_LEVELS,
    VALID_STAGES,
)
from harness.lib.signals import TaskSignal


class TestPipelineState:
    def test_initial_state(self, pipeline_state):
        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None

    def test_activate(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        assert pipeline_state.active_task == "t1"
        assert pipeline_state.stage == "classify"
        assert pipeline_state.retry_count == 0

    def test_advance(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.advance("architect", "architect")
        assert pipeline_state.stage == "architect"
        assert pipeline_state.stage_agent == "architect"

    def test_clear_active(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.clear_active()
        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None

    def test_advance_rejects_invalid_stage(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        with pytest.raises(ValueError, match="Invalid stage"):
            pipeline_state.advance("reviwer")  # typo

    def test_advance_accepts_all_valid_stages(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        for stage in VALID_STAGES:
            pipeline_state.advance(stage)
            assert pipeline_state.stage == stage

    def test_escalation_started_ts_set_on_escalation(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        assert pipeline_state.escalation_started_ts is None
        pipeline_state.advance("escalation_wait")
        assert pipeline_state.escalation_started_ts is not None

    def test_escalation_started_ts_preserved_across_tiers(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_wait")
        ts = pipeline_state.escalation_started_ts
        pipeline_state.advance("escalation_tier1")
        assert pipeline_state.escalation_started_ts == ts  # preserved, not reset

    def test_escalation_started_ts_cleared_on_non_escalation(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_wait")
        assert pipeline_state.escalation_started_ts is not None
        pipeline_state.advance("executor")
        assert pipeline_state.escalation_started_ts is None

    def test_escalation_started_ts_cleared_by_clear_active(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_wait")
        pipeline_state.clear_active()
        assert pipeline_state.escalation_started_ts is None

    def test_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        state = PipelineState(active_task="t1", stage="executor", retry_count=2)
        state.worktree = tmp_path / "worktree"
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.active_task == "t1"
        assert loaded.stage == "executor"
        assert loaded.retry_count == 2
        assert loaded.worktree == tmp_path / "worktree"

    def test_save_load_roundtrip_escalation_started_ts(self, tmp_path):
        path = tmp_path / "state.json"
        state = PipelineState(active_task="t1", stage="escalation_wait")
        state.advance("escalation_wait")
        ts = state.escalation_started_ts
        assert ts is not None
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.escalation_started_ts == ts

    def test_pre_escalation_fields_default_none(self):
        state = PipelineState()
        assert state.pre_escalation_stage is None
        assert state.pre_escalation_agent is None

    def test_pre_escalation_fields_cleared_by_activate(self, pipeline_state):
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        assert pipeline_state.pre_escalation_stage is None
        assert pipeline_state.pre_escalation_agent is None

    def test_pre_escalation_fields_cleared_by_clear_active(self, pipeline_state):
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.clear_active()
        assert pipeline_state.pre_escalation_stage is None
        assert pipeline_state.pre_escalation_agent is None

    def test_save_load_roundtrip_pre_escalation_fields(self, tmp_path):
        path = tmp_path / "state.json"
        state = PipelineState(active_task="t1", stage="escalation_tier1")
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.pre_escalation_stage == "executor"
        assert loaded.pre_escalation_agent == "executor"

    def test_load_missing_file(self, tmp_path):
        state = PipelineState.load(tmp_path / "nonexistent.json")
        assert state.active_task is None

    def test_load_corrupt_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json{{{")
        state = PipelineState.load(path)
        assert state.active_task is None


class TestAgentDef:
    def test_deny_flags_str_empty(self):
        agent = AgentDef(name="exec", model="sonnet", mode="full", lifecycle="per-task")
        assert agent.deny_flags_str == ""

    def test_deny_flags_str(self):
        agent = AgentDef(
            name="arch", model="opus", mode="read-only", lifecycle="persistent",
            deny_flags=["Edit", "Write"],
        )
        assert "Edit,Write" in agent.deny_flags_str

    def test_with_cwd(self, tmp_path):
        agent = AgentDef(name="exec", model="sonnet", mode="full", lifecycle="per-task")
        copy = agent.with_cwd(tmp_path)
        assert copy.lifecycle == "per-task"
        assert copy.auto_start is False
        assert copy.cwd == tmp_path

    def test_cwd_default_none(self):
        agent = AgentDef(name="exec", model="sonnet", mode="full", lifecycle="per-task")
        assert agent.cwd is None


class TestCavemanConfig:
    def test_level_for_default(self):
        cfg = CavemanConfig(default_level="full", agents={"architect": "off"})
        assert cfg.level_for("architect") == "off"
        assert cfg.level_for("unknown_agent") == "full"

    def test_runtime_override(self):
        cfg = CavemanConfig(default_level="full", agents={"executor": "full"})
        cfg.set_agent("executor", "ultra")
        assert cfg.level_for("executor") == "ultra"
        cfg.reset_to_defaults()
        assert cfg.level_for("executor") == "full"

    def test_set_all(self):
        cfg = CavemanConfig(
            default_level="full",
            agents={"architect": "off", "executor": "full"},
        )
        cfg.set_all("ultra")
        assert cfg.level_for("architect") == "ultra"
        assert cfg.level_for("executor") == "ultra"
        cfg.reset_to_defaults()
        assert cfg.level_for("architect") == "off"

    def test_from_toml_validates_levels(self):
        with pytest.raises(ValueError, match="Invalid caveman level"):
            CavemanConfig.from_toml({
                "caveman": {"agents": {"arch": "turbo"}}
            })

    def test_from_toml_valid(self):
        cfg = CavemanConfig.from_toml({
            "caveman": {
                "default_level": "lite",
                "agents": {"architect": "off", "executor": "full"},
                "orchestrator": {"classify": "ultra"},
                "wenyan": {"enabled": False},
                "skills": {"commit": True, "compress": False},
            }
        })
        assert cfg.default_level == "lite"
        assert cfg.level_for("architect") == "off"
        assert cfg.orchestrator["classify"] == "ultra"
        assert cfg.skills_compress is False


class TestProjectConfig:
    def test_load_from_toml(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        for name in ("architect", "executor", "reviewer"):
            (agents_dir / f"{name}.md").write_text(f"# {name}")
        config_path = tmp_path / "project.toml"
        config_path.write_text("""
[project]
name = "test"
signal_dir = "signals"
task_dir = "tasks"
state_file = "state.json"

[pipeline]
poll_interval = 2.0
max_retries = 5

[caveman]
default_level = "lite"

[caveman.agents]
architect = "off"
executor = "full"
reviewer = "lite"

[caveman.orchestrator]
classify = "ultra"
wiki = "off"
""")
        cfg = ProjectConfig.load(config_path)
        assert cfg.poll_interval == 2.0
        assert cfg.max_retries == 5
        assert cfg.caveman.default_level == "lite"
        assert cfg.caveman.level_for("architect") == "off"
        assert "architect" in cfg.agents
