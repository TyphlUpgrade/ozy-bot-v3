"""
tests/test_opportunity_ranker.py
=================================
Unit tests for OpportunityRanker.score_opportunity().
"""
from __future__ import annotations

import pytest

from ozymandias.intelligence.opportunity_ranker import OpportunityRanker
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.core.state_manager import PortfolioState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ranker() -> OpportunityRanker:
    return OpportunityRanker()


def _account() -> AccountInfo:
    return AccountInfo(
        equity=50_000.0, buying_power=50_000.0, cash=50_000.0,
        pdt_flag=False, daytrade_count=0,
        currency="USD", account_id="test-account",
    )


def _portfolio() -> PortfolioState:
    return PortfolioState()


def _opportunity(**overrides) -> dict:
    base = {
        "symbol": "AAPL",
        "action": "buy",
        "strategy": "momentum",
        "conviction": 0.7,
        "suggested_entry": 150.0,
        "suggested_exit": 165.0,
        "suggested_stop": 140.0,
        "position_size_pct": 0.10,
        "reasoning": "test opportunity",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TestScoreOpportunity
# ---------------------------------------------------------------------------

class TestScoreOpportunity:
    def test_basic_score_returns_scored_opportunity(self):
        r = _ranker()
        opp = _opportunity()
        result = r.score_opportunity(opp, {}, _account(), _portfolio())
        assert result.symbol == "AAPL"
        assert result.action == "buy"
        assert 0.0 <= result.composite_score <= 1.0

    def test_entry_conditions_field_present(self):
        r = _ranker()
        conds = {"rsi_min": 50, "require_above_vwap": True}
        opp = _opportunity(conviction=0.7)
        opp["entry_conditions"] = conds
        result = r.score_opportunity(opp, {}, _account(), _portfolio())
        assert result.entry_conditions == conds
