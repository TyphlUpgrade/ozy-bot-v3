"""
tests/test_opportunity_ranker.py
=================================
Unit tests for intelligence/opportunity_ranker.py.

All external dependencies (PDT guard, market hours, broker) are mocked so
these tests never touch the network.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field

from ozymandias.intelligence.opportunity_ranker import (
    OpportunityRanker,
    ScoredOpportunity,
    ExitAction,
    _W_AI,
    _W_TECH,
    _W_RISK,
    _W_LIQ,
    _MAX_REWARD_RISK_RATIO,
)
from ozymandias.intelligence.claude_reasoning import ReasoningResult
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.core.state_manager import PortfolioState, Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ranker(**cfg) -> OpportunityRanker:
    return OpportunityRanker(cfg or None)


def _account(
    equity: float = 100_000.0,
    buying_power: float = 50_000.0,
) -> AccountInfo:
    return AccountInfo(
        equity=equity,
        buying_power=buying_power,
        cash=buying_power,
        currency="USD",
        pdt_flag=False,
        daytrade_count=0,
        account_id="TEST",
    )


def _portfolio(n_positions: int = 0) -> PortfolioState:
    positions = [
        Position(symbol=f"SYM{i}", shares=10, avg_cost=100.0, entry_date="2026-03-13")
        for i in range(n_positions)
    ]
    return PortfolioState(positions=positions, buying_power=50_000.0)


def _opportunity(**kw) -> dict:
    base = {
        "symbol": "AAPL",
        "action": "buy",
        "strategy": "momentum",
        "timeframe": "short",
        "conviction": 0.8,
        "suggested_entry": 150.0,
        "suggested_exit": 165.0,
        "suggested_stop": 145.0,
        "position_size_pct": 0.05,
        "reasoning": "Strong momentum setup.",
    }
    base.update(kw)
    return base


def _reasoning_result(
    opportunities: list[dict] | None = None,
    reviews: list[dict] | None = None,
) -> ReasoningResult:
    return ReasoningResult(
        timestamp="2026-03-13T10:00:00Z",
        position_reviews=reviews or [],
        new_opportunities=opportunities or [],
        watchlist_changes={"add": [], "remove": [], "rationale": ""},
        market_assessment="neutral",
        risk_flags=[],
        rejected_opportunities=[],
        raw={},
    )


def _pdt_guard(allow: bool = True) -> MagicMock:
    guard = MagicMock()
    guard.can_day_trade.return_value = (allow, "ok" if allow else "PDT limit reached")
    return guard


def _market_open(open_: bool = True):
    """Return a callable that simulates is_market_open()."""
    return lambda: open_


def _tech(symbol: str, composite: float = 0.7, avg_vol: float | None = 1_000_000.0) -> dict:
    """
    Build a mock technical_signals entry matching the real generate_signal_summary() schema:
        {symbol: {"composite_technical_score": ..., "signals": {"avg_daily_volume": ...}}}
    """
    nested: dict = {}
    if avg_vol is not None:
        nested["avg_daily_volume"] = avg_vol
    return {symbol: {"composite_technical_score": composite, "signals": nested}}


# ---------------------------------------------------------------------------
# 1. Risk-adjusted return calculation
# ---------------------------------------------------------------------------

class TestRiskAdjustedReturn:

    def test_normal_setup(self):
        r = _ranker()
        # (165 - 150) / (150 - 145) = 15/5 = 3.0 → 3/5 = 0.60
        assert r._risk_adjusted_return(150, 165, 145) == pytest.approx(0.60)

    def test_capped_at_five_to_one(self):
        r = _ranker()
        # ratio = (200 - 100) / (100 - 80) = 100/20 = 5.0 → normalised = 1.0
        assert r._risk_adjusted_return(100, 200, 80) == pytest.approx(1.0)

    def test_exceeds_five_to_one_capped(self):
        r = _ranker()
        # ratio = 300/20 = 15.0 → capped at 1.0
        assert r._risk_adjusted_return(100, 400, 80) == pytest.approx(1.0)

    def test_stop_equals_entry_returns_zero(self):
        r = _ranker()
        assert r._risk_adjusted_return(100, 110, 100) == 0.0

    def test_stop_above_entry_returns_zero(self):
        r = _ranker()
        assert r._risk_adjusted_return(100, 110, 105) == 0.0

    def test_negative_expected_gain_returns_zero(self):
        r = _ranker()
        # exit below entry even though stop is below entry
        assert r._risk_adjusted_return(100, 90, 80) == 0.0

    def test_zero_entry_returns_zero(self):
        r = _ranker()
        assert r._risk_adjusted_return(0, 10, -5) == 0.0


# ---------------------------------------------------------------------------
# 2. Liquidity score
# ---------------------------------------------------------------------------

class TestLiquidityScore:

    def test_below_1m(self):
        r = _ranker()
        assert r._liquidity_score(100_000) == pytest.approx(0.1)

    def test_500k(self):
        r = _ranker()
        assert r._liquidity_score(500_000) == pytest.approx(0.5)

    def test_at_1m_gives_perfect_score(self):
        r = _ranker()
        assert r._liquidity_score(1_000_000) == pytest.approx(1.0)

    def test_above_1m_capped(self):
        r = _ranker()
        assert r._liquidity_score(5_000_000) == pytest.approx(1.0)

    def test_none_gives_neutral(self):
        r = _ranker()
        assert r._liquidity_score(None) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 3. Composite score calculation
# ---------------------------------------------------------------------------

class TestCompositeScore:

    def test_known_inputs(self):
        r = _ranker()
        opp = _opportunity(
            conviction=0.8,
            suggested_entry=150.0,
            suggested_exit=165.0,
            suggested_stop=145.0,
        )
        # rar = (165-150)/(150-145) = 3.0 → normalised = 0.60
        # liq = 1.0 (1M vol) — use real schema: composite_technical_score at top, avg_daily_volume nested
        signals = {"AAPL": {"composite_technical_score": 0.7, "signals": {"avg_daily_volume": 1_000_000}}}
        result = r.score_opportunity(opp, signals, _account(), _portfolio())

        expected = (
            0.8 * _W_AI
            + 0.7 * _W_TECH
            + 0.60 * _W_RISK
            + 1.0 * _W_LIQ
        )
        assert result.composite_score == pytest.approx(expected, abs=1e-6)
        assert result.risk_adjusted_return == pytest.approx(0.60, abs=1e-6)
        assert result.liquidity_score == pytest.approx(1.0)

    def test_missing_tech_signals_defaults_to_zero(self):
        r = _ranker()
        opp = _opportunity(conviction=1.0)
        result = r.score_opportunity(opp, {}, _account(), _portfolio())
        # technical_score = 0, liq = 0.5 (no volume), rar depends on prices
        assert result.technical_score == 0.0
        assert result.liquidity_score == pytest.approx(0.5)

    def test_composite_clamped_to_one(self):
        r = _ranker()
        opp = _opportunity(
            conviction=1.0,
            suggested_entry=100.0,
            suggested_exit=200.0,
            suggested_stop=80.0,
        )
        signals = {"AAPL": {"composite_technical_score": 1.0, "signals": {"avg_daily_volume": 5_000_000}}}
        result = r.score_opportunity(opp, signals, _account(), _portfolio())
        assert result.composite_score <= 1.0

    def test_weight_override_via_config(self):
        r = OpportunityRanker({"w_ai": 1.0, "w_tech": 0.0, "w_risk": 0.0, "w_liq": 0.0})
        opp = _opportunity(conviction=0.6)
        signals = {"AAPL": {"composite_technical_score": 0.9, "signals": {"avg_daily_volume": 2_000_000}}}
        result = r.score_opportunity(opp, signals, _account(), _portfolio())
        # Only AI conviction matters
        assert result.composite_score == pytest.approx(0.6, abs=1e-6)


# ---------------------------------------------------------------------------
# 4. Hard filters
# ---------------------------------------------------------------------------

class TestHardFilters:

    def _filter(self, opp=None, account=None, portfolio=None, pdt=None,
                mkt_open=True, signals=None):
        r = _ranker()
        opp = opp or _opportunity()
        return r.apply_hard_filters(
            opp,
            account or _account(),
            portfolio or _portfolio(),
            pdt or _pdt_guard(True),
            _market_open(mkt_open),
            orders=[],
            technical_signals=signals or _tech("AAPL"),
        )

    def test_all_pass(self):
        passes, reason = self._filter()
        assert passes is True
        assert reason == ""

    def test_market_closed_rejects(self):
        passes, reason = self._filter(mkt_open=False)
        assert passes is False
        assert "regular hours" in reason

    def test_insufficient_buying_power_rejects(self):
        # position_size_pct=0.05, equity=100k → need $5000; buying_power=$1000
        account = _account(equity=100_000.0, buying_power=1_000.0)
        passes, reason = self._filter(account=account)
        assert passes is False
        assert "buying power" in reason

    def test_max_positions_reached_rejects(self):
        portfolio = _portfolio(n_positions=8)
        passes, reason = self._filter(portfolio=portfolio)
        assert passes is False
        assert "max concurrent positions" in reason

    def test_below_max_positions_passes(self):
        portfolio = _portfolio(n_positions=7)
        passes, _ = self._filter(portfolio=portfolio)
        assert passes is True

    def test_pdt_violation_rejects(self):
        passes, reason = self._filter(pdt=_pdt_guard(allow=False))
        assert passes is False
        assert "PDT" in reason

    def test_low_volume_in_signals_rejects(self):
        # avg_daily_volume lives in the nested "signals" sub-dict of the TA output
        signals = {"AAPL": {"composite_technical_score": 0.7, "signals": {"avg_daily_volume": 50_000}}}
        passes, reason = self._filter(signals=signals)
        assert passes is False
        assert "volume" in reason.lower()

    def test_low_volume_in_opportunity_rejects(self):
        opp = _opportunity(avg_daily_volume=30_000)
        # No volume in signals → falls back to opportunity dict
        signals = {"AAPL": {"composite_technical_score": 0.7, "signals": {}}}
        passes, reason = self._filter(opp=opp, signals=signals)
        assert passes is False
        assert "volume" in reason.lower()

    def test_volume_at_100k_passes(self):
        signals = {"AAPL": {"composite_technical_score": 0.7, "signals": {"avg_daily_volume": 100_000}}}
        passes, _ = self._filter(signals=signals)
        assert passes is True

    def test_no_volume_info_passes(self):
        # Unknown volume: filter skipped (neutral)
        signals = {"AAPL": {"composite_technical_score": 0.7, "signals": {}}}
        passes, _ = self._filter(signals=signals)
        assert passes is True


# ---------------------------------------------------------------------------
# 5. Full ranking pipeline
# ---------------------------------------------------------------------------

class TestRankOpportunities:

    def _rank(self, opps, signals=None, n_positions=0, pdt=None, mkt_open=True,
              account=None):
        r = _ranker()
        rr = _reasoning_result(opportunities=opps)
        sigs = signals or {}
        return r.rank_opportunities(
            rr,
            sigs,
            account or _account(),
            _portfolio(n_positions),
            pdt or _pdt_guard(True),
            _market_open(mkt_open),
            orders=[],
        )

    def test_empty_opportunities_returns_empty(self):
        assert self._rank([]) == []

    def test_five_opportunities_sorted_by_score(self):
        # Five opportunities with different convictions, same setup otherwise.
        # Conviction dominates (W_ai=0.35). Highest conviction → highest score.
        opps = [
            _opportunity(symbol=f"S{i}", conviction=i * 0.2,
                         suggested_entry=100.0, suggested_exit=120.0, suggested_stop=90.0)
            for i in range(1, 6)  # convictions: 0.2, 0.4, 0.6, 0.8, 1.0
        ]
        signals = {f"S{i}": {"composite_technical_score": 0.5, "signals": {"avg_daily_volume": 500_000}}
                   for i in range(1, 6)}
        ranked = self._rank(opps, signals)
        assert len(ranked) == 5
        scores = [s.composite_score for s in ranked]
        assert scores == sorted(scores, reverse=True), "Not sorted descending"
        assert ranked[0].symbol == "S5"  # highest conviction

    def test_failed_filter_excluded(self):
        opps = [
            _opportunity(symbol="AAPL"),
            _opportunity(symbol="TSLA"),
        ]
        # AAPL has low volume → rejected; TSLA is fine
        # Use real TA output schema: composite_technical_score at top, avg_daily_volume nested
        signals = {
            "AAPL": {"composite_technical_score": 0.8, "signals": {"avg_daily_volume": 10_000}},
            "TSLA": {"composite_technical_score": 0.7, "signals": {"avg_daily_volume": 2_000_000}},
        }
        ranked = self._rank(opps, signals)
        assert len(ranked) == 1
        assert ranked[0].symbol == "TSLA"

    def test_all_rejected_returns_empty(self):
        opps = [_opportunity()]
        ranked = self._rank(opps, signals=_tech("AAPL"), mkt_open=False)
        assert ranked == []

    def test_pdt_rejection_removes_opportunity(self):
        opps = [_opportunity()]
        ranked = self._rank(opps, signals=_tech("AAPL"), pdt=_pdt_guard(allow=False))
        assert ranked == []

    def test_score_fields_populated(self):
        opps = [_opportunity()]
        ranked = self._rank(opps, signals=_tech("AAPL"))
        assert len(ranked) == 1
        s = ranked[0]
        assert s.symbol == "AAPL"
        assert 0.0 <= s.composite_score <= 1.0
        assert 0.0 <= s.ai_conviction <= 1.0
        assert 0.0 <= s.technical_score <= 1.0
        assert 0.0 <= s.risk_adjusted_return <= 1.0
        assert 0.0 <= s.liquidity_score <= 1.0


# ---------------------------------------------------------------------------
# 6. Exit action ranking
# ---------------------------------------------------------------------------

class TestRankExitActions:

    def _rank_exits(self, reviews, signals=None):
        r = _ranker()
        rr = _reasoning_result(reviews=reviews)
        return r.rank_exit_actions(rr, signals or {})

    def test_empty_reviews_returns_empty(self):
        assert self._rank_exits([]) == []

    def test_exit_has_maximum_urgency(self):
        reviews = [{"symbol": "AAPL", "action": "exit", "updated_reasoning": "thesis broken"}]
        actions = self._rank_exits(reviews, {"AAPL": {"composite_technical_score": 0.9}})
        assert actions[0].urgency == pytest.approx(1.0)
        assert actions[0].action == "exit"

    def test_hold_has_lower_urgency_when_tech_strong(self):
        reviews = [{"symbol": "AAPL", "action": "hold", "updated_reasoning": "all good"}]
        # tech score = 0.9 → urgency = max(0, 1 - 0.9) = 0.1
        actions = self._rank_exits(reviews, {"AAPL": {"composite_technical_score": 0.9}})
        assert actions[0].urgency == pytest.approx(0.1)

    def test_hold_has_higher_urgency_when_tech_weak(self):
        reviews = [{"symbol": "AAPL", "action": "hold", "updated_reasoning": "uncertain"}]
        # tech score = 0.2 → urgency = 0.8
        actions = self._rank_exits(reviews, {"AAPL": {"composite_technical_score": 0.2}})
        assert actions[0].urgency == pytest.approx(0.8)

    def test_adjust_urgency_higher_when_tech_weak(self):
        reviews = [{"symbol": "AAPL", "action": "adjust", "updated_reasoning": "tighten stop"}]
        # tech score = 0.2 → urgency = 0.5 + 0.5*(1-0.2) = 0.9
        actions = self._rank_exits(reviews, {"AAPL": {"composite_technical_score": 0.2}})
        assert actions[0].urgency == pytest.approx(0.9)

    def test_adjust_urgency_lower_when_tech_strong(self):
        reviews = [{"symbol": "AAPL", "action": "adjust", "updated_reasoning": "tweak"}]
        # tech score = 0.9 → urgency = 0.5 + 0.5*0.1 = 0.55
        actions = self._rank_exits(reviews, {"AAPL": {"composite_technical_score": 0.9}})
        assert actions[0].urgency == pytest.approx(0.55)

    def test_mixed_hold_exit_adjust_sorted_by_urgency(self):
        reviews = [
            {"symbol": "A", "action": "hold", "updated_reasoning": "ok"},
            {"symbol": "B", "action": "exit", "updated_reasoning": "exit now"},
            {"symbol": "C", "action": "adjust", "updated_reasoning": "tweak"},
        ]
        signals = {
            "A": {"composite_technical_score": 0.8},
            "B": {"composite_technical_score": 0.5},
            "C": {"composite_technical_score": 0.5},
        }
        actions = self._rank_exits(reviews, signals)
        # exit (1.0) > adjust (0.75) > hold (0.2)
        assert actions[0].symbol == "B"
        assert actions[0].urgency == pytest.approx(1.0)
        assert actions[1].symbol == "C"
        assert actions[2].symbol == "A"

    def test_adjusted_targets_preserved(self):
        targets = {"profit_target": 180.0, "stop_loss": 155.0}
        reviews = [
            {
                "symbol": "AAPL",
                "action": "adjust",
                "updated_reasoning": "r",
                "adjusted_targets": targets,
            }
        ]
        actions = self._rank_exits(reviews, {})
        assert actions[0].adjusted_targets == targets

    def test_no_adjusted_targets_is_none(self):
        reviews = [{"symbol": "AAPL", "action": "hold", "updated_reasoning": "r"}]
        actions = self._rank_exits(reviews, {})
        assert actions[0].adjusted_targets is None

    def test_missing_tech_signals_uses_neutral_defaults(self):
        reviews = [
            {"symbol": "AAPL", "action": "hold", "updated_reasoning": "r"},
            {"symbol": "TSLA", "action": "exit", "updated_reasoning": "r"},
        ]
        actions = self._rank_exits(reviews, {})
        # exit still max urgency; hold uses default tech=0.5 → urgency=0.5
        assert actions[0].symbol == "TSLA"
        assert actions[0].urgency == pytest.approx(1.0)
        assert actions[1].urgency == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 7. Conviction threshold (min_conviction_threshold)
# ---------------------------------------------------------------------------

class TestConvictionThreshold:

    def _filter_with_threshold(self, conviction, threshold):
        r = OpportunityRanker({"min_conviction_threshold": threshold})
        opp = _opportunity(conviction=conviction)
        return r.apply_hard_filters(
            opp,
            _account(),
            _portfolio(),
            _pdt_guard(True),
            _market_open(True),
            orders=[],
        )

    def test_conviction_below_threshold_filtered_out(self):
        passes, reason = self._filter_with_threshold(conviction=0.05, threshold=0.10)
        assert passes is False
        assert "conviction" in reason.lower()

    def test_conviction_at_threshold_passes(self):
        passes, _ = self._filter_with_threshold(conviction=0.10, threshold=0.10)
        assert passes is True

    def test_conviction_above_threshold_passes(self):
        passes, _ = self._filter_with_threshold(conviction=0.80, threshold=0.10)
        assert passes is True

    def test_conviction_filter_logged(self, caplog):
        """rank_opportunities logs rejection at INFO when conviction is below threshold."""
        import logging
        r = OpportunityRanker({"min_conviction_threshold": 0.10})
        opp = _opportunity(symbol="JUNK", conviction=0.05)
        rr = _reasoning_result(opportunities=[opp])
        with caplog.at_level(logging.INFO, logger="ozymandias.intelligence.opportunity_ranker"):
            r.rank_opportunities(
                rr, {}, _account(), _portfolio(), _pdt_guard(True), _market_open(True), []
            )
        assert any("JUNK" in r.message for r in caplog.records)
