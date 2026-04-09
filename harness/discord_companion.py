"""Discord companion — inbound command handler for the v5 harness.

Runs in the same asyncio event loop as the orchestrator. Commands that
affect the pipeline (!tell, !reply, !caveman) queue mutations instead of
mutating state directly — prevents corruption between await points.

Project-specific commands loaded from config/harness/commands.py plugin.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lib.pipeline import PipelineState, ProjectConfig
    from lib.sessions import SessionManager
    from lib.signals import SignalReader

logger = logging.getLogger("harness.discord")

from lib.pipeline import VALID_CAVEMAN_LEVELS as VALID_LEVELS  # single source of truth

# Type alias for pending mutations
Mutation = Callable[["PipelineState", "SessionManager"], Awaitable[None]]


def parse_caveman(args: str) -> tuple[str, str]:
    """Parse !caveman arguments with backward compatibility.

    !caveman full         → ("all", "full")    — backward compat global toggle
    !caveman status       → ("status", "")
    !caveman reset        → ("reset", "")
    !caveman executor ultra → ("executor", "ultra")
    """
    parts = args.strip().split(maxsplit=1)
    if not parts:
        return ("status", "")
    if len(parts) == 1:
        if parts[0] in VALID_LEVELS:
            return ("all", parts[0])        # backward compat
        return (parts[0], "")               # status, reset
    return (parts[0], parts[1])


def parse_tell(args: str) -> tuple[str, str]:
    """Parse !tell <agent> <message>."""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return ("", "")
    return (parts[0], parts[1])


def parse_reply(args: str) -> tuple[str, str]:
    """Parse !reply <task_id> <response>."""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return ("", "")
    return (parts[0], parts[1])


class DiscordCompanion:
    """Handles inbound Discord commands for the harness."""

    def __init__(self, config: "ProjectConfig",
                 pending_mutations: list[Mutation],
                 signal_reader: "SignalReader"):
        self.config = config
        self.pending_mutations = pending_mutations
        self.signal_reader = signal_reader
        self._project_commands: dict[str, str] = {}
        self._project_handler: Any = None
        self._load_project_commands()

    def _load_project_commands(self) -> None:
        """Load project-specific commands from config module."""
        if not self.config.commands_module:
            return
        try:
            mod = importlib.import_module(self.config.commands_module)
            self._project_commands = getattr(mod, "COMMANDS", {})
            self._project_handler = getattr(mod, "handle_command", None)
            logger.info("Loaded %d project commands", len(self._project_commands))
        except (ImportError, AttributeError) as e:
            logger.warning("Project commands not loaded: %s", e)

    async def handle_message(self, cmd: str, args: str) -> str | None:
        """Dispatch a command. Returns response text or None.

        Called from the Discord on_message handler. This method does NOT
        mutate PipelineState directly — it queues mutations for the main
        loop to apply.
        """
        if cmd == "!tell":
            agent, message = parse_tell(args)
            if not agent or not message:
                return "Usage: !tell <agent> <message>"
            if agent not in self.config.agents:
                return f"Unknown agent '{agent}'. Active: {', '.join(self.config.agents.keys())}"
            # NOTE: default-argument binding (a=agent, m=message) to avoid
            # Python's late-binding closure bug with pending_mutations.
            self.pending_mutations.append(
                lambda s, sm, a=agent, m=message: sm.send(a, f"[OPERATOR] {m}")
            )
            return f"Feedback queued for {agent}."

        if cmd == "!reply":
            task_id, response = parse_reply(args)
            if not task_id or not response:
                return "Usage: !reply <task_id> <response>"
            try:
                from lib.signals import _safe_task_id
                _safe_task_id(task_id)
            except ValueError:
                return f"Invalid task_id: {task_id!r}"
            sr = self.signal_reader
            self.pending_mutations.append(
                lambda s, sm, t=task_id, r=response, _sr=sr: _apply_reply(s, sm, t, r, _sr)
            )
            return f"Reply queued for {task_id}."

        if cmd == "!caveman":
            return self._handle_caveman(args)

        if cmd == "!status":
            return self._format_status()

        # Check project-specific commands
        if self._project_handler and cmd in self._project_commands:
            signal_dir = self.config.signal_dir
            return await self._project_handler(cmd, args, signal_dir)

        return None

    def _handle_caveman(self, args: str) -> str:
        agent, level = parse_caveman(args)
        if agent == "status":
            return self._format_caveman_status()
        if agent == "reset":
            self.config.caveman.reset_to_defaults()
            return "Caveman levels reset to project.toml defaults."
        if level and level not in VALID_LEVELS:
            return f"Unknown level '{level}'. Valid: {', '.join(sorted(VALID_LEVELS))}"
        if agent == "all":
            self.config.caveman.set_all(level)
            return f"All agents set to caveman {level}."
        if agent not in self.config.agents:
            return f"Unknown agent '{agent}'. Active: {', '.join(self.config.agents.keys())}"
        self.config.caveman.set_agent(agent, level)
        # NOTE: default-argument binding to avoid late-binding closure bug
        self.pending_mutations.append(
            lambda s, sm, a=agent, lvl=level: sm.inject_caveman_update(a, lvl)
        )
        return f"{agent} caveman level -> {level}."

    def _format_caveman_status(self) -> str:
        lines = ["Caveman levels:"]
        for name in sorted(self.config.agents.keys()):
            level = self.config.caveman.level_for(name)
            override = " (runtime)" if name in self.config.caveman._runtime_overrides else ""
            lines.append(f"  {name}: {level}{override}")
        lines.append(f"  default: {self.config.caveman.default_level}")
        return "\n".join(lines)

    def _format_status(self) -> str:
        return "Status: harness running. Use !caveman status for compression levels."


async def _apply_reply(state: "PipelineState", session_mgr: "SessionManager",
                       task_id: str, response: str,
                       signal_reader: "SignalReader | None" = None) -> None:
    """Apply an escalation reply — inject into the original agent and resume."""
    if state.active_task != task_id:
        logger.warning("Reply for %s but active task is %s", task_id, state.active_task)
        return
    if state.stage not in ("escalation_wait", "escalation_tier1"):
        logger.warning("Reply for %s but stage is %s (not escalation)", task_id, state.stage)
        return
    original_agent = state.pre_escalation_agent
    if original_agent and original_agent in session_mgr.sessions:
        await session_mgr.send(original_agent, f"[OPERATOR REPLY] {response}")
    elif original_agent:
        logger.warning("Session %s dead during escalation — reply not delivered", original_agent)
    state.resume_from_escalation()
    if signal_reader is not None:
        signal_reader.clear_escalation(task_id)
    logger.info("Operator reply applied for %s → resuming %s at %s",
                task_id, original_agent, state.stage)
