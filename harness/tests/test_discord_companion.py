"""Tests for harness/discord_companion.py."""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.discord_companion import (
    ACCUM_WINDOW,
    AGENT_IDENTITIES,
    DIALOGUE_CONFIRM_WORDS,
    DiscordCompanion,
    _ANNOUNCE_STAGES,
    _CONTROL_PATTERN,
    _accum_buffer,
    _accum_timers,
    _apply_pause,
    _do_update,
    _flush_accumulated,
    _infer_agent_from_response,
    _processing_lock,
    _send_response,
    _strip_mention,
    _swap_reactions,
    announce_stage,
    parse_caveman,
    parse_reply,
    parse_tell,
)
from harness.lib.claude import classify_intent, classify_resolution, classify_target
from harness.lib.pipeline import PipelineState
from harness.lib.signals import TaskSignal


# ---------- Helpers ----------


def _make_companion(config, mutations=None, active_agents_fn=None, pipeline_stage_fn=None,
                    pipeline_paused_fn=None, shutdown_event=None):
    sr = MagicMock()
    sr.clear_escalation = MagicMock()
    return DiscordCompanion(
        config=config,
        pending_mutations=mutations if mutations is not None else [],
        signal_reader=sr,
        active_agents_fn=active_agents_fn,
        pipeline_stage_fn=pipeline_stage_fn,
        pipeline_paused_fn=pipeline_paused_fn,
        shutdown_event=shutdown_event,
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
        with patch("lib.claude.classify_intent", new_callable=AsyncMock, return_value="feedback"), \
             patch("lib.claude.classify_target") as mock_ct:
            resp = await dc.handle_raw_message("focus on error handling")
        assert resp == "Message routed to executor."
        assert len(mutations) == 1
        mock_ct.assert_not_called()  # single agent, no classify needed

    @pytest.mark.asyncio
    async def test_nl_message_strips_whitespace(self, config):
        mutations = []
        dc = _make_companion(config, mutations, active_agents_fn=lambda: ["executor"])
        with patch("lib.claude.classify_intent", new_callable=AsyncMock, return_value="feedback"):
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


# ---------- TestDialogueConfirmWords ----------


class TestDialogueConfirmWords:
    def test_is_frozenset(self):
        assert isinstance(DIALOGUE_CONFIRM_WORDS, frozenset)

    def test_contains_expected_words(self):
        for word in ("yes", "y", "confirm", "go", "approved", "ok", "okay", "proceed"):
            assert word in DIALOGUE_CONFIRM_WORDS


# ---------- TestEscalationDialogueRouting ----------


class TestEscalationDialogueRouting:
    @pytest.mark.asyncio
    async def test_nl_during_escalation_wait_routes_to_pre_esc_agent(self, config):
        """NL message during escalation_wait routes to blocked agent, classify_target not called."""
        mutations = []
        dc = _make_companion(
            config, mutations,
            active_agents_fn=lambda: ["architect", "executor"],
            pipeline_stage_fn=lambda: ("escalation_wait", "executor"),
        )
        with patch("lib.claude.classify_target") as mock_ct:
            resp = await dc._route_natural_language("try approach B instead")
        assert "executor" in resp
        assert "escalation dialogue" in resp
        assert len(mutations) == 1
        mock_ct.assert_not_called()

    @pytest.mark.asyncio
    async def test_nl_during_escalation_dialogue_routes_to_pre_esc_agent(self, config):
        """NL message during escalation_dialogue also routes directly."""
        mutations = []
        dc = _make_companion(
            config, mutations,
            active_agents_fn=lambda: ["architect", "executor"],
            pipeline_stage_fn=lambda: ("escalation_dialogue", "executor"),
        )
        resp = await dc._route_natural_language("what about the timeout?")
        assert "executor" in resp
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_nl_during_normal_stage_uses_classify(self, config):
        """Non-escalation NL uses normal classify_target flow."""
        mutations = []
        dc = _make_companion(
            config, mutations,
            active_agents_fn=lambda: ["executor"],
            pipeline_stage_fn=lambda: ("executor", None),
        )
        resp = await dc._route_natural_language("focus on error handling")
        assert resp == "Message routed to executor."

    @pytest.mark.asyncio
    async def test_nl_escalation_no_pre_esc_agent_falls_through(self, config):
        """Escalation stage with no pre_escalation_agent falls through to normal routing."""
        mutations = []
        dc = _make_companion(
            config, mutations,
            active_agents_fn=lambda: ["executor"],
            pipeline_stage_fn=lambda: ("escalation_wait", None),
        )
        resp = await dc._route_natural_language("fix the bug")
        assert resp == "Message routed to executor."


# ---------- TestDialogueMessageMutation ----------


class TestDialogueMessageMutation:
    @pytest.mark.asyncio
    async def test_dialogue_message_sends_to_agent(self, config):
        """Mutation delivers [OPERATOR] prefixed message to blocked agent."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_wait")

        mutations = []
        dc = _make_companion(
            config, mutations,
            pipeline_stage_fn=lambda: ("escalation_wait", "executor"),
        )
        await dc._route_natural_language("try approach B")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        sm.send.assert_awaited_once_with("executor", "[OPERATOR] try approach B")

    @pytest.mark.asyncio
    async def test_dialogue_message_transitions_wait_to_dialogue(self, config):
        """Mutation transitions from escalation_wait to escalation_dialogue."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_wait")

        mutations = []
        dc = _make_companion(
            config, mutations,
            pipeline_stage_fn=lambda: ("escalation_wait", "executor"),
        )
        await dc._route_natural_language("try approach B")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        assert state.stage == "escalation_dialogue"

    @pytest.mark.asyncio
    async def test_dialogue_message_refreshes_timestamp(self, config):
        """Mutation sets dialogue_last_message_ts."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_wait")

        mutations = []
        dc = _make_companion(
            config, mutations,
            pipeline_stage_fn=lambda: ("escalation_wait", "executor"),
        )
        await dc._route_natural_language("try approach B")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        assert state.dialogue_last_message_ts is not None
        assert state.dialogue_last_message == "try approach B"

    @pytest.mark.asyncio
    async def test_dialogue_message_clears_pending_confirmation(self, config):
        """New dialogue message clears pending confirmation flag."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_dialogue")
        state.dialogue_pending_confirmation = True

        mutations = []
        dc = _make_companion(
            config, mutations,
            pipeline_stage_fn=lambda: ("escalation_dialogue", "executor"),
        )
        await dc._route_natural_language("actually wait, what about X?")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        assert state.dialogue_pending_confirmation is False

    @pytest.mark.asyncio
    async def test_dialogue_confirmation_yes_resumes_pipeline(self, config):
        """'yes' during pending confirmation resumes pipeline."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_dialogue")
        state.dialogue_pending_confirmation = True

        mutations = []
        dc = _make_companion(
            config, mutations,
            pipeline_stage_fn=lambda: ("escalation_dialogue", "executor"),
        )
        await dc._route_natural_language("yes")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        assert state.stage == "executor"
        assert state.dialogue_pending_confirmation is False
        assert state.dialogue_last_message_ts is None
        dc.signal_reader.clear_escalation.assert_called_once_with("task-xyz")

    @pytest.mark.asyncio
    async def test_dialogue_confirmation_clears_escalation_signal(self, config):
        """Confirmation calls signal_reader.clear_escalation."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_dialogue")
        state.dialogue_pending_confirmation = True

        mutations = []
        dc = _make_companion(
            config, mutations,
            pipeline_stage_fn=lambda: ("escalation_dialogue", "executor"),
        )
        await dc._route_natural_language("confirm")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        dc.signal_reader.clear_escalation.assert_called_once_with("task-xyz")

    @pytest.mark.asyncio
    async def test_apply_reply_accepts_escalation_dialogue(self, config):
        """!reply works during escalation_dialogue stage."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_dialogue")

        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-xyz go with plan B")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        sm.send.assert_awaited_once_with("executor", "[OPERATOR REPLY] go with plan B")
        assert state.stage == "executor"

    @pytest.mark.asyncio
    async def test_apply_reply_shelved_dialogue_accepted(self, config):
        """!reply on shelved task in escalation_dialogue is accepted."""
        task1 = TaskSignal(task_id="task-shelved", description="Shelved")
        task2 = TaskSignal(task_id="task-active", description="Active")
        state = PipelineState()
        state.activate(task1)
        state.pre_escalation_stage = "executor"
        state.pre_escalation_agent = "executor"
        state.advance("escalation_dialogue")
        state.shelve()
        state.activate(task2)

        mutations = []
        dc = _make_companion(config, mutations)
        await dc.handle_message("!reply", "task-shelved go ahead")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        assert state.shelved_tasks[0]["stage"] == "executor"
        dc.signal_reader.clear_escalation.assert_called_once_with("task-shelved")

    @pytest.mark.asyncio
    async def test_dialogue_no_pre_esc_agent_drops_message(self, config):
        """Mutation drops message when pre_escalation_agent is None."""
        task = TaskSignal(task_id="task-xyz", description="Work")
        state = PipelineState()
        state.activate(task)
        state.advance("escalation_wait")
        # pre_escalation_agent is None

        mutations = []
        dc = _make_companion(
            config, mutations,
            pipeline_stage_fn=lambda: ("escalation_wait", "executor"),
        )
        await dc._route_natural_language("some message")
        sm = _make_session_mgr()
        await mutations[0](state, sm)

        sm.send.assert_not_awaited()


# ---------- TestClassifyResolution ----------


class TestClassifyResolution:
    @pytest.mark.asyncio
    async def test_returns_resolution(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="resolution"):
            result = await classify_resolution("go with approach B", config)
        assert result == "resolution"

    @pytest.mark.asyncio
    async def test_returns_continuation(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="continuation"):
            result = await classify_resolution("what about X?", config)
        assert result == "continuation"

    @pytest.mark.asyncio
    async def test_timeout_defaults_continuation(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value=None):
            result = await classify_resolution("anything", config)
        assert result == "continuation"

    @pytest.mark.asyncio
    async def test_uses_haiku_model(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="resolution") as mock:
            await classify_resolution("go ahead", config)
        assert mock.call_args.kwargs.get("model") == "haiku"

    @pytest.mark.asyncio
    async def test_unexpected_value_defaults_continuation(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="maybe"):
            result = await classify_resolution("something", config)
        assert result == "continuation"

    @pytest.mark.asyncio
    async def test_case_insensitive(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="RESOLUTION"):
            result = await classify_resolution("approved", config)
        assert result == "resolution"


# ---------- TestControlPreFilter ----------


class TestControlPreFilter:
    def test_stop_matches(self):
        assert _CONTROL_PATTERN.match("stop")

    def test_pause_the_pipeline_matches(self):
        assert _CONTROL_PATTERN.match("pause the pipeline")

    def test_halt_matches(self):
        assert _CONTROL_PATTERN.match("halt")

    def test_resume_matches(self):
        assert _CONTROL_PATTERN.match("resume")

    def test_unpause_matches(self):
        assert _CONTROL_PATTERN.match("unpause")

    def test_status_matches(self):
        assert _CONTROL_PATTERN.match("status")

    def test_case_insensitive(self):
        assert _CONTROL_PATTERN.match("STOP")
        assert _CONTROL_PATTERN.match("Pause The Pipeline")

    def test_with_punctuation(self):
        assert _CONTROL_PATTERN.match("stop!")
        assert _CONTROL_PATTERN.match("pause.")

    def test_embedded_control_word_no_match(self):
        """'tell executor to stop' should NOT match the pre-filter."""
        assert _CONTROL_PATTERN.match("tell executor to stop") is None

    def test_longer_sentence_no_match(self):
        assert _CONTROL_PATTERN.match("please stop the pipeline now") is None

    def test_stop_the_harness_matches(self):
        assert _CONTROL_PATTERN.match("stop the harness")

    def test_resume_everything_matches(self):
        assert _CONTROL_PATTERN.match("resume everything")


# ---------- TestHandleControl ----------


class TestHandleControl:
    @pytest.mark.asyncio
    async def test_stop_queues_pause_mutation(self, config):
        mutations = []
        dc = _make_companion(config, mutations)
        resp = dc._handle_control("stop")
        assert "pausing" in resp.lower()
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_pause_mutation_sets_paused_true(self, config, pipeline_state):
        mutations = []
        dc = _make_companion(config, mutations)
        dc._handle_control("pause")
        sm = _make_session_mgr()
        await mutations[0](pipeline_state, sm)
        assert pipeline_state.paused is True

    @pytest.mark.asyncio
    async def test_resume_mutation_sets_paused_false(self, config, pipeline_state):
        mutations = []
        dc = _make_companion(config, mutations)
        pipeline_state.paused = True
        dc._handle_control("resume")
        sm = _make_session_mgr()
        await mutations[0](pipeline_state, sm)
        assert pipeline_state.paused is False

    def test_status_returns_status_text(self, config):
        dc = _make_companion(config)
        resp = dc._handle_control("status")
        assert "harness" in resp.lower()
        assert "caveman" in resp.lower()


# ---------- TestHandleNewTask ----------


class TestHandleNewTask:
    @pytest.mark.asyncio
    async def test_creates_task_signal_file(self, config):
        dc = _make_companion(config)
        resp = await dc._handle_new_task("fix the auth bug in broker.py")
        assert "Task created:" in resp
        assert "discord-" in resp
        # Verify signal file written
        task_files = list(config.task_dir.glob("discord-*.json"))
        assert len(task_files) == 1

    @pytest.mark.asyncio
    async def test_task_signal_content(self, config):
        import json
        dc = _make_companion(config)
        await dc._handle_new_task("add retry logic to the fetcher")
        task_files = list(config.task_dir.glob("discord-*.json"))
        data = json.loads(task_files[0].read_text())
        assert data["description"] == "add retry logic to the fetcher"
        assert data["source"] == "discord"

    @pytest.mark.asyncio
    async def test_truncates_long_description_in_response(self, config):
        dc = _make_companion(config)
        long_msg = "x" * 200
        resp = await dc._handle_new_task(long_msg)
        assert len(resp) < 200  # response is truncated


# ---------- TestClassifyIntent ----------


class TestClassifyIntent:
    @pytest.mark.asyncio
    async def test_returns_feedback(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="feedback"):
            result = await classify_intent("focus on error handling", True, config)
        assert result == "feedback"

    @pytest.mark.asyncio
    async def test_returns_new_task(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="new_task"):
            result = await classify_intent("fix the auth bug in broker.py", False, config)
        assert result == "new_task"

    @pytest.mark.asyncio
    async def test_timeout_defaults_feedback(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value=None):
            result = await classify_intent("anything", True, config)
        assert result == "feedback"

    @pytest.mark.asyncio
    async def test_unexpected_value_defaults_feedback(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="unknown"):
            result = await classify_intent("something", True, config)
        assert result == "feedback"

    @pytest.mark.asyncio
    async def test_uses_haiku_model(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="feedback") as mock:
            await classify_intent("fix it", True, config)
        assert mock.call_args.kwargs.get("model") == "haiku"

    @pytest.mark.asyncio
    async def test_includes_active_task_context(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="feedback") as mock:
            await classify_intent("fix it", True, config)
        system_prompt = mock.call_args.args[0]
        assert "IS an active task" in system_prompt

    @pytest.mark.asyncio
    async def test_includes_no_active_task_context(self, config):
        with patch("harness.lib.claude._run_claude", new_callable=AsyncMock, return_value="new_task") as mock:
            await classify_intent("fix the bug", False, config)
        system_prompt = mock.call_args.args[0]
        assert "NO active task" in system_prompt


# ---------- TestThreeWayDispatch ----------


class TestThreeWayDispatch:
    @pytest.mark.asyncio
    async def test_control_word_dispatches_to_handle_control(self, config):
        """'stop' goes to _handle_control, not classify_intent."""
        mutations = []
        dc = _make_companion(config, mutations)
        with patch("lib.claude.classify_intent") as mock_ci:
            resp = await dc.handle_raw_message("stop")
        mock_ci.assert_not_called()
        assert "pausing" in resp.lower()

    @pytest.mark.asyncio
    async def test_escalation_skips_classify_intent(self, config):
        """During escalation, messages skip classify_intent."""
        mutations = []
        dc = _make_companion(
            config, mutations,
            active_agents_fn=lambda: ["executor"],
            pipeline_stage_fn=lambda: ("escalation_wait", "executor"),
        )
        with patch("lib.claude.classify_intent") as mock_ci:
            resp = await dc.handle_raw_message("try approach B")
        mock_ci.assert_not_called()
        assert "escalation dialogue" in resp

    @pytest.mark.asyncio
    async def test_feedback_intent_routes_to_agent(self, config):
        mutations = []
        dc = _make_companion(
            config, mutations,
            active_agents_fn=lambda: ["executor"],
            pipeline_stage_fn=lambda: ("executor", None),
        )
        with patch("lib.claude.classify_intent", new_callable=AsyncMock, return_value="feedback"):
            resp = await dc.handle_raw_message("focus on error handling")
        assert resp == "Message routed to executor."

    @pytest.mark.asyncio
    async def test_new_task_intent_creates_signal(self, config):
        dc = _make_companion(
            config,
            pipeline_stage_fn=lambda: (None, None),
        )
        with patch("lib.claude.classify_intent", new_callable=AsyncMock, return_value="new_task"):
            resp = await dc.handle_raw_message("fix the auth bug in broker.py")
        assert "Task created:" in resp
        task_files = list(config.task_dir.glob("discord-*.json"))
        assert len(task_files) == 1


# ---------- TestStatusShowsPaused ----------


class TestStatusShowsPaused:
    def test_status_shows_paused(self, config):
        dc = _make_companion(
            config,
            pipeline_stage_fn=lambda: ("executor", None),
            pipeline_paused_fn=lambda: True,
        )
        resp = dc._format_status()
        assert "PAUSED" in resp
        assert "executor" in resp
        assert "resume" in resp.lower()

    def test_status_shows_running(self, config):
        dc = _make_companion(
            config,
            pipeline_stage_fn=lambda: ("executor", None),
            pipeline_paused_fn=lambda: False,
        )
        resp = dc._format_status()
        assert "running" in resp.lower()
        assert "executor" in resp

    def test_status_shows_idle(self, config):
        dc = _make_companion(
            config,
            pipeline_stage_fn=lambda: (None, None),
            pipeline_paused_fn=lambda: False,
        )
        resp = dc._format_status()
        assert "idle" in resp.lower()

    @pytest.mark.asyncio
    async def test_bang_status_shows_paused(self, config):
        dc = _make_companion(
            config,
            pipeline_stage_fn=lambda: ("executor", None),
            pipeline_paused_fn=lambda: True,
        )
        resp = await dc.handle_message("!status", "")
        assert "PAUSED" in resp


# ---------- TestHandleUpdate ----------


class TestHandleUpdate:
    def test_no_shutdown_event_returns_unavailable(self, config):
        dc = _make_companion(config, shutdown_event=None)
        resp = dc._handle_update()
        assert "unavailable" in resp.lower()

    def test_queues_mutation(self, config):
        mutations = []
        ev = asyncio.Event()
        dc = _make_companion(config, mutations=mutations, shutdown_event=ev)
        resp = dc._handle_update()
        assert "Pulling" in resp
        assert len(mutations) == 1

    def test_debounce_rejects_second_call(self, config):
        mutations = []
        ev = asyncio.Event()
        dc = _make_companion(config, mutations=mutations, shutdown_event=ev)
        dc._handle_update()
        resp2 = dc._handle_update()
        assert "already in progress" in resp2.lower()
        assert len(mutations) == 1

    @pytest.mark.asyncio
    async def test_dispatch_via_handle_message(self, config):
        mutations = []
        ev = asyncio.Event()
        dc = _make_companion(config, mutations=mutations, shutdown_event=ev)
        resp = await dc.handle_message("!update", "")
        assert "Pulling" in resp
        assert len(mutations) == 1


class TestDoUpdateMutation:
    @pytest.mark.asyncio
    async def test_already_up_to_date(self, config, pipeline_state):
        ev = asyncio.Event()
        fake_head = "abc1234\n"

        async def mock_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(fake_head.encode(), b""))
            return proc

        with patch("harness.discord_companion.asyncio.create_subprocess_exec", side_effect=mock_exec):
            with patch("harness.discord_companion._notify_update", new_callable=AsyncMock) as mock_notify:
                sm = _make_session_mgr()
                await _do_update(pipeline_state, sm, ev, str(config.project_root))
                assert not ev.is_set()
                mock_notify.assert_awaited()
                last_msg = mock_notify.call_args[0][1]
                assert "up to date" in last_msg.lower()

    @pytest.mark.asyncio
    async def test_new_commits_sets_shutdown(self, config, pipeline_state):
        ev = asyncio.Event()
        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            proc = AsyncMock()
            proc.returncode = 0
            if args[0] == "git" and args[1] == "rev-parse":
                call_count += 1
                # First rev-parse returns old HEAD, second returns new HEAD
                head = b"aaa1111\n" if call_count == 1 else b"bbb2222\n"
                proc.communicate = AsyncMock(return_value=(head, b""))
            else:
                # git pull
                proc.communicate = AsyncMock(return_value=(b"Updating aaa1111..bbb2222\n", b""))
            return proc

        with patch("harness.discord_companion.asyncio.create_subprocess_exec", side_effect=mock_exec):
            with patch("harness.discord_companion._notify_update", new_callable=AsyncMock) as mock_notify:
                sm = _make_session_mgr()
                await _do_update(pipeline_state, sm, ev, str(config.project_root))
                assert ev.is_set()
                last_msg = mock_notify.call_args[0][1]
                assert "Restarting" in last_msg

    @pytest.mark.asyncio
    async def test_pull_failure_no_restart(self, config, pipeline_state):
        ev = asyncio.Event()

        async def mock_exec(*args, **kwargs):
            proc = AsyncMock()
            if args[0] == "git" and args[1] == "rev-parse":
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(b"aaa1111\n", b""))
            else:
                # git pull fails
                proc.returncode = 1
                proc.communicate = AsyncMock(return_value=(b"", b"fatal: not a git repo\n"))
            return proc

        with patch("harness.discord_companion.asyncio.create_subprocess_exec", side_effect=mock_exec):
            with patch("harness.discord_companion._notify_update", new_callable=AsyncMock) as mock_notify:
                sm = _make_session_mgr()
                await _do_update(pipeline_state, sm, ev, str(config.project_root))
                assert not ev.is_set()
                last_msg = mock_notify.call_args[0][1]
                assert "failed" in last_msg.lower()

    @pytest.mark.asyncio
    async def test_timeout_no_restart(self, config, pipeline_state):
        ev = asyncio.Event()

        async def mock_exec(*args, **kwargs):
            proc = AsyncMock()
            if args[0] == "git" and args[1] == "rev-parse":
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(b"aaa1111\n", b""))
            else:
                # git pull hangs
                async def hang():
                    await asyncio.sleep(999)
                proc.communicate = hang
            return proc

        async def _raise_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        with patch("harness.discord_companion.asyncio.create_subprocess_exec", side_effect=mock_exec):
            with patch("harness.discord_companion.asyncio.wait_for", new=_raise_timeout):
                with patch("harness.discord_companion._notify_update", new_callable=AsyncMock) as mock_notify:
                    sm = _make_session_mgr()
                    await _do_update(pipeline_state, sm, ev, str(config.project_root))
                    assert not ev.is_set()
                    last_msg = mock_notify.call_args[0][1]
                    assert "timed out" in last_msg.lower()


# ---------- TestInferAgentFromResponse ----------


class TestInferAgentFromResponse:
    def test_detects_executor(self):
        assert _infer_agent_from_response("Message routed to executor.", "") == "executor"

    def test_detects_architect(self):
        assert _infer_agent_from_response("Feedback queued for architect.", "") == "architect"

    def test_detects_reviewer(self):
        assert _infer_agent_from_response("Message sent to reviewer (escalation dialogue).", "") == "reviewer"

    def test_defaults_to_orchestrator(self):
        assert _infer_agent_from_response("Pipeline pausing.", "") == "orchestrator"

    def test_status_is_orchestrator(self):
        assert _infer_agent_from_response("Status: harness idle.", "") == "orchestrator"

    def test_case_insensitive_detection(self):
        assert _infer_agent_from_response("EXECUTOR caveman level -> ultra.", "") == "executor"


# ---------- TestAgentDisplayNames ----------


class TestAgentIdentities:
    def test_all_standard_agents_have_identities(self):
        for agent in ("orchestrator", "architect", "executor", "reviewer"):
            assert agent in AGENT_IDENTITIES
            assert "name" in AGENT_IDENTITIES[agent]
            assert "avatar_url" in AGENT_IDENTITIES[agent]

    def test_names_are_title_case(self):
        for identity in AGENT_IDENTITIES.values():
            assert identity["name"][0].isupper()


# ---------- TestSendResponse ----------


class TestSendResponse:
    @pytest.mark.asyncio
    async def test_no_webhook_uses_channel_send(self, config):
        """Without webhook_url, falls back to channel.send."""
        dc = _make_companion(config)
        message = MagicMock()
        message.channel.send = AsyncMock()
        await _send_response(message, "test response", dc, "input")
        message.channel.send.assert_awaited_once_with("test response")

    @pytest.mark.asyncio
    async def test_webhook_sends_with_agent_username(self, config):
        """Webhook POST includes inferred agent username."""
        config.discord_webhook_url = "https://discord.com/api/webhooks/test/token"
        dc = _make_companion(config)
        message = MagicMock()
        message.channel.send = AsyncMock()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
        with patch("harness.discord_companion._get_aiohttp", return_value=mock_aiohttp):
            await _send_response(message, "Feedback queued for executor.", dc, "do the thing")

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["username"] == "Executor"
        assert payload["content"] == "Feedback queued for executor."
        message.channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_webhook_failure_falls_back_to_channel(self, config):
        """HTTP error from webhook falls back to channel.send."""
        config.discord_webhook_url = "https://discord.com/api/webhooks/test/token"
        dc = _make_companion(config)
        message = MagicMock()
        message.channel.send = AsyncMock()

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
        with patch("harness.discord_companion._get_aiohttp", return_value=mock_aiohttp):
            await _send_response(message, "test response", dc, "input")

        message.channel.send.assert_awaited_once_with("test response")

    @pytest.mark.asyncio
    async def test_webhook_aiohttp_missing_falls_back(self, config):
        """Missing aiohttp falls back to channel.send."""
        config.discord_webhook_url = "https://discord.com/api/webhooks/test/token"
        dc = _make_companion(config)
        message = MagicMock()
        message.channel.send = AsyncMock()

        with patch("harness.discord_companion._get_aiohttp", return_value=None):
            await _send_response(message, "test response", dc, "input")

        message.channel.send.assert_awaited_once_with("test response")

    @pytest.mark.asyncio
    async def test_webhook_exception_falls_back(self, config):
        """Network exception from webhook falls back to channel.send."""
        config.discord_webhook_url = "https://discord.com/api/webhooks/test/token"
        dc = _make_companion(config)
        message = MagicMock()
        message.channel.send = AsyncMock()

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(
            side_effect=ConnectionError("network down")
        )
        with patch("harness.discord_companion._get_aiohttp", return_value=mock_aiohttp):
            await _send_response(message, "test response", dc, "input")

        message.channel.send.assert_awaited_once_with("test response")


# ---------- TestStripMention ----------


class TestStripMention:
    def test_strips_standard_mention(self):
        msg = MagicMock()
        msg.content = "<@123456> do the thing"
        user = MagicMock()
        user.id = 123456
        assert _strip_mention(msg, user) == "do the thing"

    def test_strips_nickname_mention(self):
        msg = MagicMock()
        msg.content = "<@!123456> do the thing"
        user = MagicMock()
        user.id = 123456
        assert _strip_mention(msg, user) == "do the thing"

    def test_no_mention_returns_full_text(self):
        msg = MagicMock()
        msg.content = "do the thing"
        user = MagicMock()
        user.id = 999999
        assert _strip_mention(msg, user) == "do the thing"


# ---------- TestSwapReactions ----------


class TestSwapReactions:
    @pytest.mark.asyncio
    async def test_swaps_on_all_messages(self):
        user = MagicMock()
        msgs = [MagicMock() for _ in range(3)]
        for m in msgs:
            m.remove_reaction = AsyncMock()
            m.add_reaction = AsyncMock()
        await _swap_reactions(msgs, user, "\u2705")
        for m in msgs:
            m.remove_reaction.assert_awaited_once_with("\U0001f440", user)
            m.add_reaction.assert_awaited_once_with("\u2705")

    @pytest.mark.asyncio
    async def test_continues_on_failure(self):
        """Failure on one message doesn't block others."""
        user = MagicMock()
        m1 = MagicMock()
        m1.remove_reaction = AsyncMock(side_effect=Exception("discord error"))
        m1.add_reaction = AsyncMock()
        m2 = MagicMock()
        m2.remove_reaction = AsyncMock()
        m2.add_reaction = AsyncMock()
        await _swap_reactions([m1, m2], user, "\u2705")
        # m2 still processed despite m1 failure
        m2.remove_reaction.assert_awaited_once()


# ---------- TestFlushAccumulated ----------


class TestFlushAccumulated:
    @pytest.mark.asyncio
    async def test_flushes_concatenated_messages(self, config):
        """Multiple buffered messages flushed as one concatenated text."""
        dc = _make_companion(config, active_agents_fn=lambda: ["executor"])
        client = MagicMock()
        client.user = MagicMock()

        msg1 = MagicMock()
        msg1.remove_reaction = AsyncMock()
        msg1.add_reaction = AsyncMock()
        msg1.channel.send = AsyncMock()
        msg2 = MagicMock()
        msg2.remove_reaction = AsyncMock()
        msg2.add_reaction = AsyncMock()

        _accum_buffer[999] = [(msg1, "fix the bug"), (msg2, "in broker.py")]

        with patch("harness.discord_companion.ACCUM_WINDOW", 0.01):
            with patch.object(dc, "handle_raw_message", new_callable=AsyncMock,
                              return_value="Message routed to executor."):
                await _flush_accumulated(999, dc, client)
                dc.handle_raw_message.assert_awaited_once_with("fix the bug\nin broker.py")

        # Buffer cleared
        assert 999 not in _accum_buffer

    @pytest.mark.asyncio
    async def test_empty_buffer_no_op(self, config):
        """Empty buffer does nothing."""
        dc = _make_companion(config)
        client = MagicMock()
        client.user = MagicMock()
        _accum_buffer.pop(888, None)

        with patch("harness.discord_companion.ACCUM_WINDOW", 0.01):
            await _flush_accumulated(888, dc, client)
        # No exception, no calls

    @pytest.mark.asyncio
    async def test_error_swaps_to_x(self, config):
        """Exception during processing swaps to x."""
        dc = _make_companion(config)
        client = MagicMock()
        client.user = MagicMock()

        msg = MagicMock()
        msg.remove_reaction = AsyncMock()
        msg.add_reaction = AsyncMock()
        msg.channel.send = AsyncMock()

        _accum_buffer[777] = [(msg, "crash me")]

        with patch("harness.discord_companion.ACCUM_WINDOW", 0.01):
            with patch.object(dc, "handle_raw_message", new_callable=AsyncMock,
                              side_effect=RuntimeError("boom")):
                await _flush_accumulated(777, dc, client)

        msg.add_reaction.assert_awaited_once_with("\u274c")


# ---------- TestAnnounceStage ----------


class TestAnnounceStage:
    def test_announce_stages_set(self):
        """Standard pipeline stages are in announce set."""
        for stage in ("classify", "architect", "executor", "reviewer", "merge", "wiki"):
            assert stage in _ANNOUNCE_STAGES

    @pytest.mark.asyncio
    async def test_skips_non_announce_stage(self, config):
        """Stages not in _ANNOUNCE_STAGES are silently skipped."""
        with patch("harness.discord_companion._get_aiohttp") as mock:
            await announce_stage("escalation_dialogue", "task-1", "desc", config)
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_announcement(self, config):
        """Announce via webhook when configured."""
        config.discord_webhook_url = "https://discord.com/api/webhooks/test/token"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)

        with patch("harness.discord_companion._get_aiohttp", return_value=mock_aiohttp):
            await announce_stage("executor", "task-abc", "Fix auth bug", config)

        mock_session.post.assert_called_once()
        payload = mock_session.post.call_args.kwargs.get("json") or mock_session.post.call_args[1].get("json")
        assert "executor" in payload["content"]
        assert "task-abc" in payload["content"]
        assert "Fix auth bug" in payload["content"]
        assert payload["username"] == "Executor"

    @pytest.mark.asyncio
    async def test_clawhip_fallback(self, config):
        """Without webhook, falls back to clawhip subprocess."""
        config.discord_webhook_url = None

        with patch("harness.discord_companion.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc
            await announce_stage("architect", "task-1", "Plan design", config)

        mock_exec.assert_called_once()
        args = mock_exec.call_args.args
        assert "clawhip" in args
        assert "stage" in args

    @pytest.mark.asyncio
    async def test_format_with_task_and_description(self, config):
        """Announcement includes task ID and description."""
        config.discord_webhook_url = "https://example.com/webhook"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)

        with patch("harness.discord_companion._get_aiohttp", return_value=mock_aiohttp):
            await announce_stage("merge", "task-xyz", "Merge feature branch", config)

        payload = mock_session.post.call_args.kwargs.get("json") or mock_session.post.call_args[1].get("json")
        # merge uses "orchestrator" as agent
        assert payload["username"] == "Orchestrator"
        assert "task-xyz" in payload["content"]

    @pytest.mark.asyncio
    async def test_format_without_description(self, config):
        """Announcement works with None description."""
        config.discord_webhook_url = "https://example.com/webhook"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)

        with patch("harness.discord_companion._get_aiohttp", return_value=mock_aiohttp):
            await announce_stage("reviewer", "task-1", None, config)

        payload = mock_session.post.call_args.kwargs.get("json") or mock_session.post.call_args[1].get("json")
        assert "task-1" in payload["content"]
        assert payload["username"] == "Reviewer"
