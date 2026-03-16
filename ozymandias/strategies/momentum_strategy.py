"""
strategies/momentum_strategy.py
================================
Short-term momentum strategy.  Targets stocks with strong directional moves
confirmed by technical indicators.  Holds for hours to a few days.

Entry philosophy: price breaking out above VWAP with rising volume, RSI with
room to run, and MACD confirming bullish direction.
Exit philosophy: exit on momentum exhaustion — never let a winner become a
loser.  Hard stop on VWAP breakdown; forced exit before end-of-day if no
swing hold thesis.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from ozymandias.core.market_hours import is_last_five_minutes
from ozymandias.core.state_manager import Position
from ozymandias.strategies.base_strategy import (
    ExitSuggestion,
    PositionEval,
    Signal,
    Strategy,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fraction of the current price used to size the ATR-based stop and target
# when exact ATR is unavailable.
_FALLBACK_STOP_PCT = 0.05   # 5% below entry
_FALLBACK_TARGET_PCT = 0.10  # 10% above entry


class MomentumStrategy(Strategy):
    """
    Momentum strategy — short-term (days) breakout plays.

    Entry requires ≥ ``min_signals_for_entry`` of 6 technical conditions:

    1. Price above VWAP
    2. RSI 45–65 (mid-range with room to run; not weak trend or late chase)
    3. MACD bullish or bullish crossover
    4. Volume ratio ≥ ``min_volume_ratio``
    5. Trend structure bullishly aligned (9 + 20 EMAs at minimum)
    6. No bearish RSI divergence
    """

    _DEFAULT_PARAMS = {
        "min_volume_ratio": 1.2,
        "rsi_entry_min": 45,   # tightened from 40: RSI 40–45 = weakening trend, not momentum
        "rsi_entry_max": 65,   # tightened from 70: RSI >65 approaches overbought late-chase territory
        "rsi_overbought": 80,
        "min_signals_for_entry": 4,
        "partial_profit_pct": 0.5,      # fraction to exit at profit target
        "profit_target_proximity_pct": 2.0,  # % from target to trigger scale-out
        # Volatility regime gate: block entries when short-term vol / long-term vol
        # falls below this ratio (choppy / low-energy market with no directional thrust).
        "min_vol_regime_ratio": 0.85,  # raised from 0.75: 0.76 is still borderline choppy
        # When true, bearish EMA alignment (9/20/50/200 all downtrending) is an absolute
        # entry block. When false, bearish_aligned is a heavy negative signal but not a veto —
        # allows Claude high-conviction catalyst-driven breakout entries.
        "block_bearish_aligned": True,
        # Hard RVOL gate: current bar volume / 20-bar SMA must meet this floor before
        # any entry is considered. Prevents momentum entries when nobody is trading.
        # Distinct from the soft high_volume condition (rewarded at 1.2+, weight 0.15).
        "min_rvol_for_entry": 1.0,
    }

    # ------------------------------------------------------------------
    # Entry signals
    # ------------------------------------------------------------------

    async def generate_signals(
        self,
        symbol: str,
        market_data: pd.DataFrame,
        indicators: dict,
    ) -> list[Signal]:
        """
        Return a momentum :class:`Signal` when ≥ ``min_signals_for_entry``
        conditions are met, otherwise return an empty list.
        """
        # Hard gate: require a trending/directional regime before counting conditions.
        # When short-term vol is well below long-term vol the market is choppy —
        # momentum entries in that regime get stopped out by noise.
        vol_regime = float(indicators.get("vol_regime_ratio", 1.0))
        if vol_regime < self._p("min_vol_regime_ratio"):
            return []

        # Configurable hard gate: block entries when all EMAs are in full downtrend.
        # Off by default allows Claude high-conviction catalyst entries in lagging trends.
        if self._p("block_bearish_aligned"):
            if indicators.get("trend_structure") == "bearish_aligned":
                return []

        # Hard RVOL gate: require minimum relative volume (current bar / 20-bar SMA).
        # Blocks momentum entries when volume is absent — signal quality collapses in
        # low-participation moves. Separate from the soft high_volume condition (1.2+).
        rvol = float(indicators.get("volume_ratio", 1.0))
        if rvol < self._p("min_rvol_for_entry"):
            return []

        conditions, weights = self._evaluate_entry_conditions(indicators)
        n_met = sum(1 for v in conditions.values() if v)

        if n_met < self._p("min_signals_for_entry"):
            return []

        strength = sum(w for cond, w in weights.items() if conditions[cond])
        price = float(indicators.get("price") or market_data["close"].iloc[-1])
        atr = float(indicators.get("atr_14") or 0.0)

        # Tighter stop for marginal entries (exactly min_signals met): 1.5×ATR.
        # Full stop (2×ATR) only when conviction is high (all 6 signals fired).
        # Scales stop to trade quality — less room given to weaker setups.
        multiplier = 1.5 if n_met == self._p("min_signals_for_entry") else 2.0
        stop = price - (multiplier * atr) if atr > 0 else price * (1 - _FALLBACK_STOP_PCT)
        target = price + (3 * atr) if atr > 0 else price * (1 + _FALLBACK_TARGET_PCT)

        reasons = [cond for cond, met in conditions.items() if met]
        signal = Signal(
            symbol=symbol,
            direction="long",
            strength=round(min(strength, 1.0), 4),
            entry_price=round(price, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            timeframe="short",
            reasoning=f"Momentum conditions met ({n_met}/6): {', '.join(reasons)}",
        )
        log.debug(
            "Momentum signal for %s: strength=%.2f, %d/6 conditions",
            symbol, signal.strength, n_met,
        )
        return [signal]

    def _evaluate_entry_conditions(
        self, indicators: dict
    ) -> tuple[dict[str, bool], dict[str, float]]:
        """
        Check each of the 6 entry conditions.

        Returns (conditions_met: dict, weights: dict) where weights sum to 1.0.
        """
        rsi = float(indicators.get("rsi") or 50.0)
        vwap_pos = indicators.get("vwap_position", "at")
        macd = indicators.get("macd_signal", "bearish")
        vol_ratio = float(indicators.get("volume_ratio") or 1.0)
        trend = indicators.get("trend_structure", "mixed")
        rsi_div = indicators.get("rsi_divergence", False)

        conditions = {
            "above_vwap":      vwap_pos == "above",
            "rsi_in_range":    self._p("rsi_entry_min") <= rsi <= self._p("rsi_entry_max"),
            "macd_bullish":    macd in ("bullish", "bullish_cross"),
            "high_volume":     vol_ratio >= self._p("min_volume_ratio"),
            "trend_aligned":   trend == "bullish_aligned",
            "no_rsi_divergence": rsi_div != "bearish",
        }
        weights = {
            "above_vwap":      0.20,
            "rsi_in_range":    0.20,
            "macd_bullish":    0.20,
            "high_volume":     0.15,
            "trend_aligned":   0.15,
            "no_rsi_divergence": 0.10,
        }
        return conditions, weights

    # ------------------------------------------------------------------
    # Position evaluation
    # ------------------------------------------------------------------

    async def evaluate_position(
        self,
        position: Position,
        market_data: pd.DataFrame,
        indicators: dict,
    ) -> PositionEval:
        """
        Decide: HOLD, SCALE_OUT (partial profit), or EXIT.

        Priority order:
          1. Stop-loss breach → EXIT (highest priority)
          2. RSI extremely overbought → EXIT
          3. VWAP breakdown on volume → EXIT
          4. Near profit target → SCALE_OUT
          5. Last 5 minutes with no hold thesis → EXIT
          6. Default → HOLD
        """
        rsi = float(indicators.get("rsi") or 50.0)
        vwap_pos = indicators.get("vwap_position", "at")
        vol_ratio = float(indicators.get("volume_ratio") or 1.0)
        price = float(indicators.get("price") or market_data["close"].iloc[-1])
        roc_decel = bool(indicators.get("roc_deceleration", False))

        stop = position.intention.exit_targets.stop_loss
        target = position.intention.exit_targets.profit_target

        # 1. Stop-loss breach
        if stop > 0 and price <= stop:
            return PositionEval(
                symbol=position.symbol,
                action="exit",
                confidence=1.0,
                reasoning=f"Price {price:.2f} at/below stop {stop:.2f}",
            )

        # 2. Extremely overbought + momentum fading
        if rsi > self._p("rsi_overbought") and roc_decel:
            return PositionEval(
                symbol=position.symbol,
                action="exit",
                confidence=0.85,
                reasoning=(
                    f"RSI extremely overbought ({rsi:.1f} > {self._p('rsi_overbought')}) "
                    f"and momentum decelerating"
                ),
            )

        # 3. VWAP breakdown on elevated volume
        if vwap_pos == "below" and vol_ratio > 1.3:
            return PositionEval(
                symbol=position.symbol,
                action="exit",
                confidence=0.80,
                reasoning=(
                    f"Price broke below VWAP on elevated volume (ratio={vol_ratio:.1f}x)"
                ),
            )

        # 4. Approaching profit target → partial exit
        if target > 0:
            proximity_pct = abs(price - target) / target * 100
            if proximity_pct <= self._p("profit_target_proximity_pct"):
                return PositionEval(
                    symbol=position.symbol,
                    action="scale_out",
                    confidence=0.75,
                    reasoning=(
                        f"Price {price:.2f} within {proximity_pct:.1f}% of target {target:.2f}"
                    ),
                )

        # 5. Last 5 minutes of day — exit momentum positions
        if is_last_five_minutes():
            return PositionEval(
                symbol=position.symbol,
                action="exit",
                confidence=0.80,
                reasoning="Last 5 minutes of session — closing momentum position",
            )

        # 6. Hold
        return PositionEval(
            symbol=position.symbol,
            action="hold",
            confidence=0.70,
            reasoning=(
                f"Momentum thesis intact: VWAP {vwap_pos}, RSI {rsi:.1f}, "
                f"vol_ratio {vol_ratio:.1f}x"
            ),
        )

    # ------------------------------------------------------------------
    # Exit suggestion
    # ------------------------------------------------------------------

    async def suggest_exit(
        self,
        position: Position,
        market_data: pd.DataFrame,
        indicators: dict,
    ) -> ExitSuggestion:
        """
        Translate the evaluation into a specific order.

        - Stop-loss hit    → market, urgency 1.0
        - Overbought/VWAP  → limit slightly below current, urgency 0.7
        - Profit target    → limit at target, urgency 0.3
        - End of day       → market, urgency 0.8
        """
        eval_result = await self.evaluate_position(position, market_data, indicators)
        price = float(indicators.get("price") or market_data["close"].iloc[-1])
        stop = position.intention.exit_targets.stop_loss
        target = position.intention.exit_targets.profit_target

        action = eval_result.action

        # Stop-loss breach
        if stop > 0 and price <= stop:
            return ExitSuggestion(
                symbol=position.symbol,
                exit_price=0.0,
                order_type="market",
                urgency=1.0,
                reasoning="Stop-loss triggered — market exit",
            )

        # Profit target proximity
        if target > 0 and action == "scale_out":
            return ExitSuggestion(
                symbol=position.symbol,
                exit_price=round(target, 4),
                order_type="limit",
                urgency=0.3,
                reasoning=f"Near profit target {target:.2f} — patient limit exit",
            )

        # End of day
        if is_last_five_minutes():
            return ExitSuggestion(
                symbol=position.symbol,
                exit_price=0.0,
                order_type="market",
                urgency=0.8,
                reasoning="End-of-day close for momentum position",
            )

        # Overbought / VWAP breakdown
        limit_price = round(price * 0.998, 4)  # slightly below current bid
        return ExitSuggestion(
            symbol=position.symbol,
            exit_price=limit_price,
            order_type="limit",
            urgency=0.7,
            reasoning=eval_result.reasoning,
        )
