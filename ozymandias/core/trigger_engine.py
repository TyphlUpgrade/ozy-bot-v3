"""
TriggerEngine — evaluates slow-loop trigger conditions.

Extracted from orchestrator.py to enable independent testing and parallel
development. The orchestrator delegates to this module via thin wrappers.

All trigger evaluation logic lives here. The orchestrator retains ownership
of when to call check_triggers() and what to do with the results.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ozymandias.core.config import Config
from ozymandias.core.direction import is_short
from ozymandias.core.market_hours import Session, get_current_session, get_next_market_open
from ozymandias.core.trade_journal import TradeJournal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slow loop trigger state
# ---------------------------------------------------------------------------

@dataclass
class SlowLoopTriggerState:
    """Tracks state needed to evaluate whether a slow-loop trigger has fired."""
    last_claude_call_utc: Optional[datetime] = None
    last_prices: dict[str, float] = field(default_factory=dict)
    last_override_exit_count: int = 0
    # Whether a Claude call is currently in-flight (prevents concurrent calls)
    claude_call_in_flight: bool = False
    # Track session transitions so we only fire once per transition
    last_session: Optional[str] = None
    # Fired once after the first medium loop cycle populates indicators
    indicators_seeded: bool = False
    # Unrealised gain pct (as a fraction) at the time of the last profit trigger per symbol.
    # Trigger fires when gain_pct >= last value + position_profit_trigger_pct.
    # Updated after each successful Claude call; cleared when a position closes.
    last_profit_trigger_gain: dict[str, float] = field(default_factory=dict)
    # monotonic timestamp of the last near_target Claude call per symbol.
    # Suppresses repeated firing while price stays within the target zone — only
    # re-fires after near_target_cooldown_sec elapses since last handled review.
    # Cleared when a position closes.
    last_near_target_time: dict[str, float] = field(default_factory=dict)
    # monotonic timestamp of the last near_stop Claude call per symbol.
    # Mirrors near_target_time — same cooldown (near_target_cooldown_sec) prevents
    # repeated firing while price oscillates within 1% of the stop level.
    # Without this, near_stop fires every slow-loop tick (~60s) indefinitely.
    # Cleared when a position closes.
    last_near_stop_time: dict[str, float] = field(default_factory=dict)
    # Phase 17: price baseline anchored to the last successful Claude call (not reset each cycle).
    # Used by macro_move and sector_move triggers so that sustained index/sector moves always fire
    # even when last_prices is updated each tick. Separate from last_prices which resets per eval.
    last_claude_call_prices: dict[str, float] = field(default_factory=dict)
    # Phase 17: RSI extreme trigger re-arm tracking.
    # Once fired, the trigger cannot re-fire until RSI recovers by macro_rsi_rearm_band points.
    rsi_extreme_fired_low: bool = False    # True after panic trigger fires; cleared when RSI recovers above threshold + band
    rsi_extreme_fired_high: bool = False   # True after euphoria trigger fires; cleared when RSI falls below threshold - band
    # watchlist_stale trigger: UTC timestamp of the last completed watchlist build.
    # None at startup → elapsed = ∞ → watchlist_stale fires on the first slow loop tick.
    # Reset after every successful build (both watchlist_small and watchlist_stale paths).
    last_watchlist_build_utc: Optional[datetime] = None
    # Set by the medium loop when every symbol from the current reasoning cache is either
    # hard-filter suppressed OR already held as an open position. Cleared by check_triggers
    # after firing once per cache generation (guarded by last_exhaustion_trigger_utc <
    # last_claude_call_utc). To add another exhaustion-style trigger: follow this pattern.
    candidates_exhausted: bool = False
    last_exhaustion_trigger_utc: Optional[datetime] = None
    # Set by _handle_claude_failure so that check_triggers fires a dedicated retry trigger
    # once the backoff window expires. Without this, a failure when last_claude_call_utc is
    # set from a restored cache can leave the bot waiting up to slow_loop_max_interval_sec
    # (60 min) before any trigger fires — because no_previous_call won't fire (the cached
    # timestamp is not None) and time_ceiling won't fire until 60 min after the cached call.
    claude_retry_pending: bool = False
    # ISO date string (YYYY-MM-DD) of the market open for which a pre_market_warmup trigger
    # has already fired this cycle. Prevents re-firing every slow-loop tick once inside the
    # warmup window. Cleared implicitly when date advances to the next trading day.
    last_warmup_session_date: Optional[str] = None
    # Prevents approaching_close from firing more than once per regular session.
    # The trigger window is 4 minutes (15:28–15:32 ET) but slow_loop_check_sec is 60s,
    # so without this flag the trigger fires on every tick within the window.
    # Cleared when session transitions away from REGULAR_HOURS.
    approaching_close_fired: bool = False
    # UTC datetime of the last regime_condition trigger fire. Used to enforce a minimum
    # cooldown between consecutive regime_condition triggers so that a miscalibrated
    # valid_until_condition (or a noisy signal) cannot chain-fire every 60 seconds.
    # None = never fired. Controlled by scheduler.regime_condition_cooldown_min.
    last_regime_condition_utc: Optional[datetime] = None


# Sector ETFs tracked for sector_move triggers — subset of context symbols
# excluding the three broad-market indices (handled by market_move triggers).
# Extension point: to add a new sector, add its ETF to _CONTEXT_SYMBOLS in
# orchestrator.py and here.
_CONTEXT_SECTOR_ETFS: list[str] = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLC", "ITA", "XBI"]


class TriggerEngine:
    """Evaluates slow-loop trigger conditions.

    Owns the ``SlowLoopTriggerState`` instance. The orchestrator passes in
    the runtime data each method needs — the engine never reaches back into
    the orchestrator.
    """

    def __init__(
        self,
        config: Config,
        trade_journal: TradeJournal,
        # Sector membership map: symbol → sector ETF. Shared with orchestrator.
        sector_map: dict[str, str],
    ) -> None:
        self._config = config
        self._trade_journal = trade_journal
        self._sector_map = sector_map
        self.state = SlowLoopTriggerState()

    async def check_triggers(
        self,
        *,
        all_indicators: dict,
        latest_indicators: dict,
        market_context_indicators: dict,
        override_exit_count: int,
        last_regime_assessment: dict | None,
        daily_indicators: dict,
        entry_contexts: dict,
        state_manager,
        is_pre_market_warmup: bool,
        now: datetime | None = None,
    ) -> list[str]:
        """
        Evaluate all slow-loop trigger conditions.
        Returns a list of trigger name strings (empty = no trigger).

        ``now`` is injectable for testing; defaults to current UTC time.
        """
        triggers: list[str] = []
        now = now or datetime.now(timezone.utc)
        ts = self.state

        # 0a. Retry after failure: _handle_claude_failure sets this flag so the next
        # slow-loop tick after backoff expiry fires immediately, regardless of whether
        # last_claude_call_utc (which may be a restored cache timestamp) would otherwise
        # keep time_ceiling dormant for up to slow_loop_max_interval_sec minutes.
        if ts.claude_retry_pending:
            ts.claude_retry_pending = False
            triggers.append("claude_retry")
            log.debug("Trigger: claude_retry FIRED (backoff expired after API failure)")

        # 0b. Pre-market warmup: fires once per upcoming market session when the bot
        # enters the pre_market_warmup_min window before open. Warms the reasoning
        # cache so fresh Claude candidates are available the moment market opens —
        # identical effect to a manual 9:25 start regardless of actual bot start time.
        if is_pre_market_warmup:
            _next_open_date = get_next_market_open().date().isoformat()
            if ts.last_warmup_session_date != _next_open_date:
                ts.last_warmup_session_date = _next_open_date
                triggers.append("pre_market_warmup")
                _delta = (get_next_market_open().astimezone(timezone.utc) - now).total_seconds() / 60
                log.info("Trigger: pre_market_warmup FIRED (%.1f min before open)", _delta)

        # 1. Time ceiling: 60+ minutes since last Claude call
        if ts.last_claude_call_utc is None:
            triggers.append("no_previous_call")
            log.debug("Trigger: no_previous_call FIRED (first call)")
        else:
            elapsed_min = (now - ts.last_claude_call_utc).total_seconds() / 60
            if elapsed_min >= self._config.claude.max_reasoning_interval_min:
                triggers.append("time_ceiling")
                log.debug(
                    "Trigger: time_ceiling FIRED  elapsed=%.1fmin threshold=%.1fmin",
                    elapsed_min, self._config.claude.max_reasoning_interval_min,
                )
            else:
                log.debug(
                    "Trigger: time_ceiling skip  elapsed=%.1fmin threshold=%.1fmin",
                    elapsed_min, self._config.claude.max_reasoning_interval_min,
                )

        # 2. Price move: any Tier 1 symbol, open position, or macro index moved
        #    beyond threshold since last eval. Threshold read from config
        #    (slow_loop_price_move_threshold_pct). SPY/QQQ/IWM included for
        #    macro awareness even though they are not entry candidates.
        #    Phase 17: uses all_indicators (set by medium loop) for a
        #    single merged lookup across watchlist + context symbols.
        price_move_threshold = self._config.scheduler.slow_loop_price_move_threshold_pct / 100
        watchlist = await state_manager.load_watchlist()
        portfolio = await state_manager.load_portfolio()
        tracked = {e.symbol for e in watchlist.entries if e.priority_tier == 1}
        tracked |= {p.symbol for p in portfolio.positions}
        tracked |= {"SPY", "QQQ", "IWM"}
        # Use the medium-loop-cached merged dict; fall back to on-demand merge for
        # tests or early startup cycles where the medium loop hasn't run yet.
        all_ind = all_indicators or {
            **latest_indicators,
            **market_context_indicators,
        }
        for symbol in tracked:
            current_price = all_ind.get(symbol, {}).get("price")
            if current_price is None:
                # Fallback: try nested signals dict (context symbol format differs)
                sig = all_ind.get(symbol, {}).get("signals", {})
                current_price = sig.get("price")
            if current_price is None:
                continue
            last_price = ts.last_prices.get(symbol)
            if last_price and abs(current_price - last_price) / last_price > price_move_threshold:
                pct_chg = (current_price - last_price) / last_price * 100
                triggers.append(f"price_move:{symbol}")
                log.debug(
                    "Trigger: price_move:%s FIRED  price=%.4f last=%.4f chg=%.2f%% threshold=%.2f%%",
                    symbol, current_price, last_price, pct_chg, price_move_threshold * 100,
                )

        # 3. Position approaching target (within 1% of profit target or stop loss)
        # near_target has a cooldown: once Claude reviews and holds, the trigger is
        # suppressed for near_target_cooldown_sec to prevent repeated firing while
        # price oscillates near the target level.
        position_indicators = latest_indicators
        near_target_cooldown = self._config.scheduler.near_target_cooldown_sec
        for pos in portfolio.positions:
            targets = pos.intention.exit_targets
            current = position_indicators.get(pos.symbol, {}).get("price")
            if current is None:
                continue
            if targets.profit_target > 0:
                pct_to_target = abs(current - targets.profit_target) / targets.profit_target
                if pct_to_target <= 0.01:
                    last_fired = ts.last_near_target_time.get(pos.symbol, 0.0)
                    if time.monotonic() - last_fired >= near_target_cooldown:
                        triggers.append(f"near_target:{pos.symbol}")
                        ts.last_near_target_time[pos.symbol] = time.monotonic()
                        log.debug(
                            "Trigger: near_target:%s FIRED  price=%.4f target=%.4f pct_away=%.2f%%",
                            pos.symbol, current, targets.profit_target, pct_to_target * 100,
                        )
                    else:
                        log.debug(
                            "Trigger: near_target:%s suppressed (cooldown %.0fs remaining)",
                            pos.symbol,
                            near_target_cooldown - (time.monotonic() - last_fired),
                        )
            if targets.stop_loss > 0:
                pct_to_stop = abs(current - targets.stop_loss) / targets.stop_loss
                if pct_to_stop <= 0.01:
                    last_fired = ts.last_near_stop_time.get(pos.symbol, 0.0)
                    if time.monotonic() - last_fired >= near_target_cooldown:
                        triggers.append(f"near_stop:{pos.symbol}")
                        ts.last_near_stop_time[pos.symbol] = time.monotonic()
                        log.debug(
                            "Trigger: near_stop:%s FIRED  price=%.4f stop=%.4f pct_away=%.2f%%",
                            pos.symbol, current, targets.stop_loss, pct_to_stop * 100,
                        )
                    else:
                        log.debug(
                            "Trigger: near_stop:%s suppressed (cooldown %.0fs remaining)",
                            pos.symbol,
                            near_target_cooldown - (time.monotonic() - last_fired),
                        )

        # 4. Override exit occurred since last Claude call
        if override_exit_count > ts.last_override_exit_count:
            triggers.append("override_exit")
            log.debug(
                "Trigger: override_exit FIRED  count=%d last_seen=%d",
                override_exit_count, ts.last_override_exit_count,
            )

        # 4b. Open position has reached a meaningful unrealised gain.
        # Fires when gain_pct crosses the configured threshold, then re-arms each time
        # gain grows by another full interval — gives Claude a chance to tighten the
        # stop progressively as the position moves in our favour.
        # Direction-aware: shorts profit when price falls below avg_cost.
        profit_threshold = self._config.scheduler.position_profit_trigger_pct
        for pos in portfolio.positions:
            current = position_indicators.get(pos.symbol, {}).get("price")
            if current is None or pos.avg_cost <= 0:
                continue
            if is_short(pos.intention.direction):
                gain_pct = (pos.avg_cost - current) / pos.avg_cost
            else:
                gain_pct = (current - pos.avg_cost) / pos.avg_cost
            # Always update peak unrealized — even when below the trigger threshold.
            ctx = entry_contexts.setdefault(pos.symbol, {})
            prev_peak = ctx.get("peak_unrealized_pct", 0.0)
            if gain_pct * 100 > prev_peak:
                ctx["peak_unrealized_pct"] = round(gain_pct * 100, 4)

            if gain_pct < profit_threshold:
                continue
            last_trigger = ts.last_profit_trigger_gain.get(pos.symbol, 0.0)
            if gain_pct >= last_trigger + profit_threshold:
                triggers.append(f"position_in_profit:{pos.symbol}")
                log.debug(
                    "Trigger: position_in_profit:%s FIRED  gain=%.2f%% last_trigger=%.2f%% interval=%.2f%%",
                    pos.symbol, gain_pct * 100, last_trigger * 100, profit_threshold * 100,
                )
                await self._trade_journal.append({
                    "record_type": "snapshot",
                    "trade_id": entry_contexts.get(pos.symbol, {}).get("trade_id"),
                    "symbol": pos.symbol,
                    "trigger": "position_in_profit",
                    "unrealized_pnl_pct": round(gain_pct * 100, 4),
                    "peak_unrealized_pct": ctx.get("peak_unrealized_pct", round(gain_pct * 100, 4)),
                    "current_price": current,
                    "stop_price": pos.intention.exit_targets.stop_loss,
                    "target_price": pos.intention.exit_targets.profit_target,
                    "strategy": pos.intention.strategy,
                    "direction": pos.intention.direction,
                    "source": "live",
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })

        # 5. Market session transition (open at 9:30 ET, approaching close at 3:30 ET)
        current_session = get_current_session()
        last_session = ts.last_session
        if last_session != current_session.value:
            if current_session == Session.REGULAR_HOURS:
                triggers.append("session_open")
                ts.approaching_close_fired = False  # re-arm for the new session
                log.debug("Trigger: session_open FIRED  prev_session=%s", last_session)
            elif current_session == Session.POST_MARKET and last_session == Session.REGULAR_HOURS:
                triggers.append("session_close")
                ts.approaching_close_fired = False  # clear for next day
                log.debug("Trigger: session_close FIRED")
            # Always update last_session regardless of whether we fire
            ts.last_session = current_session.value

        # Also fire once ~30 min before close (3:28–3:32 PM ET) while still in regular hours.
        # approaching_close_fired ensures this fires exactly once per session regardless of
        # how many slow-loop ticks fall inside the 4-minute window.
        if current_session == Session.REGULAR_HOURS and not ts.approaching_close_fired:
            from datetime import time as _time
            from zoneinfo import ZoneInfo as _ZI
            et = now.astimezone(_ZI("America/New_York"))
            if _time(15, 28) <= et.time() <= _time(15, 32):
                if "session_open" not in triggers and "time_ceiling" not in triggers:
                    triggers.append("approaching_close")
                    ts.approaching_close_fired = True
                    log.debug("Trigger: approaching_close FIRED")

        # 6. Watchlist critically small
        if len(watchlist.entries) < 10:
            triggers.append("watchlist_small")
            log.debug("Trigger: watchlist_small FIRED  size=%d", len(watchlist.entries))

        # 6b. Watchlist stale — periodic proactive refresh.
        # Fires when enough time has elapsed since the last watchlist build (both
        # watchlist_small and watchlist_stale paths update last_watchlist_build_utc).
        # interval_min = 0 disables this trigger entirely (no overhead).
        # To add a new time-based watchlist trigger: add an entry here and route it
        # through the is_watchlist_build check in _slow_loop_cycle.
        interval_min = self._config.scheduler.watchlist_refresh_interval_min
        if interval_min > 0:
            last_build = ts.last_watchlist_build_utc
            elapsed_min = (
                (now - last_build).total_seconds() / 60
                if last_build is not None
                else float("inf")  # never built → fire immediately on first tick
            )
            if elapsed_min >= interval_min:
                triggers.append("watchlist_stale")
                log.debug(
                    "Trigger: watchlist_stale FIRED  elapsed_min=%.1f  interval=%d",
                    elapsed_min, interval_min,
                )

        # 7. Indicators seeded for the first time — fire once after the first medium
        #    loop cycle so Claude always has real TA data on its first call.
        if not ts.indicators_seeded and all_indicators:
            triggers.append("indicators_ready")
            ts.indicators_seeded = True
            # Phase 17: seed last_claude_call_prices from the current snapshot so that
            # macro_move / sector_move have a valid baseline from the first call forward.
            for sym, ind in all_ind.items():
                price = ind.get("price") or ind.get("signals", {}).get("price")
                if price is not None:
                    ts.last_claude_call_prices[sym] = price

        # Phase 17 (Fix 2): Macro move trigger --------------------------------
        # Fires when any SPY/QQQ/IWM index moves beyond macro_move_trigger_pct (1%)
        # from its price at the time of the last Claude call. Uses a separate
        # last_claude_call_prices baseline (not last_prices, which resets each tick)
        # so sustained intraday moves always fire even when last_prices catches up.
        if ts.last_claude_call_prices:
            macro_move_threshold = self._config.scheduler.macro_move_trigger_pct / 100
            for sym in self._config.scheduler.macro_move_symbols:
                ind = all_ind.get(sym, {})
                current_price = ind.get("price") or ind.get("signals", {}).get("price")
                if current_price is None:
                    continue
                baseline = ts.last_claude_call_prices.get(sym)
                if baseline:
                    pct_chg = (current_price - baseline) / baseline * 100
                    if abs(pct_chg) / 100 > macro_move_threshold:
                        triggers.append(f"market_move:{sym}")
                        log.debug(
                            "Trigger: market_move:%s FIRED  price=%.4f baseline=%.4f chg=%.2f%% threshold=%.2f%%",
                            sym, current_price, baseline, pct_chg, macro_move_threshold * 100,
                        )
                    else:
                        log.debug(
                            "Trigger: market_move:%s skip  chg=%.2f%% threshold=%.2f%%",
                            sym, pct_chg, macro_move_threshold * 100,
                        )

        # Phase 17 (Fix 2): Sector move trigger --------------------------------
        # Fires when a sector ETF moves beyond sector_move_trigger_pct (1.5%) from its
        # price at the last Claude call. When the portfolio has open exposure to a sector
        # (i.e. we hold a position in a symbol that maps to that ETF), the threshold is
        # tightened by sector_exposure_threshold_factor (0.7 → 1.05% instead of 1.5%).
        if ts.last_claude_call_prices:
            sector_move_threshold = self._config.scheduler.sector_move_trigger_pct / 100
            sector_factor = self._config.scheduler.sector_exposure_threshold_factor
            # Determine which sector ETFs the portfolio has exposure to.
            # _sector_map[symbol] → sector ETF. Symbols absent from the map degrade
            # gracefully (base threshold used, not tightened threshold).
            exposed_sectors = {
                self._sector_map[pos.symbol]
                for pos in portfolio.positions
                if pos.symbol in self._sector_map
            }
            for etf in _CONTEXT_SECTOR_ETFS:
                # Skip if this ETF is a held position — price_move already covers it.
                if etf in {pos.symbol for pos in portfolio.positions}:
                    continue
                ind = all_ind.get(etf, {})
                current_price = ind.get("price") or ind.get("signals", {}).get("price")
                if current_price is None:
                    continue
                baseline = ts.last_claude_call_prices.get(etf)
                if not baseline:
                    continue
                threshold = (
                    sector_move_threshold * sector_factor
                    if etf in exposed_sectors
                    else sector_move_threshold
                )
                pct_chg = (current_price - baseline) / baseline * 100
                if abs(pct_chg) / 100 > threshold:
                    triggers.append(f"sector_move:{etf}")
                    log.debug(
                        "Trigger: sector_move:%s FIRED  price=%.4f baseline=%.4f chg=%.2f%% "
                        "threshold=%.2f%% exposed=%s",
                        etf, current_price, baseline, pct_chg, threshold * 100,
                        etf in exposed_sectors,
                    )
                else:
                    log.debug(
                        "Trigger: sector_move:%s skip  chg=%.2f%% threshold=%.2f%% exposed=%s",
                        etf, pct_chg, threshold * 100, etf in exposed_sectors,
                    )

        # Phase 17 (Fix 2): Market RSI extreme trigger -------------------------
        # Fires when SPY RSI crosses into panic (< macro_rsi_panic_threshold) or
        # euphoria (> macro_rsi_euphoria_threshold) territory. Re-arm band prevents
        # rapid re-firing: once triggered, RSI must recover by macro_rsi_rearm_band
        # points before the trigger can fire again.
        # Note: the TA module key is "rsi" (not "rsi_14" as spec draft said).
        spy_sig = all_ind.get("SPY", {})
        spy_rsi = (spy_sig.get("signals") or spy_sig).get("rsi")
        if spy_rsi is not None:
            cfg_s = self._config.scheduler
            # Panic (low RSI): market selloff
            if spy_rsi < cfg_s.macro_rsi_panic_threshold and not ts.rsi_extreme_fired_low:
                triggers.append("market_rsi_extreme")
                ts.rsi_extreme_fired_low = True
                log.debug(
                    "Trigger: market_rsi_extreme FIRED (panic)  spy_rsi=%.1f threshold=%.1f",
                    spy_rsi, cfg_s.macro_rsi_panic_threshold,
                )
            elif spy_rsi > cfg_s.macro_rsi_panic_threshold + cfg_s.macro_rsi_rearm_band:
                if ts.rsi_extreme_fired_low:
                    log.debug(
                        "Trigger: market_rsi_extreme re-armed (panic)  spy_rsi=%.1f", spy_rsi,
                    )
                ts.rsi_extreme_fired_low = False  # re-arm when RSI recovers
            # Euphoria (high RSI): market overheating
            if spy_rsi > cfg_s.macro_rsi_euphoria_threshold and not ts.rsi_extreme_fired_high:
                triggers.append("market_rsi_extreme")
                ts.rsi_extreme_fired_high = True
                log.debug(
                    "Trigger: market_rsi_extreme FIRED (euphoria)  spy_rsi=%.1f threshold=%.1f",
                    spy_rsi, cfg_s.macro_rsi_euphoria_threshold,
                )
            elif spy_rsi < cfg_s.macro_rsi_euphoria_threshold - cfg_s.macro_rsi_rearm_band:
                if ts.rsi_extreme_fired_high:
                    log.debug(
                        "Trigger: market_rsi_extreme re-armed (euphoria)  spy_rsi=%.1f", spy_rsi,
                    )
                ts.rsi_extreme_fired_high = False  # re-arm when RSI normalises
        else:
            log.debug("Trigger: market_rsi_extreme skip — SPY RSI unavailable")

        # 9. Candidates exhausted — all current Claude recommendations suppressed.
        # Fires at most once per reasoning cache generation: guarded by
        # last_exhaustion_trigger_utc so a new reasoning cycle must complete before
        # this can fire again (prevents rapid-fire if the new cycle also produces
        # immediately-suppressable candidates).
        if ts.candidates_exhausted:
            last_exhausted = ts.last_exhaustion_trigger_utc
            last_call = ts.last_claude_call_utc
            already_fired_this_generation = (
                last_exhausted is not None
                and last_call is not None
                and last_exhausted >= last_call
            )
            if not already_fired_this_generation:
                triggers.append("candidates_exhausted")
                ts.candidates_exhausted = False
                ts.last_exhaustion_trigger_utc = now
                log.debug("Trigger: candidates_exhausted FIRED")

        # Phase 19: regime condition trigger — check valid_until_conditions from last
        # Sonnet regime_assessment. When a condition is met, fire a fresh reasoning cycle.
        # _run_claude_cycle uses skip_cache=True so no explicit cache expiry needed.
        # Cooldown prevents chain-firing when a condition is miscalibrated (already met) or
        # the signal oscillates. Mirrors the approaching_close_fired guard pattern.
        _regime_cooldown_sec = self._config.scheduler.regime_condition_cooldown_min * 60
        _regime_elapsed = (
            (now - ts.last_regime_condition_utc).total_seconds()
            if ts.last_regime_condition_utc else float("inf")
        )
        if self.check_regime_conditions(
            last_regime_assessment=last_regime_assessment,
            daily_indicators=daily_indicators,
        ):
            if _regime_elapsed >= _regime_cooldown_sec:
                triggers.append("regime_condition")
                ts.last_regime_condition_utc = now
                log.debug("Trigger: regime_condition FIRED  elapsed=%.0fs", _regime_elapsed)
            else:
                log.debug(
                    "Trigger: regime_condition suppressed — cooldown %.0fs remaining",
                    _regime_cooldown_sec - _regime_elapsed,
                )

        return triggers

    def check_regime_conditions(
        self,
        *,
        last_regime_assessment: dict | None,
        daily_indicators: dict,
    ) -> bool:
        """Phase 19: check regime_assessment.valid_until_conditions against live indicators.

        Returns True if any condition is now met — caller should append
        "regime_condition" to the trigger list, which causes a fresh reasoning call
        (_run_claude_cycle already uses skip_cache=True, so no explicit expiry needed).

        Conditions are parsed via simple regex — not LLM evaluation.
        Unknown condition formats are logged at DEBUG and ignored.

        Supported formats:
          "SPY daily RSI > N"  / "SPY daily RSI < N"
          "daily_trend == uptrend"  / "daily_trend == downtrend"

        To add a new condition key: add a branch below and update Phase 19 prompt docs.
        """
        if not last_regime_assessment:
            return False
        conditions = last_regime_assessment.get("valid_until_conditions")
        if not conditions or not isinstance(conditions, list):
            return False

        spy_daily = daily_indicators.get("SPY", {})
        spy_rsi_daily = spy_daily.get("rsi_14d")
        spy_trend_daily = spy_daily.get("daily_trend")

        for cond in conditions:
            if not isinstance(cond, str):
                continue
            triggered = False
            # "SPY daily RSI > N"
            m = re.match(r"SPY daily RSI\s*>\s*(\d+(?:\.\d+)?)", cond, re.IGNORECASE)
            if m and spy_rsi_daily is not None:
                triggered = float(spy_rsi_daily) > float(m.group(1))
            # "SPY daily RSI < N"
            m = re.match(r"SPY daily RSI\s*<\s*(\d+(?:\.\d+)?)", cond, re.IGNORECASE)
            if m and spy_rsi_daily is not None:
                triggered = float(spy_rsi_daily) < float(m.group(1))
            # "daily_trend == uptrend" / "daily_trend == downtrend"
            m = re.match(r"daily_trend\s*==\s*(\w+)", cond, re.IGNORECASE)
            if m and spy_trend_daily is not None:
                triggered = spy_trend_daily.lower() == m.group(1).lower()

            if triggered:
                log.info("Regime condition met — triggering fresh reasoning: %s", cond)
                return True
            else:
                log.debug("Regime condition not yet met: %s", cond)
        return False

    def update_trigger_prices(self, all_indicators: dict) -> None:
        """Snapshot current prices into last_prices for next trigger comparison."""
        # Phase 17: use all_indicators (already merged by medium loop).
        for symbol, ind in all_indicators.items():
            price = ind.get("price")
            if price is None:
                price = ind.get("signals", {}).get("price")
            if price is not None:
                self.state.last_prices[symbol] = price
