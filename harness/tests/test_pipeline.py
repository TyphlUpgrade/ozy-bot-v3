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

    def test_stage_started_ts_set_on_activate(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        assert pipeline_state.stage_started_ts is not None

    def test_stage_started_ts_updated_on_advance(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        ts_after_activate = pipeline_state.stage_started_ts
        pipeline_state.advance("architect", "architect")
        assert pipeline_state.stage_started_ts is not None
        # advance() sets a fresh timestamp (may equal activate's if same instant, but field is set)
        assert pipeline_state.stage_started_ts >= ts_after_activate

    def test_stage_started_ts_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        state = PipelineState(active_task="t1", stage="executor")
        state.stage_started_ts = "2026-04-09T12:00:00+00:00"
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.stage_started_ts == "2026-04-09T12:00:00+00:00"


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


class TestShelveUnshelve:
    def test_shelve_saves_active_task(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test task")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        pipeline_state.shelve()
        assert pipeline_state.active_task is None
        assert len(pipeline_state.shelved_tasks) == 1
        assert pipeline_state.shelved_tasks[0]["task_id"] == "t1"
        assert pipeline_state.shelved_tasks[0]["stage"] == "executor"

    def test_shelve_noop_when_no_active_task(self, pipeline_state):
        pipeline_state.shelve()
        assert pipeline_state.shelved_tasks == []

    def test_unshelve_restores_task(self, pipeline_state, tmp_path):
        task = TaskSignal(task_id="t1", description="Test task")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_wait")
        pipeline_state.worktree = tmp_path / "wt"
        pipeline_state.retry_count = 2
        pipeline_state.shelve()
        assert pipeline_state.active_task is None
        restored = pipeline_state.unshelve()
        assert restored is not None
        assert pipeline_state.active_task == "t1"
        assert pipeline_state.stage == "escalation_wait"
        assert pipeline_state.worktree == tmp_path / "wt"
        assert pipeline_state.retry_count == 2

    def test_unshelve_returns_none_when_empty(self, pipeline_state):
        assert pipeline_state.unshelve() is None

    def test_shelved_tasks_persist_through_save_load(self, tmp_path):
        path = tmp_path / "state.json"
        state = PipelineState()
        task = TaskSignal(task_id="t1", description="Shelved task")
        state.activate(task)
        state.advance("escalation_wait")
        state.shelve()
        state.save(path)
        loaded = PipelineState.load(path)
        assert len(loaded.shelved_tasks) == 1
        assert loaded.shelved_tasks[0]["task_id"] == "t1"

    def test_multiple_shelve_unshelve_lifo(self, pipeline_state):
        for tid in ("t1", "t2"):
            task = TaskSignal(task_id=tid, description=f"Task {tid}")
            pipeline_state.activate(task)
            pipeline_state.advance("escalation_wait")
            pipeline_state.shelve()
        assert len(pipeline_state.shelved_tasks) == 2
        restored = pipeline_state.unshelve()
        assert restored["task_id"] == "t2"
        restored = pipeline_state.unshelve()
        assert restored["task_id"] == "t1"

    def test_load_ignores_unknown_fields(self, tmp_path):
        path = tmp_path / "state.json"
        import json
        data = {"active_task": "t1", "stage": "executor", "future_field": "value",
                "retry_count": 0, "shelved_tasks": []}
        path.write_text(json.dumps(data))
        loaded = PipelineState.load(path)
        assert loaded.active_task == "t1"
        assert loaded.stage == "executor"

    def test_shelve_unshelve_preserves_wiki_fields(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_wait")
        pipeline_state.plan_summary = "Build the feature"
        pipeline_state.diff_stat = " file.py | 3 +++\n 1 file changed"
        pipeline_state.review_verdict = "approved with notes"
        pipeline_state.shelve()
        assert pipeline_state.plan_summary is None  # cleared by clear_active
        restored = pipeline_state.unshelve()
        assert restored is not None
        assert pipeline_state.plan_summary == "Build the feature"
        assert pipeline_state.diff_stat == " file.py | 3 +++\n 1 file changed"
        assert pipeline_state.review_verdict == "approved with notes"


class TestWikiFields:
    def test_new_fields_default_none(self):
        state = PipelineState()
        assert state.plan_summary is None
        assert state.diff_stat is None
        assert state.review_verdict is None

    def test_new_fields_persist_save_load(self, tmp_path):
        path = tmp_path / "state.json"
        state = PipelineState()
        state.plan_summary = "Architect plan text"
        state.diff_stat = " 2 files changed, 10 insertions(+)"
        state.review_verdict = "approved"
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.plan_summary == "Architect plan text"
        assert loaded.diff_stat == " 2 files changed, 10 insertions(+)"
        assert loaded.review_verdict == "approved"

    def test_clear_active_resets_new_fields(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.plan_summary = "Some plan"
        pipeline_state.diff_stat = "some diff"
        pipeline_state.review_verdict = "approved"
        pipeline_state.clear_active()
        assert pipeline_state.plan_summary is None
        assert pipeline_state.diff_stat is None
        assert pipeline_state.review_verdict is None

    def test_activate_resets_wiki_fields(self, pipeline_state):
        pipeline_state.plan_summary = "Old plan"
        pipeline_state.diff_stat = "old diff"
        pipeline_state.review_verdict = "old verdict"
        task = TaskSignal(task_id="t2", description="New task")
        pipeline_state.activate(task)
        assert pipeline_state.plan_summary is None
        assert pipeline_state.diff_stat is None
        assert pipeline_state.review_verdict is None


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


class TestDialogueFields:
    def test_dialogue_fields_default(self):
        state = PipelineState()
        assert state.dialogue_last_message_ts is None
        assert state.dialogue_last_message is None
        assert state.dialogue_pending_confirmation is False
        assert state.tier1_escalation_count == 0

    def test_escalation_dialogue_in_valid_stages(self):
        assert "escalation_dialogue" in VALID_STAGES

    def test_activate_resets_dialogue_fields(self, pipeline_state):
        pipeline_state.dialogue_last_message_ts = "some-ts"
        pipeline_state.dialogue_last_message = "some-msg"
        pipeline_state.dialogue_pending_confirmation = True
        pipeline_state.tier1_escalation_count = 5
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        assert pipeline_state.dialogue_last_message_ts is None
        assert pipeline_state.dialogue_last_message is None
        assert pipeline_state.dialogue_pending_confirmation is False
        assert pipeline_state.tier1_escalation_count == 0

    def test_clear_active_resets_dialogue_fields(self, pipeline_state):
        pipeline_state.dialogue_last_message_ts = "some-ts"
        pipeline_state.dialogue_last_message = "some-msg"
        pipeline_state.dialogue_pending_confirmation = True
        pipeline_state.tier1_escalation_count = 3
        pipeline_state.clear_active()
        assert pipeline_state.dialogue_last_message_ts is None
        assert pipeline_state.dialogue_last_message is None
        assert pipeline_state.dialogue_pending_confirmation is False
        assert pipeline_state.tier1_escalation_count == 0

    def test_resume_from_escalation_clears_dialogue_not_tier1_count(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_dialogue")
        pipeline_state.dialogue_last_message_ts = "some-ts"
        pipeline_state.dialogue_last_message = "some-msg"
        pipeline_state.dialogue_pending_confirmation = True
        pipeline_state.tier1_escalation_count = 2
        pipeline_state.resume_from_escalation()
        assert pipeline_state.dialogue_last_message_ts is None
        assert pipeline_state.dialogue_last_message is None
        assert pipeline_state.dialogue_pending_confirmation is False
        assert pipeline_state.tier1_escalation_count == 2  # NOT reset

    def test_shelve_preserves_dialogue_fields(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_dialogue")
        pipeline_state.dialogue_last_message_ts = "2026-04-09T12:00:00+00:00"
        pipeline_state.dialogue_last_message = "try approach B"
        pipeline_state.dialogue_pending_confirmation = True
        pipeline_state.tier1_escalation_count = 1
        pipeline_state.shelve()
        shelved = pipeline_state.shelved_tasks[0]
        assert shelved["dialogue_last_message_ts"] == "2026-04-09T12:00:00+00:00"
        assert shelved["dialogue_last_message"] == "try approach B"
        assert shelved["dialogue_pending_confirmation"] is True
        assert shelved["tier1_escalation_count"] == 1

    def test_unshelve_clears_pending_confirmation(self, pipeline_state):
        task = TaskSignal(task_id="t1", description="Test")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_dialogue")
        pipeline_state.dialogue_last_message_ts = "2026-04-09T12:00:00+00:00"
        pipeline_state.dialogue_last_message = "try approach B"
        pipeline_state.dialogue_pending_confirmation = True
        pipeline_state.tier1_escalation_count = 2
        pipeline_state.shelve()
        pipeline_state.unshelve()
        assert pipeline_state.dialogue_last_message_ts == "2026-04-09T12:00:00+00:00"
        assert pipeline_state.dialogue_last_message == "try approach B"
        assert pipeline_state.dialogue_pending_confirmation is False  # force-cleared
        assert pipeline_state.tier1_escalation_count == 2  # preserved

    def test_dialogue_fields_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        state = PipelineState(active_task="t1", stage="escalation_dialogue")
        state.dialogue_last_message_ts = "2026-04-09T12:00:00+00:00"
        state.dialogue_last_message = "try approach B"
        state.dialogue_pending_confirmation = True
        state.tier1_escalation_count = 2
        state.save(path)
        loaded = PipelineState.load(path)
        assert loaded.dialogue_last_message_ts == "2026-04-09T12:00:00+00:00"
        assert loaded.dialogue_last_message == "try approach B"
        assert loaded.dialogue_pending_confirmation is True
        assert loaded.tier1_escalation_count == 2
