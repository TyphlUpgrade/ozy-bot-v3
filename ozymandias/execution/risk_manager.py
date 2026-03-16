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
from ozymandias.core.market_hours import Session, get_current_session, is_last_five_minutes
from ozymandias.core.state_manager import OrderRecord, PortfolioState, Position
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.execution.pdt_guard import PDTGuard

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Override signal thresholds (per spec §4.7)
_VWAP_VOLUME_RATIO_THRESHOLD: float = 1.3
_MOMENTUM_SCORE_STRONG_THRESHOLD: float = 1.5
_ATR_TRAILING_STOP_MULTIPLIER: float = 2.0
_MIN_AVG_DAILY_VOLUME: int = 100_000


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
            "AAPL", "buy", 10, 175.0, "momentum",
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
        strategy: str,
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
            symbol:            Ticker symbol.
            side:              "buy" or "sell".
            quantity:          Number of shares.
            price:             Estimated fill price (limit price or last quote).
            strategy:          "momentum" or "swing" — affects hours check.
            account:           Current account snapshot from broker.
            portfolio:         Local portfolio state.
            orders:            All known order records (for PDT and buying-power checks).
            avg_daily_volume:  Stock's average daily volume from fundamentals.
                               Skip the min-volume check if None.
            now:               Override current time (for testing).
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
        hours_ok, hours_msg = self._check_market_hours(strategy, now)
        if not hours_ok:
            return False, hours_msg

        # 6. PDT day-trade check (only applies if selling a symbol already held)
        if side == "sell" and any(p.symbol == symbol for p in portfolio.positions):
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

        # 8. Minimum average daily volume
        if avg_daily_volume is not None and avg_daily_volume < _MIN_AVG_DAILY_VOLUME:
            return False, (
                f"{symbol} avg daily volume {avg_daily_volume:,.0f} "
                f"< {_MIN_AVG_DAILY_VOLUME:,} share minimum"
            )

        return True, "All risk checks passed"

    def _check_market_hours(self, strategy: str, now: datetime) -> tuple[bool, str]:
        """Block entries in non-regular sessions; block all entries in dead zone;
        block momentum in last 5 minutes."""
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
        if strategy == "momentum" and is_last_five_minutes(now):
            return False, (
                "Momentum entries blocked in last 5 minutes of regular session "
                "(3:55–4:00 PM ET)"
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

    def check_vwap_crossover(self, position: Position, indicators: dict) -> bool:
        """
        Return True if price is below VWAP on above-average volume (ratio > 1.3).

        Because overrides run every fast-loop cycle (~10s), detecting "price is
        currently below VWAP" is effectively equivalent to detecting a crossover
        within one loop cycle.
        """
        vwap_pos = indicators.get("vwap_position", "at")
        vol_ratio = indicators.get("volume_ratio", 0.0)
        triggered = vwap_pos == "below" and vol_ratio > _VWAP_VOLUME_RATIO_THRESHOLD
        if triggered:
            log.info(
                "Override check — VWAP crossover: %s below VWAP, volume_ratio=%.2f",
                position.symbol, vol_ratio,
            )
        return triggered

    def check_rsi_divergence(self, position: Position, indicators: dict) -> bool:
        """
        Return True if bearish RSI divergence is detected.

        NOTE: This signal cannot trigger an exit alone — requires at least one
        other signal to also be active. Enforced in evaluate_overrides().
        """
        return indicators.get("rsi_divergence") == "bearish"

    def check_roc_deceleration(self, position: Position, indicators: dict) -> bool:
        """
        Return True if 5-period ROC is decelerating while price still rising.

        Delegates to the pre-computed ``roc_deceleration`` flag from
        generate_signal_summary(), which checks: roc > 0 and roc < prev_roc.
        """
        return bool(indicators.get("roc_deceleration", False))

    def check_momentum_score_flip(self, position: Position, indicators: dict) -> bool:
        """
        Return True if the momentum score (roc_5 * volume_ratio) has flipped sign
        after being strongly positive (> 1.5) or strongly negative (< -1.5).

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

        flipped = (
            prev_score > _MOMENTUM_SCORE_STRONG_THRESHOLD and current_score < 0
        ) or (
            prev_score < -_MOMENTUM_SCORE_STRONG_THRESHOLD and current_score > 0
        )
        if flipped:
            log.info(
                "Override check — Momentum score flip: %s score %.2f → %.2f",
                symbol, prev_score, current_score,
            )
        return flipped

    def check_atr_trailing_stop(
        self,
        position: Position,
        indicators: dict,
        intraday_high: float,
    ) -> bool:
        """
        Return True if price has dropped more than 2x ATR(14) from the intraday high.

        Requires ``indicators["price"]`` (current price) and ``indicators["atr_14"]``.
        Returns False if either value is missing or ATR is zero.
        """
        price = indicators.get("price")
        atr = indicators.get("atr_14", 0.0)
        if price is None or atr <= 0:
            return False
        drop = intraday_high - price
        triggered = drop > _ATR_TRAILING_STOP_MULTIPLIER * atr
        if triggered:
            log.info(
                "Override check — ATR trailing stop: %s drop=%.2f > 2×ATR=%.2f "
                "(high=%.2f price=%.2f)",
                position.symbol, drop, atr, intraday_high, price,
            )
        return triggered

    def evaluate_overrides(
        self,
        position: Position,
        indicators: dict,
        intraday_high: float,
    ) -> tuple[bool, list[str]]:
        """
        Evaluate all quantitative override signals and apply trigger logic.

        Trigger logic per spec §4.7:
          - Signals 1 (VWAP), 3 (ROC), 4 (momentum flip), 5 (ATR stop):
            trigger independently.
          - Signal 2 (RSI divergence): requires at least one other signal active.

        Returns:
            (should_exit: bool, triggered_signals: list[str])
        """
        triggered: list[str] = []
        rsi_div_active = self.check_rsi_divergence(position, indicators)

        if self.check_vwap_crossover(position, indicators):
            triggered.append("vwap_crossover")
        if self.check_roc_deceleration(position, indicators):
            triggered.append("roc_deceleration")
        if self.check_momentum_score_flip(position, indicators):
            triggered.append("momentum_score_flip")
        if self.check_atr_trailing_stop(position, indicators, intraday_high):
            triggered.append("atr_trailing_stop")

        # RSI divergence requires at least one other signal
        if rsi_div_active and len(triggered) >= 1:
            triggered.append("rsi_divergence")

        should_exit = len(triggered) > 0
        if should_exit:
            log.warning(
                "Override exit triggered for %s: signals=%s",
                position.symbol, triggered,
            )
        return should_exit, triggered

    # ------------------------------------------------------------------
    # Daily loss tracking
    # ------------------------------------------------------------------

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
