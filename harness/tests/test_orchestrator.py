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
        """Simple classification advances stage to executor and sends task description."""
        from harness.orchestrator import classify_task

        task = TaskSignal(task_id="task-002", description="Fix typo in README")
        pipeline_state.activate(task)
        session_mgr = _make_session_mgr()

        with patch("lib.claude.classify", new=AsyncMock(return_value="simple")):
            await classify_task(pipeline_state, session_mgr, config, _make_event_log())

        assert pipeline_state.stage == "executor"
        assert pipeline_state.stage_agent == "executor"
        session_mgr.send.assert_awaited_once()
        call_args = session_mgr.send.call_args
        assert call_args[0][0] == "executor"
        msg = call_args[0][1]
        assert "[TASK] Fix typo in README" in msg
        assert "completion-task-002.json" in msg

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
    async def test_completion_signal_advances_architect_to_executor(self, config, pipeline_state):
        """Architect completion signal advances stage to executor."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Plan it")
        pipeline_state.activate(task)
        pipeline_state.advance("architect", "architect")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value={"plan": "do the thing"})

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(), config, "architect", _make_event_log())

        assert pipeline_state.stage == "executor"

    @pytest.mark.asyncio
    async def test_completion_signal_advances_executor_to_reviewer(self, config, pipeline_state):
        """Executor completion signal advances stage to reviewer."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Do it")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value={"status": "done"})

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(), config, "executor", _make_event_log())

        assert pipeline_state.stage == "reviewer"

    @pytest.mark.asyncio
    async def test_completion_signal_advances_reviewer_to_merge(self, config, pipeline_state):
        """Reviewer completion signal advances stage to merge."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Review it")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value={"verdict": "approved"})

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(), config, "reviewer", _make_event_log())

        assert pipeline_state.stage == "merge"

    @pytest.mark.asyncio
    async def test_no_signal_is_noop(self, config, pipeline_state):
        """Absent completion signal leaves state unchanged."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Do it")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value=None)

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(), config, "executor", _make_event_log())

        assert pipeline_state.stage == "executor"

    @pytest.mark.asyncio
    async def test_check_stage_passes_task_id_to_reader(self, config, pipeline_state):
        """check_stage passes active_task to signal reader."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-xyz", description="Do it")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value=None)

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(), config, "executor", _make_event_log())

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
        """Rejection at max_retries clears active task when auto_escalate disabled."""
        from harness.orchestrator import check_reviewer

        config.auto_escalate_on_max_retries = False  # legacy behavior
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
        diff_ok = _make_proc(returncode=0, stdout=b" file.py | 3 +++\n 1 file changed\n")

        with patch("asyncio.create_subprocess_exec",
                   side_effect=[merge_ok, tests_ok, diff_ok]):
            await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.stage == "wiki"
        assert pipeline_state.diff_stat is not None


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


# ---------- BUG-011: stage timeout ----------


class TestCheckStageTimeout:
    def test_timeout_fires_when_elapsed_exceeds_max(self, config):
        """Returns True when stage elapsed time exceeds max_stage_minutes."""
        from harness.orchestrator import _check_stage_timeout
        from datetime import datetime, timezone, timedelta

        state = _make_state(stage="executor")
        # Set stage_started_ts to 4 hours ago; executor max is 3 min in test config
        past = datetime.now(timezone.utc) - timedelta(hours=4)
        state.stage_started_ts = past.isoformat()

        assert _check_stage_timeout(state, config) is True

    def test_timeout_does_not_fire_within_limit(self, config):
        """Returns False when stage elapsed time is within max_stage_minutes."""
        from harness.orchestrator import _check_stage_timeout
        from datetime import datetime, timezone

        state = _make_state(stage="executor")
        state.stage_started_ts = datetime.now(timezone.utc).isoformat()

        assert _check_stage_timeout(state, config) is False

    def test_timeout_returns_false_without_stage_started_ts(self, config):
        """Returns False when stage_started_ts is None."""
        from harness.orchestrator import _check_stage_timeout

        state = _make_state(stage="executor")
        state.stage_started_ts = None

        assert _check_stage_timeout(state, config) is False

    def test_timeout_returns_false_for_unknown_stage(self, config):
        """Returns False when stage has no entry in max_stage_minutes."""
        from harness.orchestrator import _check_stage_timeout
        from datetime import datetime, timezone, timedelta

        state = _make_state(stage="escalation_wait")
        past = datetime.now(timezone.utc) - timedelta(hours=4)
        state.stage_started_ts = past.isoformat()

        assert _check_stage_timeout(state, config) is False

    @pytest.mark.asyncio
    async def test_main_loop_timeout_clears_task_and_logs_event(self, config, pipeline_state):
        """When stage timeout fires in main loop, task is cleared and event logged."""
        from datetime import datetime, timezone, timedelta
        from harness.orchestrator import _check_stage_timeout
        import harness.orchestrator as orch_mod

        task = TaskSignal(task_id="task-001", description="Do work")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        # Force the timeout to be already exceeded
        past = datetime.now(timezone.utc) - timedelta(hours=4)
        pipeline_state.stage_started_ts = past.isoformat()

        session_mgr = _make_session_mgr()
        session_mgr.sessions = {}  # no live sessions
        event_log = _make_event_log()

        # Verify _check_stage_timeout reports True
        assert _check_stage_timeout(pipeline_state, config) is True

        # Simulate the timeout branch directly (mirrors main loop logic)
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(pipeline_state.stage_started_ts)
        ).total_seconds()
        agent_name = pipeline_state.stage_agent
        if agent_name and agent_name in session_mgr.sessions:
            await session_mgr.restart(agent_name)
        await event_log.record("stage_timeout", {
            "task": pipeline_state.active_task,
            "stage": pipeline_state.stage,
            "elapsed_seconds": elapsed,
        })
        pipeline_state.clear_active()

        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None
        event_log.record.assert_awaited_once()
        call_args = event_log.record.call_args[0]
        assert call_args[0] == "stage_timeout"
        assert call_args[1]["task"] == "task-001"
        assert call_args[1]["stage"] == "executor"


# ---------- check_stage context transfer (F1) ----------


class TestCheckStageContextTransfer:
    @pytest.mark.asyncio
    async def test_sends_summary_on_architect_to_executor(self, config):
        from harness.orchestrator import check_stage

        state = _make_state(stage="architect")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "architect plan details here"}
        )
        session_mgr = _make_session_mgr()
        session_mgr.sessions = {"executor": True}
        event_log = _make_event_log()

        with patch("lib.claude.summarize", new=AsyncMock(return_value="compressed plan")):
            await check_stage(state, signal_reader, session_mgr, config, "architect", event_log)

        assert state.stage == "executor"
        session_mgr.send.assert_awaited_once()
        msg = session_mgr.send.call_args[0][1]
        assert "[CONTEXT]" in msg
        assert "compressed plan" in msg
        assert "[TASK]" in msg
        assert session_mgr.send.call_args[0][0] == "executor"

    @pytest.mark.asyncio
    async def test_sends_summary_on_executor_to_reviewer(self, config):
        from harness.orchestrator import check_stage

        state = _make_state(stage="executor")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "executor changes summary"}
        )
        session_mgr = _make_session_mgr()
        session_mgr.sessions = {"reviewer": True}
        event_log = _make_event_log()

        with patch("lib.claude.summarize", new=AsyncMock(return_value="compressed changes")):
            await check_stage(state, signal_reader, session_mgr, config, "executor", event_log)

        assert state.stage == "reviewer"
        session_mgr.send.assert_awaited_once()
        assert session_mgr.send.call_args[0][0] == "reviewer"

    @pytest.mark.asyncio
    async def test_proceeds_without_context_when_summarize_fails(self, config):
        from harness.orchestrator import check_stage

        state = _make_state(stage="architect")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "some output"}
        )
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()

        with patch("lib.claude.summarize", new=AsyncMock(return_value=None)):
            await check_stage(state, signal_reader, session_mgr, config, "architect", event_log)

        assert state.stage == "executor"
        # Task is always sent even without context summary
        session_mgr.send.assert_awaited_once()
        msg = session_mgr.send.call_args[0][1]
        assert "[TASK]" in msg
        assert "[CONTEXT]" not in msg  # no context when summarize fails

    @pytest.mark.asyncio
    async def test_proceeds_without_context_when_no_output(self, config):
        from harness.orchestrator import check_stage

        state = _make_state(stage="architect")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(return_value={"verdict": "done"})
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()

        with patch("lib.claude.summarize", new=AsyncMock()) as mock_sum:
            await check_stage(state, signal_reader, session_mgr, config, "architect", event_log)

        assert state.stage == "executor"
        mock_sum.assert_not_awaited()  # no output to summarize
        # Task is always sent even without context
        session_mgr.send.assert_awaited_once()
        msg = session_mgr.send.call_args[0][1]
        assert "[TASK]" in msg

    @pytest.mark.asyncio
    async def test_no_context_transfer_on_reviewer_to_merge(self, config):
        from harness.orchestrator import check_stage

        state = _make_state(stage="reviewer")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "review done", "verdict": "approved"}
        )
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()

        with patch("lib.claude.summarize", new=AsyncMock()) as mock_sum:
            await check_stage(state, signal_reader, session_mgr, config, "reviewer", event_log)

        assert state.stage == "merge"
        mock_sum.assert_not_awaited()


# ---------- Integration: Shelving + Unshelving ----------


class TestShelveIntegration:
    """Orchestrator-level integration tests for task shelving during escalation."""

    @pytest.mark.asyncio
    async def test_shelve_during_escalation_activates_new_task(self, config, pipeline_state):
        """Full flow: escalation_wait + new task → shelve old, activate new."""
        from harness.orchestrator import create_worktree
        from harness.lib.signals import TaskSignal

        # Set up task in escalation_wait
        task1 = TaskSignal(task_id="task-001", description="First task")
        pipeline_state.activate(task1)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")

        # Simulate new task arriving and worktree creation
        task2 = TaskSignal(task_id="task-002", description="Second task")
        signal_reader = _make_signal_reader()
        signal_reader.next_task = AsyncMock(return_value=task2)
        session_mgr = _make_session_mgr()
        session_mgr.sessions = {}
        event_log = _make_event_log()

        with patch("harness.orchestrator.create_worktree",
                   new=AsyncMock(return_value=config.worktree_base / "task-002")):
            # Inline the shelving logic from main_loop
            new_task = await signal_reader.next_task(config.task_dir)
            assert new_task is not None
            worktree = await create_worktree(new_task.task_id, config)
            pipeline_state.shelve()
            pipeline_state.activate(new_task)
            pipeline_state.worktree = worktree

        # Verify shelved state
        assert pipeline_state.active_task == "task-002"
        assert pipeline_state.stage == "classify"
        assert len(pipeline_state.shelved_tasks) == 1
        assert pipeline_state.shelved_tasks[0]["task_id"] == "task-001"
        assert pipeline_state.shelved_tasks[0]["stage"] == "escalation_wait"

    @pytest.mark.asyncio
    async def test_unshelve_after_wiki_restores_task(self, config, pipeline_state):
        """do_wiki completes active task and unshelves previously shelved task."""
        from harness.orchestrator import do_wiki
        from harness.lib.signals import TaskSignal

        # Shelve task-001, activate task-002
        task1 = TaskSignal(task_id="task-001", description="First task")
        pipeline_state.activate(task1)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        pipeline_state.shelve()

        task2 = TaskSignal(task_id="task-002", description="Second task")
        pipeline_state.activate(task2)
        pipeline_state.advance("wiki")

        with patch("lib.claude.document_task", new=AsyncMock(return_value=True)):
            await do_wiki(pipeline_state, config, _make_event_log())

        # task-002 completed, task-001 unshelved
        assert pipeline_state.active_task == "task-001"
        assert pipeline_state.stage == "escalation_wait"
        assert len(pipeline_state.shelved_tasks) == 0

    @pytest.mark.asyncio
    async def test_unshelve_injects_pending_operator_reply(self, config, pipeline_state):
        """Unshelved task with pending_operator_reply gets reply injected."""
        from harness.orchestrator import do_wiki
        from harness.lib.signals import TaskSignal

        # Shelve task-001 with a pending reply (simulates reply received while shelved)
        task1 = TaskSignal(task_id="task-001", description="First task")
        pipeline_state.activate(task1)
        pipeline_state.pre_escalation_stage = "executor"
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.advance("escalation_wait")
        pipeline_state.shelve()
        # Simulate reply resolving escalation on shelved task
        pipeline_state.shelved_tasks[0]["stage"] = "executor"
        pipeline_state.shelved_tasks[0]["stage_agent"] = "executor"
        pipeline_state.shelved_tasks[0]["pending_operator_reply"] = "[OPERATOR REPLY] go ahead"

        task2 = TaskSignal(task_id="task-002", description="Second task")
        pipeline_state.activate(task2)
        pipeline_state.advance("wiki")

        session_mgr = _make_session_mgr()
        session_mgr.sessions = {"executor": MagicMock()}

        with patch("lib.claude.document_task", new=AsyncMock(return_value=True)):
            await do_wiki(pipeline_state, config, _make_event_log(), session_mgr)

        assert pipeline_state.active_task == "task-001"
        assert pipeline_state.stage == "executor"
        session_mgr.send.assert_awaited_once_with("executor", "[OPERATOR REPLY] go ahead")

    @pytest.mark.asyncio
    async def test_escalation_entry_clears_stale_stage_signal(self, config, pipeline_state):
        """check_for_escalation clears any pending stage completion signal."""
        from harness.orchestrator import check_for_escalation
        from harness.lib.signals import EscalationRequest

        task = TaskSignal(task_id="task-001", description="Work")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")

        esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="security_concern",
            question="Is this safe?", options=["yes", "no"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.read_escalation = AsyncMock(return_value=esc)
        signal_reader.clear_stage_signal = MagicMock()
        session_mgr = _make_session_mgr()

        with patch("harness.orchestrator.notify", new=AsyncMock()):
            await check_for_escalation(pipeline_state, signal_reader, session_mgr, config, _make_event_log())

        signal_reader.clear_stage_signal.assert_called_once_with("executor", "task-001")


# ---------- Integration: Session Rotation ----------


class TestSessionRotationIntegration:
    """Orchestrator-level test for session rotation with context re-injection."""

    @pytest.mark.asyncio
    async def test_rotation_restarts_and_reinjects_context(self, config):
        """Session rotation reads log tail, summarizes, restarts, and re-injects."""
        from harness.lib.sessions import Session

        state = _make_state(stage="executor")
        state.stage_agent = "executor"

        session_mgr = _make_session_mgr()
        log_path = MagicMock()
        log_path.exists.return_value = True
        log_path.read_text.return_value = "x" * 5000
        session = Session(name="executor", role="executor", fd=5,
                          fifo=MagicMock(), log=log_path)
        session_mgr.sessions = {"executor": session}
        session_mgr.needs_rotation = MagicMock(return_value=True)

        with patch("lib.claude.summarize", new=AsyncMock(return_value="rotation summary")):
            # Inline the rotation logic from main_loop
            if session_mgr.needs_rotation(state.stage_agent, config.token_rotation_threshold):
                sess = session_mgr.sessions[state.stage_agent]
                content = sess.log.read_text()[-4000:]
                summary = await __import__("lib.claude", fromlist=["summarize"]).summarize(content, config)
                await session_mgr.restart(state.stage_agent)
                if summary:
                    await session_mgr.send(
                        state.stage_agent,
                        f"[SYSTEM] Session rotated due to token limit. Context summary:\n\n{summary}",
                    )

        session_mgr.restart.assert_awaited_once_with("executor")
        session_mgr.send.assert_awaited_once()
        msg = session_mgr.send.call_args[0][1]
        assert "[SYSTEM] Session rotated" in msg
        assert "rotation summary" in msg


# ---------- Phase 4: Wiki data collection ----------


class TestWikiDataCollection:
    """Tests for plan_summary, diff_stat, review_verdict collection (Phase 4)."""

    @pytest.mark.asyncio
    async def test_check_stage_stores_plan_summary(self, config, pipeline_state):
        """Architect completion stores plan output on state.plan_summary."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Plan it")
        pipeline_state.activate(task)
        pipeline_state.advance("architect", "architect")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "Build auth module with JWT tokens"}
        )

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(),
                         config, "architect", _make_event_log())

        assert pipeline_state.plan_summary == "Build auth module with JWT tokens"

    @pytest.mark.asyncio
    async def test_check_stage_no_plan_summary_on_executor(self, config, pipeline_state):
        """Executor completion does not set plan_summary."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Do it")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        pipeline_state.plan_summary = "Existing plan"
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "Executor output"}
        )

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(),
                         config, "executor", _make_event_log())

        assert pipeline_state.plan_summary == "Existing plan"  # unchanged

    @pytest.mark.asyncio
    async def test_check_stage_plan_summary_fallback_to_plan_key(self, config, pipeline_state):
        """Architect signal with 'plan' key (no 'output') still sets plan_summary."""
        from harness.orchestrator import check_stage

        task = TaskSignal(task_id="task-001", description="Plan it")
        pipeline_state.activate(task)
        pipeline_state.advance("architect", "architect")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"plan": "Alternative plan text"}
        )

        await check_stage(pipeline_state, signal_reader, _make_session_mgr(),
                         config, "architect", _make_event_log())

        assert pipeline_state.plan_summary == "Alternative plan text"

    @pytest.mark.asyncio
    async def test_check_reviewer_stores_verdict(self, config, pipeline_state):
        """Approved verdict stores feedback on state.review_verdict."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "approved", "feedback": "Clean implementation, well tested"}
        )

        await check_reviewer(pipeline_state, signal_reader, _make_session_mgr(),
                            config, _make_event_log())

        assert pipeline_state.review_verdict == "Clean implementation, well tested"

    @pytest.mark.asyncio
    async def test_check_reviewer_verdict_no_feedback_defaults_approved(self, config,
                                                                        pipeline_state):
        """Approved verdict with empty feedback defaults review_verdict to 'approved'."""
        from harness.orchestrator import check_reviewer

        task = TaskSignal(task_id="task-001", description="Code change")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "approve", "feedback": ""}
        )

        await check_reviewer(pipeline_state, signal_reader, _make_session_mgr(),
                            config, _make_event_log())

        assert pipeline_state.review_verdict == "approved"

    @pytest.mark.asyncio
    async def test_do_merge_captures_diff_stat(self, config, pipeline_state, tmp_dir):
        """Successful merge captures git diff --stat on state.diff_stat."""
        from harness.orchestrator import do_merge

        task = TaskSignal(task_id="task-001", description="Add feature")
        pipeline_state.activate(task)
        pipeline_state.worktree = tmp_dir / "worktrees" / "task-001"

        merge_ok = _make_proc(returncode=0)
        tests_ok = _make_proc(returncode=0)
        diff_ok = _make_proc(returncode=0, stdout=b" src/auth.py | 42 +++\n 1 file changed, 42 insertions(+)\n")

        with patch("asyncio.create_subprocess_exec",
                   side_effect=[merge_ok, tests_ok, diff_ok]):
            await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.diff_stat == "src/auth.py | 42 +++\n 1 file changed, 42 insertions(+)"

    @pytest.mark.asyncio
    async def test_do_merge_diff_stat_failure(self, config, pipeline_state, tmp_dir):
        """Git diff --stat failure sets diff_stat to None."""
        from harness.orchestrator import do_merge

        task = TaskSignal(task_id="task-001", description="Add feature")
        pipeline_state.activate(task)
        pipeline_state.worktree = tmp_dir / "worktrees" / "task-001"

        merge_ok = _make_proc(returncode=0)
        tests_ok = _make_proc(returncode=0)
        diff_fail = _make_proc(returncode=128, stderr=b"fatal: bad revision")

        with patch("asyncio.create_subprocess_exec",
                   side_effect=[merge_ok, tests_ok, diff_fail]):
            await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.stage == "wiki"
        assert pipeline_state.diff_stat is None

    @pytest.mark.asyncio
    async def test_do_merge_no_worktree_diff_stat_stays_none(self, config, pipeline_state):
        """No worktree skips merge entirely — diff_stat stays None."""
        from harness.orchestrator import do_merge

        task = TaskSignal(task_id="task-001", description="Simple task")
        pipeline_state.activate(task)
        pipeline_state.worktree = None

        await do_merge(pipeline_state, config, _make_event_log())

        assert pipeline_state.stage == "wiki"
        assert pipeline_state.diff_stat is None

    @pytest.mark.asyncio
    async def test_do_wiki_passes_real_data(self, config, pipeline_state):
        """do_wiki passes accumulated plan_summary, diff_stat, review_verdict to document_task."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Add auth")
        pipeline_state.activate(task)
        pipeline_state.advance("wiki")
        pipeline_state.plan_summary = "Build JWT auth"
        pipeline_state.diff_stat = " auth.py | 50 +++\n 1 file changed"
        pipeline_state.review_verdict = "Clean code, approved"
        captured = {}

        async def fake_doc(task_id, description, plan_summary, diff_stat,
                           review_verdict, config):
            captured.update(plan_summary=plan_summary, diff_stat=diff_stat,
                           review_verdict=review_verdict)
            return True

        with patch("lib.claude.document_task", new=fake_doc):
            await do_wiki(pipeline_state, config, _make_event_log())

        assert captured["plan_summary"] == "Build JWT auth"
        assert captured["diff_stat"] == " auth.py | 50 +++\n 1 file changed"
        assert captured["review_verdict"] == "Clean code, approved"

    @pytest.mark.asyncio
    async def test_do_wiki_fallbacks_on_none(self, config, pipeline_state):
        """do_wiki passes fallback strings when fields are None."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Simple fix")
        pipeline_state.activate(task)
        pipeline_state.advance("wiki")
        # Leave plan_summary, diff_stat, review_verdict as None
        captured = {}

        async def fake_doc(task_id, description, plan_summary, diff_stat,
                           review_verdict, config):
            captured.update(plan_summary=plan_summary, diff_stat=diff_stat,
                           review_verdict=review_verdict)
            return True

        with patch("lib.claude.document_task", new=fake_doc):
            await do_wiki(pipeline_state, config, _make_event_log())

        assert captured["plan_summary"] == "(no architect plan)"
        assert captured["diff_stat"] == "(no file changes)"
        assert captured["review_verdict"] == "(no review)"

    @pytest.mark.asyncio
    async def test_do_wiki_records_wiki_failed_event(self, config, pipeline_state):
        """Failed wiki documentation records wiki_failed event."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Fix bug")
        pipeline_state.activate(task)
        pipeline_state.advance("wiki")
        event_log = _make_event_log()

        with patch("lib.claude.document_task", new=AsyncMock(return_value=False)):
            await do_wiki(pipeline_state, config, event_log)

        events = [c[0][0] for c in event_log.record.call_args_list]
        assert "wiki_failed" in events
        wiki_event = next(c for c in event_log.record.call_args_list
                         if c[0][0] == "wiki_failed")
        assert wiki_event[0][1]["task"] == "task-001"


# ---------- TestPausedPipeline ----------


class TestPausedPipeline:
    def test_paused_field_defaults_false(self):
        state = PipelineState()
        assert state.paused is False

    def test_paused_persists_across_save_load(self, config):
        state = PipelineState()
        state.paused = True
        state.save(config.state_file)
        loaded = PipelineState.load(config.state_file)
        assert loaded.paused is True

    def test_paused_not_reset_by_clear_active(self):
        """Pause is pipeline-wide, not per-task — clear_active must not reset it."""
        state = PipelineState()
        state.paused = True
        task = TaskSignal(task_id="task-001", description="Work")
        state.activate(task)
        state.clear_active()
        assert state.paused is True

    def test_paused_not_in_shelved_dict(self):
        """Pause is pipeline-wide — it should not be stored per-shelved-task."""
        state = PipelineState()
        state.paused = True
        task = TaskSignal(task_id="task-001", description="Work")
        state.activate(task)
        state.advance("executor")
        state.shelve()
        assert "paused" not in state.shelved_tasks[0]


class TestDoWikiNotification:
    @pytest.mark.asyncio
    async def test_task_completed_notification_sent(self, config, pipeline_state):
        """do_wiki sends task_completed notification via notify."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Fix the bug")
        pipeline_state.activate(task)
        pipeline_state.advance("wiki")
        event_log = _make_event_log()

        with patch("lib.claude.document_task", new=AsyncMock(return_value=True)), \
             patch("harness.orchestrator.notify", new=AsyncMock()) as mock_notify:
            await do_wiki(pipeline_state, config, event_log)

        # Find task_completed call among notify calls
        completed_calls = [c for c in mock_notify.call_args_list
                          if c[0][0] == "task_completed"]
        assert len(completed_calls) == 1


# ---------- TestHandleEscalationDialogue ----------


class TestHandleEscalationDialogue:
    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_escalation_wait(self, config, pipeline_state):
        """dialogue_last_message_ts older than dialogue_timeout advances to escalation_wait and clears dialogue fields."""
        from datetime import datetime, UTC, timedelta
        from harness.orchestrator import handle_escalation_dialogue

        pipeline_state.advance("escalation_dialogue")
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.dialogue_last_message_ts = (
            datetime.now(UTC) - timedelta(seconds=2000)
        ).isoformat()
        pipeline_state.dialogue_last_message = "any message"
        pipeline_state.dialogue_pending_confirmation = False
        event_log = _make_event_log()
        config.dialogue_timeout = 1800

        await handle_escalation_dialogue(pipeline_state, config, event_log)

        assert pipeline_state.stage == "escalation_wait"
        assert pipeline_state.dialogue_last_message_ts is None
        assert pipeline_state.dialogue_last_message is None
        assert pipeline_state.dialogue_pending_confirmation is False
        events = [c[0][0] for c in event_log.record.call_args_list]
        assert "dialogue_timeout" in events

    @pytest.mark.asyncio
    async def test_no_timeout_when_within_threshold(self, config, pipeline_state):
        """Recent dialogue_last_message_ts with no message leaves state unchanged."""
        from datetime import datetime, UTC, timedelta
        from harness.orchestrator import handle_escalation_dialogue

        pipeline_state.advance("escalation_dialogue")
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.dialogue_last_message_ts = (
            datetime.now(UTC) - timedelta(seconds=10)
        ).isoformat()
        pipeline_state.dialogue_last_message = None
        event_log = _make_event_log()
        config.dialogue_timeout = 1800

        with patch("harness.orchestrator.claude.classify_resolution", new_callable=AsyncMock) as mock_classify:
            await handle_escalation_dialogue(pipeline_state, config, event_log)

        assert pipeline_state.stage == "escalation_dialogue"
        mock_classify.assert_not_awaited()
        event_log.record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolution_detected_sets_pending_confirmation(self, config, pipeline_state):
        """classify_resolution returning 'resolution' sets pending_confirmation and calls notify."""
        from harness.orchestrator import handle_escalation_dialogue

        pipeline_state.advance("escalation_dialogue")
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.dialogue_last_message = "I think we should proceed"
        pipeline_state.dialogue_last_message_ts = None
        pipeline_state.dialogue_pending_confirmation = False
        event_log = _make_event_log()
        config.dialogue_timeout = 1800

        with patch("harness.orchestrator.claude.classify_resolution",
                   new_callable=AsyncMock, return_value="resolution") as mock_classify, \
             patch("harness.orchestrator.notify", new_callable=AsyncMock) as mock_notify:
            await handle_escalation_dialogue(pipeline_state, config, event_log)

        mock_classify.assert_awaited_once()
        assert pipeline_state.dialogue_pending_confirmation is True
        mock_notify.assert_awaited_once()
        notify_args = mock_notify.call_args[0]
        assert notify_args[0] == "dialogue_confirm"
        assert notify_args[1] == "executor"
        assert pipeline_state.dialogue_last_message is None  # consumed

    @pytest.mark.asyncio
    async def test_continuation_consumes_message(self, config, pipeline_state):
        """classify_resolution returning 'continuation' consumes message but leaves pending_confirmation False."""
        from harness.orchestrator import handle_escalation_dialogue

        pipeline_state.advance("escalation_dialogue")
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.dialogue_last_message = "Can you explain more?"
        pipeline_state.dialogue_last_message_ts = None
        pipeline_state.dialogue_pending_confirmation = False
        event_log = _make_event_log()
        config.dialogue_timeout = 1800

        with patch("harness.orchestrator.claude.classify_resolution",
                   new_callable=AsyncMock, return_value="continuation"), \
             patch("harness.orchestrator.notify", new_callable=AsyncMock) as mock_notify:
            await handle_escalation_dialogue(pipeline_state, config, event_log)

        assert pipeline_state.dialogue_last_message is None  # consumed
        assert pipeline_state.dialogue_pending_confirmation is False
        mock_notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_classify_when_pending_confirmation(self, config, pipeline_state):
        """When dialogue_pending_confirmation is True, classify is not called and message is not consumed."""
        from harness.orchestrator import handle_escalation_dialogue

        pipeline_state.advance("escalation_dialogue")
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.dialogue_last_message = "yes, proceed"
        pipeline_state.dialogue_last_message_ts = None
        pipeline_state.dialogue_pending_confirmation = True
        event_log = _make_event_log()
        config.dialogue_timeout = 1800

        with patch("harness.orchestrator.claude.classify_resolution",
                   new_callable=AsyncMock) as mock_classify:
            await handle_escalation_dialogue(pipeline_state, config, event_log)

        mock_classify.assert_not_awaited()
        assert pipeline_state.dialogue_last_message == "yes, proceed"  # NOT consumed

    @pytest.mark.asyncio
    async def test_timeout_uses_escalation_started_when_no_message_ts(self, config, pipeline_state):
        """When dialogue_last_message_ts is None, escalation_started_ts drives timeout."""
        from datetime import datetime, UTC, timedelta
        from harness.orchestrator import handle_escalation_dialogue

        pipeline_state.advance("escalation_dialogue")
        pipeline_state.pre_escalation_agent = "executor"
        pipeline_state.dialogue_last_message_ts = None
        pipeline_state.dialogue_last_message = None
        pipeline_state.dialogue_pending_confirmation = False
        pipeline_state.escalation_started_ts = (
            datetime.now(UTC) - timedelta(seconds=2000)
        ).isoformat()
        event_log = _make_event_log()
        config.dialogue_timeout = 1800

        await handle_escalation_dialogue(pipeline_state, config, event_log)

        assert pipeline_state.stage == "escalation_wait"
        assert pipeline_state.dialogue_last_message_ts is None
        assert pipeline_state.dialogue_last_message is None
        assert pipeline_state.dialogue_pending_confirmation is False
        events = [c[0][0] for c in event_log.record.call_args_list]
        assert "dialogue_timeout" in events


# ---------- TestCircuitBreaker ----------


class TestCircuitBreaker:
    def test_tier1_increments_count(self):
        """route_escalation returns tier1, count=0, max=2 → returns tier1, count becomes 1."""
        from harness.orchestrator import _route_with_circuit_breaker

        state = _make_state()
        state.tier1_escalation_count = 0
        esc = MagicMock()
        config = MagicMock()
        config.max_tier1_escalations = 2

        with patch("harness.orchestrator.escalation.route_escalation", return_value="tier1"):
            result = _route_with_circuit_breaker(state, esc, config)

        assert result == "tier1"
        assert state.tier1_escalation_count == 1

    def test_tier1_at_max_forces_tier2(self):
        """count=2, max=2 → circuit breaker fires, returns tier2, count stays 2."""
        from harness.orchestrator import _route_with_circuit_breaker

        state = _make_state()
        state.tier1_escalation_count = 2
        esc = MagicMock()
        config = MagicMock()
        config.max_tier1_escalations = 2

        with patch("harness.orchestrator.escalation.route_escalation", return_value="tier1"):
            result = _route_with_circuit_breaker(state, esc, config)

        assert result == "tier2"
        assert state.tier1_escalation_count == 2  # not incremented

    def test_tier2_passthrough(self):
        """route_escalation returns tier2 → returns tier2, tier1 count unchanged."""
        from harness.orchestrator import _route_with_circuit_breaker

        state = _make_state()
        state.tier1_escalation_count = 0
        esc = MagicMock()
        config = MagicMock()
        config.max_tier1_escalations = 2

        with patch("harness.orchestrator.escalation.route_escalation", return_value="tier2"):
            result = _route_with_circuit_breaker(state, esc, config)

        assert result == "tier2"
        assert state.tier1_escalation_count == 0  # untouched

    def test_tier1_below_max_allows_through(self):
        """count=1, max=2 → still below limit, returns tier1, count becomes 2."""
        from harness.orchestrator import _route_with_circuit_breaker

        state = _make_state()
        state.tier1_escalation_count = 1
        esc = MagicMock()
        config = MagicMock()
        config.max_tier1_escalations = 2

        with patch("harness.orchestrator.escalation.route_escalation", return_value="tier1"):
            result = _route_with_circuit_breaker(state, esc, config)

        assert result == "tier1"
        assert state.tier1_escalation_count == 2


# ---------- TestAutoEscalation ----------


class TestAutoEscalation:
    """Tests for the auto-escalation path in check_reviewer (BUG-023, BUG-024 fixes)."""

    def _setup_config(self, config):
        config.max_retries = 3
        config.auto_escalate_on_max_retries = True
        config.max_tier1_escalations = 2
        return config

    @pytest.mark.asyncio
    async def test_auto_escalate_creates_escalation_and_routes_tier1(
        self, config, pipeline_state
    ):
        """Tier 1 path: architect receives message, state advances to escalation_tier1."""
        from harness.orchestrator import check_reviewer

        self._setup_config(config)
        task = TaskSignal(task_id="task-001", description="Fix the bug")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        pipeline_state.retry_count = 3  # at max_retries

        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Code has issues"}
        )
        signal_reader.read_escalation = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()

        with patch("harness.orchestrator.notify", new_callable=AsyncMock), \
             patch("lib.signals.write_signal") as mock_write_signal, \
             patch("harness.orchestrator.escalation") as mock_escalation:
            mock_escalation.route_escalation.return_value = "tier1"
            mock_escalation.format_escalation_for_architect.return_value = "esc msg"
            await check_reviewer(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        assert pipeline_state.stage == "escalation_tier1"
        assert pipeline_state.stage_agent == "architect"
        mock_write_signal.assert_called_once()
        session_mgr.send.assert_awaited_once()
        assert session_mgr.send.call_args[0][0] == "architect"

    @pytest.mark.asyncio
    async def test_auto_escalate_routes_tier2_when_circuit_breaker_trips(
        self, config, pipeline_state
    ):
        """tier1_escalation_count >= max_tier1_escalations → tier2, operator notified, escalation_wait."""
        from harness.orchestrator import check_reviewer

        self._setup_config(config)
        task = TaskSignal(task_id="task-001", description="Fix the bug")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        pipeline_state.retry_count = 3
        pipeline_state.tier1_escalation_count = 2  # circuit breaker tripped

        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Code has issues"}
        )
        signal_reader.read_escalation = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()

        with patch("harness.orchestrator.notify", new_callable=AsyncMock) as mock_notify, \
             patch("lib.signals.write_signal"), \
             patch("harness.orchestrator.escalation") as mock_escalation:
            mock_escalation.route_escalation.return_value = "tier1"
            mock_escalation.format_tier2_notification.return_value = "tier2 summary"
            await check_reviewer(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        assert pipeline_state.stage == "escalation_wait"
        mock_notify.assert_awaited_once()
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_escalate_skips_when_existing_escalation(
        self, config, pipeline_state
    ):
        """read_escalation returns truthy → clear_active called, no new escalation written."""
        from harness.orchestrator import check_reviewer
        from harness.lib.signals import EscalationRequest

        self._setup_config(config)
        task = TaskSignal(task_id="task-001", description="Fix the bug")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        pipeline_state.retry_count = 3

        existing_esc = EscalationRequest(
            task_id="task-001", agent="executor", stage="executor",
            severity="blocking", category="ambiguous_requirement",
            question="Already escalated?", options=["yes"], context="ctx",
        )
        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Code has issues"}
        )
        signal_reader.read_escalation = AsyncMock(return_value=existing_esc)
        session_mgr = _make_session_mgr()

        with patch("harness.orchestrator.notify", new_callable=AsyncMock), \
             patch("lib.signals.write_signal") as mock_write_signal:
            await check_reviewer(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        mock_write_signal.assert_not_called()
        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None
        session_mgr.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_escalate_uses_retry_count_zero(
        self, config, pipeline_state
    ):
        """EscalationRequest.retry_count is 0, not state.retry_count (BUG-023 fix)."""
        from harness.orchestrator import check_reviewer

        self._setup_config(config)
        task = TaskSignal(task_id="task-001", description="Fix the bug")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        pipeline_state.retry_count = 3

        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Code has issues"}
        )
        signal_reader.read_escalation = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()

        captured_requests = []

        def capture_write(directory, filename, obj):
            captured_requests.append(obj)

        with patch("harness.orchestrator.notify", new_callable=AsyncMock), \
             patch("lib.signals.write_signal", side_effect=capture_write), \
             patch("harness.orchestrator.escalation") as mock_escalation:
            mock_escalation.route_escalation.return_value = "tier1"
            mock_escalation.format_escalation_for_architect.return_value = "esc msg"
            await check_reviewer(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        assert len(captured_requests) == 1
        assert captured_requests[0].retry_count == 0

    @pytest.mark.asyncio
    async def test_auto_escalate_sets_pre_escalation_to_executor(
        self, config, pipeline_state
    ):
        """pre_escalation_stage and pre_escalation_agent are 'executor', not 'reviewer' (BUG-024 fix)."""
        from harness.orchestrator import check_reviewer

        self._setup_config(config)
        task = TaskSignal(task_id="task-001", description="Fix the bug")
        pipeline_state.activate(task)
        pipeline_state.advance("reviewer", "reviewer")
        pipeline_state.retry_count = 3

        signal_reader = _make_signal_reader()
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "reject", "feedback": "Code has issues"}
        )
        signal_reader.read_escalation = AsyncMock(return_value=None)
        session_mgr = _make_session_mgr()

        captured = {}

        def capture_route(state, esc, cfg):
            captured["pre_escalation_stage"] = state.pre_escalation_stage
            captured["pre_escalation_agent"] = state.pre_escalation_agent
            return "tier1"

        with patch("harness.orchestrator.notify", new_callable=AsyncMock), \
             patch("lib.signals.write_signal"), \
             patch("harness.orchestrator.escalation") as mock_escalation, \
             patch("harness.orchestrator._route_with_circuit_breaker",
                   side_effect=capture_route):
            mock_escalation.format_escalation_for_architect.return_value = "esc msg"
            await check_reviewer(
                pipeline_state, signal_reader, session_mgr, config, _make_event_log()
            )

        assert captured["pre_escalation_stage"] == "executor"
        assert captured["pre_escalation_agent"] == "executor"


# ---------- Full pipeline integration (BUG-025/026/027 regression) ----------


class TestFullPipelineIntegration:
    """Integration tests exercising full pipeline flow without Discord."""

    @pytest.mark.asyncio
    async def test_simple_task_full_pipeline(self, config, pipeline_state):
        """Simple task flows: classify → executor → reviewer → merge → wiki → done."""
        from harness.orchestrator import classify_task, check_stage, check_reviewer, do_merge, do_wiki

        task = TaskSignal(task_id="integ-001", description="Add hello world command")
        pipeline_state.activate(task)
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()
        signal_reader = _make_signal_reader()

        # 1. classify → executor (simple)
        with patch("lib.claude.classify", new=AsyncMock(return_value="simple")):
            await classify_task(pipeline_state, session_mgr, config, event_log)
        assert pipeline_state.stage == "executor"
        # BUG-026 fix: executor receives task description + signal path
        session_mgr.send.assert_awaited_once()
        msg = session_mgr.send.call_args[0][1]
        assert "[TASK] Add hello world command" in msg
        assert "completion-integ-001.json" in msg

        # 2. executor completes → reviewer
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "Implemented hello world command"})
        with patch("lib.claude.summarize", new=AsyncMock(return_value="Added hello command")):
            await check_stage(pipeline_state, signal_reader, session_mgr, config, "executor", event_log)
        assert pipeline_state.stage == "reviewer"

        # 3. reviewer approves → merge
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "approve", "feedback": "LGTM"})
        await check_reviewer(pipeline_state, signal_reader, session_mgr, config, event_log)
        assert pipeline_state.stage == "merge"
        assert pipeline_state.review_verdict == "LGTM"

        # 4. merge → wiki (no worktree in test = skip to wiki)
        pipeline_state.worktree = None
        await do_merge(pipeline_state, config, event_log)
        assert pipeline_state.stage == "wiki"

        # 5. wiki → done
        with patch("lib.claude.document_task", new=AsyncMock(return_value=True)), \
             patch("harness.orchestrator.notify", new_callable=AsyncMock) as mock_notify:
            await do_wiki(pipeline_state, config, event_log)
        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None
        # Verify task_completed notification sent
        mock_notify.assert_awaited_once()
        assert mock_notify.call_args[0][0] == "task_completed"

    @pytest.mark.asyncio
    async def test_complex_task_full_pipeline(self, config, pipeline_state):
        """Complex task flows: classify → architect → executor → reviewer → merge → wiki → done."""
        from harness.orchestrator import classify_task, check_stage, check_reviewer, do_merge, do_wiki

        task = TaskSignal(task_id="integ-002", description="Refactor auth module")
        pipeline_state.activate(task)
        session_mgr = _make_session_mgr()
        event_log = _make_event_log()
        signal_reader = _make_signal_reader()

        # 1. classify → architect (complex)
        with patch("lib.claude.classify", new=AsyncMock(return_value="complex")):
            await classify_task(pipeline_state, session_mgr, config, event_log)
        assert pipeline_state.stage == "architect"
        session_mgr.send.assert_awaited_once()
        assert "[TASK]" in session_mgr.send.call_args[0][1]

        # 2. architect completes → executor (with context transfer)
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "Plan: refactor auth into 3 modules"})
        session_mgr.send.reset_mock()
        with patch("lib.claude.summarize", new=AsyncMock(return_value="Refactor plan summary")):
            await check_stage(pipeline_state, signal_reader, session_mgr, config, "architect", event_log)
        assert pipeline_state.stage == "executor"
        # Executor receives [CONTEXT] + [TASK] from architect output
        session_mgr.send.assert_awaited_once()
        sent = session_mgr.send.call_args[0][1]
        assert "[CONTEXT]" in sent
        assert "[TASK]" in sent

        # 3. executor completes → reviewer
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"output": "Refactored auth module"})
        session_mgr.send.reset_mock()
        with patch("lib.claude.summarize", new=AsyncMock(return_value="Executor summary")):
            await check_stage(pipeline_state, signal_reader, session_mgr, config, "executor", event_log)
        assert pipeline_state.stage == "reviewer"

        # 4. reviewer approves → merge
        signal_reader.check_stage_complete = AsyncMock(
            return_value={"verdict": "approved", "feedback": "Clean refactor"})
        await check_reviewer(pipeline_state, signal_reader, session_mgr, config, event_log)
        assert pipeline_state.stage == "merge"

        # 5. merge → wiki (no worktree)
        pipeline_state.worktree = None
        await do_merge(pipeline_state, config, event_log)
        assert pipeline_state.stage == "wiki"

        # 6. wiki → done
        with patch("lib.claude.document_task", new=AsyncMock(return_value=True)), \
             patch("harness.orchestrator.notify", new_callable=AsyncMock):
            await do_wiki(pipeline_state, config, event_log)
        assert pipeline_state.active_task is None


# ---------- Timeout notification (BUG-025 regression) ----------


class TestTimeoutNotification:
    """Tests that stage timeout sends a Discord notification before clearing state."""

    @pytest.mark.asyncio
    async def test_timeout_calls_notify_before_clear(self, config, pipeline_state):
        """_handle_stage_timeout calls notify() with stage_timeout event before clear_active."""
        from datetime import datetime, timezone, timedelta
        from harness.orchestrator import _handle_stage_timeout

        task = TaskSignal(task_id="timeout-001", description="Stuck task")
        pipeline_state.activate(task)
        pipeline_state.advance("executor", "executor")
        past = datetime.now(timezone.utc) - timedelta(hours=4)
        pipeline_state.stage_started_ts = past.isoformat()

        session_mgr = _make_session_mgr()
        session_mgr.sessions = {}
        event_log = _make_event_log()

        with patch("harness.orchestrator.notify", new_callable=AsyncMock) as mock_notify:
            await _handle_stage_timeout(pipeline_state, session_mgr, event_log, config)

        # notify called with correct event and content (before clear_active wiped state)
        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args[0]
        assert call_args[0] == "stage_timeout"
        assert call_args[1] == "executor"
        assert "timeout-001" in call_args[2]
        assert "executor" in call_args[2]

        # State was cleared after notify
        assert pipeline_state.active_task is None
        assert pipeline_state.stage is None

        # Event log also recorded
        event_log.record.assert_awaited_once()
        assert event_log.record.call_args[0][0] == "stage_timeout"
