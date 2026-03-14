"""
Tests for core/state_manager.py

Run with: pytest tests/test_state_manager.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from ozymandias.core.state_manager import (
    ExitTargets,
    OrderRecord,
    OrdersState,
    Position,
    PortfolioState,
    StateManager,
    StateValidationError,
    TradeIntention,
    WatchlistEntry,
    WatchlistState,
    _atomic_write,
    _empty_orders,
    _empty_portfolio,
    _empty_watchlist,
)


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def sm(tmp_state_dir: Path) -> StateManager:
    return StateManager(state_dir=tmp_state_dir)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initialize_creates_empty_files(sm: StateManager, tmp_state_dir: Path):
    await sm.initialize()
    assert sm.portfolio_path.exists()
    assert sm.watchlist_path.exists()
    assert sm.orders_path.exists()


@pytest.mark.asyncio
async def test_initialize_creates_valid_state_files(sm: StateManager):
    await sm.initialize()
    portfolio = await sm.load_portfolio()
    watchlist = await sm.load_watchlist()
    orders = await sm.load_orders()

    assert portfolio.positions == []
    assert portfolio.cash == 0.0
    assert watchlist.entries == []
    assert orders.orders == []


@pytest.mark.asyncio
async def test_initialize_idempotent_does_not_overwrite(sm: StateManager):
    """Second initialize() should not blow away an existing valid state file."""
    await sm.initialize()
    portfolio = await sm.load_portfolio()
    portfolio.cash = 99999.0
    await sm.save_portfolio(portfolio)

    # second initialize — must keep the file intact
    await sm.initialize()
    reloaded = await sm.load_portfolio()
    assert reloaded.cash == 99999.0


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def test_atomic_write_creates_file(tmp_path: Path):
    target = tmp_path / "test.json"
    _atomic_write(target, {"key": "value"})
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["key"] == "value"


def test_atomic_write_no_temp_file_left_on_success(tmp_path: Path):
    target = tmp_path / "test.json"
    _atomic_write(target, {"x": 1})
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], "temp file should be cleaned up after successful write"


def test_atomic_write_overwrites_existing(tmp_path: Path):
    target = tmp_path / "test.json"
    _atomic_write(target, {"v": 1})
    _atomic_write(target, {"v": 2})
    data = json.loads(target.read_text())
    assert data["v"] == 2


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schema_validation_rejects_missing_portfolio_keys(sm: StateManager, tmp_state_dir: Path):
    """A malformed portfolio.json should raise StateValidationError, not allow startup."""
    await sm.initialize()
    # Write a broken file
    broken = {"cash": 0.0}  # missing 'positions', 'buying_power', 'last_updated'
    _atomic_write(sm.portfolio_path, broken)

    with pytest.raises(StateValidationError, match="portfolio.json"):
        await sm.load_portfolio()


@pytest.mark.asyncio
async def test_schema_validation_rejects_missing_watchlist_keys(sm: StateManager):
    await sm.initialize()
    _atomic_write(sm.watchlist_path, {"not_entries": []})

    with pytest.raises(StateValidationError, match="watchlist.json"):
        await sm.load_watchlist()


@pytest.mark.asyncio
async def test_schema_validation_rejects_missing_orders_keys(sm: StateManager):
    await sm.initialize()
    _atomic_write(sm.orders_path, {"wrong_key": []})

    with pytest.raises(StateValidationError, match="orders.json"):
        await sm.load_orders()


@pytest.mark.asyncio
async def test_schema_validation_rejects_bad_position_entry(sm: StateManager):
    await sm.initialize()
    bad_portfolio = {
        "cash": 0.0,
        "buying_power": 0.0,
        "positions": [{"symbol": "AAPL"}],  # missing shares, avg_cost, entry_date
        "last_updated": "",
    }
    _atomic_write(sm.portfolio_path, bad_portfolio)

    with pytest.raises(StateValidationError):
        await sm.load_portfolio()


# ---------------------------------------------------------------------------
# Load / save roundtrip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_portfolio_roundtrip(sm: StateManager):
    await sm.initialize()
    state = PortfolioState(
        cash=28500.0,
        buying_power=28500.0,
        positions=[
            Position(
                symbol="NVDA",
                shares=15.0,
                avg_cost=142.30,
                entry_date="2025-03-10",
                intention=TradeIntention(
                    catalyst="AI chip demand",
                    direction="long",
                    strategy="momentum",
                    expected_move="+8-12%",
                    reasoning="strong momentum",
                    exit_targets=ExitTargets(profit_target=155.0, stop_loss=138.0),
                    max_expected_loss=-64.50,
                    entry_date="2025-03-10",
                ),
                order_history=["ord_001"],
                position_id="pos_nvda_001",
            )
        ],
    )
    await sm.save_portfolio(state)
    loaded = await sm.load_portfolio()

    assert loaded.cash == 28500.0
    assert len(loaded.positions) == 1
    pos = loaded.positions[0]
    assert pos.symbol == "NVDA"
    assert pos.shares == 15.0
    assert pos.avg_cost == 142.30
    assert pos.intention.catalyst == "AI chip demand"
    assert pos.intention.exit_targets.profit_target == 155.0
    assert pos.order_history == ["ord_001"]


@pytest.mark.asyncio
async def test_watchlist_roundtrip(sm: StateManager):
    await sm.initialize()
    state = WatchlistState(
        entries=[
            WatchlistEntry(
                symbol="TSLA",
                date_added="2025-03-10",
                reason="Strong momentum",
                priority_tier=1,
                strategy="momentum",
            ),
            WatchlistEntry(
                symbol="AAPL",
                date_added="2025-03-10",
                reason="Swing setup",
                priority_tier=2,
                strategy="swing",
                removal_candidate=True,
            ),
        ]
    )
    await sm.save_watchlist(state)
    loaded = await sm.load_watchlist()

    assert len(loaded.entries) == 2
    tsla = loaded.entries[0]
    assert tsla.symbol == "TSLA"
    assert tsla.priority_tier == 1
    aapl = loaded.entries[1]
    assert aapl.removal_candidate is True


@pytest.mark.asyncio
async def test_orders_roundtrip(sm: StateManager):
    await sm.initialize()
    state = OrdersState(
        orders=[
            OrderRecord(
                order_id="ord_abc123",
                symbol="NVDA",
                side="buy",
                quantity=10.0,
                order_type="limit",
                limit_price=142.00,
                status="PARTIALLY_FILLED",
                filled_quantity=5.0,
                remaining_quantity=5.0,
                created_at="2025-03-10T14:00:00Z",
                position_id="pos_nvda_001",
            )
        ]
    )
    await sm.save_orders(state)
    loaded = await sm.load_orders()

    assert len(loaded.orders) == 1
    order = loaded.orders[0]
    assert order.order_id == "ord_abc123"
    assert order.status == "PARTIALLY_FILLED"
    assert order.filled_quantity == 5.0
    assert order.remaining_quantity == 5.0
    assert order.limit_price == 142.00


# ---------------------------------------------------------------------------
# Concurrent write safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_portfolio_writes_are_safe(sm: StateManager):
    """Two async tasks writing portfolio simultaneously should not corrupt state."""
    await sm.initialize()

    async def write_cash(amount: float) -> None:
        state = await sm.load_portfolio()
        state.cash = amount
        await sm.save_portfolio(state)

    # Fire both writes concurrently
    await asyncio.gather(write_cash(10000.0), write_cash(20000.0))

    # File must be valid JSON with one of the two values
    loaded = await sm.load_portfolio()
    assert loaded.cash in (10000.0, 20000.0)


@pytest.mark.asyncio
async def test_concurrent_watchlist_writes_are_safe(sm: StateManager):
    await sm.initialize()

    async def write_entry(symbol: str) -> None:
        state = await sm.load_watchlist()
        state.entries.append(
            WatchlistEntry(symbol=symbol, date_added="2025-03-10", reason="test")
        )
        await sm.save_watchlist(state)

    await asyncio.gather(
        write_entry("AAPL"),
        write_entry("TSLA"),
        write_entry("NVDA"),
    )

    loaded = await sm.load_watchlist()
    # All writes complete; state file is valid
    assert len(loaded.entries) >= 1


# ---------------------------------------------------------------------------
# last_updated is stamped on save
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_stamps_last_updated(sm: StateManager):
    await sm.initialize()
    state = await sm.load_portfolio()
    assert state.last_updated == ""
    await sm.save_portfolio(state)
    saved = await sm.load_portfolio()
    assert saved.last_updated != ""
