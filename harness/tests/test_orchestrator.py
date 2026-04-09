"""Tests for harness orchestrator stage handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from harness.lib.pipeline import PipelineState
from harness.lib.signals import TaskSignal


# ---------- Helpers ----------


def _make_state(task_id="task-001", stage="classify", description="Fix the bug",
                retry_count=0) -> PipelineState:
    state = PipelineState(
        active_task=task_id,
        task_description=description,
        stage=stage,
        stage_agent=None,
        retry_count=retry_count,
    )
    return state


def _make_session_mgr():
    mgr = MagicMock()
    mgr.send = AsyncMock()
    mgr.restart = AsyncMock()
    mgr.launch = AsyncMock()
    return mgr


def _make_signal_reader():
    reader = MagicMock()
    return reader


def _make_event_log():
    log = MagicMock()
    log.record = AsyncMock()
    return log


def _make_proc(returncode=0, stdout=b"", stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------- classify_task ----------


class TestClassifyTask:
    @pytest.mark.asyncio
    async def test_complex_task_routes_to_architect(self, config, pipeline_state):
        """Complex classification advances stage to architect and sends task message."""
        from harness.orchestrator import classify_task

        task = TaskSignal(task_id="task-001", description="Refactor the auth module")
        pipeline_state.activate(task)
        session_mgr = _make_session_mgr()

        with patch("lib.claude.classify", new=AsyncMock(return_value="complex")):
            await classify_task(pipeline_state, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "architect"
        assert pipeline_state.stage_agent == "architect"
        session_mgr.send.assert_awaited_once()
        sent_msg = session_mgr.send.call_args[0][1]
        assert "task-001" in sent_msg

    @pytest.mark.asyncio
    async def test_simple_task_routes_to_executor(self, config, pipeline_state):
        """Simple classification advances stage to executor without sending to architect."""
        from harness.orchestrator import classify_task

        task = TaskSignal(task_id="task-002", description="Fix typo in README")
        pipeline_state.activate(task)
        session_mgr = _make_session_mgr()

        with patch("lib.claude.classify", new=AsyncMock(return_value="simple")):
            await classify_task(pipeline_state, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "executor"
        assert pipeline_state.stage_agent == "executor"
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_classify_uses_task_description_when_set(self, config):
        """classify_task passes task_description to claude.classify when available."""
        from harness.orchestrator import classify_task

        state = _make_state(description="Specific description")
        session_mgr = _make_session_mgr()
        captured = []

        async def fake_classify(text, cfg):
            captured.append(text)
            return "simple"

        with patch("lib.claude.classify", new=fake_classify):
            await classify_task(state, session_mgr, config, _make_event_log())

        assert captured[0] == "Specific description"

    @pytest.mark.asyncio
    async def test_classify_falls_back_to_active_task_when_no_description(self, config):
        """classify_task uses active_task as text when task_description is None."""
        from harness.orchestrator import classify_task

        state = PipelineState(active_task="task-fallback", stage="classify",
                              task_description=None)
        session_mgr = _make_session_mgr()
        captured = []

        async def fake_classify(text, cfg):
            captured.append(text)
            return "simple"

        with patch("lib.claude.classify", new=fake_classify):
            await classify_task(state, session_mgr, config, _make_event_log())

        assert captured[0] == "task-fallback"


# ---------- check_stage ----------


class TestCheckStage:
    @pytest.mark.asyncio
    async def test_completion_signal_advances_architect_to_executor(self, pipeline_state):
        """Architect completion signal advances stage to executor."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Plan it")
        pipeline_state.activate(task)
        pipeline_state.advance("architect", "architect")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value={"plan": "do the thing"})

        await check_stage(pipeline_state, signal_reader, "architect", _make_event_log())

        assert pipeline_state.stage == "executor"

    @pytest.mark.asyncio
    async def test_completion_signal_advances_executor_to_reviewer(self, pipeline_state):
        """Executor completion signal advances stage to reviewer."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Do it")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value={"status": "done"})

        await check_stage(pipeline_state, signal_reader, "executor", _make_event_log())

        assert pipeline_state.stage == "reviewer"

    @pytest.mark.asyncio
    async def test_completion_signal_advances_reviewer_to_merge(self, pipeline_state):
        """Reviewer completion signal advances stage to merge."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Review it")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value={"verdict": "approved"})

        await check_stage(pipeline_state, signal_reader, "reviewer", _make_event_log())

        assert pipeline_state.stage == "merge"

    @pytest.mark.asyncio
    async def test_no_signal_is_noop(self, pipeline_state):
        """Absent completion signal leaves state unchanged."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Do it")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value=None)

        await check_stage(pipeline_state, signal_reader, "executor", _make_event_log())

        assert pipeline_state.stage == "executor"

    @pytest.mark.asyncio
    async def test_check_stage_passes_task_id_to_reader(self, pipeline_state):
        """check_stage passes active_task to signal reader."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-xyz", description="Do it")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value=None)

        await check_stage(pipeline_state, signal_reader, "executor", _make_event_log())

        signal_reader.check_stage_complete.assert_awaited_once_with("executor", "task-xyz")


# ---------- check_reviewer ----------


class TestCheckReviewer:
    @pytest.mark.asyncio
    async def test_approve_verdict_advances_to_merge(self, config, pipeline_state):
        """Approve verdict advances stage to merge without retry."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "approve", "feedback": ""}
        )
        session_mgr = _make_session_mgr()

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "merge"
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approved_verdict_alias_advances_to_merge(self, config, pipeline_state):
        """'approved' verdict (alternate spelling) also advances to merge."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "approved"}
        )
        session_mgr = _make_session_mgr()

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "merge"

    @pytest.mark.asyncio
    async def test_no_verdict_signal_is_noop(self, config, pipeline_state):
        """Absent reviewer signal leaves state unchanged."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "reviewer"
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_below_max_retries_reformulates_and_sends(self, config,
                                                                    pipeline_state):
        """Rejection below max_retries reformulates feedback and sends to executor."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.retry_count = 0
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Missing tests"}
        )
        session_mgr = _make_session_mgr()

        with patch("lib.claude.reformulate",
                   new=AsyncMock(return_value="[REFORMULATED] Add tests for X")):
            await check_reviewer(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "executor"
        assert pipeline_state.retry_count == 1
        session_mgr.send.assert_awaited_once()
        sent_msg = session_mgr.send.call_args[0][1]
        assert sent_msg == "[REFORMULATED] Add tests for X"

    @pytest.mark.asyncio
    async def test_reject_at_max_retries_clears_active(self, config, pipeline_state):
        """Rejection at max_retries clears active task instead of retrying."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.retry_count = config.max_retries  # already at limit

        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Still broken"}
        )
        session_mgr = _make_session_mgr()

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reformulate_failure_sends_raw_feedback_as_fallback(self, config,
                                                                       pipeline_state):
        """When reformulate returns None, raw feedback is sent as [RETRY] fallback."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.retry_count = 0
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Bad logic in parser"}
        )
        session_mgr = _make_session_mgr()

        with patch("lib.claude.reformulate", new=AsyncMock(return_value=None)):
            await check_reviewer(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "executor"
        assert pipeline_state.retry_count == 1
        session_mgr.send.assert_awaited_once()
        sent_msg = session_mgr.send.call_args[0][1]
        assert sent_msg == "[RETRY] Bad logic in parser"

    @pytest.mark.asyncio
    async def test_reject_uses_default_feedback_when_missing(self, config, pipeline_state):
        """Rejection with no feedback key uses default feedback string."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.retry_count = 0
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject"}  # no "feedback" key
        )
        session_mgr = _make_session_mgr()
        captured_feedback = []

        async def fake_reformulate(feedback, task, cfg):
            captured_feedback.append(feedback)
            return None  # triggers raw fallback path

        with patch("lib.claude.reformulate", new=fake_reformulate):
            await check_reviewer(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        # Default feedback message is passed to reformulate
        assert len(captured_feedback) == 1
        assert "no specific feedback" in captured_feedback[0].lower()


# ---------- do_merge ----------


class TestDoMerge:
    @pytest.mark.asyncio
    async def test_no_worktree_skips_to_wiki(self, config, pipeline_state):
        """When worktree is None, do_merge skips directly to wiki stage."""
        from harness.orchestrator import do_merge

        task = TaskSignal(task_id="task-001", description="Fix bug")
        pipeline_state.activate(task)
        pipeline_state.advance("merge")
        pipeline_state.worktree = None

        await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.stage == "wiki"

    @pytest.mark.asyncio
    async def test_merge_failure_aborts_and_clears_active(self, config, pipeline_state,
                                                           tmp_dir):
        """Git merge failure aborts and clears active task."""
        from harness.orchestrator import do_merge

        task = TaskSignal(task_id="task-001", description="Fix bug")
        pipeline_state.activate(task)
        pipeline_state.worktree = tmp_dir / "worktrees" / "task-001"

        fail_proc = _make_proc(returncode=1, stderr=b"CONFLICT")
        abort_proc = _make_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec",
                   side_effect=[fail_proc, abort_proc]) as mock_exec:
            await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None
        # git merge --abort should have been called
        abort_call_args = mock_exec.call_args_list[1][0]
        assert "merge" in abort_call_args
        assert "--abort" in abort_call_args

    @pytest.mark.asyncio
    async def test_test_failure_reverts_with_minus_m_1(self, config, pipeline_state,
                                                        tmp_dir):
        """Test failures after a successful merge trigger git revert -m 1."""
        from harness.orchestrator import do_merge

        task = TaskSignal(task_id="task-001", description="Fix bug")
        pipeline_state.activate(task)
        pipeline_state.worktree = tmp_dir / "worktrees" / "task-001"

        merge_ok = _make_proc(returncode=0)
        tests_fail = _make_proc(returncode=1)
        revert_proc = _make_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec",
                   side_effect=[merge_ok, tests_fail, revert_proc]) as mock_exec:
            with patch("asyncio.wait_for",
                       side_effect=lambda coro, timeout: coro):
                await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.active_task is None
        revert_call_args = mock_exec.call_args_list[2][0]
        assert "revert" in revert_call_args
        assert "-m" in revert_call_args
        assert "1" in revert_call_args

    @pytest.mark.asyncio
    async def test_merge_and_tests_pass_advances_to_wiki(self, config, pipeline_state,
                                                          tmp_dir):
        """Successful merge and tests advances to wiki stage."""
        from harness.orchestrator import do_merge

        task = TaskSignal(task_id="task-001", description="Fix bug")
        pipeline_state.activate(task)
        pipeline_state.worktree = tmp_dir / "worktrees" / "task-001"

        merge_ok = _make_proc(returncode=0)
        tests_ok = _make_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec",
                   side_effect=[merge_ok, tests_ok]):
            with patch("asyncio.wait_for",
                       side_effect=lambda coro, timeout: coro):
                await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.stage == "wiki"


# ---------- do_wiki ----------


class TestDoWiki:
    @pytest.mark.asyncio
    async def test_captures_task_id_before_clear_active(self, config, pipeline_state):
        """do_wiki captures task_id before clearing state, so document_task gets correct id."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-999", description="Add feature")
        pipeline_state.activate(task)
        pipeline_state.advance("wiki")
        captured_ids = []

        async def fake_document_task(task_id, description, plan_summary,
                                     diff_stat, review_verdict, config):
            captured_ids.append(task_id)
            return True

        with patch("lib.claude.document_task", new=fake_document_task):
            await do_wiki(pipeline_state, config, _make_event_log())

        assert captured_ids == ["task-999"]
        assert pipeline_state.active_task is None  # cleared after

    @pytest.mark.asyncio
    async def test_successful_wiki_clears_active(self, config, pipeline_state):
        """Successful wiki documentation clears active task."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Fix it")
        pipeline_state.activate(task)

        with patch("lib.claude.document_task", new=AsyncMock(return_value=True)):
            await do_wiki(pipeline_state, config, _make_event_log())

        assert pipeline_state.active_task is None

    @pytest.mark.asyncio
    async def test_wiki_failure_still_clears_active(self, config, pipeline_state):
        """Failed wiki documentation still clears active task (non-blocking failure)."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Fix it")
        pipeline_state.activate(task)

        with patch("lib.claude.document_task", new=AsyncMock(return_value=False)):
            await do_wiki(pipeline_state, config, _make_event_log())

        assert pipeline_state.active_task is None


# ---------- create_worktree ----------


class TestCreateWorktree:
    @pytest.mark.asyncio
    async def test_returns_path_on_success(self, config):
        """create_worktree returns the worktree Path on successful git command."""
        from harness.orchestrator import create_worktree

        success_proc = _make_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=success_proc):
            result = await create_worktree("task-001", config)

        assert result == config.worktree_base / "task-001"

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, config):
        """create_worktree returns None when git worktree add fails."""
        from harness.orchestrator import create_worktree

        fail_proc = _make_proc(returncode=1, stderr=b"already exists")

        with patch("asyncio.create_subprocess_exec", return_value=fail_proc):
            result = await create_worktree("task-001", config)

        assert result is None

    @pytest.mark.asyncio
    async def test_creates_correct_branch_name(self, config):
        """create_worktree uses task/<task_id> as the branch name."""
        from harness.orchestrator import create_worktree

        success_proc = _make_proc(returncode=0)
        captured_args = []

        async def fake_exec(*args, **kwargs):
            captured_args.extend(args)
            return success_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await create_worktree("task-042", config)

        assert "task/task-042" in captured_args

    @pytest.mark.asyncio
    async def test_worktree_path_is_under_worktree_base(self, config):
        """Returned worktree path is worktree_base / task_id."""
        from harness.orchestrator import create_worktree

        success_proc = _make_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=success_proc):
            result = await create_worktree("my-task", config)

        assert result is not None
        assert result.parent == config.worktree_base
        assert result.name == "my-task"


# ---------- check_for_escalation ----------


class TestCheckForEscalation:
    @pytest.mark.asyncio
    async def test_no_escalation_returns_false(self, config, pipeline_state):
        """No escalation signal means check returns False, state unchanged."""
        from harness.orchestrator import check_for_escalation

        task = TaskSignal(task_id="task-001", description="Do work")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()

        result = await check_for_escalation(
            pipeline_state, signal_reader, session_mgr, config, _make_event_log()
        )

        assert result is False
        assert pipeline_state.stage == "executor"

    @pytest.mark.asyncio
    async def test_tier1_escalation_routes_to_architect(self, config, pipeline_state):
        """Tier 1 category routes escalation to architect, saves pre-escalation state."""
        from harness.orchestrator import check_for_escalation
        from harness.lib.signals import EscalationRequest

        task = TaskSignal(task_id="task-001", description="Do work")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="ambiguous_requirement",
            question="What do?", options=["a", "b"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        session_mgr = _make_session_mgr()

        result = await check_for_escalation(
            pipeline_state, signal_reader, session_mgr, config, _make_event_log()
        )

        assert result is True
        assert pipeline_state.stage == "escalation_tier1"
        assert pipeline_state.pre_escalation_stage == "executor"
        assert pipeline_state.pre_escalation_agent == "executor"
        session_mgr.send.assert_awaited_once()
        assert "architect" == session_mgr.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_tier2_escalation_routes_to_operator(self, config, pipeline_state):
        """Operator-direct category skips architect, goes to escalation_wait."""
        from harness.orchestrator import check_for_escalation
        from harness.lib.signals import EscalationRequest

        task = TaskSignal(task_id="task-001", description="Do work")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="security_concern",
            question="Is this safe?", options=["yes", "no"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        session_mgr = _make_session_mgr()

        with patch("harness.orchestrator.notify", new=AsyncMock()):
            result = await check_for_escalation(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        assert result is True
        assert pipeline_state.stage == "escalation_wait"
        assert pipeline_state.pre_escalation_stage == "executor"


# ---------- handle_escalation_tier1 ----------


class TestHandleEscalationTier1:
    @pytest.mark.asyncio
    async def test_high_confidence_resolution_resumes_original_agent(self, config, pipeline_state):
        """High confidence resolution injects into original agent and resumes."""
        from harness.orchestrator import handle_escalation_tier1
        from harness.lib.signals import ArchitectResolution

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_tier1", "architect")

        resolution = ArchitectResolution(
            task_id="task-001", resolution="option_a",
            reasoning="Clear from codebase", confidence="high",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=resolution)
        session_mgr = _make_session_mgr()
        session_mgr.sessions = {"executor": MagicMock(), "architect": MagicMock()}

        await handle_escalation_tier1(
            pipeline_state, signal_reader, session_mgr, config, _make_event_log()
        )

        assert pipeline_state.stage == "executor"
        assert pipeline_state.stage_agent == "executor"
        assert pipeline_state.pre_escalation_stage is None
        session_mgr.send.assert_awaited_once()
        assert "executor" == session_mgr.send.call_args[0][0]
        assert "ESCALATION RESOLVED" in session_mgr.send.call_args[0][1]

    @pytest.mark.asyncio
    async def test_low_confidence_promotes_to_tier2(self, config, pipeline_state):
        """Low confidence promotes escalation to Tier 2."""
        from harness.orchestrator import handle_escalation_tier1
        from harness.lib.signals import ArchitectResolution, EscalationRequest

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_tier1", "architect")

        resolution = ArchitectResolution(
            task_id="task-001", resolution="option_a",
            reasoning="Not sure", confidence="low",
        )
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="ambiguous_requirement",
            question="What?", options=["a"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=resolution)
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        session_mgr = _make_session_mgr()

        with patch("harness.orchestrator.notify", new=AsyncMock()):
            await handle_escalation_tier1(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        assert pipeline_state.stage == "escalation_wait"

    @pytest.mark.asyncio
    async def test_no_resolution_is_noop(self, config, pipeline_state):
        """No resolution signal leaves state unchanged."""
        from harness.orchestrator import handle_escalation_tier1

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_tier1", "architect")
        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()

        await handle_escalation_tier1(
            pipeline_state, signal_reader, session_mgr, config, _make_event_log()
        )

        assert pipeline_state.stage == "escalation_tier1"


# ---------- handle_escalation_wait ----------


class TestHandleEscalationWait:
    @pytest.mark.asyncio
    async def test_missing_escalation_signal_is_noop(self, config, pipeline_state):
        """No escalation signal on disk → early return, state unchanged."""
        from harness.orchestrator import handle_escalation_wait

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")

        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=None)
        event_log = _make_event_log()

        await handle_escalation_wait(pipeline_state, signal_reader, config, event_log)

        assert pipeline_state.stage == "escalation_wait"  # unchanged
        event_log.record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_advisory_auto_proceeds_after_timeout(self, config, pipeline_state):
        """Advisory escalation resumes pre-escalation stage after timeout."""
        from harness.orchestrator import handle_escalation_wait
        from harness.lib.signals import EscalationRequest

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")

        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="advisory", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        signal_reader.clear_escalation = MagicMock()
        event_log = _make_event_log()

        with patch("harness.orchestrator.escalation.should_auto_proceed", return_value=True):
            await handle_escalation_wait(pipeline_state, signal_reader, config, event_log)

        assert pipeline_state.stage == "executor"
        assert pipeline_state.pre_escalation_stage is None
        signal_reader.clear_escalation.assert_called_once_with("task-001")
        event_log.record.assert_awaited_once()
        assert event_log.record.call_args[0][0] == "escalation_auto_proceeded"

    @pytest.mark.asyncio
    async def test_blocking_renotifies_operator_at_interval(self, config, pipeline_state):
        """Blocking escalation sends reminder notification at re-notify interval."""
        from harness.orchestrator import handle_escalation_wait
        from harness.lib.signals import EscalationRequest

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")

        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="security_concern",
            question="Is this safe?", options=["yes", "no"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        event_log = _make_event_log()

        with patch("harness.orchestrator.escalation.should_auto_proceed", return_value=False), \
             patch("harness.orchestrator.escalation.should_renotify", return_value=True), \
             patch("harness.orchestrator.notify", new=AsyncMock()) as mock_notify:
            await handle_escalation_wait(pipeline_state, signal_reader, config, event_log)

        assert pipeline_state.stage == "escalation_wait"  # still waiting
        mock_notify.assert_awaited_once()
        assert "REMINDER" in mock_notify.call_args[0][2]


# ---------- check_for_escalation — informational bypass ----------


class TestInformationalEscalation:
    @pytest.mark.asyncio
    async def test_informational_sends_fyi_without_pausing(self, config, pipeline_state):
        """Informational severity sends notification but does NOT pause pipeline."""
        from harness.orchestrator import check_for_escalation
        from harness.lib.signals import EscalationRequest

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="informational", category="design_choice",
            question="FYI: I chose approach A", options=[], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        signal_reader.clear_escalation = MagicMock()
        session_mgr = _make_session_mgr()

        with patch("harness.orchestrator.notify", new=AsyncMock()) as mock_notify:
            result = await check_for_escalation(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        assert result is False  # no state transition
        assert pipeline_state.stage == "executor"  # unchanged
        assert pipeline_state.pre_escalation_stage is None  # never set
        mock_notify.assert_awaited_once()
        signal_reader.clear_escalation.assert_called_once()


# ---------- _apply_reply ----------


class TestApplyReply:
    @pytest.mark.asyncio
    async def test_happy_path_resumes_original_agent(self, pipeline_state):
        """Operator reply injects into original agent and resumes original stage."""
        from harness.discord_companion import _apply_reply

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        session_mgr = _make_session_mgr()
        session_mgr.sessions = {"executor": MagicMock()}
        signal_reader = _make_signal_reader()
        signal_reader.clear_escalation = MagicMock()

        await _apply_reply(pipeline_state, session_mgr, "task-001", "use option B", signal_reader)

        assert pipeline_state.stage == "executor"
        assert pipeline_state.pre_escalation_stage is None
        session_mgr.send.assert_awaited_once()
        assert "[OPERATOR REPLY]" in session_mgr.send.call_args[0][1]

    @pytest.mark.asyncio
    async def test_wrong_task_id_is_noop(self, pipeline_state):
        """Reply for wrong task_id is silently ignored."""
        from harness.discord_companion import _apply_reply

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.advance("escalation_wait")
        session_mgr = _make_session_mgr()
        signal_reader = _make_signal_reader()
        signal_reader.clear_escalation = MagicMock()

        await _apply_reply(pipeline_state, session_mgr, "task-999", "reply", signal_reader)

        assert pipeline_state.stage == "escalation_wait"  # unchanged

    @pytest.mark.asyncio
    async def test_wrong_stage_is_noop(self, pipeline_state):
        """Reply when not in escalation stage is rejected."""
        from harness.discord_companion import _apply_reply

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        session_mgr = _make_session_mgr()
        signal_reader = _make_signal_reader()
        signal_reader.clear_escalation = MagicMock()

        await _apply_reply(pipeline_state, session_mgr, "task-001", "reply", signal_reader)

        assert pipeline_state.stage == "executor"  # unchanged

    @pytest.mark.asyncio
    async def test_dead_session_still_advances_state(self, pipeline_state):
        """Reply with dead agent session logs warning but still advances state."""
        from harness.discord_companion import _apply_reply

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        session_mgr = _make_session_mgr()
        session_mgr.sessions = {}  # no sessions alive
        signal_reader = _make_signal_reader()
        signal_reader.clear_escalation = MagicMock()

        await _apply_reply(pipeline_state, session_mgr, "task-001", "reply", signal_reader)

        assert pipeline_state.stage == "executor"  # state advanced despite dead session
        session_mgr.send.assert_not_awaited()  # but message not delivered

    @pytest.mark.asyncio
    async def test_signal_reader_clears_escalation_on_reply(self, pipeline_state):
        """When signal_reader is provided, clear_escalation is called after resume."""
        from harness.discord_companion import _apply_reply

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        session_mgr = _make_session_mgr()
        session_mgr.sessions = {"executor": MagicMock()}
        signal_reader = _make_signal_reader()
        signal_reader.clear_escalation = MagicMock()

        await _apply_reply(pipeline_state, session_mgr, "task-001", "go with B",
                           signal_reader=signal_reader)

        assert pipeline_state.stage == "executor"
        signal_reader.clear_escalation.assert_called_once_with("task-001")


# ---------- _escalation_cache cleanup ----------


class TestEscalationCacheCleanup:
    @pytest.mark.asyncio
    async def test_cache_empty_after_tier1_resolution(self, config, pipeline_state):
        """After architect resolves escalation, _escalation_cache should be empty."""
        from harness.orchestrator import handle_escalation_tier1, _escalation_cache
        from harness.lib.signals import EscalationRequest

        # Setup state in escalation_tier1
        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_tier1", "architect")

        # Pre-populate cache (simulating check_for_escalation having stashed it)
        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        _escalation_cache["task-001"] = esc

        # Mock architect resolution (resolved, not promoted)
        resolution = MagicMock()
        resolution.resolution = "resolved"
        resolution.confidence = "high"
        resolution.reasoning = "Use approach A"

        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=resolution)
        signal_reader.clear_escalation = MagicMock()

        session_mgr = _make_session_mgr()
        session_mgr.sessions = {"executor": MagicMock()}

        event_log = _make_event_log()

        try:
            with patch("harness.orchestrator.escalation.should_promote", return_value=False):
                await handle_escalation_tier1(pipeline_state, signal_reader, session_mgr, config, event_log)
            # Assert BEFORE finally cleanup — verifies the function itself popped the entry
            assert "task-001" not in _escalation_cache
            assert pipeline_state.stage == "executor"
        finally:
            _escalation_cache.pop("task-001", None)

    @pytest.mark.asyncio
    async def test_cache_cleaned_on_tier1_promote(self, config, pipeline_state):
        """When promoting to Tier 2, _escalation_cache should be cleaned."""
        from harness.orchestrator import handle_escalation_tier1, _escalation_cache
        from harness.lib.signals import EscalationRequest

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_tier1", "architect")

        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="design_choice",
            question="Which approach?", options=["a", "b"], context="ctx",
        )
        _escalation_cache["task-001"] = esc

        resolution = MagicMock()
        resolution.resolution = "uncertain"
        resolution.confidence = "low"
        resolution.reasoning = "Not sure"

        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=resolution)
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        signal_reader.clear_escalation = MagicMock()

        session_mgr = _make_session_mgr()

        event_log = _make_event_log()

        try:
            with patch("harness.orchestrator.escalation.should_promote", return_value=True), \
                 patch("harness.orchestrator.notify", new=AsyncMock()):
                await handle_escalation_tier1(pipeline_state, signal_reader, session_mgr, config, event_log)
            # Assert BEFORE finally cleanup — verifies the function itself popped the entry
            assert "task-001" not in _escalation_cache
            assert pipeline_state.stage == "escalation_wait"
        finally:
            _escalation_cache.pop("task-001", None)


# ---------- BUG-015: handle_escalation_wait force-resume ----------


class TestEscalationWaitMissingSignal:
    @pytest.mark.asyncio
    async def test_force_resumes_on_missing_signal(self, config, pipeline_state):
        """esc=None + started_ts 3h ago > 2*escalation_timeout(3600) → force-resume."""
        from harness.orchestrator import handle_escalation_wait
        import datetime as dt_mod

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        config.escalation_timeout = 3600
        # 3 hours ago; 10800s > 2*3600=7200
        pipeline_state.escalation_started_ts = "2026-04-09T00:00:00+00:00"

        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=None)
        event_log = _make_event_log()

        fixed_now = dt_mod.datetime(2026, 4, 9, 3, 0, 0, tzinfo=dt_mod.timezone.utc)
        with patch("harness.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = dt_mod.datetime.fromisoformat
            await handle_escalation_wait(pipeline_state, signal_reader, config, event_log)

        assert pipeline_state.stage == "executor"
        assert pipeline_state.pre_escalation_stage is None
        event_log.record.assert_awaited_once()
        assert event_log.record.call_args[0][0] == "escalation_force_resumed"

    @pytest.mark.asyncio
    async def test_no_force_resume_when_fresh(self, config, pipeline_state):
        """esc=None + started_ts 10min ago < 2*escalation_timeout → no change."""
        from harness.orchestrator import handle_escalation_wait
        import datetime as dt_mod

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        config.escalation_timeout = 3600
        # 10 minutes ago; 600s << 7200
        pipeline_state.escalation_started_ts = "2026-04-09T02:50:00+00:00"

        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=None)
        event_log = _make_event_log()

        fixed_now = dt_mod.datetime(2026, 4, 9, 3, 0, 0, tzinfo=dt_mod.timezone.utc)
        with patch("harness.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = dt_mod.datetime.fromisoformat
            await handle_escalation_wait(pipeline_state, signal_reader, config, event_log)

        assert pipeline_state.stage == "escalation_wait"
        event_log.record.assert_not_awaited()


# ---------- BUG-017: handle_escalation_tier1 timeout → Tier 2 ----------


class TestEscalationTier1Timeout:
    @pytest.mark.asyncio
    async def test_timeout_promotes_to_tier2(self, config, pipeline_state):
        """resolution=None + started_ts 2h ago > tier1_timeout(1800) → promote to Tier 2."""
        from harness.orchestrator import handle_escalation_tier1
        from harness.lib.signals import EscalationRequest
        import datetime as dt_mod

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_tier1", "architect")
        # 2 hours ago; 7200s > tier1_timeout=1800
        pipeline_state.escalation_started_ts = "2026-04-09T01:00:00+00:00"

        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="ambiguous_requirement",
            question="What to do?", options=["a", "b"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=None)
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()

        fixed_now = dt_mod.datetime(2026, 4, 9, 3, 0, 0, tzinfo=dt_mod.timezone.utc)
        with patch("harness.orchestrator.datetime") as mock_dt, \
             patch("harness.orchestrator.notify", new=AsyncMock()):
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = dt_mod.datetime.fromisoformat
            await handle_escalation_tier1(
                pipeline_state, signal_reader, session_mgr, config, event_log
            )

        assert pipeline_state.stage == "escalation_wait"
        event_log.record.assert_awaited_once()
        assert event_log.record.call_args[0][0] == "escalation_promoted"
        assert event_log.record.call_args[0][1]["reason"] == "tier1_timeout"

    @pytest.mark.asyncio
    async def test_no_timeout_when_fresh(self, config, pipeline_state):
        """resolution=None + started_ts 5min ago < tier1_timeout(1800) → no change."""
        from harness.orchestrator import handle_escalation_tier1
        import datetime as dt_mod

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_tier1", "architect")
        # 5 minutes ago; 300s < tier1_timeout=1800
        pipeline_state.escalation_started_ts = "2026-04-09T02:55:00+00:00"

        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()

        fixed_now = dt_mod.datetime(2026, 4, 9, 3, 0, 0, tzinfo=dt_mod.timezone.utc)
        with patch("harness.orchestrator.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = dt_mod.datetime.fromisoformat
            await handle_escalation_tier1(
                pipeline_state, signal_reader, session_mgr, config, event_log
            )

        assert pipeline_state.stage == "escalation_tier1"
        event_log.record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_started_ts_logs_warning(self, config, pipeline_state):
        """resolution=None + started_ts=None → warning logged, no crash."""
        from harness.orchestrator import handle_escalation_tier1
        import logging

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_tier1", "architect")
        pipeline_state.escalation_started_ts = None

        signal_reader = _make_signal_reader()
        signal_reader.read_architect_resolution = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()

        with patch("harness.orchestrator.logger") as mock_logger:
            await handle_escalation_tier1(
                pipeline_state, signal_reader, session_mgr, config, event_log
            )

        mock_logger.warning.assert_called_once()
        assert "no started_ts" in mock_logger.warning.call_args[0][0].lower()
        assert pipeline_state.stage == "escalation_tier1"
        event_log.record.assert_not_awaited()


# ---------- BUG-015: handle_escalation_wait started_ts=None ----------


class TestEscalationWaitNoStartedTs:
    @pytest.mark.asyncio
    async def test_no_started_ts_logs_warning(self, config, pipeline_state):
        """esc=None + started_ts=None → warning logged, no crash."""
        from harness.orchestrator import handle_escalation_wait

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        pipeline_state.escalation_started_ts = None

        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=None)
        event_log = _make_event_log()

        with patch("harness.orchestrator.logger") as mock_logger:
            await handle_escalation_wait(pipeline_state, signal_reader, config, event_log)

        mock_logger.warning.assert_called_once()
        assert "no started_ts" in mock_logger.warning.call_args[0][0].lower()
        assert pipeline_state.stage == "escalation_wait"
        event_log.record.assert_not_awaited()
