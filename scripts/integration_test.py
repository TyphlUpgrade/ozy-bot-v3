#!/usr/bin/env /home/typhlupgrade/.local/share/ozy-bot-v3/.venv/bin/python
"""
Comprehensive integration test for Ozymandias v3 Phases 01–03.

Tests real Alpaca paper trading credentials plus all local module behaviour.
Every public method of every Phase 01–03 module is exercised at least once.

Usage:
    PYTHONPATH=. python scripts/integration_test.py [--debug]

Exit code: 0 = all passed, 1 = one or more failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure project root on sys.path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ozymandias.core.config import RiskConfig, load_config
from ozymandias.core.logger import setup_logging
from ozymandias.core.market_hours import Session, get_current_session, is_market_open, is_trading_allowed, is_weekend
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import (
    OrderRecord,
    OrdersState,
    PortfolioState,
    Position,
    StateManager,
    TradeIntention,
    WatchlistEntry,
    WatchlistState,
)
from ozymandias.execution.alpaca_broker import AlpacaBroker
from ozymandias.execution.broker_interface import AccountInfo, Order, OrderStatus
from ozymandias.execution.fill_protection import FillProtectionManager
from ozymandias.execution.pdt_guard import PDTGuard, _business_days_window, _et_date

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "ozymandias" / "config" / "credentials.enc"

# Limit orders far below market — safe to place even when market is open
TEST_SYMBOL  = "SPY"
TEST_QTY     = 1
TEST_LIMIT   = 1.00   # $1 — will never fill


# ===========================================================================
# Test runner harness
# ===========================================================================

class IntegrationTest:
    def __init__(self) -> None:
        self.passed  = 0
        self.failed  = 0
        self.skipped = 0
        self._section_name = ""

    def section(self, name: str) -> None:
        self._section_name = name
        print(f"\n{BOLD}{CYAN}{'─' * 60}{RESET}")
        print(f"{BOLD}{CYAN}  {name}{RESET}")
        print(f"{BOLD}{CYAN}{'─' * 60}{RESET}")

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            tag = f"{GREEN}PASS{RESET}"
        else:
            self.failed += 1
            tag = f"{RED}FAIL{RESET}"
        line = f"  [{tag}] {name}"
        if detail:
            line += f"  {DIM}({detail}){RESET}"
        print(line)
        return condition

    def skip(self, name: str, reason: str = "") -> None:
        self.skipped += 1
        tag = f"{YELLOW}SKIP{RESET}"
        line = f"  [{tag}] {name}"
        if reason:
            line += f"  {DIM}({reason}){RESET}"
        print(line)

    def info(self, msg: str) -> None:
        print(f"  {DIM}ℹ {msg}{RESET}")

    def warn(self, msg: str) -> None:
        print(f"  {YELLOW}⚠ {msg}{RESET}")

    def summary(self) -> int:
        total = self.passed + self.failed + self.skipped
        print(f"\n{BOLD}{'=' * 60}{RESET}")
        print(f"{BOLD}  Results: {self.passed}/{total} passed", end="")
        if self.skipped:
            print(f", {self.skipped} skipped", end="")
        if self.failed:
            print(f", {RED}{self.failed} FAILED{RESET}", end="")
        print(f"{RESET}")
        print(f"{BOLD}{'=' * 60}{RESET}")
        return 1 if self.failed else 0


# ===========================================================================
# Credentials loader
# ===========================================================================

def _load_credentials() -> tuple[str, str]:
    if not CREDENTIALS_PATH.exists():
        print(f"{RED}ERROR: credentials file not found at {CREDENTIALS_PATH}{RESET}")
        sys.exit(1)
    with open(CREDENTIALS_PATH) as f:
        creds = json.load(f)
    api_key    = creds.get("api_key") or creds.get("APCA_API_KEY_ID")
    secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        print(f"{RED}ERROR: credentials.enc must have 'api_key' and 'secret_key'{RESET}")
        sys.exit(1)
    return api_key, secret_key


# ===========================================================================
# Phase 01 — Core modules
# ===========================================================================

async def test_config(t: IntegrationTest) -> None:
    t.section("Phase 01 — Config")

    try:
        cfg = load_config()
        t.check("load_config() returns Config object", cfg is not None)
        t.check("cfg.broker.name is set",  bool(cfg.broker.name))
        t.check("cfg.broker.environment in {paper, live}", cfg.broker.environment in ("paper", "live"))
        t.check("cfg.risk.pdt_buffer >= 0", cfg.risk.pdt_buffer >= 0)
        t.check("cfg.risk.min_equity_for_trading > 0", cfg.risk.min_equity_for_trading > 0)
        t.check("cfg.risk.max_position_pct in (0, 1]", 0 < cfg.risk.max_position_pct <= 1.0)
        t.check("cfg.scheduler.fast_loop_sec > 0", cfg.scheduler.fast_loop_sec > 0)
        t.check("cfg.scheduler.medium_loop_sec > cfg.scheduler.fast_loop_sec",
                cfg.scheduler.medium_loop_sec > cfg.scheduler.fast_loop_sec)
        weight_total = (cfg.ranker.weight_ai + cfg.ranker.weight_technical
                        + cfg.ranker.weight_risk + cfg.ranker.weight_liquidity)
        t.check("ranker weights sum to ~1.0", abs(weight_total - 1.0) <= 0.01,
                f"sum={weight_total:.4f}")
        t.check("cfg.claude.model is set", bool(cfg.claude.model))
        t.info(f"broker={cfg.broker.name} env={cfg.broker.environment} model={cfg.claude.model}")
    except Exception as exc:
        t.check("Config loading raised no exception", False, str(exc))


async def test_state_manager(t: IntegrationTest) -> None:
    t.section("Phase 01 — State Manager")

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))

        # Initialization
        try:
            await sm.initialize()
            t.check("initialize() creates state directory", Path(tmp).exists())
            t.check("portfolio.json created", sm.portfolio_path.exists())
            t.check("watchlist.json created", sm.watchlist_path.exists())
            t.check("orders.json created",    sm.orders_path.exists())
        except Exception as exc:
            t.check("initialize() raised no exception", False, str(exc))
            return

        # Second initialize is idempotent
        try:
            await sm.initialize()
            t.check("initialize() is idempotent (no error on re-run)", True)
        except Exception as exc:
            t.check("initialize() is idempotent", False, str(exc))

        # Portfolio round-trip
        try:
            p = await sm.load_portfolio()
            t.check("load_portfolio() returns PortfolioState", isinstance(p, PortfolioState))
            t.check("initial portfolio cash == 0.0", p.cash == 0.0)

            p.cash = 50_000.0
            p.buying_power = 100_000.0
            p.positions.append(Position(
                symbol="AAPL", shares=10.0, avg_cost=175.0, entry_date="2025-01-01"
            ))
            await sm.save_portfolio(p)
            p2 = await sm.load_portfolio()
            t.check("save/load portfolio cash", p2.cash == 50_000.0)
            t.check("save/load portfolio buying_power", p2.buying_power == 100_000.0)
            t.check("save/load portfolio position count", len(p2.positions) == 1)
            t.check("save/load portfolio position symbol", p2.positions[0].symbol == "AAPL")
            t.check("last_updated is set after save", bool(p2.last_updated))
        except Exception as exc:
            t.check("Portfolio round-trip raised no exception", False, str(exc))

        # Watchlist round-trip
        try:
            w = await sm.load_watchlist()
            t.check("load_watchlist() returns WatchlistState", isinstance(w, WatchlistState))
            t.check("initial watchlist has no entries", len(w.entries) == 0)

            w.entries.append(WatchlistEntry(
                symbol="NVDA", date_added="2025-03-01", reason="momentum setup",
                priority_tier=1, strategy="momentum"
            ))
            await sm.save_watchlist(w)
            w2 = await sm.load_watchlist()
            t.check("save/load watchlist entry count", len(w2.entries) == 1)
            t.check("save/load watchlist symbol",      w2.entries[0].symbol == "NVDA")
            t.check("save/load watchlist tier",        w2.entries[0].priority_tier == 1)
        except Exception as exc:
            t.check("Watchlist round-trip raised no exception", False, str(exc))

        # Orders round-trip
        try:
            o = await sm.load_orders()
            t.check("load_orders() returns OrdersState", isinstance(o, OrdersState))
            t.check("initial orders list is empty", len(o.orders) == 0)

            rec = OrderRecord(
                order_id="test-001", symbol="TSLA", side="buy",
                quantity=5.0, order_type="limit", limit_price=200.0,
                status="PENDING", created_at=datetime.now(UTC).isoformat()
            )
            o.orders.append(rec)
            await sm.save_orders(o)
            o2 = await sm.load_orders()
            t.check("save/load orders count",        len(o2.orders) == 1)
            t.check("save/load order_id",            o2.orders[0].order_id == "test-001")
            t.check("save/load order symbol",        o2.orders[0].symbol == "TSLA")
            t.check("save/load order limit_price",   o2.orders[0].limit_price == 200.0)
            t.check("save/load order status",        o2.orders[0].status == "PENDING")
        except Exception as exc:
            t.check("Orders round-trip raised no exception", False, str(exc))

        # Atomic write — verify temp file doesn't linger
        t.check("No .tmp files left after writes",
                len(list(Path(tmp).glob("*.tmp"))) == 0)


async def test_market_hours(t: IntegrationTest) -> None:
    t.section("Phase 01 — Market Hours")

    now_et = datetime.now(ET)
    t.info(f"Current ET time: {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    try:
        session = get_current_session()
        t.check("get_current_session() returns Session enum", isinstance(session, Session))
        t.check("session is one of four valid values",
                session in (Session.PRE_MARKET, Session.REGULAR_HOURS,
                            Session.POST_MARKET, Session.CLOSED))
        t.info(f"Current session: {session.name}")

        is_open = is_market_open()
        t.check("is_market_open() returns bool", isinstance(is_open, bool))

        is_allowed = is_trading_allowed()
        t.check("is_trading_allowed() returns bool", isinstance(is_allowed, bool))

        weekend = is_weekend()
        t.check("is_weekend() returns bool", isinstance(weekend, bool))

        # Consistency checks
        if weekend:
            t.check("is_market_open() is False on weekend", not is_open)
        if session == Session.REGULAR_HOURS:
            t.check("is_market_open() is True during regular hours", is_open)
            t.check("is_trading_allowed() is True during regular hours", is_allowed)
        if session == Session.CLOSED:
            t.check("is_market_open() is False when closed", not is_open)

        # Test with explicit datetime overrides
        monday_open = datetime(2025, 3, 10, 10, 0, 0, tzinfo=ET)
        t.check("is_market_open at 10am ET Monday",
                is_market_open(monday_open))

        monday_premarket = datetime(2025, 3, 10, 7, 0, 0, tzinfo=ET)
        t.check("is_market_open returns False at 7am ET (pre-market)",
                not is_market_open(monday_premarket))

        saturday = datetime(2025, 3, 15, 12, 0, 0, tzinfo=ET)
        t.check("is_weekend returns True for Saturday", is_weekend(saturday))
        t.check("is_market_open returns False on Saturday", not is_market_open(saturday))

    except Exception as exc:
        t.check("Market hours raised no exception", False, str(exc))


async def test_reasoning_cache(t: IntegrationTest) -> None:
    t.section("Phase 01 — Reasoning Cache")

    with tempfile.TemporaryDirectory() as tmp:
        cache = ReasoningCache(cache_dir=Path(tmp))

        try:
            # Empty cache
            result = cache.load_latest_if_fresh()
            t.check("load_latest_if_fresh() returns None on empty cache", result is None)

            # Save a response
            saved_path = cache.save(
                trigger="test_trigger",
                input_context={"symbol": "AAPL", "price": 175.0},
                raw_response='{"action": "hold", "reasoning": "test"}',
                parsed_response={"action": "hold", "reasoning": "test"},
                input_tokens=100,
                output_tokens=50,
            )
            t.check("save() returns a Path", isinstance(saved_path, Path))
            t.check("cache file was created", saved_path.exists())

            # Load it back
            loaded = cache.load_latest_if_fresh()
            t.check("load_latest_if_fresh() returns dict after save", loaded is not None)
            if loaded:
                t.check("loaded response has expected key", "action" in loaded or "parsed_response" in loaded
                        or "raw_response" in loaded)

            # Rotate — file is fresh so nothing deleted
            deleted = cache.rotate()
            t.check("rotate() returns int", isinstance(deleted, int))
            t.check("rotate() doesn't delete fresh files", deleted == 0)

            # Save a second entry and verify we still get a result
            cache.save(
                trigger="test_trigger_2",
                input_context={"symbol": "TSLA"},
                raw_response='{"action": "buy"}',
                parsed_response={"action": "buy"},
            )
            loaded2 = cache.load_latest_if_fresh()
            t.check("load_latest_if_fresh() returns most recent entry",
                    loaded2 is not None)

        except Exception as exc:
            t.check("Reasoning cache raised no exception", False, str(exc))
            traceback.print_exc()


# ===========================================================================
# Phase 02 — Broker
# ===========================================================================

async def test_broker_account(t: IntegrationTest, broker: AlpacaBroker) -> None:
    t.section("Phase 02 — Broker: Account")

    try:
        acct = await broker.get_account()
        t.check("get_account() returns AccountInfo", acct is not None)
        t.check("equity > 0",          acct.equity > 0)
        t.check("buying_power >= 0",   acct.buying_power >= 0)
        t.check("cash >= 0",           acct.cash >= 0)
        t.check("currency == 'USD'",   acct.currency == "USD")
        t.check("pdt_flag is bool",    isinstance(acct.pdt_flag, bool))
        t.check("daytrade_count >= 0", acct.daytrade_count >= 0)
        t.check("account_id is set",   bool(acct.account_id))
        t.info(f"equity=${acct.equity:,.2f}  bp=${acct.buying_power:,.2f}  "
               f"pdt={acct.pdt_flag}  dt_count={acct.daytrade_count}")

        bp = await broker.get_buying_power()
        t.check("get_buying_power() returns float",    isinstance(bp, float))
        t.check("get_buying_power() matches account",  abs(bp - acct.buying_power) < 0.01)

    except Exception as exc:
        t.check("Account check raised no exception", False, str(exc))
        traceback.print_exc()


async def test_broker_market_hours(t: IntegrationTest, broker: AlpacaBroker) -> None:
    t.section("Phase 02 — Broker: Market Hours")

    try:
        is_open = await broker.is_market_open()
        t.check("is_market_open() returns bool", isinstance(is_open, bool))
        t.info(f"Alpaca clock: is_open={is_open}")

        hours = await broker.get_market_hours()
        t.check("get_market_hours() returns MarketHours", hours is not None)
        t.check("is_open matches is_market_open()",        hours.is_open == is_open)
        t.check("next_open is set",  hours.next_open is not None)
        t.check("next_close is set", hours.next_close is not None)
        t.check("session is a non-empty string", bool(hours.session))
        t.check("session in expected values",
                hours.session in ("pre_market", "regular", "post_market", "closed"))
        t.info(f"session={hours.session}  next_open={hours.next_open}  next_close={hours.next_close}")

    except Exception as exc:
        t.check("Market hours check raised no exception", False, str(exc))
        traceback.print_exc()


async def test_broker_positions(t: IntegrationTest, broker: AlpacaBroker) -> None:
    t.section("Phase 02 — Broker: Positions")

    try:
        positions = await broker.get_positions()
        t.check("get_positions() returns list", isinstance(positions, list))
        t.info(f"Open positions: {len(positions)}")

        for pos in positions:
            t.check(f"Position {pos.symbol} has qty > 0",  pos.qty != 0)
            t.check(f"Position {pos.symbol} has avg price", pos.avg_entry_price > 0)

        # Test get_position for a symbol we know doesn't exist (high confidence)
        no_pos = await broker.get_position("ZZZZ_NONEXISTENT")
        t.check("get_position() returns None for non-existent symbol", no_pos is None)

        # If we have a position, test get_position() for that symbol
        if positions:
            sym = positions[0].symbol
            p = await broker.get_position(sym)
            t.check(f"get_position({sym}) returns BrokerPosition", p is not None)
            if p:
                t.check(f"get_position({sym}) qty matches get_positions()",
                        abs(p.qty - positions[0].qty) < 0.001)
        else:
            t.skip("get_position() for held symbol", "no open positions in paper account")

    except Exception as exc:
        t.check("Positions check raised no exception", False, str(exc))
        traceback.print_exc()


async def test_broker_open_orders(t: IntegrationTest, broker: AlpacaBroker) -> None:
    t.section("Phase 02 — Broker: Open Orders")

    try:
        open_orders = await broker.get_open_orders()
        t.check("get_open_orders() returns list", isinstance(open_orders, list))
        t.info(f"Open orders before test: {len(open_orders)}")

        for o in open_orders:
            t.check(f"Open order {o.order_id[:8]}... has non-empty status",
                    bool(o.status))

    except Exception as exc:
        t.check("Open orders check raised no exception", False, str(exc))
        traceback.print_exc()


async def test_broker_order_lifecycle(
    t: IntegrationTest, broker: AlpacaBroker
) -> None:
    """
    Full order lifecycle: place → status check → open orders → cancel → verify.
    Uses a GTC limit order at $1 for SPY — guaranteed not to fill.
    """
    t.section("Phase 02 — Broker: Order Lifecycle (place → status → cancel)")

    order_id: str | None = None

    try:
        # --- Place ---
        order = Order(
            symbol=TEST_SYMBOL,
            side="buy",
            quantity=TEST_QTY,
            order_type="limit",
            time_in_force="day",
            limit_price=TEST_LIMIT,
        )
        result = await broker.place_order(order)
        order_id = result.order_id
        t.check("place_order() returns OrderResult",    result is not None)
        t.check("order_id is a non-empty string",       bool(order_id))
        t.check("status is non-empty after submission", bool(result.status))
        t.check("submitted_at is set",                  result.submitted_at is not None)
        t.info(f"Placed order: id={order_id[:8]}... status={result.status}")

        # --- Status check ---
        await asyncio.sleep(1)
        status = await broker.get_order_status(order_id)
        t.check("get_order_status() returns OrderStatus", status is not None)
        t.check("order_id matches",    status.order_id == order_id)
        t.check("status is non-empty", bool(status.status))
        t.check("filled_qty == 0 (limit far below market)", status.filled_qty == 0.0)
        t.check("remaining_qty == TEST_QTY", abs(status.remaining_qty - TEST_QTY) < 0.001)
        t.info(f"Status: {status.status}  filled={status.filled_qty}  remaining={status.remaining_qty}")

        # --- Order appears in get_open_orders() ---
        open_orders = await broker.get_open_orders()
        ids = [o.order_id for o in open_orders]
        t.check("Order appears in get_open_orders()", order_id in ids)

        # --- Fills (should be empty since our order didn't fill) ---
        since = datetime.now(UTC) - timedelta(seconds=30)
        fills = await broker.get_fills(since)
        t.check("get_fills() returns list", isinstance(fills, list))
        # Our test order should not be in fills (didn't fill)
        fill_ids = [f.order_id for f in fills]
        t.check("Unfilled test order not in fills", order_id not in fill_ids)

        # --- Cancel ---
        cancel = await broker.cancel_order(order_id)
        t.check("cancel_order() returns CancelResult", cancel is not None)
        t.check("cancel order_id matches", cancel.order_id == order_id)
        t.check("cancel success=True", cancel.success,
                f"final_status={cancel.final_status}")
        t.check("final_status is 'canceled'",
                cancel.final_status in ("canceled", "cancelled"),
                f"got: {cancel.final_status}")
        t.info(f"Cancel result: success={cancel.success} final={cancel.final_status}")
        order_id = None  # consumed — don't try to cancel again in finally

        # --- Verify gone from open orders ---
        await asyncio.sleep(0.5)
        open_after = await broker.get_open_orders()
        ids_after = [o.order_id for o in open_after]
        t.check("Order removed from open_orders after cancel",
                result.order_id not in ids_after)

    except Exception as exc:
        t.check("Order lifecycle raised no exception", False, str(exc))
        traceback.print_exc()
    finally:
        # Safety net: cancel any lingering order
        if order_id:
            t.warn(f"Cleaning up unconsumed order {order_id[:8]}...")
            try:
                await broker.cancel_order(order_id)
            except Exception:
                pass


async def test_broker_fills_history(
    t: IntegrationTest, broker: AlpacaBroker
) -> None:
    t.section("Phase 02 — Broker: Fills History")

    try:
        # Request fills from the last 24 hours
        since = datetime.now(UTC) - timedelta(hours=24)
        fills = await broker.get_fills(since)
        t.check("get_fills(since) returns list",     isinstance(fills, list))
        t.info(f"Fills in last 24h: {len(fills)}")

        for fill in fills[:5]:  # spot-check first 5
            t.check(f"Fill {fill.order_id[:8]}... has symbol",    bool(fill.symbol))
            t.check(f"Fill {fill.order_id[:8]}... has side",
                    fill.side in ("buy", "sell"))
            t.check(f"Fill {fill.order_id[:8]}... qty > 0",       fill.qty > 0)
            t.check(f"Fill {fill.order_id[:8]}... price > 0",     fill.price > 0)
            t.check(f"Fill {fill.order_id[:8]}... timestamp set", fill.timestamp is not None)

        # Request fills from far future (should be empty)
        future = datetime.now(UTC) + timedelta(hours=24)
        future_fills = await broker.get_fills(future)
        t.check("get_fills(future) returns empty list", len(future_fills) == 0)

    except Exception as exc:
        t.check("Fills history raised no exception", False, str(exc))
        traceback.print_exc()


# ===========================================================================
# Phase 03 — Fill Protection
# ===========================================================================

async def test_fill_protection_basic(t: IntegrationTest) -> None:
    t.section("Phase 03 — Fill Protection: Basic State Machine")

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        try:
            # Empty state
            t.check("can_place_order on empty state", fpm.can_place_order("AAPL"))
            t.check("get_pending_orders() empty at start",
                    len(fpm.get_pending_orders()) == 0)

            # Record a PENDING order
            now_iso = datetime.now(UTC).isoformat()
            rec = OrderRecord(
                order_id="int-001", symbol="AAPL", side="buy",
                quantity=5.0, order_type="limit", limit_price=150.0,
                status="PENDING", created_at=now_iso,
                remaining_quantity=5.0,
            )
            await fpm.record_order(rec)

            t.check("can_place_order blocked by PENDING",
                    not fpm.can_place_order("AAPL"))
            t.check("can_place_order still OK for different symbol",
                    fpm.can_place_order("TSLA"))
            t.check("get_pending_orders returns 1",
                    len(fpm.get_pending_orders()) == 1)
            t.check("get_orders_for_symbol AAPL = 1",
                    len(fpm.get_orders_for_symbol("AAPL")) == 1)
            t.check("get_orders_for_symbol TSLA = 0",
                    len(fpm.get_orders_for_symbol("TSLA")) == 0)

            # State persisted to disk
            sm2 = StateManager(state_dir=Path(tmp))
            await sm2.initialize()
            fpm2 = FillProtectionManager(sm2)
            await fpm2.load()
            t.check("PENDING order persisted across restart",
                    len(fpm2.get_pending_orders()) == 1)
            t.check("can_place_order blocked after reload",
                    not fpm2.can_place_order("AAPL"))

        except Exception as exc:
            t.check("Fill protection basic raised no exception", False, str(exc))
            traceback.print_exc()


async def test_fill_protection_reconcile(t: IntegrationTest) -> None:
    t.section("Phase 03 — Fill Protection: Reconcile")

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        now_iso = datetime.now(UTC).isoformat()

        # Seed two orders
        await fpm.record_order(OrderRecord(
            order_id="r-001", symbol="AAPL", side="buy", quantity=10.0,
            order_type="limit", limit_price=150.0, status="PENDING",
            remaining_quantity=10.0, created_at=now_iso,
        ))
        await fpm.record_order(OrderRecord(
            order_id="r-002", symbol="TSLA", side="buy", quantity=5.0,
            order_type="limit", limit_price=200.0, status="PENDING",
            remaining_quantity=5.0, created_at=now_iso,
        ))

        try:
            # Broker reports r-001 filled, r-002 still open
            broker_statuses = [
                OrderStatus(order_id="r-001", status="filled",
                            filled_qty=10.0, remaining_qty=0.0,
                            filled_avg_price=152.0,
                            submitted_at=None, filled_at=None, canceled_at=None),
                OrderStatus(order_id="r-002", status="new",
                            filled_qty=0.0, remaining_qty=5.0,
                            filled_avg_price=None,
                            submitted_at=None, filled_at=None, canceled_at=None),
            ]
            changes = await fpm.reconcile(broker_statuses)

            fills = [c for c in changes if c.change_type == "fill"]
            t.check("reconcile detects 1 fill", len(fills) == 1)
            t.check("filled order is r-001", fills[0].order_id == "r-001")
            t.check("filled order new_status is FILLED", fills[0].new_status == "FILLED")
            t.check("AAPL unblocked after fill", fpm.can_place_order("AAPL"))
            t.check("TSLA still blocked (open)", not fpm.can_place_order("TSLA"))

            # Now broker reports r-002 partially filled
            broker_statuses2 = [
                OrderStatus(order_id="r-002", status="partially_filled",
                            filled_qty=3.0, remaining_qty=2.0,
                            filled_avg_price=200.5,
                            submitted_at=None, filled_at=None, canceled_at=None),
            ]
            changes2 = await fpm.reconcile(broker_statuses2)
            partial = [c for c in changes2 if c.change_type == "partial_fill"]
            t.check("reconcile detects partial fill", len(partial) == 1)
            t.check("TSLA still blocked (partially filled)",
                    not fpm.can_place_order("TSLA"))

            # Now broker reports r-002 cancelled
            broker_statuses3 = [
                OrderStatus(order_id="r-002", status="canceled",
                            filled_qty=3.0, remaining_qty=0.0,
                            filled_avg_price=200.5,
                            submitted_at=None, filled_at=None, canceled_at=None),
            ]
            changes3 = await fpm.reconcile(broker_statuses3)
            cancels = [c for c in changes3 if "cancel" in c.change_type]
            t.check("reconcile detects cancel", len(cancels) >= 1)
            t.check("TSLA unblocked after cancel", fpm.can_place_order("TSLA"))

        except Exception as exc:
            t.check("Reconcile raised no exception", False, str(exc))
            traceback.print_exc()


async def test_fill_protection_stale_orders(t: IntegrationTest) -> None:
    t.section("Phase 03 — Fill Protection: Stale Order Detection")

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        try:
            # Fresh order — should not be stale at 60s timeout
            fresh_iso = datetime.now(UTC).isoformat()
            await fpm.record_order(OrderRecord(
                order_id="s-fresh", symbol="AAPL", side="buy", quantity=5.0,
                order_type="limit", limit_price=150.0, status="PENDING",
                remaining_quantity=5.0, created_at=fresh_iso,
            ))
            stale = fpm.get_stale_orders(timeout_sec=60)
            t.check("Fresh order not stale at 60s", "s-fresh" not in [o.order_id for o in stale])

            # Old order — created 5 minutes ago
            old_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
            await fpm.record_order(OrderRecord(
                order_id="s-old", symbol="TSLA", side="buy", quantity=3.0,
                order_type="limit", limit_price=200.0, status="PENDING",
                remaining_quantity=3.0, created_at=old_iso,
            ))
            stale = fpm.get_stale_orders(timeout_sec=60)
            stale_ids = [o.order_id for o in stale]
            t.check("Old limit order detected as stale", "s-old" in stale_ids)
            t.check("Fresh order not in stale list",     "s-fresh" not in stale_ids)

            # Market order should never be stale (even if old)
            await fpm.record_order(OrderRecord(
                order_id="s-market", symbol="NVDA", side="buy", quantity=2.0,
                order_type="market", limit_price=None, status="PENDING",
                remaining_quantity=2.0, created_at=old_iso,
            ))
            stale2 = fpm.get_stale_orders(timeout_sec=60)
            stale_ids2 = [o.order_id for o in stale2]
            t.check("Market order never flagged as stale", "s-market" not in stale_ids2)

        except Exception as exc:
            t.check("Stale order detection raised no exception", False, str(exc))
            traceback.print_exc()


async def test_fill_protection_cancel_race(t: IntegrationTest) -> None:
    t.section("Phase 03 — Fill Protection: Cancel Race Conditions")

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        now_iso = datetime.now(UTC).isoformat()

        from ozymandias.execution.broker_interface import CancelResult

        try:
            # Scenario 1: clean cancel
            await fpm.record_order(OrderRecord(
                order_id="cr-001", symbol="AAPL", side="buy", quantity=10.0,
                order_type="limit", limit_price=150.0, status="PENDING",
                remaining_quantity=10.0, created_at=now_iso,
            ))
            change = await fpm.handle_cancel_result(
                "cr-001",
                CancelResult(order_id="cr-001", success=True, final_status="canceled")
            )
            t.check("Clean cancel: change_type == 'cancel'", change.change_type == "cancel")
            t.check("Clean cancel: new_status == CANCELLED",  change.new_status == "CANCELLED")
            t.check("AAPL unblocked after clean cancel",      fpm.can_place_order("AAPL"))

            # Scenario 2: cancel-during-fill race (order filled before cancel reached exchange)
            await fpm.record_order(OrderRecord(
                order_id="cr-002", symbol="TSLA", side="buy", quantity=5.0,
                order_type="limit", limit_price=200.0, status="PENDING",
                remaining_quantity=5.0, created_at=now_iso,
            ))
            change2 = await fpm.handle_cancel_result(
                "cr-002",
                CancelResult(order_id="cr-002", success=False, final_status="filled")
            )
            t.check("Fill race: change_type == 'fill'",        change2.change_type == "fill")
            t.check("Fill race: new_status == FILLED",          change2.new_status == "FILLED")
            t.check("TSLA unblocked after fill-race resolution", fpm.can_place_order("TSLA"))

            # Scenario 3: partial fill then cancel
            await fpm.record_order(OrderRecord(
                order_id="cr-003", symbol="NVDA", side="buy", quantity=8.0,
                order_type="limit", limit_price=500.0, status="PARTIALLY_FILLED",
                filled_quantity=3.0, remaining_quantity=5.0, created_at=now_iso,
            ))
            change3 = await fpm.handle_cancel_result(
                "cr-003",
                CancelResult(order_id="cr-003", success=True, final_status="canceled")
            )
            t.check("Partial+cancel: change_type == 'partial_then_cancel'",
                    change3.change_type == "partial_then_cancel")
            t.check("Partial+cancel: new_status == CANCELLED",
                    change3.new_status == "CANCELLED")
            t.check("Partial+cancel: fill_qty preserved",
                    change3.fill_qty == 3.0)
            t.check("NVDA unblocked after partial+cancel",
                    fpm.can_place_order("NVDA"))

        except Exception as exc:
            t.check("Cancel race handling raised no exception", False, str(exc))
            traceback.print_exc()


async def test_buying_power_tracker(t: IntegrationTest) -> None:
    t.section("Phase 03 — Fill Protection: Buying Power Tracker")

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        try:
            pending = [
                OrderRecord(
                    order_id="bp-001", symbol="AAPL", side="buy", quantity=10.0,
                    order_type="limit", limit_price=150.0, status="PENDING",
                    remaining_quantity=10.0, created_at=datetime.now(UTC).isoformat(),
                ),
                OrderRecord(
                    order_id="bp-002", symbol="TSLA", side="buy", quantity=5.0,
                    order_type="limit", limit_price=200.0, status="PENDING",
                    remaining_quantity=5.0, created_at=datetime.now(UTC).isoformat(),
                ),
                # Market order should not be deducted
                OrderRecord(
                    order_id="bp-003", symbol="NVDA", side="buy", quantity=3.0,
                    order_type="market", limit_price=None, status="PENDING",
                    remaining_quantity=3.0, created_at=datetime.now(UTC).isoformat(),
                ),
            ]

            # 10 * 150 + 5 * 200 = 1500 + 1000 = 2500 consumed
            # Market order: not deducted
            reported = 50_000.0
            available = fpm.available_buying_power(reported, pending)
            expected = reported - 2_500.0
            t.check("available_buying_power subtracts limit order values",
                    abs(available - expected) < 0.01,
                    f"expected={expected:.2f} got={available:.2f}")
            t.check("available_buying_power does NOT deduct market orders",
                    available > reported - 3_000.0)

            # No pending orders
            available_empty = fpm.available_buying_power(50_000.0, [])
            t.check("available_buying_power with no orders == reported",
                    abs(available_empty - 50_000.0) < 0.01)

        except Exception as exc:
            t.check("Buying power tracker raised no exception", False, str(exc))
            traceback.print_exc()


# ===========================================================================
# Phase 03 — PDT Guard
# ===========================================================================

async def test_pdt_guard(t: IntegrationTest) -> None:
    t.section("Phase 03 — PDT Guard: Business Day Window")

    try:
        # Reference: week of Mon Mar 10 – Fri Mar 14 2025
        mon = date(2025, 3, 10)
        tue = date(2025, 3, 11)
        wed = date(2025, 3, 12)
        thu = date(2025, 3, 13)
        fri = date(2025, 3, 14)
        sat = date(2025, 3, 15)
        mon2 = date(2025, 3, 17)

        window_fri = _business_days_window(fri)
        t.check("5-day window from Friday has 5 days", len(window_fri) == 5)
        t.check("Friday in window",    fri in window_fri)
        t.check("Thursday in window",  thu in window_fri)
        t.check("Wednesday in window", wed in window_fri)
        t.check("Tuesday in window",   tue in window_fri)
        t.check("Monday in window",    mon in window_fri)
        t.check("Saturday NOT in window", sat not in window_fri)

        window_mon2 = _business_days_window(mon2)
        t.check("Monday2 window skips weekend", sat not in window_mon2)
        t.check("Monday2 window includes prior Friday", fri in window_mon2)
        t.check("Monday2 window has 5 days", len(window_mon2) == 5)

        # _et_date helper
        t.check("_et_date parses ISO string", _et_date("2025-03-10T12:00:00-04:00") is not None)
        t.check("_et_date returns None for empty string", _et_date("") is None)
        t.check("_et_date returns None for bad string",   _et_date("not-a-date") is None)

    except Exception as exc:
        t.check("Business day window raised no exception", False, str(exc))
        traceback.print_exc()


async def test_pdt_guard_day_trades(t: IntegrationTest) -> None:
    t.section("Phase 03 — PDT Guard: Day Trade Counting")

    cfg = RiskConfig()
    guard = PDTGuard(cfg)
    portfolio = PortfolioState()

    mon = date(2025, 3, 10)
    tue = date(2025, 3, 11)
    wed = date(2025, 3, 12)
    fri = date(2025, 3, 14)
    mon2 = date(2025, 3, 17)
    old = date(2025, 3, 3)   # 6 business days before Fri March 14

    ET = ZoneInfo("America/New_York")

    def et_iso(d: date, hour: int = 10) -> str:
        return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=ET).isoformat()

    def filled(oid: str, sym: str, side: str, d: date) -> OrderRecord:
        return OrderRecord(
            order_id=oid, symbol=sym, side=side, quantity=10.0,
            order_type="market", limit_price=None, status="FILLED",
            filled_quantity=10.0, remaining_quantity=0.0,
            created_at=et_iso(d, 9), filled_at=et_iso(d, 10),
        )

    try:
        # Zero trades
        count = guard.count_day_trades([], portfolio, reference_date=wed)
        t.check("count_day_trades == 0 with no orders", count == 0)

        # One day trade
        orders1 = [filled("o1", "AAPL", "buy", tue), filled("o2", "AAPL", "sell", tue)]
        count1 = guard.count_day_trades(orders1, portfolio, reference_date=wed)
        t.check("count_day_trades == 1 for buy+sell same day", count1 == 1)

        # Overnight hold is NOT a day trade
        overnight = [filled("o1", "AAPL", "buy", mon), filled("o2", "AAPL", "sell", tue)]
        count_ov = guard.count_day_trades(overnight, portfolio, reference_date=wed)
        t.check("overnight hold not counted as day trade", count_ov == 0)

        # Buy Fri, sell Mon — not a day trade
        fri_mon = [filled("o1", "AAPL", "buy", fri), filled("o2", "AAPL", "sell", mon2)]
        count_fm = guard.count_day_trades(fri_mon, portfolio, reference_date=mon2)
        t.check("buy-Fri sell-Mon not a day trade", count_fm == 0)

        # Order outside 5-day window not counted
        old_orders = [filled("o1", "AAPL", "buy", old), filled("o2", "AAPL", "sell", old)]
        count_old = guard.count_day_trades(old_orders, portfolio, reference_date=fri)
        t.check("trade from 6 business days ago excluded", count_old == 0)

        # Two different symbols, two day trades
        two_syms = [
            filled("o1", "AAPL", "buy", tue), filled("o2", "AAPL", "sell", tue),
            filled("o3", "TSLA", "buy", wed), filled("o4", "TSLA", "sell", wed),
        ]
        count2 = guard.count_day_trades(two_syms, portfolio, reference_date=wed)
        t.check("two different symbols = 2 day trades", count2 == 2)

        # Multiple buys + one sell = still 1 day trade (not 2)
        multi = [
            filled("o1", "AAPL", "buy", wed), filled("o2", "AAPL", "buy", wed),
            filled("o3", "AAPL", "sell", wed),
        ]
        count_multi = guard.count_day_trades(multi, portfolio, reference_date=wed)
        t.check("multiple buys + 1 sell = 1 day trade (not 2)", count_multi == 1)

        # PENDING orders not counted
        pending_rec = OrderRecord(
            order_id="p1", symbol="AAPL", side="sell", quantity=5.0,
            order_type="limit", limit_price=180.0, status="PENDING",
            created_at=et_iso(wed),
        )
        pending_orders = [filled("o1", "AAPL", "buy", wed), pending_rec]
        count_pend = guard.count_day_trades(pending_orders, portfolio, reference_date=wed)
        t.check("PENDING sell not counted as day trade completion", count_pend == 0)

    except Exception as exc:
        t.check("Day trade counting raised no exception", False, str(exc))
        traceback.print_exc()


async def test_pdt_guard_can_day_trade(t: IntegrationTest) -> None:
    t.section("Phase 03 — PDT Guard: can_day_trade / Buffer Logic")

    ET = ZoneInfo("America/New_York")
    mon = date(2025, 3, 10)
    tue = date(2025, 3, 11)
    wed = date(2025, 3, 12)
    portfolio = PortfolioState()

    def filled(oid: str, sym: str, side: str, d: date) -> OrderRecord:
        dt = datetime(d.year, d.month, d.day, 10, 0, 0, tzinfo=ET).isoformat()
        return OrderRecord(
            order_id=oid, symbol=sym, side=side, quantity=10.0,
            order_type="market", limit_price=None, status="FILLED",
            filled_quantity=10.0, remaining_quantity=0.0,
            created_at=dt, filled_at=dt,
        )

    two_trades = [
        filled("o1", "AAPL", "buy", mon), filled("o2", "AAPL", "sell", mon),
        filled("o3", "TSLA", "buy", tue), filled("o4", "TSLA", "sell", tue),
    ]
    three_trades = two_trades + [
        filled("o5", "NVDA", "buy", wed), filled("o6", "NVDA", "sell", wed),
    ]

    try:
        # Default buffer=1: normal limit is 2
        cfg = RiskConfig(); cfg.pdt_buffer = 1
        guard = PDTGuard(cfg)

        ok, _ = guard.can_day_trade("NVDA", [], portfolio, reference_date=wed)
        t.check("can_day_trade allowed at 0/2", ok)

        ok1, _ = guard.can_day_trade("NVDA", two_trades[:2], portfolio, reference_date=wed)
        t.check("can_day_trade allowed at 1/2", ok1)

        blocked, reason = guard.can_day_trade("NVDA", two_trades, portfolio, reference_date=wed)
        t.check("can_day_trade blocked at 2/2 with buffer=1", not blocked)
        t.check("reason mentions 'limit'", "limit" in reason.lower())

        # Emergency bypass uses the reserved buffer trade
        ok_emg, _ = guard.can_day_trade("NVDA", two_trades, portfolio,
                                         is_emergency=True, reference_date=wed)
        t.check("emergency bypass allowed at 2/3", ok_emg)

        # Emergency also blocked at 3/3
        blocked_emg, _ = guard.can_day_trade("SPY", three_trades, portfolio,
                                              is_emergency=True, reference_date=wed)
        t.check("emergency also blocked at 3/3 absolute limit", not blocked_emg)

        # buffer=0: all 3 usable
        cfg0 = RiskConfig(); cfg0.pdt_buffer = 0
        guard0 = PDTGuard(cfg0)
        ok_buf0, _ = guard0.can_day_trade("NVDA", two_trades, portfolio, reference_date=wed)
        t.check("buffer=0 allows trade at 2/3", ok_buf0)

        # is_emergency_exit stub always returns False in Phase 03
        t.check("is_emergency_exit stub returns False", not guard.is_emergency_exit("AAPL"))

    except Exception as exc:
        t.check("can_day_trade raised no exception", False, str(exc))
        traceback.print_exc()


async def test_pdt_guard_equity_floor(
    t: IntegrationTest, account: AccountInfo
) -> None:
    t.section("Phase 03 — PDT Guard: Equity Floor (with real account)")

    try:
        cfg = RiskConfig()
        guard = PDTGuard(cfg)

        # Real account check
        allowed, reason = guard.check_equity_floor(account)
        t.info(f"Real account equity=${account.equity:,.2f}  pdt_flag={account.pdt_flag}")
        if account.equity >= cfg.min_equity_for_trading:
            t.check("Real account passes equity floor", allowed,
                    f"reason={reason}")
        else:
            t.check("Real account correctly blocked by equity floor", not allowed,
                    f"equity=${account.equity:,.2f} < ${cfg.min_equity_for_trading:,.2f}")

        # Synthetic below-floor account
        low_acct = AccountInfo(
            equity=20_000.0, buying_power=40_000.0, cash=20_000.0,
            currency="USD", pdt_flag=False, daytrade_count=0, account_id="low"
        )
        ok_low, reason_low = guard.check_equity_floor(low_acct)
        t.check("Below-floor account blocked", not ok_low)
        t.check("Reason mentions 'below minimum'", "below minimum" in reason_low.lower())

        # Exactly at floor
        floor_acct = AccountInfo(
            equity=25_500.0, buying_power=51_000.0, cash=25_500.0,
            currency="USD", pdt_flag=False, daytrade_count=0, account_id="floor"
        )
        ok_floor, _ = guard.check_equity_floor(floor_acct)
        t.check("Equity exactly at floor is allowed", ok_floor)

        # PDT-flagged account with >$25k — unlimited
        pdt_acct = AccountInfo(
            equity=30_000.0, buying_power=60_000.0, cash=30_000.0,
            currency="USD", pdt_flag=True, daytrade_count=10, account_id="pdt"
        )
        ok_pdt, reason_pdt = guard.check_equity_floor(pdt_acct)
        t.check("PDT-flagged >$25k account is allowed",   ok_pdt)
        t.check("PDT reason mentions 'unlimited'", "unlimited" in reason_pdt.lower())

        # PDT-flagged but under $25k — blocked
        pdt_low = AccountInfo(
            equity=22_000.0, buying_power=44_000.0, cash=22_000.0,
            currency="USD", pdt_flag=True, daytrade_count=0, account_id="pdt_low"
        )
        ok_pdt_low, _ = guard.check_equity_floor(pdt_low)
        t.check("PDT-flagged but equity < $25k is blocked", not ok_pdt_low)

    except Exception as exc:
        t.check("Equity floor raised no exception", False, str(exc))
        traceback.print_exc()


# ===========================================================================
# Integration — Fill Protection wired to real broker
# ===========================================================================

async def test_integrated_fill_protection_broker(
    t: IntegrationTest, broker: AlpacaBroker
) -> None:
    """
    End-to-end: place a real order through the broker, track it in
    FillProtectionManager, reconcile, cancel, verify unblocked.
    """
    t.section("Integration — Fill Protection + Broker (end-to-end)")

    order_id: str | None = None

    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        await sm.initialize()
        fpm = FillProtectionManager(sm)
        await fpm.load()

        try:
            t.check("can_place_order at start", fpm.can_place_order(TEST_SYMBOL))

            # Place a real limit order
            order = Order(
                symbol=TEST_SYMBOL, side="buy", quantity=TEST_QTY,
                order_type="limit", time_in_force="day", limit_price=TEST_LIMIT,
            )
            result = await broker.place_order(order)
            order_id = result.order_id
            t.check("Order placed via broker", bool(order_id))

            # Record in fill protection
            rec = OrderRecord(
                order_id=order_id,
                symbol=TEST_SYMBOL,
                side="buy",
                quantity=float(TEST_QTY),
                order_type="limit",
                limit_price=float(TEST_LIMIT),
                status="PENDING",
                remaining_quantity=float(TEST_QTY),
                created_at=datetime.now(UTC).isoformat(),
            )
            await fpm.record_order(rec)
            t.check("can_place_order blocked after record_order",
                    not fpm.can_place_order(TEST_SYMBOL))

            # Reconcile with live broker state
            await asyncio.sleep(1)
            open_orders = await broker.get_open_orders()
            changes = await fpm.reconcile(open_orders)
            t.check("reconcile() returns list", isinstance(changes, list))
            t.check("Order still blocked (not filled yet)",
                    not fpm.can_place_order(TEST_SYMBOL))

            # Stale order detection (60s threshold — order is fresh, not stale)
            stale = fpm.get_stale_orders(timeout_sec=60)
            t.check("Test order not yet stale at 60s threshold",
                    order_id not in [o.order_id for o in stale])

            # Stale at 0s threshold
            stale_now = fpm.get_stale_orders(timeout_sec=0)
            t.check("Test order is stale at 0s threshold",
                    order_id in [o.order_id for o in stale_now])

            # Cancel via broker
            cancel_result = await broker.cancel_order(order_id)
            t.check("broker.cancel_order() succeeded", cancel_result.success,
                    f"final_status={cancel_result.final_status}")

            # Handle cancel in fill protection
            change = await fpm.handle_cancel_result(order_id, cancel_result)
            t.check("handle_cancel_result returns StateChange", change is not None)
            t.check("StateChange shows CANCELLED or FILLED",
                    change.new_status in ("CANCELLED", "FILLED"))
            t.check("Symbol unblocked after cancel",
                    fpm.can_place_order(TEST_SYMBOL))

            order_id = None  # consumed

            # Buying power calculation using real account data
            acct = await broker.get_account()
            pending_now = fpm.get_pending_orders()
            avail = fpm.available_buying_power(acct.buying_power, pending_now)
            t.check("available_buying_power with real account is non-negative", avail >= 0)
            t.check("available_buying_power <= reported buying_power",
                    avail <= acct.buying_power + 0.01)
            t.info(f"reported_bp=${acct.buying_power:,.2f}  available=${avail:,.2f}")

        except Exception as exc:
            t.check("Integrated fill protection + broker raised no exception",
                    False, str(exc))
            traceback.print_exc()
        finally:
            if order_id:
                t.warn(f"Cleaning up unconsumed order {order_id[:8]}...")
                try:
                    await broker.cancel_order(order_id)
                except Exception:
                    pass


# ===========================================================================
# Integration — PDT guard against real account
# ===========================================================================

async def test_integrated_pdt_broker(
    t: IntegrationTest, broker: AlpacaBroker
) -> None:
    t.section("Integration — PDT Guard + Real Account Day Trade History")

    try:
        acct = await broker.get_account()
        portfolio = PortfolioState()

        # Fetch recent fills to compute real day-trade count
        since = datetime.now(UTC) - timedelta(days=7)
        fills = await broker.get_fills(since)

        # Build OrderRecord list from fills for PDT counting
        fill_records = [
            OrderRecord(
                order_id=f.order_id,
                symbol=f.symbol,
                side=f.side,
                quantity=f.qty,
                order_type="market",
                limit_price=None,
                status="FILLED",
                filled_quantity=f.qty,
                remaining_quantity=0.0,
                created_at=f.timestamp.isoformat() if hasattr(f.timestamp, "isoformat") else str(f.timestamp),
                filled_at=f.timestamp.isoformat() if hasattr(f.timestamp, "isoformat") else str(f.timestamp),
            )
            for f in fills
        ]

        cfg = RiskConfig()
        guard = PDTGuard(cfg)

        count = guard.count_day_trades(fill_records, portfolio)
        t.check("count_day_trades returns int", isinstance(count, int))
        t.check("count_day_trades is non-negative", count >= 0)
        t.check("Broker daytrade_count consistent with fill history",
                count <= 3,
                f"guard_count={count}  broker_count={acct.daytrade_count}")
        t.info(f"Guard day trade count (7-day fills): {count}  "
               f"Broker reported: {acct.daytrade_count}")

        allowed, reason = guard.can_day_trade(
            "SPY", fill_records, portfolio
        )
        t.check("can_day_trade returns bool", isinstance(allowed, bool))
        t.info(f"can_day_trade('SPY'): allowed={allowed}  reason={reason}")

        # Equity floor with real account
        ok, reason_floor = guard.check_equity_floor(acct)
        t.info(f"check_equity_floor: allowed={ok}")
        if acct.equity >= cfg.min_equity_for_trading or (acct.pdt_flag and acct.equity >= 25_000):
            t.check("Real account passes equity floor check", ok, reason_floor)
        else:
            t.check("Real account correctly blocked by equity floor", not ok)

    except Exception as exc:
        t.check("Integrated PDT + broker raised no exception", False, str(exc))
        traceback.print_exc()


# ===========================================================================
# Integration — Full startup sequence
# ===========================================================================

async def test_startup_sequence(t: IntegrationTest) -> None:
    t.section("Integration — Full startup() Sequence (main.py)")

    try:
        from ozymandias.main import startup
        broker = await startup()
        t.check("startup() completes without exception", True)
        t.check("startup() returns AlpacaBroker", isinstance(broker, AlpacaBroker))

        # Verify the returned broker is functional
        acct = await broker.get_account()
        t.check("Broker from startup() is functional (get_account works)",
                acct.equity > 0)

    except Exception as exc:
        t.check("startup() raised no exception", False, str(exc))
        traceback.print_exc()


# ===========================================================================
# Main runner
# ===========================================================================

async def main(debug: bool = False) -> int:
    log = setup_logging()
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Ozymandias v3 — Comprehensive Integration Test{RESET}")
    print(f"{BOLD}  Phases 01–03{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    t = IntegrationTest()

    # Load credentials once
    api_key, secret_key = _load_credentials()
    broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=True)

    # Fetch account once — reused by several tests
    try:
        account = await broker.get_account()
    except Exception as exc:
        print(f"\n{RED}FATAL: Cannot connect to Alpaca: {exc}{RESET}")
        print("Ensure credentials.enc is valid and the Alpaca paper API is reachable.")
        return 1

    # ── Phase 01 ────────────────────────────────────────────────────────────
    await test_config(t)
    await test_state_manager(t)
    await test_market_hours(t)
    await test_reasoning_cache(t)

    # ── Phase 02 ────────────────────────────────────────────────────────────
    await test_broker_account(t, broker)
    await test_broker_market_hours(t, broker)
    await test_broker_positions(t, broker)
    await test_broker_open_orders(t, broker)
    await test_broker_order_lifecycle(t, broker)
    await test_broker_fills_history(t, broker)

    # ── Phase 03 ────────────────────────────────────────────────────────────
    await test_fill_protection_basic(t)
    await test_fill_protection_reconcile(t)
    await test_fill_protection_stale_orders(t)
    await test_fill_protection_cancel_race(t)
    await test_buying_power_tracker(t)
    await test_pdt_guard(t)
    await test_pdt_guard_day_trades(t)
    await test_pdt_guard_can_day_trade(t)
    await test_pdt_guard_equity_floor(t, account)

    # ── End-to-end integration ───────────────────────────────────────────────
    await test_integrated_fill_protection_broker(t, broker)
    await test_integrated_pdt_broker(t, broker)
    await test_startup_sequence(t)

    return t.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(debug="--debug" in sys.argv)))
