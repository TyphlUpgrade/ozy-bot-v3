"""
tests/debug_loop_isolation.py
==============================
Runs ONE cycle of each orchestrator loop with ALL external dependencies mocked.
Verifies no crash and that key side-effects occurred.

Mocked:
  - AlpacaBroker (init, get_account, get_open_orders, get_positions,
    get_market_hours, get_order_status, cancel_order, place_order)
  - YFinanceAdapter.fetch_bars  (returns synthetic OHLCV DataFrame)
  - Anthropic API messages.create (returns canned Claude JSON)

Run with:
    PYTHONPATH=. python ozymandias/tests/debug_loop_isolation.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)-8s %(name)s  %(message)s",
    stream=sys.stdout,
)

_USE_COLOR = sys.stdout.isatty()
def _green(s): return f"\033[92m{s}\033[0m" if _USE_COLOR else s
def _red(s):   return f"\033[91m{s}\033[0m" if _USE_COLOR else s
def _bold(s):  return f"\033[1m{s}\033[0m"  if _USE_COLOR else s
def _dim(s):   return f"\033[2m{s}\033[0m"  if _USE_COLOR else s

_results: list[tuple[str, bool, str]] = []

def _pass(label: str, detail: str = "") -> None:
    print(f"  [{_green('PASS')}] {label}" + (f"  {_dim(detail)}" if detail else ""))
    _results.append((label, True, detail))

def _fail(label: str, detail: str = "") -> None:
    print(f"  [{_red('FAIL')}] {label}" + (f"\n         {_red(detail)}" if detail else ""))
    _results.append((label, False, detail))

def _section(title: str) -> None:
    print(f"\n{_bold(title)}")
    print("  " + "─" * (len(title) + 2))


# ---------------------------------------------------------------------------
# Synthetic market data
# ---------------------------------------------------------------------------

def make_synthetic_df(n: int = 60, base_price: float = 150.0) -> pd.DataFrame:
    """60 bars of plausible OHLCV data — enough for RSI/MACD/ATR to work."""
    rng = np.random.default_rng(42)
    returns = rng.normal(0, 0.005, n)
    closes = base_price * np.cumprod(1 + returns)
    highs  = closes * (1 + rng.uniform(0.001, 0.008, n))
    lows   = closes * (1 - rng.uniform(0.001, 0.008, n))
    opens  = closes * (1 + rng.normal(0, 0.002, n))
    vols   = rng.integers(200_000, 800_000, n).astype(float)
    start  = datetime(2026, 3, 15, 9, 30, tzinfo=timezone.utc)
    idx    = pd.DatetimeIndex([start + timedelta(minutes=5 * i) for i in range(n)])
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Broker stubs
# ---------------------------------------------------------------------------

from ozymandias.execution.broker_interface import (
    AccountInfo, BrokerPosition, CancelResult, MarketHours, OrderResult, OrderStatus,
)

_NOW = datetime.now(timezone.utc)

STUB_ACCOUNT = AccountInfo(
    equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
    currency="USD", pdt_flag=False, daytrade_count=0, account_id="test-001",
)

STUB_MARKET_HOURS = MarketHours(
    is_open=True, session="regular",
    next_open=_NOW - timedelta(hours=2),
    next_close=_NOW + timedelta(hours=4),
)

STUB_ORDER_RESULT = OrderResult(
    order_id="mock-order-001", status="pending_new", submitted_at=_NOW,
)

STUB_ORDER_STATUS = OrderStatus(
    order_id="mock-order-001", status="filled",
    filled_qty=0.0, remaining_qty=0.0,
    filled_avg_price=None, submitted_at=None, filled_at=None, canceled_at=None,
)

STUB_CANCEL_RESULT = CancelResult(
    order_id="mock-order-001", success=True, final_status="canceled",
)


def _make_broker_mock() -> MagicMock:
    broker = MagicMock()
    broker.get_account      = AsyncMock(return_value=STUB_ACCOUNT)
    broker.get_open_orders  = AsyncMock(return_value=[])
    broker.get_positions    = AsyncMock(return_value=[])
    broker.get_order_status = AsyncMock(return_value=STUB_ORDER_STATUS)
    broker.cancel_order     = AsyncMock(return_value=STUB_CANCEL_RESULT)
    broker.place_order      = AsyncMock(return_value=STUB_ORDER_RESULT)
    broker.get_market_hours = AsyncMock(return_value=STUB_MARKET_HOURS)
    return broker


# ---------------------------------------------------------------------------
# Claude stub
# ---------------------------------------------------------------------------

def _mock_anthropic_response(text: str) -> MagicMock:
    content_block = MagicMock()
    content_block.text = text
    usage = MagicMock()
    usage.input_tokens  = 800
    usage.output_tokens = 200
    resp = MagicMock()
    resp.content = [content_block]
    resp.usage   = usage
    return resp


CLAUDE_RESPONSE = {
    "timestamp":         _NOW.isoformat(),
    "market_assessment": "cautiously bullish",
    "risk_flags":        [],
    "position_reviews": [
        {
            "symbol":             "AAPL",
            "thesis_intact":      True,
            "thesis_assessment":  "holding",
            "recommended_action": "hold",
            "updated_reasoning":  "Momentum intact, raising profit target.",
            "adjusted_targets":   {"profit_target": 240.0, "stop_loss": 198.0},
            "notes":              "Scale in on 1% VWAP pullbacks.",
        }
    ],
    "new_opportunities": [],
    "watchlist_changes": {
        "add": [
            {
                "symbol":        "NVDA",
                "reason":        "Breakout above 200-day SMA on elevated volume",
                "priority_tier": 1,
                "strategy":      "momentum",
            }
        ],
        "remove":    ["TSLA"],
        "rationale": "TSLA thesis broken by VWAP failure; NVDA added for breakout play.",
    },
}


# ---------------------------------------------------------------------------
# State seeding
# ---------------------------------------------------------------------------

async def _seed_state(orch) -> None:
    from ozymandias.core.state_manager import (
        ExitTargets, PortfolioState, Position, TradeIntention,
        WatchlistEntry, WatchlistState,
    )
    now_iso = _NOW.isoformat()

    watchlist = WatchlistState(entries=[
        WatchlistEntry(symbol="AAPL", date_added=now_iso,
                       reason="momentum candidate", priority_tier=1),
        WatchlistEntry(symbol="TSLA", date_added=now_iso,
                       reason="momentum candidate", priority_tier=1),
    ])
    await orch._state_manager.save_watchlist(watchlist)

    portfolio = PortfolioState(
        cash=50_000.0,
        buying_power=80_000.0,
        positions=[
            Position(
                symbol="AAPL", shares=20, avg_cost=205.0,
                entry_date="2026-03-14",
                intention=TradeIntention(
                    strategy="momentum",
                    # Targets set far from current price so no override/near-target fires
                    exit_targets=ExitTargets(profit_target=999.0, stop_loss=1.0),
                    review_notes=[],
                ),
            )
        ],
    )
    await orch._state_manager.save_portfolio(portfolio)

    # Pre-populate indicators so fast loop quant-override step can read a price
    orch._latest_indicators = {
        "AAPL": {
            "price": 215.0, "rsi": 62.0, "vwap_position": "above",
            "atr_14": 2.5, "avg_daily_volume": 60_000_000,
        },
    }


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------

async def build_orchestrator(tmpdir: Path):
    """Instantiate a real Orchestrator, run _startup() with all external deps mocked,
    then swap broker/data-adapter/Claude to controllable AsyncMocks."""
    with (
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.__init__",
              MagicMock(return_value=None)),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_account",
              AsyncMock(return_value=STUB_ACCOUNT)),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours",
              AsyncMock(return_value=STUB_MARKET_HOURS)),
        patch("anthropic.AsyncAnthropic", MagicMock),
        patch("ozymandias.core.orchestrator.Orchestrator._load_credentials",
              MagicMock(return_value=("k", "s"))),
    ):
        from ozymandias.core.orchestrator import Orchestrator
        orch = Orchestrator()
        orch._state_manager._dir = tmpdir
        orch._reasoning_cache._dir = tmpdir / "cache"
        (tmpdir / "cache").mkdir(exist_ok=True)
        await orch._startup()

    # Swap broker with a fresh AsyncMock so we get clean call counts
    orch._broker = _make_broker_mock()

    # Swap data adapter
    orch._data_adapter = MagicMock()
    orch._data_adapter.fetch_bars = AsyncMock(return_value=make_synthetic_df())

    # Wire Claude mock — stub template has no {placeholder} tokens so call_claude
    # won't raise ValueError for missing keys
    stub_template = "Analyze the market. Respond only with JSON."
    orch._claude._load_prompt = MagicMock(return_value=stub_template)
    orch._claude._client = MagicMock()
    orch._claude._client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(json.dumps(CLAUDE_RESPONSE))
    )

    await _seed_state(orch)
    return orch


# ---------------------------------------------------------------------------
# Test: fast loop
# ---------------------------------------------------------------------------

async def test_fast_loop(orch) -> bool:
    _section("Fast Loop  (_fast_loop_cycle)")
    start = len(_results)

    exc_text = None
    try:
        await orch._fast_loop_cycle()
    except Exception:
        exc_text = traceback.format_exc()

    if exc_text:
        _fail("_fast_loop_cycle() raised an exception", exc_text.strip().splitlines()[-1])
        print(f"\n{exc_text}")
        return False
    _pass("_fast_loop_cycle() completed without exception")

    # poll-and-reconcile: broker.get_open_orders should be called
    n = orch._broker.get_open_orders.call_count
    if n >= 1:
        _pass("broker.get_open_orders() called  →  reconcile step ran", f"calls={n}")
    else:
        _fail("broker.get_open_orders() NOT called  →  poll/reconcile step skipped")

    # position sync: broker.get_positions should be called
    n = orch._broker.get_positions.call_count
    if n >= 1:
        _pass("broker.get_positions() called  →  position-sync step ran", f"calls={n}")
    else:
        _fail("broker.get_positions() NOT called  →  position-sync step skipped")

    # quant overrides: _latest_indicators was seeded; indicators exist for AAPL
    # but exit_targets (999/1) are far from price, so no override should fire
    placed = orch._broker.place_order.call_count
    if placed == 0:
        _pass("No override exit order placed  (targets 999/1 safely outside price range)")
    else:
        _fail("place_order() called unexpectedly in fast loop", f"calls={placed}")

    return all(ok for _, ok, _ in _results[start:])


# ---------------------------------------------------------------------------
# Test: medium loop
# ---------------------------------------------------------------------------

async def test_medium_loop(orch) -> bool:
    _section("Medium Loop  (_medium_loop_cycle)")
    start = len(_results)

    # Reset broker call counts from fast loop run
    orch._broker.get_account.reset_mock()
    prev_fetch = orch._data_adapter.fetch_bars.call_count

    exc_text = None
    try:
        await orch._medium_loop_cycle()
    except Exception:
        exc_text = traceback.format_exc()

    if exc_text:
        _fail("_medium_loop_cycle() raised an exception", exc_text.strip().splitlines()[-1])
        print(f"\n{exc_text}")
        return False
    _pass("_medium_loop_cycle() completed without exception")

    # Verify fetch_bars was called (at least once per symbol in watchlist)
    new_calls = orch._data_adapter.fetch_bars.call_count - prev_fetch
    if new_calls >= 1:
        _pass("data_adapter.fetch_bars() called  →  market data fetch step ran",
              f"new calls={new_calls}")
    else:
        _fail("data_adapter.fetch_bars() NOT called  →  TA scan may have been skipped")

    # Verify _latest_indicators was (re)populated by the TA step
    indicators = getattr(orch, "_latest_indicators", {})
    if indicators:
        _pass("_latest_indicators populated by TA step",
              f"symbols={sorted(indicators)}")
    else:
        _fail("_latest_indicators empty after medium loop  →  TA step may have failed")

    # Verify broker.get_account called (used in ranker + position eval)
    n = orch._broker.get_account.call_count
    if n >= 1:
        _pass("broker.get_account() called  →  opportunity ranking/position-eval ran",
              f"calls={n}")
    else:
        _fail("broker.get_account() NOT called  →  ranking/eval may have been skipped")

    return all(ok for _, ok, _ in _results[start:])


# ---------------------------------------------------------------------------
# Test: slow loop
# ---------------------------------------------------------------------------

async def test_slow_loop(orch) -> bool:
    _section("Slow Loop  (_slow_loop_cycle)")
    start = len(_results)

    # Reset call counts so we measure only this cycle
    orch._broker.get_account.reset_mock()
    orch._claude._client.messages.create.reset_mock()

    # Force time_ceiling trigger by back-dating last call by 2 hours
    orch._trigger_state.last_claude_call_utc = _NOW - timedelta(hours=2)
    orch._trigger_state.last_override_exit_count = 0
    orch._override_exit_count = 0

    # Match last_session to current session so session_open/close don't also fire
    from ozymandias.core.market_hours import get_current_session
    orch._trigger_state.last_session = get_current_session().value

    exc_text = None
    try:
        await orch._slow_loop_cycle()
    except Exception:
        exc_text = traceback.format_exc()

    if exc_text:
        _fail("_slow_loop_cycle() raised an exception", exc_text.strip().splitlines()[-1])
        print(f"\n{exc_text}")
        return False
    _pass("_slow_loop_cycle() completed without exception")

    # Claude was called (proof that a trigger fired and the full cycle ran)
    api_calls = orch._claude._client.messages.create.call_count
    if api_calls >= 1:
        _pass("Anthropic messages.create called  →  trigger fired + Claude cycle ran",
              f"calls={api_calls}")
    else:
        _fail("Anthropic messages.create NOT called  →  trigger may not have fired")

    # broker.get_account called inside _run_claude_cycle (context assembly)
    n = orch._broker.get_account.call_count
    if n >= 1:
        _pass("broker.get_account() called inside _run_claude_cycle", f"calls={n}")
    else:
        _fail("broker.get_account() NOT called  →  Claude context assembly may have failed")

    # last_claude_call_utc was reset to ~now
    ts = orch._trigger_state.last_claude_call_utc
    if ts is None:
        _fail("last_claude_call_utc is still None after cycle")
    else:
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < 5:
            _pass("last_claude_call_utc reset to ~now", f"age={age:.2f}s")
        else:
            _fail("last_claude_call_utc not updated", f"still {age:.0f}s old")

    # claude_call_in_flight cleared after cycle
    if not orch._trigger_state.claude_call_in_flight:
        _pass("claude_call_in_flight cleared to False after cycle")
    else:
        _fail("claude_call_in_flight still True after cycle")

    # Watchlist: NVDA added
    watchlist = await orch._state_manager.load_watchlist()
    wl_syms = {e.symbol for e in watchlist.entries}
    if "NVDA" in wl_syms:
        nvda = next(e for e in watchlist.entries if e.symbol == "NVDA")
        _pass("Watchlist: NVDA added",
              f"tier={nvda.priority_tier}  reason={nvda.reason[:45]!r}")
    else:
        _fail("Watchlist: NVDA NOT added  →  _apply_watchlist_changes may have failed",
              f"symbols present: {sorted(wl_syms)}")

    # Watchlist: TSLA removed
    if "TSLA" not in wl_syms:
        _pass("Watchlist: TSLA removed")
    else:
        _fail("Watchlist: TSLA still present", f"symbols: {sorted(wl_syms)}")

    # Position review: AAPL profit_target updated to 240.0
    portfolio = await orch._state_manager.load_portfolio()
    aapl = next((p for p in portfolio.positions if p.symbol == "AAPL"), None)
    if aapl is None:
        _fail("AAPL position not found in portfolio after slow loop")
    elif aapl.intention.exit_targets.profit_target == 240.0:
        _pass("Position review: AAPL profit_target updated to 240.0",
              f"stop_loss={aapl.intention.exit_targets.stop_loss}")
    else:
        _fail("Position review: AAPL profit_target not updated",
              f"expected 240.0, got {aapl.intention.exit_targets.profit_target}")

    # Position review: note appended
    if aapl and aapl.intention.review_notes:
        _pass("Position review: note appended to AAPL",
              f"{aapl.intention.review_notes[0][:60]!r}")
    elif aapl:
        _fail("Position review: no note appended to AAPL")

    return all(ok for _, ok, _ in _results[start:])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print(_bold("\n══ Loop Isolation Test ══"))
    print(_dim("  One cycle each of fast / medium / slow loop; all external deps mocked.\n"))

    loop_results: dict[str, bool] = {}

    with tempfile.TemporaryDirectory() as tmpdir:
        orch = await build_orchestrator(Path(tmpdir))

        loop_results["fast"]   = await test_fast_loop(orch)
        loop_results["medium"] = await test_medium_loop(orch)
        loop_results["slow"]   = await test_slow_loop(orch)

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total  = len(_results)

    print(f"\n{'─' * 55}")
    print(_bold("  Loop summary:"))
    for name, ok in loop_results.items():
        badge = _green("PASS") if ok else _red("FAIL")
        print(f"    {name:<8}  [{badge}]")

    print()
    overall = (_green(f"ALL {total} ASSERTIONS PASSED")
               if failed == 0
               else _red(f"{failed}/{total} ASSERTIONS FAILED"))
    print(f"  {overall}  ({passed} passed, {failed} failed)")
    print(f"{'─' * 55}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
