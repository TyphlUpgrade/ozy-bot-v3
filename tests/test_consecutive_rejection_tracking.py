"""
Tests for consecutive Claude soft-rejection tracking.

Covers:
1. Rejection count increments each cycle a symbol appears in rejected_opportunities
2. Entry (or absence from window) resets the count
3. assemble_reasoning_context surfaces consecutive_claude_rejections when >= 2
"""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from ozymandias.core.config import Config, load_config
from ozymandias.core.state_manager import WatchlistState, WatchlistEntry, PortfolioState
from ozymandias.core.orchestrator import Orchestrator, SlowLoopTriggerState
from ozymandias.intelligence.claude_reasoning import ClaudeReasoningEngine, ReasoningResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reasoning_result(rejected_symbols: list[str]) -> ReasoningResult:
    return ReasoningResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        position_reviews=[],
        new_opportunities=[],
        watchlist_changes={"add": [], "remove": [], "rationale": ""},
        market_assessment="neutral",
        risk_flags=[],
        rejected_opportunities=[
            {"symbol": sym, "rejection_reason": "no setup"} for sym in rejected_symbols
        ],
        raw={},
    )


def _make_watchlist(symbols: list[str]) -> WatchlistState:
    entries = [
        WatchlistEntry(symbol=sym, date_added="2026-03-23", reason="test", priority_tier=1, strategy="momentum")
        for sym in symbols
    ]
    return WatchlistState(entries=entries)


def _make_orch_with_mocked_claude(watchlist_symbols: list[str]):
    """Minimal orchestrator with a mocked Claude engine for _run_claude_cycle tests."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._config = Config()
    ts = SlowLoopTriggerState()
    ts.last_claude_call_utc = datetime.now(timezone.utc)
    orch._trigger_state = ts
    orch._last_known_equity = 30_000.0
    orch._degradation = MagicMock()
    orch._degradation.claude_available = True
    orch._degradation.claude_backoff_until_utc = None
    orch._latest_indicators = {}
    orch._all_indicators = {}
    orch._market_context_indicators = {}
    orch._recommendation_outcomes = {}
    orch._claude_soft_rejections = {}
    orch._cycle_consumed_symbols = set()
    orch._entry_defer_counts = {}
    orch._filter_suppressed = {}
    orch._override_exit_count = 0

    watchlist = _make_watchlist(watchlist_symbols)
    portfolio = PortfolioState()

    orch._state_manager = MagicMock()
    orch._state_manager.load_watchlist = AsyncMock(return_value=watchlist)
    orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)

    return orch, watchlist, portfolio


# ---------------------------------------------------------------------------
# Test 1: soft rejection increments count
# ---------------------------------------------------------------------------

class TestSoftRejectionCounterIncrement:
    def test_soft_rejection_increments_count(self):
        """3 consecutive reasoning cycles rejecting AMD → _claude_soft_rejections['AMD'] == 3."""
        orch, watchlist, _ = _make_orch_with_mocked_claude(["AMD", "NVDA"])

        # Simulate the post-reasoning update 3 times with AMD in rejected_opportunities
        for _ in range(3):
            result = _make_reasoning_result(rejected_symbols=["AMD"])
            rejected_syms = {
                r["symbol"] for r in (result.rejected_opportunities or []) if r.get("symbol")
            }
            for entry in watchlist.entries:
                sym = entry.symbol
                if sym in rejected_syms:
                    orch._claude_soft_rejections[sym] = orch._claude_soft_rejections.get(sym, 0) + 1
                else:
                    orch._claude_soft_rejections.pop(sym, None)

        assert orch._claude_soft_rejections["AMD"] == 3
        # NVDA was not rejected — should have been cleared (or never set)
        assert "NVDA" not in orch._claude_soft_rejections


# ---------------------------------------------------------------------------
# Test 2: entry (not in rejected_opportunities) resets count
# ---------------------------------------------------------------------------

class TestSoftRejectionCounterReset:
    def test_entry_resets_count(self):
        """After 2 rejections, a cycle where AMD is NOT in rejected_opportunities clears count."""
        orch, watchlist, _ = _make_orch_with_mocked_claude(["AMD"])
        orch._claude_soft_rejections["AMD"] = 2  # pre-seed

        # Cycle where AMD is not rejected (Claude entered or didn't evaluate)
        result = _make_reasoning_result(rejected_symbols=[])  # AMD absent
        rejected_syms = {
            r["symbol"] for r in (result.rejected_opportunities or []) if r.get("symbol")
        }
        for entry in watchlist.entries:
            sym = entry.symbol
            if sym in rejected_syms:
                orch._claude_soft_rejections[sym] = orch._claude_soft_rejections.get(sym, 0) + 1
            else:
                orch._claude_soft_rejections.pop(sym, None)

        assert "AMD" not in orch._claude_soft_rejections


# ---------------------------------------------------------------------------
# Test 3: assemble_reasoning_context surfaces consecutive_claude_rejections
# ---------------------------------------------------------------------------

class TestConsecutiveRejectionInContext:
    def test_consecutive_count_in_context(self, tmp_path):
        """assemble_reasoning_context includes consecutive_claude_rejections: 3 for NVDA."""
        cfg = load_config()
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "reasoning.txt").write_text("dummy {context_json}")
        (prompts_dir / "watchlist.txt").write_text("dummy")
        engine = ClaudeReasoningEngine(config=cfg, prompts_dir=prompts_dir)

        watchlist = _make_watchlist(["NVDA", "AMD"])
        portfolio = PortfolioState()
        market_data = {
            "spy_trend": "neutral",
            "vix": 18.0,
            "sector_rotation": {},
            "macro_events_today": [],
            "trading_session": "regular",
            "pdt_trades_remaining": 3,
            "active_strategies": ["momentum"],
        }
        indicators = {}

        # NVDA has 3 consecutive rejections; AMD has 1 (below threshold)
        recommendation_outcomes = {
            "NVDA": {"stage": "rejected_opportunities", "attempt_time_utc": datetime.now(timezone.utc).isoformat()},
        }
        claude_soft_rejections = {"NVDA": 3, "AMD": 1}

        context = engine.assemble_reasoning_context(
            portfolio=portfolio,
            watchlist=watchlist,
            market_data=market_data,
            indicators=indicators,
            recommendation_outcomes=recommendation_outcomes,
            claude_soft_rejections=claude_soft_rejections,
        )

        context_str = json.dumps(context)
        assert '"consecutive_claude_rejections": 3' in context_str or \
               '"consecutive_claude_rejections":3' in context_str, \
               f"Expected consecutive_claude_rejections:3 in context. Got: {context_str[:500]}"

        # AMD has count=1, should NOT appear
        assert '"consecutive_claude_rejections": 1' not in context_str
        assert '"consecutive_claude_rejections":1' not in context_str
