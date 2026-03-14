"""
Alpaca paper/live trading implementation of BrokerInterface.

All Alpaca-specific imports and logic are contained here.
No other module may import from alpaca directly.

Retry policy: exponential backoff, base 5s, max 300s (5min),
applied to transient failures (connection errors, 5xx responses).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

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

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Retry config
_RETRY_BASE_S = 5.0
_RETRY_MAX_S = 300.0
_RETRY_EXPONENT = 2.0

# How often to poll when waiting for cancel confirmation
_CANCEL_POLL_INTERVAL_S = 0.5
_CANCEL_POLL_TIMEOUT_S = 10.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _current_session(now_utc: datetime) -> str:
    """Determine current market session string from a UTC datetime."""
    now_et = now_utc.astimezone(_ET)
    t = now_et.time()
    from datetime import time
    if time(4, 0) <= t < time(9, 30):
        return "pre_market"
    if time(9, 30) <= t < time(16, 0):
        return "regular"
    if time(16, 0) <= t < time(20, 0):
        return "post_market"
    return "closed"


def _transient_error(exc: Exception) -> bool:
    """Return True if the exception is a transient failure worth retrying."""
    # alpaca-py raises requests exceptions for network errors
    # and APIError for HTTP errors
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in ("ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout"):
        return True
    if "timeout" in msg:
        return True
    # 5xx HTTP errors from alpaca raise APIError; check the message
    for code in ("500", "502", "503", "504"):
        if code in msg:
            return True
    return False


async def _with_retry(coro_factory, label: str):
    """
    Execute an async callable with exponential backoff retry.

    ``coro_factory`` is a zero-argument callable that returns a coroutine.
    """
    delay = _RETRY_BASE_S
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except Exception as exc:
            if not _transient_error(exc):
                raise
            attempt += 1
            log.warning(
                "%s: transient error on attempt %d (%s: %s), retrying in %.1fs",
                label, attempt, type(exc).__name__, exc, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * _RETRY_EXPONENT, _RETRY_MAX_S)


# ---------------------------------------------------------------------------
# Conversion helpers (Alpaca → broker-agnostic)
# ---------------------------------------------------------------------------

def _map_order_status(alpaca_order) -> OrderStatus:
    """Map an alpaca-py Order model to OrderStatus."""
    filled_qty = float(alpaca_order.filled_qty or 0)
    qty = float(alpaca_order.qty or 0)
    remaining = max(0.0, qty - filled_qty)
    avg_price = (
        float(alpaca_order.filled_avg_price)
        if alpaca_order.filled_avg_price is not None
        else None
    )
    return OrderStatus(
        order_id=str(alpaca_order.id),
        status=alpaca_order.status.value if hasattr(alpaca_order.status, "value") else str(alpaca_order.status),
        filled_qty=filled_qty,
        remaining_qty=remaining,
        filled_avg_price=avg_price,
        submitted_at=alpaca_order.submitted_at,
        filled_at=alpaca_order.filled_at,
        canceled_at=alpaca_order.canceled_at,
    )


def _map_position(alpaca_pos) -> BrokerPosition:
    return BrokerPosition(
        symbol=alpaca_pos.symbol,
        qty=float(alpaca_pos.qty),
        avg_entry_price=float(alpaca_pos.avg_entry_price),
        current_price=float(alpaca_pos.current_price or 0),
        market_value=float(alpaca_pos.market_value or 0),
        unrealized_pl=float(alpaca_pos.unrealized_pl or 0),
    )


# ---------------------------------------------------------------------------
# AlpacaBroker
# ---------------------------------------------------------------------------

class AlpacaBroker(BrokerInterface):
    """
    Alpaca paper/live implementation of BrokerInterface.

    Uses alpaca-py's synchronous TradingClient, wrapped in
    asyncio.to_thread() so calls don't block the event loop.
    """

    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        self._paper = paper
        self._client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
        )
        env = "paper" if paper else "live"
        log.info("AlpacaBroker initialised in %s mode", env)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call(self, fn, *args, label: str = "alpaca", **kwargs):
        """Run a synchronous alpaca-py call in a thread with retry."""
        log.debug("Alpaca API call: %s args=%s kwargs=%s", label, args, kwargs)

        async def _run():
            return await asyncio.to_thread(fn, *args, **kwargs)

        return await _with_retry(_run, label)

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def get_account(self) -> AccountInfo:
        acct = await self._call(self._client.get_account, label="get_account")
        return AccountInfo(
            equity=float(acct.equity or 0),
            buying_power=float(acct.buying_power or 0),
            cash=float(acct.cash or 0),
            currency=str(acct.currency or "USD"),
            pdt_flag=bool(acct.pattern_day_trader),
            daytrade_count=int(getattr(acct, "daytrade_count", 0) or 0),
            account_id=str(acct.id),
        )

    async def get_buying_power(self) -> float:
        acct = await self.get_account()
        return acct.buying_power

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def place_order(self, order: Order) -> OrderResult:
        side = OrderSide.BUY if order.side.lower() == "buy" else OrderSide.SELL
        tif_map = {"day": TimeInForce.DAY, "gtc": TimeInForce.GTC}
        tif = tif_map.get(order.time_in_force.lower(), TimeInForce.DAY)

        if order.order_type == "market":
            req = MarketOrderRequest(
                symbol=order.symbol,
                qty=order.quantity,
                side=side,
                time_in_force=tif,
            )
        else:
            if order.limit_price is None:
                raise ValueError(f"limit_price required for limit order on {order.symbol}")
            req = LimitOrderRequest(
                symbol=order.symbol,
                qty=order.quantity,
                side=side,
                time_in_force=tif,
                limit_price=order.limit_price,
            )

        log.info(
            "Placing %s %s order: %s x%.4f @ %s",
            order.order_type, order.side, order.symbol,
            order.quantity, order.limit_price or "market",
        )
        result = await self._call(self._client.submit_order, req, label=f"place_order({order.symbol})")
        log.info("Order submitted: id=%s status=%s", result.id, result.status)

        return OrderResult(
            order_id=str(result.id),
            status=result.status.value if hasattr(result.status, "value") else str(result.status),
            submitted_at=result.submitted_at or _utcnow(),
        )

    async def cancel_order(self, order_id: str) -> CancelResult:
        """
        Cancel an order and poll until the broker confirms cancellation or a fill.

        Critical for race condition handling: an order may fill between the cancel
        request and the confirmation. We poll and return the true final state.
        """
        log.info("Cancelling order %s", order_id)
        try:
            await self._call(
                self._client.cancel_order_by_id, order_id,
                label=f"cancel_order({order_id})",
            )
        except Exception as exc:
            # If already filled/cancelled, the cancel may return 422 — fetch status
            log.warning("Cancel request for %s raised %s: %s", order_id, type(exc).__name__, exc)

        # Poll until terminal state
        deadline = _utcnow().timestamp() + _CANCEL_POLL_TIMEOUT_S
        final_status = "unknown"
        success = False

        while _utcnow().timestamp() < deadline:
            try:
                status = await self.get_order_status(order_id)
                final_status = status.status
                if final_status in ("canceled", "filled", "expired", "rejected"):
                    success = final_status == "canceled"
                    break
            except Exception as exc:
                log.debug("Poll error for %s: %s", order_id, exc)
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_S)

        log.info("Cancel result for %s: success=%s final_status=%s", order_id, success, final_status)
        return CancelResult(order_id=order_id, success=success, final_status=final_status)

    async def get_order_status(self, order_id: str) -> OrderStatus:
        alpaca_order = await self._call(
            self._client.get_order_by_id, order_id,
            label=f"get_order_status({order_id})",
        )
        return _map_order_status(alpaca_order)

    async def get_open_orders(self) -> list[OrderStatus]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = await self._call(self._client.get_orders, req, label="get_open_orders")
        return [_map_order_status(o) for o in (orders or [])]

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[BrokerPosition]:
        positions = await self._call(self._client.get_all_positions, label="get_positions")
        return [_map_position(p) for p in (positions or [])]

    async def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        try:
            pos = await self._call(
                self._client.get_open_position, symbol,
                label=f"get_position({symbol})",
            )
            return _map_position(pos)
        except Exception as exc:
            # 404 → no position
            if "404" in str(exc) or "not found" in str(exc).lower() or "position does not exist" in str(exc).lower():
                return None
            raise

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    async def get_fills(self, since: datetime) -> list[Fill]:
        """
        Return fills since a given UTC datetime.

        Alpaca doesn't have a dedicated fills endpoint in the trading API;
        we fetch closed orders and extract fills from those since ``since``.
        """
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=since,
        )
        orders = await self._call(self._client.get_orders, req, label="get_fills")

        fills: list[Fill] = []
        for o in (orders or []):
            status_val = o.status.value if hasattr(o.status, "value") else str(o.status)
            if status_val != "filled":
                continue
            filled_qty = float(o.filled_qty or 0)
            avg_price = float(o.filled_avg_price or 0)
            side_val = o.side.value if hasattr(o.side, "value") else str(o.side)
            fills.append(Fill(
                order_id=str(o.id),
                symbol=o.symbol,
                side=side_val,
                qty=filled_qty,
                price=avg_price,
                timestamp=o.filled_at or _utcnow(),
            ))
        return fills

    # ------------------------------------------------------------------
    # Market
    # ------------------------------------------------------------------

    async def is_market_open(self) -> bool:
        clock = await self._call(self._client.get_clock, label="is_market_open")
        log.debug("Market clock: is_open=%s", clock.is_open)
        return bool(clock.is_open)

    async def get_market_hours(self) -> MarketHours:
        clock = await self._call(self._client.get_clock, label="get_market_hours")
        now_utc = _utcnow()
        return MarketHours(
            is_open=bool(clock.is_open),
            next_open=clock.next_open,
            next_close=clock.next_close,
            session=_current_session(now_utc),
        )
