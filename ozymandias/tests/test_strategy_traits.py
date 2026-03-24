"""
tests/test_strategy_traits.py
==============================
Unit tests for Phase 17 strategy behavioural traits:
  - is_intraday, uses_market_orders, blocks_eod_entries properties
  - apply_entry_gate() for both MomentumStrategy and SwingStrategy
  - _build_strategies() registry dispatch (no hardcoded names)
  - strategy_params dict lookup
"""
from __future__ import annotations

import pytest

from ozymandias.strategies.base_strategy import get_strategy
from ozymandias.strategies.momentum_strategy import MomentumStrategy
from ozymandias.strategies.swing_strategy import SwingStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _momentum(**params) -> MomentumStrategy:
    return MomentumStrategy(params or None)


def _swing(**params) -> SwingStrategy:
    return SwingStrategy(params or None)


def _signals(**kw) -> dict:
    base = {
        "volume_ratio": 1.5,
        "vwap_position": "above",
        "trend_structure": "bullish_aligned",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. Strategy trait properties
# ---------------------------------------------------------------------------

class TestStrategyProperties:

    def test_momentum_is_intraday(self):
        assert MomentumStrategy().is_intraday is True

    def test_swing_is_intraday(self):
        assert SwingStrategy().is_intraday is False

    def test_momentum_uses_market_orders(self):
        assert MomentumStrategy().uses_market_orders is True

    def test_swing_uses_market_orders(self):
        assert SwingStrategy().uses_market_orders is False

    def test_momentum_blocks_eod_entries(self):
        assert MomentumStrategy().blocks_eod_entries is True

    def test_swing_blocks_eod_entries(self):
        assert SwingStrategy().blocks_eod_entries is False


# ---------------------------------------------------------------------------
# 2. MomentumStrategy.apply_entry_gate
# ---------------------------------------------------------------------------

class TestMomentumEntryGate:

    def test_passes_when_conditions_met_long(self):
        strat = _momentum()
        passed, reason = strat.apply_entry_gate("buy", _signals(vwap_position="above", volume_ratio=1.5))
        assert passed is True
        assert reason == ""

    def test_passes_when_conditions_met_short(self):
        strat = _momentum()
        passed, reason = strat.apply_entry_gate("sell_short", _signals(vwap_position="below", volume_ratio=1.5))
        assert passed is True
        assert reason == ""

    def test_rejects_low_rvol(self):
        strat = _momentum()  # default min_rvol_for_entry=1.0
        passed, reason = strat.apply_entry_gate("buy", _signals(volume_ratio=0.8))
        assert passed is False
        assert "RVOL" in reason
        assert "0.80" in reason

    def test_rejects_long_with_price_below_vwap(self):
        strat = _momentum()
        passed, reason = strat.apply_entry_gate("buy", _signals(vwap_position="below", volume_ratio=1.5))
        assert passed is False
        assert "below VWAP" in reason

    def test_short_above_vwap_passes_gate(self):
        """Shorts are not VWAP-gated at the strategy level — mean-reversion fades above
        VWAP are valid; Claude controls the VWAP relationship via entry_conditions."""
        strat = _momentum()
        passed, reason = strat.apply_entry_gate("sell_short", _signals(vwap_position="above", volume_ratio=1.5))
        assert passed is True
        assert reason == ""

    def test_passes_long_with_price_above_vwap(self):
        strat = _momentum()
        passed, _ = strat.apply_entry_gate("buy", _signals(vwap_position="above", volume_ratio=1.5))
        assert passed is True

    def test_passes_short_with_price_below_vwap(self):
        strat = _momentum()
        passed, _ = strat.apply_entry_gate("sell_short", _signals(vwap_position="below", volume_ratio=1.5))
        assert passed is True

    def test_vwap_gate_disabled_via_param(self):
        strat = _momentum(require_vwap_gate=False)
        # Even with "wrong" vwap side, gate is off → passes
        passed, _ = strat.apply_entry_gate("buy", _signals(vwap_position="below", volume_ratio=1.5))
        assert passed is True

    # --- VWAP reclaim exception ---

    def test_reclaim_long_passes_with_bullish_macd_and_high_rvol(self):
        """Below-VWAP buy passes when MACD bullish + RVOL meets reclaim threshold."""
        strat = _momentum()
        passed, _ = strat.apply_entry_gate(
            "buy",
            _signals(vwap_position="below", volume_ratio=2.0, macd_signal="bullish"),
        )
        assert passed is True

    def test_reclaim_long_passes_with_bullish_cross_macd(self):
        """bullish_cross also qualifies for the reclaim exception."""
        strat = _momentum()
        passed, _ = strat.apply_entry_gate(
            "buy",
            _signals(vwap_position="below", volume_ratio=2.0, macd_signal="bullish_cross"),
        )
        assert passed is True

    def test_reclaim_rejected_when_rvol_below_threshold(self):
        """Bullish MACD alone is not enough — RVOL must meet vwap_reclaim_min_rvol."""
        strat = _momentum(vwap_reclaim_min_rvol=1.8)
        passed, reason = strat.apply_entry_gate(
            "buy",
            _signals(vwap_position="below", volume_ratio=1.5, macd_signal="bullish"),
        )
        assert passed is False
        assert "below VWAP" in reason

    def test_reclaim_rejected_when_macd_not_bullish(self):
        """High RVOL alone is not enough — MACD must be bullish for reclaim exception."""
        strat = _momentum()
        passed, reason = strat.apply_entry_gate(
            "buy",
            _signals(vwap_position="below", volume_ratio=2.5, macd_signal="bearish"),
        )
        assert passed is False
        assert "below VWAP" in reason

    def test_reclaim_disabled_when_vwap_reclaim_min_rvol_is_zero(self):
        """vwap_reclaim_min_rvol=0 disables the reclaim exception entirely."""
        strat = _momentum(vwap_reclaim_min_rvol=0)
        passed, reason = strat.apply_entry_gate(
            "buy",
            _signals(vwap_position="below", volume_ratio=5.0, macd_signal="bullish"),
        )
        assert passed is False
        assert "below VWAP" in reason

    def test_short_above_vwap_passes_regardless_of_macd(self):
        """Shorts are not VWAP-gated, so above-VWAP shorts pass the strategy gate
        regardless of MACD direction. The reclaim exception is long-only."""
        strat = _momentum()
        for macd in ("bearish", "bullish", "bullish_cross", "neutral"):
            passed, reason = strat.apply_entry_gate(
                "sell_short",
                _signals(vwap_position="above", volume_ratio=2.5, macd_signal=macd),
            )
            assert passed is True, f"expected pass with macd={macd!r}, got reason={reason!r}"

    def test_reclaim_threshold_configurable(self):
        """vwap_reclaim_min_rvol is tunable — higher threshold requires more volume."""
        strat = _momentum(vwap_reclaim_min_rvol=3.0)
        # rvol=2.5 would pass at default 1.8 but fails at 3.0
        passed, _ = strat.apply_entry_gate(
            "buy",
            _signals(vwap_position="below", volume_ratio=2.5, macd_signal="bullish"),
        )
        assert passed is False

    def test_rvol_floor_configurable(self):
        strat = _momentum(min_rvol_for_entry=2.0)
        # rvol=1.5 is now below the custom floor
        passed, reason = strat.apply_entry_gate("buy", _signals(volume_ratio=1.5, vwap_position="above"))
        assert passed is False
        assert "RVOL" in reason

    def test_missing_rvol_passes_gate(self):
        """Missing volume_ratio (None) should not block entry."""
        strat = _momentum()
        signals = _signals()
        del signals["volume_ratio"]
        passed, _ = strat.apply_entry_gate("buy", signals)
        assert passed is True


# ---------------------------------------------------------------------------
# 3. SwingStrategy.apply_entry_gate
# ---------------------------------------------------------------------------

class TestSwingEntryGate:

    def test_passes_long_with_bullish_trend(self):
        strat = _swing()
        passed, _ = strat.apply_entry_gate("buy", _signals(trend_structure="bullish_aligned"))
        assert passed is True

    def test_passes_short_with_bearish_trend(self):
        strat = _swing()
        passed, _ = strat.apply_entry_gate("sell_short", _signals(trend_structure="bearish_aligned"))
        assert passed is True

    def test_rejects_long_with_bearish_trend(self):
        strat = _swing()
        passed, reason = strat.apply_entry_gate("buy", _signals(trend_structure="bearish_aligned"))
        assert passed is False
        assert "bearish_aligned" in reason

    def test_rejects_short_with_bullish_trend(self):
        strat = _swing()
        passed, reason = strat.apply_entry_gate("sell_short", _signals(trend_structure="bullish_aligned"))
        assert passed is False
        assert "bullish_aligned" in reason

    def test_passes_long_with_mixed_trend(self):
        strat = _swing()
        passed, _ = strat.apply_entry_gate("buy", _signals(trend_structure="mixed"))
        assert passed is True

    def test_trend_gate_disabled_via_param(self):
        strat = _swing(block_bearish_trend=False)
        # Even with bearish_aligned for a long, gate is off → passes
        passed, _ = strat.apply_entry_gate("buy", _signals(trend_structure="bearish_aligned"))
        assert passed is True

    # RVOL gate disabled for swing (min_rvol_for_entry=0.0): intraday 5m RVOL
    # is not a meaningful filter for multi-day swing positions.
    def test_low_rvol_passes_swing(self):
        strat = _swing()
        passed, _ = strat.apply_entry_gate("buy", _signals(volume_ratio=0.5, trend_structure="bullish_aligned"))
        assert passed is True  # RVOL gate disabled

    def test_passes_sufficient_rvol(self):
        strat = _swing()
        passed, _ = strat.apply_entry_gate("buy", _signals(volume_ratio=0.9, trend_structure="mixed"))
        assert passed is True

    def test_missing_rvol_passes(self):
        """volume_ratio=None must not block — consistent with MomentumStrategy."""
        strat = _swing()
        signals = _signals(trend_structure="mixed")
        signals.pop("volume_ratio", None)
        passed, _ = strat.apply_entry_gate("buy", signals)
        assert passed is True


# ---------------------------------------------------------------------------
# 4. Registry-based construction (_build_strategies equivalent)
# ---------------------------------------------------------------------------

class TestRegistryDispatch:

    def test_get_strategy_momentum(self):
        strat = get_strategy("momentum")
        assert isinstance(strat, MomentumStrategy)

    def test_get_strategy_swing(self):
        strat = get_strategy("swing")
        assert isinstance(strat, SwingStrategy)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("scalp")

    def test_build_from_active_list_no_hardcoded_names(self):
        """Simulate _build_strategies() using only the registry — no if/elif."""
        active = ["momentum", "swing"]
        params = {"momentum": {"min_rvol_for_entry": 1.2}, "swing": {}}
        lookup = {name: get_strategy(name, params.get(name, {})) for name in active}
        assert set(lookup.keys()) == {"momentum", "swing"}
        assert isinstance(lookup["momentum"], MomentumStrategy)
        assert isinstance(lookup["swing"], SwingStrategy)

    def test_strategy_params_override_applied(self):
        """strategy_params dict overrides are forwarded to the strategy instance."""
        strat = get_strategy("momentum", {"min_rvol_for_entry": 2.5})
        assert strat._params["min_rvol_for_entry"] == 2.5

    def test_strategy_params_swing_override(self):
        strat = get_strategy("swing", {"target_atr_multiplier": 7.0})
        assert strat._params["target_atr_multiplier"] == 7.0
