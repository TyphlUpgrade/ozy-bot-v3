# Phase 22: Signal File API + Bot Event Emitter

Read `plans/2026-04-07-agentic-workflow-v4-omc-only.md` § Phase A (lines ~1011-1039) and
§ Design Principles (signal files are the universal bus).

**Implementation dependency:** None — this is the foundation for all other agentic workflow phases.

**Context:** The bot already has `EMERGENCY_EXIT` and `EMERGENCY_SHUTDOWN` signal files
(touch-file pattern in `ozymandias/state/`). This phase extends that pattern into a structured
JSON event bus in `ozymandias/state/signals/`. The existing `_atomic_write()` in
`core/state_manager.py` provides the atomic write primitive (temp + `os.replace`).

---

## What to Build

### 1. Signal file directory convention

Create the directory structure under `ozymandias/state/signals/`:

```
ozymandias/state/signals/
├── status.json              # Bot health + equity snapshot (overwritten every fast tick)
├── last_trade.json          # Most recent entry/exit with full context (overwritten per trade)
├── last_review.json         # Most recent Claude position review (overwritten per review)
├── alerts/                  # Append-only alert files (one file per event)
│   └── <timestamp>_<type>.json
├── orchestrator/            # Conductor heartbeat + lifecycle (Phase E writes here)
├── conductor/               # Conductor control signals (restart, shutdown)
├── architect/               # Architect plan/review signals, scoped by task-id (Phase E)
├── reviewer/                # Reviewer verdict signals, scoped by task-id (Phase E)
├── analyst/                 # Strategy Analyst findings (Phase D)
├── dialogue/                # Dialogue agent responses (Phase B.5)
└── agent_tasks/             # Inbound task queue for Conductor (Phase E)
```

Phase A creates the base directories and implements writers for `status.json`, `last_trade.json`,
`last_review.json`, and `alerts/`. The other subdirectories are created as empty placeholders
(with `.gitkeep` files) — they are populated by later phases.

### 2. Signal writer utility (`core/signals.py`)

New file: `ozymandias/core/signals.py`

```python
"""
Signal file bus — structured JSON events for inter-system communication.

The signal bus is the universal communication mechanism for the agentic workflow.
Trading bot, ops agents, dev agents, and the clawhip daemon all communicate through
structured JSON files in state/signals/.

To add a new signal type: add a write function here and document the schema.
"""

from pathlib import Path
from datetime import datetime, timezone
import json
import os
import tempfile
from typing import Any

from ozymandias.core.logger import get_logger

log = get_logger(__name__)

SIGNALS_DIR = Path(__file__).resolve().parent.parent / "state" / "signals"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file + os.replace. Same pattern as state_manager."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_status(
    equity: float,
    positions: list[dict],
    open_orders: list[dict],
    loop_health: dict,
) -> None:
    """Write bot status snapshot. Overwritten every fast tick."""
    _atomic_write_json(SIGNALS_DIR / "status.json", {
        "type": "status",
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": equity,
        "position_count": len(positions),
        "positions": positions,
        "open_order_count": len(open_orders),
        "open_orders": open_orders,
        "loop_health": loop_health,
    })


def write_last_trade(
    symbol: str,
    action: str,
    shares: float,
    price: float,
    order_id: str,
    context: dict | None = None,
) -> None:
    """Write most recent trade. Overwritten per trade."""
    _atomic_write_json(SIGNALS_DIR / "last_trade.json", {
        "type": "last_trade",
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "action": action,
        "shares": shares,
        "price": price,
        "order_id": order_id,
        "context": context or {},
    })


def write_last_review(
    symbol: str,
    action: str,
    reasoning_summary: str,
    context: dict | None = None,
) -> None:
    """Write most recent Claude position review. Overwritten per review."""
    _atomic_write_json(SIGNALS_DIR / "last_review.json", {
        "type": "last_review",
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "action": action,
        "reasoning_summary": reasoning_summary,
        "context": context or {},
    })


def write_alert(
    alert_type: str,
    severity: str,
    message: str,
    context: dict | None = None,
) -> None:
    """Write an alert file. Append-only — one file per event, never overwritten."""
    ts = datetime.now(timezone.utc)
    filename = f"{ts.strftime('%Y%m%dT%H%M%S')}_{alert_type}.json"
    alerts_dir = SIGNALS_DIR / "alerts"
    _atomic_write_json(alerts_dir / filename, {
        "type": "alert",
        "alert_type": alert_type,
        "severity": severity,
        "ts": ts.isoformat(),
        "message": message,
        "context": context or {},
    })
    log.info("Alert written: %s — %s", alert_type, message)


def read_signal(path: Path) -> dict | None:
    """Read a signal file. Returns None if file doesn't exist or is malformed."""
    try:
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read signal %s: %s", path, exc)
        return None


def check_inbound_signal(signal_name: str) -> bool:
    """Check if an inbound signal file exists (touch-file pattern).
    
    Inbound signals: PAUSE_ENTRIES, FORCE_REASONING, FORCE_BUILD.
    These are touch files in state/ (not state/signals/) for backward
    compatibility with the existing EMERGENCY_* pattern.
    """
    return (STATE_DIR / signal_name).exists()


def consume_inbound_signal(signal_name: str) -> bool:
    """Check and consume an inbound signal file (read-once pattern).
    
    Returns True if signal was present and consumed, False otherwise.
    """
    path = STATE_DIR / signal_name
    if path.exists():
        try:
            path.unlink()
            log.info("Consumed inbound signal: %s", signal_name)
            return True
        except OSError as exc:
            log.warning("Failed to consume signal %s: %s", signal_name, exc)
    return False


def ensure_signal_dirs() -> None:
    """Create all signal directories on startup. Idempotent."""
    dirs = [
        SIGNALS_DIR,
        SIGNALS_DIR / "alerts",
        SIGNALS_DIR / "orchestrator",
        SIGNALS_DIR / "conductor",
        SIGNALS_DIR / "architect",
        SIGNALS_DIR / "reviewer",
        SIGNALS_DIR / "analyst",
        SIGNALS_DIR / "dialogue",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Agent task queue lives outside signals/ (it's an input queue, not an event bus)
    (STATE_DIR / "agent_tasks").mkdir(parents=True, exist_ok=True)
```

### 3. Bot event emitters (orchestrator integration)

Wire signal writes into the existing orchestrator loops. These are fire-and-forget calls —
no new dependencies, no awaits needed (signal writes are sync and fast).

**In the fast loop** (`_fast_loop` in `orchestrator.py`):
- After the existing `_check_emergency_signals()`, add:
  - `check_inbound_signal("PAUSE_ENTRIES")` → set `self._entries_paused = True`
  - If `PAUSE_ENTRIES` is absent and `self._entries_paused` is True → resume
  - `consume_inbound_signal("FORCE_REASONING")` → set `self._force_reasoning = True`
  - `consume_inbound_signal("FORCE_BUILD")` → set `self._force_build = True`
- At the end of each fast tick: `write_status()` with current equity, positions, orders, and
  loop health metrics (tick count, last tick duration, errors since last success).

**In fill handling** (`FillHandler` or orchestrator fill callback):
- After a fill is processed: `write_last_trade()` with fill details.

**In the slow loop** (after Claude reasoning completes):
- `write_last_review()` with the Claude review summary.

**Alert emitters** (in existing error paths):
- `write_alert("equity_drawdown", "WARNING", ...)` when equity drops >2% in a session
- `write_alert("loop_stall", "ERROR", ...)` when a loop tick exceeds 60 seconds
- `write_alert("broker_error", "WARNING", ...)` on repeated broker API failures

**On startup** (`_startup` in `orchestrator.py`):
- Call `ensure_signal_dirs()` to create all directories.

### 4. Inbound signal handling

Add three new inbound signals using the existing touch-file pattern (same as `EMERGENCY_*`):

| Signal file | Behavior | Consumed on read? |
|------------|----------|-------------------|
| `state/PAUSE_ENTRIES` | Suppress new entries. Existing positions unaffected. | No — persists until removed |
| `state/FORCE_REASONING` | Trigger immediate slow loop cycle | Yes — one-shot |
| `state/FORCE_BUILD` | Trigger immediate watchlist build | Yes — one-shot |

These live in `state/` (not `state/signals/`) for backward compatibility with the existing
`EMERGENCY_EXIT` / `EMERGENCY_SHUTDOWN` touch-file convention. The operator or Discord
companion creates them with `touch`.

---

## Tests to Write

Create `ozymandias/tests/test_signals.py`:

### Signal writer tests
- `test_write_status_creates_valid_json` — write status, read back, verify all fields present
- `test_write_status_overwrites` — write twice, verify only latest data present
- `test_write_last_trade_schema` — verify all required fields in output
- `test_write_last_review_schema` — verify all required fields in output
- `test_write_alert_creates_unique_files` — write two alerts, verify two separate files exist
- `test_write_alert_append_only` — write an alert, verify it isn't overwritten by a second alert
- `test_atomic_write_survives_crash` — verify no partial writes (temp file cleaned up on error)

### Signal reader tests
- `test_read_signal_valid` — write a signal, read it back, verify contents match
- `test_read_signal_missing` — read nonexistent file, verify returns None
- `test_read_signal_malformed` — write invalid JSON, verify returns None (no crash)

### Inbound signal tests
- `test_check_inbound_signal_present` — create touch file, verify `check_inbound_signal` returns True
- `test_check_inbound_signal_absent` — verify returns False when file doesn't exist
- `test_consume_inbound_signal` — create touch file, consume, verify file deleted and returns True
- `test_consume_inbound_signal_absent` — verify returns False when file doesn't exist

### Directory setup tests
- `test_ensure_signal_dirs_creates_all` — run `ensure_signal_dirs()`, verify all directories exist
- `test_ensure_signal_dirs_idempotent` — run twice, verify no errors

---

## Done When

1. `pytest ozymandias/tests/test_signals.py` passes — all signal read/write/consume tests green
2. `ozymandias/state/signals/` directory structure is created on bot startup
3. Bot runs normally with `write_status()` writing to `state/signals/status.json` every fast tick
4. `touch ozymandias/state/PAUSE_ENTRIES` pauses new entries; `rm` resumes
5. `touch ozymandias/state/FORCE_REASONING` triggers immediate slow loop cycle (one-shot)
6. Signal files are well-formed JSON readable by `jq` (machine-readable for future agents)
7. Alert files accumulate in `state/signals/alerts/` without overwriting each other
8. No new dependencies added — only stdlib (`json`, `os`, `tempfile`, `pathlib`)
