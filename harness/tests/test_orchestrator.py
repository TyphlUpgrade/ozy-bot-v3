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
    reader.check_stage_complete = AsyncMock(return_value=None)
    return reader


def _make_proc(returncode=0, stdout=b"", stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
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
            await classify_task(pipeline_state, session_mgr, config)

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
            await classify_task(pipeline_state, session_mgr, config)

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
            await classify_task(state, session_mgr, config)

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
            await classify_task(state, session_mgr, config)

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

        await check_stage(pipeline_state, signal_reader, "architect")

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

        await check_stage(pipeline_state, signal_reader, "executor")

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

        await check_stage(pipeline_state, signal_reader, "reviewer")

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

        await check_stage(pipeline_state, signal_reader, "executor")

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

        await check_stage(pipeline_state, signal_reader, "executor")

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

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config)

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

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config)

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

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config)

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
            await check_reviewer(pipeline_state, signal_reader, session_mgr, config)

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

        await check_reviewer(pipeline_state, signal_reader, session_mgr, config)

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
            await check_reviewer(pipeline_state, signal_reader, session_mgr, config)

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
            await check_reviewer(pipeline_state, signal_reader, session_mgr, config)

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

        await do_merge(pipeline_state, config)

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
            await do_merge(pipeline_state, config)

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
                await do_merge(pipeline_state, config)

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
                await do_merge(pipeline_state, config)

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
            await do_wiki(pipeline_state, config)

        assert captured_ids == ["task-999"]
        assert pipeline_state.active_task is None  # cleared after

    @pytest.mark.asyncio
    async def test_successful_wiki_clears_active(self, config, pipeline_state):
        """Successful wiki documentation clears active task."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Fix it")
        pipeline_state.activate(task)

        with patch("lib.claude.document_task", new=AsyncMock(return_value=True)):
            await do_wiki(pipeline_state, config)

        assert pipeline_state.active_task is None

    @pytest.mark.asyncio
    async def test_wiki_failure_still_clears_active(self, config, pipeline_state):
        """Failed wiki documentation still clears active task (non-blocking failure)."""
        from harness.orchestrator import do_wiki

        task = TaskSignal(task_id="task-001", description="Fix it")
        pipeline_state.activate(task)

        with patch("lib.claude.document_task", new=AsyncMock(return_value=False)):
            await do_wiki(pipeline_state, config)

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
