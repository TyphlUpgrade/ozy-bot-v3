"""
Pattern Day Trader (PDT) protection.

Tracks rolling 5-business-day day-trade count and enforces the 3-day-trade
limit with a configurable buffer reserved for emergency exits.

Definitions:
  - Day trade: opening and closing the same position on the same ET calendar day.
    Determined by examining filled buy and sell orders for the same symbol on
    the same ET date.
  - Business day: Monday–Friday. Holiday calendar is not yet implemented
    (deferred per phases/01_scaffolding.md §6).
  - Rolling window: the current ET date plus the 4 preceding business days
    (5 business days total).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ozymandias.core.config import RiskConfig
from ozymandias.core.state_manager import OrderRecord, PortfolioState
from ozymandias.execution.broker_interface import AccountInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# FINRA PDT limit
_PDT_MAX_DAY_TRADES = 3


def _et_date(dt_iso: str) -> date | None:
    """Parse an ISO datetime string and return the ET calendar date, or None."""
    if not dt_iso:
        return None
    try:
        dt = datetime.fromisoformat(dt_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).date()
    except (ValueError, AttributeError):
        return None


def _business_days_window(today: date, n: int = 5) -> set[date]:
    """
    Return the set of the last ``n`` business days (Mon–Fri), including today.
    Does not account for market holidays (deferred).
    """
    days: set[date] = set()
    current = today
    while len(days) < n:
        if current.weekday() < 5:  # 0=Mon, 4=Fri
            days.add(current)
        current -= timedelta(days=1)
    return days


class PDTGuard:
    """
    Enforces Pattern Day Trader limits.

    Usage::

        guard = PDTGuard(cfg.risk)
        count = guard.count_day_trades(orders, portfolio)
        allowed, reason = guard.can_day_trade("AAPL", orders, portfolio)
        allowed, reason = guard.check_equity_floor(account_info)
    """

    def __init__(self, config: RiskConfig) -> None:
        self._pdt_buffer = config.pdt_buffer          # default 1 reserved for emergency exits
        self._min_equity = config.min_equity_for_trading  # default 25_500.0
        # Broker-reported day-trade count. Updated by orchestrator after each
        # account fetch. Used as a floor so the bot never undercounts day trades
        # relative to what the broker tracks (e.g. from phantom/adoption trades).
        self.broker_floor: int = 0

    # ------------------------------------------------------------------
    # Day trade counting
    # ------------------------------------------------------------------

    def count_day_trades(
        self,
        orders: list[OrderRecord],
        portfolio: PortfolioState,
        reference_date: date | None = None,
    ) -> int:
        """
        Count round-trips (buy fill + sell fill, same symbol, same ET day)
        within the rolling 5-business-day window.

        A single (symbol, ET date) pair with ≥1 buy fill and ≥1 sell fill
        counts as one day trade.

        Parameters
        ----------
        orders:
            All order records to examine (typically the full orders state).
        portfolio:
            Not currently used; reserved for future close-via-position-exit tracking.
        reference_date:
            Override today's ET date (for testing). Defaults to current ET date.
        """
        today = reference_date or datetime.now(ET).date()
        window = _business_days_window(today)

        # Collect filled orders within the window
        # buys[(symbol, date)] = True if at least one buy fill on that day
        # sells[(symbol, date)] = True if at least one sell fill on that day
        buys: dict[tuple[str, date], bool] = {}
        sells: dict[tuple[str, date], bool] = {}

        for order in orders:
            if order.status != "FILLED":
                continue
            fill_date = _et_date(order.filled_at)
            if fill_date is None or fill_date not in window:
                continue
            key = (order.symbol, fill_date)
            if order.side == "buy":
                buys[key] = True
            elif order.side == "sell":
                sells[key] = True

        # A day trade = same (symbol, date) has both buy and sell fills
        local_count = sum(1 for key in buys if key in sells)
        # Use local count as the authoritative figure for blocking decisions.
        # broker_floor is informational only — it is intentionally NOT used here
        # because the broker's rolling count can be inflated by past buggy sessions,
        # permanently blocking new entries for days. Local order tracking is
        # reliable after the re-adoption / phantom-trade bugs were fixed.
        if self.broker_floor > local_count:
            log.warning(
                "count_day_trades: broker reports %d day trades but local tracking "
                "shows only %d — broker count may reflect a prior buggy session; "
                "using local count for compliance gating (today=%s)",
                self.broker_floor, local_count, today,
            )
        else:
            log.debug(
                "count_day_trades: local=%d  broker_floor=%d (today=%s)",
                local_count, self.broker_floor, today,
            )
        return local_count

    # ------------------------------------------------------------------
    # Day trade permission check
    # ------------------------------------------------------------------

    def can_day_trade(
        self,
        symbol: str,
        orders: list[OrderRecord],
        portfolio: PortfolioState,
        is_emergency: bool = False,
        reference_date: date | None = None,
    ) -> tuple[bool, str]:
        """
        Check if a trade that would constitute a day trade is permitted.

        Normal limit:   3 - buffer  (default: 2 allowed, 1 reserved for emergencies)
        Emergency limit: 3           (uses the reserved buffer)

        Returns (allowed: bool, reason: str).
        """
        current = self.count_day_trades(orders, portfolio, reference_date)
        effective_limit = _PDT_MAX_DAY_TRADES if is_emergency else (_PDT_MAX_DAY_TRADES - self._pdt_buffer)

        if current >= effective_limit:
            kind = "emergency" if is_emergency else "normal"
            reason = (
                f"Day trade limit reached: {current}/{effective_limit} "
                f"({kind} limit, buffer={self._pdt_buffer})"
            )
            log.warning("PDT block on %s: %s", symbol, reason)
            return False, reason

        remaining = effective_limit - current
        return True, f"Day trade allowed: {current}/{effective_limit} used, {remaining} remaining"

    # ------------------------------------------------------------------
    # Emergency exit flag
    # ------------------------------------------------------------------

    def is_emergency_exit(self, symbol: str) -> bool:
        """
        Return True if this exit should use the reserved emergency buffer.

        In Phase 03, this is a stub that always returns False — the risk manager
        quant overrides (Phase 05) will drive this signal.
        """
        return False

    # ------------------------------------------------------------------
    # Equity floor
    # ------------------------------------------------------------------

    def check_equity_floor(self, account: AccountInfo) -> tuple[bool, str]:
        """
        Block all new entries if equity is below the configured minimum.

        Returns (trading_allowed: bool, reason: str).
        """
        if account.pdt_flag and account.equity >= self._min_equity:
            # PDT-flagged accounts above the equity floor have unlimited day trades
            return True, (
                f"PDT account with equity ${account.equity:,.2f} "
                f">= floor ${self._min_equity:,.2f} — unlimited day trades"
            )

        if account.equity < self._min_equity:
            reason = (
                f"Equity ${account.equity:,.2f} below minimum "
                f"${self._min_equity:,.2f} — all new entries blocked"
            )
            log.warning("Equity floor triggered: %s", reason)
            return False, reason

        return True, f"Equity ${account.equity:,.2f} above floor ${self._min_equity:,.2f}"
