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
    filename = f"{ts.strftime('%Y%m%dT%H%M%S%f')}_{alert_type}.json"
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
        SIGNALS_DIR / "executor",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # Agent task queue lives outside signals/ (it's an input queue, not an event bus)
    (STATE_DIR / "agent_tasks").mkdir(parents=True, exist_ok=True)
