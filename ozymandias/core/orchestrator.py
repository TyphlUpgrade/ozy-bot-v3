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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ozymandias.core.config import Config, load_config
from ozymandias.core.logger import setup_logging
from ozymandias.core.market_hours import is_market_open
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

        # Count of override exits since last Claude call (feeds trigger state)
        self._override_exit_count: int = 0

        # Shutdown flag — set by _shutdown(), checked by loops
        self._stopping = False

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
            "weight_ai":        self._config.ranker.weight_ai,
            "weight_technical": self._config.ranker.weight_technical,
            "weight_risk":      self._config.ranker.weight_risk,
            "weight_liquidity": self._config.ranker.weight_liquidity,
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
            await self._fast_step_pdt_check()
            return

        # Step 1 & 2: poll + reconcile + handle stale orders
        await self._fast_step_poll_and_reconcile()

        # Step 3: quant overrides
        await self._fast_step_quant_overrides()

        # Step 4: PDT guard check
        await self._fast_step_pdt_check()

        # Step 5: position sync
        await self._fast_step_position_sync()

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

        broker_symbols = {p.symbol for p in broker_positions}
        local_symbols = {p.symbol for p in portfolio.positions}

        # Positions we have locally but broker doesn't know about
        ghost_local = local_symbols - broker_symbols
        if ghost_local:
            log.warning(
                "Position sync discrepancy — local has positions not in broker: %s",
                ghost_local,
            )

        # Positions broker has that we don't track locally
        unknown_broker = broker_symbols - local_symbols
        if unknown_broker:
            log.warning(
                "Position sync discrepancy — broker has positions not tracked locally: %s",
                unknown_broker,
            )

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
        # {symbol: [Signal, ...]}
        entry_signals: dict[str, list] = {}
        for symbol, summary in indicators.items():
            df = bars.get(symbol)
            if df is None:
                continue
            sigs_flat = summary["signals"]
            for strategy in self._strategies:
                try:
                    sigs = await strategy.generate_signals(symbol, df, sigs_flat)
                    if sigs:
                        entry_signals.setdefault(symbol, []).extend(sigs)
                except Exception as exc:
                    log.warning(
                        "Medium loop: generate_signals failed for %s/%s: %s",
                        symbol, type(strategy).__name__, exc,
                    )

        # -- Step 4: re-rank opportunity queue --------------------------------
        # Load the most recent Claude reasoning result from cache.
        # If no cache hit, build an empty ReasoningResult so the ranker can
        # still score the technically-detected signals.
        cached_raw = self._reasoning_cache.load_latest_if_fresh()
        if cached_raw:
            reasoning_result = _result_from_raw_reasoning(cached_raw)
        else:
            reasoning_result = ReasoningResult(
                timestamp=datetime.now(timezone.utc).isoformat(),
                position_reviews=[],
                new_opportunities=[],
                watchlist_changes={"add": [], "remove": [], "rationale": ""},
                market_assessment="unknown",
                risk_flags=[],
                raw={},
            )

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
            "Entry order placed — %s  qty=%d  limit=%.2f  strategy=%s  score=%.3f",
            symbol, quantity, entry_price, strategy_name, top.composite_score,
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
    # Slow loop (stub — implemented in next phase increment)
    # -----------------------------------------------------------------------

    async def _slow_loop(self) -> None:
        while not self._stopping:
            try:
                pass  # TODO: implement slow loop
            except Exception as exc:
                log.error("Slow loop error: %s", exc, exc_info=True)
            await asyncio.sleep(self._config.scheduler.slow_loop_check_sec)

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
        """Read Alpaca API credentials from the configured credentials file."""
        creds_path = self._config.credentials_path
        with open(creds_path, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
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
