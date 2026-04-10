"""Discord companion — inbound command handler for the v5 harness.

Runs in the same asyncio event loop as the orchestrator. Commands that
affect the pipeline (!tell, !reply, !caveman) queue mutations instead of
mutating state directly — prevents corruption between await points.

Project-specific commands loaded from config/harness/commands.py plugin.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
import secrets
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lib.pipeline import PipelineState, ProjectConfig
    from lib.sessions import SessionManager
    from lib.signals import SignalReader

logger = logging.getLogger("harness.discord")

from lib.pipeline import VALID_CAVEMAN_LEVELS as VALID_LEVELS  # single source of truth

# Deterministic confirmation words — no LLM call for confirm detection.
# Extend this set if operators use other affirmatives.
DIALOGUE_CONFIRM_WORDS = frozenset({
    "yes", "y", "confirm", "go", "approved", "ok", "okay", "proceed",
})

# Deterministic control pre-filter — catches unambiguous pipeline control
# commands before any LLM call. Matches standalone words or "X the pipeline" forms.
_CONTROL_PATTERN = re.compile(
    r"^(stop|pause|halt|resume|unpause|status)(\s+(the\s+)?(pipeline|harness|everything))?[.!]?$",
    re.IGNORECASE,
)

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
                 signal_reader: "SignalReader",
                 active_agents_fn: Callable[[], list[str]] | None = None,
                 pipeline_stage_fn: Callable[[], tuple[str | None, str | None]] | None = None,
                 pipeline_paused_fn: Callable[[], bool] | None = None,
                 shutdown_event: asyncio.Event | None = None):
        self.config = config
        self.pending_mutations = pending_mutations
        self.signal_reader = signal_reader
        self._active_agents_fn = active_agents_fn or (lambda: list(config.agents.keys()))
        self._pipeline_stage_fn = pipeline_stage_fn or (lambda: (None, None))
        self._pipeline_paused_fn = pipeline_paused_fn or (lambda: False)
        self._shutdown_event = shutdown_event
        self._update_in_progress = False
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

    async def handle_raw_message(self, text: str) -> str | None:
        """Entry point for all Discord messages. Three-way dispatch:

        1. !prefix → existing handle_message
        2. Control pre-filter (stop/pause/resume/status) → _handle_control
        3. Escalation shortcut → _route_natural_language (skip classify_intent)
        4. classify_intent → new_task → _handle_new_task
        5. classify_intent → feedback → _route_natural_language
        """
        text = text.strip()
        if text.startswith("!"):
            parts = text.split(maxsplit=1)
            cmd = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            return await self.handle_message(cmd, args)

        # Empty after strip — route directly (no LLM call for empty messages)
        if not text:
            return await self._route_natural_language(text)

        # Deterministic control pre-filter — no LLM call for stop/pause/resume
        if _CONTROL_PATTERN.match(text):
            return self._handle_control(text)

        # Escalation shortcut — during dialogue, route directly to blocked agent
        stage, pre_esc_agent = self._pipeline_stage_fn()
        if stage in ("escalation_wait", "escalation_dialogue") and pre_esc_agent:
            return await self._route_natural_language(text)

        # Three-way intent classification: feedback vs new_task
        from lib.claude import classify_intent

        has_active_task = stage is not None
        intent = await classify_intent(text, has_active_task, self.config)
        if intent == "new_task":
            return await self._handle_new_task(text)
        return await self._route_natural_language(text)

    async def _route_natural_language(self, text: str) -> str | None:
        """Route a non-prefixed operator message to the correct agent."""
        # Escalation dialogue: route directly to blocked agent (skip classify)
        stage, pre_esc_agent = self._pipeline_stage_fn()
        if stage in ("escalation_wait", "escalation_dialogue") and pre_esc_agent:
            sr = self.signal_reader
            self.pending_mutations.append(
                lambda s, sm, m=text, _sr=sr: _apply_dialogue_message(s, sm, m, _sr)
            )
            return f"Message sent to {pre_esc_agent} (escalation dialogue)."

        from lib.claude import classify_target

        agents = self._active_agents_fn()
        if not agents:
            return "No active agents. Submit a task first."
        if len(agents) == 1:
            target = agents[0]
        else:
            target = await classify_target(text, agents, self.config)
            if target is None:
                return f"Who do you mean? Active agents: {', '.join(agents)}"
        # NOTE: default-argument binding to avoid late-binding closure bug
        self.pending_mutations.append(
            lambda s, sm, a=target, m=text: sm.send(a, f"[OPERATOR] {m}")
        )
        return f"Message routed to {target}."

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

        if cmd == "!update":
            return self._handle_update()

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

    def _handle_update(self) -> str:
        if self._update_in_progress:
            return "Update already in progress."
        if not self._shutdown_event:
            return "Update unavailable — no shutdown event wired."
        self._update_in_progress = True
        ev = self._shutdown_event
        cwd = str(self.config.project_root)

        def reset():
            self._update_in_progress = False

        self.pending_mutations.append(
            lambda s, sm, _ev=ev, _cwd=cwd, _reset=reset: _do_update(s, sm, _ev, _cwd, _reset)
        )
        return "Pulling latest code..."

    def _handle_control(self, text: str) -> str | None:
        """Handle control commands detected by deterministic pre-filter."""
        verb = text.strip().split()[0].lower()
        if verb in ("stop", "pause", "halt"):
            self.pending_mutations.append(
                lambda s, sm, p=True: _apply_pause(s, sm, p)
            )
            return "Pipeline pausing. Active task frozen — health checks continue."
        if verb in ("resume", "unpause"):
            self.pending_mutations.append(
                lambda s, sm, p=False: _apply_pause(s, sm, p)
            )
            return "Pipeline resuming."
        if verb == "status":
            return self._format_status()
        return None

    async def _handle_new_task(self, text: str) -> str:
        """Create a TaskSignal from operator NL message."""
        from lib.signals import TaskSignal, write_signal

        task_id = f"discord-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        task = TaskSignal(task_id=task_id, description=text, source="discord")
        write_signal(self.config.task_dir, f"{task_id}.json", task)
        logger.info("NL task created: %s — '%s'", task_id, text[:80])
        return f"Task created: {task_id} — '{text[:80]}'"

    def _format_status(self) -> str:
        stage, _ = self._pipeline_stage_fn()
        is_paused = self._pipeline_paused_fn()
        lines = []
        if is_paused:
            lines.append("Status: harness PAUSED.")
            if stage:
                lines.append(f"  Frozen at stage: {stage}")
            lines.append("  Send 'resume' to unpause.")
        elif stage:
            lines.append(f"Status: harness running. Active stage: {stage}.")
        else:
            lines.append("Status: harness idle.")
        lines.append("Use !caveman status for compression levels. !update to pull + restart.")
        return "\n".join(lines)


# Bounded dedup buffer for Discord gateway reconnect replays.
_seen_message_ids: deque[int] = deque(maxlen=1000)


async def start(companion: DiscordCompanion,
                channel_ids: list[int] | None = None) -> None:
    """Run the Discord client in the current event loop.

    Receives messages, delegates to companion.handle_raw_message(),
    and sends the response back. Filters to channel_ids if provided.
    """
    try:
        import discord
    except ImportError:
        logger.error("discord.py not installed — Discord companion disabled")
        return

    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        logger.error("DISCORD_BOT_TOKEN not set — Discord companion disabled")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info("Discord companion connected as %s", client.user)

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        if channel_ids and message.channel.id not in channel_ids:
            return
        if message.id in _seen_message_ids:
            return
        _seen_message_ids.append(message.id)

        response = await companion.handle_raw_message(message.content)
        if response:
            await message.channel.send(response)

    await client.start(token)


async def _do_update(state: "PipelineState", session_mgr: "SessionManager",
                     shutdown_event: asyncio.Event, cwd: str,
                     reset_fn: Callable[[], None] | None = None) -> None:
    """Pull latest code and trigger graceful restart if new commits arrived."""
    try:
        # Snapshot HEAD before pull
        head_before = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        before_out, _ = await head_before.communicate()
        old_head = before_out.decode().strip() if head_before.returncode == 0 else ""

        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--ff-only", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            err = stderr.decode().strip()[:200]
            logger.warning("git pull failed: %s", err)
            await _notify_update(session_mgr, f"Update failed: {err}")
            if reset_fn:
                reset_fn()
            return

        # Snapshot HEAD after pull
        head_after = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        after_out, _ = await head_after.communicate()
        new_head = after_out.decode().strip() if head_after.returncode == 0 else ""

        if old_head == new_head:
            logger.info("git pull: already up to date (%s)", old_head[:8])
            await _notify_update(session_mgr, f"Already up to date ({old_head[:8]}).")
            if reset_fn:
                reset_fn()
            return

        logger.info("git pull: updated %s → %s — restarting", old_head[:8], new_head[:8])
        await _notify_update(
            session_mgr,
            f"Updated {old_head[:8]} → {new_head[:8]}. Restarting...",
        )
        # Signal harness.sh to restart (not just stop) after graceful shutdown
        from pathlib import Path
        run_dir = Path(cwd) / ".run"
        run_dir.mkdir(exist_ok=True)
        (run_dir / "restart_requested").touch()
        # No reset_fn — process is about to exit; flag clears on restart
        shutdown_event.set()

    except asyncio.TimeoutError:
        logger.warning("git pull timed out after 30s")
        await _notify_update(session_mgr, "Update failed: git pull timed out.")
        if reset_fn:
            reset_fn()
    except Exception as exc:
        logger.error("_do_update failed: %s", exc, exc_info=True)
        await _notify_update(session_mgr, "Update failed — check server logs.")
        if reset_fn:
            reset_fn()


async def _notify_update(session_mgr: "SessionManager", message: str) -> None:
    """Best-effort notification of update result via clawhip. Logs on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "clawhip", "agent", "update",
            "--name", "orchestrator", "--summary", message,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, FileNotFoundError, Exception):
        logger.info("Update result (notify unavailable): %s", message)


async def _apply_pause(state: "PipelineState", session_mgr: "SessionManager",
                       paused: bool) -> None:
    """Set pipeline-wide pause state via mutation."""
    state.paused = paused
    logger.info("Pipeline %s by operator", "paused" if paused else "resumed")


async def _apply_reply(state: "PipelineState", session_mgr: "SessionManager",
                       task_id: str, response: str,
                       signal_reader: "SignalReader") -> None:
    """Apply an escalation reply — inject into the original agent and resume.

    If the task is shelved (not active), resolve its escalation in-place and
    store the reply for injection when unshelved.
    """
    if state.active_task == task_id:
        # Active task — apply immediately
        if state.stage not in ("escalation_wait", "escalation_tier1", "escalation_dialogue"):
            logger.warning("Reply for %s but stage is %s (not escalation)", task_id, state.stage)
            return
        original_agent = state.pre_escalation_agent
        if original_agent and original_agent in session_mgr.sessions:
            await session_mgr.send(original_agent, f"[OPERATOR REPLY] {response}")
        elif original_agent:
            logger.warning("Session %s dead during escalation — reply not delivered", original_agent)
        state.resume_from_escalation()
        signal_reader.clear_escalation(task_id)
        logger.info("Operator reply applied for %s → resuming %s at %s",
                    task_id, original_agent, state.stage)
        return

    # Check shelved tasks
    for shelved in state.shelved_tasks:
        if shelved.get("task_id") == task_id:
            stage = shelved.get("stage", "")
            if stage not in ("escalation_wait", "escalation_tier1", "escalation_dialogue"):
                logger.warning("Reply for shelved %s but stage is %s (not escalation)", task_id, stage)
                return
            # Resolve escalation in-place on the shelved entry
            pre_stage = shelved.get("pre_escalation_stage", "executor")
            shelved["stage"] = pre_stage
            shelved["stage_agent"] = shelved.get("pre_escalation_agent")
            shelved["pre_escalation_stage"] = None
            shelved["pre_escalation_agent"] = None
            shelved["pending_operator_reply"] = f"[OPERATOR REPLY] {response}"
            signal_reader.clear_escalation(task_id)
            logger.info("Operator reply stored for shelved task %s — escalation resolved to %s",
                        task_id, pre_stage)
            return

    logger.warning("Reply for %s but task not found (active=%s, shelved=%d)",
                   task_id, state.active_task, len(state.shelved_tasks))


async def _apply_dialogue_message(state: "PipelineState", session_mgr: "SessionManager",
                                   message: str, signal_reader: "SignalReader") -> None:
    """Handle operator message during escalation dialogue.

    Dual responsibility: (1) deliver message to blocked agent, (2) update state
    for orchestrator classification. If multiple messages queued in one poll cycle,
    each overwrites dialogue_last_message — orchestrator classifies most recent only.
    All messages are delivered to the agent regardless.
    """
    agent = state.pre_escalation_agent
    if not agent:
        logger.warning("Dialogue message but no pre_escalation_agent — dropping")
        return

    # Confirmation of previously detected resolution
    if state.dialogue_pending_confirmation:
        normalized = message.strip().lower()
        if normalized in DIALOGUE_CONFIRM_WORDS:
            if agent in session_mgr.sessions:
                await session_mgr.send(agent, f"[OPERATOR REPLY] {message}")
            task_id = state.active_task or ""
            state.resume_from_escalation()  # clears all dialogue fields
            signal_reader.clear_escalation(task_id)
            logger.info("Dialogue confirmed for %s — resuming at %s", task_id, state.stage)
            return

    # Normal dialogue message
    if agent in session_mgr.sessions:
        await session_mgr.send(agent, f"[OPERATOR] {message}")
    state.dialogue_last_message = message
    state.dialogue_last_message_ts = datetime.now(UTC).isoformat()
    if state.stage == "escalation_wait":
        state.advance("escalation_dialogue")
    state.dialogue_pending_confirmation = False  # new message cancels pending confirmation
