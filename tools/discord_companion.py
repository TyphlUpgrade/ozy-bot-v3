#!/usr/bin/env python3
"""
Discord companion — inbound command handler for the Ozymandias trading bot.

Listens for commands in Discord channels and translates them to signal files.
Does NOT import from ozymandias/ — communicates entirely through JSON files.

Requires: discord.py (`pip install discord.py`)

Environment variables:
    DISCORD_BOT_TOKEN    — Bot token (required)
    COMPANION_CHANNELS   — Comma-separated channel IDs to listen on (optional; all if empty)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
COMMAND_CHANNEL_IDS = [
    int(ch) for ch in os.environ.get("COMPANION_CHANNELS", "").split(",") if ch.strip()
]

# Paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "ozymandias" / "state"
SIGNALS_DIR = STATE_DIR / "signals"

# ---------------------------------------------------------------------------
# Intent filter — block informational questions from dispatch
# ---------------------------------------------------------------------------

_INFORMATIONAL_PATTERN = re.compile(
    r"^(what is|how does|explain|tell me about|describe)\b", re.IGNORECASE
)


def _is_informational(text: str) -> bool:
    """Return True if the message looks like a question, not a command."""
    return bool(_INFORMATIONAL_PATTERN.search(text))


# ---------------------------------------------------------------------------
# Signal file helpers (no ozymandias imports — pure file I/O)
# ---------------------------------------------------------------------------

def _touch(path: Path) -> None:
    """Create a touch file (and parent dirs if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _remove(path: Path) -> bool:
    """Remove a file. Returns True if deleted, False if it didn't exist."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _read_json(path: Path) -> dict | None:
    """Read a JSON file. Returns None on missing or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_task(description: str, source: str = "human") -> Path:
    """Write a task file to the agent task queue."""
    tasks_dir = STATE_DIR / "agent_tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc)
    filename = f"{ts.strftime('%Y%m%dT%H%M%S%f')}_{source}.json"
    path = tasks_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "type": "task",
            "source": source,
            "ts": ts.isoformat(),
            "description": description,
        }, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

COMMANDS: dict[str, str] = {
    "!pause": "Suppress new entries (PAUSE_ENTRIES signal)",
    "!resume": "Resume entries (remove PAUSE_ENTRIES signal)",
    "!status": "Show bot status from latest status.json",
    "!exit": "Emergency exit all positions",
    "!force-reasoning": "Trigger immediate Claude reasoning cycle",
    "!fix": "Submit a fix task to the agent queue",
    "!restart-conductor": "Restart the conductor wrapper cleanly",
    "!shutdown-conductor": "Shut down the conductor wrapper",
    "!approve": "Approve a pending agent permission request",
    "!deny": "Deny a pending agent permission request",
}


async def handle_command(content: str) -> str | None:
    """Parse and dispatch a command. Returns response text or None.

    Accepts raw message content (str) so it can be tested without a discord.Message object.
    """
    content = content.strip()

    if _is_informational(content):
        return None

    parts = content.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "!pause":
        _touch(STATE_DIR / "PAUSE_ENTRIES")
        return "Entries paused. Use `!resume` to lift."

    elif cmd == "!resume":
        removed = _remove(STATE_DIR / "PAUSE_ENTRIES")
        return "Entries resumed." if removed else "Entries were not paused."

    elif cmd == "!status":
        data = _read_json(SIGNALS_DIR / "status.json")
        if data is None:
            return "No status file found -- bot may not be running."
        return (
            f"**Equity:** ${data.get('equity', 0):,.2f}\n"
            f"**Positions:** {data.get('position_count', 0)}\n"
            f"**Open orders:** {data.get('open_order_count', 0)}\n"
            f"**Last update:** {data.get('ts', 'unknown')}\n"
            f"**Health:** {json.dumps(data.get('loop_health', {}))}"
        )

    elif cmd == "!exit":
        _touch(STATE_DIR / "EMERGENCY_EXIT")
        return "EMERGENCY EXIT signal written. All positions will be liquidated."

    elif cmd == "!force-reasoning":
        _touch(STATE_DIR / "FORCE_REASONING")
        return "FORCE_REASONING signal written. Next slow loop will trigger Claude."

    elif cmd == "!fix":
        if not args:
            return "Usage: `!fix <description of the issue>`"
        path = _write_task(args, source="human")
        return f"Task written: `{path.name}`"

    elif cmd == "!restart-conductor":
        conductor_dir = SIGNALS_DIR / "conductor"
        conductor_dir.mkdir(parents=True, exist_ok=True)
        (conductor_dir / "restart.json").write_text(
            json.dumps({"action": "restart", "source": "discord", "ts": datetime.now(timezone.utc).isoformat()})
        )
        return "Conductor restart signal written. Wrapper will restart on next poll."

    elif cmd == "!shutdown-conductor":
        conductor_dir = SIGNALS_DIR / "conductor"
        conductor_dir.mkdir(parents=True, exist_ok=True)
        (conductor_dir / "shutdown.json").write_text(
            json.dumps({"action": "shutdown", "source": "discord", "ts": datetime.now(timezone.utc).isoformat()})
        )
        return "Conductor shutdown signal written. Wrapper will exit on next poll."

    elif cmd in ("!approve", "!deny"):
        if not args:
            return f"Usage: `{cmd} <task-id>`"
        task_id = args.split()[0]
        decision = "approve" if cmd == "!approve" else "deny"
        conductor_dir = SIGNALS_DIR / "conductor"
        conductor_dir.mkdir(parents=True, exist_ok=True)
        (conductor_dir / "permission_response.json").write_text(
            json.dumps({
                "task_id": task_id,
                "decision": decision,
                "source": "discord",
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        )
        verb = "approved" if decision == "approve" else "denied"
        return f"Permission **{verb}** for task `{task_id}`."

    elif cmd == "!help":
        lines = ["**Available commands:**"]
        for c, desc in COMMANDS.items():
            lines.append(f"`{c}` -- {desc}")
        return "\n".join(lines)

    return None  # Unknown command — ignore


# ---------------------------------------------------------------------------
# Bot setup (only runs when executed directly, not when imported for tests)
# ---------------------------------------------------------------------------

def main():
    """Start the Discord companion bot."""
    try:
        import discord
    except ImportError:
        print(
            "Error: discord.py not installed. Run: pip install discord.py",
            file=sys.stderr,
        )
        sys.exit(1)

    if not TOKEN:
        print("Error: DISCORD_BOT_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"Discord companion connected as {client.user}")

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        if COMMAND_CHANNEL_IDS and message.channel.id not in COMMAND_CHANNEL_IDS:
            return
        if not message.content.startswith("!"):
            return

        response = await handle_command(message.content)
        if response:
            await message.channel.send(response)

    client.run(TOKEN)


if __name__ == "__main__":
    main()
