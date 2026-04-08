"""
PositionManager — evaluates open positions and applies Claude's position reviews.

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
from typing import Callable

from ozymandias.core.config import Config
from ozymandias.core.direction import EXIT_SIDE, is_short
from ozymandias.core.market_hours import is_last_five_minutes
from ozymandias.core.state_manager import OrderRecord
from ozymandias.core.trade_journal import TradeJournal
from ozymandias.execution.broker_interface import Order
from ozymandias.execution.fill_protection import FillProtectionManager
from ozymandias.strategies.base_strategy import Strategy

log = logging.getLogger(__name__)


class PositionManager:
    """Evaluates open positions and applies Claude's position reviews.

    Two entry points:
    - evaluate_positions(): medium loop — runs strategy.evaluate_position() on
      each open position and places exit orders when recommended.
    - apply_position_reviews(): slow loop — applies Claude's adjusted targets,
      review notes, and exit recommendations.

    Owns no state of its own — operates on mutable references passed by the
    orchestrator at construction time.
    """

    def __init__(
        self,
        config: Config,
        state_manager,
        fill_protection: FillProtectionManager,
        trade_journal: TradeJournal,
        strategies: list[Strategy],
        # Mutable shared state
        pending_exit_hints: dict,
        entry_contexts: dict,
        recently_closed: dict,
        last_position_review_utc: dict,
        # Callbacks for degradation state (stay on orchestrator)
        on_broker_failure: Callable[[Exception], None],
        on_broker_available: Callable[[], None],
    ) -> None:
        self._config = config
        self._state_manager = state_manager
        self._fill_protection = fill_protection
        self._trade_journal = trade_journal
        self._strategies = strategies
        self._pending_exit_hints = pending_exit_hints
        self._entry_contexts = entry_contexts
        self._recently_closed = recently_closed
        self._last_position_review_utc = last_position_review_utc
        self._on_broker_failure = on_broker_failure
        self._on_broker_available = on_broker_available

    async def evaluate_positions(
        self, portfolio, bars, indicators, acct, orders, *, broker
    ) -> None:
        """Run evaluate_position() on each open position; exit if recommended.

        broker is passed at call time (not stored) because tests commonly
        reassign the orchestrator's broker after startup.
        """
        for position in portfolio.positions:
            symbol = position.symbol
            df = bars.get(symbol)
            ind_summary = indicators.get(symbol)
            if df is None or ind_summary is None:
                log.debug("Medium loop: no data for position %s — skipping eval", symbol)
                continue

            sigs_flat = ind_summary["signals"]

            # Route to the strategy that opened this position so a swing position is
            # not evaluated by MomentumStrategy (and vice versa). Fall back to the
            # first active strategy if the original strategy has since been disabled.
            target_name = position.intention.strategy
            matching = [
                s for s in self._strategies
                if type(s).__name__.replace("Strategy", "").lower() == target_name
            ]
            eval_strategies = matching if matching else self._strategies[:1]

            for strategy in eval_strategies:
                try:
                    eval_result = await strategy.evaluate_position(position, df, sigs_flat)
                except Exception as exc:
                    log.warning(
                        "Medium loop: evaluate_position failed %s/%s: %s",
                        symbol, type(strategy).__name__, exc,
                    )
                    continue

                if eval_result.action != "exit":
                    continue

                log.info(
                    "Medium loop: strategy exit signal — %s  action=%s  confidence=%.2f  reason=%s",
                    symbol, eval_result.action, eval_result.confidence, eval_result.reasoning,
                )

                if not self._fill_protection.can_place_order(symbol):
                    log.warning(
                        "Medium loop: exit blocked for %s — pending order exists", symbol
                    )
                    break

                # Get suggested exit parameters
                try:
                    exit_sug = await strategy.suggest_exit(position, df, sigs_flat)
                except Exception as exc:
                    log.warning("Medium loop: suggest_exit failed %s: %s", symbol, exc)
                    exit_sug = None

                # buy to close short, sell to close long
                exit_side = EXIT_SIDE[position.intention.direction]
                if exit_sug and exit_sug.order_type == "limit" and exit_sug.exit_price > 0:
                    exit_order = Order(
                        symbol=symbol,
                        side=exit_side,
                        quantity=position.shares,
                        order_type="limit",
                        limit_price=exit_sug.exit_price,
                        time_in_force="day",
                    )
                else:
                    exit_order = Order(
                        symbol=symbol,
                        side=exit_side,
                        quantity=position.shares,
                        order_type="market",
                        time_in_force="day",
                    )

                try:
                    result = await broker.place_order(exit_order)
                except Exception as exc:
                    self._on_broker_failure(exc)
                    break
                self._on_broker_available()

                now_iso = datetime.now(timezone.utc).isoformat()
                record = OrderRecord(
                    order_id=result.order_id,
                    symbol=symbol,
                    side=exit_side,
                    quantity=position.shares,
                    order_type=exit_order.order_type,
                    limit_price=exit_order.limit_price,
                    status="PENDING",
                    created_at=now_iso,
                    last_checked_at=now_iso,
                )
                await self._fill_protection.record_order(record)
                log.info(
                    "Exit order placed — %s  qty=%d  type=%s  strategy=%s",
                    symbol, position.shares, exit_order.order_type, type(strategy).__name__,
                )
                break  # one exit order per position per cycle

            # EOD forced close for momentum shorts.
            # Swing shorts may be held overnight intentionally — excluded.
            # Only fires if no exit order was placed by strategy evaluation above.
            if (
                is_short(position.intention.direction)
                and position.intention.strategy == "momentum"
                and is_last_five_minutes()
                and self._fill_protection.can_place_order(symbol)
            ):
                log.info(
                    "Medium loop: EOD forced close — momentum short %s in last 5 minutes",
                    symbol,
                )
                exit_side = EXIT_SIDE[position.intention.direction]
                eod_order = Order(
                    symbol=symbol,
                    side=exit_side,
                    quantity=position.shares,
                    order_type="market",
                    time_in_force="day",
                )
                try:
                    result = await broker.place_order(eod_order)
                except Exception as exc:
                    self._on_broker_failure(exc)
                    continue
                self._on_broker_available()
                now_iso = datetime.now(timezone.utc).isoformat()
                record = OrderRecord(
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
                await self._fill_protection.record_order(record)
                # Tag exit reason for trade journal (consumed in _dispatch_confirmed_fill)
                self._pending_exit_hints[symbol] = "eod_close"
                log.info(
                    "EOD short exit order placed — %s  order_id=%s", symbol, result.order_id,
                )

    async def apply_position_reviews(
        self,
        reviews: list[dict],
        *,
        broker,
        latest_indicators: dict,
    ) -> None:
        """Append Claude's review notes and act on exit recommendations.

        Always loads a fresh portfolio snapshot so stop/target adjustments are
        not silently lost when a concurrent fast-loop or medium-loop save writes
        the disk between the slow loop's initial load and this function's save.

        broker and latest_indicators are passed at call time (not stored)
        because tests commonly reassign these on the orchestrator after startup.
        """
        portfolio = await self._state_manager.load_portfolio()
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        changed = False
        for review in reviews:
            symbol = review.get("symbol", "")
            action = review.get("action", "hold")
            note = review.get("updated_reasoning") or review.get("notes", "")
            log.info(
                "Position review: %s — action=%s — %s",
                symbol, action, note or "(no rationale provided)",
            )
            # Stamp review time so thesis breach scheduling can suppress redundant
            # Sonnet calls for positions reviewed within thesis_breach_review_cooldown_min.
            if symbol:
                self._last_position_review_utc[symbol] = now_utc
            for pos in portfolio.positions:
                if pos.symbol != symbol:
                    continue

                # Journal this review event before any mutations so the record
                # captures the state Claude reasoned about (current stop/target
                # before adjustment) plus what Claude recommended.
                _review_price = latest_indicators.get(symbol, {}).get("price")
                _pos_is_short = is_short(pos.intention.direction)
                if _review_price and pos.avg_cost > 0:
                    _gain = (
                        (pos.avg_cost - _review_price) / pos.avg_cost
                        if _pos_is_short
                        else (_review_price - pos.avg_cost) / pos.avg_cost
                    )
                    _unrealized_pnl_pct = round(_gain * 100, 4)
                else:
                    _unrealized_pnl_pct = None
                await self._trade_journal.append({
                    "record_type": "review",
                    "trade_id": self._entry_contexts.get(symbol, {}).get("trade_id"),
                    "symbol": symbol,
                    "strategy": pos.intention.strategy,
                    "direction": pos.intention.direction,
                    "action": action,
                    "note": review.get("updated_reasoning") or review.get("notes", ""),
                    "current_price": _review_price,
                    "unrealized_pnl_pct": _unrealized_pnl_pct,
                    "current_stop": pos.intention.exit_targets.stop_loss,
                    "current_target": pos.intention.exit_targets.profit_target,
                    "thesis_intact": review.get("thesis_intact"),
                    "adjusted_targets": review.get("adjusted_targets"),
                    "source": "live",
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })

                if note:
                    pos.intention.review_notes.append(f"[{now_iso}] {note}")
                    changed = True
                # Apply adjusted targets if provided
                adj = review.get("adjusted_targets") or {}
                if adj.get("profit_target"):
                    old_target = pos.intention.exit_targets.profit_target
                    new_target = float(adj["profit_target"])
                    if new_target == old_target:
                        log.debug("Position review: %s target no-op (%.4f unchanged)", symbol, old_target)
                    else:
                        pos.intention.exit_targets.profit_target = new_target
                        changed = True
                        log.info(
                            "Position review: %s target adjusted  %.4f → %.4f",
                            symbol, old_target, new_target,
                        )
                if adj.get("stop_loss"):
                    new_stop = float(adj["stop_loss"])
                    # Guard: reject a stop adjustment that would put the stop on the
                    # wrong side of current price — it would trigger an immediate exit
                    # on the next fast-loop cycle rather than protecting future gains.
                    # This happened with XOM when Claude raised the stop to $162 while
                    # price was $161.25, forcing an instant exit at +1.7%.
                    _cur = _review_price  # already fetched above; None if unavailable
                    # _from_dict_position normalises "sell_short" → "short" at load
                    # time, so _pos_is_short (from is_short()) is reliable here.
                    _is_short_dir = _pos_is_short
                    _would_trigger = (
                        _cur is not None and (
                            (not _is_short_dir and new_stop >= _cur)
                            or (_is_short_dir and new_stop <= _cur)
                        )
                    )
                    old_stop = pos.intention.exit_targets.stop_loss
                    if new_stop == old_stop:
                        log.debug("Position review: %s stop no-op (%.4f unchanged)", symbol, old_stop)
                    elif _would_trigger:
                        log.warning(
                            "Stop adjustment rejected for %s: new_stop=%.4f would "
                            "immediately trigger at current_price=%.4f — keeping "
                            "existing stop=%.4f",
                            symbol, new_stop, _cur,
                            old_stop,
                        )
                    else:
                        pos.intention.exit_targets.stop_loss = new_stop
                        changed = True
                        log.info(
                            "Position review: %s stop adjusted  %.4f → %.4f  price=%.4f",
                            symbol, old_stop, new_stop, _cur or 0.0,
                        )
                # Claude recommends exiting — place a market exit order immediately.
                # thesis_intact=False is a hard signal; action="exit" is sufficient.
                if action == "exit":
                    # Guard against the override-vs-Claude race: if this symbol was
                    # closed by the fast loop (override/stop) while Claude's API call
                    # was in flight, the position no longer exists — skip the exit.
                    if symbol in self._recently_closed:
                        log.info(
                            "Claude position review: exit for %s skipped — "
                            "position already closed by fast loop (%.0fs ago)",
                            symbol,
                            time.monotonic() - self._recently_closed[symbol],
                        )
                        break
                    if not self._fill_protection.can_place_order(symbol):
                        log.warning(
                            "Claude position review: exit for %s blocked — pending order exists",
                            symbol,
                        )
                        break
                    exit_side = EXIT_SIDE[pos.intention.direction]
                    exit_order = Order(
                        symbol=symbol,
                        side=exit_side,
                        quantity=pos.shares,
                        order_type="market",
                        time_in_force="day",
                    )
                    try:
                        result = await broker.place_order(exit_order)
                        order_record = OrderRecord(
                            order_id=result.order_id,
                            symbol=symbol,
                            side=exit_side,
                            quantity=pos.shares,
                            order_type="market",
                            limit_price=None,
                            status="PENDING",
                            created_at=now_iso,
                            last_checked_at=now_iso,
                        )
                        await self._fill_protection.record_order(order_record)
                        log.info(
                            "Claude position review exit: %s  order_id=%s  "
                            "reason=%s",
                            symbol, result.order_id,
                            review.get("updated_reasoning", "")[:120],
                        )
                    except Exception as exc:
                        log.error(
                            "Failed to place Claude review exit for %s: %s",
                            symbol, exc,
                        )
                    break  # done with this position
        if changed:
            await self._state_manager.save_portfolio(portfolio)
