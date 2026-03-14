# Phase 02: Broker Abstraction + Alpaca Paper Trading

Read section 4.8 (Broker Abstraction Layer) of `ozymandias_v3_spec_revised.md`.

## Context
Phase 01 gave us: state manager, config, logging, market hours, and data models. You now have `OrderRecord`, `Position`, and other dataclasses defined in the state manager module.

## What to Build

### 1. Broker interface (`execution/broker_interface.py`)
Implement the abstract async `BrokerInterface` exactly as specified in section 4.8. This is an ABC with all methods as `abstractmethod`. Also define the data types it uses:
- `AccountInfo` (equity, buying_power, cash, currency, pdt_flag, etc.)
- `Order` (symbol, side, quantity, order_type, limit_price, time_in_force, etc.)
- `OrderResult` (order_id, status, submitted_at)
- `OrderStatus` (order_id, status, filled_qty, remaining_qty, filled_avg_price, etc.)
- `CancelResult` (order_id, success, final_status)
- `Fill` (order_id, symbol, side, qty, price, timestamp)
- `BrokerPosition` (symbol, qty, avg_entry_price, current_price, market_value, unrealized_pl)
- `MarketHours` (is_open, next_open, next_close, session type)

Use dataclasses for all of these. Keep them broker-agnostic — no Alpaca-specific fields.

### 2. Alpaca implementation (`execution/alpaca_broker.py`)
Implement `AlpacaBroker(BrokerInterface)`:
- Use `alpaca-py` SDK with its async client.
- Constructor takes API key, secret key, and environment (`paper` or `live`). Load these from config/credentials.
- Base URL switches based on environment: `https://paper-api.alpaca.markets` for paper, `https://api.alpaca.markets` for live.
- Implement every method from the interface. Map between Alpaca's data types and the broker-agnostic dataclasses.
- Include retry logic with exponential backoff (base 5s, max 5min) for transient failures (timeouts, 5xx).
- Log every API call at DEBUG level, every order action at INFO level.

Key implementation details:
- `place_order()`: support market and limit orders. Set `time_in_force` to `day` by default for momentum trades, `gtc` for swing trades. Return immediately after submission — do not wait for fill.
- `cancel_order()`: issue cancel, then poll until the broker confirms cancellation or reports a fill. Return the final state. This is critical for race condition handling.
- `get_fills()`: filter by timestamp. Map to the `Fill` dataclass.
- `is_market_open()`: use Alpaca's clock endpoint.

### 3. Connection validation script
Create a small `scripts/validate_broker.py` that:
- Loads config and credentials
- Instantiates `AlpacaBroker` in paper mode
- Calls `get_account()` and prints account info
- Calls `is_market_open()` and prints result
- Places a tiny limit buy order for a cheap stock far below market price
- Waits 2 seconds, checks order status
- Cancels the order
- Verifies cancellation
- Prints "Broker connection validated" on success

This is a manual smoke test, not a unit test. It requires real Alpaca paper credentials.

## Tests to Write

Create `tests/test_broker_interface.py`:
- Test that `AlpacaBroker` implements all methods of `BrokerInterface` (use ABC enforcement)
- Test data type conversions with mocked Alpaca responses (mock the alpaca-py client)
- Test retry logic fires on simulated 5xx errors
- Test that paper vs live URL selection works correctly from config

**Do not write tests that hit the real Alpaca API.** Mock everything in unit tests.

## Done When
- All unit tests pass
- The `validate_broker.py` script works with real Alpaca paper trading credentials (you'll need to set these up manually)
- You can place and cancel a paper order programmatically
