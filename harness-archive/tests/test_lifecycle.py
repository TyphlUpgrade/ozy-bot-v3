"""Tests for harness.lib.lifecycle — reconcile, check_sessions, is_alive, build_reinit_prompt."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.lib.lifecycle import build_reinit_prompt, check_sessions, is_alive, reconcile
from harness.lib.pipeline import PipelineState
from harness.lib.sessions import Session
from harness.lib.signals import EscalationRequest, TaskSignal


# ---------- Helpers ----------


def _make_session(name: str, pid: int | None = 12345, lifecycle: str = "persistent"):
    session = Session(name=name, role=name, fd=3, fifo=MagicMock(), log=MagicMock(), pid=pid)
    return session


def _make_session_mgr(sessions: dict | None = None, config=None):
    mgr = MagicMock()
    mgr.sessions = sessions or {}
    mgr.send = AsyncMock()
    mgr.restart = AsyncMock()
    mgr.launch = AsyncMock()
    mgr.config = config
    return mgr


def _make_signal_reader(escalation=None):
    reader = MagicMock()
    reader.read_escalation = AsyncMock(return_value=escalation)
    return reader


def _active_state(task_id="task-001", stage="executor", stage_agent="executor"):
    state = PipelineState(
        active_task=task_id,
        stage=stage,
        stage_agent=stage_agent,
    )
    return state


# ---------- is_alive ----------


class TestIsAlive:
    def test_returns_true_for_live_pid(self):
        """is_alive returns True for the current process (which is definitely alive)."""
        assert is_alive(os.getpid()) is True

    def test_returns_false_for_dead_pid(self):
        """is_alive returns False for PID 0 (reserved, always raises OSError on kill sig 0)."""
        # PID 0 means "all processes in group" — os.kill(0, 0) succeeds on Linux,
        # so use a nonexistent high PID instead.
        assert is_alive(999999999) is False

    def test_returns_false_on_os_error(self):
        """is_alive returns False when os.kill raises any OSError."""
        with patch("os.kill", side_effect=OSError("permission denied")):
            assert is_alive(12345) is False

    def test_returns_true_when_os_kill_succeeds(self):
        """is_alive returns True when os.kill(pid, 0) raises no exception."""
        with patch("os.kill", return_value=None):
            assert is_alive(42) is True


# ---------- build_reinit_prompt ----------


class TestBuildReinitPrompt:
    def test_includes_task_id(self):
        """Reinit prompt includes the active task id."""
        state = _active_state(task_id="task-007", stage="executor")
        prompt = build_reinit_prompt(state)
        assert "task-007" in prompt

    def test_includes_stage(self):
        """Reinit prompt includes the current stage."""
        state = _active_state(task_id="task-007", stage="executor")
        prompt = build_reinit_prompt(state)
        assert "executor" in prompt

    def test_is_string(self):
        """build_reinit_prompt returns a non-empty string."""
        state = _active_state(task_id="t1", stage="reviewer")
        prompt = build_reinit_prompt(state)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_reinit_marker_present(self):
        """Reinit prompt contains the [REINIT] marker."""
        state = _active_state(task_id="t1", stage="merge")
        prompt = build_reinit_prompt(state)
        assert "[REINIT]" in prompt


# ---------- reconcile ----------


class TestReconcile:
    @pytest.mark.asyncio
    async def test_no_active_task_runs_session_checks(self, config):
        """reconcile with no active task still checks persistent session health."""
        state = PipelineState()  # no active task
        session = _make_session("architect", pid=12345)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        with patch("harness.lib.lifecycle.is_alive", return_value=True):
            await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_not_awaited()
        session_mgr.restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_escalation_wait_re_notifies_when_signal_exists(self, config):
        """reconcile re-notifies escalation for escalation_wait stage."""
        state = _active_state(task_id="task-001", stage="escalation_wait")
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=esc)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_awaited_once_with(esc)

    @pytest.mark.asyncio
    async def test_escalation_wait_no_signal_logs_warning_no_notify(self, config):
        """reconcile handles escalation_wait with missing signal gracefully (no crash)."""
        state = _active_state(task_id="task-001", stage="escalation_wait")
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=None)
        notify_fn = AsyncMock()

        # Should not raise
        await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_not_awaited()
        assert state.active_task == "task-001"  # not cleared

    @pytest.mark.asyncio
    async def test_missing_worktree_clears_active_state(self, config, tmp_dir):
        """reconcile clears active state when worktree path no longer exists."""
        state = _active_state(task_id="task-001", stage="executor")
        state.worktree = tmp_dir / "worktrees" / "task-001"  # does not exist
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        assert state.active_task is None
        assert state.stage is None

    @pytest.mark.asyncio
    async def test_existing_worktree_resumes_without_clearing(self, config, tmp_dir):
        """reconcile does not clear active state when worktree exists."""
        worktree = tmp_dir / "worktrees" / "task-001"
        worktree.mkdir(parents=True)
        state = _active_state(task_id="task-001", stage="executor")
        state.worktree = worktree
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        with patch("harness.lib.lifecycle.is_alive", return_value=True):
            await reconcile(state, session_mgr, signal_reader, notify_fn)

        assert state.active_task == "task-001"

    @pytest.mark.asyncio
    async def test_dead_persistent_session_is_restarted(self, config):
        """reconcile restarts a persistent session whose pid is dead."""
        state = PipelineState()
        session = _make_session("architect", pid=99999)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await reconcile(state, session_mgr, signal_reader, notify_fn)

        session_mgr.restart.assert_awaited_once_with("architect")

    @pytest.mark.asyncio
    async def test_dead_session_sends_reinit_when_is_stage_agent(self, config):
        """reconcile sends reinit prompt when dead session was the active stage agent."""
        # Use architect (persistent lifecycle) as stage agent — executor is per-task and skipped
        state = _active_state(task_id="task-001", stage="architect", stage_agent="architect")
        session = _make_session("architect", pid=99999)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await reconcile(state, session_mgr, signal_reader, notify_fn)

        session_mgr.restart.assert_awaited_once_with("architect")
        session_mgr.send.assert_awaited_once()
        sent_msg = session_mgr.send.call_args[0][1]
        assert "task-001" in sent_msg
        assert "architect" in sent_msg

    @pytest.mark.asyncio
    async def test_dead_session_no_reinit_when_not_stage_agent(self, config):
        """reconcile restarts dead session but does not send reinit when not the stage agent."""
        state = _active_state(task_id="task-001", stage="executor", stage_agent="executor")
        # architect is dead but executor is the stage agent, not architect
        session = _make_session("architect", pid=99999)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await reconcile(state, session_mgr, signal_reader, notify_fn)

        session_mgr.restart.assert_awaited_once_with("architect")
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_per_task_sessions_are_skipped(self, config):
        """reconcile does not restart per-task sessions (only persistent ones)."""
        state = PipelineState()
        # executor is per-task in the config fixture
        session = _make_session("executor", pid=99999)
        session_mgr = _make_session_mgr(
            sessions={"executor": session},
            config=config,
        )
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await reconcile(state, session_mgr, signal_reader, notify_fn)

        session_mgr.restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_escalation_tier1_with_signal(self, config):
        """reconcile re-sends escalation to architect when signal exists at escalation_tier1."""
        state = PipelineState(
            active_task="task-001",
            stage="escalation_tier1",
            stage_agent="executor",
            pre_escalation_stage="executor",
            pre_escalation_agent="executor",
        )
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=esc)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_awaited_once_with(esc)
        # Stage stays at escalation_tier1 — architect will handle it when session restarts
        assert state.stage == "escalation_tier1"

    @pytest.mark.asyncio
    async def test_reconcile_escalation_tier1_no_signal(self, config):
        """reconcile promotes to escalation_wait (Tier 2) when no signal found at escalation_tier1."""
        state = PipelineState(
            active_task="task-001",
            stage="escalation_tier1",
            stage_agent="executor",
            pre_escalation_stage="executor",
            pre_escalation_agent="executor",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=None)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_not_awaited()
        assert state.stage == "escalation_wait"

    @pytest.mark.asyncio
    async def test_reconcile_shelved_tasks_renotifies_escalation_wait(self, config):
        """reconcile re-notifies for shelved tasks stuck in escalation_wait."""
        state = PipelineState()
        state.shelved_tasks = [
            {"task_id": "task-shelved", "stage": "escalation_wait",
             "stage_agent": None, "worktree": None, "retry_count": 0},
        ]
        esc = EscalationRequest(
            task_id="task-shelved", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=esc)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        signal_reader.read_escalation.assert_awaited_with("task-shelved")
        notify_fn.assert_awaited_once_with(esc)

    @pytest.mark.asyncio
    async def test_reconcile_shelved_tasks_no_signal_logs_warning(self, config):
        """reconcile handles shelved escalation_wait with missing signal gracefully."""
        state = PipelineState()
        state.shelved_tasks = [
            {"task_id": "task-shelved", "stage": "escalation_wait",
             "stage_agent": None, "worktree": None, "retry_count": 0},
        ]
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=None)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_shelved_tier1_renotifies_when_signal_exists(self, config):
        """reconcile re-notifies for shelved tasks stuck in escalation_tier1."""
        state = PipelineState()
        state.shelved_tasks = [
            {"task_id": "task-tier1", "stage": "escalation_tier1",
             "stage_agent": None, "worktree": None, "retry_count": 0},
        ]
        esc = EscalationRequest(
            task_id="task-tier1", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=esc)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        signal_reader.read_escalation.assert_awaited_with("task-tier1")
        notify_fn.assert_awaited_once_with(esc)
        assert state.shelved_tasks[0]["stage"] == "escalation_tier1"  # unchanged when signal exists

    @pytest.mark.asyncio
    async def test_reconcile_shelved_tier1_promotes_when_no_signal(self, config):
        """reconcile promotes shelved escalation_tier1 to escalation_wait when signal missing."""
        state = PipelineState()
        state.shelved_tasks = [
            {"task_id": "task-tier1", "stage": "escalation_tier1",
             "stage_agent": None, "worktree": None, "retry_count": 0},
        ]
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=None)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_not_awaited()
        assert state.shelved_tasks[0]["stage"] == "escalation_wait"

    @pytest.mark.asyncio
    async def test_reconcile_shelved_tasks_non_escalation_ignored(self, config):
        """reconcile does not re-notify shelved tasks not in escalation_wait."""
        state = PipelineState()
        state.shelved_tasks = [
            {"task_id": "task-shelved", "stage": "executor",
             "stage_agent": "executor", "worktree": None, "retry_count": 0},
        ]
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader()
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        notify_fn.assert_not_awaited()
        signal_reader.read_escalation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_escalation_tier1_preserves_pre_escalation(self, config):
        """reconcile preserves pre_escalation_stage/agent after escalation_tier1 recovery."""
        state = PipelineState(
            active_task="task-001",
            stage="escalation_tier1",
            stage_agent="executor",
            pre_escalation_stage="executor",
            pre_escalation_agent="executor",
        )
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=esc)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        # pre_escalation fields must be intact so resume_from_escalation works later
        assert state.pre_escalation_stage == "executor"
        assert state.pre_escalation_agent == "executor"


# ---------- check_sessions ----------


class TestCheckSessions:
    @pytest.mark.asyncio
    async def test_skips_session_with_no_pid(self, config):
        """check_sessions skips sessions with pid=None (not yet started)."""
        state = PipelineState()
        session = _make_session("architect", pid=None)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )

        with patch("harness.lib.lifecycle.is_alive") as mock_alive:
            await check_sessions(session_mgr, state)

        mock_alive.assert_not_called()
        session_mgr.restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_session_is_not_restarted(self, config):
        """check_sessions does not restart a live session."""
        state = PipelineState()
        session = _make_session("architect", pid=12345)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )

        with patch("harness.lib.lifecycle.is_alive", return_value=True):
            await check_sessions(session_mgr, state)

        session_mgr.restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_session_is_restarted(self, config):
        """check_sessions restarts a persistent session that has died."""
        state = PipelineState()
        session = _make_session("architect", pid=12345)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await check_sessions(session_mgr, state)

        session_mgr.restart.assert_awaited_once_with("architect")

    @pytest.mark.asyncio
    async def test_dead_session_sends_reinit_when_is_stage_agent(self, config):
        """check_sessions sends reinit prompt when restarted session is the active stage agent."""
        state = _active_state(task_id="task-001", stage="reviewer", stage_agent="reviewer")
        session = _make_session("reviewer", pid=12345)
        session_mgr = _make_session_mgr(
            sessions={"reviewer": session},
            config=config,
        )

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await check_sessions(session_mgr, state)

        session_mgr.restart.assert_awaited_once_with("reviewer")
        session_mgr.send.assert_awaited_once()
        sent_msg = session_mgr.send.call_args[0][1]
        assert "task-001" in sent_msg
        assert "reviewer" in sent_msg

    @pytest.mark.asyncio
    async def test_dead_session_no_reinit_when_no_active_task(self, config):
        """check_sessions restarts dead session but skips reinit when no active task."""
        state = PipelineState()  # no active task
        session = _make_session("architect", pid=12345)
        session_mgr = _make_session_mgr(
            sessions={"architect": session},
            config=config,
        )

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await check_sessions(session_mgr, state)

        session_mgr.restart.assert_awaited_once_with("architect")
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_per_task_sessions_are_skipped(self, config):
        """check_sessions skips per-task lifecycle sessions."""
        state = PipelineState()
        session = _make_session("executor", pid=12345)
        session_mgr = _make_session_mgr(
            sessions={"executor": session},
            config=config,
        )

        with patch("harness.lib.lifecycle.is_alive", return_value=False):
            await check_sessions(session_mgr, state)

        session_mgr.restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multiple_sessions_each_checked_independently(self, config):
        """check_sessions evaluates each session independently."""
        state = PipelineState()
        arch_session = _make_session("architect", pid=11111)
        rev_session = _make_session("reviewer", pid=22222)
        session_mgr = _make_session_mgr(
            sessions={"architect": arch_session, "reviewer": rev_session},
            config=config,
        )

        def alive_by_pid(pid):
            return pid == 11111  # architect alive, reviewer dead

        with patch("harness.lib.lifecycle.is_alive", side_effect=alive_by_pid):
            await check_sessions(session_mgr, state)

        # Only reviewer restarted
        session_mgr.restart.assert_awaited_once_with("reviewer")


# ---------- TestReconcileEscalationDialogue ----------


class TestReconcileEscalationDialogue:
    @pytest.mark.asyncio
    async def test_reconcile_dialogue_falls_back_to_wait(self, config):
        """reconcile demotes escalation_dialogue to escalation_wait and clears dialogue fields."""
        state = PipelineState(
            active_task="task-001",
            stage="escalation_dialogue",
            stage_agent=None,
            pre_escalation_stage="executor",
            pre_escalation_agent="executor",
        )
        state.dialogue_last_message_ts = "2026-04-09T12:00:00+00:00"
        state.dialogue_last_message = "some message"
        state.dialogue_pending_confirmation = True
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=esc)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        assert state.stage == "escalation_wait"
        assert state.dialogue_last_message_ts is None
        assert state.dialogue_last_message is None
        assert state.dialogue_pending_confirmation is False
        notify_fn.assert_awaited_once_with(esc)

    @pytest.mark.asyncio
    async def test_reconcile_dialogue_no_signal(self, config):
        """reconcile demotes escalation_dialogue even when no signal found."""
        state = PipelineState(
            active_task="task-001",
            stage="escalation_dialogue",
            stage_agent=None,
            pre_escalation_stage="executor",
            pre_escalation_agent="executor",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=None)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        assert state.stage == "escalation_wait"
        notify_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_shelved_dialogue_reverts_to_wait(self, config):
        """reconcile demotes shelved escalation_dialogue to escalation_wait."""
        state = PipelineState()
        state.shelved_tasks = [
            {"task_id": "task-shelved", "stage": "escalation_dialogue",
             "stage_agent": None, "worktree": None, "retry_count": 0,
             "dialogue_last_message_ts": "2026-04-09T12:00:00+00:00",
             "dialogue_last_message": "some msg",
             "dialogue_pending_confirmation": True},
        ]
        esc = EscalationRequest(
            task_id="task-shelved", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        session_mgr = _make_session_mgr(sessions={}, config=config)
        signal_reader = _make_signal_reader(escalation=esc)
        notify_fn = AsyncMock()

        await reconcile(state, session_mgr, signal_reader, notify_fn)

        assert state.shelved_tasks[0]["stage"] == "escalation_wait"
        assert state.shelved_tasks[0]["dialogue_last_message_ts"] is None
        assert state.shelved_tasks[0]["dialogue_last_message"] is None
        assert state.shelved_tasks[0]["dialogue_pending_confirmation"] is False
        notify_fn.assert_awaited_once_with(esc)
