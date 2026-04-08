"""
PositionSync — reconciles local portfolio state with broker-reported positions.

Extracted from orchestrator.py to enable independent testing and parallel
development. The orchestrator delegates to this module via a thin wrapper.

Uses mutable shared references (Python dicts passed by reference) for
state that the orchestrator's other loops also read/write. This is safe
because all loops run in a single asyncio event loop (no concurrent mutation).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from ozymandias.core.config import Config
from ozymandias.core.direction import is_short
from ozymandias.core.state_manager import ExitTargets, Position, TradeIntention
from ozymandias.execution.fill_protection import FillProtectionManager

log = logging.getLogger(__name__)


class PositionSync:
    """Compares local portfolio state with broker-reported positions each fast tick.

    Handles three discrepancy types:
    - Ghost locals: positions we track but broker doesn't have (external close)
    - Untracked broker: positions broker has that we don't track (external open)
    - Quantity mismatch: shares differ between local and broker (partial fills)

    Owns no state of its own — operates on mutable references passed by the
    orchestrator at construction time.
    """

    def __init__(
        self,
        config: Config,
        state_manager,
        fill_protection: FillProtectionManager,
        trade_journal,
        # Mutable shared state
        entry_contexts: dict,
        recently_closed: dict,
        pending_intentions: dict,
        position_entry_times: dict,
        # Callbacks for degradation state (stay on orchestrator)
        on_broker_failure: Callable[[Exception], None],
        on_broker_available: Callable[[], None],
    ) -> None:
        self._config = config
        self._state_manager = state_manager
        self._fill_protection = fill_protection
        self._trade_journal = trade_journal
        self._entry_contexts = entry_contexts
        self._recently_closed = recently_closed
        self._pending_intentions = pending_intentions
        self._position_entry_times = position_entry_times
        self._on_broker_failure = on_broker_failure
        self._on_broker_available = on_broker_available

    async def step(self, broker, latest_indicators: dict) -> None:
        """Run one position sync cycle.

        broker and latest_indicators are passed at call time (not stored)
        because tests commonly reassign these on the orchestrator after startup.
        """
        try:
            broker_positions = await broker.get_positions()
        except Exception as exc:
            self._on_broker_failure(exc)
            return

        self._on_broker_available()

        try:
            portfolio = await self._state_manager.load_portfolio()
        except Exception as exc:
            log.error("Failed to load portfolio for sync: %s", exc, exc_info=True)
            return

        broker_symbols = {p.symbol for p in broker_positions}
        local_symbols = {p.symbol for p in portfolio.positions}
        local_map = {p.symbol: p for p in portfolio.positions}
        portfolio_updated = False

        # Positions we have locally but broker no longer holds — these were closed
        # outside the normal fill-detection path (e.g. manual broker close).
        # Exception: if there's a pending/partially-filled exit order for the symbol,
        # the position was closed by our own order but the fill hasn't been processed
        # yet (common with fast-settling paper market orders). Skip ghost cleanup and
        # let _dispatch_confirmed_fill handle it on the next cycle.
        ghost_local = local_symbols - broker_symbols
        if ghost_local:
            deferred = {s for s in ghost_local if not self._fill_protection.can_place_order(s)}
            if deferred:
                log.debug(
                    "Position sync: deferring ghost cleanup for %s — active exit order in flight",
                    deferred,
                )
            ghost_local -= deferred

        if ghost_local:
            log.warning(
                "Position sync: local positions not found at broker (likely closed externally): %s",
                ghost_local,
            )
            for symbol in ghost_local:
                pos = local_map.get(symbol)
                if pos is None:
                    continue
                ctx = self._entry_contexts.pop(symbol, {})
                current_price = latest_indicators.get(symbol, {}).get("price", 0.0)
                # Fall back to avg_cost when no market price is available — records
                # pnl=0% rather than a misleading 0-price exit.
                if current_price == 0.0:
                    current_price = pos.avg_cost
                await self._trade_journal.append({
                    "symbol": symbol,
                    "strategy": pos.intention.strategy,
                    "direction": pos.intention.direction,
                    "entry_time": pos.entry_date,
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "entry_price": pos.avg_cost,
                    "exit_price": current_price,
                    "shares": pos.shares,
                    "pnl_pct": round(
                        (
                            (pos.avg_cost - current_price) / pos.avg_cost * 100
                            if is_short(pos.intention.direction)
                            else (current_price - pos.avg_cost) / pos.avg_cost * 100
                        )
                        if pos.avg_cost > 0 and current_price > 0 else 0.0, 4
                    ),
                    "stop_price": pos.intention.exit_targets.stop_loss,
                    "target_price": pos.intention.exit_targets.profit_target,
                    "exit_reason": "external_close",
                    "record_type": "close",
                    "trade_id": ctx.get("trade_id"),
                    "signals_at_entry": ctx.get("signals", {}),
                    "claude_conviction": ctx.get("claude_conviction", 0.0),
                    "composite_score": ctx.get("composite_score", 0.0),
                    "position_size_pct": ctx.get("position_size_pct", 0.0),
                    "peak_unrealized_pct": ctx.get("peak_unrealized_pct", 0.0),
                    "source": "live",
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })
            portfolio.positions = [p for p in portfolio.positions if p.symbol not in ghost_local]
            now_utc_iso = datetime.now(timezone.utc).isoformat()
            for symbol in ghost_local:
                self._recently_closed[symbol] = time.monotonic()
                portfolio.recently_closed[symbol] = now_utc_iso
            portfolio_updated = True

        # Positions broker has that we don't track locally.
        # Normal bot fills are created by _register_buy_fill in the reconcile loop.
        # This path is a fallback for positions opened externally (manual trades, etc.).
        _readopt_ttl = 60.0  # seconds to block re-adoption after a position is closed
        for bp in broker_positions:
            if bp.symbol not in local_symbols:
                # Guard: if we just closed this symbol, don't immediately re-adopt.
                # This prevents the runaway loop where every close triggers a re-adopt
                # which triggers another override exit (e.g. 16 AMD sell orders).
                closed_at = self._recently_closed.get(bp.symbol, 0.0)
                if time.monotonic() - closed_at < _readopt_ttl:
                    log.debug(
                        "Position sync: skipping re-adoption of %s — closed %.0fs ago "
                        "(fill detection pending or position settling)",
                        bp.symbol, time.monotonic() - closed_at,
                    )
                    continue
                # Guard: if we have an active (PENDING or PARTIALLY_FILLED) opening
                # order for this symbol, the fill handler will register it properly
                # with the full intention when the order completes. Adopting here
                # would consume _pending_intentions prematurely, which causes the full
                # fill to be routed as a close (position already exists) and then
                # re-adopted without intention (strategy="unknown").
                in_flight = [
                    o for o in self._fill_protection.get_orders_for_symbol(bp.symbol)
                    if o.status in ("PENDING", "PARTIALLY_FILLED")
                ]
                if in_flight:
                    log.debug(
                        "Position sync: skipping adoption of %s — in-flight opening order %s",
                        bp.symbol, in_flight[0].order_id,
                    )
                    continue
                pending = self._pending_intentions.pop(bp.symbol, {})
                if pending:
                    self._entry_contexts[bp.symbol] = {
                        "trade_id": str(uuid.uuid4()),
                        "signals": pending.pop("_signals", {}),
                        "claude_conviction": pending.pop("_claude_conviction", 0.0),
                        "composite_score": pending.pop("_composite_score", 0.0),
                        "position_size_pct": pending.pop("_position_size_pct", 0.0),
                    }
                intention = TradeIntention(
                    strategy=pending.get("strategy", "unknown"),
                    direction=pending.get("direction", bp.side),
                    reasoning=pending.get("reasoning", ""),
                    exit_targets=ExitTargets(
                        stop_loss=pending.get("stop", 0.0),
                        profit_target=pending.get("target", 0.0),
                    ),
                )
                # Broker reports negative qty for short positions; shares are
                # always stored as positive — direction field carries the sign.
                adopted_qty = abs(bp.qty)
                portfolio.positions.append(Position(
                    symbol=bp.symbol,
                    shares=adopted_qty,
                    avg_cost=bp.avg_entry_price,
                    entry_date=datetime.now(timezone.utc).isoformat(),
                    intention=intention,
                ))
                # Give adopted positions the hold-time window so override signals
                # based on stale indicators don't fire immediately after adoption.
                self._position_entry_times[bp.symbol] = time.monotonic()
                portfolio_updated = True
                log.warning(
                    "Position sync: untracked broker position adopted: %s  qty=%.2f  avg_cost=%.4f  side=%s",
                    bp.symbol, adopted_qty, bp.avg_entry_price, bp.side,
                )

        # Quantity mismatches — broker is authoritative; correct local state.
        # Broker reports negative qty for shorts; compare using abs() since
        # local shares are always stored as a positive number.
        #
        # IMPORTANT: Skip upward corrections when there is an active (PENDING or
        # PARTIALLY_FILLED) order for the symbol. The broker qty already reflects
        # shares from that in-flight order, which haven't been dispatched as a fill
        # event yet. Correcting upward here would cause _dispatch_confirmed_fill to
        # see "position already exists" and route the fill as a close instead of an
        # additional open. Downward corrections are always safe (they mean shares
        # were removed, e.g. an external close).
        local_map = {p.symbol: p for p in portfolio.positions}
        for bp in broker_positions:
            lp = local_map.get(bp.symbol)
            if lp is None:
                continue
            broker_abs_qty = abs(bp.qty)
            if abs(broker_abs_qty - lp.shares) > 0.001:
                if broker_abs_qty > lp.shares and not self._fill_protection.can_place_order(bp.symbol):
                    log.debug(
                        "Position sync: skipping upward qty correction for %s "
                        "(local=%.4f broker=%.4f) — active order in flight",
                        bp.symbol, lp.shares, broker_abs_qty,
                    )
                    continue
                log.warning(
                    "Position sync: correcting %s qty local=%.4f → broker=%.4f",
                    bp.symbol, lp.shares, broker_abs_qty,
                )
                lp.shares = broker_abs_qty
                portfolio_updated = True

        if portfolio_updated:
            await self._state_manager.save_portfolio(portfolio)
