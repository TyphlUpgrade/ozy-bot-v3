"""
tests/test_entry_conditions.py
================================
Unit tests for Phase 14: Claude-Directed Entry Conditions.

Tests cover:
  - evaluate_entry_conditions() evaluator function (all condition keys)
  - ScoredOpportunity.entry_conditions field propagation
  - _medium_try_entry gate in the orchestrator
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ozymandias.intelligence.opportunity_ranker import (
    OpportunityRanker,
    ScoredOpportunity,
    evaluate_entry_conditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_signals(**overrides) -> dict:
    base = {
        "vwap_position": "above",
        "rsi": 58.0,
        "rsi_slope_5": 0.8,
        "volume_ratio": 1.6,
        "volume_trend_bars": 3,
        "macd_signal": "bullish",
        "macd_histogram_expanding": True,
        "trend_structure": "bullish_aligned",
        "rsi_divergence": False,
        "bollinger_position": "upper_half",
        "price": 150.0,
        "avg_daily_volume": 2_000_000,
        "atr_14": 3.0,
        "long_score": 0.72,
    "short_score": 0.45,
    }
    base.update(overrides)
    return base


def _conditions(**kwargs) -> dict:
    return kwargs


# ---------------------------------------------------------------------------
# evaluate_entry_conditions — pass cases
# ---------------------------------------------------------------------------

class TestEvaluateEntryConditionsPasses:
    def test_empty_conditions_always_pass(self):
        passed, reason = evaluate_entry_conditions({}, _full_signals())
        assert passed is True
        assert reason == ""

    def test_none_conditions_always_pass(self):
        passed, reason = evaluate_entry_conditions(None, _full_signals())
        assert passed is True
        assert reason == ""

    def test_all_conditions_met(self):
        conds = _conditions(
            require_above_vwap=True,
            rsi_min=50,
            rsi_max=72,
            require_volume_ratio_min=1.4,
            require_macd_bullish=True,
        )
        passed, reason = evaluate_entry_conditions(conds, _full_signals())
        assert passed is True
        assert reason == ""

    def test_rsi_at_exact_min_boundary(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(rsi_min=58),
            _full_signals(rsi=58.0),
        )
        assert passed is True

    def test_rsi_at_exact_max_boundary(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(rsi_max=58),
            _full_signals(rsi=58.0),
        )
        assert passed is True

    def test_require_above_vwap_false_is_noop(self):
        # require_above_vwap=False should not enforce the condition
        passed, _ = evaluate_entry_conditions(
            _conditions(require_above_vwap=False),
            _full_signals(vwap_position="below"),
        )
        assert passed is True

    def test_require_macd_bullish_cross_counts(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_macd_bullish=True),
            _full_signals(macd_signal="bullish_cross"),
        )
        assert passed is True


# ---------------------------------------------------------------------------
# evaluate_entry_conditions — fail cases
# ---------------------------------------------------------------------------

class TestEvaluateEntryConditionsFails:
    def test_rsi_min_not_met(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(rsi_min=55),
            _full_signals(rsi=48.0),
        )
        assert passed is False
        assert "rsi" in reason.lower()
        assert "48" in reason

    def test_rsi_max_exceeded(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(rsi_max=65),
            _full_signals(rsi=71.0),
        )
        assert passed is False
        assert "rsi" in reason.lower()
        assert "71" in reason

    def test_require_above_vwap_fails(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_above_vwap=True),
            _full_signals(vwap_position="below"),
        )
        assert passed is False
        assert "vwap" in reason.lower()

    def test_volume_ratio_below_min(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_volume_ratio_min=1.5),
            _full_signals(volume_ratio=1.1),
        )
        assert passed is False
        assert "volume" in reason.lower()
        assert "1.1" in reason

    def test_require_macd_bullish_fails_on_bearish(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_macd_bullish=True),
            _full_signals(macd_signal="bearish"),
        )
        assert passed is False
        assert "macd" in reason.lower()

    def test_require_macd_bullish_fails_on_neutral(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_macd_bullish=True),
            _full_signals(macd_signal="neutral"),
        )
        assert passed is False

    def test_first_failing_condition_returned(self):
        # rsi_min fails but vwap also fails — only first rejection returned
        conds = _conditions(require_above_vwap=True, rsi_min=70)
        sigs = _full_signals(vwap_position="below", rsi=65.0)
        passed, reason = evaluate_entry_conditions(conds, sigs)
        assert passed is False
        # First check is require_above_vwap
        assert "vwap" in reason.lower()


# ---------------------------------------------------------------------------
# evaluate_entry_conditions — missing signal keys
# ---------------------------------------------------------------------------

class TestEvaluateEntryConditionsMissingSignals:
    def test_missing_vwap_position_key(self):
        sigs = _full_signals()
        del sigs["vwap_position"]
        passed, reason = evaluate_entry_conditions(
            _conditions(require_above_vwap=True), sigs
        )
        assert passed is False
        assert "vwap_position" in reason
        assert "unavailable" in reason

    def test_missing_rsi_key(self):
        sigs = _full_signals()
        del sigs["rsi"]
        passed, reason = evaluate_entry_conditions(_conditions(rsi_min=50), sigs)
        assert passed is False
        assert "rsi" in reason
        assert "unavailable" in reason

    def test_missing_volume_ratio_key(self):
        sigs = _full_signals()
        del sigs["volume_ratio"]
        passed, reason = evaluate_entry_conditions(
            _conditions(require_volume_ratio_min=1.2), sigs
        )
        assert passed is False
        assert "volume_ratio" in reason
        assert "unavailable" in reason

    def test_missing_macd_signal_key(self):
        sigs = _full_signals()
        del sigs["macd_signal"]
        passed, reason = evaluate_entry_conditions(
            _conditions(require_macd_bullish=True), sigs
        )
        assert passed is False
        assert "macd_signal" in reason
        assert "unavailable" in reason

    def test_empty_signals_dict_all_conditions_unavailable(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(rsi_min=50), {}
        )
        assert passed is False
        assert "unavailable" in reason


# ---------------------------------------------------------------------------
# ScoredOpportunity — entry_conditions propagation
# ---------------------------------------------------------------------------

def _make_account():
    from ozymandias.execution.broker_interface import AccountInfo
    return AccountInfo(
        equity=50_000.0, buying_power=50_000.0, cash=50_000.0,
        currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
    )

def _make_portfolio():
    from ozymandias.core.state_manager import PortfolioState
    return PortfolioState()

def _make_signals(symbol="AAPL"):
    return {symbol: {"long_score": 0.7, "short_score": 0.45, "signals": _full_signals()}}


class TestScoredOpportunityPropagation:
    def test_entry_conditions_populated(self):
        r = OpportunityRanker()
        conds = {"rsi_min": 52, "require_above_vwap": True}
        opp = {
            "symbol": "AAPL", "action": "buy", "strategy": "momentum",
            "conviction": 0.7, "suggested_entry": 150.0,
            "suggested_exit": 165.0, "suggested_stop": 140.0,
            "position_size_pct": 0.10, "reasoning": "test",
            "entry_conditions": conds,
        }
        result = r.score_opportunity(opp, _make_signals(), _make_account(), _make_portfolio())
        assert result.entry_conditions == conds

    def test_entry_conditions_default_empty(self):
        r = OpportunityRanker()
        opp = {
            "symbol": "AAPL", "action": "buy", "strategy": "momentum",
            "conviction": 0.7, "suggested_entry": 150.0,
            "suggested_exit": 165.0, "suggested_stop": 140.0,
            "position_size_pct": 0.10, "reasoning": "test",
        }
        result = r.score_opportunity(opp, _make_signals(), _make_account(), _make_portfolio())
        assert result.entry_conditions == {}

    def test_entry_conditions_none_becomes_empty(self):
        r = OpportunityRanker()
        opp = {
            "symbol": "AAPL", "action": "buy", "strategy": "momentum",
            "conviction": 0.7, "suggested_entry": 150.0,
            "suggested_exit": 165.0, "suggested_stop": 140.0,
            "position_size_pct": 0.10, "reasoning": "test",
            "entry_conditions": None,
        }
        result = r.score_opportunity(opp, _make_signals(), _make_account(), _make_portfolio())
        assert result.entry_conditions == {}


# ---------------------------------------------------------------------------
# New condition keys added in Phase 16 session (short-direction support)
# ---------------------------------------------------------------------------

class TestRequireBelowVwap:
    def test_passes_when_below(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_below_vwap=True),
            _full_signals(vwap_position="below"),
        )
        assert passed is True
        assert reason == ""

    def test_fails_when_above(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_below_vwap=True),
            _full_signals(vwap_position="above"),
        )
        assert passed is False
        assert "vwap" in reason.lower()
        assert "above" in reason

    def test_false_is_noop(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_below_vwap=False),
            _full_signals(vwap_position="above"),
        )
        assert passed is True

    def test_missing_signal(self):
        sigs = _full_signals()
        del sigs["vwap_position"]
        passed, reason = evaluate_entry_conditions(_conditions(require_below_vwap=True), sigs)
        assert passed is False
        assert "vwap_position" in reason
        assert "unavailable" in reason


class TestRsiSlopeMin:
    def test_passes_when_slope_meets_min(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(rsi_slope_min=0.5),
            _full_signals(rsi_slope_5=0.8),
        )
        assert passed is True

    def test_passes_at_exact_boundary(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(rsi_slope_min=0.5),
            _full_signals(rsi_slope_5=0.5),
        )
        assert passed is True

    def test_fails_when_slope_below_min(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(rsi_slope_min=0.5),
            _full_signals(rsi_slope_5=0.2),
        )
        assert passed is False
        assert "rsi_slope_min" in reason
        assert "0.20" in reason

    def test_fails_when_slope_negative(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(rsi_slope_min=0.5),
            _full_signals(rsi_slope_5=-1.0),
        )
        assert passed is False

    def test_missing_signal(self):
        sigs = _full_signals()
        del sigs["rsi_slope_5"]
        passed, reason = evaluate_entry_conditions(_conditions(rsi_slope_min=0.5), sigs)
        assert passed is False
        assert "rsi_slope_5" in reason
        assert "unavailable" in reason


class TestRsiSlopeMax:
    def test_passes_when_slope_at_or_below_max(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(rsi_slope_max=-0.5),
            _full_signals(rsi_slope_5=-0.8),
        )
        assert passed is True

    def test_passes_at_exact_boundary(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(rsi_slope_max=-0.5),
            _full_signals(rsi_slope_5=-0.5),
        )
        assert passed is True

    def test_fails_when_slope_above_max(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(rsi_slope_max=-0.5),
            _full_signals(rsi_slope_5=0.3),
        )
        assert passed is False
        assert "rsi_slope_max" in reason
        assert "0.30" in reason

    def test_missing_signal(self):
        sigs = _full_signals()
        del sigs["rsi_slope_5"]
        passed, reason = evaluate_entry_conditions(_conditions(rsi_slope_max=-0.5), sigs)
        assert passed is False
        assert "rsi_slope_5" in reason
        assert "unavailable" in reason


class TestRequireVolumeTrendBarsMin:
    def test_passes_when_bars_meet_min(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_volume_trend_bars_min=2),
            _full_signals(volume_trend_bars=3),
        )
        assert passed is True

    def test_passes_at_exact_boundary(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_volume_trend_bars_min=3),
            _full_signals(volume_trend_bars=3),
        )
        assert passed is True

    def test_fails_when_bars_below_min(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_volume_trend_bars_min=3),
            _full_signals(volume_trend_bars=1),
        )
        assert passed is False
        assert "volume_trend_bars" in reason
        assert "1" in reason

    def test_fails_when_zero_bars(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_volume_trend_bars_min=1),
            _full_signals(volume_trend_bars=0),
        )
        assert passed is False

    def test_missing_signal(self):
        sigs = _full_signals()
        del sigs["volume_trend_bars"]
        passed, reason = evaluate_entry_conditions(
            _conditions(require_volume_trend_bars_min=2), sigs
        )
        assert passed is False
        assert "volume_trend_bars" in reason
        assert "unavailable" in reason


class TestRequireMacdBearish:
    def test_passes_on_bearish(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_macd_bearish=True),
            _full_signals(macd_signal="bearish"),
        )
        assert passed is True

    def test_passes_on_bearish_cross(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_macd_bearish=True),
            _full_signals(macd_signal="bearish_cross"),
        )
        assert passed is True

    def test_fails_on_bullish(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_macd_bearish=True),
            _full_signals(macd_signal="bullish"),
        )
        assert passed is False
        assert "macd" in reason.lower()

    def test_fails_on_neutral(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_macd_bearish=True),
            _full_signals(macd_signal="neutral"),
        )
        assert passed is False

    def test_false_is_noop(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_macd_bearish=False),
            _full_signals(macd_signal="bullish"),
        )
        assert passed is True

    def test_missing_signal(self):
        sigs = _full_signals()
        del sigs["macd_signal"]
        passed, reason = evaluate_entry_conditions(_conditions(require_macd_bearish=True), sigs)
        assert passed is False
        assert "macd_signal" in reason
        assert "unavailable" in reason


class TestRequireMacdHistogramExpanding:
    def test_passes_when_expanding(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_macd_histogram_expanding=True),
            _full_signals(macd_histogram_expanding=True),
        )
        assert passed is True

    def test_fails_when_contracting(self):
        passed, reason = evaluate_entry_conditions(
            _conditions(require_macd_histogram_expanding=True),
            _full_signals(macd_histogram_expanding=False),
        )
        assert passed is False
        assert "macd_histogram" in reason.lower() or "macd" in reason.lower()
        assert "contracting" in reason

    def test_false_is_noop(self):
        passed, _ = evaluate_entry_conditions(
            _conditions(require_macd_histogram_expanding=False),
            _full_signals(macd_histogram_expanding=False),
        )
        assert passed is True

    def test_missing_signal(self):
        sigs = _full_signals()
        del sigs["macd_histogram_expanding"]
        passed, reason = evaluate_entry_conditions(
            _conditions(require_macd_histogram_expanding=True), sigs
        )
        assert passed is False
        assert "macd_histogram_expanding" in reason
        assert "unavailable" in reason
