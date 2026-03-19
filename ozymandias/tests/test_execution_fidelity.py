"""
tests/test_execution_fidelity.py
==================================
Tests for Phase 11: Execution Fidelity
  1. Current market price substitution in _medium_try_entry
  2. Entry price staleness / drift check
  3. Minimum technical score hard filter in apply_hard_filters
  4. TA signal strength as position size modifier
"""
from __future__ import annotations

import asyncio
import dataclasses
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: F401 (patch used in fallback test)

import pytest
import pytest_asyncio

from ozymandias.core.orchestrator import Orchestrator
from ozymandias.core.state_manager import OrdersState, PortfolioState
from ozymandias.execution.broker_interface import AccountInfo, MarketHours, OrderResult
from ozymandias.intelligence.opportunity_ranker import OpportunityRanker, ScoredOpportunity


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _stub_account() -> AccountInfo:
    return AccountInfo(
        equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
        currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
    )


def _stub_hours() -> MarketHours:
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    return MarketHours(
        is_open=True,
        next_open=now + timedelta(hours=1),
        next_close=now + timedelta(hours=8),
        session="regular",
    )


def _make_opportunity(
    symbol: str = "NVDA",
    action: str = "buy",
    suggested_entry: float = 200.0,
    suggested_exit: float = 220.0,
    suggested_stop: float = 190.0,
    position_size_pct: float = 0.05,
) -> ScoredOpportunity:
    return ScoredOpportunity(
        symbol=symbol,
        action=action,
        strategy="momentum",
        composite_score=0.75,
        ai_conviction=0.70,   # below market_order_conviction_threshold (0.80) — tests limit order path
        technical_score=0.70,
        risk_adjusted_return=0.60,
        liquidity_score=0.80,
        suggested_entry=suggested_entry,
        suggested_exit=suggested_exit,
        suggested_stop=suggested_stop,
        position_size_pct=position_size_pct,
        reasoning="Test setup.",
    )


@pytest_asyncio.fixture
async def orch(tmp_path):
    """Orchestrator with all external calls mocked."""
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
        o._trade_journal._path = tmp_path / "trade_journal.jsonl"
        o._reasoning_cache._dir = tmp_path / "cache"
        o._reasoning_cache._dir.mkdir()
        await o._startup()

    broker = MagicMock()
    broker.get_account = AsyncMock(return_value=_stub_account())
    broker.get_open_orders = AsyncMock(return_value=[])
    broker.get_positions = AsyncMock(return_value=[])
    broker.place_order = AsyncMock(return_value=OrderResult(
        order_id="test-001", status="pending_new",
        submitted_at=datetime.now(timezone.utc),
    ))
    broker.cancel_order = AsyncMock()
    o._broker = broker
    return o


def _stub_entry_guards(orch, *, indicators: dict | None = None):
    """Allow entry to proceed: bypass risk, fill protection, and thesis challenge."""
    orch._risk_manager.validate_entry = MagicMock(return_value=(True, ""))
    orch._fill_protection.can_place_order = MagicMock(return_value=True)
    orch._fill_protection.record_order = AsyncMock()
    orch._latest_market_context = {}
    orch._latest_indicators = indicators if indicators is not None else {}
    # Disable thesis challenge for all tests in this file
    orch._claude.run_thesis_challenge = AsyncMock(
        return_value={"proceed": True, "conviction": 0.80, "challenge_reasoning": "ok"}
    )


# ===========================================================================
# 1. Current price substitution
# ===========================================================================

class TestCurrentPriceSubstitution:
    @pytest.mark.asyncio
    async def test_uses_current_price_when_available(self, orch):
        """When _latest_indicators has a price, the limit order uses it, not suggested_entry."""
        top = _make_opportunity(suggested_entry=200.0)
        # 201.0 is 0.5% above suggested_entry — within drift tolerance (1.5%), distinct from 200.0
        _stub_entry_guards(orch, indicators={"NVDA": {"price": 201.0, "composite_technical_score": 1.0}})
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        placed_orders = []
        async def capture(order):
            placed_orders.append(order)
            return OrderResult(order_id="o1", status="pending_new",
                               submitted_at=datetime.now(timezone.utc))
        orch._broker.place_order = capture

        await orch._medium_try_entry(top, acct, portfolio, [])

        assert len(placed_orders) == 1
        assert placed_orders[0].limit_price == 201.0, (
            "limit_price should be current price 201.0, not suggested_entry 200.0"
        )

    @pytest.mark.asyncio
    async def test_fallback_to_suggested_entry_when_price_missing(self, orch):
        """When indicators lack a price, falls back to suggested_entry and logs WARNING."""
        top = _make_opportunity(suggested_entry=200.0)
        # No "price" key in indicators
        _stub_entry_guards(orch, indicators={"NVDA": {"composite_technical_score": 1.0}})
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        placed_orders = []
        async def capture(order):
            placed_orders.append(order)
            return OrderResult(order_id="o2", status="pending_new",
                               submitted_at=datetime.now(timezone.utc))
        orch._broker.place_order = capture

        import logging
        with patch.object(orch._risk_manager, "validate_entry", return_value=(True, "")):
            await orch._medium_try_entry(top, acct, portfolio, [])

        assert len(placed_orders) == 1
        assert placed_orders[0].limit_price == 200.0, (
            "limit_price should fall back to suggested_entry 200.0"
        )


# ===========================================================================
# 2. Entry drift check
# ===========================================================================

class TestEntryDriftCheck:

    def _make_top(self, action="buy", suggested_entry=200.0):
        return _make_opportunity(action=action, suggested_entry=suggested_entry,
                                 suggested_exit=220.0, suggested_stop=190.0)

    @pytest.mark.asyncio
    async def test_long_chase_blocks_entry(self, orch):
        """Buy blocked when current price is > suggested_entry × (1 + max_entry_drift_pct)."""
        top = self._make_top(action="buy", suggested_entry=200.0)
        # 2.5% above suggested_entry; default max_entry_drift_pct=0.015
        current = 200.0 * 1.025
        _stub_entry_guards(orch, indicators={"NVDA": {"price": current, "composite_technical_score": 1.0}})
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        result = await orch._medium_try_entry(top, acct, portfolio, [])

        assert result is False
        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_adverse_drift_blocks_entry(self, orch):
        """Buy blocked when current price < suggested_entry × (1 - max_adverse_drift_pct)."""
        top = self._make_top(action="buy", suggested_entry=200.0)
        # 3% below suggested_entry; default max_adverse_drift_pct=0.020
        current = 200.0 * 0.97
        _stub_entry_guards(orch, indicators={"NVDA": {"price": current, "composite_technical_score": 1.0}})
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        result = await orch._medium_try_entry(top, acct, portfolio, [])

        assert result is False
        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_chase_blocks_entry(self, orch):
        """Short sell blocked when current price < suggested_entry × (1 - max_entry_drift_pct).
        Symmetrically: a short 'chase' means price already fell past the target.
        """
        top = _make_opportunity(
            symbol="NVDA", action="sell_short",
            suggested_entry=200.0, suggested_exit=180.0, suggested_stop=210.0,
        )
        # 2.5% below suggested_entry — short already chased
        current = 200.0 * 0.975
        _stub_entry_guards(orch, indicators={"NVDA": {"price": current, "composite_technical_score": 1.0}})
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        result = await orch._medium_try_entry(top, acct, portfolio, [])

        assert result is False
        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_within_tolerance_proceeds(self, orch):
        """Buy allowed when drift is within both bounds."""
        top = self._make_top(action="buy", suggested_entry=200.0)
        # 0.5% above — within max_entry_drift_pct=1.5%
        current = 200.0 * 1.005
        _stub_entry_guards(orch, indicators={"NVDA": {"price": current, "composite_technical_score": 1.0}})
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        placed_orders = []
        async def capture(order):
            placed_orders.append(order)
            return OrderResult(order_id="o3", status="pending_new",
                               submitted_at=datetime.now(timezone.utc))
        orch._broker.place_order = capture
        orch._fill_protection.record_order = AsyncMock()

        result = await orch._medium_try_entry(top, acct, portfolio, [])

        assert result is True
        assert len(placed_orders) == 1


# ===========================================================================
# 3. Minimum technical score hard filter
# ===========================================================================

class TestMinTechnicalScoreFilter:

    def _ranker(self, min_technical_score=0.30) -> OpportunityRanker:
        return OpportunityRanker({
            "w_ai": 0.35, "w_tech": 0.30, "w_risk": 0.20, "w_liq": 0.15,
            "min_technical_score": min_technical_score,
        })

    def _opportunity(self, conviction=0.8) -> dict:
        return {
            "symbol": "AAPL",
            "action": "buy",
            "strategy": "momentum",
            "conviction": conviction,
            "suggested_entry": 150.0,
            "suggested_exit": 165.0,
            "suggested_stop": 145.0,
            "position_size_pct": 0.05,
        }

    def _account(self) -> AccountInfo:
        return AccountInfo(
            equity=100_000.0, buying_power=50_000.0, cash=50_000.0,
            currency="USD", pdt_flag=False, daytrade_count=0, account_id="TEST",
        )

    def _portfolio(self) -> PortfolioState:
        return PortfolioState(positions=[])

    def _pdt_guard(self):
        pdt = MagicMock()
        pdt.can_day_trade = MagicMock(return_value=(True, ""))
        return pdt

    def _signals(self, score: float) -> dict:
        return {"AAPL": {"composite_technical_score": score, "signals": {}}}

    def test_score_below_floor_rejects(self):
        """composite_technical_score=0.25 < floor=0.30 → hard filter rejects."""
        ranker = self._ranker(min_technical_score=0.30)
        opp = self._opportunity()

        passes, reason = ranker.apply_hard_filters(
            opp,
            self._account(),
            self._portfolio(),
            self._pdt_guard(),
            market_hours_fn=lambda: True,
            technical_signals=self._signals(0.25),
        )

        assert passes is False
        assert "composite_technical_score" in reason
        assert "0.25" in reason

    def test_score_at_floor_passes(self):
        """composite_technical_score=0.30 >= floor=0.30 → not rejected by this filter."""
        ranker = self._ranker(min_technical_score=0.30)
        opp = self._opportunity()

        passes, reason = ranker.apply_hard_filters(
            opp,
            self._account(),
            self._portfolio(),
            self._pdt_guard(),
            market_hours_fn=lambda: True,
            technical_signals=self._signals(0.30),
        )

        assert passes is True

    def test_score_above_floor_passes(self):
        """composite_technical_score=0.55 >= floor=0.30 → passes."""
        ranker = self._ranker(min_technical_score=0.30)
        opp = self._opportunity()

        passes, _ = ranker.apply_hard_filters(
            opp,
            self._account(),
            self._portfolio(),
            self._pdt_guard(),
            market_hours_fn=lambda: True,
            technical_signals=self._signals(0.55),
        )

        assert passes is True

    def test_no_technical_signals_skips_check(self):
        """technical_signals=None → TA floor check skipped entirely."""
        ranker = self._ranker(min_technical_score=0.30)
        opp = self._opportunity()

        passes, _ = ranker.apply_hard_filters(
            opp,
            self._account(),
            self._portfolio(),
            self._pdt_guard(),
            market_hours_fn=lambda: True,
            technical_signals=None,
        )

        assert passes is True


# ===========================================================================
# 4. TA size modifier
# ===========================================================================

class TestTASizeModifier:
    """Tests for the TA signal strength → position size scaling in _medium_try_entry.

    Sizing formula: target = int(equity × position_size_pct / entry_price)
    then scaled by: size_factor = ta_size_factor_min + (1 − ta_size_factor_min) × tech_score

    With equity=100_000, position_size_pct=0.05, entry_price=200: target = 25 shares.
    """

    def _make_top(self, suggested_entry=200.0, position_size_pct=0.05):
        return _make_opportunity(suggested_entry=suggested_entry, position_size_pct=position_size_pct)

    async def _run_entry(self, orch, top, tech_score: float) -> list:
        """Helper: stub entry guards with given tech_score, return placed orders."""
        _stub_entry_guards(orch, indicators={"NVDA": {
            "price": top.suggested_entry,  # no drift
            "composite_technical_score": tech_score,
        }})
        # Disable thesis challenge
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"proceed": True, "conviction": 0.80, "challenge_reasoning": "ok"}
        )

        placed_orders = []
        async def capture(order):
            placed_orders.append(order)
            return OrderResult(order_id="sz-001", status="pending_new",
                               submitted_at=datetime.now(timezone.utc))
        orch._broker.place_order = capture

        acct = _stub_account()
        portfolio = PortfolioState(positions=[])
        await orch._medium_try_entry(top, acct, portfolio, [])
        return placed_orders

    @pytest.mark.asyncio
    async def test_size_modifier_at_min_score(self, orch):
        """tech_score=0.0, ta_size_factor_min=0.60 → quantity = 60% of target."""
        top = self._make_top()
        orders = await self._run_entry(orch, top, tech_score=0.0)

        assert len(orders) == 1
        # target = int(100_000 × 0.05 / 200) = 25; size_factor=0.60 → int(25×0.60)=15
        acct = _stub_account()
        ta_min = orch._config.ranker.ta_size_factor_min
        target = int(acct.equity * top.position_size_pct / top.suggested_entry)
        assert orders[0].quantity == max(1, int(target * ta_min))

    @pytest.mark.asyncio
    async def test_size_modifier_at_max_score(self, orch):
        """tech_score=1.0 → quantity = 100% of target (no reduction)."""
        top = self._make_top()
        orders = await self._run_entry(orch, top, tech_score=1.0)

        assert len(orders) == 1
        acct = _stub_account()
        target = int(acct.equity * top.position_size_pct / top.suggested_entry)
        assert orders[0].quantity == target

    @pytest.mark.asyncio
    async def test_size_modifier_at_midpoint(self, orch):
        """tech_score=0.5, ta_size_factor_min=0.60 → quantity = 80% of target."""
        top = self._make_top()
        orders = await self._run_entry(orch, top, tech_score=0.5)

        assert len(orders) == 1
        # target = 25; size_factor = 0.60 + 0.40*0.5 = 0.80 → int(25×0.80) = 20
        acct = _stub_account()
        ta_min = orch._config.ranker.ta_size_factor_min
        target = int(acct.equity * top.position_size_pct / top.suggested_entry)
        size_factor = ta_min + (1.0 - ta_min) * 0.5
        assert orders[0].quantity == max(1, int(target * size_factor))

    @pytest.mark.asyncio
    async def test_size_modifier_floors_at_one(self, orch):
        """TA scaling never reduces a 1-share target below 1 share.

        position_size_pct=0.002 → target = int(100_000 × 0.002 / 200) = 1 share.
        At tech_score=0.0, size_factor=0.60 → int(1×0.60)=0, but max(1,...) floors to 1.
        """
        top = self._make_top(position_size_pct=0.002)
        orders = await self._run_entry(orch, top, tech_score=0.0)

        assert len(orders) == 1
        assert orders[0].quantity == 1


# ===========================================================================
# 5. Market order for high-conviction momentum entries
# ===========================================================================

class TestMarketOrderPath:
    """High-conviction momentum entries use market orders; others use limit orders."""

    @pytest.mark.asyncio
    async def test_high_conviction_momentum_uses_market_order(self, orch):
        """conviction >= threshold AND strategy=momentum → market order, no limit_price."""
        top = _make_opportunity(suggested_entry=200.0)
        top = dataclasses.replace(top, ai_conviction=0.85)
        _stub_entry_guards(orch, indicators={"NVDA": {"price": 200.5, "composite_technical_score": 1.0}})
        placed_orders = []
        async def capture(order):
            placed_orders.append(order)
            return OrderResult(order_id="o1", status="pending_new",
                               submitted_at=datetime.now(timezone.utc))
        orch._broker.place_order = capture
        await orch._medium_try_entry(top, _stub_account(), PortfolioState(positions=[]), [])
        assert len(placed_orders) == 1
        assert placed_orders[0].order_type == "market"
        assert placed_orders[0].limit_price is None

    @pytest.mark.asyncio
    async def test_below_threshold_uses_limit_order(self, orch):
        """conviction < threshold → limit order with limit_price set."""
        top = _make_opportunity(suggested_entry=200.0)  # ai_conviction=0.70 by default
        _stub_entry_guards(orch, indicators={"NVDA": {"price": 200.5, "composite_technical_score": 1.0}})
        placed_orders = []
        async def capture(order):
            placed_orders.append(order)
            return OrderResult(order_id="o1", status="pending_new",
                               submitted_at=datetime.now(timezone.utc))
        orch._broker.place_order = capture
        await orch._medium_try_entry(top, _stub_account(), PortfolioState(positions=[]), [])
        assert len(placed_orders) == 1
        assert placed_orders[0].order_type == "limit"
        assert placed_orders[0].limit_price is not None

    @pytest.mark.asyncio
    async def test_high_conviction_swing_uses_limit_order(self, orch):
        """conviction >= threshold but strategy=swing → limit order (market only for momentum)."""
        top = _make_opportunity(suggested_entry=200.0)
        top = dataclasses.replace(top, ai_conviction=0.85, strategy="swing")
        _stub_entry_guards(orch, indicators={"NVDA": {"price": 200.5, "composite_technical_score": 1.0}})
        placed_orders = []
        async def capture(order):
            placed_orders.append(order)
            return OrderResult(order_id="o1", status="pending_new",
                               submitted_at=datetime.now(timezone.utc))
        orch._broker.place_order = capture
        await orch._medium_try_entry(top, _stub_account(), PortfolioState(positions=[]), [])
        assert len(placed_orders) == 1
        assert placed_orders[0].order_type == "limit"
