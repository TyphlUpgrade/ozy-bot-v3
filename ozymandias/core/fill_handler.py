"""
FillHandler — routes confirmed fills, registers opening positions, journals closed trades.

Extracted from orchestrator.py to enable independent testing and parallel
development. The orchestrator delegates to this module via thin wrappers.

Uses mutable shared references (Python dicts/sets passed by reference) for
state that the orchestrator's other loops also read/write. This is safe
because all loops run in a single asyncio event loop (no concurrent mutation).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from ozymandias.core.config import Config
from ozymandias.core.direction import is_short
from ozymandias.core.trade_journal import TradeJournal
from ozymandias.core.trigger_engine import SlowLoopTriggerState

log = logging.getLogger(__name__)


class FillHandler:
    """Routes confirmed fills to open/close handlers and journals trades.

    Owns no state of its own — operates on mutable references passed by the
    orchestrator at construction time. This keeps the orchestrator as the
    single owner of shared state while letting the fill logic be independently
    testable.
    """

    def __init__(
        self,
        config: Config,
        state_manager,
        trade_journal: TradeJournal,
        trigger_state: SlowLoopTriggerState,
        # Mutable shared dicts (references, not copies)
        entry_contexts: dict,
        pending_intentions: dict,
        pending_exit_hints: dict,
        recommendation_outcomes: dict,
        position_entry_times: dict,
        recently_closed: dict,
        cycle_consumed_symbols: set,
        override_closed: dict,
        latest_indicators: dict,
    ) -> None:
        self._config = config
        self._state_manager = state_manager
        self._trade_journal = trade_journal
        self._trigger_state = trigger_state
        self._entry_contexts = entry_contexts
        self._pending_intentions = pending_intentions
        self._pending_exit_hints = pending_exit_hints
        self._recommendation_outcomes = recommendation_outcomes
        self._position_entry_times = position_entry_times
        self._recently_closed = recently_closed
        self._cycle_consumed_symbols = cycle_consumed_symbols
        self._override_closed = override_closed
        self._latest_indicators = latest_indicators

    async def dispatch_confirmed_fill(self, change) -> None:
        """Route a confirmed fill to the correct handler.

        Uses portfolio state to determine intent:
        - Symbol already has a local position → this is a closing fill → journal it
        - No local position → this is an opening fill → register the new position

        This is correct for all four cases: long open (buy), long close (sell),
        short open (sell), short close (buy). Using change.side alone is wrong
        because sell can mean either short-open or long-close.
        """
        try:
            portfolio = await self._state_manager.load_portfolio()
        except Exception as exc:
            log.error("dispatch_confirmed_fill: failed to load portfolio for %s: %s", change.symbol, exc, exc_info=True)
            return
        has_position = any(p.symbol == change.symbol for p in portfolio.positions)
        if has_position:
            hint = self._pending_exit_hints.pop(change.symbol, None)
            await self.journal_closed_trade(change, exit_reason_hint=hint)
        else:
            await self.register_opening_fill(change)
            # Phase 15: mark recommendation as filled on confirmed opening fill.
            if change.symbol in self._recommendation_outcomes:
                self._recommendation_outcomes[change.symbol]["stage"] = "filled"

    async def register_opening_fill(self, change) -> None:
        """Create a local portfolio position when an opening fill is confirmed.

        Called from dispatch_confirmed_fill for both long opens (buy fill) and
        short opens (sell fill). Positions are created at the moment of confirmed
        fill — with the correct quantity, avg fill price, and intention — rather
        than speculatively from get_positions() which can race against partial fills.

        Shares are always stored as a positive number; direction="short" conveys
        the sign. This matches how exit order quantity= fields are used throughout.
        """
        from ozymandias.core.state_manager import ExitTargets, Position, TradeIntention

        symbol = change.symbol
        pending = self._pending_intentions.pop(symbol, {})

        # Move signal context into entry_contexts for use when the position closes.
        # Also generate a stable trade_id here so all journal records for this
        # trade (open, snapshot, review, close) can be correlated by trade_id.
        if pending:
            self._entry_contexts[symbol] = {
                "trade_id": str(uuid.uuid4()),
                "signals": pending.pop("_signals", {}),
                "claude_conviction": pending.pop("_claude_conviction", 0.0),
                "composite_score": pending.pop("_composite_score", 0.0),
                "position_size_pct": pending.pop("_position_size_pct", 0.0),
            }

        try:
            portfolio = await self._state_manager.load_portfolio()
        except Exception as exc:
            log.error("register_opening_fill: failed to load portfolio for %s: %s", symbol, exc, exc_info=True)
            return

        # Guard: if position already exists (e.g. duplicate fill event), skip
        if any(p.symbol == symbol for p in portfolio.positions):
            log.debug("register_opening_fill: position for %s already exists — skipping duplicate", symbol)
            return

        intention = TradeIntention(
            strategy=pending.get("strategy", "unknown"),
            direction=pending.get("direction", "long"),
            reasoning=pending.get("reasoning", ""),
            exit_targets=ExitTargets(
                stop_loss=pending.get("stop", 0.0),
                profit_target=pending.get("target", 0.0),
            ),
            # Persist signal context in portfolio.json so it survives bot restarts.
            # _entry_contexts (in-memory) is populated from these fields at startup.
            entry_signals=self._entry_contexts.get(symbol, {}).get("signals", {}),
            entry_conviction=self._entry_contexts.get(symbol, {}).get("claude_conviction", 0.0),
            entry_score=self._entry_contexts.get(symbol, {}).get("composite_score", 0.0),
        )
        entry_date = datetime.now(timezone.utc).isoformat()
        from ozymandias.core.state_manager import Position
        portfolio.positions.append(Position(
            symbol=symbol,
            shares=change.fill_qty,  # always positive; direction field carries the sign
            avg_cost=change.fill_price if change.fill_price > 0 else 0.0,
            entry_date=entry_date,
            intention=intention,
        ))
        await self._state_manager.save_portfolio(portfolio)
        self._position_entry_times[symbol] = time.monotonic()
        log.info(
            "Position registered from opening fill: %s  qty=%.2f  avg_cost=%.4f  "
            "stop=%.4f  target=%.4f  strategy=%s  direction=%s",
            symbol, change.fill_qty, change.fill_price,
            pending.get("stop", 0.0), pending.get("target", 0.0),
            pending.get("strategy", "unknown"), pending.get("direction", "long"),
        )

        # Write the "open" journal record. Omitted for broker-adopted positions
        # (pending is empty) because we lack entry context in that case.
        trade_id = self._entry_contexts.get(symbol, {}).get("trade_id")
        if trade_id:
            ctx = self._entry_contexts[symbol]
            await self._trade_journal.append({
                "record_type": "open",
                "trade_id": trade_id,
                "symbol": symbol,
                "strategy": pending.get("strategy", "unknown"),
                "direction": pending.get("direction", "long"),
                "entry_time": entry_date,
                "entry_price": change.fill_price if change.fill_price > 0 else 0.0,
                "shares": change.fill_qty,
                "stop_price": pending.get("stop", 0.0),
                "target_price": pending.get("target", 0.0),
                "signals_at_entry": ctx.get("signals", {}),
                "claude_conviction": ctx.get("claude_conviction", 0.0),
                "composite_score": ctx.get("composite_score", 0.0),
                "position_size_pct": ctx.get("position_size_pct", 0.0),
                "source": "live",
                "prompt_version": self._config.claude.prompt_version,
                "bot_version": self._config.claude.model,
            })

    async def journal_closed_trade(self, change, exit_reason_hint: str | None = None) -> None:
        """Write a trade journal entry and remove the position from the portfolio.

        Called for any closing fill: sell fill (long close) or buy fill (short close).
        Captures: entry data from portfolio, exit price from broker fill, signal
        context from _entry_contexts (populated when the opening fill arrived).

        exit_reason_hint, if provided, overrides price-inferred exit reason logic.
        Set by override/short-exit/EOD paths via _pending_exit_hints.
        """
        symbol = change.symbol
        try:
            portfolio = await self._state_manager.load_portfolio()
            position = next((p for p in portfolio.positions if p.symbol == symbol), None)
            if position is None:
                log.debug("journal_closed_trade: no local position for %s — skipping", symbol)
                return

            entry_price = position.avg_cost
            exit_price = change.fill_price
            pos_is_short = is_short(position.intention.direction)
            if entry_price > 0 and exit_price > 0:
                if pos_is_short:
                    pnl_pct = round((entry_price - exit_price) / entry_price * 100, 4)
                else:
                    pnl_pct = round((exit_price - entry_price) / entry_price * 100, 4)
            else:
                pnl_pct = 0.0

            # Infer exit reason from fill price vs stop/target levels.
            # Hint from caller (override/short-protection/EOD paths) takes priority.
            # For shorts: target is below entry (profit when price falls),
            # stop is above entry (loss when price rises).
            stop = position.intention.exit_targets.stop_loss
            target = position.intention.exit_targets.profit_target
            if exit_reason_hint:
                exit_reason = exit_reason_hint
            elif pos_is_short:
                if exit_price > 0 and target > 0 and exit_price <= target * 1.001:
                    exit_reason = "target"
                elif exit_price > 0 and stop > 0 and exit_price >= stop * 0.999:
                    exit_reason = "stop"
                else:
                    exit_reason = "strategy"
            else:
                if exit_price > 0 and target > 0 and exit_price >= target * 0.999:
                    exit_reason = "target"
                elif exit_price > 0 and stop > 0 and exit_price <= stop * 1.001:
                    exit_reason = "stop"
                else:
                    exit_reason = "strategy"

            ctx = self._entry_contexts.pop(symbol, {})
            # Capture live TA indicators at exit time for threshold tuning.
            # These are the fast-loop cached values — same data the override logic uses.
            signals_at_exit = dict(self._latest_indicators.get(symbol, {}))
            # Fall back to TradeIntention fields if in-memory context was lost (e.g. restart).
            signals_at_entry = (
                ctx.get("signals")
                or position.intention.entry_signals
            )
            claude_conviction = (
                ctx.get("claude_conviction")
                or position.intention.entry_conviction
            )
            composite_score = (
                ctx.get("composite_score")
                or position.intention.entry_score
            )
            exit_time = datetime.now(timezone.utc)
            try:
                entry_dt = datetime.fromisoformat(position.entry_date)
                hold_duration_min = round((exit_time - entry_dt).total_seconds() / 60, 1)
            except Exception:
                hold_duration_min = None
            await self._trade_journal.append({
                "record_type": "close",
                "trade_id": ctx.get("trade_id"),
                "symbol": symbol,
                "strategy": position.intention.strategy,
                "direction": position.intention.direction,
                "entry_time": position.entry_date,
                "exit_time": exit_time.isoformat(),
                "hold_duration_min": hold_duration_min,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "shares": change.fill_qty,
                "pnl_pct": pnl_pct,
                "stop_price": stop,
                "target_price": target,
                "exit_reason": exit_reason,
                "signals_at_entry": signals_at_entry,
                "signals_at_exit": signals_at_exit or None,
                "claude_conviction": claude_conviction,
                "composite_score": composite_score,
                "position_size_pct": ctx.get("position_size_pct", 0.0),
                "peak_unrealized_pct": ctx.get("peak_unrealized_pct", 0.0),
                "source": "live",
                "prompt_version": self._config.claude.prompt_version,
                "bot_version": self._config.claude.model,
            })

            portfolio.positions = [p for p in portfolio.positions if p.symbol != symbol]
            # Persist close timestamp before saving so _recently_closed survives restarts.
            now_utc_iso = datetime.now(timezone.utc).isoformat()
            portfolio.recently_closed[symbol] = now_utc_iso
            await self._state_manager.save_portfolio(portfolio)
            self._recently_closed[symbol] = time.monotonic()
            self._cycle_consumed_symbols.add(symbol)
            self._position_entry_times.pop(symbol, None)
            self._trigger_state.last_profit_trigger_gain.pop(symbol, None)
            self._trigger_state.last_near_target_time.pop(symbol, None)
            self._trigger_state.last_near_stop_time.pop(symbol, None)
            self._override_closed.pop(symbol, None)
            log.info(
                "Trade closed and journaled: %s  pnl=%.2f%%  exit_reason=%s",
                symbol, pnl_pct, exit_reason,
            )
        except Exception as exc:
            log.error("journal_closed_trade failed for %s: %s", symbol, exc, exc_info=True)
