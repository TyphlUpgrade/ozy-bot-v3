"""
tests/test_strategies.py
=========================
Unit tests for strategies/base_strategy.py, momentum_strategy.py, and
swing_strategy.py.

All tests use synthetic indicator dicts and Position objects — no real market
data or broker calls.
"""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np
from dataclasses import dataclass
from unittest.mock import patch

from ozymandias.strategies.base_strategy import (
    Signal,
    PositionEval,
    ExitSuggestion,
    Strategy,
    get_strategy,
)
from ozymandias.strategies.momentum_strategy import MomentumStrategy
from ozymandias.strategies.swing_strategy import SwingStrategy
from ozymandias.core.state_manager import Position, TradeIntention, ExitTargets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _df(n: int = 20, price: float = 100.0) -> pd.DataFrame:
    """Minimal OHLCV DataFrame."""
    return pd.DataFrame({
        "open":   [price] * n,
        "high":   [price * 1.01] * n,
        "low":    [price * 0.99] * n,
        "close":  [price] * n,
        "volume": [1_000_000] * n,
    })


def _position(
    symbol: str = "AAPL",
    avg_cost: float = 100.0,
    profit_target: float = 115.0,
    stop_loss: float = 92.0,
    strategy: str = "momentum",
) -> Position:
    intention = TradeIntention(
        strategy=strategy,
        exit_targets=ExitTargets(profit_target=profit_target, stop_loss=stop_loss),
    )
    return Position(
        symbol=symbol,
        shares=100,
        avg_cost=avg_cost,
        entry_date="2026-03-13",
        intention=intention,
    )


def _momentum_indicators(**overrides) -> dict:
    """Perfect momentum indicator set (all 6 conditions met)."""
    base = {
        "vwap_position":    "above",
        "rsi":              55.0,
        "macd_signal":      "bullish_cross",
        "volume_ratio":     1.5,
        "trend_structure":  "bullish_aligned",
        "rsi_divergence":   False,
        "roc_5":            2.0,
        "roc_deceleration": False,
        "atr_14":           2.0,
        "price":            105.0,
    }
    base.update(overrides)
    return base


def _swing_indicators(**overrides) -> dict:
    """Perfect swing indicator set (all 5 conditions met)."""
    base = {
        "bollinger_position": "lower_half",
        "rsi":               38.0,
        "macd_signal":       "bearish",     # not bearish_cross — improving
        "trend_structure":   "bullish_aligned",
        "volume_ratio":      1.1,           # no panic selling
        "atr_14":            2.5,
        "price":             97.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Strategy registry
# ---------------------------------------------------------------------------

class TestStrategyRegistry:

    def test_get_momentum(self):
        s = get_strategy("momentum")
        assert isinstance(s, MomentumStrategy)

    def test_get_swing(self):
        s = get_strategy("swing")
        assert isinstance(s, SwingStrategy)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("options")

    def test_params_forwarded(self):
        s = get_strategy("momentum", {"rsi_entry_max": 65})
        assert s.get_parameters()["rsi_entry_max"] == 65

    def test_get_parameters_returns_copy(self):
        s = MomentumStrategy()
        params = s.get_parameters()
        params["rsi_overbought"] = 999
        assert s.get_parameters()["rsi_overbought"] == 80  # original unchanged


# ---------------------------------------------------------------------------
# 2. MomentumStrategy — entry signals
# ---------------------------------------------------------------------------

class TestMomentumEntry:

    @pytest.mark.asyncio
    async def test_all_conditions_met_generates_signal(self):
        s = MomentumStrategy()
        signals = await s.generate_signals("AAPL", _df(), _momentum_indicators())
        assert len(signals) == 1
        sig = signals[0]
        assert sig.symbol == "AAPL"
        assert sig.direction == "long"
        assert sig.timeframe == "short"
        assert 0.0 < sig.strength <= 1.0
        assert sig.stop_price < sig.entry_price < sig.target_price

    @pytest.mark.asyncio
    async def test_below_vwap_reduces_signal(self):
        s = MomentumStrategy()
        # Remove above_vwap (1/6 gone → 5 remain, still >= 4)
        inds = _momentum_indicators(vwap_position="below")
        signals = await s.generate_signals("AAPL", _df(), inds)
        assert len(signals) == 1
        # Strength lower without above_vwap condition
        full_signals = await s.generate_signals("AAPL", _df(), _momentum_indicators())
        assert signals[0].strength < full_signals[0].strength

    @pytest.mark.asyncio
    async def test_three_conditions_no_signal(self):
        s = MomentumStrategy()
        inds = _momentum_indicators(
            vwap_position="below",
            rsi=75.0,       # outside 40-70
            macd_signal="bearish",
        )  # 3/6 met — below threshold
        signals = await s.generate_signals("AAPL", _df(), inds)
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_rsi_too_high_fails_condition(self):
        """RSI > 70 fails rsi_in_range condition."""
        s = MomentumStrategy()
        inds = _momentum_indicators(rsi=72.0)  # 5/6 still passes
        signals = await s.generate_signals("AAPL", _df(), inds)
        assert len(signals) == 1
        # But strength is lower
        full = await s.generate_signals("AAPL", _df(), _momentum_indicators())
        assert signals[0].strength < full[0].strength

    @pytest.mark.asyncio
    async def test_rsi_too_low_fails_condition(self):
        """RSI < 40 fails rsi_in_range condition (overbought concern removed, but momentum not in range)."""
        s = MomentumStrategy()
        inds = _momentum_indicators(rsi=35.0)  # 5/6 — still passes
        signals = await s.generate_signals("AAPL", _df(), inds)
        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_bearish_divergence_reduces_strength(self):
        """Bearish RSI divergence counts against signal strength (one of 6 conditions)."""
        s = MomentumStrategy()
        inds_clean = _momentum_indicators(rsi_divergence=False)
        inds_div = _momentum_indicators(rsi_divergence="bearish")
        sigs_clean = await s.generate_signals("AAPL", _df(), inds_clean)
        sigs_div = await s.generate_signals("AAPL", _df(), inds_div)
        # 5/6 still ≥ min_signals_for_entry (4) → signal still generated
        assert len(sigs_div) == 1
        # but strength is lower without the no_rsi_divergence condition
        assert sigs_div[0].strength < sigs_clean[0].strength

    @pytest.mark.asyncio
    async def test_entry_blocked_when_few_conditions_met(self):
        """bearish_divergence + below_vwap + bearish_macd → only 3/6 met."""
        s = MomentumStrategy()
        inds = _momentum_indicators(
            rsi_divergence="bearish",   # no_rsi_divergence fails
            vwap_position="below",      # above_vwap fails
            macd_signal="bearish",      # macd_bullish fails
        )  # 3/6 conditions — below min_signals_for_entry (4)
        signals = await s.generate_signals("AAPL", _df(), inds)
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_min_signals_parameter_respected(self):
        s = MomentumStrategy({"min_signals_for_entry": 6})
        # All 6 conditions → should pass
        signals = await s.generate_signals("AAPL", _df(), _momentum_indicators())
        assert len(signals) == 1
        # With one removed → should fail
        inds = _momentum_indicators(vwap_position="below")
        signals = await s.generate_signals("AAPL", _df(), inds)
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_set_parameters_changes_rsi_range(self):
        s = MomentumStrategy()
        s.set_parameters({"rsi_entry_max": 60})
        # RSI 65 now fails
        inds = _momentum_indicators(rsi=65.0)  # was valid, now outside range
        signals_restricted = await s.generate_signals("AAPL", _df(), inds)
        s.set_parameters({"rsi_entry_max": 70})
        signals_full = await s.generate_signals("AAPL", _df(), inds)
        # With max=60, RSI 65 fails that condition; strength should differ
        assert signals_restricted[0].strength < signals_full[0].strength

    @pytest.mark.asyncio
    async def test_signal_uses_atr_for_stop_and_target(self):
        """With ATR = 2.0, stop = price - 4, target = price + 6."""
        s = MomentumStrategy()
        inds = _momentum_indicators(price=100.0, atr_14=2.0)
        signals = await s.generate_signals("AAPL", _df(price=100.0), inds)
        assert len(signals) == 1
        assert signals[0].stop_price == pytest.approx(96.0)
        assert signals[0].target_price == pytest.approx(106.0)

    @pytest.mark.asyncio
    async def test_signal_fallback_stop_without_atr(self):
        """Without ATR, uses percentage-based stop/target."""
        s = MomentumStrategy()
        inds = _momentum_indicators(price=100.0, atr_14=0.0)
        signals = await s.generate_signals("AAPL", _df(price=100.0), inds)
        assert len(signals) == 1
        assert signals[0].stop_price == pytest.approx(95.0)    # 5% below
        assert signals[0].target_price == pytest.approx(110.0)  # 10% above


# ---------------------------------------------------------------------------
# 3. MomentumStrategy — position evaluation
# ---------------------------------------------------------------------------

class TestMomentumEval:

    @pytest.mark.asyncio
    async def test_hold_when_thesis_intact(self):
        s = MomentumStrategy()
        pos = _position(avg_cost=100.0, profit_target=115.0, stop_loss=92.0)
        inds = _momentum_indicators(price=105.0)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "hold"

    @pytest.mark.asyncio
    async def test_exit_on_stop_loss_breach(self):
        s = MomentumStrategy()
        pos = _position(stop_loss=92.0)
        inds = _momentum_indicators(price=91.0)  # below stop
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "exit"
        assert result.confidence == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_exit_on_vwap_breakdown_with_high_volume(self):
        s = MomentumStrategy()
        pos = _position()
        inds = _momentum_indicators(vwap_position="below", volume_ratio=1.5)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "exit"

    @pytest.mark.asyncio
    async def test_no_exit_vwap_below_low_volume(self):
        """Price below VWAP but low volume — hold."""
        s = MomentumStrategy()
        pos = _position()
        inds = _momentum_indicators(vwap_position="below", volume_ratio=1.0)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "hold"

    @pytest.mark.asyncio
    async def test_exit_rsi_overbought_with_deceleration(self):
        s = MomentumStrategy()
        pos = _position()
        inds = _momentum_indicators(rsi=82.0, roc_deceleration=True)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "exit"

    @pytest.mark.asyncio
    async def test_no_exit_rsi_overbought_without_deceleration(self):
        """RSI overbought alone doesn't trigger exit — needs deceleration too."""
        s = MomentumStrategy()
        pos = _position()
        inds = _momentum_indicators(rsi=82.0, roc_deceleration=False)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "hold"

    @pytest.mark.asyncio
    async def test_scale_out_near_profit_target(self):
        s = MomentumStrategy()
        pos = _position(profit_target=115.0)
        # Within 2% of 115 → 113 is 1.7% away
        inds = _momentum_indicators(price=113.0)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "scale_out"

    @pytest.mark.asyncio
    async def test_hold_far_from_profit_target(self):
        s = MomentumStrategy()
        pos = _position(profit_target=115.0)
        inds = _momentum_indicators(price=105.0)  # ~8.7% from target
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "hold"

    @pytest.mark.asyncio
    async def test_exit_last_five_minutes(self):
        s = MomentumStrategy()
        pos = _position()
        inds = _momentum_indicators(price=105.0)
        with patch("ozymandias.strategies.momentum_strategy.is_last_five_minutes", return_value=True):
            result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "exit"
        assert "Last 5 minutes" in result.reasoning


# ---------------------------------------------------------------------------
# 4. MomentumStrategy — exit suggestions
# ---------------------------------------------------------------------------

class TestMomentumExit:

    @pytest.mark.asyncio
    async def test_stop_loss_is_market_urgency_1(self):
        s = MomentumStrategy()
        pos = _position(stop_loss=92.0)
        inds = _momentum_indicators(price=91.0)
        suggestion = await s.suggest_exit(pos, _df(), inds)
        assert suggestion.order_type == "market"
        assert suggestion.urgency == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_profit_target_is_limit_low_urgency(self):
        s = MomentumStrategy()
        pos = _position(profit_target=115.0)
        inds = _momentum_indicators(price=113.5)
        suggestion = await s.suggest_exit(pos, _df(), inds)
        assert suggestion.order_type == "limit"
        assert suggestion.urgency == pytest.approx(0.3)
        assert suggestion.exit_price == pytest.approx(115.0)

    @pytest.mark.asyncio
    async def test_end_of_day_exit_is_market_urgency_0_8(self):
        s = MomentumStrategy()
        pos = _position()
        inds = _momentum_indicators(price=105.0)
        with patch("ozymandias.strategies.momentum_strategy.is_last_five_minutes", return_value=True):
            suggestion = await s.suggest_exit(pos, _df(), inds)
        assert suggestion.order_type == "market"
        assert suggestion.urgency == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_vwap_breakdown_is_limit_urgency_0_7(self):
        s = MomentumStrategy()
        pos = _position(stop_loss=80.0)  # stop far away
        inds = _momentum_indicators(vwap_position="below", volume_ratio=1.5, price=100.0)
        with patch("ozymandias.strategies.momentum_strategy.is_last_five_minutes", return_value=False):
            suggestion = await s.suggest_exit(pos, _df(), inds)
        assert suggestion.order_type == "limit"
        assert suggestion.urgency == pytest.approx(0.7)
        # limit slightly below current price
        assert suggestion.exit_price < 100.0


# ---------------------------------------------------------------------------
# 5. SwingStrategy — entry signals
# ---------------------------------------------------------------------------

class TestSwingEntry:

    @pytest.mark.asyncio
    async def test_all_conditions_generates_signal(self):
        s = SwingStrategy()
        signals = await s.generate_signals("TSLA", _df(), _swing_indicators())
        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == "long"
        assert sig.timeframe == "medium"
        assert 0.0 < sig.strength <= 1.0

    @pytest.mark.asyncio
    async def test_broken_trend_no_signal(self):
        """Bearish trend structure disqualifies swing entry."""
        s = SwingStrategy()
        inds = _swing_indicators(trend_structure="bearish_aligned")
        signals = await s.generate_signals("TSLA", _df(), inds)
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_rsi_too_high_no_signal(self):
        """RSI > 50 + not near support → only 3/5 conditions → no signal."""
        s = SwingStrategy()
        inds = _swing_indicators(
            rsi=55.0,                      # fails rsi_oversold_range
            bollinger_position="middle",   # fails near_support
        )  # 3/5 met — below min_signals_for_entry (4)
        signals = await s.generate_signals("TSLA", _df(), inds)
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_panic_selling_no_signal(self):
        """Panic selling + MACD collapsing → only 3/5 conditions → no signal."""
        s = SwingStrategy()
        inds = _swing_indicators(
            volume_ratio=2.0,              # fails no_panic_selling
            macd_signal="bearish_cross",   # fails macd_not_collapsing
        )  # 3/5 met — below min_signals_for_entry (4)
        signals = await s.generate_signals("TSLA", _df(), inds)
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_oversold_dip_in_uptrend_generates_signal(self):
        """Classic swing setup: lower Bollinger, RSI 35, bullish trend."""
        s = SwingStrategy()
        inds = _swing_indicators(
            rsi=35.0,
            bollinger_position="lower_half",
            trend_structure="bullish_aligned",
            volume_ratio=1.0,
            macd_signal="bearish",
        )
        signals = await s.generate_signals("TSLA", _df(), inds)
        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_set_parameters_changes_rsi_range(self):
        s = SwingStrategy({"rsi_entry_min": 25})
        # RSI 28 (previously too low) should now pass
        inds = _swing_indicators(rsi=28.0)
        signals = await s.generate_signals("TSLA", _df(), inds)
        assert len(signals) == 1

    @pytest.mark.asyncio
    async def test_signal_atr_based_targets(self):
        """Stop = price - 2×ATR, target = price + 5×ATR (target_atr_multiplier default 5.0)."""
        s = SwingStrategy()
        inds = _swing_indicators(price=100.0, atr_14=3.0)
        signals = await s.generate_signals("TSLA", _df(price=100.0), inds)
        assert len(signals) == 1
        assert signals[0].stop_price == pytest.approx(94.0)   # 100 - 2*3
        assert signals[0].target_price == pytest.approx(115.0) # 100 + 5*3


# ---------------------------------------------------------------------------
# 6. SwingStrategy — position evaluation
# ---------------------------------------------------------------------------

class TestSwingEval:

    @pytest.mark.asyncio
    async def test_hold_when_thesis_intact(self):
        s = SwingStrategy()
        pos = _position(avg_cost=95.0, profit_target=115.0, stop_loss=88.0, strategy="swing")
        inds = _swing_indicators(price=98.0)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "hold"

    @pytest.mark.asyncio
    async def test_exit_on_stop_loss_breach(self):
        s = SwingStrategy()
        pos = _position(stop_loss=88.0, strategy="swing")
        inds = _swing_indicators(price=87.0)
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "exit"
        assert result.confidence == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_exit_on_trend_structure_breakdown(self):
        s = SwingStrategy()
        pos = _position(strategy="swing")
        inds = _swing_indicators(price=98.0, trend_structure="bearish_aligned")
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "exit"
        assert "bearish_aligned" in result.reasoning

    @pytest.mark.asyncio
    async def test_scale_out_near_profit_target(self):
        s = SwingStrategy()
        pos = _position(avg_cost=90.0, profit_target=115.0, stop_loss=82.0, strategy="swing")
        inds = _swing_indicators(price=113.5)  # within 2% of 115
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "scale_out"

    @pytest.mark.asyncio
    async def test_scale_in_on_further_dip(self):
        """Price dipped 4% below entry with trend intact → scale_in."""
        s = SwingStrategy()
        pos = _position(avg_cost=100.0, profit_target=120.0, stop_loss=88.0, strategy="swing")
        inds = _swing_indicators(price=96.0, trend_structure="mixed")  # 4% dip
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "scale_in"

    @pytest.mark.asyncio
    async def test_no_scale_in_on_small_dip(self):
        """Price dipped only 1% — below the 3% threshold."""
        s = SwingStrategy()
        pos = _position(avg_cost=100.0, profit_target=120.0, stop_loss=88.0, strategy="swing")
        inds = _swing_indicators(price=99.0, trend_structure="mixed")  # 1% dip
        result = await s.evaluate_position(pos, _df(), inds)
        assert result.action == "hold"

    @pytest.mark.asyncio
    async def test_no_scale_in_when_trend_bearish(self):
        """Do not average down into a broken trend."""
        s = SwingStrategy()
        pos = _position(avg_cost=100.0, profit_target=120.0, stop_loss=88.0, strategy="swing")
        inds = _swing_indicators(price=94.0, trend_structure="bearish_aligned")
        result = await s.evaluate_position(pos, _df(), inds)
        # trend breakdown takes priority → exit, not scale_in
        assert result.action == "exit"

    @pytest.mark.asyncio
    async def test_swing_no_end_of_day_forced_exit(self):
        """Swing positions do NOT get forced out at end of day."""
        s = SwingStrategy()
        pos = _position(strategy="swing")
        inds = _swing_indicators(price=98.0)
        with patch("ozymandias.strategies.momentum_strategy.is_last_five_minutes", return_value=True):
            result = await s.evaluate_position(pos, _df(), inds)
        # Swing strategy doesn't import or call is_last_five_minutes — should hold
        assert result.action == "hold"


# ---------------------------------------------------------------------------
# 7. SwingStrategy — exit suggestions
# ---------------------------------------------------------------------------

class TestSwingExit:

    @pytest.mark.asyncio
    async def test_stop_loss_is_market_urgency_1(self):
        s = SwingStrategy()
        pos = _position(stop_loss=88.0, strategy="swing")
        inds = _swing_indicators(price=87.0)
        suggestion = await s.suggest_exit(pos, _df(), inds)
        assert suggestion.order_type == "market"
        assert suggestion.urgency == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_trend_breakdown_is_market_urgency_0_9(self):
        s = SwingStrategy()
        pos = _position(stop_loss=80.0, strategy="swing")
        inds = _swing_indicators(price=95.0, trend_structure="bearish_aligned")
        suggestion = await s.suggest_exit(pos, _df(), inds)
        assert suggestion.order_type == "market"
        assert suggestion.urgency == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_profit_target_is_limit_low_urgency(self):
        s = SwingStrategy()
        pos = _position(avg_cost=90.0, profit_target=115.0, stop_loss=82.0, strategy="swing")
        inds = _swing_indicators(price=113.5)
        suggestion = await s.suggest_exit(pos, _df(), inds)
        assert suggestion.order_type == "limit"
        assert suggestion.urgency == pytest.approx(0.3)
        assert suggestion.exit_price == pytest.approx(115.0)
