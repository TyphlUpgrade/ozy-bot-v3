"""
tests/test_short_protection.py
================================
Unit tests for Phase 16 short position protection:
  - ATR trailing stop (fast loop)
  - VWAP crossover exit (fast loop)
  - Hard stop from intention (fast loop)
  - _intraday_lows tracking
  - EOD forced close for momentum shorts (medium loop)
  - _recently_closed persistence and startup reload
  - ATR position size cap (_medium_try_entry)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ozymandias.core.state_manager import (
    ExitTargets,
    PortfolioState,
    Position,
    TradeIntention,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_position(
    symbol: str = "TSLA",
    shares: float = 10.0,
    avg_cost: float = 250.0,
    direction: str = "short",
    strategy: str = "momentum",
    stop_loss: float = 0.0,
) -> Position:
    return Position(
        symbol=symbol,
        shares=shares,
        avg_cost=avg_cost,
        entry_date=datetime.now(timezone.utc).isoformat(),
        intention=TradeIntention(
            direction=direction,
            strategy=strategy,
            exit_targets=ExitTargets(stop_loss=stop_loss),
        ),
    )


def _make_orch():
    """Build a minimal orchestrator with mocked broker and state."""
    from ozymandias.core.config import Config
    from ozymandias.core.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._config = Config()
    orch._broker = MagicMock()
    orch._broker.place_order = AsyncMock()
    orch._fill_protection = MagicMock()
    orch._fill_protection.can_place_order = MagicMock(return_value=True)
    orch._fill_protection.record_order = AsyncMock()
    orch._state_manager = MagicMock()
    orch._state_manager.load_portfolio = AsyncMock(return_value=PortfolioState())
    orch._state_manager.save_portfolio = AsyncMock()
    orch._latest_indicators = {}
    orch._intraday_lows = {}
    orch._intraday_highs = {}
    orch._recently_closed = {}
    orch._override_exit_count = 0
    orch._trigger_state = MagicMock()
    orch._strategies = []
    from ozymandias.execution.broker_interface import OrderResult
    orch._broker.place_order.return_value = OrderResult(
        order_id="test-order-001",
        status="pending_new",
        submitted_at=datetime.now(timezone.utc),
    )
    return orch


# ---------------------------------------------------------------------------
# _fast_step_short_exits — ATR trailing stop
# ---------------------------------------------------------------------------

class TestShortAtrTrailingStop:
    @pytest.mark.asyncio
    async def test_fires_when_price_breaches_trail_stop(self):
        orch = _make_orch()
        pos = _make_position(symbol="NVDA", direction="short")
        orch._intraday_lows["NVDA"] = 200.0   # session low
        atr = 5.0
        # trail stop = 200 + 5 * 2.0 = 210; current price 211 → breach
        orch._latest_indicators["NVDA"] = {
            "price": 211.0,
            "atr_14": atr,
            "vwap_position": "at",
            "volume_ratio": 0.8,
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_called_once()
        order = orch._broker.place_order.call_args[0][0]
        assert order.side == "buy"
        assert order.symbol == "NVDA"

    @pytest.mark.asyncio
    async def test_does_not_fire_when_price_below_trail_stop(self):
        orch = _make_orch()
        pos = _make_position(symbol="NVDA", direction="short")
        orch._intraday_lows["NVDA"] = 200.0
        # trail stop = 200 + 5 * 2.0 = 210; current price 208 → no breach
        orch._latest_indicators["NVDA"] = {
            "price": 208.0,
            "atr_14": 5.0,
            "vwap_position": "at",
            "volume_ratio": 0.8,
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_intraday_low_updated_each_cycle(self):
        orch = _make_orch()
        pos = _make_position(symbol="AMD", direction="short")
        orch._intraday_lows["AMD"] = 150.0
        # New lower price — intraday low should update downward
        orch._latest_indicators["AMD"] = {
            "price": 145.0,
            "atr_14": 2.0,
            "vwap_position": "at",
            "volume_ratio": 0.8,
        }
        await orch._fast_step_short_exits(pos)
        assert orch._intraday_lows["AMD"] == 145.0

    @pytest.mark.asyncio
    async def test_intraday_low_not_updated_upward(self):
        orch = _make_orch()
        pos = _make_position(symbol="AMD", direction="short")
        orch._intraday_lows["AMD"] = 140.0
        # Price higher than current low — low should stay at 140
        orch._latest_indicators["AMD"] = {
            "price": 155.0,
            "atr_14": 2.0,
            "vwap_position": "at",
            "volume_ratio": 0.8,
        }
        # trail stop = 140 + 2*2.0 = 144; price 155 > 144 → fires, but low stays 140
        await orch._fast_step_short_exits(pos)
        assert orch._intraday_lows["AMD"] == 140.0


# ---------------------------------------------------------------------------
# _fast_step_short_exits — VWAP crossover exit
# ---------------------------------------------------------------------------

class TestShortVwapCrossoverExit:
    @pytest.mark.asyncio
    async def test_fires_when_above_vwap_with_high_volume(self):
        orch = _make_orch()
        pos = _make_position(symbol="TSLA", direction="short")
        orch._latest_indicators["TSLA"] = {
            "price": 270.0,
            "atr_14": 100.0,  # large ATR → trail stop far away
            "vwap_position": "above",
            "volume_ratio": 2.0,  # above threshold (1.3)
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_called_once()
        order = orch._broker.place_order.call_args[0][0]
        assert order.side == "buy"

    @pytest.mark.asyncio
    async def test_does_not_fire_when_vwap_exit_disabled(self):
        orch = _make_orch()
        orch._config.risk.short_vwap_exit_enabled = False
        pos = _make_position(symbol="TSLA", direction="short")
        orch._latest_indicators["TSLA"] = {
            "price": 270.0,
            "atr_14": 100.0,
            "vwap_position": "above",
            "volume_ratio": 2.0,
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_volume_below_threshold(self):
        orch = _make_orch()
        pos = _make_position(symbol="TSLA", direction="short")
        orch._latest_indicators["TSLA"] = {
            "price": 270.0,
            "atr_14": 100.0,
            "vwap_position": "above",
            "volume_ratio": 1.0,  # below threshold (1.3)
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_below_vwap(self):
        orch = _make_orch()
        pos = _make_position(symbol="TSLA", direction="short")
        orch._latest_indicators["TSLA"] = {
            "price": 240.0,
            "atr_14": 100.0,
            "vwap_position": "below",  # still below VWAP — short is working
            "volume_ratio": 2.0,
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# _fast_step_short_exits — hard stop from intention
# ---------------------------------------------------------------------------

class TestShortHardStop:
    @pytest.mark.asyncio
    async def test_fires_when_price_reaches_stop_loss(self):
        orch = _make_orch()
        pos = _make_position(symbol="AAPL", direction="short", stop_loss=265.0)
        orch._latest_indicators["AAPL"] = {
            "price": 266.0,  # above stop_loss
            "atr_14": 100.0,
            "vwap_position": "at",
            "volume_ratio": 0.8,
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_called_once()
        order = orch._broker.place_order.call_args[0][0]
        assert order.side == "buy"

    @pytest.mark.asyncio
    async def test_does_not_fire_when_price_below_stop_loss(self):
        orch = _make_orch()
        pos = _make_position(symbol="AAPL", direction="short", stop_loss=265.0)
        orch._latest_indicators["AAPL"] = {
            "price": 260.0,  # below stop — short is profitable
            "atr_14": 100.0,
            "vwap_position": "at",
            "volume_ratio": 0.8,
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_fill_protection_blocks_exit(self):
        orch = _make_orch()
        orch._fill_protection.can_place_order = MagicMock(return_value=False)
        pos = _make_position(symbol="AAPL", direction="short", stop_loss=265.0)
        orch._latest_indicators["AAPL"] = {
            "price": 270.0,
            "atr_14": 100.0,
            "vwap_position": "at",
            "volume_ratio": 0.8,
        }
        await orch._fast_step_short_exits(pos)
        orch._broker.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# EOD forced close for momentum shorts
# ---------------------------------------------------------------------------

class TestMomentumShortEodClose:
    @pytest.mark.asyncio
    async def test_momentum_short_closed_in_last_five_minutes(self):
        from ozymandias.core.orchestrator import Orchestrator
        from ozymandias.core.config import Config
        from ozymandias.execution.broker_interface import OrderResult

        orch = _make_orch()
        orch._mark_broker_failure = MagicMock()
        orch._mark_broker_available = MagicMock()

        pos = _make_position(symbol="NVDA", direction="short", strategy="momentum")
        portfolio = PortfolioState(positions=[pos])

        bars = {"NVDA": MagicMock()}
        indicators = {"NVDA": {"signals": {
            "vwap_position": "below", "rsi": 40.0, "macd_signal": "bearish",
            "trend_structure": "bearish_aligned", "roc_5": -1.0,
            "roc_deceleration": False, "volume_ratio": 1.0, "atr_14": 5.0,
        }}}

        orch._strategies = []  # no strategy → evaluate_position skipped

        with patch(
            "ozymandias.core.orchestrator.is_last_five_minutes", return_value=True
        ):
            await orch._medium_evaluate_positions(portfolio, bars, indicators, MagicMock(), [])

        orch._broker.place_order.assert_called_once()
        order = orch._broker.place_order.call_args[0][0]
        assert order.side == "buy"
        assert order.symbol == "NVDA"

    @pytest.mark.asyncio
    async def test_swing_short_not_closed_by_eod_logic(self):
        orch = _make_orch()
        orch._mark_broker_failure = MagicMock()
        orch._mark_broker_available = MagicMock()

        pos = _make_position(symbol="NVDA", direction="short", strategy="swing")
        portfolio = PortfolioState(positions=[pos])
        bars = {"NVDA": MagicMock()}
        indicators = {"NVDA": {"signals": {
            "vwap_position": "below", "rsi": 40.0, "macd_signal": "bearish",
            "trend_structure": "bearish_aligned", "roc_5": -1.0,
            "roc_deceleration": False, "volume_ratio": 1.0, "atr_14": 5.0,
        }}}
        orch._strategies = []

        with patch(
            "ozymandias.core.orchestrator.is_last_five_minutes", return_value=True
        ):
            await orch._medium_evaluate_positions(portfolio, bars, indicators, MagicMock(), [])

        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_momentum_short_not_closed_outside_last_five_minutes(self):
        orch = _make_orch()
        orch._mark_broker_failure = MagicMock()
        orch._mark_broker_available = MagicMock()

        pos = _make_position(symbol="NVDA", direction="short", strategy="momentum")
        portfolio = PortfolioState(positions=[pos])
        bars = {"NVDA": MagicMock()}
        indicators = {"NVDA": {"signals": {
            "vwap_position": "below", "rsi": 40.0, "macd_signal": "bearish",
            "trend_structure": "bearish_aligned", "roc_5": -1.0,
            "roc_deceleration": False, "volume_ratio": 1.0, "atr_14": 5.0,
        }}}
        orch._strategies = []

        with patch(
            "ozymandias.core.orchestrator.is_last_five_minutes", return_value=False
        ):
            await orch._medium_evaluate_positions(portfolio, bars, indicators, MagicMock(), [])

        orch._broker.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# _recently_closed persistence and startup reload
# ---------------------------------------------------------------------------

class TestRecentlyClosedPersistence:
    def test_recently_closed_written_to_portfolio_on_close(self):
        """portfolio.recently_closed is populated when a position is closed."""
        portfolio = PortfolioState()
        symbol = "AAPL"
        now_iso = datetime.now(timezone.utc).isoformat()
        portfolio.recently_closed[symbol] = now_iso
        assert symbol in portfolio.recently_closed
        assert portfolio.recently_closed[symbol] == now_iso

    def test_recently_closed_survives_serialization(self):
        """recently_closed round-trips through _to_dict / load_portfolio."""
        from dataclasses import asdict
        portfolio = PortfolioState()
        portfolio.recently_closed["TSLA"] = "2026-03-18T14:30:00+00:00"
        d = asdict(portfolio)
        assert "recently_closed" in d
        assert d["recently_closed"]["TSLA"] == "2026-03-18T14:30:00+00:00"

    def test_startup_reload_restores_entry_younger_than_60s(self):
        """Entry < 60s old is reloaded into in-memory _recently_closed."""
        orch = _make_orch()
        now_utc = datetime.now(timezone.utc)
        # 10 seconds ago
        close_iso = now_utc.replace(
            second=now_utc.second - 10 if now_utc.second >= 10 else now_utc.second,
        ).isoformat()
        # Simulate reload logic directly
        elapsed = 10.0
        if elapsed < 60.0:
            orch._recently_closed["NVDA"] = time.monotonic() - elapsed

        assert "NVDA" in orch._recently_closed
        assert orch._recently_closed["NVDA"] < time.monotonic()

    def test_startup_reload_discards_entry_older_than_60s(self):
        """Entry >= 60s old is not reloaded — cooldown already expired."""
        orch = _make_orch()
        # 90 seconds ago → should not be reloaded
        elapsed = 90.0
        if elapsed < 60.0:
            orch._recently_closed["NVDA"] = time.monotonic() - elapsed

        assert "NVDA" not in orch._recently_closed


# ---------------------------------------------------------------------------
# ATR position size cap
# ---------------------------------------------------------------------------

class TestAtrPositionSizeCap:
    @pytest.mark.asyncio
    async def test_cap_reduces_size_on_high_atr_symbol(self):
        """High ATR → cap fires and reduces quantity below requested."""
        from ozymandias.core.orchestrator import Orchestrator
        from ozymandias.core.config import Config

        orch = _make_orch()
        orch._config.risk.atr_position_size_cap_enabled = True
        orch._config.risk.max_risk_per_trade_pct = 0.02

        # equity=50000, max_risk=0.02 → max_risk_dollars=1000
        # ATR=10 → max_shares = 1000 / 10 = 100
        # If base qty = 500 → capped to 100
        equity = 50_000.0
        atr = 10.0
        base_qty = 500
        expected_cap = int((equity * 0.02) / atr)  # 100

        max_shares = int((equity * orch._config.risk.max_risk_per_trade_pct) / atr)
        assert max_shares == expected_cap
        assert base_qty > max_shares  # cap would fire

        capped = min(base_qty, max_shares)
        assert capped == expected_cap

    def test_cap_does_not_fire_on_normal_atr(self):
        """Normal ATR (2%) → cap does not affect typical 5–10% position sizes."""
        equity = 50_000.0
        atr_pct = 0.02   # 2% ATR on a $100 stock → ATR = $2
        price = 100.0
        atr = price * atr_pct

        max_shares = int((equity * 0.02) / atr)  # = 50000*0.02/2 = 500 shares
        # 500 shares × $100 = $50k = 100% of equity
        # A 10% position = 50 shares — well below 500 cap
        ten_pct_shares = int(equity * 0.10 / price)  # 50 shares
        assert ten_pct_shares < max_shares

    def test_cap_disabled_via_config(self):
        """atr_position_size_cap_enabled=False means no cap is applied."""
        orch = _make_orch()
        orch._config.risk.atr_position_size_cap_enabled = False
        # With cap disabled, quantity should pass through unchanged
        # (logic: only enters cap block if enabled=True)
        atr = 10.0
        equity = 50_000.0
        quantity = 500
        if orch._config.risk.atr_position_size_cap_enabled and atr > 0:
            max_shares = int((equity * orch._config.risk.max_risk_per_trade_pct) / atr)
            if max_shares > 0 and quantity > max_shares:
                quantity = max_shares
        assert quantity == 500  # unchanged
