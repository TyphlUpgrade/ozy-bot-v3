"""
strategies/swing_strategy.py
==============================
Medium-term swing strategy.  Buys dips in existing uptrends — looking for
stocks that have pulled back to support (lower Bollinger band or key EMA)
with signs of reversal.  Holds for days to weeks.

Entry philosophy: buy the dip when the long-term trend (50/200 EMA) is
intact and the stock is oversold.  Patience — wait for confirmation.
Exit philosophy: let winners run, but exit decisively if the trend breaks.
No forced end-of-day exit (swing trades hold overnight by design).
"""
from __future__ import annotations

import logging

import pandas as pd

from ozymandias.core.state_manager import Position
from ozymandias.strategies.base_strategy import (
    ExitSuggestion,
    PositionEval,
    Signal,
    Strategy,
)

log = logging.getLogger(__name__)

_FALLBACK_STOP_PCT = 0.07    # 7% below entry (wider than momentum)
_FALLBACK_TARGET_PCT = 0.15  # 15% above entry (swing trades aim further)

# Entry gate: maps action → the trend_structure value that disqualifies the entry.
# Longs avoid bearish downtrends; shorts avoid bullish uptrends.
# To support a new action type, add one entry here; gate logic is unchanged.
_SWING_WRONG_TREND: dict[str, str] = {
    "buy":        "bearish_aligned",   # longs avoid downtrends
    "sell_short": "bullish_aligned",   # shorts avoid uptrends
}


class SwingStrategy(Strategy):
    """
    Swing strategy — medium-term (days to weeks) dip-buying in uptrends.

    Entry requires ≥ ``min_signals_for_entry`` of 6 technical conditions:

    1. Price near support (lower Bollinger half or within ``support_proximity_pct``
       of a key EMA)
    2. RSI between ``rsi_entry_min`` and ``rsi_entry_max`` (oversold range)
    3. MACD not at peak bearishness (histogram improving or already bullish)
    4. Long-term trend intact: 50 and 200 EMAs bullishly aligned (or at
       minimum not bearishly aligned)
    5. Volume not indicating panic selling (``volume_ratio < panic_volume_ratio``)
    6. RSI is turning up: rsi[i] > rsi[i-2] (distinguishes bottoming from still-falling)
    """

    _DEFAULT_PARAMS = {
        "rsi_entry_min": 30,
        "rsi_entry_max": 50,
        "trend_ema_short": 50,
        "trend_ema_long": 200,
        "max_scale_in_count": 2,
        "scale_in_dip_pct": 3.0,       # only scale in if price drops ≥ 3% more
        "panic_volume_ratio": 1.5,      # above this → panic selling, skip
        "min_signals_for_entry": 5,     # raised from 4: 5/6 required for high-quality entries
        "profit_target_proximity_pct": 2.0,
        # Swing targets aim for multi-day moves — 5×ATR gives ~2.5:1 R:R, breakeven at ~30% WR.
        "target_atr_multiplier": 5.0,  # raised from 4.0: overnight gap risk warrants wider target
        # Volatility regime gate: swing trades tolerate quieter regimes than momentum
        # but still need some directional energy to avoid pure chop stop-outs.
        "min_vol_regime_ratio": 0.70,
        # RVOL gate disabled for swing (0.0 = no gate).
        # Swing holds for hours-to-days; intraday 5m RVOL is not a meaningful entry
        # filter for a multi-day thesis. Volume participation is already captured via
        # the volume_trend_bars signal in generate_signals(). Claude's per-trade
        # require_volume_ratio_min can add a volume gate on high-conviction setups.
        "min_rvol_for_entry": 0.0,
        # Entry gate: when True, reject entries where trend_structure is fully adverse.
        # Longs reject bearish_aligned; shorts reject bullish_aligned.
        "block_bearish_trend": True,
        # Minimum rsi_slope_5 required for swing long entries. A positive slope confirms
        # the RSI is turning up — distinguishing a genuine bottom from a still-falling RSI.
        # For shorts, the condition is inverted: slope must be <= -rsi_slope_min_for_entry.
        # Replaces the former 2-bar rsi_turning check with a 5-bar velocity measure.
        "rsi_slope_min_for_entry": 0.5,
        # Quant override exit thresholds (read by base_strategy.py accessors).
        # Wider than momentum: 3× ATR and 1.5 vol threshold prevent intraday noise from
        # exiting multi-day swing positions. Set in config.json strategy_params.swing.
        "override_atr_multiplier": 3.0,
        "override_vwap_volume_threshold": 1.5,
    }

    def applicable_override_signals(self) -> frozenset[str]:
        """
        Swing positions are held for days and managed by their explicit stop/target
        levels and Claude's slow-loop position review. Intraday momentum signals
        (ROC deceleration, VWAP crossover, momentum score flip) are noise on a
        multi-day timeframe and must not trigger exits.

        No override signals are active for swing by default. To add catastrophic
        loss protection via ATR trailing stop, add "atr_trailing_stop" here and
        set override_atr_multiplier in config.json strategy_params.swing.
        """
        return frozenset()

    @property
    def is_intraday(self) -> bool:
        return False

    @property
    def uses_market_orders(self) -> bool:
        return False

    @property
    def blocks_eod_entries(self) -> bool:
        return False

    def apply_entry_gate(self, action: str, signals: dict) -> tuple[bool, str]:
        """Reject swing entries when the long-term trend is fully adverse or RVOL is absent."""
        rvol = signals.get("volume_ratio")
        if rvol is not None and rvol < self._p("min_rvol_for_entry"):
            return (
                False,
                f"swing {action} rejected — RVOL {rvol:.2f} below floor "
                f"{self._p('min_rvol_for_entry'):.2f} (no volume participation)",
            )
        if self._p("block_bearish_trend"):
            wrong_trend = _SWING_WRONG_TREND.get(action)
            if wrong_trend and signals.get("trend_structure", "") == wrong_trend:
                return False, f"swing {action} rejected — {wrong_trend} trend"
        return True, ""

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
        Return a swing :class:`Signal` when ≥ ``min_signals_for_entry``
        conditions are met, otherwise return an empty list.
        """
        # Hard requirement: long-term trend must not be broken.
        # Swing trading is "buying the dip in an uptrend" — no uptrend means no entry.
        if indicators.get("trend_structure") == "bearish_aligned":
            return []

        # Hard gate: require minimum volatility regime energy.
        vol_regime = float(indicators.get("vol_regime_ratio", 1.0))
        if vol_regime < self._p("min_vol_regime_ratio"):
            return []

        # Hard RVOL gate: require minimum relative volume (current bar / 20-bar SMA).
        # Softer than momentum — swing entries tolerate quieter tape but still need
        # some participation to avoid getting trapped in illiquid dips.
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

        # Swing stops are wider — 2× ATR or fallback percentage
        stop = price - (2 * atr) if atr > 0 else price * (1 - _FALLBACK_STOP_PCT)
        # Swing targets aim for multi-day reward — configurable ATR multiplier or fallback
        target = (
            price + (self._p("target_atr_multiplier") * atr)
            if atr > 0
            else price * (1 + _FALLBACK_TARGET_PCT)
        )

        reasons = [cond for cond, met in conditions.items() if met]
        signal = Signal(
            symbol=symbol,
            direction="long",
            strength=round(min(strength, 1.0), 4),
            entry_price=round(price, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            timeframe="medium",
            reasoning=f"Swing conditions met ({n_met}/6): {', '.join(reasons)}",
        )
        log.debug(
            "Swing signal for %s: strength=%.2f, %d/6 conditions",
            symbol, signal.strength, n_met,
        )
        return [signal]

    def _evaluate_entry_conditions(
        self, indicators: dict
    ) -> tuple[dict[str, bool], dict[str, float]]:
        """
        Check each of the 6 swing entry conditions.

        Returns (conditions_met: dict, weights: dict).
        Weights do not sum to 1.0 — strength is capped at min(sum, 1.0).
        """
        rsi = float(indicators.get("rsi") or 50.0)
        bb_pos = indicators.get("bollinger_position", "middle")
        macd = indicators.get("macd_signal", "bearish_cross")
        trend = indicators.get("trend_structure", "mixed")
        vol_ratio = float(indicators.get("volume_ratio", 1.0))
        rsi_slope = float(indicators.get("rsi_slope_5", 0.0))
        slope_min = self._p("rsi_slope_min_for_entry")

        # RSI slope gate: confirms the bottom is forming, not still falling.
        # Longs require rising RSI (slope >= threshold); shorts require falling RSI.
        # 5-bar velocity is more robust than the former 2-bar rsi_turning check and
        # is available directly from _latest_indicators with no extra computation.
        rsi_slope_ok = rsi_slope >= slope_min

        conditions = {
            "near_support":        bb_pos == "lower_half",
            "rsi_oversold_range":  self._p("rsi_entry_min") <= rsi <= self._p("rsi_entry_max"),
            "macd_not_collapsing": macd != "bearish_cross",
            "longterm_trend_ok":   trend != "bearish_aligned",
            "no_panic_selling":    vol_ratio < self._p("panic_volume_ratio"),
            # RSI slope: highest single weight — a still-falling RSI is the core
            # false-bottom failure mode for swing trades. 5-bar slope confirms
            # the turn is real, not a single-bar dead-cat bounce.
            "rsi_slope_rising":    rsi_slope_ok,
        }
        weights = {
            "near_support":        0.20,
            "rsi_oversold_range":  0.20,
            "macd_not_collapsing": 0.15,
            "longterm_trend_ok":   0.20,
            "no_panic_selling":    0.10,
            "rsi_slope_rising":    0.20,
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
        Decide: HOLD, SCALE_IN (average down), SCALE_OUT (partial profit), or EXIT.

        Priority order:
          1. Stop-loss breach → EXIT
          2. Long-term trend structure broken → EXIT (bearish_aligned)
          3. Near profit target → SCALE_OUT
          4. Price dipped further but trend intact → SCALE_IN (if under limit)
          5. Default → HOLD
        """
        rsi = float(indicators.get("rsi") or 50.0)
        trend = indicators.get("trend_structure", "mixed")
        price = float(indicators.get("price") or market_data["close"].iloc[-1])
        vol_ratio = float(indicators.get("volume_ratio", 1.0))

        stop = position.intention.exit_targets.stop_loss
        target = position.intention.exit_targets.profit_target
        entry = position.avg_cost

        # 1. Stop-loss breach
        if stop > 0 and price <= stop:
            return PositionEval(
                symbol=position.symbol,
                action="exit",
                confidence=1.0,
                reasoning=f"Price {price:.2f} at/below stop {stop:.2f}",
            )

        # 2. Long-term trend breakdown (50 EMA crossed below 200 EMA)
        if trend == "bearish_aligned":
            return PositionEval(
                symbol=position.symbol,
                action="exit",
                confidence=0.90,
                reasoning="Long-term trend structure broken (bearish_aligned) — thesis invalidated",
            )

        # 3. Approaching profit target
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

        # 4. Price dipped further from entry — potential scale-in
        scale_in_count = getattr(position.intention, "scale_in_count", 0)
        if (
            entry > 0
            and price < entry
            and scale_in_count < self._p("max_scale_in_count")
        ):
            dip_pct = (entry - price) / entry * 100
            if dip_pct >= self._p("scale_in_dip_pct") and trend != "bearish_aligned":
                return PositionEval(
                    symbol=position.symbol,
                    action="scale_in",
                    confidence=0.60,
                    reasoning=(
                        f"Price dipped {dip_pct:.1f}% below entry — "
                        f"averaging down while trend intact ({trend})"
                    ),
                )

        # 5. Hold
        return PositionEval(
            symbol=position.symbol,
            action="hold",
            confidence=0.70,
            reasoning=(
                f"Swing thesis intact: trend={trend}, RSI={rsi:.1f}, "
                f"vol_ratio={vol_ratio:.1f}x"
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

        - Stop-loss hit              → market, urgency 1.0
        - Trend structure breakdown  → market, urgency 0.9
        - Profit target              → limit at target, urgency 0.3
        - Other exit                 → limit slightly below, urgency 0.6
        """
        eval_result = await self.evaluate_position(position, market_data, indicators)
        price = float(indicators.get("price") or market_data["close"].iloc[-1])
        stop = position.intention.exit_targets.stop_loss
        target = position.intention.exit_targets.profit_target
        trend = indicators.get("trend_structure", "mixed")

        # Stop-loss breach
        if stop > 0 and price <= stop:
            return ExitSuggestion(
                symbol=position.symbol,
                exit_price=0.0,
                order_type="market",
                urgency=1.0,
                reasoning="Stop-loss triggered — market exit",
            )

        # Trend structure breakdown
        if trend == "bearish_aligned":
            return ExitSuggestion(
                symbol=position.symbol,
                exit_price=0.0,
                order_type="market",
                urgency=0.9,
                reasoning="Trend structure breakdown — urgent market exit",
            )

        # Profit target
        if target > 0 and eval_result.action == "scale_out":
            return ExitSuggestion(
                symbol=position.symbol,
                exit_price=round(target, 4),
                order_type="limit",
                urgency=0.3,
                reasoning=f"Near profit target {target:.2f} — patient limit exit",
            )

        # Default exit
        limit_price = round(price * 0.998, 4)
        return ExitSuggestion(
            symbol=position.symbol,
            exit_price=limit_price,
            order_type="limit",
            urgency=0.6,
            reasoning=eval_result.reasoning,
        )
