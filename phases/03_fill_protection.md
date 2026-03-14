# Phase 03: Order State Machine + Fill Protection + PDT Guard

Read sections 7.1 (Fill Protection), 7.2 (PDT Protection), and 5.4 (Order State Machine) of `ozymandias_v3_spec_revised.md`.

## Context
Phase 01 gave us: state manager (with `OrderRecord` dataclass), logging, config.
Phase 02 gave us: `BrokerInterface` ABC and `AlpacaBroker` implementation.

This phase is the safety net. It must be bulletproof before any trading logic is added.

## What to Build

### 1. Order state machine (`execution/fill_protection.py`)

This module manages the lifecycle of every order the bot places. Read section 7.1 carefully — every rule matters.

**Order states:** `PENDING` → `PARTIALLY_FILLED` → `FILLED` | `CANCELLED` | `REJECTED`

Implement a `FillProtectionManager` class:
- `can_place_order(symbol: str) -> bool`: The core double-order prevention check. Returns False if ANY order for this symbol is in `PENDING` or `PARTIALLY_FILLED` state. This is the most important method.
- `record_order(order: OrderRecord) -> None`: Record a newly placed order in local state.
- `reconcile(broker_statuses: list[OrderStatus]) -> list[StateChange]`: Compare broker-reported order statuses against local state. Return a list of state changes that occurred (fills, cancellations, partial fills, unexpected fills). Update local state accordingly.
- `get_stale_orders(timeout_sec: int = 60) -> list[OrderRecord]`: Return pending limit orders older than the timeout.
- `handle_cancel_result(order_id: str, result: CancelResult) -> StateChange`: Process the result of a cancel request. If the order was filled between the cancel decision and execution, accept the fill. If partially filled then cancelled, record the partial position. Never assume a cancel succeeded.
- `get_pending_orders() -> list[OrderRecord]`: All orders in PENDING or PARTIALLY_FILLED state.
- `get_orders_for_symbol(symbol: str) -> list[OrderRecord]`: All active orders for a symbol.

**Critical race condition handling (section 7.1):**
The cancel-during-fill race: we decide to cancel, but the order fills between our decision and the API call. After issuing cancel, we MUST poll the broker for final state before proceeding. The `handle_cancel_result` method must handle: fully cancelled, filled before cancel, partially filled then cancelled.

All state mutations go through the state manager (atomic writes to `orders.json`).

### 2. PDT guard (`execution/pdt_guard.py`)

Implement a `PDTGuard` class per section 7.2:
- `count_day_trades(orders: list[OrderRecord], portfolio: PortfolioState) -> int`: Count round-trips (buy + sell same symbol same day) in the rolling 5-business-day window.
- `can_day_trade(symbol: str) -> tuple[bool, str]`: Check if a new order that would constitute a day trade is allowed. Account for the configurable buffer (default: 1 reserved for emergencies). Return (allowed, reason).
- `is_emergency_exit(symbol: str) -> bool`: Used to determine if this trade should use the reserved emergency buffer. Called by the risk manager when a quant override fires.
- `check_equity_floor(account: AccountInfo) -> tuple[bool, str]`: If equity < $25,500, block all new entries (not just day trades). Return (trading_allowed, reason).

A "day trade" is opening and closing the same position on the same calendar day (Eastern time). Track this by examining order history — if a symbol has both a buy fill and a sell fill on the same ET calendar day, that's a day trade.

### 3. Buying power tracker

Add a method to the fill protection manager or create a utility:
- `available_buying_power(reported: float, pending_orders: list[OrderRecord]) -> float`: Calculate `reported_buying_power - sum(pending_order_values)`. Pending order value = quantity * limit_price (for limit orders) or quantity * estimated_market_price (for market orders).

## Tests to Write

Create `tests/test_fill_protection.py` — this needs to be thorough:
- Test `can_place_order` returns False when a PENDING order exists for the symbol
- Test `can_place_order` returns False when a PARTIALLY_FILLED order exists
- Test `can_place_order` returns True when only FILLED/CANCELLED orders exist
- Test `reconcile` correctly handles: full fill, partial fill, cancellation, unexpected fill (broker reports fill we don't have locally)
- Test stale order detection with various timestamps
- Test the cancel-during-fill race: simulate cancel request returning "filled" instead of "cancelled"
- Test the partial-fill-then-cancel race: simulate cancel after partial fill
- Test concurrent access safety (two reconcile calls shouldn't corrupt state)

Create `tests/test_pdt_guard.py`:
- Test day trade counting across a 5-day rolling window
- Test buffer logic: with buffer=1 and 2 existing day trades, new day trade is blocked
- Test emergency exit bypasses the buffer
- Test equity floor check blocks all entries below $25,500
- Test that holding overnight (buy Monday, sell Tuesday) does NOT count as a day trade
- Test weekend boundary (buy Friday, sell Monday is not a day trade)

## Done When
- All tests pass
- You can walk through the cancel-during-fill race condition in the tests and verify correct behavior at each step
- The PDT guard correctly counts day trades across realistic multi-day scenarios
