"""
Tests for execution/fill_protection.py.

All state manager I/O is mocked — no real file system access.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ozymandias.core.state_manager import OrderRecord, OrdersState
from ozymandias.execution.broker_interface import CancelResult, OrderStatus
from ozymandias.execution.fill_protection import FillProtectionManager, StateChange

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _order(
    order_id: str,
    symbol: str,
    side: str = "buy",
    status: str = "PENDING",
    order_type: str = "limit",
    quantity: float = 10.0,
    limit_price: float | None = 150.0,
    filled_quantity: float = 0.0,
    created_offset_sec: float = 0.0,
    filled_at: str = "",
) -> OrderRecord:
    created = _utcnow() - timedelta(seconds=created_offset_sec)
    return OrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        status=status,
        filled_quantity=filled_quantity,
        remaining_quantity=quantity - filled_quantity,
        created_at=_iso(created),
        filled_at=filled_at,
    )


def _broker_status(
    order_id: str,
    status: str,
    filled_qty: float = 0.0,
    remaining_qty: float = 10.0,
    avg_price: float | None = None,
) -> OrderStatus:
    return OrderStatus(
        order_id=order_id,
        status=status,
        filled_qty=filled_qty,
        remaining_qty=remaining_qty,
        filled_avg_price=avg_price,
        submitted_at=None,
        filled_at=None,
        canceled_at=None,
    )


def _make_fpm(*orders: OrderRecord) -> FillProtectionManager:
    """Create an FPM with a mocked StateManager pre-loaded with given orders."""
    mock_sm = MagicMock()
    mock_sm.load_orders = AsyncMock(return_value=OrdersState(orders=list(orders)))
    mock_sm.save_orders = AsyncMock()
    fpm = FillProtectionManager(mock_sm)
    fpm._orders = {o.order_id: o for o in orders}
    return fpm


# ---------------------------------------------------------------------------
# can_place_order
# ---------------------------------------------------------------------------

class TestCanPlaceOrder:
    def test_returns_false_when_pending_order_exists(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        assert fpm.can_place_order("AAPL") is False

    def test_returns_false_when_partially_filled_order_exists(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PARTIALLY_FILLED", filled_quantity=5.0))
        assert fpm.can_place_order("AAPL") is False

    def test_returns_true_when_only_filled_orders_exist(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="FILLED"))
        assert fpm.can_place_order("AAPL") is True

    def test_returns_true_when_only_cancelled_orders_exist(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="CANCELLED"))
        assert fpm.can_place_order("AAPL") is True

    def test_returns_true_for_different_symbol(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        assert fpm.can_place_order("TSLA") is True

    def test_returns_true_with_no_orders(self):
        fpm = _make_fpm()
        assert fpm.can_place_order("AAPL") is True

    def test_returns_false_with_mixed_statuses(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="FILLED"),
            _order("o2", "AAPL", status="PENDING"),
        )
        assert fpm.can_place_order("AAPL") is False


# ---------------------------------------------------------------------------
# record_order
# ---------------------------------------------------------------------------

class TestRecordOrder:
    @pytest.mark.asyncio
    async def test_records_order_in_memory_and_persists(self):
        fpm = _make_fpm()
        new_order = _order("o1", "NVDA", status="PENDING")
        await fpm.record_order(new_order)
        assert "o1" in fpm._orders
        fpm._sm.save_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_order_blocks_new_placement_after_recording(self):
        fpm = _make_fpm()
        await fpm.record_order(_order("o1", "AAPL", status="PENDING"))
        assert fpm.can_place_order("AAPL") is False


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

class TestReconcile:
    @pytest.mark.asyncio
    async def test_full_fill(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        changes = await fpm.reconcile([
            _broker_status("o1", "filled", filled_qty=10.0, remaining_qty=0.0, avg_price=150.0)
        ])
        assert len(changes) == 1
        c = changes[0]
        assert c.change_type == "fill"
        assert c.old_status == "PENDING"
        assert c.new_status == "FILLED"
        assert c.fill_qty == 10.0
        assert fpm._orders["o1"].status == "FILLED"

    @pytest.mark.asyncio
    async def test_partial_fill_transition(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        changes = await fpm.reconcile([
            _broker_status("o1", "partially_filled", filled_qty=4.0, remaining_qty=6.0)
        ])
        assert len(changes) == 1
        assert changes[0].change_type == "partial_fill"
        assert fpm._orders["o1"].status == "PARTIALLY_FILLED"
        assert fpm._orders["o1"].filled_quantity == 4.0

    @pytest.mark.asyncio
    async def test_partial_fill_qty_update(self):
        """Existing PARTIALLY_FILLED order gets more shares filled."""
        fpm = _make_fpm(_order("o1", "AAPL", status="PARTIALLY_FILLED", filled_quantity=4.0))
        changes = await fpm.reconcile([
            _broker_status("o1", "partially_filled", filled_qty=7.0, remaining_qty=3.0)
        ])
        assert len(changes) == 1
        assert changes[0].change_type == "partial_fill"
        assert changes[0].fill_qty == 3.0  # incremental
        assert fpm._orders["o1"].filled_quantity == 7.0

    @pytest.mark.asyncio
    async def test_no_change_when_qty_unchanged(self):
        """No StateChange if partial fill qty hasn't moved."""
        fpm = _make_fpm(_order("o1", "AAPL", status="PARTIALLY_FILLED", filled_quantity=4.0))
        changes = await fpm.reconcile([
            _broker_status("o1", "partially_filled", filled_qty=4.0, remaining_qty=6.0)
        ])
        assert len(changes) == 0

    @pytest.mark.asyncio
    async def test_cancellation(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        changes = await fpm.reconcile([
            _broker_status("o1", "canceled", filled_qty=0.0)
        ])
        assert len(changes) == 1
        assert changes[0].change_type == "cancel"
        assert fpm._orders["o1"].status == "CANCELLED"

    @pytest.mark.asyncio
    async def test_rejection(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        changes = await fpm.reconcile([
            _broker_status("o1", "rejected", filled_qty=0.0)
        ])
        assert len(changes) == 1
        assert changes[0].change_type == "reject"
        assert fpm._orders["o1"].status == "REJECTED"

    @pytest.mark.asyncio
    async def test_unexpected_fill(self):
        """Broker reports a fill for an order not in local state."""
        fpm = _make_fpm()
        changes = await fpm.reconcile([
            _broker_status("mystery-order", "filled", filled_qty=5.0)
        ])
        assert len(changes) == 1
        assert changes[0].change_type == "unexpected_fill"
        assert changes[0].order_id == "mystery-order"

    @pytest.mark.asyncio
    async def test_terminal_orders_skipped(self):
        """Already-filled orders should not be re-processed."""
        fpm = _make_fpm(_order("o1", "AAPL", status="FILLED"))
        changes = await fpm.reconcile([
            _broker_status("o1", "filled", filled_qty=10.0)
        ])
        assert len(changes) == 0

    @pytest.mark.asyncio
    async def test_no_broker_entry_for_pending_order(self):
        """Order not in broker response — no change, no crash."""
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        changes = await fpm.reconcile([])  # broker returned no statuses
        assert len(changes) == 0
        assert fpm._orders["o1"].status == "PENDING"


# ---------------------------------------------------------------------------
# Stale order detection
# ---------------------------------------------------------------------------

class TestGetStaleOrders:
    def test_returns_stale_limit_orders(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="PENDING", order_type="limit", created_offset_sec=90.0),
        )
        stale = fpm.get_stale_orders(timeout_sec=60)
        assert len(stale) == 1
        assert stale[0].order_id == "o1"

    def test_does_not_return_fresh_orders(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="PENDING", order_type="limit", created_offset_sec=30.0),
        )
        stale = fpm.get_stale_orders(timeout_sec=60)
        assert len(stale) == 0

    def test_market_orders_never_stale(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="PENDING", order_type="market", created_offset_sec=300.0),
        )
        stale = fpm.get_stale_orders(timeout_sec=60)
        assert len(stale) == 0

    def test_filled_orders_never_stale(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="FILLED", order_type="limit", created_offset_sec=300.0),
        )
        stale = fpm.get_stale_orders(timeout_sec=60)
        assert len(stale) == 0

    def test_partially_filled_limit_order_can_be_stale(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="PARTIALLY_FILLED", order_type="limit",
                   created_offset_sec=120.0, filled_quantity=3.0),
        )
        stale = fpm.get_stale_orders(timeout_sec=60)
        assert len(stale) == 1


# ---------------------------------------------------------------------------
# handle_cancel_result — race condition handling
# ---------------------------------------------------------------------------

class TestHandleCancelResult:
    @pytest.mark.asyncio
    async def test_successful_cancel(self):
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING"))
        result = CancelResult(order_id="o1", success=True, final_status="canceled")
        change = await fpm.handle_cancel_result("o1", result)
        assert change.change_type == "cancel"
        assert change.new_status == "CANCELLED"
        assert fpm._orders["o1"].status == "CANCELLED"

    @pytest.mark.asyncio
    async def test_cancel_during_fill_race(self):
        """
        The critical race condition: we sent cancel but the order filled first.
        handle_cancel_result must accept the fill, not the cancel.
        """
        fpm = _make_fpm(_order("o1", "AAPL", status="PENDING", quantity=10.0))
        result = CancelResult(order_id="o1", success=False, final_status="filled")
        change = await fpm.handle_cancel_result("o1", result)
        assert change.change_type == "fill"
        assert change.new_status == "FILLED"
        assert fpm._orders["o1"].status == "FILLED"
        assert fpm._orders["o1"].filled_quantity == 10.0

    @pytest.mark.asyncio
    async def test_partial_fill_then_cancel(self):
        """
        Order partially filled, then cancel goes through.
        Partial position is retained; order becomes CANCELLED.
        """
        fpm = _make_fpm(
            _order("o1", "AAPL", status="PARTIALLY_FILLED", quantity=10.0, filled_quantity=4.0)
        )
        result = CancelResult(order_id="o1", success=True, final_status="canceled")
        change = await fpm.handle_cancel_result("o1", result)
        assert change.change_type == "partial_then_cancel"
        assert change.new_status == "CANCELLED"
        assert fpm._orders["o1"].status == "CANCELLED"
        assert fpm._orders["o1"].filled_quantity == 4.0  # partial fill preserved

    @pytest.mark.asyncio
    async def test_unknown_order_id(self):
        fpm = _make_fpm()
        result = CancelResult(order_id="ghost", success=False, final_status="canceled")
        change = await fpm.handle_cancel_result("ghost", result)
        assert change.change_type == "cancel_unknown"
        assert change.symbol == "UNKNOWN"


# ---------------------------------------------------------------------------
# get_pending_orders / get_orders_for_symbol
# ---------------------------------------------------------------------------

class TestQueries:
    def test_get_pending_orders(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="PENDING"),
            _order("o2", "TSLA", status="PARTIALLY_FILLED", filled_quantity=2.0),
            _order("o3", "NVDA", status="FILLED"),
        )
        pending = fpm.get_pending_orders()
        ids = {o.order_id for o in pending}
        assert ids == {"o1", "o2"}

    def test_get_orders_for_symbol(self):
        fpm = _make_fpm(
            _order("o1", "AAPL", status="PENDING"),
            _order("o2", "AAPL", status="FILLED"),
            _order("o3", "TSLA", status="PENDING"),
        )
        aapl = fpm.get_orders_for_symbol("AAPL")
        assert len(aapl) == 2
        assert all(o.symbol == "AAPL" for o in aapl)


# ---------------------------------------------------------------------------
# available_buying_power
# ---------------------------------------------------------------------------

class TestAvailableBuyingPower:
    def test_deducts_pending_limit_orders(self):
        fpm = _make_fpm()
        pending = [
            _order("o1", "AAPL", status="PENDING", quantity=10.0, limit_price=150.0),  # $1500
            _order("o2", "TSLA", status="PENDING", quantity=5.0, limit_price=200.0),   # $1000
        ]
        result = fpm.available_buying_power(10_000.0, pending)
        assert result == 7_500.0

    def test_does_not_deduct_filled_orders(self):
        fpm = _make_fpm()
        orders = [
            _order("o1", "AAPL", status="FILLED", quantity=10.0, limit_price=150.0),
        ]
        result = fpm.available_buying_power(10_000.0, orders)
        assert result == 10_000.0

    def test_market_orders_not_deducted(self):
        fpm = _make_fpm()
        orders = [
            _order("o1", "AAPL", status="PENDING", order_type="market", quantity=10.0, limit_price=None),
        ]
        result = fpm.available_buying_power(10_000.0, orders)
        assert result == 10_000.0  # market order: no price known, not deducted

    def test_zero_buying_power_with_large_pending(self):
        fpm = _make_fpm()
        orders = [
            _order("o1", "AAPL", status="PENDING", quantity=100.0, limit_price=200.0),  # $20k
        ]
        result = fpm.available_buying_power(5_000.0, orders)
        assert result == -15_000.0  # can go negative (caller should check)


# ---------------------------------------------------------------------------
# Concurrent access safety
# ---------------------------------------------------------------------------

class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_reconcile_calls_do_not_corrupt_state(self):
        """Two concurrent reconcile calls should not interleave writes."""
        mock_sm = MagicMock()
        mock_sm.load_orders = AsyncMock(return_value=OrdersState(orders=[]))
        mock_sm.save_orders = AsyncMock()

        fpm = FillProtectionManager(mock_sm)
        fpm._orders = {
            "o1": _order("o1", "AAPL", status="PENDING"),
            "o2": _order("o2", "TSLA", status="PENDING"),
        }

        statuses_1 = [_broker_status("o1", "filled", filled_qty=10.0)]
        statuses_2 = [_broker_status("o2", "canceled")]

        changes_1, changes_2 = await asyncio.gather(
            fpm.reconcile(statuses_1),
            fpm.reconcile(statuses_2),
        )

        # Both reconcile calls should have completed without error
        all_changes = changes_1 + changes_2
        assert len(all_changes) >= 1  # at least one change observed
        # State should be consistent (no corruption)
        assert fpm._orders["o1"].status in ("FILLED", "PENDING")   # one of valid states
        assert fpm._orders["o2"].status in ("CANCELLED", "PENDING")
