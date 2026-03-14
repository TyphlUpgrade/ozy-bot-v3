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

Holiday detection: # TODO: NYSE holiday calendar
"""
from __future__ import annotations

from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")

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
    # TODO: NYSE holiday calendar — treat market holidays as CLOSED

    et = _now_et(now)
    t = et.time()

    if is_weekend(et):
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
