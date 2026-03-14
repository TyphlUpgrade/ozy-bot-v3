"""
Tests for broker_interface.py and alpaca_broker.py.

All external API calls are mocked — no real Alpaca credentials required.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ozymandias.execution.broker_interface import (
    AccountInfo,
    BrokerInterface,
    BrokerPosition,
    CancelResult,
    Fill,
    MarketHours,
    Order,
    OrderResult,
    OrderStatus,
)
from ozymandias.execution.alpaca_broker import AlpacaBroker, _transient_error, _current_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _make_broker(paper: bool = True) -> AlpacaBroker:
    """Return an AlpacaBroker whose TradingClient is fully mocked."""
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._paper = paper
    broker._client = MagicMock()
    return broker


# ---------------------------------------------------------------------------
# Interface completeness
# ---------------------------------------------------------------------------

class TestBrokerInterfaceCompleteness:
    """AlpacaBroker must implement every abstract method on BrokerInterface."""

    def test_alpaca_broker_is_concrete(self):
        """ABC enforcement: instantiation only fails if abstractmethods are missing."""
        # If AlpacaBroker doesn't implement all methods, this import-time class
        # definition itself would still work, but instantiation would raise TypeError.
        # We verify by checking the abstract method set is empty.
        abstract = getattr(AlpacaBroker, "__abstractmethods__", frozenset())
        assert abstract == frozenset(), (
            f"AlpacaBroker is missing implementations for: {abstract}"
        )

    def test_all_interface_methods_present(self):
        required = {
            "get_account", "get_buying_power",
            "place_order", "cancel_order", "get_order_status", "get_open_orders",
            "get_positions", "get_position",
            "get_fills",
            "is_market_open", "get_market_hours",
        }
        for method in required:
            assert hasattr(AlpacaBroker, method), f"AlpacaBroker missing method: {method}"


# ---------------------------------------------------------------------------
# Paper vs live URL selection
# ---------------------------------------------------------------------------

class TestPaperVsLive:
    def test_paper_flag_true(self):
        with patch("alpaca.trading.client.TradingClient.__init__", return_value=None):
            broker = AlpacaBroker(api_key="k", secret_key="s", paper=True)
            assert broker._paper is True

    def test_paper_flag_false(self):
        with patch("alpaca.trading.client.TradingClient.__init__", return_value=None):
            broker = AlpacaBroker(api_key="k", secret_key="s", paper=False)
            assert broker._paper is False


# ---------------------------------------------------------------------------
# get_account
# ---------------------------------------------------------------------------

class TestGetAccount:
    @pytest.mark.asyncio
    async def test_maps_account_fields(self):
        broker = _make_broker()
        mock_acct = SimpleNamespace(
            equity="100000.00",
            buying_power="50000.00",
            cash="25000.00",
            currency="USD",
            pattern_day_trader=False,
            daytrade_count=1,
            id="acc-123",
        )
        broker._client.get_account = MagicMock(return_value=mock_acct)

        result = await broker.get_account()

        assert isinstance(result, AccountInfo)
        assert result.equity == 100000.0
        assert result.buying_power == 50000.0
        assert result.cash == 25000.0
        assert result.currency == "USD"
        assert result.pdt_flag is False
        assert result.daytrade_count == 1
        assert result.account_id == "acc-123"

    @pytest.mark.asyncio
    async def test_get_buying_power_delegates(self):
        broker = _make_broker()
        mock_acct = SimpleNamespace(
            equity="80000", buying_power="40000", cash="20000",
            currency="USD", pattern_day_trader=True, daytrade_count=3, id="x",
        )
        broker._client.get_account = MagicMock(return_value=mock_acct)
        bp = await broker.get_buying_power()
        assert bp == 40000.0


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------

class TestPlaceOrder:
    def _mock_submit(self, broker, order_id="ord-1", status="new"):
        mock_result = SimpleNamespace(
            id=order_id,
            status=SimpleNamespace(value=status),
            submitted_at=datetime(2025, 3, 11, 14, 30, 0, tzinfo=UTC),
        )
        broker._client.submit_order = MagicMock(return_value=mock_result)
        return mock_result

    @pytest.mark.asyncio
    async def test_market_order(self):
        broker = _make_broker()
        self._mock_submit(broker, "ord-mkt", "new")

        order = Order(
            symbol="AAPL", side="buy", quantity=10,
            order_type="market", time_in_force="day",
        )
        result = await broker.place_order(order)

        assert isinstance(result, OrderResult)
        assert result.order_id == "ord-mkt"
        assert result.status == "new"

        # Verify alpaca-py was called with a MarketOrderRequest
        from alpaca.trading.requests import MarketOrderRequest
        call_arg = broker._client.submit_order.call_args[0][0]
        assert isinstance(call_arg, MarketOrderRequest)

    @pytest.mark.asyncio
    async def test_limit_order(self):
        broker = _make_broker()
        self._mock_submit(broker, "ord-lmt", "new")

        order = Order(
            symbol="TSLA", side="sell", quantity=5,
            order_type="limit", time_in_force="gtc", limit_price=250.00,
        )
        result = await broker.place_order(order)
        assert result.order_id == "ord-lmt"

        from alpaca.trading.requests import LimitOrderRequest
        call_arg = broker._client.submit_order.call_args[0][0]
        assert isinstance(call_arg, LimitOrderRequest)

    @pytest.mark.asyncio
    async def test_limit_order_without_price_raises(self):
        broker = _make_broker()
        order = Order(
            symbol="TSLA", side="buy", quantity=5,
            order_type="limit", time_in_force="day", limit_price=None,
        )
        with pytest.raises(ValueError, match="limit_price"):
            await broker.place_order(order)


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_successful_cancel(self):
        broker = _make_broker()
        broker._client.cancel_order_by_id = MagicMock(return_value=None)

        # get_order_status returns "canceled" on first poll
        canceled_status = OrderStatus(
            order_id="ord-1", status="canceled",
            filled_qty=0, remaining_qty=10, filled_avg_price=None,
            submitted_at=None, filled_at=None, canceled_at=datetime.now(UTC),
        )
        with patch.object(broker, "get_order_status", new=AsyncMock(return_value=canceled_status)):
            result = await broker.cancel_order("ord-1")

        assert isinstance(result, CancelResult)
        assert result.success is True
        assert result.final_status == "canceled"

    @pytest.mark.asyncio
    async def test_cancel_race_condition_filled(self):
        """Order fills between cancel request and poll — should report success=False."""
        broker = _make_broker()
        broker._client.cancel_order_by_id = MagicMock(return_value=None)

        filled_status = OrderStatus(
            order_id="ord-2", status="filled",
            filled_qty=10, remaining_qty=0, filled_avg_price=150.0,
            submitted_at=None, filled_at=datetime.now(UTC), canceled_at=None,
        )
        with patch.object(broker, "get_order_status", new=AsyncMock(return_value=filled_status)):
            result = await broker.cancel_order("ord-2")

        assert result.success is False
        assert result.final_status == "filled"


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------

class TestGetOrderStatus:
    @pytest.mark.asyncio
    async def test_maps_order_fields(self):
        broker = _make_broker()
        mock_order = SimpleNamespace(
            id="ord-abc",
            status=SimpleNamespace(value="partially_filled"),
            qty="10",
            filled_qty="4",
            filled_avg_price="150.25",
            submitted_at=datetime(2025, 3, 11, 14, 0, tzinfo=UTC),
            filled_at=None,
            canceled_at=None,
        )
        broker._client.get_order_by_id = MagicMock(return_value=mock_order)

        status = await broker.get_order_status("ord-abc")

        assert isinstance(status, OrderStatus)
        assert status.order_id == "ord-abc"
        assert status.status == "partially_filled"
        assert status.filled_qty == 4.0
        assert status.remaining_qty == 6.0
        assert status.filled_avg_price == 150.25


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

class TestGetPositions:
    @pytest.mark.asyncio
    async def test_maps_positions(self):
        broker = _make_broker()
        mock_pos = SimpleNamespace(
            symbol="NVDA",
            qty="15",
            avg_entry_price="420.00",
            current_price="445.50",
            market_value="6682.50",
            unrealized_pl="382.50",
        )
        broker._client.get_all_positions = MagicMock(return_value=[mock_pos])

        positions = await broker.get_positions()
        assert len(positions) == 1
        p = positions[0]
        assert isinstance(p, BrokerPosition)
        assert p.symbol == "NVDA"
        assert p.qty == 15.0
        assert p.avg_entry_price == 420.0
        assert p.current_price == 445.5
        assert p.unrealized_pl == 382.5

    @pytest.mark.asyncio
    async def test_get_position_not_found(self):
        broker = _make_broker()
        broker._client.get_open_position = MagicMock(
            side_effect=Exception("404: position does not exist")
        )
        result = await broker.get_position("AAPL")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_position_found(self):
        broker = _make_broker()
        mock_pos = SimpleNamespace(
            symbol="AAPL", qty="5", avg_entry_price="175.00",
            current_price="180.00", market_value="900.00", unrealized_pl="25.00",
        )
        broker._client.get_open_position = MagicMock(return_value=mock_pos)
        result = await broker.get_position("AAPL")
        assert result is not None
        assert result.symbol == "AAPL"


# ---------------------------------------------------------------------------
# get_fills
# ---------------------------------------------------------------------------

class TestGetFills:
    @pytest.mark.asyncio
    async def test_returns_only_filled_orders(self):
        broker = _make_broker()
        filled_order = SimpleNamespace(
            id="ord-f1",
            symbol="SPY",
            side=SimpleNamespace(value="buy"),
            status=SimpleNamespace(value="filled"),
            qty="10",
            filled_qty="10",
            filled_avg_price="450.00",
            filled_at=datetime(2025, 3, 11, 14, 30, tzinfo=UTC),
        )
        partial_order = SimpleNamespace(
            id="ord-p1",
            symbol="QQQ",
            side=SimpleNamespace(value="buy"),
            status=SimpleNamespace(value="partially_filled"),
            qty="5",
            filled_qty="2",
            filled_avg_price="380.00",
            filled_at=None,
        )
        broker._client.get_orders = MagicMock(return_value=[filled_order, partial_order])

        since = datetime(2025, 3, 11, 14, 0, tzinfo=UTC)
        fills = await broker.get_fills(since)

        assert len(fills) == 1
        f = fills[0]
        assert isinstance(f, Fill)
        assert f.order_id == "ord-f1"
        assert f.symbol == "SPY"
        assert f.qty == 10.0
        assert f.price == 450.0


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

class TestMarketHours:
    @pytest.mark.asyncio
    async def test_is_market_open_true(self):
        broker = _make_broker()
        broker._client.get_clock = MagicMock(return_value=SimpleNamespace(
            is_open=True,
            next_open=datetime(2025, 3, 12, 13, 30, tzinfo=UTC),
            next_close=datetime(2025, 3, 11, 20, 0, tzinfo=UTC),
        ))
        assert await broker.is_market_open() is True

    @pytest.mark.asyncio
    async def test_is_market_open_false(self):
        broker = _make_broker()
        broker._client.get_clock = MagicMock(return_value=SimpleNamespace(
            is_open=False,
            next_open=datetime(2025, 3, 12, 13, 30, tzinfo=UTC),
            next_close=datetime(2025, 3, 12, 20, 0, tzinfo=UTC),
        ))
        assert await broker.is_market_open() is False

    @pytest.mark.asyncio
    async def test_get_market_hours(self):
        broker = _make_broker()
        next_open = datetime(2025, 3, 12, 13, 30, tzinfo=UTC)
        next_close = datetime(2025, 3, 11, 20, 0, tzinfo=UTC)
        broker._client.get_clock = MagicMock(return_value=SimpleNamespace(
            is_open=True,
            next_open=next_open,
            next_close=next_close,
        ))
        hours = await broker.get_market_hours()
        assert isinstance(hours, MarketHours)
        assert hours.is_open is True
        assert hours.next_open == next_open
        assert hours.next_close == next_close
        assert isinstance(hours.session, str)


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        """Should retry on connection errors and eventually succeed."""
        broker = _make_broker()
        call_count = 0

        def flaky_get_account():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("simulated timeout")
            return SimpleNamespace(
                equity="50000", buying_power="25000", cash="10000",
                currency="USD", pattern_day_trader=False, daytrade_count=0, id="acc",
            )

        broker._client.get_account = MagicMock(side_effect=flaky_get_account)

        with patch("ozymandias.execution.alpaca_broker._RETRY_BASE_S", 0.01), \
             patch("ozymandias.execution.alpaca_broker._RETRY_MAX_S", 0.1):
            result = await broker.get_account()

        assert call_count == 3
        assert result.equity == 50000.0

    @pytest.mark.asyncio
    async def test_does_not_retry_non_transient_error(self):
        """Non-transient errors (e.g., auth failure) should propagate immediately."""
        broker = _make_broker()
        call_count = 0

        def auth_fail():
            nonlocal call_count
            call_count += 1
            raise ValueError("403 Forbidden: invalid credentials")

        broker._client.get_account = MagicMock(side_effect=auth_fail)

        with pytest.raises(ValueError, match="403"):
            await broker.get_account()

        assert call_count == 1

    def test_transient_error_detection(self):
        assert _transient_error(ConnectionError("network unreachable")) is True
        assert _transient_error(TimeoutError("read timeout")) is True
        assert _transient_error(Exception("503 Service Unavailable")) is True
        assert _transient_error(Exception("500 Internal Server Error")) is True
        assert _transient_error(ValueError("400 Bad Request")) is False
        assert _transient_error(Exception("403 Forbidden")) is False


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

class TestSessionDetection:
    def _et_dt(self, time_str: str) -> datetime:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        date = f"2025-03-11T{time_str}-04:00"  # EDT
        return datetime.fromisoformat(date).astimezone(timezone.utc)

    def test_pre_market(self):
        assert _current_session(self._et_dt("05:00:00")) == "pre_market"

    def test_regular_hours(self):
        assert _current_session(self._et_dt("10:30:00")) == "regular"

    def test_post_market(self):
        assert _current_session(self._et_dt("17:00:00")) == "post_market"

    def test_closed(self):
        assert _current_session(self._et_dt("22:00:00")) == "closed"

    def test_market_open_boundary(self):
        assert _current_session(self._et_dt("09:30:00")) == "regular"

    def test_pre_market_boundary(self):
        assert _current_session(self._et_dt("04:00:00")) == "pre_market"
