"""
tests/debug_triggers_degradation.py
=====================================
Part 1: Call _check_triggers() directly with manipulated state — no loops.
Part 2: Graceful degradation — Claude timeout, broker failure, safe mode.

Run with:
    PYTHONPATH=. python ozymandias/tests/debug_triggers_degradation.py
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

_NOW = datetime.now(timezone.utc)

# ---------------------------------------------------------------------------
# Orchestrator factory (same pattern as debug_loop_isolation.py)
# ---------------------------------------------------------------------------

from ozymandias.execution.broker_interface import (
    AccountInfo, BrokerPosition, CancelResult, MarketHours, OrderResult, OrderStatus,
)

STUB_ACCOUNT = AccountInfo(
    equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
    currency="USD", pdt_flag=False, daytrade_count=0, account_id="test-001",
)
STUB_MARKET_HOURS = MarketHours(
    is_open=True, session="regular",
    next_open=_NOW - timedelta(hours=2),
    next_close=_NOW + timedelta(hours=4),
)


def _make_broker_mock() -> MagicMock:
    b = MagicMock()
    b.get_account      = AsyncMock(return_value=STUB_ACCOUNT)
    b.get_open_orders  = AsyncMock(return_value=[])
    b.get_positions    = AsyncMock(return_value=[])
    b.get_order_status = AsyncMock(return_value=OrderStatus(
        order_id="x", status="filled", filled_qty=0, remaining_qty=0,
        filled_avg_price=None, submitted_at=None, filled_at=None, canceled_at=None,
    ))
    b.cancel_order     = AsyncMock(return_value=CancelResult("x", True, "canceled"))
    b.place_order      = AsyncMock(return_value=OrderResult("o1", "pending_new", _NOW))
    b.get_market_hours = AsyncMock(return_value=STUB_MARKET_HOURS)
    return b


async def build_orchestrator(tmpdir: Path):
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

    orch._broker = _make_broker_mock()
    orch._claude._load_prompt = MagicMock(return_value="Respond in JSON.")
    orch._claude._client = MagicMock()
    orch._claude._client.messages.create = AsyncMock(
        return_value=_mock_claude_response(json.dumps({
            "timestamp": _NOW.isoformat(),
            "market_assessment": "neutral",
            "risk_flags": [],
            "position_reviews": [],
            "new_opportunities": [],
            "watchlist_changes": {"add": [], "remove": [], "rationale": ""},
        }))
    )
    return orch


def _mock_claude_response(text: str) -> MagicMock:
    cb = MagicMock(); cb.text = text
    u  = MagicMock(); u.input_tokens = 100; u.output_tokens = 50
    r  = MagicMock(); r.content = [cb]; r.usage = u
    return r


# ---------------------------------------------------------------------------
# Part 1: neutral-state helpers
# ---------------------------------------------------------------------------

from ozymandias.core.state_manager import (
    ExitTargets, PortfolioState, Position, TradeIntention,
    WatchlistEntry, WatchlistState,
)
from ozymandias.core.market_hours import get_current_session


async def _apply_neutral_state(orch, *, watchlist_size: int = 15,
                                price_symbol: str = "AAPL",
                                current_price: float = 100.0,
                                last_price: float = 100.0) -> None:
    """
    Set up a state where no triggers should fire, then callers mutate
    exactly the one condition they want to test.
    """
    # Watchlist: N tier-1 entries + the price-test symbol
    entries = [
        WatchlistEntry(symbol=f"F{i:02d}", date_added=_NOW.isoformat(),
                       reason="filler", priority_tier=1)
        for i in range(watchlist_size)
    ]
    if price_symbol not in {e.symbol for e in entries}:
        entries.append(WatchlistEntry(symbol=price_symbol,
                                      date_added=_NOW.isoformat(),
                                      reason="price test", priority_tier=1))
    await orch._state_manager.save_watchlist(WatchlistState(entries=entries))

    # Empty portfolio — no near_target / near_stop / position price_move
    await orch._state_manager.save_portfolio(
        PortfolioState(cash=50_000.0, buying_power=80_000.0, positions=[])
    )

    # Trigger state: everything quiescent
    orch._trigger_state.last_claude_call_utc = _NOW - timedelta(minutes=1)
    orch._trigger_state.last_prices = {price_symbol: last_price}
    orch._trigger_state.last_override_exit_count = 0
    orch._override_exit_count = 0
    orch._trigger_state.last_session = get_current_session().value

    # Indicators: current price matches last (no move)
    orch._latest_indicators = {price_symbol: {"price": current_price}}


# ---------------------------------------------------------------------------
# Trigger test runner
# ---------------------------------------------------------------------------

# Each row: (label, expected_trigger_present, expected_in_list)
# expected_in_list=True  → the named trigger SHOULD appear
# expected_in_list=False → the named trigger should NOT appear
_trigger_rows: list[tuple[str, str, bool, list[str], str]] = []
# (label, trigger_name, expected_present, actual_list, status)


async def _run_trigger_test(
    orch,
    label: str,
    expected_trigger: str,
    expected_present: bool,
    setup_fn,           # async callable that mutates orch state
) -> bool:
    await setup_fn(orch)
    triggers = await orch._check_triggers()
    actual_present = expected_trigger in triggers
    ok = actual_present == expected_present
    status = _green("PASS") if ok else _red("FAIL")
    expected_str = f"IN  results" if expected_present else f"NOT in results"
    actual_str   = f"IN  {triggers}" if actual_present else f"NOT in {triggers}"
    _trigger_rows.append((label, expected_trigger, expected_present, triggers, "PASS" if ok else "FAIL"))
    return ok


# ---------------------------------------------------------------------------
# Part 1: trigger tests
# ---------------------------------------------------------------------------

async def part1_triggers(orch) -> bool:
    print(_bold("\nPart 1 — Trigger Evaluation (_check_triggers)"))
    print("  " + "─" * 60)

    results: list[bool] = []

    # ── time_ceiling ─────────────────────────────────────────────────────────

    async def setup_time_59(o):
        await _apply_neutral_state(o)
        o._trigger_state.last_claude_call_utc = _NOW - timedelta(minutes=59)

    async def setup_time_61(o):
        await _apply_neutral_state(o)
        o._trigger_state.last_claude_call_utc = _NOW - timedelta(minutes=61)

    results.append(await _run_trigger_test(
        orch, "time_ceiling @ 59 min (no trigger)", "time_ceiling", False, setup_time_59))
    results.append(await _run_trigger_test(
        orch, "time_ceiling @ 61 min (trigger)",    "time_ceiling", True,  setup_time_61))

    # ── price_move ───────────────────────────────────────────────────────────

    async def setup_price_2_5(o):
        await _apply_neutral_state(o, current_price=100.0, last_price=100.0)
        o._trigger_state.last_prices["AAPL"] = 100.0
        o._latest_indicators["AAPL"]["price"] = 102.50   # +2.5 %

    async def setup_price_1_5(o):
        await _apply_neutral_state(o, current_price=100.0, last_price=100.0)
        o._trigger_state.last_prices["AAPL"] = 100.0
        o._latest_indicators["AAPL"]["price"] = 101.50   # +1.5 %

    results.append(await _run_trigger_test(
        orch, "price_move AAPL +2.5% (trigger)",    "price_move:AAPL", True,  setup_price_2_5))
    results.append(await _run_trigger_test(
        orch, "price_move AAPL +1.5% (no trigger)", "price_move:AAPL", False, setup_price_1_5))

    # ── watchlist_small ───────────────────────────────────────────────────────

    async def setup_wl_8(o):
        await _apply_neutral_state(o, watchlist_size=8)

    async def setup_wl_15(o):
        await _apply_neutral_state(o, watchlist_size=15)

    results.append(await _run_trigger_test(
        orch, "watchlist_small @ 8 entries (trigger)",    "watchlist_small", True,  setup_wl_8))
    results.append(await _run_trigger_test(
        orch, "watchlist_small @ 15 entries (no trigger)", "watchlist_small", False, setup_wl_15))

    # ── override_exit ─────────────────────────────────────────────────────────

    async def setup_override_set(o):
        await _apply_neutral_state(o)
        o._override_exit_count = 1
        o._trigger_state.last_override_exit_count = 0   # count > last → fire

    async def setup_override_clear(o):
        await _apply_neutral_state(o)
        o._override_exit_count = 0
        o._trigger_state.last_override_exit_count = 0   # equal → no fire

    results.append(await _run_trigger_test(
        orch, "override_exit when count > last (trigger)",    "override_exit", True,  setup_override_set))
    results.append(await _run_trigger_test(
        orch, "override_exit when count == last (no trigger)", "override_exit", False, setup_override_clear))

    # ── summary table ─────────────────────────────────────────────────────────
    print()
    col = [55, 16, 10, 6]
    header = (f"  {'Test':<{col[0]}} {'Expected':<{col[1]}} {'Actual':<{col[2]}} {'':>{col[3]}}")
    print(header)
    print("  " + "─" * (sum(col) + 4))
    for label, trigger, exp_present, actual_list, status in _trigger_rows:
        exp_str  = f"IN results"       if exp_present else "NOT in results"
        act_str  = f"IN {actual_list}" if trigger in actual_list else f"absent"
        badge    = _green(status) if status == "PASS" else _red(status)
        print(f"  {label:<{col[0]}} {exp_str:<{col[1]}} {act_str:<{col[2]}} [{badge}]")

    passed = sum(results)
    total  = len(results)
    print(f"\n  {passed}/{total} trigger tests passed")
    return all(results)


# ---------------------------------------------------------------------------
# Part 2: degradation tests
# ---------------------------------------------------------------------------

async def part2_degradation(orch) -> bool:
    print(_bold("\nPart 2 — Graceful Degradation"))
    print("  " + "─" * 60)

    _deg_rows: list[tuple[str, str, str, str]] = []  # (test, check, expected, actual, status)
    all_ok: list[bool] = []

    def _check(label: str, test_name: str, expected: str, actual: str) -> bool:
        ok = expected == actual
        status = "PASS" if ok else "FAIL"
        _deg_rows.append((test_name, label, expected, actual, status))
        all_ok.append(ok)
        return ok

    # ── 2a. Claude API timeout ───────────────────────────────────────────────
    print(f"\n  {_bold('2a')}  Claude timeout during slow loop cycle")

    # Reset degradation state
    orch._degradation.claude_available       = True
    orch._degradation.claude_backoff_until_utc = None
    orch._claude_failure_count               = 0

    # Mock call_claude to raise asyncio.TimeoutError
    orch._claude.call_claude = AsyncMock(
        side_effect=asyncio.TimeoutError("simulated 120s timeout")
    )

    # Set trigger (time_ceiling) and suppress other triggers
    await _apply_neutral_state(orch, watchlist_size=15)
    orch._trigger_state.last_claude_call_utc     = _NOW - timedelta(hours=2)
    orch._trigger_state.claude_call_in_flight    = False

    exc_text = None
    try:
        await orch._slow_loop_cycle()
    except Exception:
        exc_text = traceback.format_exc()

    crashed = exc_text is not None
    _check("slow loop crash",         "2a Claude timeout", "False", str(crashed))
    _check("claude_available=False",  "2a Claude timeout",
           "False", str(orch._degradation.claude_available))
    _check("claude_backoff set",      "2a Claude timeout",
           "True", str(orch._degradation.claude_backoff_until_utc is not None))
    _check("call_in_flight cleared",  "2a Claude timeout",
           "False", str(orch._trigger_state.claude_call_in_flight))

    if crashed:
        print(f"  EXCEPTION (unexpected):\n{exc_text}")
    else:
        bk = orch._degradation.claude_backoff_until_utc
        bk_sec = round((bk - _NOW).total_seconds()) if bk else None
        print(f"    claude_available={orch._degradation.claude_available}  "
              f"backoff_in~{bk_sec}s  "
              f"failure_count={orch._claude_failure_count}  "
              f"in_flight={orch._trigger_state.claude_call_in_flight}")

    # Restore a working Claude mock for subsequent tests
    orch._claude.call_claude = AsyncMock(return_value=json.dumps({
        "timestamp": _NOW.isoformat(),
        "market_assessment": "neutral", "risk_flags": [],
        "position_reviews": [], "new_opportunities": [],
        "watchlist_changes": {"add": [], "remove": [], "rationale": ""},
    }))

    # ── 2b. Broker failure in fast loop ──────────────────────────────────────
    print(f"\n  {_bold('2b')}  Broker connection error during fast loop cycle")

    # Reset broker degradation
    orch._degradation.broker_available        = True
    orch._degradation.broker_first_failure_utc = None
    orch._degradation.safe_mode               = False

    # Inject failing broker — BOTH get_open_orders AND get_positions raise.
    # If only get_open_orders fails, _fast_step_position_sync will call
    # get_positions() successfully and _mark_broker_available() resets the
    # flag in the same cycle.  A real outage kills all endpoints.
    failing_broker = _make_broker_mock()
    failing_broker.get_open_orders = AsyncMock(
        side_effect=ConnectionError("broker unreachable")
    )
    failing_broker.get_positions = AsyncMock(
        side_effect=ConnectionError("broker unreachable")
    )
    orch._broker = failing_broker

    exc_text = None
    try:
        await orch._fast_loop_cycle()
    except Exception:
        exc_text = traceback.format_exc()

    crashed = exc_text is not None
    _check("fast loop crash",          "2b Broker failure", "False", str(crashed))
    _check("broker_available=False",   "2b Broker failure",
           "False", str(orch._degradation.broker_available))
    _check("first_failure_utc set",    "2b Broker failure",
           "True", str(orch._degradation.broker_first_failure_utc is not None))
    _check("safe_mode still False",    "2b Broker failure",
           "False", str(orch._degradation.safe_mode))

    if crashed:
        print(f"  EXCEPTION (unexpected):\n{exc_text}")
    else:
        print(f"    broker_available={orch._degradation.broker_available}  "
              f"safe_mode={orch._degradation.safe_mode}  "
              f"first_failure_utc={'set' if orch._degradation.broker_first_failure_utc else 'None'}")

    # ── 2c. Safe mode after 6 minutes of broker failures ─────────────────────
    print(f"\n  {_bold('2c')}  Safe mode after 6-min broker outage (timer faked)")

    # Simulate: broker already known-down since 6 minutes ago
    orch._degradation.broker_available         = False
    orch._degradation.broker_first_failure_utc = _NOW - timedelta(minutes=6)
    orch._degradation.safe_mode                = False

    # One more failure triggers the elapsed-time check in _mark_broker_failure
    orch._mark_broker_failure(ConnectionError("still down after 6 min"))

    _check("safe_mode=True",           "2c Safe mode",
           "True", str(orch._degradation.safe_mode))
    print(f"    safe_mode={orch._degradation.safe_mode}  "
          f"(BROKER_SAFE_MODE_SECONDS={orch._degradation.BROKER_SAFE_MODE_SECONDS}s, "
          f"elapsed~360s)")

    # ── degradation summary table ─────────────────────────────────────────────
    print()
    col = [30, 22, 10, 10, 6]
    header = (f"  {'Test':<{col[0]}} {'Check':<{col[1]}} {'Expected':<{col[2]}} "
              f"{'Actual':<{col[3]}} {'':<{col[4]}}")
    print(header)
    print("  " + "─" * (sum(col) + 5))
    for test_name, label, expected, actual, status in _deg_rows:
        badge = _green(status) if status == "PASS" else _red(status)
        print(f"  {test_name:<{col[0]}} {label:<{col[1]}} {expected:<{col[2]}} "
              f"{actual:<{col[3]}} [{badge}]")

    passed = sum(1 for _, _, _, _, s in _deg_rows if s == "PASS")
    total  = len(_deg_rows)
    print(f"\n  {passed}/{total} degradation checks passed")
    return all(all_ok)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print(_bold("\n══ Trigger + Degradation Test ══"))

    with tempfile.TemporaryDirectory() as tmpdir:
        orch = await build_orchestrator(Path(tmpdir))
        p1_ok = await part1_triggers(orch)
        p2_ok = await part2_degradation(orch)

    print(f"\n{'─' * 55}")
    p1_badge = _green("PASS") if p1_ok else _red("FAIL")
    p2_badge = _green("PASS") if p2_ok else _red("FAIL")
    print(_bold("  Section summary:"))
    print(f"    Part 1  Trigger evaluation  [{p1_badge}]")
    print(f"    Part 2  Graceful degradation [{p2_badge}]")
    overall = _green("ALL PASSED") if (p1_ok and p2_ok) else _red("FAILURES DETECTED")
    print(f"\n  Overall: {overall}")
    print(f"{'─' * 55}\n")

    if not (p1_ok and p2_ok):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
