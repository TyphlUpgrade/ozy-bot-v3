"""
tests/test_integration.py
==========================
End-to-end integration tests. All external services (broker, yfinance, Claude)
are mocked. The real orchestrator logic runs.

These tests exercise full loop cycles and verify cross-module interactions:
ordering, risk validation, PDT enforcement, degradation handling.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, time as dtime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
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
    WatchlistEntry,
    WatchlistState,
)
from ozymandias.execution.broker_interface import (
    AccountInfo,
    BrokerPosition,
    MarketHours,
    OrderResult,
    OrderStatus,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _stub_account(
    equity: float = 100_000.0,
    buying_power: float = 80_000.0,
    daytrade_count: int = 0,
    pdt_flag: bool = False,
) -> AccountInfo:
    return AccountInfo(
        equity=equity, buying_power=buying_power,
        cash=equity * 0.5, currency="USD",
        pdt_flag=pdt_flag, daytrade_count=daytrade_count, account_id="test-001",
    )


def _stub_hours(session: str = "regular") -> MarketHours:
    now = datetime.now(timezone.utc)
    return MarketHours(
        is_open=True,
        next_open=now - timedelta(hours=1),
        next_close=now + timedelta(hours=5),
        session=session,
    )


def _make_bars(n: int = 60, base_price: float = 200.0) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame — enough bars for all TA indicators."""
    np.random.seed(42)
    close = base_price + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame({
        "open":   close * 0.999,
        "high":   close * 1.005,
        "low":    close * 0.995,
        "close":  close,
        "volume": np.random.randint(500_000, 2_000_000, size=n).astype(float),
    }, index=pd.date_range("2026-01-01", periods=n, freq="1min"))
    return df


def _mock_claude_response(
    add_symbols: list[str] | None = None,
    remove_symbols: list[str] | None = None,
    price: float = 200.0,
    symbol: str = "NVDA",
) -> object:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_assessment": "bullish",
        "risk_flags": [],
        "position_reviews": [],
        "new_opportunities": [
            {
                "symbol": symbol,
                "action": "buy",
                "strategy": "momentum",
                "timeframe": "short",
                "conviction": 0.82,
                "suggested_entry": price,
                "suggested_exit": price * 1.07,
                "suggested_stop": price * 0.96,
                "position_size_pct": 0.06,
                "reasoning": "Breakout above key level.",
            }
        ],
        "watchlist_changes": {
            "add":    [{"symbol": s, "reason": "test add", "priority_tier": 1, "strategy": "momentum"}
                       for s in (add_symbols or [])],
            "remove": remove_symbols or [],
            "rationale": "test",
        },
    }
    content_block = MagicMock()
    content_block.text = json.dumps(payload)
    usage = MagicMock()
    usage.input_tokens = 800
    usage.output_tokens = 200
    resp = MagicMock()
    resp.content = [content_block]
    resp.usage = usage
    return resp


def _stub_order_result(order_id: str = "ord-001") -> OrderResult:
    return OrderResult(
        order_id=order_id,
        status="pending_new",
        submitted_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def orch(tmp_path):
    """
    Fully started Orchestrator with all external calls mocked.
    is_market_open is patched to True so the ranker doesn't reject all candidates.
    """
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
    broker.place_order     = AsyncMock(return_value=_stub_order_result())
    broker.cancel_order    = AsyncMock()
    o._broker = broker

    # Wire Claude mock into the reasoning engine
    o._claude._load_prompt = MagicMock(return_value="Context: {context_json} Respond in JSON.")
    o._claude._client = MagicMock()
    o._claude._client.messages.create = AsyncMock(return_value=_mock_claude_response())

    return o


# ---------------------------------------------------------------------------
# Helpers to seed state
# ---------------------------------------------------------------------------

async def _seed_watchlist(o: Orchestrator, symbols: list[str], tier: int = 1) -> None:
    now = datetime.now(timezone.utc).isoformat()
    entries = [
        WatchlistEntry(symbol=s, date_added=now, reason="test", priority_tier=tier)
        for s in symbols
    ]
    await o._state_manager.save_watchlist(WatchlistState(entries=entries))


async def _seed_portfolio(o: Orchestrator, positions: list[Position]) -> None:
    await o._state_manager.save_portfolio(
        PortfolioState(cash=50_000.0, buying_power=80_000.0, positions=positions)
    )


def _make_position(
    symbol: str,
    shares: float = 10.0,
    avg_cost: float = 200.0,
    profit_target: float = 220.0,
    stop_loss: float = 185.0,
) -> Position:
    return Position(
        symbol=symbol,
        shares=shares,
        avg_cost=avg_cost,
        entry_date="2026-03-01",
        intention=TradeIntention(
            strategy="momentum",
            exit_targets=ExitTargets(
                profit_target=profit_target,
                stop_loss=stop_loss,
            ),
        ),
    )


# ===========================================================================
# Test 1: Full cycle
# ===========================================================================

class TestFullCycle:
    """
    Run one cycle of fast → medium → slow and verify cross-loop integration.
    """

    @pytest.fixture(autouse=True)
    def market_open(self, orch):
        """Patch is_market_open to True and seed indicators so guards don't short-circuit."""
        orch._latest_indicators = {"NVDA": {"price": 875.0}}
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            yield

    @pytest.mark.asyncio
    async def test_full_cycle_places_order(self, orch, tmp_path):
        """
        After a Claude call (slow loop), an opportunity should rank through
        and an entry order placed by the medium loop.
        """
        # Watchlist: NVDA (tier1) — small enough to potentially trigger watchlist_small
        # but we want the time_ceiling trigger instead
        await _seed_watchlist(orch, ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN",
                                     "META", "GOOGL", "NFLX", "AMD", "PYPL",
                                     "NVDA"], tier=1)

        # Seed empty portfolio
        await _seed_portfolio(orch, [])

        # Configure data adapter to return synthetic bars for NVDA
        bars = _make_bars(60, base_price=875.0)
        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = AsyncMock(return_value=bars)

        # Patch is_market_open so ranker hard filter passes
        # Patch is_market_open in orchestrator (used by ranker) AND get_current_session
        # in risk_manager (used by validate_entry._check_market_hours).
        # Tests run outside NYSE hours so both real-clock calls would block entries.
        # Dead zone uses datetime.now() directly — override the parsed bounds so no
        # real clock time falls in the dead zone window.
        orch._risk_manager._dead_zone_start = dtime(0, 0)
        orch._risk_manager._dead_zone_end   = dtime(0, 1)
        from ozymandias.core.market_hours import Session
        with (
            patch("ozymandias.core.orchestrator.is_market_open", return_value=True),
            patch("ozymandias.execution.risk_manager.get_current_session",
                  return_value=Session.REGULAR_HOURS),
        ):
            # Fast loop — should run cleanly (no orders to reconcile)
            await orch._fast_loop_cycle()
            assert orch._broker.get_open_orders.called

            # Force time_ceiling trigger by backdating last call
            orch._trigger_state.last_claude_call_utc = (
                datetime.now(timezone.utc) - timedelta(hours=2)
            )

            # Slow loop — should call Claude
            await orch._slow_loop_cycle()

            assert orch._claude._client.messages.create.called
            # last_claude_call_utc should have been reset
            age = (datetime.now(timezone.utc) -
                   orch._trigger_state.last_claude_call_utc).total_seconds()
            assert age < 10

            # Medium loop — should see NVDA in watchlist and attempt entry
            await orch._medium_loop_cycle()

        # Verify an order was placed
        assert orch._broker.place_order.called
        call_args = orch._broker.place_order.call_args[0][0]
        assert call_args.symbol == "NVDA"
        assert call_args.side == "buy"

    @pytest.mark.asyncio
    async def test_fast_loop_reconciles_orders(self, orch):
        """Fast loop processes open orders from broker without error."""
        # Simulate one open broker order (OrderStatus has no symbol/side — those are on Fill)
        now = datetime.now(timezone.utc)
        orch._broker.get_open_orders = AsyncMock(return_value=[
            OrderStatus(
                order_id="b-001",
                status="new", filled_qty=0.0, remaining_qty=5.0,
                filled_avg_price=None, submitted_at=now,
                filled_at=None, canceled_at=None,
            )
        ])

        await orch._fast_loop_cycle()

        assert orch._broker.get_open_orders.call_count == 1
        assert orch._broker.get_positions.call_count == 1


# ===========================================================================
# Test 2: Override exit
# ===========================================================================

class TestOverrideExit:
    """
    Quant override fires when price crosses below VWAP with volume spike.
    """

    @pytest.mark.asyncio
    async def test_vwap_override_places_sell_order(self, orch):
        """VWAP crossover on an open position triggers a market exit order."""
        symbol = "AAPL"
        await _seed_portfolio(orch, [_make_position(symbol)])

        # Synthetic bars: price well below its VWAP equivalent
        bars = _make_bars(60, base_price=180.0)  # low price = below VWAP
        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = AsyncMock(return_value=bars)

        # Inject indicators with price below VWAP and elevated volume ratio
        orch._latest_indicators = {
            symbol: {
                "price": 178.0,
                "vwap": 200.0,
                "vwap_position": "below",
                "volume_ratio": 1.8,
                "rsi": 35.0,
                "rsi_divergence": False,
                "roc_5": -0.03,
                "atr_14": 3.5,
                "composite_technical_score": 0.25,
                "signals": {
                    "vwap_position": "below",
                    "volume_ratio": 1.8,
                    "rsi": 35.0,
                    "rsi_divergence": False,
                    "roc_5": -0.03,
                    "atr_14": 3.5,
                    "composite_technical_score": 0.25,
                },
            }
        }

        sell_result = OrderResult(
            order_id="sell-001",
            status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        )
        orch._broker.place_order = AsyncMock(return_value=sell_result)

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_quant_overrides()

        assert orch._broker.place_order.called
        order = orch._broker.place_order.call_args[0][0]
        assert order.symbol == symbol
        assert order.side == "sell"
        assert order.order_type == "market"


# ===========================================================================
# Test 3: PDT blocking
# ===========================================================================

class TestPDTBlocking:
    """
    PDTGuard enforces: effective_limit = 3 - buffer (default buffer=1, so limit=2).
    A round-trip (buy+sell same symbol same day) = 1 day trade.
    """

    # Use a fixed weekday so the 5-business-day window always includes it,
    # regardless of what day the test suite runs.
    _TRADE_DATE = "2026-03-13T14:30:00+00:00"   # Friday 2026-03-13 09:30 ET
    from datetime import date as _date
    _REFERENCE_DATE = _date(2026, 3, 13)

    def _filled_round_trip(self, symbol: str, order_id_prefix: str) -> list[OrderRecord]:
        """Return a FILLED buy + FILLED sell pair for one day trade."""
        ts = self._TRADE_DATE
        return [
            OrderRecord(
                order_id=f"{order_id_prefix}-buy",
                symbol=symbol, side="buy",
                quantity=10, order_type="market", limit_price=None,
                status="FILLED", filled_quantity=10.0,
                created_at=ts, filled_at=ts,
            ),
            OrderRecord(
                order_id=f"{order_id_prefix}-sell",
                symbol=symbol, side="sell",
                quantity=10, order_type="market", limit_price=None,
                status="FILLED", filled_quantity=10.0,
                created_at=ts, filled_at=ts,
            ),
        ]

    @pytest.mark.asyncio
    async def test_pdt_blocks_sell_when_at_limit(self, orch):
        """can_day_trade returns False when 2 round-trips already completed (buffer=1)."""
        portfolio = PortfolioState(
            cash=50_000.0, buying_power=80_000.0,
            positions=[_make_position("AAPL")],
        )
        orders = (
            self._filled_round_trip("AAPL", "dt1") +
            self._filled_round_trip("MSFT", "dt2")
        )
        # 2 day trades = at effective limit (3 - buffer=1 = 2)
        allowed, reason = orch._pdt_guard.can_day_trade(
            "NVDA", orders, portfolio, reference_date=self._REFERENCE_DATE
        )
        assert not allowed
        assert "limit" in reason.lower()

    @pytest.mark.asyncio
    async def test_pdt_allows_emergency_sell_past_normal_limit(self, orch):
        """Emergency exits use the full 3-trade limit, not the buffered limit."""
        portfolio = PortfolioState(
            cash=50_000.0, buying_power=80_000.0,
            positions=[_make_position("AAPL")],
        )
        orders = (
            self._filled_round_trip("AAPL", "dt1") +
            self._filled_round_trip("MSFT", "dt2")
        )

        # Normal mode: blocked at 2/2
        allowed_normal, _ = orch._pdt_guard.can_day_trade(
            "NVDA", orders, portfolio, reference_date=self._REFERENCE_DATE
        )
        assert not allowed_normal

        # Emergency mode: allowed at 2/3
        allowed_emergency, reason = orch._pdt_guard.can_day_trade(
            "NVDA", orders, portfolio, is_emergency=True,
            reference_date=self._REFERENCE_DATE,
        )
        assert allowed_emergency, f"Emergency should allow but got: {reason}"


# ===========================================================================
# Test 4: Degradation
# ===========================================================================

class TestDegradation:
    """
    When Claude fails, the system enters quant-only mode:
    - claude_available = False
    - claude_backoff_until_utc is set
    - Slow loop skips Claude calls until backoff expires
    """

    @pytest.fixture(autouse=True)
    def seed_indicators(self, orch):
        """Seed _latest_indicators so the slow loop's indicator guard doesn't short-circuit."""
        orch._latest_indicators = {"TEST": {"price": 100.0}}

    @pytest.mark.asyncio
    async def test_claude_failure_sets_degradation_flag(self, orch):
        """A Claude API exception activates degradation and backoff."""
        assert orch._degradation.claude_available is True

        orch._claude._client.messages.create = AsyncMock(
            side_effect=Exception("503 Service Unavailable")
        )
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._slow_loop_cycle()

        assert orch._degradation.claude_available is False
        assert orch._degradation.claude_backoff_until_utc is not None
        assert orch._degradation.claude_backoff_until_utc > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_slow_loop_skips_claude_during_backoff(self, orch):
        """While claude_backoff_until_utc is in the future, no API call is made."""
        orch._degradation.claude_available = False
        orch._degradation.claude_backoff_until_utc = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )
        orch._claude._client.messages.create.reset_mock()

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._slow_loop_cycle()

        assert orch._claude._client.messages.create.call_count == 0

    @pytest.mark.asyncio
    async def test_broker_failure_blocks_order_step(self, orch):
        """When broker is unavailable, the medium loop skips entry attempts."""
        orch._degradation.broker_available = False

        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch, [])

        bars = _make_bars(60, base_price=875.0)
        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = AsyncMock(return_value=bars)

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_loop_cycle()

        # Should not have called get_open_orders while broker is down
        assert orch._broker.get_open_orders.call_count == 0
        assert orch._broker.place_order.call_count == 0
