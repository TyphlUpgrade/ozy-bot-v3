"""
JSON state management with:
- Atomic writes (write to temp, os.replace to target)
- Schema validation on load
- asyncio.Lock per file to prevent concurrent writes
- First-run initialization of empty valid state files
- Typed read/write methods for portfolio, watchlist, and orders
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ozymandias.core.direction import Direction


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
PORTFOLIO_FILE    = STATE_DIR / "portfolio.json"
WATCHLIST_FILE    = STATE_DIR / "watchlist.json"
ORDERS_FILE       = STATE_DIR / "orders.json"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ExitTargets:
    profit_target: float = 0.0
    stop_loss: float = 0.0


@dataclass
class TradeIntention:
    catalyst: str = ""
    direction: Direction = "long"
    strategy: str = "momentum"         # "momentum" | "swing"
    expected_move: str = ""
    reasoning: str = ""
    exit_targets: ExitTargets = field(default_factory=ExitTargets)
    max_expected_loss: float = 0.0
    entry_date: str = ""               # ISO date string
    review_notes: list[str] = field(default_factory=list)


@dataclass
class Position:
    symbol: str
    shares: float
    avg_cost: float
    entry_date: str                    # ISO date string
    intention: TradeIntention = field(default_factory=TradeIntention)
    order_history: list[str] = field(default_factory=list)   # broker order IDs
    position_id: str = ""
    reconciled: bool = False           # True if detected during startup reconciliation


@dataclass
class PortfolioState:
    cash: float = 0.0
    buying_power: float = 0.0
    positions: list[Position] = field(default_factory=list)
    last_updated: str = ""


@dataclass
class WatchlistEntry:
    symbol: str
    date_added: str                    # ISO date string
    reason: str
    priority_tier: int = 2            # 1 = active candidate, 2 = monitoring, 3 = cooling off
    strategy: str = "both"            # "momentum" | "swing" | "both"
    removal_candidate: bool = False


@dataclass
class WatchlistState:
    entries: list[WatchlistEntry] = field(default_factory=list)
    last_updated: str = ""


@dataclass
class OrderRecord:
    order_id: str                      # broker-assigned
    symbol: str
    side: str                          # "buy" | "sell"
    quantity: float
    order_type: str                    # "market" | "limit"
    limit_price: Optional[float]
    status: str = "PENDING"            # PENDING | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED
    filled_quantity: float = 0.0
    remaining_quantity: float = 0.0
    created_at: str = ""
    last_checked_at: str = ""
    filled_at: str = ""
    cancelled_at: str = ""
    position_id: str = ""
    timeout_seconds: int = 60


@dataclass
class OrdersState:
    orders: list[OrderRecord] = field(default_factory=list)
    last_updated: str = ""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _to_dict(obj: Any) -> Any:
    """Recursively convert dataclass / list / dict to JSON-serialisable form."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _from_dict_position(d: dict) -> Position:
    intention_raw = d.get("intention", {})
    exit_targets_raw = intention_raw.get("exit_targets", {})
    intention = TradeIntention(
        catalyst=intention_raw.get("catalyst", ""),
        direction=intention_raw.get("direction", "long"),
        strategy=intention_raw.get("strategy", "momentum"),
        expected_move=intention_raw.get("expected_move", ""),
        reasoning=intention_raw.get("reasoning", ""),
        exit_targets=ExitTargets(
            profit_target=exit_targets_raw.get("profit_target", 0.0),
            stop_loss=exit_targets_raw.get("stop_loss", 0.0),
        ),
        max_expected_loss=intention_raw.get("max_expected_loss", 0.0),
        entry_date=intention_raw.get("entry_date", ""),
        review_notes=intention_raw.get("review_notes", []),
    )
    return Position(
        symbol=d["symbol"],
        shares=d["shares"],
        avg_cost=d["avg_cost"],
        entry_date=d["entry_date"],
        intention=intention,
        order_history=d.get("order_history", []),
        position_id=d.get("position_id", ""),
        reconciled=d.get("reconciled", False),
    )


def _from_dict_watchlist_entry(d: dict) -> WatchlistEntry:
    return WatchlistEntry(
        symbol=d["symbol"],
        date_added=d["date_added"],
        reason=d["reason"],
        priority_tier=d.get("priority_tier", 2),
        strategy=d.get("strategy", "both"),
        removal_candidate=d.get("removal_candidate", False),
    )


def _from_dict_order(d: dict) -> OrderRecord:
    return OrderRecord(
        order_id=d["order_id"],
        symbol=d["symbol"],
        side=d["side"],
        quantity=d["quantity"],
        order_type=d["order_type"],
        limit_price=d.get("limit_price"),
        status=d.get("status", "PENDING"),
        filled_quantity=d.get("filled_quantity", 0.0),
        remaining_quantity=d.get("remaining_quantity", 0.0),
        created_at=d.get("created_at", ""),
        last_checked_at=d.get("last_checked_at", ""),
        filled_at=d.get("filled_at", ""),
        cancelled_at=d.get("cancelled_at", ""),
        position_id=d.get("position_id", ""),
        timeout_seconds=d.get("timeout_seconds", 60),
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class StateValidationError(Exception):
    pass


def _validate_portfolio(data: dict) -> None:
    required = {"cash", "buying_power", "positions", "last_updated"}
    missing = required - set(data.keys())
    if missing:
        raise StateValidationError(f"portfolio.json missing required keys: {missing}")
    if not isinstance(data["positions"], list):
        raise StateValidationError("portfolio.json: 'positions' must be a list")
    for i, pos in enumerate(data["positions"]):
        for key in ("symbol", "shares", "avg_cost", "entry_date"):
            if key not in pos:
                raise StateValidationError(f"portfolio.json position[{i}] missing key: {key!r}")


def _validate_watchlist(data: dict) -> None:
    required = {"entries", "last_updated"}
    missing = required - set(data.keys())
    if missing:
        raise StateValidationError(f"watchlist.json missing required keys: {missing}")
    if not isinstance(data["entries"], list):
        raise StateValidationError("watchlist.json: 'entries' must be a list")
    for i, entry in enumerate(data["entries"]):
        for key in ("symbol", "date_added", "reason"):
            if key not in entry:
                raise StateValidationError(f"watchlist.json entry[{i}] missing key: {key!r}")


def _validate_orders(data: dict) -> None:
    required = {"orders", "last_updated"}
    missing = required - set(data.keys())
    if missing:
        raise StateValidationError(f"orders.json missing required keys: {missing}")
    if not isinstance(data["orders"], list):
        raise StateValidationError("orders.json: 'orders' must be a list")
    for i, order in enumerate(data["orders"]):
        for key in ("order_id", "symbol", "side", "quantity", "order_type"):
            if key not in order:
                raise StateValidationError(f"orders.json order[{i}] missing key: {key!r}")


# ---------------------------------------------------------------------------
# Empty state factories
# ---------------------------------------------------------------------------

def _empty_portfolio() -> dict:
    return {"cash": 0.0, "buying_power": 0.0, "positions": [], "last_updated": ""}


def _empty_watchlist() -> dict:
    return {"entries": [], "last_updated": ""}


def _empty_orders() -> dict:
    return {"orders": [], "last_updated": ""}


# ---------------------------------------------------------------------------
# Atomic file I/O
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON to a temp file in the same directory, then os.replace to target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

class StateManager:
    """
    Manages all persistent JSON state files with async locking and atomic writes.

    Usage::

        sm = StateManager()
        await sm.initialize()          # creates files if missing, validates schemas
        portfolio = await sm.load_portfolio()
        portfolio.cash = 30000.0
        await sm.save_portfolio(portfolio)
    """

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self._dir = Path(state_dir) if state_dir else STATE_DIR
        self._portfolio_lock = asyncio.Lock()
        self._watchlist_lock = asyncio.Lock()
        self._orders_lock    = asyncio.Lock()

    @property
    def portfolio_path(self) -> Path:
        return self._dir / "portfolio.json"

    @property
    def watchlist_path(self) -> Path:
        return self._dir / "watchlist.json"

    @property
    def orders_path(self) -> Path:
        return self._dir / "orders.json"

    async def initialize(self) -> None:
        """
        Prepare the state directory.

        - Creates the directory if it doesn't exist.
        - Creates empty state files for any that are missing.
        - Validates schemas on all existing files; raises StateValidationError
          on invalid state (refuse to start with uncertain state).
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        if not self.portfolio_path.exists():
            _atomic_write(self.portfolio_path, _empty_portfolio())
        if not self.watchlist_path.exists():
            _atomic_write(self.watchlist_path, _empty_watchlist())
        if not self.orders_path.exists():
            _atomic_write(self.orders_path, _empty_orders())

        # Validate all existing files
        _validate_portfolio(_read_json(self.portfolio_path))
        _validate_watchlist(_read_json(self.watchlist_path))
        _validate_orders(_read_json(self.orders_path))

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------

    async def load_portfolio(self) -> PortfolioState:
        async with self._portfolio_lock:
            data = _read_json(self.portfolio_path)
            _validate_portfolio(data)
            return PortfolioState(
                cash=data["cash"],
                buying_power=data["buying_power"],
                positions=[_from_dict_position(p) for p in data["positions"]],
                last_updated=data["last_updated"],
            )

    async def save_portfolio(self, state: PortfolioState) -> None:
        state.last_updated = datetime.now(timezone.utc).isoformat()
        async with self._portfolio_lock:
            _atomic_write(self.portfolio_path, _to_dict(state))

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    async def load_watchlist(self) -> WatchlistState:
        async with self._watchlist_lock:
            data = _read_json(self.watchlist_path)
            _validate_watchlist(data)
            return WatchlistState(
                entries=[_from_dict_watchlist_entry(e) for e in data["entries"]],
                last_updated=data["last_updated"],
            )

    async def save_watchlist(self, state: WatchlistState) -> None:
        state.last_updated = datetime.now(timezone.utc).isoformat()
        async with self._watchlist_lock:
            _atomic_write(self.watchlist_path, _to_dict(state))

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def load_orders(self) -> OrdersState:
        async with self._orders_lock:
            data = _read_json(self.orders_path)
            _validate_orders(data)
            return OrdersState(
                orders=[_from_dict_order(o) for o in data["orders"]],
                last_updated=data["last_updated"],
            )

    async def save_orders(self, state: OrdersState) -> None:
        state.last_updated = datetime.now(timezone.utc).isoformat()
        async with self._orders_lock:
            _atomic_write(self.orders_path, _to_dict(state))


