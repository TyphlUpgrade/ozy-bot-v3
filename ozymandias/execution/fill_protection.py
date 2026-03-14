"""
Order state machine and fill protection.

Manages the lifecycle of every order the bot places. This is the most
critical safety module — a bug here results in double orders and uncontrolled
positions.

Order state transitions:
    PENDING → PARTIALLY_FILLED → FILLED
                               → CANCELLED
    PENDING → FILLED
    PENDING → CANCELLED
    PENDING → REJECTED
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ozymandias.core.state_manager import OrderRecord, OrdersState, StateManager
from ozymandias.execution.broker_interface import CancelResult, OrderStatus

log = logging.getLogger(__name__)

# Order statuses that block placing a new order for the same symbol
_BLOCKING_STATUSES = {"PENDING", "PARTIALLY_FILLED"}
# Terminal statuses — no further state transitions expected
_TERMINAL_STATUSES = {"FILLED", "CANCELLED", "REJECTED"}

# Broker status string → local status string
_BROKER_STATUS_MAP = {
    "new":               "PENDING",
    "accepted":          "PENDING",
    "pending_new":       "PENDING",
    "held":              "PENDING",
    "partially_filled":  "PARTIALLY_FILLED",
    "filled":            "FILLED",
    "canceled":          "CANCELLED",
    "cancelled":         "CANCELLED",
    "expired":           "CANCELLED",
    "rejected":          "REJECTED",
    "done_for_day":      "CANCELLED",
    "replaced":          "CANCELLED",
}


def _map_broker_status(broker_status: str) -> str:
    return _BROKER_STATUS_MAP.get(broker_status.lower(), broker_status.upper())


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# StateChange — result type from reconcile / handle_cancel_result
# ---------------------------------------------------------------------------

@dataclass
class StateChange:
    order_id: str
    symbol: str
    old_status: str
    new_status: str
    fill_qty: float = 0.0
    change_type: str = ""  # "fill" | "partial_fill" | "cancel" | "unexpected_fill" | "reject"


# ---------------------------------------------------------------------------
# FillProtectionManager
# ---------------------------------------------------------------------------

class FillProtectionManager:
    """
    Manages order state and enforces fill protection rules.

    All state mutations are persisted atomically via StateManager.
    In-memory dict provides O(1) lookup for the hot path (can_place_order).

    Usage::

        fpm = FillProtectionManager(state_manager)
        await fpm.load()                   # call once on startup

        if fpm.can_place_order("AAPL"):
            result = await broker.place_order(...)
            await fpm.record_order(order_record)

        # In the fast loop:
        changes = await fpm.reconcile(await broker.get_open_orders())
        stale = fpm.get_stale_orders()
    """

    def __init__(self, state_manager: StateManager) -> None:
        self._sm = state_manager
        self._orders: dict[str, OrderRecord] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Load existing order state from disk. Call once at startup."""
        state = await self._sm.load_orders()
        self._orders = {o.order_id: o for o in state.orders}
        log.info(
            "FillProtectionManager loaded %d orders (%d active)",
            len(self._orders),
            len(self.get_pending_orders()),
        )

    # ------------------------------------------------------------------
    # Core double-order prevention
    # ------------------------------------------------------------------

    def can_place_order(self, symbol: str) -> bool:
        """
        Return False if ANY order for this symbol is PENDING or PARTIALLY_FILLED.

        This is the most important method in the module. Must be called before
        every order submission.
        """
        for order in self._orders.values():
            if order.symbol == symbol and order.status in _BLOCKING_STATUSES:
                log.debug(
                    "can_place_order(%s) → False: existing %s order %s",
                    symbol, order.status, order.order_id,
                )
                return False
        return True

    # ------------------------------------------------------------------
    # Record a newly placed order
    # ------------------------------------------------------------------

    async def record_order(self, order: OrderRecord) -> None:
        """Persist a newly submitted order into local state."""
        async with self._lock:
            self._orders[order.order_id] = order
            await self._persist()
        log.debug("Recorded order %s for %s (%s)", order.order_id, order.symbol, order.order_type)

    # ------------------------------------------------------------------
    # Reconcile with broker
    # ------------------------------------------------------------------

    async def reconcile(self, broker_statuses: list[OrderStatus]) -> list[StateChange]:
        """
        Compare broker-reported order statuses against local state.

        Returns a list of StateChange objects for every transition that occurred:
        full fills, partial fills, cancellations, and unexpected fills.

        Expected usage: call on every fast-loop cycle with the result of
        broker.get_open_orders() + explicit status polls for tracked orders.
        """
        changes: list[StateChange] = []
        broker_map = {s.order_id: s for s in broker_statuses}

        async with self._lock:
            # Reconcile locally tracked orders against broker reports
            for order_id, local in list(self._orders.items()):
                if local.status in _TERMINAL_STATUSES:
                    continue  # already done, skip

                broker = broker_map.get(order_id)
                if broker is None:
                    # Broker no longer knows about this order — treat as filled/cancelled
                    # based on context. Only log; do not auto-transition without confirmation.
                    log.debug("Order %s not in broker response; skipping until explicit poll", order_id)
                    continue

                new_local = _map_broker_status(broker.status)
                now = _utcnow_iso()

                if new_local == local.status:
                    # Same status — update fill quantities if partially filled
                    if new_local == "PARTIALLY_FILLED" and broker.filled_qty > local.filled_quantity:
                        old_qty = local.filled_quantity
                        local.filled_quantity = broker.filled_qty
                        local.remaining_quantity = broker.remaining_qty
                        local.last_checked_at = now
                        changes.append(StateChange(
                            order_id=order_id, symbol=local.symbol,
                            old_status="PARTIALLY_FILLED", new_status="PARTIALLY_FILLED",
                            fill_qty=broker.filled_qty - old_qty,
                            change_type="partial_fill",
                        ))
                        log.info("Partial fill update: %s filled=%.4f remaining=%.4f",
                                 local.symbol, local.filled_quantity, local.remaining_quantity)
                    continue

                # Status changed
                old_status = local.status
                local.status = new_local
                local.filled_quantity = broker.filled_qty
                local.remaining_quantity = broker.remaining_qty
                local.last_checked_at = now

                if new_local == "FILLED":
                    local.filled_at = now
                    change_type = "fill"
                    log.info("Order filled: %s %s x%.4f avg=%.4f",
                             local.symbol, local.side, local.filled_quantity,
                             broker.filled_avg_price or 0)
                elif new_local == "CANCELLED":
                    local.cancelled_at = now
                    change_type = "cancel"
                    log.info("Order cancelled: %s %s", local.symbol, order_id)
                elif new_local == "PARTIALLY_FILLED":
                    change_type = "partial_fill"
                    log.info("Order partially filled: %s %.4f/%.4f",
                             local.symbol, broker.filled_qty, local.quantity)
                elif new_local == "REJECTED":
                    change_type = "reject"
                    log.warning("Order rejected: %s %s", local.symbol, order_id)
                else:
                    change_type = new_local.lower()

                changes.append(StateChange(
                    order_id=order_id, symbol=local.symbol,
                    old_status=old_status, new_status=new_local,
                    fill_qty=broker.filled_qty, change_type=change_type,
                ))

            # Check for unexpected fills — broker reports filled orders we don't track
            for order_id, broker in broker_map.items():
                if order_id not in self._orders and _map_broker_status(broker.status) == "FILLED":
                    log.warning(
                        "Unexpected fill from broker: order_id=%s filled_qty=%.4f — "
                        "updating local state immediately",
                        order_id, broker.filled_qty,
                    )
                    changes.append(StateChange(
                        order_id=order_id, symbol="UNKNOWN",
                        old_status="UNKNOWN", new_status="FILLED",
                        fill_qty=broker.filled_qty, change_type="unexpected_fill",
                    ))

            await self._persist()

        return changes

    # ------------------------------------------------------------------
    # Stale order detection
    # ------------------------------------------------------------------

    def get_stale_orders(self, timeout_sec: int = 60) -> list[OrderRecord]:
        """
        Return limit orders in PENDING or PARTIALLY_FILLED state that have been
        open longer than ``timeout_sec``. Market orders are not considered stale
        (they should fill immediately or be rejected).
        """
        now = datetime.now(timezone.utc)
        stale = []
        for order in self._orders.values():
            if order.status not in _BLOCKING_STATUSES:
                continue
            if order.order_type != "limit":
                continue
            if not order.created_at:
                continue
            created = datetime.fromisoformat(order.created_at)
            if (now - created).total_seconds() > timeout_sec:
                stale.append(order)
                log.debug("Stale order detected: %s %s age=%.0fs",
                          order.symbol, order.order_id,
                          (now - created).total_seconds())
        return stale

    # ------------------------------------------------------------------
    # Cancel result handling (race condition safe)
    # ------------------------------------------------------------------

    async def handle_cancel_result(self, order_id: str, result: CancelResult) -> StateChange:
        """
        Process the result of a cancel request.

        Handles all three outcomes:
        - fully cancelled → local PENDING/PARTIALLY_FILLED → CANCELLED
        - filled before cancel could execute → local → FILLED (accept the fill)
        - partially filled then cancelled → local → CANCELLED (partial qty retained)

        Never assumes a cancel succeeded without broker confirmation.
        """
        async with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                log.warning("handle_cancel_result: unknown order_id=%s", order_id)
                return StateChange(
                    order_id=order_id, symbol="UNKNOWN",
                    old_status="UNKNOWN", new_status=result.final_status.upper(),
                    change_type="cancel_unknown",
                )

            old_status = order.status
            now = _utcnow_iso()
            final = result.final_status.lower()

            if final == "filled":
                # Race condition: filled before cancel reached the exchange
                order.status = "FILLED"
                order.filled_quantity = order.quantity
                order.remaining_quantity = 0.0
                order.filled_at = now
                change_type = "fill"
                log.warning(
                    "Cancel-during-fill race: %s order %s was filled before cancel executed",
                    order.symbol, order_id,
                )
            elif final in ("canceled", "cancelled"):
                order.status = "CANCELLED"
                order.cancelled_at = now
                # Preserve any partial fill quantity already recorded
                change_type = "partial_then_cancel" if order.filled_quantity > 0 else "cancel"
                if order.filled_quantity > 0:
                    log.info(
                        "Partial-fill-then-cancel: %s order %s — %.4f shares filled, rest cancelled",
                        order.symbol, order_id, order.filled_quantity,
                    )
            else:
                # Unexpected final state — record as-is and log
                order.status = final.upper()
                change_type = f"cancel_{final}"
                log.warning("Unexpected cancel result for %s: final_status=%s", order_id, final)

            order.last_checked_at = now
            await self._persist()

        return StateChange(
            order_id=order_id, symbol=order.symbol,
            old_status=old_status, new_status=order.status,
            fill_qty=order.filled_quantity, change_type=change_type,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_pending_orders(self) -> list[OrderRecord]:
        """All orders in PENDING or PARTIALLY_FILLED state."""
        return [o for o in self._orders.values() if o.status in _BLOCKING_STATUSES]

    def get_orders_for_symbol(self, symbol: str) -> list[OrderRecord]:
        """All tracked orders for a symbol (any status)."""
        return [o for o in self._orders.values() if o.symbol == symbol]

    # ------------------------------------------------------------------
    # Buying power utility
    # ------------------------------------------------------------------

    def available_buying_power(
        self, reported: float, pending_orders: list[OrderRecord]
    ) -> float:
        """
        Calculate buying power after deducting pending order commitments.

        For limit orders: deducts quantity * limit_price.
        For market orders: no deduction (price unknown; broker handles margin check).

        available = reported_buying_power - sum(pending_order_values)
        """
        consumed = sum(
            o.quantity * (o.limit_price or 0.0)
            for o in pending_orders
            if o.status in _BLOCKING_STATUSES and o.order_type == "limit"
        )
        available = reported - consumed
        log.debug("available_buying_power: reported=%.2f consumed=%.2f available=%.2f",
                  reported, consumed, available)
        return available

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _persist(self) -> None:
        """Write current in-memory order state to disk. Caller must hold _lock."""
        state = OrdersState(orders=list(self._orders.values()))
        await self._sm.save_orders(state)
