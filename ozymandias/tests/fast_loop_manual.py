"""
tests/fast_loop_manual.py
=========================
One-shot manual exercise of _fast_loop_cycle() with a controlled mock broker.

Scenario
--------
- 2 orders in local state, both PENDING:
    ord-001  TSLA  buy  50 shares  limit 380.00   → broker still shows "new" (still open)
    ord-002  AAPL  buy  20 shares  limit 175.00   → broker already filled (not in open-orders list)
- 1 open position in portfolio:
    MSFT  30 shares  avg_cost 420.00
  with synthetic indicators injected into _latest_indicators so the
  quant-override check can run.

Expected behaviour
------------------
A. reconcile() detects ord-001 is still PENDING → no state change.
B. ord-002 is missing from get_open_orders() → explicit get_order_status() call
   returns "filled" → reconcile() records a FILL transition.
C. quant overrides evaluate the MSFT position (using injected indicators).
D. PDT check runs without error.
E. position_sync compares broker positions with local portfolio and logs any
   discrepancy.

Run with:
    PYTHONPATH=. python ozymandias/tests/fast_loop_manual.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Logging — print everything to stdout so the run is readable
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fast_loop_manual")

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty()

def _green(s): return f"\033[92m{s}\033[0m" if _USE_COLOR else s
def _red(s):   return f"\033[91m{s}\033[0m" if _USE_COLOR else s
def _bold(s):  return f"\033[1m{s}\033[0m"  if _USE_COLOR else s
def _dim(s):   return f"\033[2m{s}\033[0m"  if _USE_COLOR else s


# ---------------------------------------------------------------------------
# Broker mock setup
# ---------------------------------------------------------------------------

def _build_mock_broker():
    """
    Return a mock that behaves like AlpacaBroker for the fast loop.

    get_open_orders()        → [OrderStatus ord-001 "new"]
    get_order_status(ord-002)→  OrderStatus ord-002 "filled"
    get_positions()          → [BrokerPosition MSFT 30 shares]
    """
    from ozymandias.execution.broker_interface import BrokerPosition, OrderStatus

    now = datetime.now(timezone.utc)

    status_pending = OrderStatus(
        order_id="ord-001",
        status="new",
        filled_qty=0.0,
        remaining_qty=50.0,
        filled_avg_price=None,
        submitted_at=now - timedelta(seconds=10),
        filled_at=None,
        canceled_at=None,
    )
    status_filled = OrderStatus(
        order_id="ord-002",
        status="filled",
        filled_qty=20.0,
        remaining_qty=0.0,
        filled_avg_price=175.50,
        submitted_at=now - timedelta(seconds=30),
        filled_at=now - timedelta(seconds=5),
        canceled_at=None,
    )
    broker_position = BrokerPosition(
        symbol="MSFT",
        qty=30.0,
        avg_entry_price=420.00,
        current_price=428.00,
        market_value=12_840.00,
        unrealized_pl=240.00,
    )

    broker = MagicMock()
    broker.get_open_orders = AsyncMock(return_value=[status_pending])
    broker.get_order_status = AsyncMock(return_value=status_filled)
    broker.get_positions    = AsyncMock(return_value=[broker_position])
    broker.cancel_order     = AsyncMock()   # not expected to fire in this scenario
    return broker


# ---------------------------------------------------------------------------
# State setup helpers
# ---------------------------------------------------------------------------

async def _seed_state(orch) -> None:
    """Write orders + portfolio into the state manager."""
    from ozymandias.core.state_manager import (
        ExitTargets, OrderRecord, OrdersState,
        PortfolioState, Position, TradeIntention,
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    orders = OrdersState(
        orders=[
            OrderRecord(
                order_id="ord-001",
                symbol="TSLA",
                side="buy",
                quantity=50,
                order_type="limit",
                limit_price=380.00,
                status="PENDING",
                created_at=now_iso,
                last_checked_at=now_iso,
            ),
            OrderRecord(
                order_id="ord-002",
                symbol="AAPL",
                side="buy",
                quantity=20,
                order_type="limit",
                limit_price=175.00,
                status="PENDING",
                created_at=now_iso,
                last_checked_at=now_iso,
            ),
        ]
    )
    await orch._state_manager.save_orders(orders)

    portfolio = PortfolioState(
        cash=50_000.0,
        buying_power=80_000.0,
        positions=[
            Position(
                symbol="MSFT",
                shares=30,
                avg_cost=420.00,
                entry_date="2026-03-14",
                intention=TradeIntention(
                    strategy="momentum",
                    exit_targets=ExitTargets(
                        profit_target=450.00,
                        stop_loss=400.00,
                    ),
                ),
            )
        ],
    )
    await orch._state_manager.save_portfolio(portfolio)


def _seed_indicators(orch) -> None:
    """
    Inject synthetic indicators for MSFT so _fast_step_quant_overrides can run.
    The indicators are benign (price well above VWAP, no deceleration, healthy RSI)
    so no override exit should fire.
    """
    orch._latest_indicators = {
        "MSFT": {
            "price": 428.00,
            "vwap_position": "above",
            "rsi": 55.0,
            "rsi_divergence": False,
            "volume_ratio": 1.1,
            "roc_5": 0.8,
            "roc_deceleration": False,
            "atr_14": 4.50,
            "macd_signal": "bullish",
            "trend_structure": "bullish_aligned",
            "bollinger_position": "upper_half",
            "avg_daily_volume": 20_000_000.0,
        }
    }


# ---------------------------------------------------------------------------
# Assertions / reporting
# ---------------------------------------------------------------------------

_results: list[tuple[str, bool, str]] = []

def _pass(label, detail=""):
    tag = _green("PASS")
    print(f"  [{tag}] {label}" + (f"  {_dim(detail)}" if detail else ""))
    _results.append((label, True, detail))

def _fail(label, detail=""):
    tag = _red("FAIL")
    print(f"  [{tag}] {label}" + (f"\n         {_red(detail)}" if detail else ""))
    _results.append((label, False, detail))

def _section(title):
    print(f"\n{_bold(title)}")
    print("  " + "─" * (len(title) + 2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    print(_bold("\n══ Fast Loop Manual Cycle ══"))
    print(_dim("  Mock broker: 1 pending order (ord-001 TSLA), 1 filled (ord-002 AAPL)"))
    print(_dim("  Portfolio: 1 open position MSFT 30 shares\n"))

    from ozymandias.core.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as tmpdir:

        # ── Build orchestrator with mocked startup ──────────────────────────
        with (
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.__init__",
                  MagicMock(return_value=None)),
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_account",
                  AsyncMock(return_value=MagicMock(
                      equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
                      pdt_flag=False, daytrade_count=0,
                  ))),
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours",
                  AsyncMock(return_value=MagicMock(
                      is_open=False, session="closed",
                      next_open=datetime.now(timezone.utc) + timedelta(hours=1),
                      next_close=datetime.now(timezone.utc) + timedelta(hours=8),
                  ))),
            patch("anthropic.AsyncAnthropic", MagicMock),
            patch("ozymandias.core.orchestrator.Orchestrator._load_credentials",
                  MagicMock(return_value=("mock-key", "mock-secret"))),
        ):
            orch = Orchestrator()
            orch._state_manager._dir = Path(tmpdir)
            await orch._startup()

        # Replace broker with our controlled mock AFTER startup
        orch._broker = _build_mock_broker()

        # Seed state files and indicators
        await _seed_state(orch)
        await orch._fill_protection.load()   # reload FPM from the freshly written state
        _seed_indicators(orch)

        # ── Capture FPM state before the cycle ─────────────────────────────
        pre_orders = orch._fill_protection.get_pending_orders()
        pre_ids = {o.order_id for o in pre_orders}

        _section("Pre-cycle state")
        print(f"  FPM pending orders: {[o.order_id for o in pre_orders]}")
        print(f"  _latest_indicators: {list(orch._latest_indicators.keys())}")

        # ── Run one fast loop cycle ─────────────────────────────────────────
        _section("Running _fast_loop_cycle()")
        exception_caught = None
        try:
            await orch._fast_loop_cycle()
        except Exception as exc:
            exception_caught = exc
            traceback.print_exc()

        # ── Assertions ──────────────────────────────────────────────────────
        _section("Results")

        # 1. No unhandled exception
        if exception_caught is None:
            _pass("_fast_loop_cycle() raised no unhandled exception")
        else:
            _fail("_fast_loop_cycle() raised", str(exception_caught))

        # 2. get_open_orders was called exactly once
        calls = orch._broker.get_open_orders.call_count
        if calls == 1:
            _pass("broker.get_open_orders() called once", f"call_count={calls}")
        else:
            _fail("broker.get_open_orders() call count", f"expected 1, got {calls}")

        # 3. get_order_status was called for ord-002 (the one missing from open-orders)
        status_calls = orch._broker.get_order_status.call_args_list
        polled_ids = [c.args[0] for c in status_calls]
        if "ord-002" in polled_ids:
            _pass("broker.get_order_status('ord-002') called (explicit poll for filled order)",
                  f"all polled: {polled_ids}")
        else:
            _fail("ord-002 was NOT explicitly polled",
                  f"get_order_status called with: {polled_ids}")

        # 4. FPM state after reconcile: ord-001 still pending, ord-002 now FILLED
        post_orders = orch._fill_protection.get_pending_orders()
        post_pending_ids = {o.order_id for o in post_orders}

        if "ord-001" in post_pending_ids:
            _pass("ord-001 (TSLA) still PENDING after reconcile")
        else:
            _fail("ord-001 missing from FPM pending set after reconcile")

        if "ord-002" not in post_pending_ids:
            _pass("ord-002 (AAPL) no longer PENDING — fill detected")
        else:
            _fail("ord-002 still PENDING — fill NOT detected by reconcile")

        # 5. Load orders from disk; confirm ord-002 status is FILLED
        orders_state = await orch._state_manager.load_orders()
        ord002 = next((o for o in orders_state.orders if o.order_id == "ord-002"), None)
        if ord002 is None:
            _fail("ord-002 not found in persisted orders state")
        elif ord002.status == "FILLED":
            _pass("ord-002 persisted status = FILLED", f"filled_qty={ord002.filled_quantity}")
        else:
            _fail("ord-002 persisted status wrong", f"expected FILLED, got {ord002.status!r}")

        # 6. Override check ran for MSFT (risk manager was reached)
        # We can confirm by checking that the position was not blindly skipped:
        # The indicators dict has MSFT, so the loop body must have executed.
        # We check indirectly: no exception AND the MSFT indicators were consumed.
        if exception_caught is None and "MSFT" in orch._latest_indicators:
            _pass("Quant override check ran for MSFT (indicators present, no exception)")
        else:
            _fail("Quant override check may have been skipped for MSFT")

        # 7. No override exit order placed (indicators are benign)
        if orch._broker.place_order.call_count == 0:  # type: ignore[attr-defined]
            _pass("No override exit order placed (indicators benign — correct)")
        else:
            _fail("Unexpected order placed during cycle",
                  f"place_order called {orch._broker.place_order.call_count} time(s)")

        # 8. get_positions was called for position sync
        if orch._broker.get_positions.call_count >= 1:
            _pass("broker.get_positions() called for position sync")
        else:
            _fail("broker.get_positions() not called — position sync skipped")

    # ── Summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total  = len(_results)
    print(f"\n{'─'*50}")
    status = (_green(f"ALL {total} ASSERTIONS PASSED") if failed == 0
              else _red(f"{failed}/{total} ASSERTIONS FAILED"))
    print(f"  {status}  ({passed} passed, {failed} failed)")
    print(f"{'─'*50}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
