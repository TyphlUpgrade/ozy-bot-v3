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

# Template fallback for response generation when haiku is unavailable.
# To add a response type, add one entry here.
_RESPONSE_TEMPLATES: dict[str, str] = {
    "route": "Got it, passing that to {agent}.",
    "escalation_route": "Passing that to {agent} — they're waiting on your input.",
    "no_agents": "Nobody's online right now — submit a task to spin up agents.",
    "ambiguous": "Not sure who that's for — {agents} are active. Could you be more specific?",
    "pause": "Pausing the pipeline. Current task is frozen — I'll keep running health checks.",
    "resume": "Resuming — picking up where we left off.",
    "task_created": "On it — created task `{task_id}`. Pipeline will pick it up shortly.",
    "reply_sent": "Got it, sending that reply to `{task_id}`.",
    "caveman_set": "Set {agent} to caveman `{level}`.",
    "caveman_set_all": "All agents set to caveman {level}.",
    "caveman_reset": "Caveman levels reset to defaults.",
    "update_started": "Pulling latest code...",
}

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

    async def _respond(self, action: dict | None) -> str | None:
        """Convert an action dict into a response string.

        Tries haiku for conversational output, falls back to templates.
        Passthrough types (status, error, display) return detail as-is.
        """
        if action is None:
            return None

        atype = action["type"]

        # Passthrough — structured display or error, no LLM needed
        if atype in ("status", "caveman_status", "error"):
            return action.get("detail")

        # Try haiku for conversational response
        try:
            from lib.claude import generate_response
            haiku_resp = await generate_response(action, self.config)
            if haiku_resp:
                return haiku_resp
        except Exception:
            pass

        # Template fallback
        template = _RESPONSE_TEMPLATES.get(atype)
        if template:
            fmt = {k: v for k, v in action.items() if k != "type"}
            if "agents" in fmt and isinstance(fmt["agents"], list):
                fmt["agents"] = ", ".join(fmt["agents"])
            return template.format(**fmt)

        return action.get("detail", "Done.")

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

        Internal handlers return action dicts; _respond converts to strings
        (haiku for conversational output, template fallback).
        """
        text = text.strip()
        if text.startswith("!"):
            parts = text.split(maxsplit=1)
            cmd = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            return await self.handle_message(cmd, args)

        # Empty after strip — route directly (no LLM call for empty messages)
        if not text:
            return await self._respond(await self._route_natural_language(text))

        # Deterministic control pre-filter — no LLM call for stop/pause/resume
        if _CONTROL_PATTERN.match(text):
            return await self._respond(self._handle_control(text))

        # Escalation shortcut — during dialogue, route directly to blocked agent
        stage, pre_esc_agent = self._pipeline_stage_fn()
        if stage in ("escalation_wait", "escalation_dialogue") and pre_esc_agent:
            return await self._respond(await self._route_natural_language(text))

        # Three-way intent classification: feedback vs new_task
        from lib.claude import classify_intent

        has_active_task = stage is not None
        intent = await classify_intent(text, has_active_task, self.config)
        if intent == "new_task":
            return await self._respond(await self._handle_new_task(text))
        return await self._respond(await self._route_natural_language(text))

    async def _route_natural_language(self, text: str) -> dict:
        """Route a non-prefixed operator message to the correct agent.

        Returns an action dict — caller wraps with _respond for string output.
        """
        # Escalation dialogue: route directly to blocked agent (skip classify)
        stage, pre_esc_agent = self._pipeline_stage_fn()
        if stage in ("escalation_wait", "escalation_dialogue") and pre_esc_agent:
            sr = self.signal_reader
            self.pending_mutations.append(
                lambda s, sm, m=text, _sr=sr: _apply_dialogue_message(s, sm, m, _sr)
            )
            return {"type": "escalation_route", "agent": pre_esc_agent}

        from lib.claude import classify_target

        agents = self._active_agents_fn()
        if not agents:
            return {"type": "no_agents", "agent": "orchestrator"}
        if len(agents) == 1:
            target = agents[0]
        else:
            target = await classify_target(text, agents, self.config)
            if target is None:
                return {"type": "ambiguous", "agent": "orchestrator", "agents": agents}
        # NOTE: default-argument binding to avoid late-binding closure bug
        self.pending_mutations.append(
            lambda s, sm, a=target, m=text: sm.send(a, f"[OPERATOR] {m}")
        )
        return {"type": "route", "agent": target}

    async def handle_message(self, cmd: str, args: str) -> str | None:
        """Dispatch a command. Returns response text or None.

        Called from the Discord on_message handler. This method does NOT
        mutate PipelineState directly — it queues mutations for the main
        loop to apply. Conversational responses go through _respond (haiku
        with template fallback); error/usage strings returned directly.
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
            return await self._respond({"type": "route", "agent": agent})

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
            return await self._respond({"type": "reply_sent", "agent": "orchestrator", "task_id": task_id})

        if cmd == "!caveman":
            return await self._respond(self._handle_caveman(args))

        if cmd == "!update":
            return await self._respond(self._handle_update())

        if cmd == "!status":
            return self._format_status()

        # Check project-specific commands
        if self._project_handler and cmd in self._project_commands:
            signal_dir = self.config.signal_dir
            return await self._project_handler(cmd, args, signal_dir)

        return None

    def _handle_caveman(self, args: str) -> dict:
        """Handle !caveman command. Returns action dict."""
        agent, level = parse_caveman(args)
        if agent == "status":
            return {"type": "caveman_status", "detail": self._format_caveman_status()}
        if agent == "reset":
            self.config.caveman.reset_to_defaults()
            return {"type": "caveman_reset"}
        if level and level not in VALID_LEVELS:
            return {"type": "error", "detail": f"Unknown level '{level}'. Valid: {', '.join(sorted(VALID_LEVELS))}"}
        if agent == "all":
            self.config.caveman.set_all(level)
            return {"type": "caveman_set_all", "agent": "all", "level": level}
        if agent not in self.config.agents:
            return {"type": "error", "detail": f"Unknown agent '{agent}'. Active: {', '.join(self.config.agents.keys())}"}
        self.config.caveman.set_agent(agent, level)
        # NOTE: default-argument binding to avoid late-binding closure bug
        self.pending_mutations.append(
            lambda s, sm, a=agent, lvl=level: sm.inject_caveman_update(a, lvl)
        )
        return {"type": "caveman_set", "agent": agent, "level": level}

    def _format_caveman_status(self) -> str:
        lines = ["Caveman levels:"]
        for name in sorted(self.config.agents.keys()):
            level = self.config.caveman.level_for(name)
            override = " (runtime)" if name in self.config.caveman._runtime_overrides else ""
            lines.append(f"  {name}: {level}{override}")
        lines.append(f"  default: {self.config.caveman.default_level}")
        return "\n".join(lines)

    def _handle_update(self) -> dict:
        """Handle !update command. Returns action dict."""
        if self._update_in_progress:
            return {"type": "error", "detail": "Update already in progress."}
        if not self._shutdown_event:
            return {"type": "error", "detail": "Update unavailable — no shutdown event wired."}
        self._update_in_progress = True
        ev = self._shutdown_event
        cwd = str(self.config.project_root)

        def reset():
            self._update_in_progress = False

        self.pending_mutations.append(
            lambda s, sm, _ev=ev, _cwd=cwd, _reset=reset: _do_update(s, sm, _ev, _cwd, _reset)
        )
        return {"type": "update_started", "agent": "orchestrator"}

    def _handle_control(self, text: str) -> dict | None:
        """Handle control commands detected by deterministic pre-filter.

        Returns action dict — caller wraps with _respond for string output.
        """
        verb = text.strip().split()[0].lower()
        if verb in ("stop", "pause", "halt"):
            self.pending_mutations.append(
                lambda s, sm, p=True: _apply_pause(s, sm, p)
            )
            return {"type": "pause", "agent": "orchestrator"}
        if verb in ("resume", "unpause"):
            self.pending_mutations.append(
                lambda s, sm, p=False: _apply_pause(s, sm, p)
            )
            return {"type": "resume", "agent": "orchestrator"}
        if verb == "status":
            return {"type": "status", "agent": "orchestrator", "detail": self._format_status()}
        return None

    async def _handle_new_task(self, text: str) -> dict:
        """Create a TaskSignal from operator NL message. Returns action dict."""
        from lib.signals import TaskSignal, write_signal

        task_id = f"discord-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        task = TaskSignal(task_id=task_id, description=text, source="discord")
        write_signal(self.config.task_dir, f"{task_id}.json", task)
        logger.info("NL task created: %s — '%s'", task_id, text[:80])
        return {"type": "task_created", "agent": "orchestrator", "task_id": task_id}

    def _format_status(self) -> str:
        stage, _ = self._pipeline_stage_fn()
        is_paused = self._pipeline_paused_fn()
        lines = []
        if is_paused:
            lines.append("\u23f8\ufe0f **Paused.**")
            if stage:
                lines.append(f"Frozen at **{stage}** stage.")
            lines.append("Say `resume` to unpause.")
        elif stage:
            lines.append(f"\u25b6\ufe0f **Running** — currently at **{stage}** stage.")
        else:
            lines.append("\U0001f7e2 **Idle** — waiting for tasks.")
        lines.append("`!caveman status` for compression levels \u00b7 `!update` to pull + restart")
        return "\n".join(lines)


def _get_aiohttp():
    """Lazy import aiohttp — returns module or None if not installed."""
    try:
        import aiohttp
        return aiohttp
    except ImportError:
        logger.warning("aiohttp not installed — webhook disabled, using channel.send")
        return None


# --- Webhook per-agent identity ---

# Hardcoded fallback — used when config has no [discord.agents] section.
# To add a new agent, add one entry here AND in project.toml [discord.agents.*].
AGENT_IDENTITIES: dict[str, dict[str, str | None]] = {
    "orchestrator": {"name": "Orchestrator", "avatar_url": None},
    "architect":    {"name": "Architect",    "avatar_url": None},
    "executor":     {"name": "Executor",     "avatar_url": None},
    "reviewer":     {"name": "Reviewer",     "avatar_url": None},
}


def _agent_identity(agent: str, config: "ProjectConfig | None" = None) -> dict[str, str | None]:
    """Resolve agent identity from config, falling back to hardcoded defaults."""
    if config and config.discord_agent_identities:
        ident = config.discord_agent_identities.get(agent)
        if ident:
            return ident
    return AGENT_IDENTITIES.get(agent, {"name": agent.title(), "avatar_url": None})


def _agent_display_name(agent: str, config: "ProjectConfig | None" = None) -> str:
    return _agent_identity(agent, config)["name"]  # type: ignore[return-value]


def _agent_avatar_url(agent: str, config: "ProjectConfig | None" = None) -> str | None:
    return _agent_identity(agent, config)["avatar_url"]


def _infer_agent_from_response(response: str, text: str) -> str:
    """Best-effort agent inference from companion response text.

    Looks for patterns like "routed to executor", "queued for architect",
    "sent to reviewer". Falls back to "orchestrator" for status/control/unknown.
    """
    lower = response.lower()
    for agent in ("executor", "architect", "reviewer"):
        if agent in lower:
            return agent
    return "orchestrator"


async def _send_response(message: Any, response: str,
                         companion: DiscordCompanion, text: str) -> None:
    """Send response via webhook (agent identity) or fall back to channel.send."""
    webhook_url = companion.config.discord_webhook_url
    if not webhook_url:
        await message.channel.send(response)
        return

    agent = _infer_agent_from_response(response, text)
    username = _agent_display_name(agent, companion.config)
    avatar_url = _agent_avatar_url(agent, companion.config)

    try:
        aiohttp = _get_aiohttp()
        if aiohttp is None:
            await message.channel.send(response)
            return
        payload: dict[str, str] = {
            "content": response,
            "username": username,
        }
        if avatar_url:
            payload["avatar_url"] = avatar_url
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload) as resp:
                if resp.status >= 400:
                    logger.warning("Webhook POST failed (status %d) — falling back", resp.status)
                    await message.channel.send(response)
    except Exception:
        logger.warning("Webhook send failed — falling back to channel.send", exc_info=True)
        await message.channel.send(response)


# --- Stage transition announcements ---

# Stages that get Discord announcements (skip internal/transient stages)
_ANNOUNCE_STAGES = frozenset({
    "classify", "architect", "executor", "reviewer", "merge", "wiki",
    "escalation_wait", "escalation_tier1",
})


async def announce_stage(stage: str, task_id: str | None,
                         description: str | None,
                         config: "ProjectConfig",
                         *,
                         plan_summary: str | None = None,
                         diff_stat: str | None = None,
                         review_verdict: str | None = None,
                         retry_count: int = 0) -> None:
    """Post stage transition to Discord via webhook or clawhip.

    Called from orchestrator after state.advance(). Non-blocking, best-effort.
    Accepts optional pipeline context for richer messages.
    """
    if stage not in _ANNOUNCE_STAGES:
        return

    agent = stage if stage not in ("merge", "wiki", "classify") else "orchestrator"
    desc_snippet = (description or "")[:80]

    # Build structured message with available context
    lines: list[str] = []
    if task_id and desc_snippet:
        lines.append(f"\U0001f504 **{stage}** — `{task_id}`: {desc_snippet}")
    elif task_id:
        lines.append(f"\U0001f504 **{stage}** — `{task_id}`")
    else:
        lines.append(f"\U0001f504 **{stage}**")

    if stage == "architect" and plan_summary:
        lines.append(f"> {plan_summary[:200]}")
    elif stage == "executor" and plan_summary:
        lines.append(f"Plan: {plan_summary[:150]}")
    elif stage == "reviewer" and diff_stat:
        lines.append(f"```\n{diff_stat[:300]}\n```")
    elif stage == "merge":
        if review_verdict:
            lines.append(f"\u2705 Reviewer: **{review_verdict}**")
        if retry_count > 0:
            lines.append(f"\u26a0\ufe0f Retries: {retry_count}")
    elif stage == "wiki":
        lines.append("Documenting completed task")

    text = "\n".join(lines)

    webhook_url = config.discord_webhook_url
    if webhook_url:
        username = _agent_display_name(agent, config)
        avatar = _agent_avatar_url(agent, config)
        try:
            aiohttp = _get_aiohttp()
            if aiohttp:
                payload: dict[str, Any] = {
                    "content": text, "username": username,
                }
                if avatar:
                    payload["avatar_url"] = avatar
                async with aiohttp.ClientSession() as session:
                    async with session.post(webhook_url, json=payload) as resp:
                        if resp.status >= 400:
                            logger.debug("Stage announce webhook failed: %d", resp.status)
                return
        except Exception:
            logger.debug("Stage announce webhook error — falling through to clawhip", exc_info=True)

    # Fallback 1: clawhip notify
    try:
        proc = await asyncio.create_subprocess_exec(
            "clawhip", "agent", "stage",
            "--name", agent, "--summary", text,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return
    except Exception:
        pass

    # Fallback 2: direct channel send via Discord client
    if _discord_client and _discord_channel_ids:
        try:
            ch = _discord_client.get_channel(_discord_channel_ids[0])
            if ch:
                await ch.send(text)
        except Exception:
            logger.debug("Stage announce unavailable for %s", stage)


# Discord client + channel refs for direct sends (set by start())
_discord_client: Any = None
_discord_channel_ids: list[int] = []

# Bounded dedup buffer for Discord gateway reconnect replays.
_seen_message_ids: deque[int] = deque(maxlen=1000)

# --- Message accumulator ---
# NL messages buffer per-channel with debounce timer. ! commands bypass.
# Single event loop assumption: these module globals are safe because
# all Discord operations run in one asyncio event loop (see CLAUDE.md).
ACCUM_WINDOW = 2.0  # seconds after last NL message before flush
_accum_buffer: dict[int, list[tuple[Any, str]]] = {}   # channel_id → [(message, text)]
_accum_timers: dict[int, asyncio.Task] = {}             # channel_id → debounce task
_processing_lock = asyncio.Lock()


def _strip_mention(message: Any, client_user: Any) -> str:
    """Strip bot mention prefix from message text."""
    text = message.content
    for mention_str in (f"<@{client_user.id}>", f"<@!{client_user.id}>"):
        text = text.replace(mention_str, "").strip()
    return text


async def _swap_reactions(messages: list[Any], client_user: Any,
                          emoji: str) -> None:
    """Remove 👀 and add final emoji on all messages. Best-effort."""
    for msg in messages:
        try:
            await msg.remove_reaction("\U0001f440", client_user)
            await msg.add_reaction(emoji)
        except Exception:
            pass


async def _flush_accumulated(channel_id: int, companion: DiscordCompanion,
                             client: Any) -> None:
    """Flush accumulated NL messages for a channel after debounce window."""
    await asyncio.sleep(ACCUM_WINDOW)

    async with _processing_lock:
        entries = _accum_buffer.pop(channel_id, [])
        _accum_timers.pop(channel_id, None)
        if not entries:
            return

        messages = [e[0] for e in entries]
        texts = [e[1] for e in entries]
        combined = "\n".join(texts)

        try:
            response = await companion.handle_raw_message(combined)
            if response:
                await _send_response(messages[0], response, companion, combined)
            await _swap_reactions(messages, client.user, "\u2705")  # ✅
        except Exception:
            logger.exception("Flush handler failed for channel %d", channel_id)
            await _swap_reactions(messages, client.user, "\u274c")  # ❌


async def start(companion: DiscordCompanion,
                channel_ids: list[int] | None = None) -> None:
    """Run the Discord client in the current event loop.

    Receives messages, delegates to companion.handle_raw_message(),
    and sends the response back. Filters to channel_ids if provided.

    Three-lane message handling:
    - Immediate: ! commands and control words — process under lock, no buffering
    - Accumulate: NL messages — buffer per-channel, 2s debounce, flush concatenated
    - Bypass: own messages, non-mentioned, dedup — drop silently
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

    global _discord_client, _discord_channel_ids
    _discord_client = client
    _discord_channel_ids = channel_ids or []

    @client.event
    async def on_ready():
        logger.info("Discord companion connected as %s", client.user)
        # Post startup announcement to first configured channel
        if channel_ids:
            try:
                ch = client.get_channel(channel_ids[0])
                if ch:
                    await ch.send("\U0001f7e2 **Orchestrator online** — ready for tasks")
            except Exception:
                logger.debug("Startup announcement failed", exc_info=True)

    @client.event
    async def on_message(message):
        # --- Bypass lane ---
        if message.author == client.user:
            return
        if channel_ids and message.channel.id not in channel_ids:
            return
        if not client.user or client.user not in message.mentions:
            return
        if message.id in _seen_message_ids:
            return
        _seen_message_ids.append(message.id)

        # Acknowledge receipt
        try:
            await message.add_reaction("\U0001f440")  # 👀
        except Exception:
            pass

        text = _strip_mention(message, client.user)

        # --- Immediate lane: ! commands and control words ---
        if text.startswith("!") or _CONTROL_PATTERN.match(text):
            async with _processing_lock:
                try:
                    response = await companion.handle_raw_message(text)
                    if response:
                        await _send_response(message, response, companion, text)
                    await _swap_reactions([message], client.user, "\u2705")
                except Exception:
                    logger.exception("Immediate handler failed")
                    await _swap_reactions([message], client.user, "\u274c")
            return

        # --- Accumulate lane: NL messages ---
        ch_id = message.channel.id
        _accum_buffer.setdefault(ch_id, []).append((message, text))

        # Start debounce timer if none pending. Don't cancel in-progress
        # flushes — cancelling a task holding _processing_lock can drop
        # messages silently. Trade-off: window starts from first message
        # rather than resetting on each, but safer and simpler.
        if ch_id not in _accum_timers or _accum_timers[ch_id].done():
            _accum_timers[ch_id] = asyncio.create_task(
                _flush_accumulated(ch_id, companion, client)
            )

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
