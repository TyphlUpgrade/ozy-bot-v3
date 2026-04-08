"""
QuantOverrides — evaluates quantitative override signals and places emergency exits.

Extracted from orchestrator.py to enable independent testing and parallel
development. The orchestrator delegates to this module via thin wrappers.

Uses mutable shared references (Python dicts passed by reference) for
state that the orchestrator's other loops also read/write. This is safe
because all loops run in a single asyncio event loop (no concurrent mutation).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ozymandias.core.config import Config
from ozymandias.core.direction import EXIT_SIDE, is_short
from ozymandias.core.state_manager import OrderRecord
from ozymandias.execution.broker_interface import BrokerInterface, Order
from ozymandias.execution.fill_protection import FillProtectionManager
from ozymandias.execution.risk_manager import RiskManager
from ozymandias.strategies.base_strategy import Strategy

log = logging.getLogger(__name__)


class QuantOverrides:
    """Evaluates quantitative override signals and places emergency exits.

    Owns no state of its own — operates on mutable references passed by the
    orchestrator at construction time.
    """

    def __init__(
        self,
        config: Config,
        broker: BrokerInterface,
        state_manager,
        fill_protection: FillProtectionManager,
        risk_manager: RiskManager,
        strategies: list[Strategy],
        # Mutable shared state
        pending_exit_hints: dict,
        position_entry_times: dict,
        intraday_highs: dict,
        intraday_lows: dict,
        override_closed: dict,
    ) -> None:
        self._config = config
        self._broker = broker
        self._state_manager = state_manager
        self._fill_protection = fill_protection
        self._risk_manager = risk_manager
        self._strategies = strategies
        self._pending_exit_hints = pending_exit_hints
        self._position_entry_times = position_entry_times
        self._intraday_highs = intraday_highs
        self._intraday_lows = intraday_lows
        self._override_closed = override_closed

    async def place_override_exit(self, position, exit_hint: str) -> bool:
        """Place a market exit order for a position flagged by a quant override or hard stop.

        Handles fill-protection check, order construction, record keeping,
        pending exit hint tagging, and override cooldown recording.
        Returns True if an order was placed, False otherwise.
        """
        symbol = position.symbol

        if not self._fill_protection.can_place_order(symbol):
            log.warning(
                "Override exit for %s blocked — pending order already exists (hint=%s)",
                symbol, exit_hint,
            )
            return False

        exit_side = EXIT_SIDE[position.intention.direction]
        exit_order = Order(
            symbol=symbol,
            side=exit_side,
            quantity=position.shares,
            order_type="market",
            time_in_force="day",
        )
        try:
            result = await self._broker.place_order(exit_order)
            now_iso = datetime.now(timezone.utc).isoformat()
            order_record = OrderRecord(
                order_id=result.order_id,
                symbol=symbol,
                side=exit_side,
                quantity=position.shares,
                order_type="market",
                limit_price=None,
                status="PENDING",
                created_at=now_iso,
                last_checked_at=now_iso,
            )
            await self._fill_protection.record_order(order_record)
            log.info(
                "Override exit order placed — %s  order_id=%s  qty=%.2f  hint=%s",
                symbol, result.order_id, position.shares, exit_hint,
            )
            # Tag exit reason for trade journal (consumed in _dispatch_confirmed_fill)
            self._pending_exit_hints[symbol] = exit_hint
            # Record override-close timestamp for the extended re-entry cooldown.
            # override_exit_cooldown_min >> re_entry_cooldown_min because the quant
            # signal broke down — re-entry should not be allowed until momentum resets.
            self._override_closed[symbol] = time.monotonic()
            return True
        except Exception as exc:
            log.error("Failed to place override exit for %s (hint=%s): %s", symbol, exit_hint, exc)
            return False

    async def step(self, latest_indicators: dict) -> int:
        """Run quant override checks for all open positions.

        Returns the number of override exits placed (for the orchestrator
        to add to its _override_exit_count).

        latest_indicators is passed at call time (not stored) because tests
        commonly reassign the orchestrator's dict, which would break a stored
        reference.

        Both long and short positions are handled in a single unified loop.
        Signal semantics are direction-aware: the same signal names invert their
        indicator logic for shorts (e.g. vwap_crossover fires on price-above-VWAP
        for shorts, price-below-VWAP for longs).

        Exit priority:
          1. Hard stop (short-only): price >= stop_loss. Fires before min-hold guard
             and before allow_signals gating — the hard stop is unconditional.
          2. Signal-triggered exit: evaluated through evaluate_overrides() with
             per-strategy allow_signals and threshold kwargs.

        ``intraday_extremum`` is the session HIGH for longs and session LOW for
        shorts, used by the ATR trailing stop signal.
        """
        portfolio = await self._state_manager.load_portfolio()
        if not portfolio.positions:
            return 0

        exits_placed = 0

        for position in portfolio.positions:
            symbol = position.symbol
            direction = position.intention.direction

            # We need current indicators for all paths.
            indicators = latest_indicators.get(symbol)
            if indicators is None:
                log.debug("No indicators cached for %s — skipping override check", symbol)
                continue

            current_price = indicators.get("price")
            if current_price is None:
                continue

            # Hard stop: short-only, fires before min-hold guard and allow_signals gate.
            if self._risk_manager.check_hard_stop(position, indicators):
                placed = await self.place_override_exit(position, "hard_stop")
                if placed:
                    exits_placed += 1
                continue

            # Resolve strategy for this position.
            target_name = position.intention.strategy
            matching = [
                s for s in self._strategies
                if type(s).__name__.replace("Strategy", "").lower() == target_name
            ]
            strategy_for_position = (matching or self._strategies)[0] if (matching or self._strategies) else None
            allow_signals = (
                strategy_for_position.applicable_override_signals()
                if strategy_for_position is not None else None
            )

            # If the strategy declares no applicable signals, skip signal evaluation.
            if allow_signals is not None and not allow_signals:
                log.debug(
                    "Override check skipped for %s — %s strategy has no applicable override signals",
                    symbol, target_name,
                )
                continue

            # Enforce minimum hold time before overrides can fire.
            # This prevents stale indicators (computed before entry) from triggering
            # an immediate exit on the same fast loop tick that registered the fill.
            min_hold_sec = self._config.scheduler.min_hold_before_override_min * 60
            entry_ts = self._position_entry_times.get(symbol)
            held_sec = (time.monotonic() - entry_ts) if entry_ts is not None else 0.0
            if held_sec < min_hold_sec:
                log.debug(
                    "Override check skipped for %s — %s",
                    symbol,
                    f"held {held_sec:.0f}s < min {min_hold_sec:.0f}s"
                    if entry_ts is not None
                    else "no entry time recorded (reconciled/adopted position)",
                )
                continue

            # Track intraday extremum: session HIGH for longs, session LOW for shorts.
            if is_short(direction):
                prev = self._intraday_lows.get(symbol, current_price)
                self._intraday_lows[symbol] = min(prev, current_price)
                intraday_extremum = self._intraday_lows[symbol]
            else:
                prev_high = self._intraday_highs.get(symbol, 0.0)
                self._intraday_highs[symbol] = max(prev_high, current_price)
                intraday_extremum = self._intraday_highs[symbol]

            # Per-strategy override thresholds.
            atr_multiplier = strategy_for_position.override_atr_multiplier() if strategy_for_position else 2.0
            vwap_threshold = strategy_for_position.override_vwap_volume_threshold() if strategy_for_position else 1.3

            should_exit, triggered_signals = self._risk_manager.evaluate_overrides(
                position, indicators, intraday_extremum,
                allow_signals=allow_signals,
                direction=direction,
                atr_multiplier=atr_multiplier,
                vwap_volume_threshold=vwap_threshold,
            )

            if not should_exit:
                continue

            log.warning(
                "QUANT OVERRIDE EXIT — %s  signals=%s  direction=%s  price=%.4f  "
                "atr=%.4f  intraday_extremum=%.4f  vwap_pos=%s  vol_ratio=%.2f",
                symbol, triggered_signals, direction, current_price,
                float(indicators.get("atr_14") or 0.0),
                intraday_extremum,
                indicators.get("vwap_position", "unknown"),
                float(indicators.get("volume_ratio") or 0.0),
            )
            placed = await self.place_override_exit(position, "quant_override")
            if placed:
                exits_placed += 1

        return exits_placed
