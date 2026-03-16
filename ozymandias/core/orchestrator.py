"""
Orchestrator — wires all modules together and runs three concurrent async loops.

Fast loop  (every 5-15s):   order polling, fill reconciliation, quant overrides, PDT, position sync
Medium loop (every 1-5min): TA scans, signal detection, opportunity ranking, order execution
Slow loop  (every 5min check, Claude only on trigger): strategic AI reasoning

This module is the only place that knows about all other modules. No cross-module
imports are allowed outside of orchestrator.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ozymandias.core.config import Config, load_config
from ozymandias.core.logger import setup_logging
from ozymandias.core.market_hours import Session, get_current_session, is_market_open
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import (
    OrderRecord,
    PortfolioState,
    StateManager,
    WatchlistState,
)
from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
from ozymandias.execution.alpaca_broker import AlpacaBroker
from ozymandias.execution.broker_interface import BrokerInterface, Order
from ozymandias.execution.fill_protection import FillProtectionManager
from ozymandias.execution.pdt_guard import PDTGuard
from ozymandias.execution.risk_manager import RiskManager
from ozymandias.intelligence.claude_reasoning import (
    ClaudeReasoningEngine,
    ReasoningResult,
    _result_from_raw_reasoning,
)
from ozymandias.intelligence.opportunity_ranker import OpportunityRanker
from ozymandias.intelligence.technical_analysis import generate_signal_summary
from ozymandias.strategies.base_strategy import Strategy
from ozymandias.strategies.momentum_strategy import MomentumStrategy
from ozymandias.strategies.swing_strategy import SwingStrategy

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Degradation state
# ---------------------------------------------------------------------------

@dataclass
class DegradationState:
    """Tracks which external dependencies are currently available."""
    claude_available: bool = True
    market_data_available: bool = True
    broker_available: bool = True
    safe_mode: bool = False

    # Timestamps for backoff / recovery tracking (UTC)
    broker_first_failure_utc: Optional[datetime] = None
    claude_backoff_until_utc: Optional[datetime] = None

    BROKER_SAFE_MODE_SECONDS: int = 300  # 5 minutes unreachable → safe mode


# ---------------------------------------------------------------------------
# Slow loop trigger state
# ---------------------------------------------------------------------------

@dataclass
class SlowLoopTriggerState:
    """Tracks state needed to evaluate whether a slow-loop trigger has fired."""
    last_claude_call_utc: Optional[datetime] = None
    last_prices: dict[str, float] = field(default_factory=dict)
    last_override_exit_count: int = 0
    # Whether a Claude call is currently in-flight (prevents concurrent calls)
    claude_call_in_flight: bool = False
    # Track session transitions so we only fire once per transition
    last_session: Optional[str] = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Central loop manager. Initializes all modules, then runs three concurrent
    async loops until interrupted.

    Usage::

        orch = Orchestrator(config_path=Path("config/config.json"))
        await orch.run()
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        log_level: str = "INFO",
        dry_run: bool = False,
    ) -> None:
        """
        Prepare the orchestrator. Loads config and sets up logging.
        Does NOT connect to any external services — that happens in _startup().

        Args:
            config_path: Optional explicit path to config.json.
            log_level:   Root log level ("DEBUG", "INFO", etc.).
        """
        self._log_level = log_level
        self._config: Config = load_config(config_path)

        # -- Core infrastructure (initialized immediately from config) --------
        self._state_manager = StateManager()
        self._reasoning_cache = ReasoningCache()

        # -- External connectors (instantiated in _startup after credentials) -
        self._broker: Optional[BrokerInterface] = None
        self._data_adapter: Optional[YFinanceAdapter] = None

        # -- Intelligence modules (instantiated in _startup) ------------------
        self._claude: Optional[ClaudeReasoningEngine] = None
        self._ranker: Optional[OpportunityRanker] = None

        # -- Execution modules (instantiated in _startup) ---------------------
        self._fill_protection: Optional[FillProtectionManager] = None
        self._pdt_guard: Optional[PDTGuard] = None
        self._risk_manager: Optional[RiskManager] = None

        # -- Strategies (instantiated in _startup) ----------------------------
        self._strategies: list[Strategy] = []

        # -- Runtime state ---------------------------------------------------
        self._degradation = DegradationState()
        self._trigger_state = SlowLoopTriggerState()

        # Intraday highs per symbol — maintained by the fast loop for the
        # ATR trailing stop check.
        self._intraday_highs: dict[str, float] = {}

        # Latest market context from slow loop — consumed by thesis challenge in medium loop
        self._latest_market_context: dict = {}

        # Count of override exits since last Claude call (feeds trigger state)
        self._override_exit_count: int = 0

        # Consecutive Claude failure count — used for exponential backoff
        self._claude_failure_count: int = 0

        # Pending entry intentions: symbol → {stop, target, strategy, reasoning}.
        # Written by _medium_try_entry when an order is placed; consumed by
        # _fast_step_position_sync when a new broker position is discovered.
        self._pending_intentions: dict[str, dict] = {}

        # Shutdown flag — set by _shutdown(), checked by loops
        self._stopping = False

        # Dry-run mode — if True, orders are logged but never submitted
        self._dry_run: bool = dry_run

        # Conservative startup mode — no new entries until this UTC timestamp
        self._conservative_mode_until: Optional[datetime] = None

        log.debug("Orchestrator created (config loaded, modules not yet connected)")

    # -----------------------------------------------------------------------
    # Startup
    # -----------------------------------------------------------------------

    async def _startup(self) -> None:
        """
        Connect to external services and load persistent state.

        Called once before the loops start. Raises on any unrecoverable error
        (bad credentials, corrupt state files, etc.).
        """
        log.info("=== Ozymandias v3 startup ===")
        log.info("Model: %s  env: %s", self._config.claude.model, self._config.broker.environment)

        # -- State files ------------------------------------------------------
        await self._state_manager.initialize()
        log.info("State files ready at: %s", self._state_manager._dir)

        # -- Reasoning cache --------------------------------------------------
        deleted = self._reasoning_cache.rotate()
        log.info("Reasoning cache rotated — %d stale file(s) removed", deleted)

        fresh = self._reasoning_cache.load_latest_if_fresh()
        if fresh:
            log.info("Fresh reasoning cache found (timestamp=%s)", fresh.get("timestamp"))
        else:
            log.info("No fresh cache — Claude will be called on first trigger")

        # -- Broker -----------------------------------------------------------
        api_key, secret_key = self._load_credentials()
        paper = self._config.broker.environment == "paper"
        self._broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)

        acct = await self._broker.get_account()
        log.info(
            "Broker connected [%s] — equity=$%.2f  buying_power=$%.2f  "
            "cash=$%.2f  pdt=%s  daytrades_used=%d",
            "paper" if paper else "live",
            acct.equity, acct.buying_power, acct.cash,
            acct.pdt_flag, acct.daytrade_count,
        )

        hours = await self._broker.get_market_hours()
        log.info(
            "Market: is_open=%s  session=%s  next_open=%s  next_close=%s",
            hours.is_open, hours.session, hours.next_open, hours.next_close,
        )
        self._trigger_state.last_session = hours.session

        # -- Fill protection (loads persisted order state) --------------------
        self._fill_protection = FillProtectionManager(self._state_manager)
        await self._fill_protection.load()

        # -- PDT guard & risk manager -----------------------------------------
        self._pdt_guard = PDTGuard(self._config.risk)
        self._risk_manager = RiskManager(self._config.risk, self._pdt_guard)

        # -- Market data adapter ----------------------------------------------
        self._data_adapter = YFinanceAdapter()

        # -- Claude reasoning engine ------------------------------------------
        self._claude = ClaudeReasoningEngine(
            config=self._config,
            cache=self._reasoning_cache,
        )

        # -- Opportunity ranker -----------------------------------------------
        ranker_cfg = {
            "weight_ai":                   self._config.ranker.weight_ai,
            "weight_technical":            self._config.ranker.weight_technical,
            "weight_risk":                 self._config.ranker.weight_risk,
            "weight_liquidity":            self._config.ranker.weight_liquidity,
            "min_conviction_threshold":    self._config.ranker.min_conviction_threshold,
            "thesis_challenge_size_threshold": self._config.ranker.thesis_challenge_size_threshold,
        }
        self._ranker = OpportunityRanker(config=ranker_cfg)

        # -- Strategies -------------------------------------------------------
        self._strategies = self._build_strategies()
        log.info(
            "Active strategies: %s",
            [type(s).__name__ for s in self._strategies],
        )

        # -- Startup banner ---------------------------------------------------
        watchlist = await self._state_manager.load_watchlist()
        portfolio = await self._state_manager.load_portfolio()
        tier1_count = sum(1 for e in watchlist.entries if e.priority_tier == 1)
        log.info(
            "Startup complete — equity=$%.2f  positions=%d  watchlist=%d (tier1=%d)",
            acct.equity,
            len(portfolio.positions),
            len(watchlist.entries),
            tier1_count,
        )

    # -----------------------------------------------------------------------
    # Startup reconciliation
    # -----------------------------------------------------------------------

    async def startup_reconciliation(self) -> None:
        """
        Compare broker state against local state on startup.

        Any errors (position mismatch, phantom positions, stale orders) are
        logged at ERROR level and trigger conservative startup mode: no new
        entries for 10 minutes. This gives the operator time to review logs
        before the system starts making decisions.
        """
        from ozymandias.core.state_manager import Position, TradeIntention

        log.info("=== Startup reconciliation ===")
        reconciliation_errors = False

        # -- Step 1: Position check ------------------------------------------
        try:
            broker_positions = await self._broker.get_positions()
            portfolio = await self._state_manager.load_portfolio()
            broker_map = {p.symbol: p for p in broker_positions}
            local_map  = {p.symbol: p for p in portfolio.positions}
            updated = False

            for symbol, broker_pos in broker_map.items():
                if symbol in local_map:
                    local_pos = local_map[symbol]
                    if abs(local_pos.shares - broker_pos.qty) > 0.001:
                        log.error(
                            "Position mismatch: %s local=%.4f broker=%.4f — "
                            "updating local to broker (broker is source of truth)",
                            symbol, local_pos.shares, broker_pos.qty,
                        )
                        local_pos.shares = broker_pos.qty
                        reconciliation_errors = True
                        updated = True
                else:
                    log.error(
                        "Unknown broker position: %s %.4f shares @ %.4f — "
                        "adding to local state with reconciled=True",
                        symbol, broker_pos.qty, broker_pos.avg_entry_price,
                    )
                    now_iso = datetime.now(timezone.utc).isoformat()
                    portfolio.positions.append(Position(
                        symbol=symbol,
                        shares=broker_pos.qty,
                        avg_cost=broker_pos.avg_entry_price,
                        entry_date=now_iso,
                        intention=TradeIntention(),
                        reconciled=True,
                    ))
                    reconciliation_errors = True
                    updated = True

            for symbol in list(local_map.keys()):
                if symbol not in broker_map:
                    log.error(
                        "Phantom local position: %s not found broker-side — "
                        "removing from local state",
                        symbol,
                    )
                    portfolio.positions = [
                        p for p in portfolio.positions if p.symbol != symbol
                    ]
                    reconciliation_errors = True
                    updated = True

            if updated:
                await self._state_manager.save_portfolio(portfolio)
                log.info("Step 1: portfolio updated after reconciliation")
            else:
                log.info("Step 1 OK — all positions match broker")

        except Exception as exc:
            log.error("Startup reconciliation step 1 failed: %s", exc, exc_info=True)
            reconciliation_errors = True

        # -- Step 2: Order cleanup -------------------------------------------
        try:
            broker_open_orders = await self._broker.get_open_orders()
            broker_order_ids = {o.order_id for o in broker_open_orders}
            orders_state = await self._state_manager.load_orders()
            now_iso = datetime.now(timezone.utc).isoformat()
            stale_found = False

            for order in orders_state.orders:
                if order.status in ("PENDING", "PARTIALLY_FILLED"):
                    if order.order_id not in broker_order_ids:
                        log.warning(
                            "Local order %s (%s %s) not found broker-side — "
                            "marking CANCELLED",
                            order.order_id, order.side, order.symbol,
                        )
                        order.status = "CANCELLED"
                        order.cancelled_at = now_iso
                        stale_found = True

            local_ids = {o.order_id for o in orders_state.orders}
            for broker_order in broker_open_orders:
                if broker_order.order_id not in local_ids:
                    log.warning(
                        "Broker order %s (status=%s) not tracked locally",
                        broker_order.order_id, broker_order.status,
                    )

            if stale_found:
                await self._state_manager.save_orders(orders_state)
                log.info("Step 2: stale orders marked CANCELLED")
            else:
                log.info("Step 2 OK — all local orders tracked broker-side")

        except Exception as exc:
            log.error("Startup reconciliation step 2 failed: %s", exc, exc_info=True)
            reconciliation_errors = True

        # -- Step 3: Account state -------------------------------------------
        try:
            acct = await self._broker.get_account()
            log.info(
                "Account snapshot — equity=$%.2f  buying_power=$%.2f  cash=$%.2f  "
                "pdt=%s  daytrades_used=%d",
                acct.equity, acct.buying_power, acct.cash,
                acct.pdt_flag, acct.daytrade_count,
            )
            if acct.equity < 25_500.0:
                log.warning(
                    "Account equity $%.2f below PDT threshold $25,500 — "
                    "new entries will be blocked by risk manager",
                    acct.equity,
                )
        except Exception as exc:
            log.error("Startup reconciliation step 3 failed: %s", exc, exc_info=True)
            reconciliation_errors = True

        # -- Step 4: Reasoning cache -----------------------------------------
        fresh = self._reasoning_cache.load_latest_if_fresh()
        if fresh:
            log.info(
                "Step 4 — fresh cache found (timestamp=%s)",
                fresh.get("timestamp"),
            )
        else:
            log.info("Step 4 — no fresh cache — Claude will be called on first trigger")

        # -- Step 5: Validation gate -----------------------------------------
        if reconciliation_errors:
            mins = self._config.scheduler.conservative_startup_mode_min
            conservative_until = datetime.now(timezone.utc) + timedelta(minutes=mins)
            self._conservative_mode_until = conservative_until
            log.warning(
                "Reconciliation errors found — conservative startup mode active until %s "
                "(no new entries for %d minutes)",
                conservative_until.isoformat(), mins,
            )
        else:
            log.info("Step 5 OK — startup reconciliation clean, proceeding normally")

        log.info("=== Startup reconciliation complete ===")

    # -----------------------------------------------------------------------
    # Dry-run mode
    # -----------------------------------------------------------------------

    def _apply_dry_run_mode(self) -> None:
        """
        Replace broker.place_order with a stub that logs but never submits.
        Called once at startup when --dry-run is active.
        """
        from ozymandias.execution.broker_interface import OrderResult

        async def _dry_place_order(order: Order) -> OrderResult:
            log.info(
                "[DRY RUN] Would place order — symbol=%s  side=%s  qty=%g  "
                "type=%s  limit=%s  tif=%s",
                order.symbol, order.side, order.quantity,
                order.order_type,
                f"{order.limit_price:.4f}" if order.limit_price else "market",
                order.time_in_force,
            )
            return OrderResult(
                order_id=f"dry-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
                status="pending_new",
                submitted_at=datetime.now(timezone.utc),
            )

        self._broker.place_order = _dry_place_order
        log.info("Dry-run mode active — orders will be logged but NOT submitted")

    # -----------------------------------------------------------------------
    # Run (main entry point)
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main entry point. Calls _startup() then launches all three loops
        concurrently via asyncio.TaskGroup.

        Handles KeyboardInterrupt / SIGINT for graceful shutdown.
        """
        setup_logging()

        await self._startup()
        await self.startup_reconciliation()
        if self._dry_run:
            self._apply_dry_run_mode()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._fast_loop(),   name="fast_loop")
                tg.create_task(self._medium_loop(), name="medium_loop")
                tg.create_task(self._slow_loop(),   name="slow_loop")
        except* KeyboardInterrupt:
            log.info("KeyboardInterrupt received — shutting down")
            await self._shutdown()
        except* Exception as eg:
            for exc in eg.exceptions:
                log.critical("Unhandled exception in task group: %s", exc, exc_info=exc)
            await self._shutdown()
            raise

    # -----------------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Gracefully stop all loops and persist state."""
        self._stopping = True
        log.info("Shutdown initiated — saving state")

        if self._state_manager:
            try:
                portfolio = await self._state_manager.load_portfolio()
                await self._state_manager.save_portfolio(portfolio)
                log.info("Portfolio state saved")
            except Exception as exc:
                log.error("Failed to save portfolio on shutdown: %s", exc)

        log.info("Shutdown complete")

    # -----------------------------------------------------------------------
    # Fast loop
    # -----------------------------------------------------------------------

    async def _fast_loop(self) -> None:
        """
        Fast loop wrapper — runs _fast_loop_cycle() on every tick.
        Never raises; errors are logged and the loop continues.
        """
        while not self._stopping:
            try:
                await self._fast_loop_cycle()
            except Exception as exc:
                log.error("Fast loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self._config.scheduler.fast_loop_sec)

    async def _fast_loop_cycle(self) -> None:
        """
        One fast loop tick. Steps:
        1. Poll broker for open order statuses and reconcile with local state.
        2. Handle stale orders: cancel any limit orders past their timeout.
        3. Execute quant overrides on open positions.
        4. PDT guard check.
        5. Position sync (broker vs local).
        """
        # Guard — if broker is unavailable, skip order operations but still
        # run local checks.
        if not self._degradation.broker_available:
            log.warning("Fast loop: broker unavailable — skipping order operations")
            try:
                await self._fast_step_pdt_check()
            except Exception as exc:
                log.error("Fast loop PDT check error: %s", exc, exc_info=True)
            return

        # Each step is isolated: an exception in one must not prevent the others.
        # Step 1 & 2: poll + reconcile + handle stale orders
        try:
            await self._fast_step_poll_and_reconcile()
        except Exception as exc:
            log.error("Fast loop poll/reconcile error: %s", exc, exc_info=True)

        # Step 3: quant overrides
        try:
            await self._fast_step_quant_overrides()
        except Exception as exc:
            log.error("Fast loop quant overrides error: %s", exc, exc_info=True)

        # Step 4: PDT guard check
        try:
            await self._fast_step_pdt_check()
        except Exception as exc:
            log.error("Fast loop PDT check error: %s", exc, exc_info=True)

        # Step 5: position sync
        try:
            await self._fast_step_position_sync()
        except Exception as exc:
            log.error("Fast loop position sync error: %s", exc, exc_info=True)

    # -- Fast loop steps ----------------------------------------------------

    async def _fast_step_poll_and_reconcile(self) -> None:
        """
        Step 1: Poll broker for all open order statuses.
        Step 2: Reconcile broker state with local FillProtectionManager.
        Step 3: Cancel any stale limit orders; wait for confirmation.
        """
        try:
            broker_statuses = await self._broker.get_open_orders()
        except Exception as exc:
            self._mark_broker_failure(exc)
            return

        self._mark_broker_available()

        # Reconcile
        changes = await self._fill_protection.reconcile(broker_statuses)
        for change in changes:
            log.info(
                "Order state change: %s %s → %s (type=%s fill_qty=%.2f)",
                change.symbol, change.old_status, change.new_status,
                change.change_type, change.fill_qty,
            )

        # Also poll any locally-tracked orders not in broker's open list
        orders_state = await self._state_manager.load_orders()
        pending = [
            o for o in orders_state.orders
            if o.status in ("PENDING", "PARTIALLY_FILLED")
        ]
        broker_ids = {s.order_id for s in broker_statuses}
        for order in pending:
            if order.order_id not in broker_ids:
                # Order disappeared from broker open list — poll explicitly
                try:
                    status = await self._broker.get_order_status(order.order_id)
                    changes2 = await self._fill_protection.reconcile([status])
                    for change in changes2:
                        log.info(
                            "Explicit poll — order state change: %s %s → %s",
                            change.symbol, change.old_status, change.new_status,
                        )
                except Exception as exc:
                    log.warning("Failed to poll order %s: %s", order.order_id, exc)

        # Handle stale orders
        stale = self._fill_protection.get_stale_orders(
            timeout_sec=self._config.scheduler.fast_loop_sec * 6  # default 60s
        )
        for stale_order in stale:
            log.warning(
                "Cancelling stale order %s for %s (type=%s age=%ds)",
                stale_order.order_id, stale_order.symbol,
                stale_order.order_type, stale_order.timeout_seconds,
            )
            try:
                cancel_result = await self._broker.cancel_order(stale_order.order_id)
                change = await self._fill_protection.handle_cancel_result(
                    stale_order.order_id, cancel_result
                )
                log.info(
                    "Cancel result for %s: success=%s final_status=%s (change_type=%s)",
                    stale_order.order_id, cancel_result.success,
                    cancel_result.final_status, change.change_type,
                )
            except Exception as exc:
                log.error(
                    "Failed to cancel stale order %s: %s",
                    stale_order.order_id, exc,
                )

    async def _fast_step_quant_overrides(self) -> None:
        """
        Step 3: For each open position, evaluate quantitative override signals.
        If triggered, place a market exit order immediately.
        """
        portfolio = await self._state_manager.load_portfolio()
        if not portfolio.positions:
            return

        orders_state = await self._state_manager.load_orders()

        for position in portfolio.positions:
            symbol = position.symbol

            # We need current indicators — use cached TA data if available.
            # In the fast loop we reuse the most recent indicator data computed
            # by the medium loop (stored in _latest_indicators). If not yet
            # computed, skip override checks for this symbol.
            indicators = getattr(self, "_latest_indicators", {}).get(symbol)
            if indicators is None:
                log.debug(
                    "No indicators cached for %s — skipping override check", symbol
                )
                continue

            # Update intraday high
            current_price = indicators.get("price")
            if current_price is not None:
                prev_high = self._intraday_highs.get(symbol, 0.0)
                self._intraday_highs[symbol] = max(prev_high, current_price)

            intraday_high = self._intraday_highs.get(symbol, current_price or 0.0)

            should_exit, triggered_signals = self._risk_manager.evaluate_overrides(
                position, indicators, intraday_high
            )

            if not should_exit:
                continue

            log.warning(
                "QUANT OVERRIDE EXIT — %s  signals=%s  price=%.4f",
                symbol, triggered_signals, current_price or 0.0,
            )

            # Fill protection: only place if no pending order for this symbol
            if not self._fill_protection.can_place_order(symbol):
                log.warning(
                    "Override exit for %s blocked — pending order already exists",
                    symbol,
                )
                continue

            # Place market sell order
            exit_order = Order(
                symbol=symbol,
                side="sell",
                quantity=position.shares,
                order_type="market",
                time_in_force="day",
            )
            try:
                result = await self._broker.place_order(exit_order)
                now_iso = datetime.now(timezone.utc).isoformat()
                order_record = OrderRecord(
                    order_id=result.order_id,
                    symbol=symbol,
                    side="sell",
                    quantity=position.shares,
                    order_type="market",
                    limit_price=None,
                    status="PENDING",
                    created_at=now_iso,
                    last_checked_at=now_iso,
                )
                await self._fill_protection.record_order(order_record)
                log.info(
                    "Override exit order placed — %s  order_id=%s  qty=%.2f",
                    symbol, result.order_id, position.shares,
                )

                # Increment override exit counter (feeds slow loop trigger)
                self._override_exit_count += 1
                self._trigger_state.last_override_exit_count = self._override_exit_count

            except Exception as exc:
                log.error("Failed to place override exit for %s: %s", symbol, exc)

    async def _fast_step_pdt_check(self) -> None:
        """
        Step 4: Check that the PDT day-trade count hasn't been exceeded.
        Log a WARNING if approaching the limit.
        """
        try:
            orders_state = await self._state_manager.load_orders()
            portfolio = await self._state_manager.load_portfolio()

            day_trades = self._pdt_guard.count_day_trades(
                orders_state.orders, portfolio
            )
            allowed = 3 - self._config.risk.pdt_buffer  # effective safe limit
            log.debug("PDT check: %d day trades used (safe limit=%d)", day_trades, allowed)

            if day_trades >= allowed:
                log.warning(
                    "PDT WARNING: %d of %d day trades used (buffer=%d). "
                    "No more entries without emergency flag.",
                    day_trades, 3, self._config.risk.pdt_buffer,
                )
        except Exception as exc:
            log.error("PDT check error: %s", exc, exc_info=True)

    async def _fast_step_position_sync(self) -> None:
        """
        Step 5: Compare local portfolio state with broker-reported positions.
        Log any discrepancies.
        """
        try:
            broker_positions = await self._broker.get_positions()
        except Exception as exc:
            self._mark_broker_failure(exc)
            return

        self._mark_broker_available()

        try:
            portfolio = await self._state_manager.load_portfolio()
        except Exception as exc:
            log.error("Failed to load portfolio for sync: %s", exc, exc_info=True)
            return

        from ozymandias.core.state_manager import ExitTargets, Position, TradeIntention

        broker_symbols = {p.symbol for p in broker_positions}
        local_symbols = {p.symbol for p in portfolio.positions}

        # Positions we have locally but broker doesn't know about
        ghost_local = local_symbols - broker_symbols
        if ghost_local:
            log.warning(
                "Position sync discrepancy — local has positions not in broker: %s",
                ghost_local,
            )

        # Positions broker has that we don't track locally — create them now so that
        # stop/target checks and Claude position reviews work correctly during the session.
        unknown_broker = broker_symbols - local_symbols
        portfolio_updated = False
        for bp in broker_positions:
            if bp.symbol not in local_symbols:
                # Pull pending intention stored when the entry order was placed.
                # Falls back to empty intention if the position arrived unexpectedly.
                pending = self._pending_intentions.pop(bp.symbol, {})
                now_iso = datetime.now(timezone.utc).isoformat()
                intention = TradeIntention(
                    strategy=pending.get("strategy", "unknown"),
                    reasoning=pending.get("reasoning", ""),
                    exit_targets=ExitTargets(
                        stop_loss=pending.get("stop", 0.0),
                        profit_target=pending.get("target", 0.0),
                    ),
                )
                portfolio.positions.append(Position(
                    symbol=bp.symbol,
                    shares=bp.qty,
                    avg_cost=bp.avg_entry_price,
                    entry_date=now_iso,
                    intention=intention,
                ))
                portfolio_updated = True
                log.info(
                    "New position created from fill: %s  qty=%.2f  avg_cost=%.4f  "
                    "stop=%.4f  target=%.4f",
                    bp.symbol, bp.qty, bp.avg_entry_price,
                    pending.get("stop", 0.0), pending.get("target", 0.0),
                )

        if portfolio_updated:
            await self._state_manager.save_portfolio(portfolio)

        # Quantity mismatches for shared symbols
        local_map = {p.symbol: p for p in portfolio.positions}
        for bp in broker_positions:
            lp = local_map.get(bp.symbol)
            if lp is None:
                continue
            if abs(bp.qty - lp.shares) > 0.001:
                log.warning(
                    "Position sync discrepancy — %s: local=%.4f broker=%.4f",
                    bp.symbol, lp.shares, bp.qty,
                )

    # -----------------------------------------------------------------------
    # Medium loop
    # -----------------------------------------------------------------------

    async def _medium_loop(self) -> None:
        while not self._stopping:
            try:
                await self._medium_loop_cycle()
            except Exception as exc:
                log.error("Medium loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self._config.scheduler.medium_loop_sec)

    async def _medium_loop_cycle(self) -> None:
        """
        One medium loop tick. Steps:
        1. Fetch latest bars for Tier 1 watchlist symbols + open positions.
        2. Run TA on all fetched data; cache indicators for fast loop.
        3. Detect entry signals via active strategies.
        4. Re-rank opportunity queue using cached Claude reasoning + fresh TA.
        5. Execute top opportunity (one per cycle) if risk-validated.
        6. Re-evaluate open positions; exit if strategy recommends it.
        """
        if self._degradation.market_data_available is False:
            log.warning("Medium loop: market data unavailable — skipping cycle")
            return

        # -- Step 1: gather symbols to scan ----------------------------------
        watchlist = await self._state_manager.load_watchlist()
        portfolio = await self._state_manager.load_portfolio()
        orders_state = await self._state_manager.load_orders()

        tier1_symbols = [e.symbol for e in watchlist.entries if e.priority_tier == 1]
        position_symbols = [p.symbol for p in portfolio.positions]
        scan_symbols = list(dict.fromkeys(tier1_symbols + position_symbols))  # dedup, order-preserving

        if not scan_symbols:
            log.debug("Medium loop: no symbols to scan")
            return

        # -- Step 2: fetch bars + run TA -------------------------------------
        indicators: dict[str, dict] = {}  # symbol → full generate_signal_summary() output
        bars: dict[str, object] = {}      # symbol → DataFrame

        for symbol in scan_symbols:
            try:
                df = await self._data_adapter.fetch_bars(symbol, interval="5m", period="1d")
                if df is None or df.empty:
                    log.warning("Medium loop: no bars returned for %s", symbol)
                    continue
                summary = generate_signal_summary(symbol, df)
                indicators[symbol] = summary
                bars[symbol] = df
            except Exception as exc:
                log.warning("Medium loop: TA failed for %s: %s", symbol, exc)

        if not indicators:
            log.warning("Medium loop: TA produced no results — all fetches failed")
            self._degradation.market_data_available = False
            return

        self._degradation.market_data_available = True

        # Cache indicators for the fast loop (quant overrides)
        self._latest_indicators = {sym: v["signals"] for sym, v in indicators.items()}

        log.debug("Medium loop: scanned %d symbol(s)", len(indicators))

        # -- Step 3: detect entry signals ------------------------------------
        # Load the most recent Claude reasoning result early so session_veto and
        # require_strong_entry can gate signal generation before ranking.
        cached_raw = self._reasoning_cache.load_latest_if_fresh()
        if cached_raw:
            parsed = cached_raw.get("parsed_response") or {}
            reasoning_result = _result_from_raw_reasoning(parsed)
        else:
            reasoning_result = ReasoningResult(
                timestamp=datetime.now(timezone.utc).isoformat(),
                position_reviews=[],
                new_opportunities=[],
                watchlist_changes={"add": [], "remove": [], "rationale": ""},
                market_assessment="unknown",
                risk_flags=[],
                rejected_opportunities=[],
                session_veto=[],
                raw={},
            )

        # Build per-(symbol, strategy) require_strong_entry lookup from Claude opportunities.
        _require_strong: dict[tuple[str, str], bool] = {
            (opp["symbol"], opp.get("strategy", "")): bool(opp.get("require_strong_entry", False))
            for opp in reasoning_result.new_opportunities
            if "symbol" in opp
        }

        # {symbol: [Signal, ...]}
        entry_signals: dict[str, list] = {}
        for symbol, summary in indicators.items():
            df = bars.get(symbol)
            if df is None:
                continue
            sigs_flat = summary["signals"]
            for strategy in self._strategies:
                strategy_name = type(strategy).__name__.replace("Strategy", "").lower()

                # Session veto: Claude has assessed this strategy as invalid for today's regime.
                if strategy_name in reasoning_result.session_veto:
                    log.info(
                        "[veto] skipping %s entry for %s — session veto active",
                        strategy_name, symbol,
                    )
                    continue

                # require_strong_entry: temporarily raise min_signals_for_entry by 1
                # for this symbol. Safe because asyncio is single-threaded (no races).
                strong = _require_strong.get((symbol, strategy_name), False)
                if strong:
                    orig_min = strategy._params["min_signals_for_entry"]
                    strategy._params["min_signals_for_entry"] = orig_min + 1
                try:
                    sigs = await strategy.generate_signals(symbol, df, sigs_flat)
                    if sigs:
                        entry_signals.setdefault(symbol, []).extend(sigs)
                except Exception as exc:
                    log.warning(
                        "Medium loop: generate_signals failed for %s/%s: %s",
                        symbol, type(strategy).__name__, exc,
                    )
                finally:
                    if strong:
                        strategy._params["min_signals_for_entry"] = orig_min

        # -- Step 4: re-rank opportunity queue --------------------------------
        # reasoning_result already loaded above (before step 3).

        try:
            acct = await self._broker.get_account()
        except Exception as exc:
            self._mark_broker_failure(exc)
            return
        self._mark_broker_available()

        ranked = self._ranker.rank_opportunities(
            reasoning_result,
            indicators,
            acct,
            portfolio,
            self._pdt_guard,
            is_market_open,
            orders=orders_state.orders,
        )

        log.debug(
            "Medium loop: ranker returned %d opportunity/ies (from %d Claude candidates)",
            len(ranked), len(reasoning_result.new_opportunities),
        )

        # -- Step 5: execute top opportunity (one per cycle) -----------------
        if not self._degradation.safe_mode and ranked:
            top = ranked[0]
            await self._medium_try_entry(top, acct, portfolio, orders_state.orders)

        # -- Step 6: re-evaluate open positions ------------------------------
        await self._medium_evaluate_positions(portfolio, bars, indicators, acct, orders_state.orders)

    async def _medium_try_entry(self, top, acct, portfolio, orders) -> None:
        """Validate and execute a single entry order for the top-ranked opportunity."""
        symbol = top.symbol
        entry_price = top.suggested_entry

        # Conservative startup mode — suppress new entries until timer expires
        if self._conservative_mode_until is not None:
            now = datetime.now(timezone.utc)
            if now < self._conservative_mode_until:
                log.info(
                    "Medium loop: conservative mode active until %s — skipping entry for %s",
                    self._conservative_mode_until.isoformat(), symbol,
                )
                return
        strategy_name = top.strategy

        # Determine ATR for position sizing
        ind = self._latest_indicators.get(symbol, {})
        atr = ind.get("atr_14", 0.0)
        avg_vol = ind.get("avg_daily_volume")

        quantity = self._risk_manager.calculate_position_size(
            symbol, entry_price, atr, acct.equity
        )
        if quantity <= 0:
            log.debug("Medium loop: position size = 0 for %s — skipping", symbol)
            return

        allowed, reason = self._risk_manager.validate_entry(
            symbol=symbol,
            side="buy",
            quantity=quantity,
            price=entry_price,
            strategy=strategy_name,
            account=acct,
            portfolio=portfolio,
            orders=orders,
            avg_daily_volume=avg_vol,
        )
        if not allowed:
            log.debug("Medium loop: entry blocked for %s — %s", symbol, reason)
            return

        if not self._fill_protection.can_place_order(symbol):
            log.debug("Medium loop: fill protection blocking entry for %s", symbol)
            return

        # Thesis challenge for large positions (config: ranker.thesis_challenge_size_threshold)
        challenge_size_threshold = self._config.ranker.thesis_challenge_size_threshold
        if top.position_size_pct >= challenge_size_threshold:
            challenge = await self._claude.run_thesis_challenge(
                opportunity={
                    "symbol": symbol,
                    "strategy": strategy_name,
                    "conviction": top.ai_conviction,
                    "suggested_entry": entry_price,
                    "suggested_exit": top.suggested_exit,
                    "suggested_stop": top.suggested_stop,
                    "position_size_pct": top.position_size_pct,
                    "reasoning": top.reasoning,
                },
                market_context=self._latest_market_context,
                indicators=self._latest_indicators,
            )
            if challenge is not None:
                if not challenge.get("proceed", True):
                    log.info(
                        "Thesis challenge blocked entry for %s: %s",
                        symbol, challenge.get("challenge_reasoning", ""),
                    )
                    return
                challenge_conviction = float(challenge.get("conviction", top.ai_conviction))
                if challenge_conviction < top.ai_conviction and top.ai_conviction > 0:
                    # Scale quantity proportionally to conviction reduction
                    ratio = challenge_conviction / top.ai_conviction
                    quantity = max(1, int(quantity * ratio))
                    log.info(
                        "Thesis challenge reduced conviction for %s: %.2f → %.2f, "
                        "quantity scaled to %d",
                        symbol, top.ai_conviction, challenge_conviction, quantity,
                    )

        # Determine stop and target for position intention.
        # Prefer Claude's suggested levels (specific support/resistance) when provided;
        # fall back to ATR-based calculation if Claude returned 0.
        atr_for_intention = ind.get("atr_14", 0.0)
        use_stop = (
            top.suggested_stop
            if top.suggested_stop > 0
            else (entry_price - 2 * atr_for_intention if atr_for_intention > 0 else entry_price * 0.95)
        )
        use_target = (
            top.suggested_exit
            if top.suggested_exit > 0
            else (entry_price + 3 * atr_for_intention if atr_for_intention > 0 else entry_price * 1.10)
        )

        order = Order(
            symbol=symbol,
            side="buy",
            quantity=quantity,
            order_type="limit",
            limit_price=entry_price,
            time_in_force="day",
        )
        try:
            result = await self._broker.place_order(order)
        except Exception as exc:
            self._mark_broker_failure(exc)
            return
        self._mark_broker_available()

        # Store intention so _fast_step_position_sync can attach it to the position on fill.
        self._pending_intentions[symbol] = {
            "stop": round(use_stop, 4),
            "target": round(use_target, 4),
            "strategy": strategy_name,
            "reasoning": top.reasoning,
        }

        now_iso = datetime.now(timezone.utc).isoformat()
        record = OrderRecord(
            order_id=result.order_id,
            symbol=symbol,
            side="buy",
            quantity=quantity,
            order_type="limit",
            limit_price=entry_price,
            status="PENDING",
            created_at=now_iso,
            last_checked_at=now_iso,
        )
        await self._fill_protection.record_order(record)
        log.info(
            "Entry order placed — %s  qty=%d  limit=%.2f  strategy=%s  score=%.3f  "
            "stop=%.4f  target=%.4f  (stop_src=%s  target_src=%s)",
            symbol, quantity, entry_price, strategy_name, top.composite_score,
            use_stop, use_target,
            "claude" if top.suggested_stop > 0 else "atr",
            "claude" if top.suggested_exit > 0 else "atr",
        )

    async def _medium_evaluate_positions(self, portfolio, bars, indicators, acct, orders) -> None:
        """Run evaluate_position() on each open position; exit if recommended."""
        for position in portfolio.positions:
            symbol = position.symbol
            df = bars.get(symbol)
            ind_summary = indicators.get(symbol)
            if df is None or ind_summary is None:
                log.debug("Medium loop: no data for position %s — skipping eval", symbol)
                continue

            sigs_flat = ind_summary["signals"]

            for strategy in self._strategies:
                try:
                    eval_result = await strategy.evaluate_position(position, df, sigs_flat)
                except Exception as exc:
                    log.warning(
                        "Medium loop: evaluate_position failed %s/%s: %s",
                        symbol, type(strategy).__name__, exc,
                    )
                    continue

                if eval_result.action != "exit":
                    continue

                log.info(
                    "Medium loop: strategy exit signal — %s  action=%s  confidence=%.2f  reason=%s",
                    symbol, eval_result.action, eval_result.confidence, eval_result.reasoning,
                )

                if not self._fill_protection.can_place_order(symbol):
                    log.warning(
                        "Medium loop: exit blocked for %s — pending order exists", symbol
                    )
                    break

                # Get suggested exit parameters
                try:
                    exit_sug = await strategy.suggest_exit(position, df, sigs_flat)
                except Exception as exc:
                    log.warning("Medium loop: suggest_exit failed %s: %s", symbol, exc)
                    exit_sug = None

                if exit_sug and exit_sug.order_type == "limit" and exit_sug.exit_price > 0:
                    exit_order = Order(
                        symbol=symbol,
                        side="sell",
                        quantity=position.shares,
                        order_type="limit",
                        limit_price=exit_sug.exit_price,
                        time_in_force="day",
                    )
                else:
                    exit_order = Order(
                        symbol=symbol,
                        side="sell",
                        quantity=position.shares,
                        order_type="market",
                        time_in_force="day",
                    )

                try:
                    result = await self._broker.place_order(exit_order)
                except Exception as exc:
                    self._mark_broker_failure(exc)
                    break
                self._mark_broker_available()

                now_iso = datetime.now(timezone.utc).isoformat()
                record = OrderRecord(
                    order_id=result.order_id,
                    symbol=symbol,
                    side="sell",
                    quantity=position.shares,
                    order_type=exit_order.order_type,
                    limit_price=exit_order.limit_price,
                    status="PENDING",
                    created_at=now_iso,
                    last_checked_at=now_iso,
                )
                await self._fill_protection.record_order(record)
                log.info(
                    "Exit order placed — %s  qty=%d  type=%s  strategy=%s",
                    symbol, position.shares, exit_order.order_type, type(strategy).__name__,
                )
                break  # one exit order per position per cycle

    # -----------------------------------------------------------------------
    # Slow loop
    # -----------------------------------------------------------------------

    async def _slow_loop(self) -> None:
        while not self._stopping:
            try:
                await self._slow_loop_cycle()
            except Exception as exc:
                log.error("Slow loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self._config.scheduler.slow_loop_check_sec)

    async def _slow_loop_cycle(self) -> None:
        """
        One slow loop tick (runs every slow_loop_check_sec, default 5 min).

        Evaluates all trigger conditions. If none fire, updates trigger state
        and returns. If one or more fire and no Claude call is already in-flight,
        starts a Claude reasoning cycle. The cycle is async — fast and medium
        loops continue uninterrupted while waiting for Claude's response.
        """
        # Guard: never fire two concurrent Claude calls
        if self._trigger_state.claude_call_in_flight:
            log.debug("Slow loop: Claude call already in-flight — skipping check")
            return

        # Guard: Claude API is in backoff
        if self._degradation.claude_backoff_until_utc is not None:
            now = datetime.now(timezone.utc)
            if now < self._degradation.claude_backoff_until_utc:
                log.debug(
                    "Slow loop: Claude in backoff until %s — skipping",
                    self._degradation.claude_backoff_until_utc.isoformat(),
                )
                return
            # Backoff expired — try again
            self._degradation.claude_backoff_until_utc = None

        triggers = await self._check_triggers()

        if not triggers:
            log.debug("Slow loop: no triggers fired — no-op")
            await self._update_trigger_prices()
            return

        log.info("Slow loop: triggers fired — %s", triggers)

        # Mark in-flight before the await so concurrent checks skip
        self._trigger_state.claude_call_in_flight = True
        try:
            await self._run_claude_cycle(trigger_name="|".join(triggers))
        finally:
            self._trigger_state.claude_call_in_flight = False

    async def _check_triggers(self) -> list[str]:
        """
        Evaluate all slow-loop trigger conditions.
        Returns a list of trigger name strings (empty = no trigger).
        """
        triggers: list[str] = []
        now = datetime.now(timezone.utc)
        ts = self._trigger_state

        # 1. Time ceiling: 60+ minutes since last Claude call
        if ts.last_claude_call_utc is None:
            triggers.append("no_previous_call")
        else:
            elapsed_min = (now - ts.last_claude_call_utc).total_seconds() / 60
            if elapsed_min >= self._config.claude.max_reasoning_interval_min:
                triggers.append("time_ceiling")

        # 2. Price move: any Tier 1 symbol or position moved >2% since last eval
        watchlist = await self._state_manager.load_watchlist()
        portfolio = await self._state_manager.load_portfolio()
        tracked = {e.symbol for e in watchlist.entries if e.priority_tier == 1}
        tracked |= {p.symbol for p in portfolio.positions}
        indicators = getattr(self, "_latest_indicators", {})
        for symbol in tracked:
            current_price = indicators.get(symbol, {}).get("price")
            if current_price is None:
                continue
            last_price = ts.last_prices.get(symbol)
            if last_price and abs(current_price - last_price) / last_price > 0.02:
                triggers.append(f"price_move:{symbol}")

        # 3. Position approaching target (within 1% of profit target or stop loss)
        for pos in portfolio.positions:
            targets = pos.intention.exit_targets
            current = indicators.get(pos.symbol, {}).get("price")
            if current is None:
                continue
            if targets.profit_target > 0:
                pct_to_target = abs(current - targets.profit_target) / targets.profit_target
                if pct_to_target <= 0.01:
                    triggers.append(f"near_target:{pos.symbol}")
            if targets.stop_loss > 0:
                pct_to_stop = abs(current - targets.stop_loss) / targets.stop_loss
                if pct_to_stop <= 0.01:
                    triggers.append(f"near_stop:{pos.symbol}")

        # 4. Override exit occurred since last Claude call
        if self._override_exit_count > ts.last_override_exit_count:
            triggers.append("override_exit")

        # 5. Market session transition (open at 9:30 ET, approaching close at 3:30 ET)
        current_session = get_current_session()
        last_session = ts.last_session
        if last_session != current_session.value:
            if current_session == Session.REGULAR_HOURS:
                triggers.append("session_open")
            elif current_session == Session.POST_MARKET and last_session == Session.REGULAR_HOURS:
                triggers.append("session_close")
            # Always update last_session regardless of whether we fire
            ts.last_session = current_session.value

        # Also fire ~30 min before close (3:30 PM ET) while still in regular hours
        if current_session == Session.REGULAR_HOURS:
            from datetime import time as _time
            from zoneinfo import ZoneInfo as _ZI
            et = datetime.now(_ZI("America/New_York"))
            if _time(15, 28) <= et.time() <= _time(15, 32):
                # Only fire once in the ~5-min window; use time_ceiling or a flag
                if "session_open" not in triggers and "time_ceiling" not in triggers:
                    triggers.append("approaching_close")

        # 6. Watchlist critically small
        if len(watchlist.entries) < 10:
            triggers.append("watchlist_small")

        return triggers

    async def _update_trigger_prices(self) -> None:
        """Snapshot current prices into last_prices for next trigger comparison."""
        indicators = getattr(self, "_latest_indicators", {})
        for symbol, ind in indicators.items():
            price = ind.get("price")
            if price is not None:
                self._trigger_state.last_prices[symbol] = price

    async def _run_claude_cycle(self, trigger_name: str) -> None:
        """
        Assembles context, calls Claude, processes the response, and
        updates state accordingly. On API failure, enters quantitative-only
        mode with exponential backoff.
        """
        portfolio = await self._state_manager.load_portfolio()
        watchlist = await self._state_manager.load_watchlist()
        orders_state = await self._state_manager.load_orders()
        indicators = getattr(self, "_latest_indicators", {})

        # Build market_data context block
        try:
            acct = await self._broker.get_account()
        except Exception as exc:
            self._mark_broker_failure(exc)
            log.warning("Slow loop: cannot fetch account for Claude context — skipping cycle")
            return
        self._mark_broker_available()

        pdt_remaining = 3 - self._config.risk.pdt_buffer - \
            self._pdt_guard.count_day_trades(orders_state.orders, portfolio)

        market_data = {
            "spy_trend":             "unknown",   # populated if data adapter provides it
            "vix":                   None,
            "sector_rotation":       "unknown",
            "macro_events_today":    [],
            "trading_session":       get_current_session().value,
            "pdt_trades_remaining":  max(0, pdt_remaining),
            "account_equity":        acct.equity,
            "buying_power":          acct.buying_power,
        }

        self._latest_market_context = market_data  # store for medium loop thesis challenge

        log.info(
            "Slow loop: calling Claude reasoning [trigger=%s]  "
            "positions=%d  watchlist=%d  session=%s",
            trigger_name,
            len(portfolio.positions),
            len(watchlist.entries),
            market_data["trading_session"],
        )

        try:
            result = await self._claude.run_reasoning_cycle(
                portfolio=portfolio,
                watchlist=watchlist,
                market_data=market_data,
                indicators=indicators,
                trigger=trigger_name,
                skip_cache=True,   # slow loop always wants a fresh call
            )
        except Exception as exc:
            self._handle_claude_failure(exc)
            return

        if result is None:
            log.warning("Slow loop: Claude returned unparseable response — skipping state update")
            return

        # Claude call succeeded — clear any backoff state
        self._degradation.claude_available = True
        self._degradation.claude_backoff_until_utc = None
        self._trigger_state.last_claude_call_utc = datetime.now(timezone.utc)
        self._trigger_state.last_override_exit_count = self._override_exit_count

        # Snapshot current prices after a successful cycle
        await self._update_trigger_prices()

        # -- Apply watchlist changes ------------------------------------------
        changes = result.watchlist_changes
        add_list    = changes.get("add", [])
        remove_list = changes.get("remove", [])
        if add_list or remove_list:
            await self._apply_watchlist_changes(watchlist, add_list, remove_list)

        # -- Apply position review notes -------------------------------------
        if result.position_reviews:
            await self._apply_position_reviews(portfolio, result.position_reviews)

        log.info(
            "Slow loop: Claude cycle complete — %d new opportunities  "
            "%d watchlist adds  %d removes  %d position reviews",
            len(result.new_opportunities),
            len(add_list),
            len(remove_list),
            len(result.position_reviews),
        )

    def _handle_claude_failure(self, exc: Exception) -> None:
        """Enter quantitative-only mode; schedule exponential backoff retry."""
        now = datetime.now(timezone.utc)
        # Exponential backoff: base 30s, doubles each failure, cap 10 min
        if self._degradation.claude_available:
            self._degradation.claude_available = False
            self._claude_failure_count = 1
        else:
            self._claude_failure_count = getattr(self, "_claude_failure_count", 1) + 1

        backoff_sec = min(30 * (2 ** (self._claude_failure_count - 1)), 600)
        self._degradation.claude_backoff_until_utc = now + timedelta(seconds=backoff_sec)
        log.error(
            "Claude API failure (attempt %d): %s — quantitative-only mode, "
            "retry in %ds",
            self._claude_failure_count, exc, backoff_sec,
        )

    async def _apply_watchlist_changes(
        self,
        watchlist: "WatchlistState",
        add_list: list[dict],
        remove_list: list[str],
    ) -> None:
        """Apply Claude-suggested watchlist additions and removals."""
        now_iso = datetime.now(timezone.utc).isoformat()
        from ozymandias.core.state_manager import WatchlistEntry

        existing_symbols = {e.symbol for e in watchlist.entries}

        for item in add_list:
            # Claude may return plain strings ("SPY") or dicts ({"symbol": "SPY", ...})
            if isinstance(item, str):
                symbol = item.strip().upper()
                reason = "Added by Claude"
                tier = 1
                strategy = "both"
            else:
                symbol = item.get("symbol", "").upper()
                reason = item.get("reason", "Added by Claude")
                tier = item.get("priority_tier", 1)
                strategy = item.get("strategy", "both")
            if not symbol or symbol in existing_symbols:
                continue
            watchlist.entries.append(WatchlistEntry(
                symbol=symbol,
                date_added=now_iso,
                reason=reason,
                priority_tier=tier,
                strategy=strategy,
            ))
            existing_symbols.add(symbol)
            log.info("Watchlist: added %s (tier=%s)", symbol, tier)

        if remove_list:
            before = len(watchlist.entries)
            watchlist.entries = [
                e for e in watchlist.entries if e.symbol not in remove_list
            ]
            removed = before - len(watchlist.entries)
            if removed:
                log.info("Watchlist: removed %d symbol(s): %s", removed, remove_list)

        await self._state_manager.save_watchlist(watchlist)

    async def _apply_position_reviews(
        self,
        portfolio: "PortfolioState",
        reviews: list[dict],
    ) -> None:
        """Append Claude's review notes to each position's intention."""
        now_iso = datetime.now(timezone.utc).isoformat()
        changed = False
        for review in reviews:
            symbol = review.get("symbol", "")
            for pos in portfolio.positions:
                if pos.symbol != symbol:
                    continue
                note = review.get("updated_reasoning") or review.get("notes", "")
                if note:
                    pos.intention.review_notes.append(f"[{now_iso}] {note}")
                    changed = True
                # Apply adjusted targets if provided
                adj = review.get("adjusted_targets") or {}
                if adj.get("profit_target"):
                    pos.intention.exit_targets.profit_target = float(adj["profit_target"])
                    changed = True
                if adj.get("stop_loss"):
                    pos.intention.exit_targets.stop_loss = float(adj["stop_loss"])
                    changed = True
        if changed:
            await self._state_manager.save_portfolio(portfolio)

    # -----------------------------------------------------------------------
    # Degradation helpers
    # -----------------------------------------------------------------------

    def _mark_broker_failure(self, exc: Exception) -> None:
        """Record a broker failure. Enter safe mode after 5 minutes of failures."""
        now = datetime.now(timezone.utc)
        if self._degradation.broker_available:
            log.error("Broker failure: %s — entering degraded mode", exc)
            self._degradation.broker_available = False
            self._degradation.broker_first_failure_utc = now
        else:
            elapsed = (now - self._degradation.broker_first_failure_utc).total_seconds()
            if (
                not self._degradation.safe_mode
                and elapsed >= DegradationState.BROKER_SAFE_MODE_SECONDS
            ):
                self._degradation.safe_mode = True
                log.critical(
                    "SAFE MODE ACTIVATED — broker unreachable for >%.0fs. "
                    "No new orders will be placed.",
                    elapsed,
                )

    def _mark_broker_available(self) -> None:
        if not self._degradation.broker_available:
            log.info("Broker connection restored — leaving degraded mode")
            self._degradation.broker_available = True
            self._degradation.broker_first_failure_utc = None
            # Do NOT automatically clear safe_mode — operator must confirm

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _load_credentials(self) -> tuple[str, str]:
        """
        Read all API credentials from the configured credentials file and
        inject them into the process environment.

        Supports two file formats:
        - Encrypted: Fernet token (detected by b'gAAAAA' prefix). Decrypted
          using the key at config.broker.credentials_key_file.
        - Plaintext: plain JSON. Accepted without decryption for recovery /
          initial setup.

        After parsing, sets ANTHROPIC_API_KEY in os.environ (if present in
        the file and not already set externally) so the Anthropic SDK picks
        it up automatically when ClaudeReasoningEngine initialises its client.

        Returns (alpaca_api_key, alpaca_secret_key).
        """
        import os
        from cryptography.fernet import Fernet, InvalidToken

        creds_path = self._config.credentials_path
        raw = creds_path.read_bytes()

        if raw.lstrip().startswith(b"gAAAAA"):
            # Encrypted — load key from keyfile
            key_path = Path(self._config.broker.credentials_key_file).expanduser()
            if not key_path.exists():
                raise RuntimeError(
                    f"credentials file is encrypted but key file not found: {key_path}\n"
                    f"Generate a key with:  python scripts/encrypt_credentials.py --keygen"
                )
            key = key_path.read_bytes().strip()
            try:
                raw = Fernet(key).decrypt(raw.strip())
            except InvalidToken as exc:
                raise RuntimeError(
                    f"Failed to decrypt credentials — wrong key or corrupted file: {exc}"
                ) from exc
            log.debug("Credentials decrypted from %s using key at %s", creds_path, key_path)
        else:
            log.debug("Credentials loaded as plaintext from %s", creds_path)

        creds = json.loads(raw)

        # Inject Anthropic key into env if present and not already set externally
        anthropic_key = creds.get("anthropic_api_key")
        if anthropic_key and not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = anthropic_key
            log.debug("ANTHROPIC_API_KEY set from credentials file")
        elif os.environ.get("ANTHROPIC_API_KEY"):
            log.debug("ANTHROPIC_API_KEY already set in environment — credentials file value ignored")

        api_key = creds.get("api_key") or creds.get("APCA_API_KEY_ID")
        secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError(
                f"credentials file at {creds_path} must contain "
                f"'api_key' and 'secret_key'"
            )
        return api_key, secret_key

    def _build_strategies(self) -> list[Strategy]:
        """Instantiate the strategies listed in config."""
        strategies: list[Strategy] = []
        active = self._config.strategy.active_strategies
        if "momentum" in active:
            strategies.append(MomentumStrategy(self._config.strategy.momentum_params or {}))
        if "swing" in active:
            strategies.append(SwingStrategy(self._config.strategy.swing_params or {}))
        return strategies
