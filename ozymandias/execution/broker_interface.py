"""
Broker abstraction layer.

All broker-specific code lives behind this interface. No broker imports
are allowed outside execution/alpaca_broker.py.

Data types are broker-agnostic — no Alpaca-specific fields here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Broker-agnostic data types
# ---------------------------------------------------------------------------

@dataclass
class AccountInfo:
    equity: float
    buying_power: float
    cash: float
    currency: str
    pdt_flag: bool          # pattern day trader flag
    daytrade_count: int     # day trades used in rolling 5-session window
    account_id: str


@dataclass
class Order:
    """Represents an order to be placed. Broker-agnostic."""
    symbol: str
    side: str               # "buy" | "sell"
    quantity: float
    order_type: str         # "market" | "limit"
    time_in_force: str      # "day" | "gtc"
    limit_price: Optional[float] = None
    client_order_id: Optional[str] = None


@dataclass
class OrderResult:
    """Returned immediately after order submission."""
    order_id: str
    status: str             # "pending_new" | "new" | "accepted" etc.
    submitted_at: datetime


@dataclass
class OrderStatus:
    """Current state of an order, returned by polling."""
    order_id: str
    status: str             # "new" | "partially_filled" | "filled" | "canceled" | "rejected"
    filled_qty: float
    remaining_qty: float
    filled_avg_price: Optional[float]
    submitted_at: Optional[datetime]
    filled_at: Optional[datetime]
    canceled_at: Optional[datetime]


@dataclass
class CancelResult:
    order_id: str
    success: bool
    final_status: str       # the status at the time of cancellation confirmation


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str               # "buy" | "sell"
    qty: float
    price: float
    timestamp: datetime


@dataclass
class BrokerPosition:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float


@dataclass
class MarketHours:
    is_open: bool
    next_open: datetime
    next_close: datetime
    session: str            # "pre_market" | "regular" | "post_market" | "closed"


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class BrokerInterface(ABC):
    """
    Abstract async broker interface.

    All methods are async. Implementations must handle auth, retries,
    and mapping between broker-specific types and the dataclasses above.
    """

    # Account
    @abstractmethod
    async def get_account(self) -> AccountInfo: ...

    @abstractmethod
    async def get_buying_power(self) -> float: ...

    # Orders
    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> CancelResult: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus: ...

    @abstractmethod
    async def get_open_orders(self) -> list[OrderStatus]: ...

    # Positions
    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[BrokerPosition]: ...

    # Fills
    @abstractmethod
    async def get_fills(self, since: datetime) -> list[Fill]: ...

    # Market
    @abstractmethod
    async def is_market_open(self) -> bool: ...

    @abstractmethod
    async def get_market_hours(self) -> MarketHours: ...
