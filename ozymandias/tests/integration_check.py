"""
tests/integration_check.py
============================
Manual integration verification script — NOT a pytest file.

Run directly:
    PYTHONPATH=. python ozymandias/tests/integration_check.py

Each check instantiates real module instances (no mocks unless noted) and
verifies cross-phase interactions that unit tests with hand-built data cannot
catch.  Every check prints PASS / FAIL with a brief detail line.
"""
from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Colour helpers (degrade gracefully if stdout is not a tty)
# ---------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty()

def _green(s: str) -> str:
    return f"\033[92m{s}\033[0m" if _USE_COLOR else s

def _red(s: str) -> str:
    return f"\033[91m{s}\033[0m" if _USE_COLOR else s

def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _USE_COLOR else s

def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _USE_COLOR else s


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
_results: list[tuple[str, bool, str]] = []


def _pass(name: str, detail: str = "") -> None:
    tag = _green("PASS")
    print(f"  [{tag}] {name}" + (f"  {_dim(detail)}" if detail else ""))
    _results.append((name, True, detail))


def _fail(name: str, detail: str = "") -> None:
    tag = _red("FAIL")
    print(f"  [{tag}] {name}" + (f"\n         {_red(detail)}" if detail else ""))
    _results.append((name, False, detail))


def _section(title: str) -> None:
    print(f"\n{_bold(title)}")
    print("  " + "─" * (len(title) + 2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nan_safe(val) -> bool:
    """Return True if value is a float NaN."""
    try:
        return isinstance(val, float) and math.isnan(val)
    except Exception:
        return False


# ===========================================================================
# Check 1: State → Fill Protection round-trip
# ===========================================================================

async def check1_state_fill_protection() -> None:
    _section("Check 1: State → Fill Protection round-trip")

    try:
        from ozymandias.core.state_manager import OrderRecord, OrdersState, StateManager
        from ozymandias.execution.fill_protection import FillProtectionManager

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateManager(state_dir=Path(tmpdir))
            await sm.initialize()

            # Persist a PENDING order for AAPL
            order = OrderRecord(
                order_id="integ-001",
                symbol="AAPL",
                side="buy",
                quantity=10,
                order_type="limit",
                limit_price=175.00,
                status="PENDING",
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            await sm.save_orders(OrdersState(orders=[order]))

            # Wire up a real FPM backed by the real StateManager
            mock_broker = MagicMock()
            fpm = FillProtectionManager(sm)
            await fpm.load()

            # AAPL should be blocked
            if not fpm.can_place_order("AAPL"):
                _pass("can_place_order('AAPL') → False (blocked by PENDING order)")
            else:
                _fail("can_place_order('AAPL')", "expected False, got True — pending order not loaded")

            # TSLA should be allowed
            if fpm.can_place_order("TSLA"):
                _pass("can_place_order('TSLA') → True (no pending order)")
            else:
                _fail("can_place_order('TSLA')", "expected True, got False — spurious block")

            # Verify loaded order count
            pending = fpm.get_pending_orders()
            if len(pending) == 1 and pending[0].order_id == "integ-001":
                _pass("FPM loaded exactly 1 pending order from disk", f"order_id={pending[0].order_id}")
            else:
                _fail("FPM pending order count", f"expected 1, got {len(pending)}")

    except Exception as exc:
        _fail("Check 1 raised an exception", traceback.format_exc().strip().splitlines()[-1])


# ===========================================================================
# Check 2: yfinance → Technical Analysis pipeline
# ===========================================================================

async def check2_yfinance_ta() -> None:
    _section("Check 2: yfinance → Technical Analysis pipeline")

    try:
        from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
        from ozymandias.intelligence.technical_analysis import generate_signal_summary

        adapter = YFinanceAdapter()

        print("  Fetching AAPL 1-month daily bars from yfinance …")
        df = await adapter.fetch_bars("AAPL", interval="1d", period="1mo")

        if df is None or df.empty:
            _fail("yfinance fetch_bars", "returned empty DataFrame")
            return

        _pass(f"Fetched {len(df)} bars from yfinance", f"columns={list(df.columns)}")

        # Verify lowercase columns
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                _fail(f"Column '{col}' missing from DataFrame")
                return
        _pass("All required columns present (lowercase)")

        result = generate_signal_summary("AAPL", df)

        # Top-level keys
        for key in ("symbol", "timestamp", "signals", "composite_technical_score"):
            if key not in result:
                _fail(f"generate_signal_summary missing top-level key: '{key}'")
                return
        _pass("Top-level keys present in signal summary")

        signals = result["signals"]
        print(f"\n  {'─'*50}")
        print(f"  Signal output for AAPL:")
        for k, v in sorted(signals.items()):
            print(f"    {k:<22} = {v!r}")
        print(f"    {'composite_technical_score':<22} = {result['composite_technical_score']!r}")
        print(f"  {'─'*50}\n")

        # Validate expected numeric keys are not None / NaN
        numeric_keys = ("rsi", "roc_5", "volume_ratio", "atr_14", "price", "avg_daily_volume")
        all_ok = True
        for k in numeric_keys:
            v = signals.get(k)
            if v is None:
                _fail(f"signals['{k}'] is None")
                all_ok = False
            elif _nan_safe(v):
                _fail(f"signals['{k}'] is NaN")
                all_ok = False
        if all_ok:
            _pass("No None or NaN in numeric signal fields")

        # Validate categorical keys have valid values
        cats = {
            "vwap_position": {"above", "at", "below"},
            "macd_signal": {"bullish_cross", "bullish", "bearish", "bearish_cross"},
            "trend_structure": {"bullish_aligned", "mixed", "bearish_aligned"},
            "bollinger_position": {"upper_half", "lower_half", "middle"},
        }
        all_ok = True
        for k, valid in cats.items():
            v = signals.get(k)
            if v not in valid:
                _fail(f"signals['{k}'] = {v!r} — not in {valid}")
                all_ok = False
        if all_ok:
            _pass("All categorical signals have valid values")

        score = result["composite_technical_score"]
        if 0.0 <= score <= 1.0:
            _pass(f"composite_technical_score in [0,1]", f"score={score}")
        else:
            _fail("composite_technical_score out of range", f"score={score}")

        return result, df  # pass forward to Check 3

    except Exception as exc:
        _fail("Check 2 raised an exception", traceback.format_exc().strip().splitlines()[-1])
        return None, None


# ===========================================================================
# Check 3: Technical Analysis → Strategy pipeline
# ===========================================================================

async def check3_ta_strategy(ta_result: dict | None, df) -> None:
    _section("Check 3: Technical Analysis → Strategy pipeline")

    if ta_result is None:
        _fail("Check 3 skipped", "no TA result from Check 2")
        return

    try:
        from ozymandias.strategies.momentum_strategy import MomentumStrategy
        from ozymandias.strategies.swing_strategy import SwingStrategy

        signals = ta_result["signals"]   # nested sub-dict
        symbol = ta_result["symbol"]

        # --- Momentum ---
        mom = MomentumStrategy()
        mom_signals = await mom.generate_signals(symbol, df, signals)
        if isinstance(mom_signals, list):
            _pass(
                f"MomentumStrategy.generate_signals() returned {len(mom_signals)} signal(s)",
                (
                    f"strength={mom_signals[0].strength:.3f}, "
                    f"entry={mom_signals[0].entry_price:.2f}, "
                    f"stop={mom_signals[0].stop_price:.2f}, "
                    f"target={mom_signals[0].target_price:.2f}"
                ) if mom_signals else "no signal (conditions not met — OK)",
            )
        else:
            _fail("MomentumStrategy.generate_signals()", f"expected list, got {type(mom_signals)}")

        # --- Swing ---
        swing = SwingStrategy()
        swing_signals = await swing.generate_signals(symbol, df, signals)
        if isinstance(swing_signals, list):
            _pass(
                f"SwingStrategy.generate_signals() returned {len(swing_signals)} signal(s)",
                (
                    f"strength={swing_signals[0].strength:.3f}, "
                    f"entry={swing_signals[0].entry_price:.2f}, "
                    f"stop={swing_signals[0].stop_price:.2f}"
                ) if swing_signals else "no signal (conditions not met — OK)",
            )
        else:
            _fail("SwingStrategy.generate_signals()", f"expected list, got {type(swing_signals)}")

        # Signal fields are valid types
        for strategy_name, sigs in [("momentum", mom_signals), ("swing", swing_signals)]:
            for sig in sigs:
                checks = [
                    isinstance(sig.symbol, str),
                    isinstance(sig.strength, float) and 0.0 <= sig.strength <= 1.0,
                    isinstance(sig.entry_price, float) and sig.entry_price > 0,
                    sig.stop_price < sig.entry_price,
                    sig.target_price > sig.entry_price,
                    sig.direction == "long",
                ]
                if all(checks):
                    _pass(f"{strategy_name} signal fields valid")
                else:
                    _fail(f"{strategy_name} signal field validation", f"checks={checks}")

    except Exception as exc:
        _fail("Check 3 raised an exception", traceback.format_exc().strip().splitlines()[-1])


# ===========================================================================
# Check 4: TA → Opportunity Ranker pipeline
# ===========================================================================

async def check4_ta_opportunity_ranker(ta_result: dict | None) -> None:
    _section("Check 4: Technical Analysis → Opportunity Ranker pipeline")

    if ta_result is None:
        _fail("Check 4 skipped", "no TA result from Check 2")
        return

    try:
        from ozymandias.core.config import RiskConfig
        from ozymandias.core.state_manager import PortfolioState
        from ozymandias.execution.broker_interface import AccountInfo
        from ozymandias.execution.pdt_guard import PDTGuard
        from ozymandias.intelligence.claude_reasoning import ReasoningResult
        from ozymandias.intelligence.opportunity_ranker import OpportunityRanker

        price = ta_result["signals"].get("price", 175.0)

        # Simulate Claude output: 3 opportunities using real current prices
        fake_opportunities = [
            {
                "symbol": "AAPL",
                "action": "buy",
                "strategy": "momentum",
                "timeframe": "short",
                "conviction": 0.78,
                "suggested_entry": round(price, 2),
                "suggested_exit": round(price * 1.08, 2),
                "suggested_stop": round(price * 0.95, 2),
                "position_size_pct": 0.08,
                "reasoning": "Strong momentum with VWAP breakout",
            },
            {
                "symbol": "MSFT",
                "action": "buy",
                "strategy": "swing",
                "timeframe": "medium",
                "conviction": 0.65,
                "suggested_entry": 420.0,
                "suggested_exit": 445.0,
                "suggested_stop": 408.0,
                "position_size_pct": 0.07,
                "reasoning": "Oversold dip in long-term uptrend",
            },
            {
                "symbol": "NVDA",
                "action": "buy",
                "strategy": "momentum",
                "timeframe": "short",
                "conviction": 0.55,
                "suggested_entry": 850.0,
                "suggested_exit": 900.0,
                "suggested_stop": 820.0,
                "position_size_pct": 0.06,
                "reasoning": "Breakout continuation setup",
            },
        ]

        reasoning_result = ReasoningResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            position_reviews=[],
            new_opportunities=fake_opportunities,
            watchlist_changes={"add": [], "remove": [], "rationale": ""},
            market_assessment="neutral",
            risk_flags=[],
            raw={},
        )

        # Build technical_signals map: AAPL uses real signals, others use plausible stubs
        # Format: {symbol: full generate_signal_summary() output dict}
        aapl_full = ta_result  # full output from Check 2
        stub_summary = lambda sym, score: {
            "symbol": sym,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signals": {
                "vwap_position": "above",
                "rsi": 52.0,
                "rsi_divergence": False,
                "macd_signal": "bullish",
                "trend_structure": "bullish_aligned",
                "roc_5": 1.2,
                "roc_deceleration": False,
                "volume_ratio": 1.3,
                "atr_14": 5.0,
                "bollinger_position": "upper_half",
                "price": 420.0,
                "avg_daily_volume": 25_000_000.0,
            },
            "composite_technical_score": score,
        }
        technical_signals = {
            "AAPL": aapl_full,
            "MSFT": stub_summary("MSFT", 0.62),
            "NVDA": stub_summary("NVDA", 0.58),
        }

        account = AccountInfo(
            equity=100_000.0,
            buying_power=80_000.0,
            cash=50_000.0,
            currency="USD",
            pdt_flag=False,
            daytrade_count=1,
            account_id="integ-test",
        )
        portfolio = PortfolioState(positions=[], buying_power=80_000.0)
        pdt_guard = PDTGuard(RiskConfig())

        ranker = OpportunityRanker()

        # Use a lambda that returns True for market hours (avoids depending on current time)
        market_open = lambda: True

        ranked = ranker.rank_opportunities(
            reasoning_result,
            technical_signals,
            account,
            portfolio,
            pdt_guard,
            market_open,
            orders=[],
        )

        if not isinstance(ranked, list):
            _fail("rank_opportunities() type", f"expected list, got {type(ranked)}")
            return

        _pass(
            f"rank_opportunities() returned {len(ranked)} scored opportunity/ies",
            f"(from 3 candidates — some may be filtered by hard filters)",
        )

        if ranked:
            # Verify sorted descending
            scores = [s.composite_score for s in ranked]
            if scores == sorted(scores, reverse=True):
                _pass("Results sorted by composite_score descending", f"scores={[round(s,3) for s in scores]}")
            else:
                _fail("Results not sorted", f"scores={scores}")

            # Verify score range and type validity
            all_ok = True
            for opp in ranked:
                if not (0.0 <= opp.composite_score <= 1.0):
                    _fail(f"{opp.symbol} composite_score out of range", f"{opp.composite_score}")
                    all_ok = False
                if not (0.0 <= opp.ai_conviction <= 1.0):
                    _fail(f"{opp.symbol} ai_conviction out of range", f"{opp.ai_conviction}")
                    all_ok = False
                if not (0.0 <= opp.technical_score <= 1.0):
                    _fail(f"{opp.symbol} technical_score out of range", f"{opp.technical_score}")
                    all_ok = False
            if all_ok:
                _pass("All composite_score / ai_conviction / technical_score fields in [0,1]")

            print(f"\n  Ranked opportunities:")
            for opp in ranked:
                print(
                    f"    #{ranked.index(opp)+1} {opp.symbol:<6} "
                    f"composite={opp.composite_score:.3f}  "
                    f"ai={opp.ai_conviction:.2f}  "
                    f"tech={opp.technical_score:.2f}  "
                    f"rar={opp.risk_adjusted_return:.2f}  "
                    f"liq={opp.liquidity_score:.2f}"
                )

    except Exception as exc:
        _fail("Check 4 raised an exception", traceback.format_exc().strip().splitlines()[-1])


# ===========================================================================
# Check 5: Risk Manager overrides with real intraday data
# ===========================================================================

async def check5_risk_override_real_data() -> None:
    _section("Check 5: Risk Manager override signals with real intraday data")

    try:
        from ozymandias.core.config import RiskConfig
        from ozymandias.core.state_manager import ExitTargets, Position, TradeIntention
        from ozymandias.execution.pdt_guard import PDTGuard
        from ozymandias.execution.risk_manager import RiskManager
        from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
        from ozymandias.intelligence.technical_analysis import generate_signal_summary

        adapter = YFinanceAdapter()
        symbol = "TSLA"
        print(f"  Fetching {symbol} 5-minute bars (1 day) from yfinance …")
        df = await adapter.fetch_bars(symbol, interval="5m", period="1d")

        if df is None or df.empty:
            _fail("yfinance fetch_bars (5m)", "returned empty DataFrame")
            return

        _pass(f"Fetched {len(df)} intraday bars for {symbol}")

        result = generate_signal_summary(symbol, df)
        signals = result["signals"]

        _pass(
            "generate_signal_summary() on intraday data",
            f"price={signals.get('price'):.2f}, rsi={signals.get('rsi'):.1f}, "
            f"vol_ratio={signals.get('volume_ratio'):.2f}",
        )

        # Fake long position entered at ~5% below current price
        current_price = float(signals.get("price", 200.0))
        entry_price = current_price * 0.95

        pos = Position(
            symbol=symbol,
            shares=50,
            avg_cost=entry_price,
            entry_date="2026-03-13",
            intention=TradeIntention(
                strategy="momentum",
                exit_targets=ExitTargets(
                    profit_target=current_price * 1.10,
                    stop_loss=current_price * 0.90,
                ),
            ),
        )

        risk_config = RiskConfig()
        pdt_guard = PDTGuard(risk_config)
        rm = RiskManager(risk_config, pdt_guard)

        # Use current price as intraday high (conservative — checks ATR stop)
        intraday_high = current_price

        should_exit, triggered = rm.evaluate_overrides(pos, signals, intraday_high)

        if not isinstance(should_exit, bool):
            _fail("evaluate_overrides return type", f"expected bool, got {type(should_exit)}")
            return
        if not isinstance(triggered, list):
            _fail("evaluate_overrides triggered type", f"expected list, got {type(triggered)}")
            return

        _pass(
            f"evaluate_overrides() completed without error",
            f"should_exit={should_exit}, triggered={triggered or '[]'}",
        )

        # Verify no NaN comparisons leaked into the result
        for sig_name in triggered:
            if not isinstance(sig_name, str):
                _fail("triggered signal name type", f"expected str, got {type(sig_name)}: {sig_name!r}")
                return
        _pass("All triggered signal names are strings")

        # Print signal breakdown for inspection
        print(f"\n  Override signal inputs:")
        for k in ("price", "vwap_position", "volume_ratio", "rsi_divergence",
                  "roc_deceleration", "roc_5", "atr_14"):
            print(f"    {k:<22} = {signals.get(k)!r}")
        print(f"    {'intraday_high':<22} = {intraday_high:.2f}")
        print(f"    {'entry_price (pos)':<22} = {entry_price:.2f}")

    except Exception as exc:
        _fail("Check 5 raised an exception", traceback.format_exc().strip().splitlines()[-1])
        traceback.print_exc()


# ===========================================================================
# Check 6: Orchestrator.__init__ + _startup() wiring
# ===========================================================================

# Stubs used by check6
from ozymandias.execution.broker_interface import AccountInfo, MarketHours as _MarketHours

_stub_account = AccountInfo(
    equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
    currency="USD", pdt_flag=False, daytrade_count=0, account_id="mock-001",
)
_stub_hours = _MarketHours(
    is_open=False,
    next_open=datetime.now(timezone.utc) + timedelta(hours=1),
    next_close=datetime.now(timezone.utc) + timedelta(hours=8),
    session="closed",
)


async def check6_orchestrator_init() -> None:
    _section("Check 6: Orchestrator.__init__ + _startup() wiring")

    try:
        from ozymandias.core.orchestrator import Orchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch(
                    "ozymandias.execution.alpaca_broker.AlpacaBroker.__init__",
                    MagicMock(return_value=None),
                ),
                patch(
                    "ozymandias.execution.alpaca_broker.AlpacaBroker.get_account",
                    AsyncMock(return_value=_stub_account),
                ),
                patch(
                    "ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours",
                    AsyncMock(return_value=_stub_hours),
                ),
                patch("anthropic.AsyncAnthropic", MagicMock),
                patch(
                    "ozymandias.core.orchestrator.Orchestrator._load_credentials",
                    MagicMock(return_value=("mock-key", "mock-secret")),
                ),
            ):
                orch = Orchestrator()
                # Override state dir to the temp directory so no disk pollution
                orch._state_manager._dir = Path(tmpdir)

                try:
                    await orch._startup()
                except Exception:
                    _fail(
                        "_startup() raised an exception",
                        traceback.format_exc().strip().splitlines()[-1],
                    )
                    traceback.print_exc()
                    return

        _pass("_startup() completed without exception")

        # Print all attributes
        print("\n  Orchestrator attributes after _startup():")
        for attr, val in sorted(orch.__dict__.items()):
            print(f"    {attr:<35} {type(val).__name__:<25} {repr(val)[:80]}")
        print()

        # Identity cross-reference assertions
        checks = [
            (
                "orch._fill_protection._sm is orch._state_manager",
                orch._fill_protection is not None
                and orch._fill_protection._sm is orch._state_manager,
            ),
            (
                "orch._risk_manager._pdt is orch._pdt_guard",
                orch._risk_manager is not None
                and orch._risk_manager._pdt is orch._pdt_guard,
            ),
            (
                "orch._claude._cache is orch._reasoning_cache",
                orch._claude is not None
                and orch._claude._cache is orch._reasoning_cache,
            ),
            (
                "orch._claude._cfg is orch._config",
                orch._claude is not None
                and orch._claude._cfg is orch._config,
            ),
        ]

        all_ok = True
        for desc, result in checks:
            if result:
                _pass(f"identity: {desc}")
            else:
                _fail(f"identity: {desc}", "cross-reference broken — modules not wired correctly")
                all_ok = False

        if all_ok:
            _pass("All identity cross-references verified")

    except Exception:
        _fail("Check 6 raised an exception", traceback.format_exc().strip().splitlines()[-1])
        traceback.print_exc()


# ===========================================================================
# Main runner
# ===========================================================================

async def _run_all() -> None:
    print(_bold("\n══ Ozymandias v3 — Integration Check ══"))
    print(_dim("  Instantiates real modules; requires internet access for Checks 2-5.\n  Check 6 uses mocks only.\n"))

    # Check 1 — no network
    await check1_state_fill_protection()

    # Checks 2-3: fetch once, reuse
    ta_result, df = None, None
    try:
        ret = await check2_yfinance_ta()
        if ret is not None:
            ta_result, df = ret
    except Exception:
        pass

    await check3_ta_strategy(ta_result, df)
    await check4_ta_opportunity_ranker(ta_result)
    await check5_risk_override_real_data()
    await check6_orchestrator_init()

    # Summary
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total = len(_results)

    print(f"\n{'─'*50}")
    status = _green(f"ALL {total} CHECKS PASSED") if failed == 0 else _red(f"{failed}/{total} CHECKS FAILED")
    print(f"  {status}  ({passed} passed, {failed} failed)")
    print(f"{'─'*50}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_run_all())
