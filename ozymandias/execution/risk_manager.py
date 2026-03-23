"""
Risk manager — pre-trade validation, quantitative override signals, daily loss
tracking, and position sizing.

Has override authority over all other modules. Called before every order
placement (validate_entry) and on every fast-loop cycle for each open
position (evaluate_overrides).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from datetime import time as dtime

from ozymandias.core.config import RiskConfig, SchedulerConfig
from ozymandias.core.direction import is_short
from ozymandias.core.market_hours import Session, get_current_session, is_last_five_minutes
from ozymandias.core.state_manager import OrderRecord, PortfolioState, Position
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.execution.pdt_guard import PDTGuard

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Override signal thresholds (per spec §4.7)
# _VWAP_VOLUME_RATIO_THRESHOLD and _ATR_TRAILING_STOP_MULTIPLIER removed —
# now passed as kwargs from orchestrator using per-strategy values from
# strategy.override_vwap_volume_threshold() and strategy.override_atr_multiplier().
_MOMENTUM_SCORE_STRONG_THRESHOLD: float = 1.5
_MIN_AVG_DAILY_DOLLAR_VOLUME: int = 10_000_000  # configured in config.json ranker.min_avg_daily_dollar_volume


def _et_date_str(date_str: str) -> date | None:
    """Parse an ISO date/datetime string and return the ET calendar date, or None."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            return dt.date()
        return dt.astimezone(ET).date()
    except (ValueError, AttributeError):
        # Plain date string like "2026-03-17"
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            return None


def _pending_order_commitment(orders: list[OrderRecord]) -> float:
    """
    Sum of capital committed by pending limit orders.

    Mirrors the logic in FillProtectionManager.available_buying_power without
    requiring a FillProtectionManager instance. Market orders are excluded
    because their cost is unknown until fill.
    """
    total = 0.0
    for o in orders:
        if o.status in ("PENDING", "PARTIALLY_FILLED") and o.side == "buy":
            if o.order_type == "limit" and o.limit_price is not None:
                remaining = o.quantity - o.filled_quantity
                total += remaining * o.limit_price
    return total


class RiskManager:
    """
    Central risk authority. Enforces hard rules, computes quantitative
    override signals, tracks daily P&L, and sizes positions.

    Usage::

        rm = RiskManager(cfg.risk, pdt_guard)

        allowed, reason = rm.validate_entry(
            "AAPL", "buy", 10, 175.0, True,   # blocks_eod_entries=strategy_obj.blocks_eod_entries
            account, portfolio, orders,
        )
        should_exit, signals = rm.evaluate_overrides(position, indicators, intraday_high)
        shares = rm.calculate_position_size("AAPL", 175.0, 3.5, 50_000.0)
    """

    def __init__(
        self,
        config: RiskConfig,
        pdt_guard: PDTGuard,
        scheduler: SchedulerConfig | None = None,
    ) -> None:
        self._cfg = config
        self._pdt = pdt_guard
        # Dead zone: parse HH:MM strings from scheduler config
        sched = scheduler or SchedulerConfig()
        self._dead_zone_start: dtime = dtime.fromisoformat(sched.dead_zone_start_et)
        self._dead_zone_end: dtime = dtime.fromisoformat(sched.dead_zone_end_et)
        self._bypass_market_hours: bool = sched.bypass_market_hours  # skip all session/dead-zone checks when True
        # Previous momentum scores per symbol (for flip detection)
        self._prev_momentum_scores: dict[str, float] = {}
        # Daily loss state
        self._daily_start_equity: float = 0.0
        self._daily_loss_date: date | None = None
        self._trading_halted_until: date | None = None

    # ------------------------------------------------------------------
    # Pre-trade validation
    # ------------------------------------------------------------------

    def validate_entry(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        blocks_eod_entries: bool,
        account: AccountInfo,
        portfolio: PortfolioState,
        orders: list[OrderRecord],
        avg_daily_volume: float | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """
        Run all pre-trade checks. Return (allowed, reason).

        First failing check is returned; remaining checks are skipped.

        Args:
            symbol:             Ticker symbol.
            side:               "buy" or "sell".
            quantity:           Number of shares.
            price:              Estimated fill price (limit price or last quote).
            blocks_eod_entries: True if this strategy blocks entries in the last
                                5 minutes of the session (i.e. is_intraday).
                                Pass ``strategy_obj.blocks_eod_entries``.
            account:            Current account snapshot from broker.
            portfolio:          Local portfolio state.
            orders:             All known order records (for PDT and buying-power checks).
            avg_daily_volume:   Stock's average daily volume from fundamentals.
                                Skip the min-volume check if None.
            now:                Override current time (for testing).
        """
        now = now or datetime.now(ET)

        # 1. Equity floor (PDT guard)
        floor_ok, floor_msg = self._pdt.check_equity_floor(account)
        if not floor_ok:
            return False, floor_msg

        # 2. Daily loss halt
        halted, halt_msg = self.check_daily_loss(account, now=now)
        if halted:
            return False, halt_msg

        # 3. Position size: would this exceed max_position_pct of portfolio equity?
        position_value = quantity * price
        portfolio_value = account.equity
        if portfolio_value > 0 and position_value > portfolio_value * self._cfg.max_position_pct:
            return False, (
                f"Position size ${position_value:,.2f} would exceed "
                f"{self._cfg.max_position_pct * 100:.0f}% of portfolio "
                f"(equity ${portfolio_value:,.2f})"
            )

        # 4. Concurrent positions limit
        open_count = len(portfolio.positions)
        if open_count >= self._cfg.max_concurrent_positions:
            return False, (
                f"At max concurrent positions "
                f"({open_count}/{self._cfg.max_concurrent_positions})"
            )

        # 5. Market hours
        hours_ok, hours_msg = self._check_market_hours(blocks_eod_entries, now)
        if not hours_ok:
            return False, hours_msg

        # 6. PDT day-trade check — guards against opening a second position on a
        # symbol that already has a position open today (which, when closed same-day,
        # would count as two round-trips on the same symbol in one session).
        # NOTE: closing orders do NOT go through validate_entry; the pdt_buffer
        # reserves one slot for exits so they are never blocked. This check only
        # fires for the edge case of re-entering a symbol same-day — which the
        # recently_closed guard normally prevents.
        # NOTE: side == "sell" covers long closes only; short closes (side="buy")
        # are not reachable here anyway since exits bypass validate_entry.
        if account.equity < self._cfg.min_equity_for_trading:
            today_et = now.astimezone(ET).date()
            opened_today = any(
                p.symbol == symbol and _et_date_str(p.entry_date) == today_et
                for p in portfolio.positions
            )
            if side == "sell" and opened_today:
                pdt_ok, pdt_msg = self._pdt.can_day_trade(symbol, orders, portfolio)
                if not pdt_ok:
                    return False, pdt_msg

        # 7. Buying power
        if side == "buy":
            committed = _pending_order_commitment(orders)
            available = account.buying_power - committed
            if position_value > available:
                return False, (
                    f"Insufficient buying power: need ${position_value:,.2f}, "
                    f"available ${available:,.2f} "
                    f"(reported ${account.buying_power:,.2f} minus "
                    f"${committed:,.2f} in pending orders)"
                )

        # 8. Minimum average daily dollar volume (ticker-agnostic liquidity check)
        if avg_daily_volume is not None and price > 0:
            avg_dollar_vol = avg_daily_volume * price
            if avg_dollar_vol < _MIN_AVG_DAILY_DOLLAR_VOLUME:
                return False, (
                    f"{symbol} avg daily dollar volume ${avg_dollar_vol:,.0f} "
                    f"< ${_MIN_AVG_DAILY_DOLLAR_VOLUME:,} minimum"
                )

        return True, "All risk checks passed"

    def _check_market_hours(self, blocks_eod_entries: bool, now: datetime) -> tuple[bool, str]:
        """Block entries in non-regular sessions; block all entries in dead zone;
        block intraday strategies (blocks_eod_entries=True) in last 5 minutes."""
        if self._bypass_market_hours:
            return True, "Market hours bypass active"
        session = get_current_session(now)
        if session != Session.REGULAR_HOURS:
            return False, (
                f"Market not in regular hours (session={session.value}) — "
                f"new entries blocked"
            )
        # Dead zone: no new entries during midday low-volume window
        et = now.astimezone(ET)
        t = et.time()
        if self._dead_zone_start <= t < self._dead_zone_end:
            return False, (
                f"Dead zone active ({self._dead_zone_start.strftime('%H:%M')}–"
                f"{self._dead_zone_end.strftime('%H:%M')} ET) "
                "— new entries suspended"
            )
        if blocks_eod_entries and is_last_five_minutes(now):
            return False, (
                "Entry blocked — last 5 minutes of regular session (3:55–4:00 PM ET)"
            )
        return True, "Market hours OK"

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        atr: float,
        account_value: float,
        risk_per_trade_pct: float = 0.01,
        atr_multiplier: float = 2.0,
    ) -> int:
        """
        ATR-based position sizing formula from spec §4.7:

            shares = (account_value * risk_per_trade_pct) / (atr * atr_multiplier)

        Capped at max_position_pct of portfolio / entry_price.
        Returns integer shares (minimum 0).

        Args:
            symbol:            Ticker (for logging).
            entry_price:       Estimated entry price per share.
            atr:               14-period ATR value.
            account_value:     Total account equity.
            risk_per_trade_pct: Fraction of account to risk per trade (default 1%).
            atr_multiplier:    ATR stop distance multiplier (default 2.0).
        """
        if atr <= 0 or entry_price <= 0 or account_value <= 0:
            log.warning(
                "calculate_position_size(%s): invalid inputs "
                "atr=%.4f entry_price=%.2f account_value=%.2f",
                symbol, atr, entry_price, account_value,
            )
            return 0

        raw_shares = (account_value * risk_per_trade_pct) / (atr * atr_multiplier)
        max_shares = (account_value * self._cfg.max_position_pct) / entry_price
        shares = min(raw_shares, max_shares)
        result = max(0, int(shares))

        log.debug(
            "calculate_position_size(%s): raw=%.1f cap=%.1f → %d shares "
            "(acct_value=%.0f risk_pct=%.3f atr=%.4f mult=%.1f)",
            symbol, raw_shares, max_shares, result,
            account_value, risk_per_trade_pct, atr, atr_multiplier,
        )
        return result

    # ------------------------------------------------------------------
    # Quantitative override signals
    # ------------------------------------------------------------------

    def check_vwap_crossover(
        self,
        position: Position,
        indicators: dict,
        *,
        direction: str,
        volume_threshold: float,
    ) -> bool:
        """
        Return True if price crossed VWAP on above-average volume.

        Long fires when vwap_position == "below"; short fires when == "above".
        Because overrides run every fast-loop cycle (~10s), detecting "price is
        currently on the adverse side of VWAP" is effectively equivalent to
        detecting a crossover within one loop cycle.
        """
        vwap_pos = indicators.get("vwap_position", "at")
        vol_ratio = indicators.get("volume_ratio", 0.0)
        adverse_side = "above" if is_short(direction) else "below"
        triggered = vwap_pos == adverse_side and vol_ratio > volume_threshold
        if triggered:
            log.info(
                "Override check — VWAP crossover: %s %s VWAP, volume_ratio=%.2f (dir=%s)",
                position.symbol, adverse_side, vol_ratio, direction,
            )
        return triggered

    def check_rsi_divergence(
        self,
        position: Position,
        indicators: dict,
        *,
        direction: str,
    ) -> bool:
        """
        Return True if RSI divergence adverse to the position direction is detected.

        Long fires on "bearish" divergence; short fires on "bullish" divergence.

        NOTE: This signal cannot trigger an exit alone — requires at least one
        other signal to also be active. Enforced in evaluate_overrides().
        """
        adverse_divergence = "bullish" if is_short(direction) else "bearish"
        return indicators.get("rsi_divergence") == adverse_divergence

    def check_roc_deceleration(
        self,
        position: Position,
        indicators: dict,
        *,
        direction: str,
    ) -> bool:
        """
        Return True if ROC is decelerating in the direction adverse to the position.

        Long uses the ``roc_deceleration`` flag (roc > 0 and roc < prev_roc).
        Short uses the ``roc_negative_deceleration`` flag (roc < 0 and roc > prev_roc).
        """
        if is_short(direction):
            return bool(indicators.get("roc_negative_deceleration", False))
        return bool(indicators.get("roc_deceleration", False))

    def check_momentum_score_flip(
        self,
        position: Position,
        indicators: dict,
        *,
        direction: str,
    ) -> bool:
        """
        Return True if the momentum score (roc_5 * volume_ratio) has flipped sign
        after being strongly in the position's favour.

        Long: fires when prev_score > +1.5 and current_score < 0.
        Short: fires when prev_score < -1.5 and current_score > 0.

        Stores the previous score per symbol. Requires two consecutive calls to
        detect a flip; returns False on the first call for a given symbol.
        """
        roc = indicators.get("roc_5", 0.0)
        vol_ratio = indicators.get("volume_ratio", 1.0)
        current_score = roc * vol_ratio
        symbol = position.symbol

        prev_score = self._prev_momentum_scores.get(symbol)
        self._prev_momentum_scores[symbol] = current_score

        if prev_score is None:
            return False  # no prior data to compare against

        if is_short(direction):
            flipped = prev_score < -_MOMENTUM_SCORE_STRONG_THRESHOLD and current_score > 0
        else:
            flipped = prev_score > _MOMENTUM_SCORE_STRONG_THRESHOLD and current_score < 0
        if flipped:
            log.info(
                "Override check — Momentum score flip: %s score %.2f → %.2f (dir=%s)",
                symbol, prev_score, current_score, direction,
            )
        return flipped

    def check_atr_trailing_stop(
        self,
        position: Position,
        indicators: dict,
        intraday_extremum: float,
        *,
        direction: str,
        atr_multiplier: float,
    ) -> bool:
        """
        Return True if price has moved adversely more than atr_multiplier × ATR(14)
        from the intraday extremum.

        Long: drop = intraday_HIGH − price > atr_multiplier × ATR.
        Short: rise = price − intraday_LOW > atr_multiplier × ATR.

        ``intraday_extremum`` is the session HIGH for longs and session LOW for shorts.
        Caller (orchestrator) is responsible for tracking and passing the correct value.

        Returns False if price or ATR is missing or ATR is zero.
        """
        price = indicators.get("price")
        atr = indicators.get("atr_14", 0.0)
        if price is None or atr <= 0:
            return False
        if is_short(direction):
            move = price - intraday_extremum
        else:
            move = intraday_extremum - price
        triggered = move > atr_multiplier * atr
        if triggered:
            log.info(
                "Override check — ATR trailing stop: %s move=%.2f > %.1f×ATR=%.2f "
                "(extremum=%.2f price=%.2f dir=%s)",
                position.symbol, move, atr_multiplier, atr,
                intraday_extremum, price, direction,
            )
        return triggered

    def check_hard_stop(self, position: Position, indicators: dict) -> bool:
        """Short-only. Fires when price >= stop_loss. Bypasses allow_signals gating.

        Long stops are managed by broker limit orders, not polled here.
        """
        if not is_short(position.intention.direction):
            return False
        stop_loss = position.intention.exit_targets.stop_loss
        price = indicators.get("price")
        if stop_loss <= 0 or price is None:
            return False
        triggered = price >= stop_loss
        if triggered:
            log.warning(
                "Hard stop hit (short): %s price=%.4f >= stop=%.4f",
                position.symbol, price, stop_loss,
            )
        return triggered

    def evaluate_overrides(
        self,
        position: Position,
        indicators: dict,
        intraday_extremum: float,
        allow_signals: frozenset[str] | None = None,
        *,
        direction: str | None = None,
        atr_multiplier: float = 2.0,
        vwap_volume_threshold: float = 1.3,
    ) -> tuple[bool, list[str]]:
        """
        Evaluate quantitative override signals and apply trigger logic.

        Trigger logic per spec §4.7:
          - Signals 1 (VWAP), 3 (ROC), 4 (momentum flip), 5 (ATR stop):
            trigger independently.
          - Signal 2 (RSI divergence): requires at least one other signal active.

        Parameters
        ----------
        intraday_extremum:
            Session HIGH for long positions; session LOW for shorts. Caller
            (orchestrator) tracks and passes the correct value per direction.
        allow_signals:
            Optional set of signal names to evaluate. Signals not in this set
            are skipped regardless of their computed value. None means all signals
            are evaluated. Pass ``strategy.applicable_override_signals()`` to
            restrict to the signals relevant for the position's strategy type.
        direction:
            Position direction ("long"/"short"). Defaults to
            ``position.intention.direction`` when None.
        atr_multiplier:
            ATR trailing stop multiplier. Pass ``strategy.override_atr_multiplier()``.
        vwap_volume_threshold:
            Volume ratio floor for VWAP crossover. Pass
            ``strategy.override_vwap_volume_threshold()``.

        Returns:
            (should_exit: bool, triggered_signals: list[str])
        """
        pos_direction = direction or position.intention.direction

        def _allowed(name: str) -> bool:
            return allow_signals is None or name in allow_signals

        triggered: list[str] = []
        rsi_div_active = _allowed("rsi_divergence") and self.check_rsi_divergence(
            position, indicators, direction=pos_direction
        )

        if _allowed("vwap_crossover") and self.check_vwap_crossover(
            position, indicators, direction=pos_direction, volume_threshold=vwap_volume_threshold
        ):
            triggered.append("vwap_crossover")
        if _allowed("roc_deceleration") and self.check_roc_deceleration(
            position, indicators, direction=pos_direction
        ):
            triggered.append("roc_deceleration")
        if _allowed("momentum_score_flip") and self.check_momentum_score_flip(
            position, indicators, direction=pos_direction
        ):
            triggered.append("momentum_score_flip")
        if _allowed("atr_trailing_stop") and self.check_atr_trailing_stop(
            position, indicators, intraday_extremum,
            direction=pos_direction, atr_multiplier=atr_multiplier,
        ):
            triggered.append("atr_trailing_stop")

        # RSI divergence requires at least one other signal
        if rsi_div_active and len(triggered) >= 1:
            triggered.append("rsi_divergence")

        should_exit = len(triggered) > 0
        if should_exit:
            log.warning(
                "Override exit triggered for %s: signals=%s direction=%s",
                position.symbol, triggered, pos_direction,
            )
        return should_exit, triggered

    # ------------------------------------------------------------------
    # Daily loss tracking
    # ------------------------------------------------------------------

    def initialize_daily_tracking(self, account: AccountInfo) -> None:
        """Seed the daily loss baseline with the startup equity snapshot.

        Call once from startup_reconciliation before any orders are placed.
        Ensures the daily loss limit is measured from a known clean baseline,
        not from the equity at first entry attempt (which may differ due to
        open position P&L movement).
        """
        et_today = datetime.now(ET).date()
        self._daily_loss_date = et_today
        self._daily_start_equity = account.equity
        log.info(
            "Daily loss tracker initialized at startup: %s (equity=$%.2f)",
            et_today, account.equity,
        )

    def _reset_daily_if_needed(self, account: AccountInfo, today: date) -> None:
        """Reset daily tracking when the calendar date changes."""
        if self._daily_loss_date != today:
            self._daily_loss_date = today
            self._daily_start_equity = account.equity
            self._trading_halted_until = None
            log.info(
                "Daily loss tracker reset for %s (start equity=$%.2f)",
                today, account.equity,
            )

    def check_daily_loss(
        self,
        account: AccountInfo,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """
        Return (trading_halted, reason) if daily loss exceeds the threshold.

        Automatically resets on a new trading day (ET date change).
        Should be called before every order placement.

        Args:
            account: Current account info from broker.
            now:     Override current time (for testing).
        """
        now = now or datetime.now(ET)
        today = now.date()
        self._reset_daily_if_needed(account, today)

        if self._trading_halted_until == today:
            loss = account.equity - self._daily_start_equity
            loss_pct = loss / self._daily_start_equity * 100 if self._daily_start_equity > 0 else 0.0
            return True, (
                f"Trading halted for today ({today}): "
                f"daily loss {loss_pct:.2f}% "
                f"(limit: -{self._cfg.max_daily_loss_pct * 100:.1f}%)"
            )

        if self._daily_start_equity <= 0:
            return False, "Daily loss tracker not yet initialized"

        loss = account.equity - self._daily_start_equity
        loss_pct = loss / self._daily_start_equity

        if loss_pct < -self._cfg.max_daily_loss_pct:
            self._trading_halted_until = today
            reason = (
                f"Daily loss limit hit: {loss_pct * 100:.2f}% "
                f"(limit: -{self._cfg.max_daily_loss_pct * 100:.1f}%, "
                f"start equity: ${self._daily_start_equity:,.2f})"
            )
            log.warning("DAILY LOSS HALT: %s", reason)
            return True, reason

        return False, (
            f"Daily P&L: ${loss:+,.2f} ({loss_pct * 100:+.2f}%) — "
            f"within -{self._cfg.max_daily_loss_pct * 100:.1f}% limit"
        )

    # ------------------------------------------------------------------
    # Settlement tracking (GFV guard, section 7.3)
    # ------------------------------------------------------------------

    def check_settlement(
        self,
        symbol: str,
        portfolio: PortfolioState,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        """
        Check whether selling ``symbol`` risks a Good Faith Violation (GFV).

        Equities settle T+1 business days. For margin accounts, Alpaca handles
        settlement internally, so this is defensive logging only — does NOT
        hard-block the trade. Returns (potential_gfv_risk: bool, message: str).

        Args:
            symbol:    Ticker to check.
            portfolio: Local portfolio state containing position entry dates.
            now:       Override current time (for testing).
        """
        now = now or datetime.now(ET)
        today = now.date()

        position = next((p for p in portfolio.positions if p.symbol == symbol), None)
        if position is None:
            return False, f"No open position in {symbol}"

        try:
            entry_date = date.fromisoformat(position.entry_date)
        except (ValueError, AttributeError, TypeError):
            return False, (
                f"Cannot parse entry_date for {symbol}: {position.entry_date!r}"
            )

        # T+1 settlement: advance one calendar day, skip weekends
        settlement_date = entry_date + timedelta(days=1)
        while settlement_date.weekday() >= 5:  # 5=Sat, 6=Sun
            settlement_date += timedelta(days=1)

        if today < settlement_date:
            msg = (
                f"GFV risk: {symbol} purchased {entry_date}, "
                f"settles {settlement_date} (T+1), today is {today}. "
                f"Selling before settlement may cause a Good Faith Violation."
            )
            log.warning(msg)
            return True, msg

        return False, f"{symbol} settled on {settlement_date}"
