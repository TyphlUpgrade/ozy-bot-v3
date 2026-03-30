"""
Market hours / session detection.

All comparisons use US/Eastern time via zoneinfo. Every public function
accepts an optional ``now`` parameter for testability.

Session definitions (ET):
  pre_market      04:00 – 09:30
  regular_hours   09:30 – 16:00  (last 5 min = 15:55-16:00, special sub-state)
  post_market     16:00 – 20:00
  closed          20:00 – 04:00 (next day)
  weekends        Saturday / Sunday → closed
  holidays        NYSE-scheduled full-day closures → closed
"""
from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

# NYSE scheduled full-day closures.  When a holiday falls on Saturday the
# preceding Friday is observed; when it falls on Sunday the following Monday
# is observed.  Extend this set at the start of each calendar year.
_NYSE_HOLIDAYS: frozenset[date] = frozenset({
    # 2025
    date(2025, 1,  1),   # New Year's Day
    date(2025, 1, 20),   # MLK Jr. Day
    date(2025, 2, 17),   # Presidents' Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7,  4),   # Independence Day
    date(2025, 9,  1),   # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas Day
    # 2026
    date(2026, 1,  1),   # New Year's Day
    date(2026, 1, 19),   # MLK Jr. Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4,  3),   # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7,  3),   # Independence Day (observed; July 4 is Saturday)
    date(2026, 9,  7),   # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas Day
})

# Session boundary times (ET)
_PRE_MARKET_OPEN  = time(4, 0)
_REGULAR_OPEN     = time(9, 30)
_CLOSING_WINDOW   = time(15, 55)   # start of "last 5 minutes"
_REGULAR_CLOSE    = time(16, 0)
_POST_MARKET_CLOSE = time(20, 0)


class Session(str, Enum):
    PRE_MARKET    = "pre_market"
    REGULAR_HOURS = "regular_hours"
    POST_MARKET   = "post_market"
    CLOSED        = "closed"


def _now_et(now: datetime | None) -> datetime:
    """Return ``now`` converted to ET, or the current wall-clock time in ET."""
    if now is None:
        return datetime.now(ET)
    # Accept both timezone-aware and timezone-naive datetimes.
    # Naive datetimes are assumed to already be in ET.
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def is_weekend(now: datetime | None = None) -> bool:
    """Return True if the current day (in ET) is Saturday or Sunday."""
    et = _now_et(now)
    return et.weekday() >= 5  # 5 = Saturday, 6 = Sunday


def get_current_session(now: datetime | None = None) -> Session:
    """
    Return the current market session as a :class:`Session` enum value.

    Parameters
    ----------
    now:
        Optional override for the current time. Accepts timezone-aware or
        timezone-naive datetimes. Timezone-naive datetimes are assumed to be ET.
        UTC datetimes are converted automatically.
    """
    et = _now_et(now)
    t = et.time()

    if is_weekend(et) or et.date() in _NYSE_HOLIDAYS:
        return Session.CLOSED

    if _PRE_MARKET_OPEN <= t < _REGULAR_OPEN:
        return Session.PRE_MARKET
    if _REGULAR_OPEN <= t < _REGULAR_CLOSE:
        return Session.REGULAR_HOURS
    if _REGULAR_CLOSE <= t < _POST_MARKET_CLOSE:
        return Session.POST_MARKET
    return Session.CLOSED


def is_last_five_minutes(now: datetime | None = None) -> bool:
    """
    Return True if we are in the 3:55–4:00 PM ET window.

    During this window, no new momentum entries should be placed.
    Swing entries are allowed with AI approval.
    """
    et = _now_et(now)
    t = et.time()
    return _CLOSING_WINDOW <= t < _REGULAR_CLOSE


def is_market_open(now: datetime | None = None) -> bool:
    """Return True if the current session is regular_hours."""
    return get_current_session(now) == Session.REGULAR_HOURS


def is_trading_allowed(now: datetime | None = None) -> bool:
    """
    Return True if the system should be running any trading logic.
    False during weekends and overnight closed period.
    """
    session = get_current_session(now)
    return session != Session.CLOSED


def get_next_market_open(now: datetime | None = None) -> datetime:
    """
    Return the next NYSE regular market open as a timezone-aware ET datetime.

    If the current time is before 09:30 ET on a trading day, returns today's open.
    Otherwise advances day-by-day until a non-weekend, non-holiday date is found.
    """
    from datetime import timedelta
    et = _now_et(now)
    d = et.date()

    # Today's open is still upcoming if it's a trading day and before 09:30
    if d.weekday() < 5 and d not in _NYSE_HOLIDAYS and et.time() < _REGULAR_OPEN:
        return datetime.combine(d, _REGULAR_OPEN).replace(tzinfo=ET)

    # Advance to the next trading day
    d = d + timedelta(days=1)
    while d.weekday() >= 5 or d in _NYSE_HOLIDAYS:
        d = d + timedelta(days=1)
    return datetime.combine(d, _REGULAR_OPEN).replace(tzinfo=ET)
