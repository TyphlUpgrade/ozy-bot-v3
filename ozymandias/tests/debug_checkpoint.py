"""
Debug Checkpoint: State Stress + Signal Sanity
Manual script — not pytest. Run with: PYTHONPATH=. python ozymandias/tests/debug_checkpoint.py
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ── colour helpers ───────────────────────────────────────────────────────────
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"

def ok(msg: str, detail: str = "") -> None:
    print(f"  [{PASS}] {msg}" + (f"  {detail}" if detail else ""))

def fail(msg: str, detail: str = "") -> None:
    print(f"  [{FAIL}] {msg}" + (f"  {detail}" if detail else ""))

def warn(msg: str) -> None:
    print(f"  [{WARN}] {msg}")

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

# ── imports ──────────────────────────────────────────────────────────────────
from ozymandias.core.state_manager import (
    StateManager, OrderRecord, OrdersState, PortfolioState, Position,
    TradeIntention, ExitTargets,
)
from ozymandias.execution.fill_protection import FillProtectionManager
from ozymandias.execution.broker_interface import OrderStatus, CancelResult
from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
from ozymandias.intelligence.technical_analysis import generate_signal_summary
from ozymandias.strategies.momentum_strategy import MomentumStrategy
from ozymandias.strategies.swing_strategy import SwingStrategy


# ═══════════════════════════════════════════════════════════════════════════
# PART A — STATE STRESS
# ═══════════════════════════════════════════════════════════════════════════

def _make_order(order_id: str, symbol: str = "AAPL",
                side: str = "buy", qty: float = 100.0) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type="limit",
        limit_price=150.0,
        status="PENDING",
        created_at=_utcnow(),
    )


def _broker_status(order_id: str, status: str,
                   filled: float = 0.0, remaining: float = 100.0,
                   avg_price: float | None = None) -> OrderStatus:
    return OrderStatus(
        order_id=order_id,
        status=status,
        filled_qty=filled,
        remaining_qty=remaining,
        filled_avg_price=avg_price,
        submitted_at=None,
        filled_at=None,
        canceled_at=None,
    )


async def check_a1_concurrent_writes() -> bool:
    """10 simultaneous add_order() calls for the same symbol, different IDs."""
    print("\n  A1 — Concurrent writes (10 tasks, same symbol)")
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        orders = [_make_order(f"conc-{i:02d}") for i in range(10)]

        # Fire all record_order() calls at once
        await asyncio.gather(*[fpm.record_order(o) for o in orders])

        # Read raw JSON from disk
        raw = Path(tmp, "orders.json").read_text()
        try:
            data = json.loads(raw)
            valid_json = True
        except json.JSONDecodeError as e:
            valid_json = False
            fail("orders.json is not valid JSON", str(e))
            return False

        if valid_json:
            ok("orders.json is valid JSON")

        count = len(data.get("orders", []))
        if count == 10:
            ok(f"Correct order count on disk", f"count={count}")
        else:
            fail(f"Expected 10 orders on disk", f"got {count}")
            return False

        blocked = not fpm.can_place_order("AAPL")
        if blocked:
            ok("can_place_order('AAPL') → False (blocked by PENDING orders)")
        else:
            fail("can_place_order('AAPL') should return False")
            return False

    return True


async def check_a2_lifecycle() -> bool:
    """Full buy→fill→sell→fill→remove lifecycle; reload from disk after each step."""
    print("\n  A2 — Full trade lifecycle (reload from disk at each step)")
    errors = []

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        # Step 1: add buy order
        buy = _make_order("buy-001", "TSLA", "buy", 50.0)
        await fpm.record_order(buy)
        await fpm.load()  # reload
        orders_on_disk = (await sm.load_orders()).orders
        if not any(o.order_id == "buy-001" for o in orders_on_disk):
            errors.append("Step 1: buy-001 missing after reload")

        # Step 2: mark buy FILLED via reconcile
        await fpm.reconcile([_broker_status("buy-001", "filled", filled=50.0, remaining=0.0, avg_price=200.0)])
        await fpm.load()
        orders_on_disk = (await sm.load_orders()).orders
        buy_rec = next((o for o in orders_on_disk if o.order_id == "buy-001"), None)
        if buy_rec is None or buy_rec.status != "FILLED":
            errors.append(f"Step 2: buy-001 status wrong: {getattr(buy_rec, 'status', 'missing')}")

        # Step 3: add position
        portfolio = await sm.load_portfolio()
        portfolio.positions.append(Position(
            symbol="TSLA", shares=50.0, avg_cost=200.0,
            entry_date="2026-03-13", position_id="pos-001",
        ))
        await sm.save_portfolio(portfolio)
        portfolio2 = await sm.load_portfolio()
        if not any(p.symbol == "TSLA" for p in portfolio2.positions):
            errors.append("Step 3: TSLA position missing after reload")

        # Step 4: add sell order
        sell = _make_order("sell-001", "TSLA", "sell", 50.0)
        await fpm.record_order(sell)
        blocked = not fpm.can_place_order("TSLA")
        if not blocked:
            errors.append("Step 4: can_place_order should be False with pending sell")

        # Step 5: mark sell FILLED
        await fpm.reconcile([_broker_status("sell-001", "filled", filled=50.0, remaining=0.0, avg_price=210.0)])
        can_now = fpm.can_place_order("TSLA")
        if not can_now:
            errors.append("Step 5: can_place_order should be True after sell filled")

        # Step 6: remove position
        portfolio3 = await sm.load_portfolio()
        portfolio3.positions = [p for p in portfolio3.positions if p.symbol != "TSLA"]
        await sm.save_portfolio(portfolio3)
        portfolio4 = await sm.load_portfolio()
        if any(p.symbol == "TSLA" for p in portfolio4.positions):
            errors.append("Step 6: TSLA position still present after removal")

    if errors:
        for e in errors:
            fail(e)
        return False
    else:
        ok("All 6 lifecycle steps consistent")
        return True


async def check_a3_partial_fill() -> bool:
    """Partial fill → cancel remaining; verify state at each stage."""
    print("\n  A3 — Partial fill + cancel")
    errors = []

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        # Add order for 100 shares
        order = _make_order("pf-001", "MSFT", "buy", 100.0)
        await fpm.record_order(order)

        # Partially fill: 60 of 100
        await fpm.reconcile([
            _broker_status("pf-001", "partially_filled", filled=60.0, remaining=40.0)
        ])
        rec = fpm._orders.get("pf-001")
        if rec is None:
            errors.append("pf-001 missing from in-memory orders")
        elif rec.status != "PARTIALLY_FILLED":
            errors.append(f"Expected PARTIALLY_FILLED, got {rec.status}")
        elif rec.filled_quantity != 60.0:
            errors.append(f"Expected filled_quantity=60, got {rec.filled_quantity}")
        else:
            ok("Order record matches after partial fill", "status=PARTIALLY_FILLED, filled=60")

        # Add position with 60 shares
        portfolio = await sm.load_portfolio()
        portfolio.positions.append(Position(
            symbol="MSFT", shares=60.0, avg_cost=300.0,
            entry_date="2026-03-13",
        ))
        await sm.save_portfolio(portfolio)

        # Cancel remaining
        cancel_res = CancelResult(order_id="pf-001", success=True, final_status="canceled")
        await fpm.handle_cancel_result("pf-001", cancel_res)

        # Position still 60 shares?
        portfolio2 = await sm.load_portfolio()
        msft_pos = next((p for p in portfolio2.positions if p.symbol == "MSFT"), None)
        if msft_pos is None:
            errors.append("MSFT position gone after cancel — should still be 60 shares")
        elif msft_pos.shares != 60.0:
            errors.append(f"MSFT position shares wrong: expected 60, got {msft_pos.shares}")
        else:
            ok("Position still 60 shares after cancel", f"shares={msft_pos.shares}")

        # can_place_order True again?
        can = fpm.can_place_order("MSFT")
        if can:
            ok("can_place_order('MSFT') → True after cancel")
        else:
            errors.append("can_place_order('MSFT') still False after cancel")

    if errors:
        for e in errors:
            fail(e)
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# PART B — SIGNAL SANITY
# ═══════════════════════════════════════════════════════════════════════════

_CATEGORICAL_VALID = {
    "vwap_position":   {"above", "below"},
    "macd_signal":     {"bullish", "bearish", "neutral"},
    "trend_structure": {"bullish_aligned", "bearish_aligned", "mixed"},
    "bollinger_position": {"upper_half", "lower_half"},
    "rsi_divergence":  {False, "bearish", "bullish"},
    "roc_deceleration": {True, False},
}

_NUMERIC_FIELDS = ["price", "rsi", "roc_5", "atr_14", "volume_ratio",
                   "avg_daily_volume", "composite_technical_score"]


def _check_signals(symbol: str, result: dict) -> tuple[list[str], list[str]]:
    """Return (warnings, errors) for signal sanity."""
    warnings = []
    errors = []
    signals = result.get("signals", {})
    all_fields = {**signals, "composite_technical_score": result.get("composite_technical_score")}

    for field in _NUMERIC_FIELDS:
        val = signals.get(field) if field != "composite_technical_score" else result.get(field)
        if val is None:
            warnings.append(f"{symbol}.{field} is None")
        elif isinstance(val, float) and math.isnan(val):
            warnings.append(f"{symbol}.{field} is NaN")

    for field, valid in _CATEGORICAL_VALID.items():
        val = signals.get(field)
        if val not in valid:
            warnings.append(f"{symbol}.{field}={val!r} not in {valid}")

    return warnings, errors


async def check_b_signal_sanity() -> tuple[bool, list[str]]:
    """Fetch real bars, run TA + strategies, print table."""
    symbols = ["AAPL", "TSLA", "NVDA"]
    adapter = YFinanceAdapter()
    momentum_strat = MomentumStrategy()
    swing_strat = SwingStrategy()

    rows = []
    all_warnings = []

    for sym in symbols:
        print(f"  Fetching {sym} …", end="", flush=True)
        try:
            df = await adapter.fetch_bars(sym, "1d", "1mo")
        except Exception as e:
            print()
            fail(f"fetch_bars({sym}) raised", str(e))
            continue

        if df is None or df.empty:
            print()
            fail(f"fetch_bars({sym}) returned empty DataFrame")
            continue

        print(f" {len(df)} bars", end="", flush=True)

        try:
            result = generate_signal_summary(sym, df)
        except Exception as e:
            print()
            fail(f"generate_signal_summary({sym}) raised", str(e))
            continue

        print(" ✓")

        warnings, errors = _check_signals(sym, result)
        all_warnings.extend(warnings)

        signals = result.get("signals", {})

        # Strategy signals — pass nested signals sub-dict
        try:
            mom_sigs = await momentum_strat.generate_signals(sym, df, signals)
        except Exception as e:
            mom_sigs = []
            all_warnings.append(f"{sym}: MomentumStrategy.generate_signals raised: {e}")

        try:
            sw_sigs = await swing_strat.generate_signals(sym, df, signals)
        except Exception as e:
            sw_sigs = []
            all_warnings.append(f"{sym}: SwingStrategy.generate_signals raised: {e}")

        rows.append({
            "symbol": sym,
            "price": signals.get("price", "?"),
            "rsi": signals.get("rsi", "?"),
            "macd": signals.get("macd_signal", "?"),
            "trend": signals.get("trend_structure", "?"),
            "vol_ratio": signals.get("volume_ratio", "?"),
            "score": result.get("composite_technical_score", "?"),
            "momentum": "YES" if mom_sigs else "no",
            "swing": "YES" if sw_sigs else "no",
        })

    # Print table
    print()
    header = f"  {'SYMBOL':<6} | {'Price':>7} | {'RSI':>5} | {'MACD':<8} | {'Trend':<18} | {'VolRatio':>8} | {'Score':>5} | Mom? | Swing?"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        price = f"{r['price']:.2f}" if isinstance(r['price'], float) else r['price']
        rsi   = f"{r['rsi']:.1f}"   if isinstance(r['rsi'],   float) else r['rsi']
        vr    = f"{r['vol_ratio']:.3f}" if isinstance(r['vol_ratio'], float) else r['vol_ratio']
        score = f"{r['score']:.2f}" if isinstance(r['score'], float) else r['score']
        trend_short = str(r['trend']).replace("_aligned", "").replace("bullish", "bull").replace("bearish", "bear")
        print(f"  {r['symbol']:<6} | {price:>7} | {rsi:>5} | {str(r['macd']):<8} | {trend_short:<18} | {vr:>8} | {score:>5} | {r['momentum']:<4} | {r['swing']}")

    return len(rows) == len(symbols), all_warnings


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    print("\n=== STATE STRESS ===")
    a1 = await check_a1_concurrent_writes()
    a2 = await check_a2_lifecycle()
    a3 = await check_a3_partial_fill()

    state_pass = a1 and a2 and a3
    print(f"\n  Concurrent writes: {'PASS' if a1 else 'FAIL'}")
    print(f"  Lifecycle:         {'PASS' if a2 else 'FAIL'}")
    print(f"  Partial fill:      {'PASS' if a3 else 'FAIL'}")

    print("\n=== SIGNAL SANITY ===")
    sig_pass, warnings = await check_b_signal_sanity()

    if warnings:
        print(f"\n  WARNINGS:")
        for w in warnings:
            warn(w)
    else:
        print(f"\n  WARNINGS: none")

    print()
    if state_pass and sig_pass and not warnings:
        print("  All checks PASSED with no warnings.")
    elif state_pass and sig_pass:
        print("  All functional checks PASSED — see warnings above.")
    else:
        print("  One or more checks FAILED — see details above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
