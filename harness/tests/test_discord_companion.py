"""Tests for harness/discord_companion.py."""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.discord_companion import (
    DiscordCompanion,
    parse_caveman,
    parse_reply,
    parse_tell,
)
from harness.lib.claude import classify_target
from harness.lib.pipeline import PipelineState
from harness.lib.signals import TaskSignal


# ---------- Helpers ----------


def _make_companion(config, mutations=None, active_agents_fn=None):
    sr = MagicMock()
    sr.clear_escalation = MagicMock()
    return DiscordCompanion(
        config=config,
        pending_mutations=mutations if mutations is not None else [],
        signal_reader=sr,
        active_agents_fn=active_agents_fn,
    )


def _make_session_mgr():
    sm = MagicMock()
    sm.send = AsyncMock()
    sm.inject_caveman_update = AsyncMock()
    sm.sessions = {
        "architect": MagicMock(),
        "executor": MagicMock(),
        "reviewer": MagicMock(),
    }
    return sm


# ---------- TestParseCaveman ----------


class TestParseCaveman:
    def test_no_args_returns_status(self):
        assert parse_caveman("") == ("status", "")
        assert parse_caveman("   ") == ("status", "")

    def test_status_keyword(self):
        assert parse_caveman("status") == ("status", "")

    def test_reset_keyword(self):
        assert parse_caveman("reset") == ("reset", "")

    def test_valid_level_alone_is_backward_compat_all(self):
        assert parse_caveman("full") == ("all", "full")
        assert parse_caveman("ultra") == ("all", "ultra")
        assert parse_caveman("off") == ("all", "off")

    def test_agent_and_level(self):
        assert parse_caveman("executor ultra") == ("executor", "ultra")
        assert parse_caveman("architect off") == ("architect", "off")

    def test_unknown_word_alone_treated_as_command(self):
        # not a valid level, so treated as a bare command token
        assert parse_caveman("fakecommand") == ("fakecommand", "")

    def test_multiple_spaces_between_agent_and_level(self):
        assert parse_caveman("executor   ultra") == ("executor", "ultra")


# ---------- TestParseTell ----------


class TestParseTell:
    def test_valid_args(self):
        assert parse_tell("executor do the thing") == ("executor", "do the thing")

    def test_missing_message_returns_empty(self):
        assert parse_tell("executor") == ("", "")

    def test_empty_string_returns_empty(self):
        assert parse_tell("") == ("", "")

    def test_preserves_multi_word_message(self):
        agent, msg = parse_tell("architect please review this carefully")
        assert agent == "architect"
        assert msg == "please review this carefully"


# ---------- TestParseReply ----------


class TestParseReply:
    def test_valid_args(self):
        assert parse_reply("task-001 looks good") == ("task-001", "looks good")

    def test_missing_response_returns_empty(self):
        assert parse_reply("task-001") == ("", "")

    def test_empty_string_returns_empty(self):
        assert parse_reply("") == ("", "")

    def test_preserves_multi_word_response(self):
        tid, resp = parse_reply("task-abc proceed with plan B")
        assert tid == "task-abc"
        assert resp == "proceed with plan B"


# ---------- TestHandleMessage ----------


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_tell_happy_path(self, config):
        mutations = []
        dc = _make_companion(config, mutations)
        resp = await dc.handle_message("!tell", "executor do the thing")
        assert resp == "Feedback queued for executor."
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_tell_missing_args(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_message("!tell", "executor")
        assert resp == "Usage: !tell <agent> <message>"

    @pytest.mark.asyncio
    async def test_tell_unknown_agent(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_message("!tell", "ghost do the thing")
        assert "Unknown agent 'ghost'" in resp
        assert "architect" in resp or "executor" in resp

    @pytest.mark.asyncio
    async def test_reply_happy_path(self, config):
        mutations = []
        dc = _make_companion(config, mutations)
        resp = await dc.handle_message("!reply", "task-001 looks good")
        assert resp == "Reply queued for task-001."
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_reply_missing_args(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_message("!reply", "task-001")
        assert resp == "Usage: !reply <task_id> <response>"

    @pytest.mark.asyncio
    async def test_reply_invalid_task_id_path_traversal(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_message("!reply", "../../evil looks good")
        assert "Invalid task_id" in resp

    @pytest.mark.asyncio
    async def test_caveman_delegates(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_message("!caveman", "status")
        assert "Caveman levels" in resp

    @pytest.mark.asyncio
    async def test_status_command(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_message("!status", "")
        assert resp is not None
        assert "!caveman" in resp

    @pytest.mark.asyncio
    async def test_unknown_command_returns_none(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_message("!unknown", "whatever")
        assert resp is None

    @pytest.mark.asyncio
    async def test_project_command_dispatched(self, config):
        dc = _make_companion(config)
        handler = AsyncMock(return_value="project response")
        dc._project_handler = handler
        dc._project_commands = {"!deploy": "Deploy the app"}
        resp = await dc.handle_message("!deploy", "staging")
        assert resp == "project response"
        handler.assert_awaited_once_with("!deploy", "staging", config.signal_dir)

    @pytest.mark.asyncio
    async def test_project_command_not_in_registry_returns_none(self, config):
        dc = _make_companion(config)
        dc._project_handler = AsyncMock(return_value="should not call")
        dc._project_commands = {"!deploy": "Deploy"}
        resp = await dc.handle_message("!unknown", "args")
        assert resp is None


# ---------- TestHandleCaveman ----------


class TestHandleCaveman:
    def test_no_args_returns_status(self, config):
        dc = _make_companion(config)
        resp = dc._handle_caveman("")
        assert "Caveman levels" in resp

    def test_status_returns_formatted_status(self, config):
        dc = _make_companion(config)
        dc.config.caveman.set_agent("executor", "ultra")
        resp = dc._handle_caveman("status")
        assert "Caveman levels" in resp
        assert "architect" in resp
        assert "executor" in resp
        assert "ultra" in resp
        assert "(runtime)" in resp

    def test_reset_resets_to_defaults(self, config):
        dc = _make_companion(config)
        dc.config.caveman.set_agent("executor", "ultra")
        assert dc.config.caveman.level_for("executor") == "ultra"  # precondition
        resp = dc._handle_caveman("reset")
        assert "reset" in resp.lower()
        assert dc.config.caveman.level_for("executor") == "full"  # back to config default

    def test_full_sets_all_agents(self, config):
        mutations = []
        dc = _make_companion(config, mutations)
        resp = dc._handle_caveman("full")
        assert "All agents set to caveman full" in resp
        assert config.caveman.level_for("architect") == "full"  # was "off" initially

    def test_valid_per_agent_level(self, config):
        mutations = []
        dc = _make_companion(config, mutations)
        resp = dc._handle_caveman("executor ultra")
        assert "executor caveman level -> ultra" in resp
        assert len(mutations) == 1
        assert config.caveman.level_for("executor") == "ultra"

    def test_invalid_level_returns_error(self, config):
        dc = _make_companion(config)
        resp = dc._handle_caveman("executor badlevel")
        assert "Unknown level 'badlevel'" in resp

    def test_unknown_agent_returns_error(self, config):
        dc = _make_companion(config)
        resp = dc._handle_caveman("fakeagent full")
        assert "Unknown agent 'fakeagent'" in resp


# ---------- TestMutationExecution ----------


class TestMutationExecution:
    @pytest.mark.asyncio
    async def test_tell_mutation_calls_session_send(self, config, pipeline_state):
        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!tell", "executor deploy now")
        sm = _make_session_mgr()
        await mutations[0](pipeline_state, sm)
        sm.send.assert_awaited_once_with("executor", "[OPERATOR] deploy now")

    @pytest.mark.asyncio
    async def test_tell_mutation_late_binding_safe(self, config, pipeline_state):
        """Two !tell commands must produce independent mutations (no late-binding bug)."""
        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!tell", "executor first message")
        await dc.handle_message("!tell", "architect second message")
        sm = _make_session_mgr()
        await mutations[0](pipeline_state, sm)
        await mutations[1](pipeline_state, sm)
        calls = [c.args for c in sm.send.await_args_list]
        assert ("executor", "[OPERATOR] first message") in calls
        assert ("architect", "[OPERATOR] second message") in calls

    @pytest.mark.asyncio
    async def test_caveman_mutation_calls_inject_caveman_update(self, config, pipeline_state):
        mutations = []
        dc = _make_companion(config, mutations)
        dc._handle_caveman("executor ultra")
        sm = _make_session_mgr()
        await mutations[0](pipeline_state, sm)
        sm.inject_caveman_update.assert_awaited_once_with("executor", "ultra")

    @pytest.mark.asyncio
    async def test_reply_mutation_calls_apply_reply(self, config, pipeline_state):
        """Reply mutation invokes _apply_reply; verify it doesn't crash with default state."""
        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-xyz looks good")
        sm = _make_session_mgr()
        # pipeline_state.active_task is None by default — _apply_reply logs a warning and returns
        await mutations[0](pipeline_state, sm)
        # No exception raised; send not called because active_task doesn't match
        sm.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_reply_happy_path(self, config):
        """_apply_reply injects reply into original agent and resumes pipeline stage."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_wait")

        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-xyz approve it")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        sm.send.assert_awaited_once_with("executor", "[OPERATOR REPLY] approve it")
        assert state.stage == "executor"
        dc.signal_reader.clear_escalation.assert_called_once_with("task-xyz")

    @pytest.mark.asyncio
    async def test_apply_reply_wrong_stage_no_effect(self, config):
        """_apply_reply does nothing when stage is not an escalation stage."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        # stage is "classify" after activate — not escalation_wait
        state.advance("executor")

        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-xyz approve it")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        sm.send.assert_not_awaited()
        assert state.stage == "executor"  # unchanged

    @pytest.mark.asyncio
    async def test_apply_reply_shelved_task_stores_reply(self, config):
        """_apply_reply resolves escalation on shelved task and stores pending reply."""
        task1 = TaskSignal(task_id="task-shelved", description="Shelved task")
        task2 = TaskSignal(task_id="task-active", description="Active task")
        state = PipelineState()
        state.activate(task1)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_wait")
        state.shelve()
        state.activate(task2)

        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-shelved go ahead")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        # Shelved task escalation resolved in-place
        assert state.shelved_tasks[0]["stage"] == "executor"
        assert state.shelved_tasks[0]["pending_operator_reply"] == "[OPERATOR REPLY] go ahead"
        assert state.shelved_tasks[0]["pre_escalation_stage"] is None
        dc.signal_reader.clear_escalation.assert_called_once_with("task-shelved")
        sm.send.assert_not_awaited()  # not injected yet — task still shelved

    @pytest.mark.asyncio
    async def test_apply_reply_unknown_task_logs_warning(self, config):
        """_apply_reply logs warning when task_id not found in active or shelved."""
        task = TaskSignal(task_id="task-active", description="Active task")
        state = PipelineState()
        state.activate(task)
        state.advance("executor")

        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-unknown go ahead")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        sm.send.assert_not_awaited()
        dc.signal_reader.clear_escalation.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_reply_dead_session_still_resumes(self, config):
        """_apply_reply resumes pipeline even when the original agent session is dead."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_wait")

        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-xyz approve it")

        # Session manager has no sessions — executor is dead
        sm = _make_session_mgr()
        sm.sessions = {}
        await mutations[0](state, sm)

        sm.send.assert_not_awaited()
        assert state.stage == "executor"  # still resumed
        dc.signal_reader.clear_escalation.assert_called_once_with("task-xyz")


# ---------- TestLoadProjectCommands ----------


class TestLoadProjectCommands:
    def test_loads_commands_and_handler_from_module(self, config):
        """_load_project_commands populates _project_commands and _project_handler."""
        fake_module = MagicMock()
        fake_module.COMMANDS = {"!deploy": "Deploy the app"}
        fake_handler = AsyncMock(return_value="deployed")
        fake_module.handle_command = fake_handler

        config.commands_module = "config.commands_module"
        with patch("importlib.import_module", return_value=fake_module) as mock_import:
            dc = _make_companion(config)

        mock_import.assert_called_once_with("config.commands_module")
        assert dc._project_commands == {"!deploy": "Deploy the app"}
        assert dc._project_handler is fake_handler


# ---------- TestHandleRawMessage ----------


class TestHandleRawMessage:
    @pytest.mark.asyncio
    async def test_prefix_command_delegates_to_handle_message(self, config):
        mutations = []
        dc = _make_companion(config, mutations)
        resp = await dc.handle_raw_message("!tell executor do the thing")
        assert resp == "Feedback queued for executor."
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_prefix_command_with_no_args(self, config):
        dc = _make_companion(config)
        resp = await dc.handle_raw_message("!status")
        assert resp is not None
        assert "!caveman" in resp

    @pytest.mark.asyncio
    async def test_nl_message_routes_to_single_agent(self, config):
        mutations = []
        dc = _make_companion(config, mutations, active_agents_fn=lambda: ["executor"])
        with patch("lib.claude.classify_target") as mock_ct:
            resp = await dc.handle_raw_message("focus on error handling")
        assert resp == "Message routed to executor."
        assert len(mutations) == 1
        mock_ct.assert_not_called()  # single agent, no classify needed

    @pytest.mark.asyncio
    async def test_nl_message_strips_whitespace(self, config):
        mutations = []
        dc = _make_companion(config, mutations, active_agents_fn=lambda: ["executor"])
        resp = await dc.handle_raw_message("  focus on tests  ")
        assert resp == "Message routed to executor."

    @pytest.mark.asyncio
    async def test_empty_message_routes_as_nl(self, config):
        dc = _make_companion(config, active_agents_fn=lambda: [])
        resp = await dc.handle_raw_message("  ")
        assert resp == "No active agents. Submit a task first."


# ---------- TestRouteNaturalLanguage ----------


class TestRouteNaturalLanguage:
    @pytest.mark.asyncio
    async def test_no_active_agents(self, config):
        dc = _make_companion(config, active_agents_fn=lambda: [])
        resp = await dc._route_natural_language("do something")
        assert "No active agents" in resp

    @pytest.mark.asyncio
    async def test_single_agent_routes_directly(self, config):
        mutations = []
        dc = _make_companion(config, mutations, active_agents_fn=lambda: ["architect"])
        resp = await dc._route_natural_language("review the design")
        assert resp == "Message routed to architect."
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_single_agent_mutation_sends_operator_prefix(self, config, pipeline_state):
        mutations = []
        dc = _make_companion(config, mutations, active_agents_fn=lambda: ["executor"])
        await dc._route_natural_language("fix the bug")
        sm = _make_session_mgr()
        await mutations[0](pipeline_state, sm)
        sm.send.assert_awaited_once_with("executor", "[OPERATOR] fix the bug")

    @pytest.mark.asyncio
    async def test_multiple_agents_uses_classify(self, config):
        mutations = []
        dc = _make_companion(config, mutations, active_agents_fn=lambda: ["architect", "executor"])
        with patch("lib.claude.classify_target", new_callable=AsyncMock, return_value="executor"):
            resp = await dc._route_natural_language("focus on error handling")
        assert resp == "Message routed to executor."
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_multiple_agents_ambiguous_returns_prompt(self, config):
        dc = _make_companion(config, active_agents_fn=lambda: ["architect", "executor"])
        with patch("lib.claude.classify_target", new_callable=AsyncMock, return_value=None):
            resp = await dc._route_natural_language("do something")
        assert "Who do you mean?" in resp
        assert "architect" in resp
        assert "executor" in resp

    @pytest.mark.asyncio
    async def test_mutation_late_binding_safe(self, config, pipeline_state):
        """Two NL messages produce independent mutations."""
        mutations = []
        dc = _make_companion(config, mutations, active_agents_fn=lambda: ["executor"])
        await dc._route_natural_language("first message")
        await dc._route_natural_language("second message")
        sm = _make_session_mgr()
        await mutations[0](pipeline_state, sm)
        await mutations[1](pipeline_state, sm)
        calls = [c.args for c in sm.send.await_args_list]
        assert ("executor", "[OPERATOR] first message") in calls
        assert ("executor", "[OPERATOR] second message") in calls


# ---------- TestActiveAgentsFn ----------


class TestActiveAgentsFn:
    def test_default_uses_config_agents(self, config):
        dc = _make_companion(config)
        agents = dc._active_agents_fn()
        assert set(agents) == set(config.agents.keys())

    def test_custom_fn_overrides_default(self, config):
        dc = _make_companion(config, active_agents_fn=lambda: ["custom-agent"])
        agents = dc._active_agents_fn()
        assert agents == ["custom-agent"]


# ---------- TestClassifyTarget ----------


class TestClassifyTarget:
    @pytest.mark.asyncio
    async def test_returns_matching_agent(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="executor"):
            result = await classify_target("fix the bug", ["architect", "executor"], config)
        assert result == "executor"

    @pytest.mark.asyncio
    async def test_case_insensitive_match(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="EXECUTOR"):
            result = await classify_target("fix it", ["architect", "executor"], config)
        assert result == "executor"

    @pytest.mark.asyncio
    async def test_ambiguous_returns_none(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="ambiguous"):
            result = await classify_target("do something", ["architect", "executor"], config)
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_none(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="debugger"):
            result = await classify_target("debug this", ["architect", "executor"], config)
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value=None):
            result = await classify_target("do the thing", ["executor"], config)
        assert result is None

    @pytest.mark.asyncio
    async def test_uses_haiku_model(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="executor") as mock:
            await classify_target("fix it", ["executor"], config)
        assert mock.call_args.kwargs.get("model") == "haiku"

    @pytest.mark.asyncio
    async def test_uses_classify_target_timeout(self, config):
        config.timeouts["classify_target"] = 42
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="executor") as mock:
            await classify_target("fix it", ["executor"], config)
        # timeout is the 3rd positional arg
        assert mock.call_args.args[2] == 42


# ---------- TestRunClaudeModelParam ----------


class TestRunClaudeModelParam:
    @pytest.mark.asyncio
    async def test_model_param_added_to_command(self, config):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"output", b""))
            mock_exec.return_value = mock_proc
            from harness.lib.claude import _run_claude
            await _run_claude("sys", "user", 10, "test", config, model="haiku")
        cmd_args = mock_exec.call_args.args
        assert "--model" in cmd_args
        idx = cmd_args.index("--model")
        assert cmd_args[idx + 1] == "haiku"

    @pytest.mark.asyncio
    async def test_no_model_param_when_none(self, config):
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"output", b""))
            mock_exec.return_value = mock_proc
            from harness.lib.claude import _run_claude
            await _run_claude("sys", "user", 10, "test", config)
        cmd_args = mock_exec.call_args.args
        assert "--model" not in cmd_args
