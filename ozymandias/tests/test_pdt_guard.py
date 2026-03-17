"""
Tests for execution/pdt_guard.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from ozymandias.core.config import RiskConfig
from ozymandias.core.state_manager import OrderRecord, PortfolioState
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.execution.pdt_guard import PDTGuard, _business_days_window, _et_date

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _risk_config(pdt_buffer: int = 1, min_equity: float = 25_500.0) -> RiskConfig:
    cfg = RiskConfig()
    cfg.pdt_buffer = pdt_buffer
    cfg.min_equity_for_trading = min_equity
    return cfg


def _guard(pdt_buffer: int = 1) -> PDTGuard:
    return PDTGuard(_risk_config(pdt_buffer=pdt_buffer))


def _et_iso(d: date, hour: int = 12) -> str:
    """Return an ISO string for a datetime at the given ET date/hour."""
    dt = datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=ET)
    return dt.isoformat()


def _filled_order(
    order_id: str,
    symbol: str,
    side: str,
    fill_date: date,
    quantity: float = 10.0,
) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type="market",
        limit_price=None,
        status="FILLED",
        filled_quantity=quantity,
        remaining_quantity=0.0,
        created_at=_et_iso(fill_date, 9),
        filled_at=_et_iso(fill_date, 10),
    )


def _pending_order(order_id: str, symbol: str, side: str) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=10.0,
        order_type="limit",
        limit_price=100.0,
        status="PENDING",
        filled_quantity=0.0,
        remaining_quantity=10.0,
        created_at="",
    )


def _portfolio() -> PortfolioState:
    return PortfolioState()


def _account(equity: float, pdt_flag: bool = False) -> AccountInfo:
    return AccountInfo(
        equity=equity,
        buying_power=equity * 2,
        cash=equity,
        currency="USD",
        pdt_flag=pdt_flag,
        daytrade_count=0,
        account_id="test-acc",
    )


# Reference dates (all weekdays)
MON = date(2025, 3, 10)
TUE = date(2025, 3, 11)
WED = date(2025, 3, 12)
THU = date(2025, 3, 13)
FRI = date(2025, 3, 14)
SAT = date(2025, 3, 15)
MON2 = date(2025, 3, 17)   # following Monday


# ---------------------------------------------------------------------------
# _business_days_window helper
# ---------------------------------------------------------------------------

class TestBusinessDaysWindow:
    def test_five_consecutive_weekdays(self):
        window = _business_days_window(FRI)
        assert FRI in window
        assert THU in window
        assert WED in window
        assert TUE in window
        assert MON in window
        assert len(window) == 5

    def test_skips_weekend(self):
        # Monday's window should skip Saturday and Sunday
        window = _business_days_window(MON2)
        assert SAT not in window
        assert date(2025, 3, 16) not in window  # Sunday
        assert FRI in window  # previous Friday counts

    def test_window_size(self):
        window = _business_days_window(WED)
        assert len(window) == 5


# ---------------------------------------------------------------------------
# count_day_trades
# ---------------------------------------------------------------------------

class TestCountDayTrades:
    def test_zero_when_no_orders(self):
        guard = _guard()
        assert guard.count_day_trades([], _portfolio(), reference_date=WED) == 0

    def test_one_day_trade_same_day(self):
        orders = [
            _filled_order("o1", "AAPL", "buy", TUE),
            _filled_order("o2", "AAPL", "sell", TUE),
        ]
        guard = _guard()
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 1

    def test_two_day_trades_different_symbols(self):
        orders = [
            _filled_order("o1", "AAPL", "buy", TUE),
            _filled_order("o2", "AAPL", "sell", TUE),
            _filled_order("o3", "TSLA", "buy", WED),
            _filled_order("o4", "TSLA", "sell", WED),
        ]
        guard = _guard()
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 2

    def test_overnight_hold_not_a_day_trade(self):
        """Buy Monday, sell Tuesday — NOT a day trade."""
        orders = [
            _filled_order("o1", "AAPL", "buy", MON),
            _filled_order("o2", "AAPL", "sell", TUE),
        ]
        guard = _guard()
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 0

    def test_weekend_boundary_not_a_day_trade(self):
        """Buy Friday, sell Monday — NOT a day trade."""
        orders = [
            _filled_order("o1", "AAPL", "buy", FRI),
            _filled_order("o2", "AAPL", "sell", MON2),
        ]
        guard = _guard()
        assert guard.count_day_trades(orders, _portfolio(), reference_date=MON2) == 0

    def test_excludes_trades_outside_5_day_window(self):
        """Day trade from 6 business days ago should not count."""
        old_mon = date(2025, 3, 3)  # 6 business days before FRI 2025-03-14
        orders = [
            _filled_order("o1", "AAPL", "buy", old_mon),
            _filled_order("o2", "AAPL", "sell", old_mon),
        ]
        guard = _guard()
        count = guard.count_day_trades(orders, _portfolio(), reference_date=FRI)
        assert count == 0

    def test_includes_trades_within_5_day_window(self):
        orders = [
            _filled_order("o1", "AAPL", "buy", MON),
            _filled_order("o2", "AAPL", "sell", MON),
        ]
        guard = _guard()
        count = guard.count_day_trades(orders, _portfolio(), reference_date=FRI)
        assert count == 1

    def test_buy_only_not_a_day_trade(self):
        """Only a buy fill — not a day trade (no matching sell)."""
        orders = [_filled_order("o1", "AAPL", "buy", WED)]
        guard = _guard()
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 0

    def test_pending_orders_not_counted(self):
        """PENDING orders should not count as day trades."""
        orders = [
            _filled_order("o1", "AAPL", "buy", WED),
            _pending_order("o2", "AAPL", "sell"),
        ]
        guard = _guard()
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 0

    def test_same_symbol_multiple_buys_sells_same_day_counts_once(self):
        """Multiple buy+sell fills for same symbol same day = 1 day trade."""
        orders = [
            _filled_order("o1", "AAPL", "buy", WED),
            _filled_order("o2", "AAPL", "buy", WED),
            _filled_order("o3", "AAPL", "sell", WED),
        ]
        guard = _guard()
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 1


# ---------------------------------------------------------------------------
# can_day_trade
# ---------------------------------------------------------------------------

class TestCanDayTrade:
    def test_allowed_when_no_existing_day_trades(self):
        guard = _guard(pdt_buffer=1)
        allowed, reason = guard.can_day_trade(
            "AAPL", [], _portfolio(), reference_date=WED
        )
        assert allowed is True

    def test_allowed_with_one_existing_day_trade(self):
        orders = [
            _filled_order("o1", "AAPL", "buy", TUE),
            _filled_order("o2", "AAPL", "sell", TUE),
        ]
        guard = _guard(pdt_buffer=1)
        allowed, _ = guard.can_day_trade("TSLA", orders, _portfolio(), reference_date=WED)
        assert allowed is True  # 1 used, limit is 3-1=2, still ok

    def test_blocked_at_buffer_limit(self):
        """With buffer=1, limit is 2. At 2 day trades, new one blocked."""
        orders = [
            _filled_order("o1", "AAPL", "buy", MON),
            _filled_order("o2", "AAPL", "sell", MON),
            _filled_order("o3", "TSLA", "buy", TUE),
            _filled_order("o4", "TSLA", "sell", TUE),
        ]
        guard = _guard(pdt_buffer=1)
        allowed, reason = guard.can_day_trade(
            "NVDA", orders, _portfolio(), reference_date=WED
        )
        assert allowed is False
        assert "limit" in reason.lower()

    def test_emergency_exit_bypasses_buffer(self):
        """Emergency exit can use the reserved buffer trade."""
        orders = [
            _filled_order("o1", "AAPL", "buy", MON),
            _filled_order("o2", "AAPL", "sell", MON),
            _filled_order("o3", "TSLA", "buy", TUE),
            _filled_order("o4", "TSLA", "sell", TUE),
        ]
        guard = _guard(pdt_buffer=1)
        # Normal: blocked (2/2 normal limit)
        allowed_normal, _ = guard.can_day_trade("NVDA", orders, _portfolio(), reference_date=WED)
        assert allowed_normal is False
        # Emergency: allowed (2/3 absolute limit)
        allowed_emergency, _ = guard.can_day_trade(
            "NVDA", orders, _portfolio(), is_emergency=True, reference_date=WED
        )
        assert allowed_emergency is True

    def test_emergency_blocked_at_absolute_limit(self):
        """Emergency exit also blocked when at the absolute 3-trade limit."""
        orders = [
            _filled_order("o1", "AAPL", "buy", MON),
            _filled_order("o2", "AAPL", "sell", MON),
            _filled_order("o3", "TSLA", "buy", TUE),
            _filled_order("o4", "TSLA", "sell", TUE),
            _filled_order("o5", "NVDA", "buy", WED),
            _filled_order("o6", "NVDA", "sell", WED),
        ]
        guard = _guard(pdt_buffer=1)
        allowed, reason = guard.can_day_trade(
            "SPY", orders, _portfolio(), is_emergency=True, reference_date=WED
        )
        assert allowed is False

    def test_zero_buffer_allows_all_three(self):
        """With buffer=0, all 3 day trades are usable."""
        orders = [
            _filled_order("o1", "AAPL", "buy", MON),
            _filled_order("o2", "AAPL", "sell", MON),
            _filled_order("o3", "TSLA", "buy", TUE),
            _filled_order("o4", "TSLA", "sell", TUE),
        ]
        guard = _guard(pdt_buffer=0)
        allowed, _ = guard.can_day_trade("NVDA", orders, _portfolio(), reference_date=WED)
        assert allowed is True  # 2/3 used, 1 remaining


# ---------------------------------------------------------------------------
# check_equity_floor
# ---------------------------------------------------------------------------

class TestCheckEquityFloor:
    def test_blocks_below_floor(self):
        guard = _guard()
        acct = _account(equity=24_000.0)
        allowed, reason = guard.check_equity_floor(acct)
        assert allowed is False
        assert "below minimum" in reason.lower()

    def test_allows_above_floor(self):
        guard = _guard()
        acct = _account(equity=30_000.0)
        allowed, reason = guard.check_equity_floor(acct)
        assert allowed is True

    def test_allows_exactly_at_floor(self):
        guard = PDTGuard(_risk_config(min_equity=25_500.0))
        acct = _account(equity=25_500.0)
        allowed, _ = guard.check_equity_floor(acct)
        assert allowed is True

    def test_pdt_flagged_account_above_25k_unlimited(self):
        """PDT-flagged account with >$25k equity gets unlimited day trades."""
        guard = _guard()
        acct = _account(equity=30_000.0, pdt_flag=True)
        allowed, reason = guard.check_equity_floor(acct)
        assert allowed is True
        assert "unlimited" in reason.lower()

    def test_pdt_flagged_below_25k_still_blocked(self):
        """PDT flag doesn't help if equity < $25k."""
        guard = _guard()
        acct = _account(equity=24_000.0, pdt_flag=True)
        # pdt_flag=True but equity < 25_000 → the pdt check fails, falls through to equity check
        allowed, reason = guard.check_equity_floor(acct)
        assert allowed is False

    def test_custom_floor(self):
        guard = PDTGuard(_risk_config(min_equity=30_000.0))
        acct = _account(equity=28_000.0)
        allowed, _ = guard.check_equity_floor(acct)
        assert allowed is False

    def test_pdt_flagged_blocked_by_custom_floor(self):
        """PDT-flagged account is blocked when equity < configured floor (regression: was hardcoded 25_000)."""
        guard = PDTGuard(_risk_config(min_equity=30_000.0))
        # equity > old hardcoded 25k but < configured 30k — must be BLOCKED
        acct = _account(equity=27_000.0, pdt_flag=True)
        allowed, reason = guard.check_equity_floor(acct)
        assert allowed is False
        assert "below minimum" in reason.lower()

    def test_pdt_flagged_allowed_above_custom_floor(self):
        """PDT-flagged account is allowed when equity >= configured floor."""
        guard = PDTGuard(_risk_config(min_equity=30_000.0))
        acct = _account(equity=31_000.0, pdt_flag=True)
        allowed, _ = guard.check_equity_floor(acct)
        assert allowed is True


# ---------------------------------------------------------------------------
# broker_floor — Bug #9 (2026-03-16)
# ---------------------------------------------------------------------------

class TestBrokerFloor:
    """
    broker_floor is stored for informational/logging purposes but is NOT used
    to inflate the local count for blocking decisions.

    Context: broker_floor was originally used as max(local, broker) to catch
    phantom trades. This caused Bug #9b (2026-03-17): broker_floor=5 from a
    buggy paper session permanently blocked all entries for the rest of the
    5-day rolling window. Local order tracking is now authoritative.
    """

    def test_default_floor_zero_has_no_effect(self):
        """broker_floor=0 (default) does not affect local count."""
        guard = _guard()
        orders = [
            _filled_order("o1", "AAPL", "buy", TUE),
            _filled_order("o2", "AAPL", "sell", TUE),
        ]
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 1

    def test_broker_floor_higher_than_local_does_not_inflate_count(self):
        """broker_floor > local count: local count is still returned (broker is informational)."""
        guard = _guard()
        guard.broker_floor = 5
        orders = [
            _filled_order("o1", "AAPL", "buy", TUE),
            _filled_order("o2", "AAPL", "sell", TUE),
        ]
        # local=1, broker_floor=5 → local count wins: 1
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 1

    def test_local_count_returned_regardless_of_broker_floor(self):
        """Local count is always the return value."""
        guard = _guard()
        guard.broker_floor = 1
        orders = [
            _filled_order("o1", "AAPL", "buy", TUE),
            _filled_order("o2", "AAPL", "sell", TUE),
            _filled_order("o3", "TSLA", "buy", WED),
            _filled_order("o4", "TSLA", "sell", WED),
            _filled_order("o5", "NVDA", "buy", WED),
            _filled_order("o6", "NVDA", "sell", WED),
        ]
        # local=3, broker_floor=1 → local 3
        assert guard.count_day_trades(orders, _portfolio(), reference_date=WED) == 3

    def test_high_broker_floor_does_not_block_can_day_trade(self):
        """Even with broker_floor above limit, can_day_trade uses local count."""
        guard = _guard(pdt_buffer=1)  # effective limit = 3-1 = 2
        guard.broker_floor = 5        # would have blocked under old logic
        orders = []                   # local=0
        # local=0 < effective_limit=2 → allowed
        allowed, _ = guard.can_day_trade("AAPL", orders, _portfolio(), reference_date=WED)
        assert allowed is True

    def test_can_day_trade_blocked_by_local_count(self):
        """can_day_trade is blocked when local count reaches the effective limit."""
        guard = _guard(pdt_buffer=1)  # effective limit = 2
        orders = [
            _filled_order("o1", "AAPL", "buy", TUE),
            _filled_order("o2", "AAPL", "sell", TUE),
            _filled_order("o3", "TSLA", "buy", TUE),
            _filled_order("o4", "TSLA", "sell", TUE),
        ]
        # local=2 >= effective_limit=2 → blocked
        allowed, reason = guard.can_day_trade("NVDA", orders, _portfolio(), reference_date=WED)
        assert allowed is False
        assert "limit" in reason.lower()

    def test_broker_floor_stored_for_logging(self):
        """broker_floor attribute is settable and readable (used for warning logs)."""
        guard = _guard()
        guard.broker_floor = 3
        assert guard.broker_floor == 3
        # count_day_trades still returns local count, not broker_floor
        assert guard.count_day_trades([], _portfolio(), reference_date=WED) == 0


# ---------------------------------------------------------------------------
# is_emergency_exit (stub)
# ---------------------------------------------------------------------------

class TestIsEmergencyExit:
    def test_returns_false_in_phase03(self):
        guard = _guard()
        assert guard.is_emergency_exit("AAPL") is False
