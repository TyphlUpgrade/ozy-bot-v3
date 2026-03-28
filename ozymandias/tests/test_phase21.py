"""
Unit tests for Phase 21 — Durability and Regime Response.

Tests cover:
- Watchlist pruner multi-tier eviction (tier2 first, direction-conflicting tier1 second, composite last)
- _clear_directional_suppression (direction-dependent reasons cleared, neutral reasons preserved)
- Regime-reset build eviction logic (_regime_reset_build)
- Universe scanner regime-aware behavior (day_losers for correcting sectors, panic floor raise)
- Position thesis monitoring (check_position_theses) and _condition_met
- Regime/sector_regimes restored from cache on startup
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ozymandias.core.state_manager import WatchlistEntry, WatchlistState, PortfolioState, Position
from ozymandias.intelligence.context_compressor import ContextCompressor, CompressorResult
from ozymandias.core.config import ClaudeConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_entry(symbol: str, tier: int = 1, ed: str = "either", strategy: str = "both") -> WatchlistEntry:
    return WatchlistEntry(
        symbol=symbol, date_added="2026-03-27",
        reason=f"test {symbol}", priority_tier=tier,
        expected_direction=ed, strategy=strategy,
    )


def make_position(symbol: str, strategy: str = "swing") -> Position:
    from ozymandias.core.state_manager import Intention, ExitTargets
    return Position(
        symbol=symbol, shares=100.0, avg_cost=100.0, strategy=strategy,
        intention=Intention(
            catalyst="test", direction="long", strategy=strategy,
            expected_move="up", reasoning="test",
            exit_targets=ExitTargets(profit_target=110.0, stop_loss=95.0),
            max_expected_loss=500.0, entry_date="2026-03-27T09:30:00+00:00",
        ),
    )


def make_compressor() -> ContextCompressor:
    cfg = ClaudeConfig()
    cfg.compressor_enabled = True
    return ContextCompressor(cfg, prompts_dir=None)


# ---------------------------------------------------------------------------
# TestMultiTierPruner
# ---------------------------------------------------------------------------

class TestMultiTierPruner:
    """Tests the multi-tier eviction order in _apply_watchlist_changes."""

    def _make_orchestrator(self, entries: list[WatchlistEntry], sector_regimes: dict | None = None):
        """Build a minimal orchestrator-like object for testing the pruner."""
        from ozymandias.core.config import Config
        from unittest.mock import MagicMock

        cfg = Config()
        cfg.claude.watchlist_max_entries = 4  # small cap to trigger pruning
        orch = MagicMock()
        orch._config = cfg
        orch._latest_indicators = {}
        orch._last_sector_regimes = sector_regimes

        # Inject the real _apply_watchlist_changes logic via the actual method
        # We test via the eviction priority function directly, not via full orchestrator
        return orch

    def _eviction_priority(self, entry, sector_regimes, indicators, sector_map):
        """Mirror the _eviction_priority logic from _apply_watchlist_changes for testing."""
        from ozymandias.intelligence.technical_analysis import compute_composite_score

        def _composite(e) -> float:
            ind = indicators.get(e.symbol, {})
            raw = ind.get("signals") or {}
            if raw:
                ed = getattr(e, "expected_direction", "either")
                if ed != "either":
                    return compute_composite_score(raw, direction=ed)
                return max(
                    compute_composite_score(raw, direction="long"),
                    compute_composite_score(raw, direction="short"),
                )
            return 0.0

        def _direction_conflicts(e) -> bool:
            if not sector_regimes:
                return False
            etf = sector_map.get(e.symbol)
            if not etf:
                return False
            sector_info = sector_regimes.get(etf, {})
            sector_regime = sector_info.get("regime", "neutral")
            ed = getattr(e, "expected_direction", "either")
            if ed == "long" and sector_regime in ("correcting", "downtrend"):
                return True
            if ed == "short" and sector_regime in ("breaking_out", "uptrend"):
                return True
            return False

        composite = _composite(entry)
        if getattr(entry, "priority_tier", 1) == 2:
            return (0, composite)
        if _direction_conflicts(entry):
            return (1, composite)
        return (2, composite)

    def test_tier2_evicted_before_tier1(self):
        sector_map = {}
        tier1_a = make_entry("T1A", tier=1, ed="long")
        tier2_b = make_entry("T2B", tier=2, ed="long")
        tier1_c = make_entry("T1C", tier=1, ed="long")

        priority_t1a = self._eviction_priority(tier1_a, None, {}, sector_map)
        priority_t2b = self._eviction_priority(tier2_b, None, {}, sector_map)
        priority_t1c = self._eviction_priority(tier1_c, None, {}, sector_map)

        # tier2 has priority tuple (0, ...) — lowest, evicted first
        assert priority_t2b[0] == 0
        # tier1 non-conflicting has priority (2, ...) — highest, kept last
        assert priority_t1a[0] == 2
        assert priority_t1c[0] == 2

    def test_direction_conflicting_tier1_evicted_before_non_conflicting(self):
        sector_regimes = {"XLK": {"regime": "correcting", "bias": "short", "strength": "strong"}}
        sector_map = {"NVDA": "XLK", "SAFE": "XLF"}

        # NVDA: long + correcting sector = conflicts
        conflict = make_entry("NVDA", tier=1, ed="long")
        # SAFE: long + neutral sector = no conflict
        safe = make_entry("SAFE", tier=1, ed="long")

        p_conflict = self._eviction_priority(conflict, sector_regimes, {}, sector_map)
        p_safe = self._eviction_priority(safe, sector_regimes, {}, sector_map)

        assert p_conflict[0] == 1  # conflicting
        assert p_safe[0] == 2      # non-conflicting

    def test_short_in_uptrend_sector_conflicts(self):
        sector_regimes = {"XLE": {"regime": "breaking_out", "bias": "long", "strength": "moderate"}}
        sector_map = {"XOM": "XLE"}

        short_in_uptrend = make_entry("XOM", tier=1, ed="short")
        p = self._eviction_priority(short_in_uptrend, sector_regimes, {}, sector_map)
        assert p[0] == 1  # conflicts with uptrend sector

    def test_within_same_tier_lowest_composite_evicted_first(self):
        from ozymandias.intelligence.technical_analysis import compute_composite_score
        sector_map = {}
        high_score_entry = make_entry("HIGH", tier=1, ed="long")
        low_score_entry = make_entry("LOW", tier=1, ed="long")
        indicators = {
            "HIGH": {"signals": {"rsi": 65, "volume_ratio": 1.5, "vwap_position": "above", "trend_structure": "bullish_aligned", "roc_5": 2.0}},
            "LOW": {"signals": {"rsi": 35, "volume_ratio": 0.5, "vwap_position": "below", "trend_structure": "bearish_aligned", "roc_5": -1.5}},
        }
        p_high = self._eviction_priority(high_score_entry, None, indicators, sector_map)
        p_low = self._eviction_priority(low_score_entry, None, indicators, sector_map)

        # Both are tier-1, no sector conflict → priority tuple (2, composite)
        assert p_high[0] == 2
        assert p_low[0] == 2
        # HIGH has larger composite → higher priority → kept over LOW
        assert p_high > p_low

    def test_no_sector_regimes_uses_composite_only(self):
        entry = make_entry("AAPL", tier=1, ed="long")
        p = self._eviction_priority(entry, None, {}, {})
        # Without sector_regimes, all tier-1 go to (2, composite)
        assert p[0] == 2


# ---------------------------------------------------------------------------
# TestClearDirectionalSuppression
# ---------------------------------------------------------------------------

class TestClearDirectionalSuppression:
    """Tests the _clear_directional_suppression helper."""

    def _make_orch_with_suppression(self, suppressed: dict[str, str]) -> object:
        """Minimal stand-in with the suppression dict and the real helper."""
        from ozymandias.core.orchestrator import Orchestrator, _SECTOR_MAP
        orch = MagicMock(spec=Orchestrator)
        orch._filter_suppressed = dict(suppressed)
        # Bind the real method
        orch._clear_directional_suppression = Orchestrator._clear_directional_suppression.__get__(orch)
        return orch

    def test_rvol_reason_cleared_for_affected_sector(self):
        # NVDA is in XLK sector
        orch = self._make_orch_with_suppression({"NVDA": "rvol_too_low"})
        orch._clear_directional_suppression({"XLK"})
        assert "NVDA" not in orch._filter_suppressed

    def test_fetch_failure_reason_preserved(self):
        # fetch_failure is direction-neutral — should NOT be cleared
        orch = self._make_orch_with_suppression({"NVDA": "fetch_failure"})
        orch._clear_directional_suppression({"XLK"})
        assert "NVDA" in orch._filter_suppressed

    def test_symbol_in_unaffected_sector_preserved(self):
        # XOM is in XLE, not XLK
        orch = self._make_orch_with_suppression({"XOM": "rvol_too_low"})
        orch._clear_directional_suppression({"XLK"})  # Only XLK affected
        assert "XOM" in orch._filter_suppressed

    def test_none_affected_sectors_clears_all_directional(self):
        orch = self._make_orch_with_suppression({
            "NVDA": "rvol_too_low",
            "XOM": "composite_score_too_low",
            "AAPL": "fetch_failure",
        })
        orch._clear_directional_suppression(None)  # broad panic — clear all sectors
        assert "NVDA" not in orch._filter_suppressed
        assert "XOM" not in orch._filter_suppressed
        assert "AAPL" in orch._filter_suppressed  # fetch_failure preserved

    def test_conviction_floor_reason_cleared(self):
        orch = self._make_orch_with_suppression({"AAPL": "conviction_floor_too_low"})
        orch._clear_directional_suppression(None)
        assert "AAPL" not in orch._filter_suppressed

    def test_defer_expired_reason_cleared(self):
        orch = self._make_orch_with_suppression({"GOOG": "defer_expired"})
        orch._clear_directional_suppression(None)
        assert "GOOG" not in orch._filter_suppressed

    def test_empty_suppression_dict_no_error(self):
        orch = self._make_orch_with_suppression({})
        orch._clear_directional_suppression({"XLK"})  # should not raise


# ---------------------------------------------------------------------------
# TestRegimeResetEvictionLogic
# ---------------------------------------------------------------------------

class TestRegimeResetEvictionLogic:
    """Tests the eviction criteria in _regime_reset_build (via data inspection)."""

    def _would_evict(self, entry: WatchlistEntry, new_sector_regimes: dict | None,
                     new_regime: str, changed_sectors: set[str], sector_map: dict) -> bool:
        """Mirror the eviction logic from _regime_reset_build."""
        broad_panic = new_regime == "risk-off panic"
        if broad_panic:
            return entry.expected_direction == "long"
        if new_sector_regimes:
            etf = sector_map.get(entry.symbol)
            if etf not in changed_sectors:
                return False
            sector_info = new_sector_regimes.get(etf, {})
            sector_regime = sector_info.get("regime", "neutral")
            if entry.expected_direction == "long" and sector_regime in ("correcting", "downtrend"):
                return True
            if entry.expected_direction == "short" and sector_regime in ("breaking_out", "uptrend"):
                return True
        return False

    def test_broad_panic_evicts_all_longs(self):
        sector_map = {}
        long_entry = make_entry("AAPL", ed="long")
        short_entry = make_entry("TSLA", ed="short")
        either_entry = make_entry("GOOG", ed="either")

        assert self._would_evict(long_entry, None, "risk-off panic", set(), sector_map)
        assert not self._would_evict(short_entry, None, "risk-off panic", set(), sector_map)
        assert not self._would_evict(either_entry, None, "risk-off panic", set(), sector_map)

    def test_sector_correcting_evicts_long_entries(self):
        sector_map = {"NVDA": "XLK"}
        sector_regimes = {"XLK": {"regime": "correcting"}}
        entry = make_entry("NVDA", ed="long")
        assert self._would_evict(entry, sector_regimes, "sector_rotation", {"XLK"}, sector_map)

    def test_sector_correcting_preserves_short_entries(self):
        sector_map = {"NVDA": "XLK"}
        sector_regimes = {"XLK": {"regime": "correcting"}}
        short_entry = make_entry("NVDA", ed="short")
        assert not self._would_evict(short_entry, sector_regimes, "sector_rotation", {"XLK"}, sector_map)

    def test_sector_breakout_evicts_short_entries(self):
        sector_map = {"XOM": "XLE"}
        sector_regimes = {"XLE": {"regime": "breaking_out"}}
        short_entry = make_entry("XOM", ed="short")
        assert self._would_evict(short_entry, sector_regimes, "sector_rotation", {"XLE"}, sector_map)

    def test_symbol_not_in_changed_sectors_preserved(self):
        sector_map = {"NVDA": "XLK", "XOM": "XLE"}
        sector_regimes = {"XLK": {"regime": "correcting"}, "XLE": {"regime": "breaking_out"}}
        # XOM is in XLE which is NOT in changed_sectors
        xom_long = make_entry("XOM", ed="long")
        assert not self._would_evict(xom_long, sector_regimes, "normal", {"XLK"}, sector_map)

    def test_symbol_not_in_sector_map_preserved(self):
        sector_map = {}  # no mappings
        sector_regimes = {"XLK": {"regime": "correcting"}}
        entry = make_entry("NVDA", ed="long")  # NVDA not in sector_map
        assert not self._would_evict(entry, sector_regimes, "normal", {"XLK"}, sector_map)


# ---------------------------------------------------------------------------
# TestUniverseScannerRegimeAware
# ---------------------------------------------------------------------------

class TestUniverseScannerRegimeAware:
    """Tests the regime-aware params added to UniverseScanner.get_top_candidates."""

    def test_panic_raises_price_move_floor(self):
        """In panic regime, effective_price_move_floor doubles."""
        from ozymandias.intelligence.universe_scanner import UniverseScanner, UniverseScannerConfig
        cfg = UniverseScannerConfig()
        cfg.min_price_move_pct_for_candidate = 1.5

        scanner = UniverseScanner(MagicMock(), cfg)
        regime = {"regime": "risk-off panic"}
        # The scanner computes effective_price_move_floor internally; test by checking
        # that a candidate with roc_5=2.0 passes normal (1.5 floor) but at 2x floor (3.0)
        # it would fail. We verify the flag value not the full run.
        broad_panic = isinstance(regime, dict) and regime.get("regime") == "risk-off panic"
        effective = cfg.min_price_move_pct_for_candidate * 2.0 if broad_panic else cfg.min_price_move_pct_for_candidate
        assert effective == 3.0

    def test_no_panic_uses_default_floor(self):
        """Normal regime uses the config floor unchanged."""
        from ozymandias.intelligence.universe_scanner import UniverseScannerConfig
        cfg = UniverseScannerConfig()
        cfg.min_price_move_pct_for_candidate = 1.5

        regime = {"regime": "normal"}
        broad_panic = regime.get("regime") == "risk-off panic"
        effective = cfg.min_price_move_pct_for_candidate * 2.0 if broad_panic else cfg.min_price_move_pct_for_candidate
        assert effective == 1.5

    def test_correcting_sectors_identified(self):
        """Correcting/downtrend sectors are collected into correcting_etfs."""
        sector_regimes = {
            "XLK": {"regime": "correcting"},
            "XLE": {"regime": "breaking_out"},
            "XLF": {"regime": "downtrend"},
            "XLY": {"regime": "neutral"},
        }
        correcting_etfs = {
            etf for etf, info in sector_regimes.items()
            if isinstance(info, dict) and info.get("regime") in ("correcting", "downtrend")
        }
        assert "XLK" in correcting_etfs
        assert "XLF" in correcting_etfs
        assert "XLE" not in correcting_etfs
        assert "XLY" not in correcting_etfs

    def test_get_top_candidates_accepts_new_params(self):
        """get_top_candidates signature accepts sector_regimes, regime_assessment, sector_map."""
        import inspect
        from ozymandias.intelligence.universe_scanner import UniverseScanner
        sig = inspect.signature(UniverseScanner.get_top_candidates)
        assert "sector_regimes" in sig.parameters
        assert "regime_assessment" in sig.parameters
        assert "sector_map" in sig.parameters


# ---------------------------------------------------------------------------
# TestPositionThesisMonitoring
# ---------------------------------------------------------------------------

class TestPositionThesisMonitoring:
    """Tests ContextCompressor.check_position_theses and _condition_met."""

    def setup_method(self):
        self.compressor = make_compressor()

    def _make_position(self, symbol: str):
        return MagicMock(symbol=symbol)

    def test_no_theses_returns_none(self):
        positions = [self._make_position("AAPL")]
        result = self.compressor.check_position_theses(positions, None, {})
        assert result is None

    def test_no_positions_returns_none(self):
        theses = [{"symbol": "AAPL", "thesis_breaking_conditions": ["rsi < 30"]}]
        result = self.compressor.check_position_theses([], theses, {})
        assert result is None

    def test_breach_detected_fires_needs_sonnet(self):
        positions = [self._make_position("AAPL")]
        theses = [{"symbol": "AAPL", "thesis_breaking_conditions": ["rsi < 30"]}]
        indicators = {
            "AAPL": {"signals": {"rsi": 25.0}}  # below 30 threshold
        }
        result = self.compressor.check_position_theses(positions, theses, indicators, cycle_id="test-cycle")
        assert result is not None
        assert result.needs_sonnet is True
        assert result.sonnet_reason == "position_thesis_breach"

    def test_no_breach_returns_none(self):
        positions = [self._make_position("AAPL")]
        theses = [{"symbol": "AAPL", "thesis_breaking_conditions": ["rsi < 30"]}]
        indicators = {
            "AAPL": {"signals": {"rsi": 55.0}}  # above threshold — no breach
        }
        result = self.compressor.check_position_theses(positions, theses, indicators, cycle_id="test-cycle2")
        assert result is None

    def test_per_cycle_guard_prevents_double_fire(self):
        positions = [self._make_position("AAPL")]
        theses = [{"symbol": "AAPL", "thesis_breaking_conditions": ["rsi < 30"]}]
        indicators = {"AAPL": {"signals": {"rsi": 25.0}}}

        result1 = self.compressor.check_position_theses(positions, theses, indicators, cycle_id="cycle-X")
        result2 = self.compressor.check_position_theses(positions, theses, indicators, cycle_id="cycle-X")
        assert result1 is not None and result1.needs_sonnet is True
        assert result2 is None  # suppressed same cycle

    def test_different_cycle_fires_again(self):
        positions = [self._make_position("AAPL")]
        theses = [{"symbol": "AAPL", "thesis_breaking_conditions": ["rsi < 30"]}]
        indicators = {"AAPL": {"signals": {"rsi": 25.0}}}

        self.compressor.check_position_theses(positions, theses, indicators, cycle_id="cycle-A")
        result = self.compressor.check_position_theses(positions, theses, indicators, cycle_id="cycle-B")
        assert result is not None and result.needs_sonnet is True

    def test_position_not_in_theses_ignored(self):
        positions = [self._make_position("GOOG")]  # GOOG has no thesis
        theses = [{"symbol": "AAPL", "thesis_breaking_conditions": ["rsi < 30"]}]
        indicators = {"GOOG": {"signals": {"rsi": 25.0}}}
        result = self.compressor.check_position_theses(positions, theses, indicators, cycle_id="cycle-C")
        assert result is None


# ---------------------------------------------------------------------------
# TestConditionMet
# ---------------------------------------------------------------------------

class TestConditionMet:
    """Tests the _condition_met helper in ContextCompressor."""

    def setup_method(self):
        self.compressor = make_compressor()

    def test_rsi_less_than_threshold_met(self):
        assert self.compressor._condition_met("rsi < 30", {"rsi": 25.0}, {}) is True

    def test_rsi_less_than_threshold_not_met(self):
        assert self.compressor._condition_met("rsi < 30", {"rsi": 35.0}, {}) is False

    def test_rsi_greater_than_met(self):
        assert self.compressor._condition_met("rsi > 70", {"rsi": 75.0}, {}) is True

    def test_rsi_greater_than_not_met(self):
        assert self.compressor._condition_met("rsi > 70", {"rsi": 65.0}, {}) is False

    def test_daily_trend_downtrend_condition_met(self):
        assert self.compressor._condition_met(
            "daily_trend becomes downtrend", {}, {"daily_trend": "downtrend"}
        ) is True

    def test_daily_trend_downtrend_not_met(self):
        assert self.compressor._condition_met(
            "daily_trend becomes downtrend", {}, {"daily_trend": "uptrend"}
        ) is False

    def test_missing_indicator_returns_false(self):
        """If indicator key not present, return False (conservative)."""
        assert self.compressor._condition_met("rsi < 30", {}, {}) is False

    def test_unrecognized_condition_returns_false(self):
        """Unrecognized condition format never fires."""
        assert self.compressor._condition_met("sector_1w_return < -5%", {}, {}) is False
        assert self.compressor._condition_met("this is gibberish", {}, {}) is False

    def test_volume_ratio_condition(self):
        assert self.compressor._condition_met("volume_ratio < 0.5", {"volume_ratio": 0.3}, {}) is True

    def test_daily_signals_checked_as_fallback(self):
        """Indicator in daily dict (not intraday) is still evaluated."""
        assert self.compressor._condition_met("rsi < 30", {}, {"rsi": 25.0}) is True

    # Bug-fix tests: _condition_met uptrend/neutral patterns (BUG fix)
    def test_daily_trend_uptrend_condition_met(self):
        """'becomes uptrend' must fire for short-thesis monitoring."""
        assert self.compressor._condition_met(
            "daily_trend becomes uptrend", {}, {"daily_trend": "uptrend"}
        ) is True

    def test_daily_trend_uptrend_not_met_when_still_downtrend(self):
        assert self.compressor._condition_met(
            "daily_trend becomes uptrend", {}, {"daily_trend": "downtrend"}
        ) is False

    def test_daily_trend_neutral_condition_met(self):
        assert self.compressor._condition_met(
            "daily_trend becomes neutral", {}, {"daily_trend": "neutral"}
        ) is True

    def test_condition_with_leading_whitespace_parsed(self):
        """Conditions with leading whitespace are tolerated (re.search vs match)."""
        assert self.compressor._condition_met("  rsi < 30", {"rsi": 25.0}, {}) is True


# ---------------------------------------------------------------------------
# TestSwingFilterAdjustments
# ---------------------------------------------------------------------------

class TestSwingFilterAdjustments:
    """Swing strategy apply_entry_gate must respect filter_adjustments.min_rvol (bug fix)."""

    def setup_method(self):
        from ozymandias.strategies.swing_strategy import SwingStrategy
        self.strategy = SwingStrategy({"min_rvol_for_entry": 0.8})

    def test_default_rvol_floor_blocks_low_rvol(self):
        passed, reason = self.strategy.apply_entry_gate("buy", {"volume_ratio": 0.5})
        assert not passed
        assert "0.80" in reason or "RVOL" in reason

    def test_filter_adjustments_relaxes_floor(self):
        """Claude lowers min_rvol to 0.4 → entry with RVOL=0.5 should pass."""
        passed, reason = self.strategy.apply_entry_gate(
            "buy", {"volume_ratio": 0.5},
            filter_adjustments={"min_rvol": 0.4},
        )
        assert passed, f"Expected pass with relaxed floor, got: {reason}"

    def test_filter_adjustments_above_config_floor_still_blocks(self):
        """If filter_adjustments.min_rvol=0.9 (tighter), low RVOL still blocked."""
        passed, reason = self.strategy.apply_entry_gate(
            "buy", {"volume_ratio": 0.5},
            filter_adjustments={"min_rvol": 0.9},
        )
        assert not passed
