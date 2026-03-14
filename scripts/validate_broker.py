#!/usr/bin/env /home/typhlupgrade/.local/share/ozy-bot-v3/.venv/bin/python
"""
Broker connection validation smoke test.

Requires real Alpaca paper trading credentials in ozymandias/config/credentials.enc
(plaintext JSON for now — encrypted storage is a later phase).

Usage:
    PYTHONPATH=. python scripts/validate_broker.py [--debug]

Expected output on success:
    [account] equity=... buying_power=...
    [market]  is_open=True/False  session=...
    [order]   placed limit buy: order_id=... status=...
    [status]  2s later: status=...
    [cancel]  success=True  final_status=canceled
    Broker connection validated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ozymandias.core.logger import setup_logging
from ozymandias.execution.alpaca_broker import AlpacaBroker
from ozymandias.execution.broker_interface import Order


CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "ozymandias" / "config" / "credentials.enc"
# Cheap, highly liquid stock; place limit far below market so it never fills
SMOKE_SYMBOL = "SPY"
SMOKE_QTY = 1
SMOKE_LIMIT_PRICE = 1.00   # $1 limit — will not fill at market


def _load_credentials() -> tuple[str, str]:
    """Load API key and secret from credentials file (plaintext JSON)."""
    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: credentials file not found at {CREDENTIALS_PATH}")
        print("Create it with: {\"api_key\": \"...\", \"secret_key\": \"...\"}")
        sys.exit(1)
    with open(CREDENTIALS_PATH, "r") as f:
        creds = json.load(f)
    api_key = creds.get("api_key") or creds.get("APCA_API_KEY_ID")
    secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: credentials.enc must have 'api_key' and 'secret_key' fields")
        sys.exit(1)
    return api_key, secret_key


async def main(debug: bool = False) -> None:
    log = setup_logging()
    if debug:
        # Also send DEBUG to stdout for terminal visibility
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
                handler.setLevel(logging.DEBUG)
                break

    api_key, secret_key = _load_credentials()

    broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=True)

    # 1. Account info
    print("--- Account ---")
    acct = await broker.get_account()
    print(f"  equity        = ${acct.equity:,.2f}")
    print(f"  buying_power  = ${acct.buying_power:,.2f}")
    print(f"  cash          = ${acct.cash:,.2f}")
    print(f"  pdt_flag      = {acct.pdt_flag}")
    print(f"  daytrade_count= {acct.daytrade_count}")
    print()

    # 2. Market hours
    print("--- Market ---")
    hours = await broker.get_market_hours()
    print(f"  is_open    = {hours.is_open}")
    print(f"  session    = {hours.session}")
    print(f"  next_open  = {hours.next_open}")
    print(f"  next_close = {hours.next_close}")
    print()

    # 3. Place a limit buy far below market (won't fill)
    print("--- Order placement ---")
    order = Order(
        symbol=SMOKE_SYMBOL,
        side="buy",
        quantity=SMOKE_QTY,
        order_type="limit",
        time_in_force="day",
        limit_price=SMOKE_LIMIT_PRICE,
    )
    result = await broker.place_order(order)
    print(f"  order_id   = {result.order_id}")
    print(f"  status     = {result.status}")
    print(f"  submitted  = {result.submitted_at}")
    print()

    # 4. Wait 2 seconds, check status
    print("  waiting 2 seconds...")
    await asyncio.sleep(2)
    status = await broker.get_order_status(result.order_id)
    print(f"  status after 2s = {status.status}")
    print(f"  filled_qty      = {status.filled_qty}")
    print()

    # 5. Cancel the order
    print("--- Cancellation ---")
    cancel = await broker.cancel_order(result.order_id)
    print(f"  success      = {cancel.success}")
    print(f"  final_status = {cancel.final_status}")
    print()

    if not cancel.success:
        print(f"WARNING: cancel did not return success=True (final_status={cancel.final_status})")
        print("This may be expected if the order already expired or was filled.")
    else:
        print("Broker connection validated.")


if __name__ == "__main__":
    asyncio.run(main(debug="--debug" in sys.argv))
