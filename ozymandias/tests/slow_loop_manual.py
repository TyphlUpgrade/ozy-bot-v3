"""
tests/slow_loop_manual.py
==========================
One-shot manual exercise of _slow_loop_cycle() with last_claude_call_utc
set to 2 hours ago so the time-ceiling trigger fires.

Pipeline under test
-------------------
  _slow_loop_cycle()
    → _check_triggers()            time_ceiling fires → ["time_ceiling"]
    → _run_claude_cycle()
        → ClaudeReasoningEngine.run_reasoning_cycle(skip_cache=True)
            → _load_prompt()       mocked: returns stub template
            → call_claude()
                → _client.messages.create()   mocked: returns valid JSON string
            → parse_claude_response()          REAL — verifies parsing works
        → _apply_watchlist_changes()   adds NVDA, removes TSLA
        → _apply_position_reviews()    updates AAPL targets
    → last_claude_call_utc reset

Assertions (printed as PASS / FAIL)
------------------------------------
1. time_ceiling trigger fired (last call was 2 hours ago)
2. _run_claude_cycle was entered (broker.get_account called)
3. Anthropic client was called (messages.create call count)
4. parse_claude_response succeeded (result is not None → verified via watchlist update)
5. Watchlist add: NVDA added with tier 1
6. Watchlist remove: TSLA removed
7. Position review: AAPL profit_target updated to 240.0
8. Position review: review note appended
9. last_claude_call_utc was reset (now within 5s of run time)
10. claude_call_in_flight cleared to False after cycle

Run with:
    PYTHONPATH=. python ozymandias/tests/slow_loop_manual.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)-8s %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("slow_loop_manual")

_USE_COLOR = sys.stdout.isatty()
def _green(s): return f"\033[92m{s}\033[0m" if _USE_COLOR else s
def _red(s):   return f"\033[91m{s}\033[0m" if _USE_COLOR else s
def _bold(s):  return f"\033[1m{s}\033[0m"  if _USE_COLOR else s
def _dim(s):   return f"\033[2m{s}\033[0m"  if _USE_COLOR else s

_results: list[tuple[str, bool, str]] = []

def _pass(label, detail=""):
    print(f"  [{_green('PASS')}] {label}" + (f"  {_dim(detail)}" if detail else ""))
    _results.append((label, True, detail))

def _fail(label, detail=""):
    print(f"  [{_red('FAIL')}] {label}" + (f"\n         {_red(detail)}" if detail else ""))
    _results.append((label, False, detail))

def _section(title):
    print(f"\n{_bold(title)}")
    print("  " + "─" * (len(title) + 2))


# ---------------------------------------------------------------------------
# Valid JSON response that Claude would return
# ---------------------------------------------------------------------------

CLAUDE_RESPONSE = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "market_assessment": "cautiously bullish — momentum holding above VWAP",
    "risk_flags": ["PDT buffer at 1 remaining"],
    "position_reviews": [
        {
            "symbol": "AAPL",
            "thesis_intact": True,
            "thesis_assessment": "momentum intact, raising target slightly",
            "recommended_action": "hold",
            "updated_reasoning": "Strong bid absorption at VWAP; trend structure bullish.",
            "adjusted_targets": {
                "profit_target": 240.0,
                "stop_loss": 198.0,
            },
            "notes": "Consider scaling in on any 1% pullback to VWAP.",
        }
    ],
    "new_opportunities": [
        {
            "symbol": "NVDA",
            "action": "buy",
            "strategy": "momentum",
            "timeframe": "short",
            "conviction": 0.80,
            "suggested_entry": 875.0,
            "suggested_exit": 940.0,
            "suggested_stop": 845.0,
            "position_size_pct": 0.07,
            "reasoning": "Breakout above 200-day SMA on elevated volume.",
        }
    ],
    "watchlist_changes": {
        "add": [
            {
                "symbol": "NVDA",
                "reason": "AI/GPU demand remains elevated — breakout candidate",
                "priority_tier": 1,
                "strategy": "momentum",
            }
        ],
        "remove": ["TSLA"],
        "rationale": "TSLA thesis broken by VWAP failure; NVDA added for breakout.",
    },
}


# ---------------------------------------------------------------------------
# Build an Anthropic-shaped mock response
# ---------------------------------------------------------------------------

def _mock_anthropic_response(text: str):
    """Return an object that looks like anthropic.types.Message."""
    content_block = MagicMock()
    content_block.text = text
    usage = MagicMock()
    usage.input_tokens = 1200
    usage.output_tokens = 340
    resp = MagicMock()
    resp.content = [content_block]
    resp.usage = usage
    return resp


# ---------------------------------------------------------------------------
# State setup
# ---------------------------------------------------------------------------

async def _seed_state(orch) -> None:
    from ozymandias.core.state_manager import (
        ExitTargets, PortfolioState, Position,
        TradeIntention, WatchlistEntry, WatchlistState,
    )
    now_iso = datetime.now(timezone.utc).isoformat()

    # Watchlist: AAPL + TSLA tier1 (TSLA should be removed by Claude's response)
    watchlist = WatchlistState(entries=[
        WatchlistEntry(symbol="AAPL", date_added=now_iso,
                       reason="momentum candidate", priority_tier=1),
        WatchlistEntry(symbol="TSLA", date_added=now_iso,
                       reason="momentum candidate", priority_tier=1),
    ])
    await orch._state_manager.save_watchlist(watchlist)

    # Portfolio: long AAPL position with exit targets
    portfolio = PortfolioState(
        cash=50_000.0,
        buying_power=80_000.0,
        positions=[
            Position(
                symbol="AAPL",
                shares=20,
                avg_cost=205.0,
                entry_date="2026-03-14",
                intention=TradeIntention(
                    strategy="momentum",
                    exit_targets=ExitTargets(profit_target=225.0, stop_loss=195.0),
                    review_notes=[],
                ),
            )
        ],
    )
    await orch._state_manager.save_portfolio(portfolio)

    # Inject synthetic indicators so context assembly has price data
    orch._latest_indicators = {
        "AAPL": {"price": 217.50, "rsi": 62.0, "vwap_position": "above"},
        "TSLA": {"price": 308.00, "rsi": 48.5, "vwap_position": "below"},
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> None:
    print(_bold("\n══ Slow Loop Manual Cycle ══"))
    print(_dim("  last_claude_call_utc = 2 hours ago → time_ceiling trigger should fire"))
    print(_dim("  Claude mocked to return valid JSON: add NVDA, remove TSLA, update AAPL\n"))

    from ozymandias.execution.broker_interface import AccountInfo, MarketHours
    from ozymandias.core.orchestrator import Orchestrator

    stub_account = AccountInfo(
        equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
        currency="USD", pdt_flag=False, daytrade_count=1, account_id="mock-001",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # ── Build orchestrator ──────────────────────────────────────────────
        with (
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.__init__",
                  MagicMock(return_value=None)),
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_account",
                  AsyncMock(return_value=stub_account)),
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours",
                  AsyncMock(return_value=MagicMock(
                      is_open=True, session="regular",
                      next_open=datetime.now(timezone.utc) - timedelta(hours=2),
                      next_close=datetime.now(timezone.utc) + timedelta(hours=4),
                  ))),
            patch("anthropic.AsyncAnthropic", MagicMock),
            patch("ozymandias.core.orchestrator.Orchestrator._load_credentials",
                  MagicMock(return_value=("k", "s"))),
        ):
            orch = Orchestrator()
            orch._state_manager._dir = tmpdir
            orch._reasoning_cache._dir = tmpdir / "cache"
            orch._reasoning_cache._dir.mkdir()
            await orch._startup()

        # Swap broker mock
        orch._broker = MagicMock()
        orch._broker.get_account = AsyncMock(return_value=stub_account)

        # Seed state
        await _seed_state(orch)

        # ── Set trigger state ───────────────────────────────────────────────
        _section("Trigger setup")
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )
        orch._trigger_state.last_override_exit_count = 0
        orch._override_exit_count = 0
        orch._trigger_state.last_session = "regular"  # suppress session_open trigger
        print(f"  last_claude_call_utc = {orch._trigger_state.last_claude_call_utc.isoformat()}")
        print(f"  (2 hours ago — time_ceiling at 60 min should fire)")

        # ── Wire the Anthropic mock into the already-constructed engine ─────
        # mock _load_prompt so it returns a stub template (file doesn't exist yet)
        stub_template = "Analyze the market. Context: {context_json} Respond in JSON."
        orch._claude._load_prompt = MagicMock(return_value=stub_template)

        # mock the Anthropic API response
        anthropic_response = _mock_anthropic_response(json.dumps(CLAUDE_RESPONSE))
        orch._claude._client = MagicMock()
        orch._claude._client.messages.create = AsyncMock(return_value=anthropic_response)

        # ── Intercept triggers before the cycle so we can inspect them ──────
        triggers_seen: list[list[str]] = []
        orig_check = orch._check_triggers

        async def traced_check():
            result = await orig_check()
            triggers_seen.append(result)
            return result

        orch._check_triggers = traced_check

        # ── Run one slow loop cycle ─────────────────────────────────────────
        _section("Running _slow_loop_cycle()")
        run_time = datetime.now(timezone.utc)
        exc_caught = None
        try:
            await orch._slow_loop_cycle()
        except Exception:
            exc_caught = traceback.format_exc()
            traceback.print_exc()

        # ── Assertions ──────────────────────────────────────────────────────
        _section("Results")

        # 1. No unhandled exception
        if exc_caught is None:
            _pass("_slow_loop_cycle() raised no unhandled exception")
        else:
            _fail("_slow_loop_cycle() raised", exc_caught.strip().splitlines()[-1])
            return

        # 2. time_ceiling trigger fired
        all_triggers = triggers_seen[0] if triggers_seen else []
        if "time_ceiling" in all_triggers:
            _pass("time_ceiling trigger fired", f"all triggers: {all_triggers}")
        else:
            _fail("time_ceiling trigger did NOT fire",
                  f"triggers seen: {all_triggers}")

        # 3. broker.get_account was called (proof _run_claude_cycle was entered)
        if orch._broker.get_account.call_count >= 1:
            _pass("_run_claude_cycle entered (broker.get_account called)",
                  f"call_count={orch._broker.get_account.call_count}")
        else:
            _fail("_run_claude_cycle may not have been entered — broker.get_account not called")

        # 4. Anthropic client messages.create was called
        api_calls = orch._claude._client.messages.create.call_count
        if api_calls == 1:
            _pass("Anthropic client messages.create called once", f"call_count={api_calls}")
        else:
            _fail("messages.create call count wrong", f"expected 1, got {api_calls}")

        # 5. parse_claude_response succeeded — verified by checking watchlist was updated
        #    (if parsing failed, watchlist_changes would never be applied)
        watchlist = await orch._state_manager.load_watchlist()
        wl_symbols = {e.symbol for e in watchlist.entries}

        # 6. NVDA added
        if "NVDA" in wl_symbols:
            nvda_entry = next(e for e in watchlist.entries if e.symbol == "NVDA")
            _pass("Watchlist: NVDA added by Claude response",
                  f"tier={nvda_entry.priority_tier}  reason={nvda_entry.reason[:40]!r}")
        else:
            _fail("Watchlist: NVDA NOT added — parse_claude_response may have failed "
                  "or apply_watchlist_changes was not called",
                  f"watchlist symbols: {sorted(wl_symbols)}")

        # 7. TSLA removed
        if "TSLA" not in wl_symbols:
            _pass("Watchlist: TSLA removed by Claude response")
        else:
            _fail("Watchlist: TSLA still present — remove not applied",
                  f"watchlist symbols: {sorted(wl_symbols)}")

        # 8. AAPL still present
        if "AAPL" in wl_symbols:
            _pass("Watchlist: AAPL untouched", f"symbols={sorted(wl_symbols)}")
        else:
            _fail("Watchlist: AAPL unexpectedly removed")

        # 9. parse_claude_response explicitly — reparse and confirm non-None
        from ozymandias.intelligence.claude_reasoning import parse_claude_response
        parsed = parse_claude_response(json.dumps(CLAUDE_RESPONSE))
        if parsed is not None:
            _pass("parse_claude_response returned non-None for the mock JSON",
                  f"keys={sorted(parsed.keys())}")
        else:
            _fail("parse_claude_response returned None for the mock JSON — "
                  "structure is invalid")

        # 10. Position review: profit_target updated
        portfolio = await orch._state_manager.load_portfolio()
        aapl_pos = next((p for p in portfolio.positions if p.symbol == "AAPL"), None)
        if aapl_pos is None:
            _fail("AAPL position not found in portfolio")
        else:
            if aapl_pos.intention.exit_targets.profit_target == 240.0:
                _pass("Position review: AAPL profit_target updated to 240.0",
                      f"stop_loss={aapl_pos.intention.exit_targets.stop_loss}")
            else:
                _fail("Position review: profit_target not updated",
                      f"expected 240.0, got {aapl_pos.intention.exit_targets.profit_target}")

        # 11. Position review: note appended
        if aapl_pos and aapl_pos.intention.review_notes:
            note = aapl_pos.intention.review_notes[0]
            _pass("Position review: note appended to AAPL",
                  f"{note[:70]!r}")
        elif aapl_pos:
            _fail("Position review: no note appended to AAPL")

        # 12. last_claude_call_utc reset to ~now
        updated_ts = orch._trigger_state.last_claude_call_utc
        if updated_ts is None:
            _fail("last_claude_call_utc is still None after successful cycle")
        else:
            age_sec = (datetime.now(timezone.utc) - updated_ts).total_seconds()
            if age_sec < 5:
                _pass("last_claude_call_utc reset to current time",
                      f"age={age_sec:.2f}s")
            else:
                _fail("last_claude_call_utc not updated",
                      f"still {age_sec:.0f}s old")

        # 13. claude_call_in_flight cleared
        if not orch._trigger_state.claude_call_in_flight:
            _pass("claude_call_in_flight cleared to False after cycle")
        else:
            _fail("claude_call_in_flight still True after cycle")

    # ── Summary ─────────────────────────────────────────────────────────────
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
