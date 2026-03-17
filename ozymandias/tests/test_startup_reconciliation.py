"""
tests/test_startup_reconciliation.py
=====================================
Unit tests for Orchestrator.startup_reconciliation().

All broker calls are mocked. Each test seeds specific state, runs the
reconciliation protocol, and asserts on the resulting portfolio/orders
and on whether conservative mode was activated.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from ozymandias.core.orchestrator import Orchestrator
from ozymandias.core.state_manager import (
    ExitTargets,
    OrderRecord,
    OrdersState,
    PortfolioState,
    Position,
    TradeIntention,
    WatchlistState,
)
from ozymandias.execution.broker_interface import (
    AccountInfo,
    BrokerPosition,
    MarketHours,
    OrderStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_account() -> AccountInfo:
    return AccountInfo(
        equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
        currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
    )


def _stub_hours(session: str = "regular") -> MarketHours:
    now = datetime.now(timezone.utc)
    return MarketHours(
        is_open=True, next_open=now, next_close=now + timedelta(hours=6),
        session=session,
    )


def _broker_pos(symbol: str, qty: float, avg: float = 100.0) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol, qty=qty, avg_entry_price=avg,
        current_price=avg, market_value=qty * avg, unrealized_pl=0.0,
    )


def _order_status(order_id: str, symbol: str = "AAPL", side: str = "buy") -> OrderStatus:
    now = datetime.now(timezone.utc)
    return OrderStatus(
        order_id=order_id, symbol=symbol, side=side,
        status="new", filled_qty=0.0, remaining_qty=10.0,
        filled_avg_price=None, submitted_at=now,
        filled_at=None, canceled_at=None,
    )


def _local_position(
    symbol: str,
    shares: float = 10.0,
    avg_cost: float = 100.0,
) -> Position:
    return Position(
        symbol=symbol,
        shares=shares,
        avg_cost=avg_cost,
        entry_date="2026-03-01",
        intention=TradeIntention(),
    )


@pytest_asyncio.fixture
async def orch(tmp_path):
    """Orchestrator with _startup() done and a configurable broker mock."""
    with (
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.__init__",
              MagicMock(return_value=None)),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_account",
              AsyncMock(return_value=_stub_account())),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours",
              AsyncMock(return_value=_stub_hours())),
        patch("anthropic.AsyncAnthropic", MagicMock),
        patch("ozymandias.core.orchestrator.Orchestrator._load_credentials",
              MagicMock(return_value=("k", "s"))),
    ):
        o = Orchestrator()
        o._state_manager._dir = tmp_path
        o._reasoning_cache._dir = tmp_path / "cache"
        o._reasoning_cache._dir.mkdir()
        await o._startup()

    broker = MagicMock()
    broker.get_account     = AsyncMock(return_value=_stub_account())
    broker.get_open_orders = AsyncMock(return_value=[])
    broker.get_positions   = AsyncMock(return_value=[])
    broker.place_order     = AsyncMock()
    broker.cancel_order    = AsyncMock()
    o._broker = broker
    return o


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStartupReconciliation:

    @pytest.mark.asyncio
    async def test_clean_startup_no_errors(self, orch):
        """Clean startup: broker and local agree → no conservative mode."""
        # Seed matching state
        portfolio = PortfolioState(
            cash=50_000.0, buying_power=80_000.0,
            positions=[_local_position("AAPL", shares=10.0)],
        )
        await orch._state_manager.save_portfolio(portfolio)
        orch._broker.get_positions = AsyncMock(return_value=[
            _broker_pos("AAPL", qty=10.0),
        ])

        await orch.startup_reconciliation()

        assert orch._conservative_mode_until is None
        loaded = await orch._state_manager.load_portfolio()
        assert len(loaded.positions) == 1
        assert loaded.positions[0].symbol == "AAPL"
        assert loaded.positions[0].shares == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_position_mismatch_updates_local(self, orch):
        """Broker has 15 shares, local has 10 → local updated to 15."""
        portfolio = PortfolioState(
            cash=50_000.0, buying_power=80_000.0,
            positions=[_local_position("AAPL", shares=10.0)],
        )
        await orch._state_manager.save_portfolio(portfolio)
        orch._broker.get_positions = AsyncMock(return_value=[
            _broker_pos("AAPL", qty=15.0),
        ])

        await orch.startup_reconciliation()

        loaded = await orch._state_manager.load_portfolio()
        assert loaded.positions[0].shares == pytest.approx(15.0)
        # Mismatch is an error → conservative mode should activate
        assert orch._conservative_mode_until is not None
        assert orch._conservative_mode_until > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_phantom_local_position_removed(self, orch):
        """Local has TSLA but broker has nothing → TSLA removed."""
        portfolio = PortfolioState(
            cash=50_000.0, buying_power=80_000.0,
            positions=[_local_position("TSLA", shares=5.0)],
        )
        await orch._state_manager.save_portfolio(portfolio)
        orch._broker.get_positions = AsyncMock(return_value=[])  # broker is empty

        await orch.startup_reconciliation()

        loaded = await orch._state_manager.load_portfolio()
        assert not any(p.symbol == "TSLA" for p in loaded.positions)
        assert orch._conservative_mode_until is not None

    @pytest.mark.asyncio
    async def test_unknown_broker_position_added_with_reconciled_flag(self, orch):
        """Broker has NVDA not tracked locally → added with reconciled=True."""
        await orch._state_manager.save_portfolio(
            PortfolioState(cash=50_000.0, buying_power=80_000.0, positions=[])
        )
        orch._broker.get_positions = AsyncMock(return_value=[
            _broker_pos("NVDA", qty=8.0, avg=870.0),
        ])

        await orch.startup_reconciliation()

        loaded = await orch._state_manager.load_portfolio()
        nvda = next((p for p in loaded.positions if p.symbol == "NVDA"), None)
        assert nvda is not None
        assert nvda.shares == pytest.approx(8.0)
        assert nvda.avg_cost == pytest.approx(870.0)
        assert nvda.reconciled is True
        assert orch._conservative_mode_until is not None

    @pytest.mark.asyncio
    async def test_stale_local_orders_marked_cancelled(self, orch):
        """Local PENDING order not found broker-side → marked CANCELLED."""
        # Seed a PENDING order locally
        orders = OrdersState(orders=[
            OrderRecord(
                order_id="old-001",
                symbol="AAPL",
                side="buy",
                quantity=10.0,
                order_type="limit",
                limit_price=200.0,
                status="PENDING",
                created_at="2026-03-01T00:00:00+00:00",
            )
        ])
        await orch._state_manager.save_orders(orders)

        # Broker has no open orders
        orch._broker.get_open_orders = AsyncMock(return_value=[])

        await orch.startup_reconciliation()

        loaded = await orch._state_manager.load_orders()
        order = next(o for o in loaded.orders if o.order_id == "old-001")
        assert order.status == "CANCELLED"
        assert order.cancelled_at != ""

    @pytest.mark.asyncio
    async def test_broker_short_position_adopted_with_correct_direction(self, orch):
        """
        Broker reports side='short' → adopted position has direction='short'.

        Regression for Bug #3 (2026-03-16): startup_reconciliation used bare
        TradeIntention() which defaults to direction='long', causing quant
        overrides to fire SELL orders on newly-adopted short positions.
        """
        await orch._state_manager.save_portfolio(PortfolioState())
        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="AMD", qty=-30.0, avg_entry_price=198.07,
                current_price=195.0, market_value=-5942.1, unrealized_pl=90.0,
                side="short",
            )
        ])

        await orch.startup_reconciliation()

        loaded = await orch._state_manager.load_portfolio()
        pos = next((p for p in loaded.positions if p.symbol == "AMD"), None)
        assert pos is not None, "AMD not adopted"
        assert pos.intention.direction == "short", (
            f"Expected direction='short' for broker short, got '{pos.intention.direction}'"
        )
        assert pos.shares == pytest.approx(30.0), "Shares must be stored as positive"

    @pytest.mark.asyncio
    async def test_broker_long_position_adopted_with_long_direction(self, orch):
        """Broker reports side='long' → adopted position has direction='long'."""
        await orch._state_manager.save_portfolio(PortfolioState())
        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="AAPL", qty=10.0, avg_entry_price=200.0,
                current_price=205.0, market_value=2050.0, unrealized_pl=50.0,
                side="long",
            )
        ])

        await orch.startup_reconciliation()

        loaded = await orch._state_manager.load_portfolio()
        pos = next((p for p in loaded.positions if p.symbol == "AAPL"), None)
        assert pos is not None
        assert pos.intention.direction == "long"

    @pytest.mark.asyncio
    async def test_conservative_mode_activates_on_any_error(self, orch):
        """Any reconciliation error → conservative_mode_until is set to ~now + configured minutes."""
        cfg_mins = orch._config.scheduler.conservative_startup_mode_min

        # Create a position mismatch (guaranteed error)
        portfolio = PortfolioState(
            cash=50_000.0, buying_power=80_000.0,
            positions=[_local_position("AAPL", shares=5.0)],
        )
        await orch._state_manager.save_portfolio(portfolio)
        orch._broker.get_positions = AsyncMock(return_value=[
            _broker_pos("AAPL", qty=99.0),  # large mismatch
        ])

        before = datetime.now(timezone.utc)
        await orch.startup_reconciliation()
        after = datetime.now(timezone.utc)

        assert orch._conservative_mode_until is not None
        # Should be approximately now + cfg_mins
        expected_min = before + timedelta(minutes=cfg_mins) - timedelta(seconds=2)
        expected_max = after  + timedelta(minutes=cfg_mins) + timedelta(seconds=2)
        assert expected_min <= orch._conservative_mode_until <= expected_max
