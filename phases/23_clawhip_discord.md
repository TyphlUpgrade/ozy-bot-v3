# Phase 23: clawhip + Discord Companion

Read `plans/2026-04-07-agentic-workflow-v4-omc-only.md` § Phase B (lines ~1041-1143).

**Implementation dependency:** Phase 22 (Signal File API) — signal files must exist for clawhip
to monitor and for the companion to read/write.

**Context:** The bot writes structured JSON signal files to `state/signals/` (Phase 22). This
phase adds two independent processes that bridge those signals to Discord: clawhip (outbound
file-watching daemon) and a thin Python companion (inbound Discord commands).

---

## What to Build

### 1. clawhip configuration (`clawhip.toml`)

New file at project root: `clawhip.toml`

clawhip is an external binary (installed separately, not a Python dependency). This file
configures it to watch signal files and route events to Discord channels.

```toml
[providers.discord]
token = "${DISCORD_BOT_TOKEN}"
default_channel = "${ALERTS_CHANNEL}"

[daemon]
bind = "127.0.0.1:25294"

# --- Monitors ---

[[monitors]]
kind = "workspace"
path = "state/signals/"
poll_interval_secs = 5
debounce_ms = 2000

[[monitors]]
kind = "git"
path = "."
poll_interval_secs = 30

# --- Routes ---

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/last_trade.json" }
sink = "discord"
channel = "${TRADES_CHANNEL}"
format = "compact"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/alerts/*" }
sink = "discord"
channel = "${ALERTS_CHANNEL}"
format = "alert"
mention = "${OPERATOR_MENTION}"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/last_review.json" }
sink = "discord"
channel = "${REVIEWS_CHANNEL}"
format = "compact"

[[routes]]
event = "git.commit"
sink = "discord"
channel = "${DEV_CHANNEL}"
format = "compact"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/agent_tasks/*" }
sink = "discord"
channel = "${AGENT_CHANNEL}"
format = "compact"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/executor/checkpoint.json" }
sink = "discord"
channel = "${AGENT_CHANNEL}"
format = "alert"
```

Environment variables required (operator task to configure):
- `DISCORD_BOT_TOKEN` — Discord bot token
- `ALERTS_CHANNEL`, `TRADES_CHANNEL`, `REVIEWS_CHANNEL`, `DEV_CHANNEL`, `AGENT_CHANNEL` — channel IDs
- `OPERATOR_MENTION` — Discord user mention string for alerts

External dependency: `inotify-tools` on Linux (`sudo pacman -S inotify-tools`).

### 2. Discord companion (`tools/discord_companion.py`, ~150 lines)

New file: `tools/discord_companion.py`

A standalone Python script using `discord.py` that listens for commands in configured channels.
**Does not import from `ozymandias/`.** Reads and writes JSON signal files only.

```python
#!/usr/bin/env python3
"""
Discord companion — inbound command handler for the Ozymandias trading bot.

Listens for commands in Discord channels and translates them to signal files.
Does NOT import from ozymandias/ — communicates entirely through JSON files.

Requires: discord.py (`pip install discord.py`)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _remove(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _read_json(path: Path) -> dict | None:
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
}


async def handle_command(message: discord.Message) -> str | None:
    """Parse and dispatch a command. Returns response text or None."""
    content = message.content.strip()

    if _is_informational(content):
        return None  # Not a command — ignore

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
            return "No status file found — bot may not be running."
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

    elif cmd == "!help":
        lines = ["**Available commands:**"]
        for c, desc in COMMANDS.items():
            lines.append(f"`{c}` — {desc}")
        return "\n".join(lines)

    return None  # Unknown command — ignore


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Discord companion connected as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return
    if COMMAND_CHANNEL_IDS and message.channel.id not in COMMAND_CHANNEL_IDS:
        return
    if not message.content.startswith("!"):
        return

    response = await handle_command(message)
    if response:
        await message.channel.send(response)


def main():
    if not TOKEN:
        print("Error: DISCORD_BOT_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)
    client.run(TOKEN)


if __name__ == "__main__":
    main()
```

### 3. tmux session management notes

clawhip can monitor tmux sessions for keyword detection and stale timeouts. This is configured
in `clawhip.toml` when the operator sets up the tmux layout. No code to write — just document
the pattern:

```toml
# Add to clawhip.toml when tmux sessions are set up:
# [[monitors]]
# kind = "tmux"
# session = "ozy-bot"
# keywords = ["error", "FAILED", "panic", "CRITICAL"]
# stale_timeout_secs = 1800
```

---

## Tests to Write

Create `ozymandias/tests/test_discord_companion.py`:

### Command parsing tests
- `test_pause_creates_signal_file` — verify `!pause` creates `state/PAUSE_ENTRIES`
- `test_resume_removes_signal_file` — verify `!resume` removes `state/PAUSE_ENTRIES`
- `test_resume_when_not_paused` — verify `!resume` returns appropriate message
- `test_status_reads_signal` — write a status.json, verify `!status` returns formatted data
- `test_status_missing` — verify returns "bot may not be running" when no status file
- `test_exit_creates_signal` — verify `!exit` creates `state/EMERGENCY_EXIT`
- `test_force_reasoning_creates_signal` — verify `!force-reasoning` creates `state/FORCE_REASONING`
- `test_fix_writes_task` — verify `!fix some issue` creates task file in `state/agent_tasks/`
- `test_fix_no_args` — verify `!fix` with no arguments returns usage hint

### Intent filter tests
- `test_informational_filtered` — verify "what is the current status" is filtered
- `test_command_not_filtered` — verify "!pause" is not filtered
- `test_unknown_command_ignored` — verify "!nonexistent" returns None

### Signal file helpers
- `test_touch_creates_file` — verify `_touch()` creates parent dirs and file
- `test_remove_existing` — verify `_remove()` deletes file and returns True
- `test_remove_missing` — verify `_remove()` returns False for nonexistent file
- `test_read_json_valid` — write JSON, read it back
- `test_read_json_missing` — verify returns None for nonexistent file

---

## Done When

1. `clawhip.toml` exists at project root with all route configurations
2. `tools/discord_companion.py` exists and handles all 6 commands + `!help`
3. Companion does NOT import from `ozymandias/` — pure file I/O only
4. `pytest ozymandias/tests/test_discord_companion.py` passes — all command parsing tests green
5. Intent filter blocks informational questions from being dispatched as commands
6. Signal file paths match Phase 22 conventions (`state/PAUSE_ENTRIES`, `state/signals/status.json`, etc.)
7. Task files written by `!fix` use the same directory as Phase 22's `agent_tasks/`
8. No new Python dependencies beyond `discord.py` (which is already in requirements.txt or added)
