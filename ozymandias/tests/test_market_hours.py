"""
Tests for core/market_hours.py

All tests pass explicit ``now`` datetimes to avoid system-clock dependence.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ozymandias.core.market_hours import (
    ET,
    Session,
    get_current_session,
    is_last_five_minutes,
    is_market_open,
    is_trading_allowed,
    is_weekend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Construct a timezone-aware ET datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Construct a timezone-aware UTC datetime."""
    from datetime import timezone
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Weekend detection
# ---------------------------------------------------------------------------

class TestWeekend:
    def test_saturday_is_weekend(self):
        sat = et(2025, 3, 8, 10, 0)   # Saturday
        assert is_weekend(sat) is True

    def test_sunday_is_weekend(self):
        sun = et(2025, 3, 9, 10, 0)   # Sunday
        assert is_weekend(sun) is True

    def test_monday_is_not_weekend(self):
        mon = et(2025, 3, 10, 10, 0)  # Monday
        assert is_weekend(mon) is False

    def test_friday_is_not_weekend(self):
        fri = et(2025, 3, 7, 15, 0)   # Friday
        assert is_weekend(fri) is False


# ---------------------------------------------------------------------------
# Session detection — weekdays
# ---------------------------------------------------------------------------

class TestSessionDetection:
    def test_pre_market_at_4am(self):
        assert get_current_session(et(2025, 3, 10, 4, 0)) == Session.PRE_MARKET

    def test_pre_market_at_6am(self):
        assert get_current_session(et(2025, 3, 10, 6, 0)) == Session.PRE_MARKET

    def test_pre_market_at_9_29(self):
        assert get_current_session(et(2025, 3, 10, 9, 29)) == Session.PRE_MARKET

    def test_regular_hours_at_9_30(self):
        assert get_current_session(et(2025, 3, 10, 9, 30)) == Session.REGULAR_HOURS

    def test_regular_hours_at_noon(self):
        assert get_current_session(et(2025, 3, 10, 12, 0)) == Session.REGULAR_HOURS

    def test_regular_hours_at_15_54(self):
        # 3:54 PM — still regular hours, not in closing window
        assert get_current_session(et(2025, 3, 10, 15, 54)) == Session.REGULAR_HOURS

    def test_regular_hours_at_15_55(self):
        # 3:55 PM — still REGULAR_HOURS (last-5-min is a sub-state, not its own session)
        assert get_current_session(et(2025, 3, 10, 15, 55)) == Session.REGULAR_HOURS

    def test_regular_hours_at_15_59(self):
        assert get_current_session(et(2025, 3, 10, 15, 59)) == Session.REGULAR_HOURS

    def test_post_market_at_16_00(self):
        assert get_current_session(et(2025, 3, 10, 16, 0)) == Session.POST_MARKET

    def test_post_market_at_18_00(self):
        assert get_current_session(et(2025, 3, 10, 18, 0)) == Session.POST_MARKET

    def test_post_market_at_19_59(self):
        assert get_current_session(et(2025, 3, 10, 19, 59)) == Session.POST_MARKET

    def test_closed_at_20_00(self):
        assert get_current_session(et(2025, 3, 10, 20, 0)) == Session.CLOSED

    def test_closed_midnight(self):
        assert get_current_session(et(2025, 3, 10, 0, 0)) == Session.CLOSED

    def test_closed_at_3_59am(self):
        # 3:59 AM — just before pre-market opens
        assert get_current_session(et(2025, 3, 10, 3, 59)) == Session.CLOSED

    def test_weekend_is_closed(self):
        sat = et(2025, 3, 8, 10, 0)   # Saturday during what would be regular hours
        assert get_current_session(sat) == Session.CLOSED


# ---------------------------------------------------------------------------
# 3:55 PM boundary (last 5 minutes)
# ---------------------------------------------------------------------------

class TestLastFiveMinutes:
    def test_at_15_55_is_closing_window(self):
        assert is_last_five_minutes(et(2025, 3, 10, 15, 55)) is True

    def test_at_15_57_is_closing_window(self):
        assert is_last_five_minutes(et(2025, 3, 10, 15, 57)) is True

    def test_at_15_59_is_closing_window(self):
        assert is_last_five_minutes(et(2025, 3, 10, 15, 59)) is True

    def test_at_15_54_is_not_closing_window(self):
        assert is_last_five_minutes(et(2025, 3, 10, 15, 54)) is False

    def test_at_16_00_is_not_closing_window(self):
        # 4:00 PM is post-market, not the closing window
        assert is_last_five_minutes(et(2025, 3, 10, 16, 0)) is False


# ---------------------------------------------------------------------------
# Convenience predicates
# ---------------------------------------------------------------------------

class TestConveniencePredicates:
    def test_is_market_open_during_regular_hours(self):
        assert is_market_open(et(2025, 3, 10, 11, 0)) is True

    def test_is_market_open_false_pre_market(self):
        assert is_market_open(et(2025, 3, 10, 8, 0)) is False

    def test_is_market_open_false_post_market(self):
        assert is_market_open(et(2025, 3, 10, 17, 0)) is False

    def test_is_trading_allowed_during_pre_market(self):
        # System is active during pre-market (monitoring only)
        assert is_trading_allowed(et(2025, 3, 10, 6, 0)) is True

    def test_is_trading_allowed_false_overnight(self):
        assert is_trading_allowed(et(2025, 3, 10, 22, 0)) is False

    def test_is_trading_allowed_false_weekend(self):
        assert is_trading_allowed(et(2025, 3, 8, 10, 0)) is False


# ---------------------------------------------------------------------------
# UTC input — timezone conversion
# ---------------------------------------------------------------------------

class TestUTCConversion:
    def test_utc_9am_is_pre_market(self):
        # 9:00 AM UTC on a weekday = 4:00 AM ET (assuming EST = UTC-5)
        # (This test runs in March so ET is EDT = UTC-4, 9 UTC = 5 AM ET)
        dt_utc = utc(2025, 1, 13, 9, 0)  # January: EST = UTC-5 → 4:00 AM ET
        assert get_current_session(dt_utc) == Session.PRE_MARKET

    def test_utc_14_30_is_regular_hours(self):
        # 14:30 UTC in January = 9:30 AM ET (EST = UTC-5)
        dt_utc = utc(2025, 1, 13, 14, 30)
        assert get_current_session(dt_utc) == Session.REGULAR_HOURS

    def test_utc_21_00_is_regular_hours_in_summer(self):
        # 21:00 UTC in June (EDT = UTC-4) = 5:00 PM ET → post-market
        dt_utc = utc(2025, 6, 9, 21, 0)  # Monday in June
        assert get_current_session(dt_utc) == Session.POST_MARKET

    def test_naive_datetime_treated_as_et(self):
        # Naive datetime (no tzinfo) should be treated as ET
        naive = datetime(2025, 3, 10, 12, 0)  # noon, no tz
        assert get_current_session(naive) == Session.REGULAR_HOURS
