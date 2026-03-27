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
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ozymandias.core.config import Config, load_config
from ozymandias.core.direction import EXIT_SIDE, direction_from_action, is_short
from ozymandias.core.logger import setup_logging
from ozymandias.core.market_hours import Session, get_current_session, is_last_five_minutes, is_market_open
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import (
    OrderRecord,
    PortfolioState,
    STATE_DIR,
    StateManager,
    WatchlistState,
)
from ozymandias.core.trade_journal import TradeJournal
from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter
from ozymandias.execution.alpaca_broker import AlpacaBroker
from ozymandias.execution.broker_interface import BrokerInterface, Order
from ozymandias.execution.fill_protection import FillProtectionManager
from ozymandias.execution.pdt_guard import PDTGuard
from ozymandias.execution.risk_manager import RiskManager
from ozymandias.data.adapters.search_adapter import SearchAdapter
from ozymandias.intelligence.claude_reasoning import (
    ClaudeReasoningEngine,
    ReasoningResult,
    _result_from_raw_reasoning,
)
from ozymandias.intelligence.opportunity_ranker import OpportunityRanker, RankResult, evaluate_entry_conditions
from ozymandias.intelligence.universe_scanner import UniverseScanner
from ozymandias.intelligence.technical_analysis import compute_composite_score, generate_daily_signal_summary, generate_signal_summary
from ozymandias.strategies.base_strategy import Strategy, get_strategy

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _rejection_gate_category(reason: str) -> str:
    """Map a ranker rejection-reason string to a short gate-category label.

    Used to build the gate-breakdown summary in the no-opportunity streak WARN.
    To add a new category: add one entry to the lookup before the fallback.
    """
    r = reason.lower()
    if "no_entry_symbols" in r or "market-context" in r:
        return "no_entry_list"
    if "already open in portfolio" in r:
        return "already_open"
    if "conviction" in r:
        return "conviction_floor"
    if "composite_technical_score" in r or "technical score" in r:
        return "technical_score_floor"
    if "rvol" in r or "volume_ratio" in r:
        return "rvol_gate"
    if "rsi" in r:
        return "rsi_gate"
    if "vwap" in r:
        return "vwap_gate"
    if "entry_condition" in r:
        return "entry_conditions"
    if "pdt" in r:
        return "pdt_guard"
    if "market" in r and "open" in r:
        return "market_hours"
    if "dead zone" in r or "dead_zone" in r:
        return "dead_zone"
    if "max_concurrent" in r or "position cap" in r:
        return "position_cap"
    if "deployment" in r:
        return "deployment_cap"
    if "drift" in r:
        return "price_drift"
    if "suppressed" in r:
        return "session_suppressed"
    return "other"

# Market context instruments fetched every medium cycle for Claude macro context.
# Results go into _market_context_indicators only — never into _latest_indicators
# (no entry pipeline contamination).
_CONTEXT_SYMBOLS = [
    "SPY", "QQQ", "IWM",                                # broad market
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLC",  # SPDR sectors
    "ITA",                                               # Aerospace & Defense (iShares)
    "XBI",                                               # Biotechnology (SPDR S&P Biotech)
]

# Sector membership map: symbol → sector ETF tracked in _CONTEXT_SYMBOLS.
# Used by the sector_move trigger to detect portfolio exposure per sector.
# Extension point: to register a new symbol's sector, add one entry here.
# Symbols absent from this map degrade gracefully: their sector ETF fires
# at the base threshold rather than the tightened exposure threshold.
# Note: COIN and similar crypto-adjacent names have no clean ETF mapping
# and are intentionally left unmapped.
_SECTOR_MAP: dict[str, str] = {
    # Energy (XLE)
    "XLE": "XLE", "XOM": "XLE", "CVX": "XLE", "COP": "XLE",
    "EOG": "XLE", "OXY": "XLE", "SLB": "XLE", "HAL": "XLE",
    "BKR": "XLE", "DVN": "XLE", "FANG": "XLE", "MRO": "XLE",
    "MPC": "XLE", "VLO": "XLE", "PSX": "XLE",
    # Financials (XLF)
    "XLF": "XLF", "JPM": "XLF", "BAC": "XLF", "WFC": "XLF",
    "GS": "XLF", "MS": "XLF", "C": "XLF", "AXP": "XLF",
    "BLK": "XLF", "BX": "XLF", "KKR": "XLF", "SCHW": "XLF",
    "IBKR": "XLF", "COF": "XLF", "KRE": "XLF", "MA": "XLF", "V": "XLF",
    # Technology (XLK)
    "XLK": "XLK", "NVDA": "XLK", "AAPL": "XLK", "MSFT": "XLK",
    "AMD": "XLK", "AVGO": "XLK", "QCOM": "XLK", "INTC": "XLK",
    "ARM": "XLK", "MRVL": "XLK", "MU": "XLK", "SMCI": "XLK",
    "AMAT": "XLK", "LRCX": "XLK", "KLAC": "XLK", "ASML": "XLK",
    "SMH": "XLK", "SOXX": "XLK",
    "PLTR": "XLK", "NOW": "XLK", "ORCL": "XLK", "DELL": "XLK",
    "CRWD": "XLK", "PANW": "XLK", "ZS": "XLK", "FTNT": "XLK",
    "NET": "XLK", "DDOG": "XLK", "SNOW": "XLK", "MDB": "XLK",
    # Consumer Discretionary (XLY)
    "XLY": "XLY", "TSLA": "XLY", "AMZN": "XLY", "RIVN": "XLY",
    "NIO": "XLY", "XPEV": "XLY", "LI": "XLY",
    "GM": "XLY", "F": "XLY",
    "HD": "XLY", "LOW": "XLY", "TGT": "XLY", "WMT": "XLY",
    "CMG": "XLY", "MCD": "XLY",
    "BKNG": "XLY", "ABNB": "XLY", "UBER": "XLY", "LYFT": "XLY",
    "DKNG": "XLY", "MGM": "XLY", "LVS": "XLY", "WYNN": "XLY",
    "NKE": "XLY",
    # Healthcare / large pharma (XLV) — managed care + large-cap pharma
    "XLV": "XLV", "UNH": "XLV", "JNJ": "XLV", "MRK": "XLV",
    "PFE": "XLV", "ABBV": "XLV", "BMY": "XLV",
    "LLY": "XLV", "NVO": "XLV",   # GLP-1 names trade on pharma/obesity thesis
    # Biotechnology (XBI) — pure biotech; moves sharply on FDA news and trial readouts
    "XBI": "XBI", "GILD": "XBI", "BIIB": "XBI", "REGN": "XBI",
    "VRTX": "XBI", "MRNA": "XBI", "NVAX": "XBI", "INCY": "XBI",
    "HIMS": "XBI", "RXRX": "XBI", "EXAS": "XBI", "ARKG": "XBI",
    # Industrials (XLI) — broad industrial/machinery names
    "XLI": "XLI", "CAT": "XLI", "DE": "XLI", "HON": "XLI", "ETN": "XLI",
    # Aerospace & Defense (ITA) — defense-specific ETF; more sensitive than XLI
    # to geopolitical events, budget resolutions, and contract awards
    "ITA": "ITA", "LMT": "ITA", "RTX": "ITA", "NOC": "ITA", "GD": "ITA",
    "BA": "ITA", "GE": "ITA", "AXON": "ITA", "RKLB": "ITA", "KTOS": "ITA",
    "HII": "ITA", "L3H": "ITA",
    # Communications (XLC)
    "XLC": "XLC", "META": "XLC", "GOOGL": "XLC", "GOOG": "XLC",
    "NFLX": "XLC", "DIS": "XLC", "SPOT": "XLC", "ROKU": "XLC",
    "RBLX": "XLC", "EA": "XLC", "TTWO": "XLC",
}

# Sector ETFs tracked for sector_move triggers — subset of _CONTEXT_SYMBOLS
# excluding the three broad-market indices (handled by market_move triggers).
# Extension point: to add a new sector, add its ETF to _CONTEXT_SYMBOLS and here.
_CONTEXT_SECTOR_ETFS: list[str] = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLC", "ITA", "XBI"]


# ---------------------------------------------------------------------------
# Emergency signal files
# ---------------------------------------------------------------------------
# Create either file to trigger the corresponding action on the next fast-loop
# tick (~5-15 seconds). Both files are deleted immediately after processing.
# Designed for Discord integration, CLI scripts, or manual operator intervention.
#
#   touch ozymandias/state/EMERGENCY_EXIT      → liquidate all positions, bot keeps running
#   touch ozymandias/state/EMERGENCY_SHUTDOWN  → graceful bot shutdown
EMERGENCY_EXIT_SIGNAL     = STATE_DIR / "EMERGENCY_EXIT"
EMERGENCY_SHUTDOWN_SIGNAL = STATE_DIR / "EMERGENCY_SHUTDOWN"


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
    # Fired once after the first medium loop cycle populates indicators
    indicators_seeded: bool = False
    # Unrealised gain pct (as a fraction) at the time of the last profit trigger per symbol.
    # Trigger fires when gain_pct >= last value + position_profit_trigger_pct.
    # Updated after each successful Claude call; cleared when a position closes.
    last_profit_trigger_gain: dict[str, float] = field(default_factory=dict)
    # monotonic timestamp of the last near_target Claude call per symbol.
    # Suppresses repeated firing while price stays within the target zone — only
    # re-fires after near_target_cooldown_sec elapses since last handled review.
    # Cleared when a position closes.
    last_near_target_time: dict[str, float] = field(default_factory=dict)
    # monotonic timestamp of the last near_stop Claude call per symbol.
    # Mirrors near_target_time — same cooldown (near_target_cooldown_sec) prevents
    # repeated firing while price oscillates within 1% of the stop level.
    # Without this, near_stop fires every slow-loop tick (~60s) indefinitely.
    # Cleared when a position closes.
    last_near_stop_time: dict[str, float] = field(default_factory=dict)
    # Phase 17: price baseline anchored to the last successful Claude call (not reset each cycle).
    # Used by macro_move and sector_move triggers so that sustained index/sector moves always fire
    # even when last_prices is updated each tick. Separate from last_prices which resets per eval.
    last_claude_call_prices: dict[str, float] = field(default_factory=dict)
    # Phase 17: RSI extreme trigger re-arm tracking.
    # Once fired, the trigger cannot re-fire until RSI recovers by macro_rsi_rearm_band points.
    rsi_extreme_fired_low: bool = False    # True after panic trigger fires; cleared when RSI recovers above threshold + band
    rsi_extreme_fired_high: bool = False   # True after euphoria trigger fires; cleared when RSI falls below threshold - band
    # watchlist_stale trigger: UTC timestamp of the last completed watchlist build.
    # None at startup → elapsed = ∞ → watchlist_stale fires on the first slow loop tick.
    # Reset after every successful build (both watchlist_small and watchlist_stale paths).
    last_watchlist_build_utc: Optional[datetime] = None
    # Set by the medium loop when every symbol from the current reasoning cache has been
    # suppressed by the hard-filter. Cleared by _check_triggers after firing once per
    # cache generation (guarded by last_exhaustion_trigger_utc < last_claude_call_utc).
    # To add another exhaustion-style trigger: follow this same flag+timestamp pattern.
    candidates_exhausted: bool = False
    last_exhaustion_trigger_utc: Optional[datetime] = None


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
        # Derive journal path from state manager so both always point at the same
        # directory. This matters in tests: redirecting _state_manager._dir must be
        # accompanied by an equivalent redirect of _trade_journal._path, and having
        # the path derived here makes the relationship explicit.
        self._trade_journal = TradeJournal(
            path=self._state_manager._dir / "trade_journal.jsonl"
        )
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
        self._strategy_lookup: dict[str, Strategy] = {}  # name → instance, built in _startup

        # -- Runtime state ---------------------------------------------------
        self._degradation = DegradationState()
        self._trigger_state = SlowLoopTriggerState()

        # Intraday highs per symbol — maintained by the fast loop for the
        # ATR trailing stop check.
        self._intraday_highs: dict[str, float] = {}
        self._intraday_lows: dict[str, float] = {}   # tracks minimum price seen each session for short ATR trailing stops

        # Monotonic timestamp of each position's opening fill — used to enforce
        # a minimum hold time before quant overrides can fire (prevents the override
        # from triggering on stale indicators the moment a position is registered).
        self._position_entry_times: dict[str, float] = {}

        # Latest market context from slow loop — consumed by thesis challenge in medium loop
        self._latest_market_context: dict = {}

        # TA results for watchlist + position symbols.
        # Updated each medium cycle; consumed by fast loop quant overrides and ranker.
        # Initialized empty; populated after first medium loop cycle.
        self._latest_indicators: dict = {}

        # TA results for context instruments (SPY, QQQ, sector ETFs).
        # Updated each medium cycle; consumed by _build_market_context for Claude calls.
        self._market_context_indicators: dict = {}

        # Phase 17: merged dict of _latest_indicators + _market_context_indicators.
        # Set once at the end of each medium loop cycle. Consumed by:
        #   - _check_triggers (macro/sector move baseline)
        #   - Phase 19 ContextCompressor (_build_all_candidates)
        # Initialized empty; populated after first medium loop cycle completes.
        self._all_indicators: dict = {}

        # Daily-bar TA signals for swing position reviews and macro regime context.
        # Fetched each slow loop before run_reasoning_cycle; keyed by symbol.
        # Subset: open swing positions + SPY + QQQ. Updated in-place on each slow
        # loop pass; stale entries for closed positions are harmless (small dict).
        self._daily_indicators: dict[str, dict] = {}

        # Phase 17: UTC timestamp of the last completed medium loop cycle.
        # Used by the medium-loop gate in _slow_loop_cycle to prevent Claude from
        # firing on stale indicators (Fix 3). None until the first cycle completes.
        self._last_medium_loop_completed_utc: Optional[datetime] = None

        # Count of override exits since last Claude call (feeds trigger state)
        self._override_exit_count: int = 0

        # Consecutive Claude failure count — used for exponential backoff
        self._claude_failure_count: int = 0

        # Pending entry intentions: symbol → {stop, target, strategy, reasoning, ...}.
        # Written by _medium_try_entry when an order is placed; consumed by
        # _fast_step_position_sync when a new broker position is discovered.
        self._pending_intentions: dict[str, dict] = {}

        # Entry contexts: symbol → {signals, claude_conviction, composite_score}.
        # Populated from _pending_intentions when a buy fill is detected; consumed
        # by _journal_closed_trade when the position is later closed.
        self._entry_contexts: dict[str, dict] = {}

        # Exit reason hints: symbol → reason string.
        # Set by override/short-exit/EOD paths when placing exit orders; consumed
        # by _journal_closed_trade (via _dispatch_confirmed_fill) on fill detection.
        # Ensures override and short-protection exits are journaled with accurate
        # exit_reason instead of the price-inferred "strategy" fallback.
        self._pending_exit_hints: dict[str, str] = {}

        # Symbols traded (entered-and-exited) within the current Claude reasoning
        # cycle. Blocks the medium loop from re-entering the same symbol on a stale
        # recommendation after an exit — prevents chasing the same thesis twice
        # without fresh reasoning (the INTC double-loss pattern from 2026-03-19).
        # Cleared each time a new Claude slow-loop call succeeds.
        # To add a new guard: just add the symbol here; it clears automatically.
        self._cycle_consumed_symbols: set[str] = set()

        # Consecutive entry-condition defer counts per symbol.
        # Incremented each medium cycle entry_conditions are unmet; the opportunity
        # is dropped (gate cleared) when count reaches max_entry_defer_cycles.
        # Cleared with _cycle_consumed_symbols on each successful Claude call.
        self._entry_defer_counts: dict[str, int] = {}

        # Consecutive medium-loop cycles where the ranker returned zero candidates.
        # Used to emit a gate-breakdown WARN (Finding 4) so the operator can diagnose
        # whether a watchlist or a specific gate is the bottleneck. Reset when ranked
        # candidates appear or a fresh Claude reasoning cycle fires.
        self._no_opportunity_streak: int = 0

        # Phase 15: unified recommendation outcome tracker.
        # symbol → {claude_entry_target, attempt_time_utc, stage, stage_detail,
        #            rejection_count, order_id}
        # Populated by the ranker hard-filter path, entry-conditions gate, order
        # placement, fill detection, and cancel detection.
        # Purged daily (stale entries removed at slow-loop cycle start).
        # In-memory only — no state file persistence; cleared on restart (acceptable).
        self._recommendation_outcomes: dict[str, dict] = {}

        # Consecutive Claude soft-rejection tracker.
        # symbol → number of consecutive cycles Claude placed it in rejected_opportunities.
        # Reset to 0 when Claude enters the symbol or it is absent from the reasoning window.
        # Never cleared on restart (session-scoped, so restarts are acceptable).
        # To add a new tracking dimension: add a parallel dict here and update
        # _run_claude_cycle after run_reasoning_cycle returns.
        self._claude_soft_rejections: dict[str, int] = {}

        # Symbols suppressed for this session after repeatedly failing hard filters.
        # Maps symbol → rejection reason. Populated by the medium loop when a symbol
        # reaches max_filter_rejection_cycles consecutive ranker rejections. Cleared
        # on restart (session-scoped). Passed to Claude context so it stops nominating
        # them, and checked in the medium loop to skip them before ranking.
        # To add a new suppression reason: increment rejection_count in
        # _recommendation_outcomes and let the threshold check here handle suppression.
        self._filter_suppressed: dict[str, str] = {}

        # Phase 18: Universe scanner + search adapter (instantiated in _startup)
        self._universe_scanner: Optional[UniverseScanner] = None
        self._search_adapter: Optional[SearchAdapter] = None
        # Session cache for universe scan results — avoids re-scanning on every
        # watchlist_small trigger within the same session.
        self._last_universe_scan: list[dict] = []
        self._last_universe_scan_time: float = 0.0

        # Shutdown flag — set by _shutdown(), checked by loops
        self._stopping = False

        # Dry-run mode — if True, orders are logged but never submitted
        self._dry_run: bool = dry_run

        # Conservative startup mode — no new entries until this UTC timestamp
        self._conservative_mode_until: Optional[datetime] = None

        # Thesis challenge result cache: symbol → (concern_level, monotonic_timestamp).
        # Prevents hammering Claude with repeated challenges on the same symbol every cycle.
        self._thesis_challenge_cache: dict[str, tuple[float, float]] = {}

        # Monotonic timestamp of the last PDT warning log — suppresses repeated warnings
        # that would otherwise fire every fast loop tick (every 10s) when near the limit.
        self._last_pdt_warning_ts: float = 0.0

        # Last-known account equity, updated after every broker account fetch.
        # Used by _fast_step_pdt_check to skip PDT limits when above the $25k threshold.
        self._last_known_equity: float = 0.0

        # Recently-closed symbols: symbol → monotonic timestamp of closure.
        # Prevents _fast_step_position_sync from re-adopting a position the bot just
        # closed (which would cause a runaway loop of repeated exit orders).
        # Values are time.monotonic() timestamps of when the position was closed.
        #
        # PHASE 14 NOTE — persistence reload math:
        # When reloading _recently_closed from disk (UTC timestamps), convert each entry as:
        #   elapsed = (datetime.now(timezone.utc) - stored_utc_ts).total_seconds()
        #   self._recently_closed[symbol] = time.monotonic() - elapsed
        # Do NOT set time.monotonic() directly — that restarts the full guard window
        # from now, rather than expiring it relative to the actual close time.
        # A position closed 58 seconds before restart should expire in ~2 seconds, not 60.
        self._recently_closed: dict[str, float] = {}

        # Tracks consecutive medium-loop fetch failures per watchlist symbol.
        # When a symbol reaches fetch_failure_removal_threshold failures without
        # a successful fetch in between, it is automatically removed from the watchlist.
        # Prevents delisted or stale symbols from generating persistent yfinance errors.
        self._fetch_failure_counts: dict[str, int] = {}

        # Tracks when each symbol was last exited via quant override (hard stop or signal).
        # Enforces a longer re-entry cooldown than the normal recently_closed guard,
        # because a quant override indicates the momentum signal broke down — the setup
        # needs more time to reset before re-entry is appropriate.
        # Values are time.monotonic() timestamps of the override exit.
        self._override_closed: dict[str, float] = {}

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
        try:
            api_key, secret_key = self._load_credentials()
        except Exception as exc:
            log.critical(
                "STARTUP FAILED — cannot load credentials: %s\n"
                "Check that the credentials file exists and the key file is correct.",
                exc,
            )
            raise

        paper = self._config.broker.environment == "paper"
        self._broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)

        try:
            acct = await self._broker.get_account()
        except Exception as exc:
            log.critical(
                "STARTUP FAILED — broker connection rejected: %s\n"
                "Check that your Alpaca API key and secret are valid and match the "
                "environment (%s).",
                exc,
                "paper" if paper else "live",
            )
            raise

        self._last_known_equity = acct.equity
        log.info(
            "Broker connected [%s] — equity=$%.2f  buying_power=$%.2f  "
            "cash=$%.2f  pdt=%s  daytrades_used=%d",
            "paper" if paper else "live",
            acct.equity, acct.buying_power, acct.cash,
            acct.pdt_flag, acct.daytrade_count,
        )

        try:
            hours = await self._broker.get_market_hours()
        except Exception as exc:
            log.critical("STARTUP FAILED — could not fetch market hours: %s", exc)
            raise
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
        # Seed broker_floor from startup account fetch so the bot never starts
        # a session thinking it has more day trades remaining than the broker knows.
        self._pdt_guard.broker_floor = acct.daytrade_count
        self._risk_manager = RiskManager(
            self._config.risk, self._pdt_guard, self._config.scheduler
        )

        # -- Market data adapter ----------------------------------------------
        self._data_adapter = YFinanceAdapter(
            bars_ttl=self._config.scheduler.bars_cache_ttl_sec,
            fetch_stagger_max_sec=self._config.scheduler.yfinance_fetch_stagger_max_sec,
        )

        # -- Claude reasoning engine ------------------------------------------
        self._claude = ClaudeReasoningEngine(
            config=self._config,
            cache=self._reasoning_cache,
        )

        # -- Universe scanner + search adapter (Phase 18) ---------------------
        self._universe_scanner = UniverseScanner(
            data_adapter=self._data_adapter,
            config=self._config.universe_scanner,
            no_entry_symbols=list(self._config.ranker.no_entry_symbols),
        )
        self._search_adapter = SearchAdapter(
            api_key=os.environ.get("BRAVE_SEARCH_API_KEY"),
            retry_count=self._config.search.search_429_retry_count,
            retry_sec=self._config.search.search_429_retry_sec,
        )

        # -- Opportunity ranker -----------------------------------------------
        ranker_cfg = {
            "weight_ai":                   self._config.ranker.weight_ai,
            "weight_technical":            self._config.ranker.weight_technical,
            "weight_risk":                 self._config.ranker.weight_risk,
            "weight_liquidity":            self._config.ranker.weight_liquidity,
            "min_conviction_threshold":    self._config.ranker.min_conviction_threshold,
            "thesis_challenge_size_threshold": self._config.ranker.thesis_challenge_size_threshold,
            "min_technical_score":         self._config.ranker.min_technical_score,
            "ta_size_factor_min":          self._config.ranker.ta_size_factor_min,
            # Position count cap sourced from risk config (single source of truth).
            "max_positions":               self._config.risk.max_concurrent_positions,
            # Deployment cap: blocks new entries when too much equity is already deployed.
            "max_portfolio_deployment_pct": self._config.ranker.max_portfolio_deployment_pct,
            # Strategy-specific gate thresholds are now owned by each Strategy class
            # via _DEFAULT_PARAMS and apply_entry_gate().  No ranker config needed.
        }
        self._ranker = OpportunityRanker(config=ranker_cfg)

        # -- Strategies -------------------------------------------------------
        self._strategy_lookup = self._build_strategies()
        self._strategies = list(self._strategy_lookup.values())
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
                # Broker reports negative qty for shorts; shares always stored positive.
                broker_abs_qty = abs(broker_pos.qty)
                if symbol in local_map:
                    local_pos = local_map[symbol]
                    if abs(local_pos.shares - broker_abs_qty) > 0.001:
                        log.error(
                            "Position mismatch: %s local=%.4f broker=%.4f — "
                            "updating local to broker (broker is source of truth)",
                            symbol, local_pos.shares, broker_abs_qty,
                        )
                        local_pos.shares = broker_abs_qty
                        reconciliation_errors = True
                        updated = True
                else:
                    # Broker has a position we don't know about (e.g. carried over
                    # from a prior session). Adopt it — this is fully handled: hold-time
                    # is set, overrides are gated, and the medium loop will manage it.
                    # Does NOT trigger conservative mode; there is no uncertainty here.
                    log.warning(
                        "Unknown broker position: %s %.4f shares @ %.4f — "
                        "adopting into local state (direction=%s)",
                        symbol, broker_abs_qty, broker_pos.avg_entry_price,
                        "short" if broker_pos.side in ("short", "sell") else "long",
                    )
                    now_iso = datetime.now(timezone.utc).isoformat()
                    reconciled_direction = (
                        "short" if broker_pos.side in ("short", "sell") else "long"
                    )
                    portfolio.positions.append(Position(
                        symbol=symbol,
                        shares=broker_abs_qty,
                        avg_cost=broker_pos.avg_entry_price,
                        entry_date=now_iso,
                        intention=TradeIntention(direction=reconciled_direction),
                        reconciled=True,
                    ))
                    # Give reconciled positions the hold-time window so overrides
                    # cannot fire on stale indicators immediately after adoption.
                    self._position_entry_times[symbol] = time.monotonic()
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
            self._last_known_equity = acct.equity
            self._risk_manager.initialize_daily_tracking(acct)
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
            # Suppress no_previous_call trigger — we already have recent reasoning.
            try:
                ts = datetime.fromisoformat(fresh["timestamp"])
                self._trigger_state.last_claude_call_utc = ts
            except Exception:
                pass  # malformed timestamp — let trigger fire naturally
        else:
            log.info("Step 4 — no fresh cache — Claude will be called on first trigger")

        # Restore last_watchlist_build_utc from the watchlist file's last_updated timestamp
        # so that watchlist_stale respects the real build time across restarts.
        # Without this, every restart fires watchlist_stale immediately regardless of how
        # recently the build ran — causing the full rebuild+prune cycle on every startup.
        # Skipped when watchlist_rebuild_on_restart=True — in that case we leave
        # last_watchlist_build_utc as None so watchlist_stale fires immediately as before.
        if self._config.scheduler.watchlist_rebuild_on_restart:
            log.info("Step 4b — watchlist_rebuild_on_restart=True, skipping timestamp restore")
        else:
            try:
                _wl = await self._state_manager.load_watchlist()
                if _wl.last_updated:
                    _wl_ts = datetime.fromisoformat(_wl.last_updated)
                    self._trigger_state.last_watchlist_build_utc = _wl_ts
                    log.info(
                        "Step 4b — watchlist build timestamp restored: %s (%.0f min ago)",
                        _wl_ts.isoformat(),
                        (datetime.now(timezone.utc) - _wl_ts).total_seconds() / 60,
                    )
            except Exception as _exc:
                log.debug("Step 4b — could not restore watchlist build timestamp: %s", _exc)

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

        # Repopulate _entry_contexts from TradeIntention fields on open positions.
        # These were written when each fill was first registered. Without this,
        # any position that was open when the bot last stopped loses its signal
        # context and the trade journal records zeros for signals/conviction/score.
        portfolio = await self._state_manager.load_portfolio()
        restored = 0
        for pos in portfolio.positions:
            if pos.intention.entry_signals or pos.intention.entry_conviction:
                self._entry_contexts[pos.symbol] = {
                    "trade_id": str(uuid.uuid4()),
                    "signals": pos.intention.entry_signals,
                    "claude_conviction": pos.intention.entry_conviction,
                    "composite_score": pos.intention.entry_score,
                }
                restored += 1
        if restored:
            log.info("Startup: restored entry context for %d open position(s)", restored)

        # Reload recently_closed guard from persistent state.
        # Only entries younger than 60 seconds are reloaded — older entries have
        # already expired so there is no point reinstating the cooldown.
        # Monotonic timestamps are reconstructed from the elapsed time since close:
        #   self._recently_closed[sym] = time.monotonic() - elapsed_seconds
        # This means the guard expires at the same real-world time regardless of restart.
        now_utc = datetime.now(timezone.utc)
        reloaded = 0
        for sym, close_iso in portfolio.recently_closed.items():
            try:
                close_utc = datetime.fromisoformat(close_iso)
                elapsed = (now_utc - close_utc).total_seconds()
                if elapsed < 60.0:
                    self._recently_closed[sym] = time.monotonic() - elapsed
                    reloaded += 1
                    log.debug(
                        "Startup: reloaded recently_closed guard for %s (%.0fs elapsed)", sym, elapsed
                    )
            except Exception as exc:
                log.debug("Startup: could not parse recently_closed entry %s: %s", sym, exc)
        if reloaded:
            log.info("Startup: reloaded recently_closed guard for %d symbol(s)", reloaded)

        # Reload recommendation_outcomes from the previous session within the same day.
        # Rejection counts must survive restarts so _filter_suppressed can accumulate
        # correctly across bot restarts (e.g. 2 rejections in session 1, 1 more in
        # session 2 → suppressed, rather than resetting to 0 on each restart).
        today_utc_date = datetime.now(timezone.utc).date()
        reloaded_outcomes = 0
        for sym, rec in portfolio.recommendation_outcomes.items():
            ts = rec.get("attempt_time_utc")
            if not ts:
                continue
            try:
                entry_date = datetime.fromisoformat(ts).date()
            except Exception:
                continue
            if entry_date == today_utc_date:
                self._recommendation_outcomes[sym] = rec
                reloaded_outcomes += 1
        if reloaded_outcomes:
            log.info(
                "Startup: reloaded %d recommendation outcome(s) from previous session",
                reloaded_outcomes,
            )

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

        Logging must be configured by the caller (main.py) before run() is
        invoked. setup_logging() is NOT called here to avoid creating a second
        orphaned session log file when launched via the normal entry point.
        """
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

    async def _emergency_exit_all(self) -> None:
        """Aggressively liquidate every open position as fast as possible.

        Called when EMERGENCY_EXIT signal file is detected. Bot continues
        running after liquidation so normal monitoring resumes.

        Three-phase approach:
        1. Cancel all pending orders — clears any blocking limit orders so
           market exits can be placed without interference.
        2. Place market exits for every local position.
        3. Poll broker every 2 seconds for up to 60 seconds to verify
           positions actually close. Logs CRITICAL for any that don't,
           so the operator knows manual intervention is needed.
        """
        portfolio = await self._state_manager.load_portfolio()
        if not portfolio.positions:
            log.warning("EMERGENCY EXIT: no open positions to close")
            return

        symbols = [p.symbol for p in portfolio.positions]
        log.critical(
            "EMERGENCY EXIT: liquidating %d position(s) — %s",
            len(portfolio.positions), symbols,
        )

        # -- Phase 1: cancel all pending orders --------------------------------
        # Any open limit (buy or sell) could block the market exit or leave the
        # account in an unexpected state. Cancel everything unconditionally.
        pending = self._fill_protection.get_pending_orders()
        if pending:
            log.critical(
                "EMERGENCY EXIT: cancelling %d pending order(s) before placing exits",
                len(pending),
            )
        for order in pending:
            try:
                cancel_result = await self._broker.cancel_order(order.order_id)
                await self._fill_protection.handle_cancel_result(
                    order.order_id, cancel_result
                )
                log.critical(
                    "EMERGENCY EXIT: cancelled order %s for %s (success=%s)",
                    order.order_id, order.symbol, cancel_result.success,
                )
            except Exception as exc:
                log.error(
                    "EMERGENCY EXIT: failed to cancel order %s for %s: %s",
                    order.order_id, order.symbol, exc,
                )

        # -- Phase 2: place market exits for every position --------------------
        now_iso = datetime.now(timezone.utc).isoformat()
        exits_placed: list[str] = []
        for pos in portfolio.positions:
            symbol = pos.symbol
            exit_side = EXIT_SIDE[pos.intention.direction]
            exit_order = Order(
                symbol=symbol,
                side=exit_side,
                quantity=pos.shares,
                order_type="market",
                time_in_force="day",
            )
            try:
                result = await self._broker.place_order(exit_order)
                order_record = OrderRecord(
                    order_id=result.order_id,
                    symbol=symbol,
                    side=exit_side,
                    quantity=pos.shares,
                    order_type="market",
                    limit_price=None,
                    status="PENDING",
                    created_at=now_iso,
                    last_checked_at=now_iso,
                )
                await self._fill_protection.record_order(order_record)
                self._pending_exit_hints[symbol] = "emergency_exit"
                exits_placed.append(symbol)
                log.critical(
                    "EMERGENCY EXIT: market order placed — %s  qty=%.2f  order_id=%s",
                    symbol, pos.shares, result.order_id,
                )
            except Exception as exc:
                log.error(
                    "EMERGENCY EXIT: failed to place exit for %s: %s", symbol, exc
                )

        if not exits_placed:
            log.critical("EMERGENCY EXIT: no exit orders placed — manual action required")
            return

        # -- Phase 3: poll broker until all positions confirm closed -----------
        # Market orders on liquid equities fill in milliseconds, but we verify
        # for up to 60 seconds in case of degraded broker connectivity.
        poll_interval_sec = 2
        poll_timeout_sec = 60
        deadline = time.monotonic() + poll_timeout_sec
        remaining = set(exits_placed)

        log.critical(
            "EMERGENCY EXIT: monitoring fills for %s (timeout=%ds)",
            list(remaining), poll_timeout_sec,
        )
        while remaining and time.monotonic() < deadline:
            await asyncio.sleep(poll_interval_sec)
            try:
                broker_positions = await self._broker.get_positions()
                open_symbols = {bp.symbol for bp in broker_positions}
                newly_filled = remaining - open_symbols
                for sym in newly_filled:
                    log.critical("EMERGENCY EXIT: confirmed closed — %s", sym)
                remaining -= newly_filled
            except Exception as exc:
                log.error("EMERGENCY EXIT: broker poll error: %s", exc)

        if remaining:
            log.critical(
                "EMERGENCY EXIT INCOMPLETE: %d position(s) still open after %ds — "
                "MANUAL ACTION REQUIRED: %s",
                len(remaining), poll_timeout_sec, list(remaining),
            )

    async def _check_emergency_signals(self) -> None:
        """Check for operator signal files and act on them.

        Called at the top of every fast-loop iteration so it fires regardless
        of market hours. Signal files are deleted immediately after detection
        so a single touch triggers exactly one action.
        """
        if EMERGENCY_SHUTDOWN_SIGNAL.exists():
            log.critical(
                "EMERGENCY SHUTDOWN signal detected — initiating graceful shutdown"
            )
            try:
                EMERGENCY_SHUTDOWN_SIGNAL.unlink()
            except OSError:
                pass
            await self._shutdown()
            return

        if EMERGENCY_EXIT_SIGNAL.exists():
            log.critical("EMERGENCY EXIT signal detected — liquidating all positions")
            try:
                EMERGENCY_EXIT_SIGNAL.unlink()
            except OSError:
                pass
            try:
                await self._emergency_exit_all()
            except Exception as exc:
                log.error("EMERGENCY EXIT failed: %s", exc, exc_info=True)

    async def _fast_loop(self) -> None:
        """
        Fast loop wrapper — runs _fast_loop_cycle() on every tick.
        Never raises; errors are logged and the loop continues.
        """
        while not self._stopping:
            try:
                await self._check_emergency_signals()
            except Exception as exc:
                log.error("Emergency signal check error: %s", exc, exc_info=True)
            if self._stopping:
                break
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
        if not self._is_market_open():
            return  # no action outside regular hours — resumes cleanly at 9:30

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
                "Order state change: %s %s → %s (type=%s fill_qty=%.2f fill_price=%.4f)",
                change.symbol, change.old_status, change.new_status,
                change.change_type, change.fill_qty, change.fill_price,
            )
            if change.change_type == "fill":
                await self._dispatch_confirmed_fill(change)
            elif change.change_type in ("cancel", "partial_then_cancel", "reject"):
                # Phase 15: mark cancelled/rejected entries in recommendation outcomes.
                if change.symbol in self._recommendation_outcomes:
                    self._recommendation_outcomes[change.symbol]["stage"] = "cancelled"

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
                        if change.change_type == "fill":
                            await self._dispatch_confirmed_fill(change)
                        elif change.change_type in ("cancel", "partial_then_cancel", "reject"):
                            # Phase 15: mark cancelled in recommendation outcomes.
                            if change.symbol in self._recommendation_outcomes:
                                self._recommendation_outcomes[change.symbol]["stage"] = "cancelled"
                except Exception as exc:
                    log.warning("Failed to poll order %s: %s", order.order_id, exc)

        # Handle stale orders — configurable timeout (default 5 min)
        stale_timeout = self._config.scheduler.limit_order_timeout_sec
        stale = self._fill_protection.get_stale_orders(timeout_sec=stale_timeout)
        for stale_order in stale:
            try:
                age_sec = int((datetime.now(timezone.utc) - datetime.fromisoformat(stale_order.created_at)).total_seconds())
            except Exception:
                age_sec = -1
            log.warning(
                "Cancelling stale order %s for %s (type=%s age=%ds timeout=%ds)",
                stale_order.order_id, stale_order.symbol,
                stale_order.order_type, age_sec, stale_timeout,
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

    async def _dispatch_confirmed_fill(self, change) -> None:
        """Route a confirmed fill to the correct handler.

        Uses portfolio state to determine intent:
        - Symbol already has a local position → this is a closing fill → journal it
        - No local position → this is an opening fill → register the new position

        This is correct for all four cases: long open (buy), long close (sell),
        short open (sell), short close (buy). Using change.side alone is wrong
        because sell can mean either short-open or long-close.
        """
        try:
            portfolio = await self._state_manager.load_portfolio()
        except Exception as exc:
            log.error("_dispatch_confirmed_fill: failed to load portfolio for %s: %s", change.symbol, exc, exc_info=True)
            return
        has_position = any(p.symbol == change.symbol for p in portfolio.positions)
        if has_position:
            hint = self._pending_exit_hints.pop(change.symbol, None)
            await self._journal_closed_trade(change, exit_reason_hint=hint)
        else:
            await self._register_opening_fill(change)
            # Phase 15: mark recommendation as filled on confirmed opening fill.
            if change.symbol in self._recommendation_outcomes:
                self._recommendation_outcomes[change.symbol]["stage"] = "filled"

    async def _register_opening_fill(self, change) -> None:
        """Create a local portfolio position when an opening fill is confirmed.

        Called from _dispatch_confirmed_fill for both long opens (buy fill) and
        short opens (sell fill). Positions are created at the moment of confirmed
        fill — with the correct quantity, avg fill price, and intention — rather
        than speculatively from get_positions() which can race against partial fills.

        Shares are always stored as a positive number; direction="short" conveys
        the sign. This matches how exit order quantity= fields are used throughout.
        """
        from ozymandias.core.state_manager import ExitTargets, Position, TradeIntention

        symbol = change.symbol
        pending = self._pending_intentions.pop(symbol, {})

        # Move signal context into entry_contexts for use when the position closes.
        # Also generate a stable trade_id here so all journal records for this
        # trade (open, snapshot, review, close) can be correlated by trade_id.
        if pending:
            self._entry_contexts[symbol] = {
                "trade_id": str(uuid.uuid4()),
                "signals": pending.pop("_signals", {}),
                "claude_conviction": pending.pop("_claude_conviction", 0.0),
                "composite_score": pending.pop("_composite_score", 0.0),
                "position_size_pct": pending.pop("_position_size_pct", 0.0),
            }

        try:
            portfolio = await self._state_manager.load_portfolio()
        except Exception as exc:
            log.error("_register_opening_fill: failed to load portfolio for %s: %s", symbol, exc, exc_info=True)
            return

        # Guard: if position already exists (e.g. duplicate fill event), skip
        if any(p.symbol == symbol for p in portfolio.positions):
            log.debug("_register_opening_fill: position for %s already exists — skipping duplicate", symbol)
            return

        intention = TradeIntention(
            strategy=pending.get("strategy", "unknown"),
            direction=pending.get("direction", "long"),
            reasoning=pending.get("reasoning", ""),
            exit_targets=ExitTargets(
                stop_loss=pending.get("stop", 0.0),
                profit_target=pending.get("target", 0.0),
            ),
            # Persist signal context in portfolio.json so it survives bot restarts.
            # _entry_contexts (in-memory) is populated from these fields at startup.
            entry_signals=self._entry_contexts.get(symbol, {}).get("signals", {}),
            entry_conviction=self._entry_contexts.get(symbol, {}).get("claude_conviction", 0.0),
            entry_score=self._entry_contexts.get(symbol, {}).get("composite_score", 0.0),
        )
        entry_date = datetime.now(timezone.utc).isoformat()
        portfolio.positions.append(Position(
            symbol=symbol,
            shares=change.fill_qty,  # always positive; direction field carries the sign
            avg_cost=change.fill_price if change.fill_price > 0 else 0.0,
            entry_date=entry_date,
            intention=intention,
        ))
        await self._state_manager.save_portfolio(portfolio)
        self._position_entry_times[symbol] = time.monotonic()
        log.info(
            "Position registered from opening fill: %s  qty=%.2f  avg_cost=%.4f  "
            "stop=%.4f  target=%.4f  strategy=%s  direction=%s",
            symbol, change.fill_qty, change.fill_price,
            pending.get("stop", 0.0), pending.get("target", 0.0),
            pending.get("strategy", "unknown"), pending.get("direction", "long"),
        )

        # Write the "open" journal record. Omitted for broker-adopted positions
        # (pending is empty) because we lack entry context in that case.
        trade_id = self._entry_contexts.get(symbol, {}).get("trade_id")
        if trade_id:
            ctx = self._entry_contexts[symbol]
            await self._trade_journal.append({
                "record_type": "open",
                "trade_id": trade_id,
                "symbol": symbol,
                "strategy": pending.get("strategy", "unknown"),
                "direction": pending.get("direction", "long"),
                "entry_time": entry_date,
                "entry_price": change.fill_price if change.fill_price > 0 else 0.0,
                "shares": change.fill_qty,
                "stop_price": pending.get("stop", 0.0),
                "target_price": pending.get("target", 0.0),
                "signals_at_entry": ctx.get("signals", {}),
                "claude_conviction": ctx.get("claude_conviction", 0.0),
                "composite_score": ctx.get("composite_score", 0.0),
                "position_size_pct": ctx.get("position_size_pct", 0.0),
                "source": "live",
                "prompt_version": self._config.claude.prompt_version,
                "bot_version": self._config.claude.model,
            })

    async def _journal_closed_trade(self, change, exit_reason_hint: str | None = None) -> None:
        """Write a trade journal entry and remove the position from the portfolio.

        Called for any closing fill: sell fill (long close) or buy fill (short close).
        Captures: entry data from portfolio, exit price from broker fill, signal
        context from _entry_contexts (populated when the opening fill arrived).

        exit_reason_hint, if provided, overrides price-inferred exit reason logic.
        Set by override/short-exit/EOD paths via _pending_exit_hints.
        """
        symbol = change.symbol
        try:
            portfolio = await self._state_manager.load_portfolio()
            position = next((p for p in portfolio.positions if p.symbol == symbol), None)
            if position is None:
                log.debug("_journal_closed_trade: no local position for %s — skipping", symbol)
                return

            entry_price = position.avg_cost
            exit_price = change.fill_price
            pos_is_short = is_short(position.intention.direction)
            if entry_price > 0 and exit_price > 0:
                if pos_is_short:
                    pnl_pct = round((entry_price - exit_price) / entry_price * 100, 4)
                else:
                    pnl_pct = round((exit_price - entry_price) / entry_price * 100, 4)
            else:
                pnl_pct = 0.0

            # Infer exit reason from fill price vs stop/target levels.
            # Hint from caller (override/short-protection/EOD paths) takes priority.
            # For shorts: target is below entry (profit when price falls),
            # stop is above entry (loss when price rises).
            stop = position.intention.exit_targets.stop_loss
            target = position.intention.exit_targets.profit_target
            if exit_reason_hint:
                exit_reason = exit_reason_hint
            elif pos_is_short:
                if exit_price > 0 and target > 0 and exit_price <= target * 1.001:
                    exit_reason = "target"
                elif exit_price > 0 and stop > 0 and exit_price >= stop * 0.999:
                    exit_reason = "stop"
                else:
                    exit_reason = "strategy"
            else:
                if exit_price > 0 and target > 0 and exit_price >= target * 0.999:
                    exit_reason = "target"
                elif exit_price > 0 and stop > 0 and exit_price <= stop * 1.001:
                    exit_reason = "stop"
                else:
                    exit_reason = "strategy"

            ctx = self._entry_contexts.pop(symbol, {})
            # Capture live TA indicators at exit time for threshold tuning.
            # These are the fast-loop cached values — same data the override logic uses.
            signals_at_exit = dict(getattr(self, "_latest_indicators", {}).get(symbol, {}))
            # Fall back to TradeIntention fields if in-memory context was lost (e.g. restart).
            signals_at_entry = (
                ctx.get("signals")
                or position.intention.entry_signals
            )
            claude_conviction = (
                ctx.get("claude_conviction")
                or position.intention.entry_conviction
            )
            composite_score = (
                ctx.get("composite_score")
                or position.intention.entry_score
            )
            exit_time = datetime.now(timezone.utc)
            try:
                entry_dt = datetime.fromisoformat(position.entry_date)
                hold_duration_min = round((exit_time - entry_dt).total_seconds() / 60, 1)
            except Exception:
                hold_duration_min = None
            await self._trade_journal.append({
                "record_type": "close",
                "trade_id": ctx.get("trade_id"),
                "symbol": symbol,
                "strategy": position.intention.strategy,
                "direction": position.intention.direction,
                "entry_time": position.entry_date,
                "exit_time": exit_time.isoformat(),
                "hold_duration_min": hold_duration_min,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "shares": change.fill_qty,
                "pnl_pct": pnl_pct,
                "stop_price": stop,
                "target_price": target,
                "exit_reason": exit_reason,
                "signals_at_entry": signals_at_entry,
                "signals_at_exit": signals_at_exit or None,
                "claude_conviction": claude_conviction,
                "composite_score": composite_score,
                "position_size_pct": ctx.get("position_size_pct", 0.0),
                "peak_unrealized_pct": ctx.get("peak_unrealized_pct", 0.0),
                "source": "live",
                "prompt_version": self._config.claude.prompt_version,
                "bot_version": self._config.claude.model,
            })

            portfolio.positions = [p for p in portfolio.positions if p.symbol != symbol]
            # Persist close timestamp before saving so _recently_closed survives restarts.
            now_utc_iso = datetime.now(timezone.utc).isoformat()
            portfolio.recently_closed[symbol] = now_utc_iso
            await self._state_manager.save_portfolio(portfolio)
            self._recently_closed[symbol] = time.monotonic()
            self._cycle_consumed_symbols.add(symbol)
            self._position_entry_times.pop(symbol, None)
            self._trigger_state.last_profit_trigger_gain.pop(symbol, None)
            self._trigger_state.last_near_target_time.pop(symbol, None)
            self._trigger_state.last_near_stop_time.pop(symbol, None)
            self._override_closed.pop(symbol, None)
            log.info(
                "Trade closed and journaled: %s  pnl=%.2f%%  exit_reason=%s",
                symbol, pnl_pct, exit_reason,
            )
        except Exception as exc:
            log.error("_journal_closed_trade failed for %s: %s", symbol, exc, exc_info=True)

    async def _place_override_exit(self, position, exit_hint: str) -> None:
        """Place a market exit order for a position flagged by a quant override or hard stop.

        Handles fill-protection check, order construction, record keeping,
        pending exit hint tagging, and override counter increment. Shared by
        both hard-stop and signal-triggered paths in _fast_step_quant_overrides.
        """
        symbol = position.symbol

        if not self._fill_protection.can_place_order(symbol):
            log.warning(
                "Override exit for %s blocked — pending order already exists (hint=%s)",
                symbol, exit_hint,
            )
            return

        exit_side = EXIT_SIDE[position.intention.direction]
        exit_order = Order(
            symbol=symbol,
            side=exit_side,
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
                side=exit_side,
                quantity=position.shares,
                order_type="market",
                limit_price=None,
                status="PENDING",
                created_at=now_iso,
                last_checked_at=now_iso,
            )
            await self._fill_protection.record_order(order_record)
            log.info(
                "Override exit order placed — %s  order_id=%s  qty=%.2f  hint=%s",
                symbol, result.order_id, position.shares, exit_hint,
            )
            # Tag exit reason for trade journal (consumed in _dispatch_confirmed_fill)
            self._pending_exit_hints[symbol] = exit_hint
            # Increment override exit counter (feeds slow loop trigger)
            self._override_exit_count += 1
            # Record override-close timestamp for the extended re-entry cooldown.
            # override_exit_cooldown_min >> re_entry_cooldown_min because the quant
            # signal broke down — re-entry should not be allowed until momentum resets.
            self._override_closed[symbol] = time.monotonic()
        except Exception as exc:
            log.error("Failed to place override exit for %s (hint=%s): %s", symbol, exit_hint, exc)

    async def _fast_step_quant_overrides(self) -> None:
        """
        Step 3: For each open position, evaluate quantitative override signals.
        If triggered, place a market exit order immediately.

        Both long and short positions are handled in a single unified loop.
        Signal semantics are direction-aware: the same signal names invert their
        indicator logic for shorts (e.g. vwap_crossover fires on price-above-VWAP
        for shorts, price-below-VWAP for longs).

        Exit priority:
          1. Hard stop (short-only): price >= stop_loss. Fires before min-hold guard
             and before allow_signals gating — the hard stop is unconditional.
          2. Signal-triggered exit: evaluated through evaluate_overrides() with
             per-strategy allow_signals and threshold kwargs.

        ``intraday_extremum`` is the session HIGH for longs and session LOW for
        shorts, used by the ATR trailing stop signal.

        ``_fast_step_short_exits`` was removed in Phase 18 refactor; all short
        exit logic now runs through this method.
        """
        portfolio = await self._state_manager.load_portfolio()
        if not portfolio.positions:
            return

        for position in portfolio.positions:
            symbol = position.symbol
            direction = position.intention.direction

            # We need current indicators for all paths.
            indicators = getattr(self, "_latest_indicators", {}).get(symbol)
            if indicators is None:
                log.debug("No indicators cached for %s — skipping override check", symbol)
                continue

            current_price = indicators.get("price")
            if current_price is None:
                continue

            # Hard stop: short-only, fires before min-hold guard and allow_signals gate.
            if self._risk_manager.check_hard_stop(position, indicators):
                await self._place_override_exit(position, "hard_stop")
                continue

            # Resolve strategy for this position.
            target_name = position.intention.strategy
            matching = [
                s for s in self._strategies
                if type(s).__name__.replace("Strategy", "").lower() == target_name
            ]
            strategy_for_position = (matching or self._strategies)[0] if (matching or self._strategies) else None
            allow_signals = (
                strategy_for_position.applicable_override_signals()
                if strategy_for_position is not None else None
            )

            # If the strategy declares no applicable signals, skip signal evaluation.
            if allow_signals is not None and not allow_signals:
                log.debug(
                    "Override check skipped for %s — %s strategy has no applicable override signals",
                    symbol, target_name,
                )
                continue

            # Enforce minimum hold time before overrides can fire.
            # This prevents stale indicators (computed before entry) from triggering
            # an immediate exit on the same fast loop tick that registered the fill.
            min_hold_sec = self._config.scheduler.min_hold_before_override_min * 60
            entry_ts = self._position_entry_times.get(symbol)
            held_sec = (time.monotonic() - entry_ts) if entry_ts is not None else 0.0
            if held_sec < min_hold_sec:
                log.debug(
                    "Override check skipped for %s — %s",
                    symbol,
                    f"held {held_sec:.0f}s < min {min_hold_sec:.0f}s"
                    if entry_ts is not None
                    else "no entry time recorded (reconciled/adopted position)",
                )
                continue

            # Track intraday extremum: session HIGH for longs, session LOW for shorts.
            if is_short(direction):
                prev = self._intraday_lows.get(symbol, current_price)
                self._intraday_lows[symbol] = min(prev, current_price)
                intraday_extremum = self._intraday_lows[symbol]
            else:
                prev_high = self._intraday_highs.get(symbol, 0.0)
                self._intraday_highs[symbol] = max(prev_high, current_price)
                intraday_extremum = self._intraday_highs[symbol]

            # Per-strategy override thresholds.
            atr_multiplier = strategy_for_position.override_atr_multiplier() if strategy_for_position else 2.0
            vwap_threshold = strategy_for_position.override_vwap_volume_threshold() if strategy_for_position else 1.3

            should_exit, triggered_signals = self._risk_manager.evaluate_overrides(
                position, indicators, intraday_extremum,
                allow_signals=allow_signals,
                direction=direction,
                atr_multiplier=atr_multiplier,
                vwap_volume_threshold=vwap_threshold,
            )

            if not should_exit:
                continue

            log.warning(
                "QUANT OVERRIDE EXIT — %s  signals=%s  direction=%s  price=%.4f  "
                "atr=%.4f  intraday_extremum=%.4f  vwap_pos=%s  vol_ratio=%.2f",
                symbol, triggered_signals, direction, current_price,
                float(indicators.get("atr_14") or 0.0),
                intraday_extremum,
                indicators.get("vwap_position", "unknown"),
                float(indicators.get("volume_ratio") or 0.0),
            )
            await self._place_override_exit(position, "quant_override")

    async def _fast_step_pdt_check(self) -> None:
        """
        Step 4: Check that the PDT day-trade count hasn't been exceeded.
        Log a WARNING if approaching the limit.

        PDT limits only apply when equity is below the configured minimum ($25,500).
        Above that threshold the broker permits unlimited day trades regardless of
        PDT designation, so this check is skipped entirely.
        """
        try:
            # Skip the entire PDT limit check when equity is above the threshold.
            # The $25k rule only restricts trading when an account is BOTH PDT-flagged
            # AND under-capitalised. Above the threshold, day trade count is irrelevant.
            min_equity = self._config.risk.min_equity_for_trading
            if self._last_known_equity >= min_equity:
                log.debug(
                    "PDT check skipped — equity $%.2f >= PDT threshold $%.2f",
                    self._last_known_equity, min_equity,
                )
                return

            orders_state = await self._state_manager.load_orders()
            portfolio = await self._state_manager.load_portfolio()

            day_trades = self._pdt_guard.count_day_trades(
                orders_state.orders, portfolio
            )
            allowed = 3 - self._config.risk.pdt_buffer  # effective safe limit
            log.debug("PDT check: %d day trades used (safe limit=%d)", day_trades, allowed)

            if day_trades >= allowed:
                # Rate-limit to once per 5 minutes — this fires every fast loop tick otherwise.
                now_mono = time.monotonic()
                if now_mono - self._last_pdt_warning_ts >= 300:
                    log.warning(
                        "PDT WARNING: %d of %d day trades used (buffer=%d). "
                        "Same-day closes will be blocked; new entries are unaffected.",
                        day_trades, 3, self._config.risk.pdt_buffer,
                    )
                    self._last_pdt_warning_ts = now_mono
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
        local_map = {p.symbol: p for p in portfolio.positions}
        portfolio_updated = False

        # Positions we have locally but broker no longer holds — these were closed
        # outside the normal fill-detection path (e.g. manual broker close).
        # Exception: if there's a pending/partially-filled exit order for the symbol,
        # the position was closed by our own order but the fill hasn't been processed
        # yet (common with fast-settling paper market orders). Skip ghost cleanup and
        # let _dispatch_confirmed_fill handle it on the next cycle.
        ghost_local = local_symbols - broker_symbols
        if ghost_local:
            deferred = {s for s in ghost_local if not self._fill_protection.can_place_order(s)}
            if deferred:
                log.debug(
                    "Position sync: deferring ghost cleanup for %s — active exit order in flight",
                    deferred,
                )
            ghost_local -= deferred

        if ghost_local:
            log.warning(
                "Position sync: local positions not found at broker (likely closed externally): %s",
                ghost_local,
            )
            for symbol in ghost_local:
                pos = local_map.get(symbol)
                if pos is None:
                    continue
                ctx = self._entry_contexts.pop(symbol, {})
                current_price = getattr(self, "_latest_indicators", {}).get(symbol, {}).get("price", 0.0)
                # Fall back to avg_cost when no market price is available — records
                # pnl=0% rather than a misleading 0-price exit.
                if current_price == 0.0:
                    current_price = pos.avg_cost
                await self._trade_journal.append({
                    "symbol": symbol,
                    "strategy": pos.intention.strategy,
                    "direction": pos.intention.direction,
                    "entry_time": pos.entry_date,
                    "exit_time": datetime.now(timezone.utc).isoformat(),
                    "entry_price": pos.avg_cost,
                    "exit_price": current_price,
                    "shares": pos.shares,
                    "pnl_pct": round(
                        (
                            (pos.avg_cost - current_price) / pos.avg_cost * 100
                            if is_short(pos.intention.direction)
                            else (current_price - pos.avg_cost) / pos.avg_cost * 100
                        )
                        if pos.avg_cost > 0 and current_price > 0 else 0.0, 4
                    ),
                    "stop_price": pos.intention.exit_targets.stop_loss,
                    "target_price": pos.intention.exit_targets.profit_target,
                    "exit_reason": "external_close",
                    "record_type": "close",
                    "trade_id": ctx.get("trade_id"),
                    "signals_at_entry": ctx.get("signals", {}),
                    "claude_conviction": ctx.get("claude_conviction", 0.0),
                    "composite_score": ctx.get("composite_score", 0.0),
                    "position_size_pct": ctx.get("position_size_pct", 0.0),
                    "peak_unrealized_pct": ctx.get("peak_unrealized_pct", 0.0),
                    "source": "live",
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })
            portfolio.positions = [p for p in portfolio.positions if p.symbol not in ghost_local]
            now_utc_iso = datetime.now(timezone.utc).isoformat()
            for symbol in ghost_local:
                self._recently_closed[symbol] = time.monotonic()
                portfolio.recently_closed[symbol] = now_utc_iso
            portfolio_updated = True

        # Positions broker has that we don't track locally.
        # Normal bot fills are created by _register_buy_fill in the reconcile loop.
        # This path is a fallback for positions opened externally (manual trades, etc.).
        _readopt_ttl = 60.0  # seconds to block re-adoption after a position is closed
        for bp in broker_positions:
            if bp.symbol not in local_symbols:
                # Guard: if we just closed this symbol, don't immediately re-adopt.
                # This prevents the runaway loop where every close triggers a re-adopt
                # which triggers another override exit (e.g. 16 AMD sell orders).
                closed_at = self._recently_closed.get(bp.symbol, 0.0)
                if time.monotonic() - closed_at < _readopt_ttl:
                    log.debug(
                        "Position sync: skipping re-adoption of %s — closed %.0fs ago "
                        "(fill detection pending or position settling)",
                        bp.symbol, time.monotonic() - closed_at,
                    )
                    continue
                # Guard: if we have an active (PENDING or PARTIALLY_FILLED) opening
                # order for this symbol, the fill handler will register it properly
                # with the full intention when the order completes. Adopting here
                # would consume _pending_intentions prematurely, which causes the full
                # fill to be routed as a close (position already exists) and then
                # re-adopted without intention (strategy="unknown").
                in_flight = [
                    o for o in self._fill_protection.get_orders_for_symbol(bp.symbol)
                    if o.status in ("PENDING", "PARTIALLY_FILLED")
                ]
                if in_flight:
                    log.debug(
                        "Position sync: skipping adoption of %s — in-flight opening order %s",
                        bp.symbol, in_flight[0].order_id,
                    )
                    continue
                pending = self._pending_intentions.pop(bp.symbol, {})
                if pending:
                    self._entry_contexts[bp.symbol] = {
                        "trade_id": str(uuid.uuid4()),
                        "signals": pending.pop("_signals", {}),
                        "claude_conviction": pending.pop("_claude_conviction", 0.0),
                        "composite_score": pending.pop("_composite_score", 0.0),
                        "position_size_pct": pending.pop("_position_size_pct", 0.0),
                    }
                intention = TradeIntention(
                    strategy=pending.get("strategy", "unknown"),
                    direction=pending.get("direction", bp.side),
                    reasoning=pending.get("reasoning", ""),
                    exit_targets=ExitTargets(
                        stop_loss=pending.get("stop", 0.0),
                        profit_target=pending.get("target", 0.0),
                    ),
                )
                # Broker reports negative qty for short positions; shares are
                # always stored as positive — direction field carries the sign.
                adopted_qty = abs(bp.qty)
                portfolio.positions.append(Position(
                    symbol=bp.symbol,
                    shares=adopted_qty,
                    avg_cost=bp.avg_entry_price,
                    entry_date=datetime.now(timezone.utc).isoformat(),
                    intention=intention,
                ))
                # Give adopted positions the hold-time window so override signals
                # based on stale indicators don't fire immediately after adoption.
                self._position_entry_times[bp.symbol] = time.monotonic()
                portfolio_updated = True
                log.warning(
                    "Position sync: untracked broker position adopted: %s  qty=%.2f  avg_cost=%.4f  side=%s",
                    bp.symbol, adopted_qty, bp.avg_entry_price, bp.side,
                )

        # Quantity mismatches — broker is authoritative; correct local state.
        # Broker reports negative qty for shorts; compare using abs() since
        # local shares are always stored as a positive number.
        #
        # IMPORTANT: Skip upward corrections when there is an active (PENDING or
        # PARTIALLY_FILLED) order for the symbol. The broker qty already reflects
        # shares from that in-flight order, which haven't been dispatched as a fill
        # event yet. Correcting upward here would cause _dispatch_confirmed_fill to
        # see "position already exists" and route the fill as a close instead of an
        # additional open. Downward corrections are always safe (they mean shares
        # were removed, e.g. an external close).
        local_map = {p.symbol: p for p in portfolio.positions}
        for bp in broker_positions:
            lp = local_map.get(bp.symbol)
            if lp is None:
                continue
            broker_abs_qty = abs(bp.qty)
            if abs(broker_abs_qty - lp.shares) > 0.001:
                if broker_abs_qty > lp.shares and not self._fill_protection.can_place_order(bp.symbol):
                    log.debug(
                        "Position sync: skipping upward qty correction for %s "
                        "(local=%.4f broker=%.4f) — active order in flight",
                        bp.symbol, lp.shares, broker_abs_qty,
                    )
                    continue
                log.warning(
                    "Position sync: correcting %s qty local=%.4f → broker=%.4f",
                    bp.symbol, lp.shares, broker_abs_qty,
                )
                lp.shares = broker_abs_qty
                portfolio_updated = True

        if portfolio_updated:
            await self._state_manager.save_portfolio(portfolio)

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
        3. Load Claude reasoning cache; all entry opportunities originate here.
           (Strategy.generate_signals() is defined but not wired — see base_strategy.py.)
        4. Re-rank opportunity queue using cached Claude reasoning + fresh TA.
        5. Execute top opportunity (one per cycle) if risk-validated.
        6. Re-evaluate open positions; exit if strategy recommends it.
        """
        if not self._is_market_open():
            return  # no data fetches or analysis outside regular hours

        if self._degradation.market_data_available is False:
            log.warning("Medium loop: market data unavailable — skipping cycle")
            return

        # -- Step 1: gather symbols to scan ----------------------------------
        _medium_loop_start = time.monotonic()
        watchlist = await self._state_manager.load_watchlist()
        portfolio = await self._state_manager.load_portfolio()
        orders_state = await self._state_manager.load_orders()

        tier1_symbols = [e.symbol for e in watchlist.entries if e.priority_tier == 1]
        position_symbols = [p.symbol for p in portfolio.positions]
        scan_symbols = list(dict.fromkeys(tier1_symbols + position_symbols))  # dedup, order-preserving

        if not scan_symbols:
            log.debug("Medium loop: no symbols to scan")
            return

        # -- Step 2: fetch bars + run TA (parallel) --------------------------
        # All watchlist + position symbols fetched concurrently, bounded by
        # medium_loop_scan_concurrency (default 10). generate_signal_summary is
        # CPU-bound (pure pandas/numpy) — wrapped in asyncio.to_thread so it
        # does not block the event loop during TA computation.
        indicators: dict[str, dict] = {}  # symbol → full generate_signal_summary() output
        bars: dict[str, object] = {}      # symbol → DataFrame

        semaphore = asyncio.Semaphore(self._config.scheduler.medium_loop_scan_concurrency)

        async def _fetch_one(sym: str):
            async with semaphore:
                try:
                    df = await self._data_adapter.fetch_bars(sym, interval="5m", period="5d")
                except Exception:
                    # fetch_bars already logged the failure with the symbol name;
                    # swallow here so asyncio.gather never returns an Exception item
                    # and the orchestrator doesn't double-log without context.
                    return sym, None, None
                if df is None or df.empty:
                    log.warning("Medium loop: no bars returned for %s", sym)
                    return sym, None, None
                summary = await asyncio.to_thread(generate_signal_summary, sym, df)
                return sym, df, summary

        fetch_results = await asyncio.gather(
            *[_fetch_one(s) for s in scan_symbols],
            return_exceptions=True,
        )
        for item in fetch_results:
            if isinstance(item, Exception):
                # Should not happen — _fetch_one catches internally — but kept as
                # a safety net in case a future refactor re-introduces propagation.
                log.warning("Medium loop: unexpected TA error: %s", item)
                continue
            sym, df, summary = item
            if summary is None:
                # Track consecutive fetch failures for watchlist symbols so persistently
                # broken symbols (e.g. delisted tickers) can be auto-removed.
                # Context symbols (SPY, QQQ, sector ETFs) are excluded — they are not
                # on the watchlist and their fetch failures are normal and expected.
                if sym in scan_symbols:
                    self._fetch_failure_counts[sym] = self._fetch_failure_counts.get(sym, 0) + 1
                continue
            # Successful fetch — reset any failure streak for this symbol.
            self._fetch_failure_counts.pop(sym, None)
            indicators[sym] = summary
            bars[sym] = df

        # Auto-remove watchlist symbols that have persistently failed to fetch data.
        # This cleans up delisted tickers without requiring Claude to see them first.
        _removal_threshold = self._config.scheduler.fetch_failure_removal_threshold
        _symbols_to_purge = [
            s for s, c in self._fetch_failure_counts.items() if c >= _removal_threshold
        ]
        if _symbols_to_purge:
            _watchlist = await self._state_manager.load_watchlist()
            _open_syms = set()
            try:
                _pf = await self._state_manager.load_portfolio()
                _open_syms = {p.symbol for p in _pf.positions}
            except Exception:
                pass
            _actually_removed = []
            for _sym in _symbols_to_purge:
                if _sym in _open_syms:
                    # Never auto-remove a symbol with an open position.
                    continue
                _watchlist.entries = [e for e in _watchlist.entries if e.symbol != _sym]
                self._fetch_failure_counts.pop(_sym, None)
                _actually_removed.append(_sym)
            if _actually_removed:
                await self._state_manager.save_watchlist(_watchlist)
                log.warning(
                    "Auto-removed %d symbol(s) from watchlist after %d consecutive "
                    "fetch failures (likely delisted): %s",
                    len(_actually_removed), _removal_threshold, _actually_removed,
                )

        if not indicators:
            log.warning("Medium loop: TA produced no results — all fetches failed")
            self._degradation.market_data_available = False
            return

        self._degradation.market_data_available = True

        # Cache indicators for the fast loop (quant overrides).
        # Track whether this is the first population so we can fire Claude immediately.
        indicators_were_empty = not self._latest_indicators
        self._latest_indicators = {
            sym: {
                **v["signals"],
                "composite_technical_score": v.get("composite_technical_score", 0.0),
                "bars_available": v.get("bars_available", 0),
            }
            for sym, v in indicators.items()
        }

        log.debug("Medium loop: scanned %d symbol(s)", len(indicators))

        # -- Context instruments: best-effort macro data for Claude (parallel) -
        # Fetch TA for SPY, QQQ, sector ETFs not already in the main scan.
        # Failures are silently skipped — this data is informational only.
        # Uses the same semaphore so combined concurrency stays within the limit.
        ctx_needed = [s for s in _CONTEXT_SYMBOLS if s not in indicators]
        ctx_already = [s for s in _CONTEXT_SYMBOLS if s in indicators]
        for sym in ctx_already:
            self._market_context_indicators[sym] = indicators[sym]

        async def _fetch_ctx(sym: str):
            async with semaphore:
                df = await self._data_adapter.fetch_bars(sym, interval="5m", period="5d")
                if df is None or df.empty:
                    return sym, None
                summary = await asyncio.to_thread(generate_signal_summary, sym, df)
                return sym, summary

        ctx_results = await asyncio.gather(
            *[_fetch_ctx(s) for s in ctx_needed],
            return_exceptions=True,
        )
        for item in ctx_results:
            if isinstance(item, Exception):
                log.debug("Medium loop: context fetch failed: %s", item)
                continue
            sym, summary = item
            if summary is not None:
                self._market_context_indicators[sym] = summary

        # Phase 17: merge watchlist + context indicators into _all_indicators.
        # Consumed by _check_triggers (macro/sector baseline) and Phase 19 compressor.
        self._all_indicators = {**self._latest_indicators, **self._market_context_indicators}

        _medium_loop_elapsed = time.monotonic() - _medium_loop_start
        log.info(
            "Medium loop: TA complete — %d watchlist + %d context symbols  elapsed=%.1fs",
            len(indicators), len(self._market_context_indicators), _medium_loop_elapsed,
        )

        # Phase 17 (Fix 3): stamp completion time so the slow loop medium-loop gate passes.
        # Must be set before the immediate slow-loop call below so that the gate does not
        # block the very first Claude call on startup.
        self._last_medium_loop_completed_utc = datetime.now(timezone.utc)

        # Fire the slow loop immediately the first time indicators are populated.
        # This gets Claude's assessment within seconds of startup rather than waiting
        # up to slow_loop_check_sec (default 5 min) for the next scheduled tick.
        if indicators_were_empty and self._latest_indicators:
            log.info("Medium loop: indicators seeded — triggering immediate slow loop cycle")
            try:
                await self._slow_loop_cycle()
            except Exception as exc:
                log.error("Immediate slow loop cycle after indicator seed failed: %s", exc, exc_info=True)

        # -- Step 3: load Claude reasoning result for ranking -----------------
        # Phase 17 (Fix 4): use adaptive cache TTL based on current market regime.
        cached_raw = self._reasoning_cache.load_latest_if_fresh(
            max_age_min=self._compute_cache_max_age()
        )
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

        # -- Step 4: re-rank opportunity queue --------------------------------

        try:
            acct = await self._broker.get_account()
            self._last_known_equity = acct.equity
        except Exception as exc:
            self._mark_broker_failure(exc)
            return
        self._mark_broker_available()

        # Keep portfolio cash/buying_power in sync with broker so downstream
        # logic (ranker, Claude context) always sees current capital figures.
        if portfolio.cash != acct.cash or portfolio.buying_power != acct.buying_power:
            portfolio.cash = acct.cash
            portfolio.buying_power = acct.buying_power
            await self._state_manager.save_portfolio(portfolio)

        rank_result = self._ranker.rank_opportunities(
            reasoning_result,
            indicators,
            acct,
            portfolio,
            self._pdt_guard,
            self._is_market_open,
            orders=orders_state.orders,
            strategy_lookup=self._strategy_lookup,
            suppressed_symbols=self._filter_suppressed,
        )
        ranked = rank_result.candidates

        # Phase 15: record hard-filter rejections in _recommendation_outcomes.
        # Session-veto symbols and already-open duplicates are not recorded (spec §1).
        # Finding 6: build a symbol → opportunity lookup once so we can pull
        # conviction/strategy for the first-occurrence journal record below.
        _opp_by_symbol: dict[str, dict] = {
            o.get("symbol", ""): o
            for o in reasoning_result.new_opportunities
        }
        for symbol, reason in rank_result.rejections:
            # Skip symbols that are already in the portfolio (expected every cycle
            # on stale recommendations while a position is held — not useful noise).
            if "already open in portfolio" in reason:
                continue
            existing = self._recommendation_outcomes.get(symbol, {})
            new_count = existing.get("rejection_count", 0) + 1
            self._recommendation_outcomes[symbol] = {
                "claude_entry_target": existing.get("claude_entry_target", 0.0),
                "attempt_time_utc": existing.get("attempt_time_utc") or datetime.now(timezone.utc).isoformat(),
                "stage": "ranker_rejected",
                "stage_detail": reason,
                "rejection_count": new_count,
                "order_id": None,
            }
            # Finding 6: journal the first conviction_floor rejection per symbol
            # per session so calibration data accumulates across sessions.
            # Write only on new_count==1 (first occurrence this session) to avoid
            # flooding the journal on every cycle for the same stale proposal.
            if new_count == 1 and _rejection_gate_category(reason) == "conviction_floor":
                opp = _opp_by_symbol.get(symbol, {})
                await self._trade_journal.append({
                    "record_type": "rejected",
                    "rejection_gate": "conviction_floor",
                    "symbol": symbol,
                    "strategy": opp.get("strategy", ""),
                    "conviction": opp.get("conviction", 0.0),
                    "conviction_threshold": self._config.ranker.min_conviction_threshold,
                    "reason": reason,
                })
            # After max_filter_rejection_cycles consecutive hard-filter failures,
            # suppress the symbol for the rest of the session so Claude stops
            # re-proposing it and the ranker stops evaluating it every cycle.
            threshold = self._config.scheduler.max_filter_rejection_cycles
            if new_count >= threshold and symbol not in self._filter_suppressed:
                self._filter_suppressed[symbol] = reason
                log.warning(
                    "Symbol %s suppressed for session — failed hard filter %d time(s): %s",
                    symbol, new_count, reason,
                )

        log.info(
            "Medium loop: ranker returned %d opportunity/ies (from %d Claude candidates)",
            len(ranked), len(reasoning_result.new_opportunities),
        )

        # -- Candidates exhaustion detection ----------------------------------
        # When every symbol Claude recommended this cache generation has been
        # suppressed, no recovery is possible without a fresh reasoning call.
        # Set a flag so _check_triggers fires "candidates_exhausted" immediately.
        # Guard: only set once per cache generation (cleared after trigger fires).
        cache_opps = {
            o.get("symbol")
            for o in reasoning_result.new_opportunities
            if o.get("symbol")
        }
        if (
            cache_opps
            and cache_opps.issubset(self._filter_suppressed)
            and not self._trigger_state.candidates_exhausted
        ):
            self._trigger_state.candidates_exhausted = True
            log.info(
                "Candidates exhausted — all %d Claude candidate(s) suppressed %s; "
                "flagging for immediate re-reasoning",
                len(cache_opps),
                sorted(cache_opps),
            )

        # -- No-opportunity streak tracking (Finding 4 / Proposal B) ----------------
        # When ranked is empty, increment the streak counter. After
        # no_opportunity_streak_warn_threshold consecutive empty loops, emit a
        # gate-breakdown WARN so the operator can see which filter is the bottleneck.
        # "already_open" rejections are excluded from the breakdown (expected noise
        # while holding positions). Reset when candidates are available or fresh
        # reasoning fires (see _run_claude_cycle).
        if not ranked:
            self._no_opportunity_streak += 1
            warn_threshold = self._config.scheduler.no_opportunity_streak_warn_threshold
            if self._no_opportunity_streak >= warn_threshold:
                # Build gate → count breakdown, excluding already_open noise.
                gate_counts: dict[str, int] = {}
                for _sym, _reason in rank_result.rejections:
                    cat = _rejection_gate_category(_reason)
                    if cat == "already_open":
                        continue
                    gate_counts[cat] = gate_counts.get(cat, 0) + 1
                breakdown = ", ".join(
                    f"{g}={c}" for g, c in sorted(gate_counts.items(), key=lambda x: -x[1])
                ) or "no hard-filter rejections (zero Claude candidates?)"
                log.warning(
                    "No-opportunity streak: %d consecutive empty medium loops. "
                    "Gate breakdown: [%s]. "
                    "Consider reviewing the watchlist or relaxing entry gates if "
                    "market conditions remain favorable.",
                    self._no_opportunity_streak,
                    breakdown,
                )
        else:
            if self._no_opportunity_streak > 0:
                log.info(
                    "No-opportunity streak cleared after %d loop(s)",
                    self._no_opportunity_streak,
                )
            self._no_opportunity_streak = 0

        # -- Step 5: try ranked opportunities in order (one entry per cycle) --------
        if not self._degradation.safe_mode and ranked:
            for candidate in ranked[:self._config.scheduler.entry_attempts_per_cycle]:
                entered = await self._medium_try_entry(candidate, acct, portfolio, orders_state.orders)
                if entered:
                    break  # one entry per cycle; stop after first successful placement

        # -- Step 6: re-evaluate open positions ------------------------------
        await self._medium_evaluate_positions(portfolio, bars, indicators, acct, orders_state.orders)

    async def _medium_try_entry(self, top, acct, portfolio, orders) -> bool:
        """Validate and execute a single entry order for the top-ranked opportunity.

        Returns True if an order was placed, False if this candidate was skipped
        (so the caller can try the next ranked candidate).
        """
        symbol = top.symbol

        # Reasoning-cycle guard: block re-entry on a symbol that was already entered
        # and exited within the current Claude reasoning cycle. Prevents the medium
        # loop from re-using a stale recommendation after a loss (e.g. INTC entered
        # at 14:36, exited at 14:41, then re-entered at 15:21 from the same cycle).
        # Cleared by _run_claude_cycle when fresh reasoning arrives.
        if symbol in self._cycle_consumed_symbols:
            log.info(
                "Medium loop: skipping %s — already traded and closed this reasoning cycle "
                "(awaiting fresh Claude call before re-entry)",
                symbol,
            )
            return False

        # Composite score floor — prevents marginal entries where each individual component
        # clears its gate but the combined score is too weak to justify capital deployment.
        min_score = self._config.ranker.min_composite_score
        if top.composite_score < min_score:
            log.info(
                "Medium loop: skipping %s — composite score %.3f < floor %.2f",
                symbol, top.composite_score, min_score,
            )
            # Finding 6: journal every composite_score_floor rejection for calibration.
            # These pass the ranker's hard filters but fail the second-stage composite
            # floor, so they don't appear in rank_result.rejections. Journalling here
            # captures how close each near-miss was to the 0.45 threshold.
            await self._trade_journal.append({
                "record_type": "rejected",
                "rejection_gate": "composite_score_floor",
                "symbol": symbol,
                "strategy": top.strategy,
                "conviction": top.ai_conviction,
                "composite_score": round(top.composite_score, 4),
                "composite_score_floor": min_score,
            })
            return False

        log.info(
            "Medium loop: attempting entry — %s  action=%s  conviction=%.2f  score=%.2f  strategy=%s",
            symbol, top.action, top.ai_conviction, top.composite_score, top.strategy,
        )
        ind = self._latest_indicators.get(symbol, {})

        # Phase 11: Use current market price as limit order price.
        # top.suggested_entry is retained only as a reference for the drift check below.
        current_price = ind.get("price")
        if current_price is not None:
            entry_price = current_price
        else:
            log.warning(
                "Medium loop: current price unavailable for %s — falling back to suggested_entry %.2f",
                symbol, top.suggested_entry,
            )
            entry_price = top.suggested_entry

        # Conservative startup mode — suppress new entries until timer expires
        if self._conservative_mode_until is not None and not self._config.scheduler.disable_conservative_mode:
            now = datetime.now(timezone.utc)
            if now < self._conservative_mode_until:
                log.info(
                    "Medium loop: conservative mode active until %s — skipping entry for %s",
                    self._conservative_mode_until.isoformat(), symbol,
                )
                return False
        # Fill protection is the cheapest possible check — a dict lookup.
        # Run it here, before ATR sizing, validate_entry, and thesis challenge.
        if not self._fill_protection.can_place_order(symbol):
            log.debug("Medium loop: fill protection blocking entry for %s", symbol)
            return False

        # Re-entry cooldown: block new entries for a symbol after it was closed.
        # Prevents rapid churn (e.g. TSLA exited at 15:22, re-entered at 15:23).
        # Cooldown duration is strategy-specific: look up re_entry_cooldown_min in
        # the strategy's params dict (strategy_params[name]); fall back to 5 min.
        closed_at = self._recently_closed.get(symbol, 0.0)
        if closed_at > 0:
            strategy_params = self._config.strategy.strategy_params.get(top.strategy, {})
            cooldown_min = int(strategy_params.get("re_entry_cooldown_min", 5))
            elapsed = time.monotonic() - closed_at
            if elapsed < cooldown_min * 60:
                log.info(
                    "Medium loop: re-entry cooldown for %s (%s) — closed %.0fmin ago "
                    "(cooldown=%dmin)",
                    symbol, top.strategy, elapsed / 60, cooldown_min,
                )
                return False

        # Extended re-entry cooldown for quant-override exits.
        # A quant override means the momentum signal structurally failed — the setup
        # needs more time to reset than the normal re_entry_cooldown_min allows.
        # This guard runs AFTER the normal cooldown check: if recently_closed hasn't
        # expired yet, this is moot; if it has, this provides additional protection.
        override_closed_at = self._override_closed.get(symbol, 0.0)
        if override_closed_at > 0:
            override_cooldown_min = self._config.scheduler.override_exit_cooldown_min
            override_elapsed = time.monotonic() - override_closed_at
            if override_elapsed < override_cooldown_min * 60:
                log.info(
                    "Medium loop: override-exit cooldown for %s — override closed %.0fmin ago "
                    "(cooldown=%dmin)",
                    symbol, override_elapsed / 60, override_cooldown_min,
                )
                return False

        # Hard PDT gate for intraday strategies: an intraday strategy forces same-day
        # exit (last-5-min rule). With 0 day trades remaining that forced exit becomes
        # a PDT violation.  Strategies that hold overnight (is_intraday=False) are exempt.
        # PDT rules only apply below min_equity_for_trading ($25,500); above that the
        # broker permits unlimited day trades regardless of PDT designation.
        strategy_obj = self._strategy_lookup.get(top.strategy)
        if strategy_obj is not None and strategy_obj.is_intraday:
            if acct.equity < self._config.risk.min_equity_for_trading:
                pdt_used = self._pdt_guard.count_day_trades(orders, portfolio)
                pdt_remaining = max(0, 3 - self._config.risk.pdt_buffer - pdt_used)
                if pdt_remaining == 0:
                    log.info(
                        "Medium loop: PDT block — 0 day trades remaining, "
                        "skipping intraday entry for %s (%s)",
                        symbol, top.strategy,
                    )
                    return False

        # Phase 14: Claude's per-trade entry conditions gate.
        # Checked here — after the cheapest guards (fill protection, cooldown, PDT)
        # and before any computation (drift, sizing, risk manager).
        # Log at INFO — blocked conditions are normal expected behaviour, not errors.
        entry_conds = getattr(top, "entry_conditions", {}) or {}
        if entry_conds:
            conds_met, conds_reason = evaluate_entry_conditions(entry_conds, ind)
            if not conds_met:
                max_defers = self._config.scheduler.max_entry_defer_cycles
                self._entry_defer_counts[symbol] = self._entry_defer_counts.get(symbol, 0) + 1
                count = self._entry_defer_counts[symbol]
                if count >= max_defers:
                    # Suppress for the session — same mechanism as hard-filter rejection.
                    # Clearing entry_conditions on the live object is insufficient because
                    # the object is rebuilt from the reasoning cache each medium loop, so
                    # the cleared gate is restored on the next cycle. Session suppression
                    # is the correct termination — Claude will reconsider on the next build.
                    suppression_reason = (
                        f"stale thesis gate: entry conditions expired after {count} consecutive defers"
                    )
                    self._filter_suppressed[symbol] = suppression_reason
                    log.warning(
                        "Entry conditions not met for %s: %s — "
                        "suppressed for session after %d consecutive defers (stale thesis gate)",
                        symbol, conds_reason, count,
                    )
                    # Phase 15: record gate expiry
                    self._recommendation_outcomes[symbol] = {
                        **self._recommendation_outcomes.get(symbol, {}),
                        "stage": "gate_expired",
                        "stage_detail": (
                            f"session-suppressed after {count} defers: {suppression_reason}"
                        ),
                    }
                else:
                    log.info(
                        "Entry conditions not met for %s: %s — defer %d/%d",
                        symbol, conds_reason, count, max_defers,
                    )
                    # Phase 15: record conditions_waiting
                    self._recommendation_outcomes[symbol] = {
                        **self._recommendation_outcomes.get(symbol, {}),
                        "stage": "conditions_waiting",
                        "stage_detail": (
                            f"defer_count={count}, conditions={top.entry_conditions}"
                        ),
                        "attempt_time_utc": (
                            self._recommendation_outcomes.get(symbol, {}).get("attempt_time_utc")
                            or datetime.now(timezone.utc).isoformat()
                        ),
                    }
                return False

        entry_direction = direction_from_action(top.action)
        order_side = "sell" if is_short(entry_direction) else "buy"
        strategy_name = top.strategy

        # Phase 11: Staleness / drift check — skip entry if price has moved too far from Claude's target.
        # Long chase: price ran past Claude's entry (momentum already captured without us).
        # Long adverse: price broke below Claude's intended buy level (thesis likely invalid).
        # Direction is inverted for shorts.
        if top.suggested_entry > 0:
            drift = (entry_price - top.suggested_entry) / top.suggested_entry
            if is_short(entry_direction):
                if drift < -self._config.ranker.max_entry_drift_pct:
                    log.info(
                        "Entry skipped for %s: short chase — current %.2f vs suggested %.2f (%.1f%%)",
                        symbol, entry_price, top.suggested_entry, drift * 100,
                    )
                    return False
                if drift > self._config.ranker.max_adverse_drift_pct:
                    log.info(
                        "Entry skipped for %s: short adverse drift — current %.2f vs suggested %.2f (+%.1f%%)",
                        symbol, entry_price, top.suggested_entry, drift * 100,
                    )
                    return False
            else:
                if drift > self._config.ranker.max_entry_drift_pct:
                    log.info(
                        "Entry skipped for %s: long chase — current %.2f vs suggested %.2f (+%.1f%%)",
                        symbol, entry_price, top.suggested_entry, drift * 100,
                    )
                    return False
                if drift < -self._config.ranker.max_adverse_drift_pct:
                    log.info(
                        "Entry skipped for %s: long adverse drift — current %.2f vs suggested %.2f (%.1f%%)",
                        symbol, entry_price, top.suggested_entry, drift * 100,
                    )
                    return False

        # Determine ATR for position sizing
        atr = ind.get("atr_14", 0.0)
        avg_vol = ind.get("avg_daily_volume")

        # Primary sizing: Claude's conviction-based position_size_pct drives the target.
        # ATR formula (calculate_position_size) used previously ignored position_size_pct
        # entirely, always saturating near the 20% max_position cap regardless of conviction.
        # Claude sizes 5%–20% based on setup quality; honour that recommendation here.
        # Clamp by max_position_pct (config) before computing shares so the ceiling is
        # respected at the sizing step and not just caught later as a validate_entry rejection.
        effective_pct = min(top.position_size_pct, self._config.risk.max_position_pct)
        target_qty = int((acct.equity * effective_pct) / entry_price) if entry_price > 0 else 0
        if target_qty <= 0:
            log.debug(
                "Medium loop: position_size_pct=%.2f (capped=%.2f) gives 0 shares for %s at %.2f — skipping",
                top.position_size_pct, effective_pct, symbol, entry_price,
            )
            return False

        # Scale quantity by TA signal quality.
        # At composite_technical_score=0 → ta_size_factor_min of base qty; at 1.0 → 100% of base qty.
        tech_score = ind.get("composite_technical_score", 0.5)
        size_factor = (
            self._config.ranker.ta_size_factor_min
            + (1.0 - self._config.ranker.ta_size_factor_min) * tech_score
        )
        quantity = max(1, int(target_qty * size_factor))
        log.debug(
            "Position sizing for %s: pct=%.0f%% (cap=%.0f%%) target=%d  TA_factor=%.2f (tech_score=%.2f) → qty=%d",
            symbol, top.position_size_pct * 100, self._config.risk.max_position_pct * 100,
            target_qty, size_factor, tech_score, quantity,
        )

        # ATR-based position size cap: hard ceiling to prevent a single stop-out from exceeding
        # max_risk_per_trade_pct of portfolio equity regardless of ATR on the day.
        # max_shares = (equity × max_risk_pct) / ATR (risk per share = one ATR).
        # Direction-agnostic: ATR measures two-way risk symmetrically.
        if self._config.risk.atr_position_size_cap_enabled and atr and atr > 0:
            max_shares_by_atr = int(
                (acct.equity * self._config.risk.max_risk_per_trade_pct) / atr
            )
            if max_shares_by_atr > 0 and quantity > max_shares_by_atr:
                log.info(
                    "ATR position cap applied for %s: qty %d → %d "
                    "(equity=%.0f, max_risk_pct=%.1f%%, ATR=%.4f)",
                    symbol, quantity, max_shares_by_atr,
                    acct.equity, self._config.risk.max_risk_per_trade_pct * 100, atr,
                )
                quantity = max_shares_by_atr

        allowed, reason = self._risk_manager.validate_entry(
            symbol=symbol,
            side=order_side,
            quantity=quantity,
            price=entry_price,
            blocks_eod_entries=strategy_obj.blocks_eod_entries if strategy_obj is not None else False,
            dead_zone_exempt=strategy_obj.dead_zone_exempt if strategy_obj is not None else False,
            account=acct,
            portfolio=portfolio,
            orders=orders,
            avg_daily_volume=avg_vol,
        )
        if not allowed:
            log.info("Medium loop: entry blocked for %s — %s", symbol, reason)
            return False

        # Thesis challenge for large positions — returns a concern_level (0–1) that applies
        # a bounded size penalty. Never blocks the trade; worst case shrinks it by
        # thesis_challenge_max_penalty (default 35%).
        challenge_size_threshold = self._config.ranker.thesis_challenge_size_threshold
        if top.position_size_pct >= challenge_size_threshold:
            ttl = self._config.ranker.thesis_challenge_ttl_min * 60
            cached = self._thesis_challenge_cache.get(symbol)
            cached_concern: float | None = None
            if cached:
                cached_concern_val, ts = cached
                if time.monotonic() - ts < ttl:
                    cached_concern = cached_concern_val
                    log.debug(
                        "Thesis challenge cache hit for %s: concern_level=%.2f (cached %.0fmin ago)",
                        symbol, cached_concern, (time.monotonic() - ts) / 60,
                    )

            if cached_concern is None:
                # Build a compact portfolio summary for concentration assessment.
                portfolio_summary = {
                    "open_positions": [
                        {"symbol": p.symbol, "direction": getattr(p, "direction", "long")}
                        for p in portfolio.positions
                    ],
                    "position_count": len(portfolio.positions),
                }
                challenge = await self._claude.run_thesis_challenge(
                    opportunity={
                        "symbol": symbol,
                        "strategy": strategy_name,
                        "conviction": top.ai_conviction,
                        "suggested_entry": entry_price,
                        "suggested_exit": top.suggested_exit,
                        "suggested_stop": top.suggested_stop,
                        "atr": ind.get("atr_14", 0.0),
                        "position_size_pct": top.position_size_pct,
                        "reasoning": top.reasoning,
                    },
                    market_context=self._latest_market_context,
                    portfolio=portfolio_summary,
                )
                if challenge is not None:
                    try:
                        cached_concern = max(0.0, min(1.0, float(challenge.get("concern_level", 0.0))))
                    except (TypeError, ValueError):
                        log.warning("Thesis challenge for %s: non-numeric concern_level — ignoring", symbol)
                        cached_concern = 0.0
                    self._thesis_challenge_cache[symbol] = (cached_concern, time.monotonic())

            if cached_concern is not None and cached_concern > 0.0:
                max_penalty = self._config.ranker.thesis_challenge_max_penalty
                size_factor = 1.0 - (cached_concern * max_penalty)
                quantity = max(1, int(quantity * size_factor))
                log.info(
                    "Thesis challenge penalty for %s: concern=%.2f → size_factor=%.2f → qty=%d",
                    symbol, cached_concern, size_factor, quantity,
                )

        # Determine stop and target for position intention.
        # Prefer Claude's suggested levels (specific support/resistance) when provided;
        # fall back to ATR-based calculation if Claude returned 0.
        # For shorts, stop is above entry (loss if price rises) and target is below entry.
        atr_for_intention = ind.get("atr_14", 0.0)
        atr_or_pct = atr_for_intention if atr_for_intention > 0 else entry_price * 0.02
        if is_short(entry_direction):
            use_stop = (
                top.suggested_stop if top.suggested_stop > 0
                else entry_price + 2 * atr_or_pct
            )
            use_target = (
                top.suggested_exit if top.suggested_exit > 0
                else entry_price - 3 * atr_or_pct
            )
        else:
            use_stop = (
                top.suggested_stop if top.suggested_stop > 0
                else entry_price - 2 * atr_or_pct
            )
            use_target = (
                top.suggested_exit if top.suggested_exit > 0
                else entry_price + 3 * atr_or_pct
            )

        # High-conviction market-order strategies use market orders for immediate fills.
        # Below the threshold, or for strategies that require limit entries, use a limit.
        mkt_threshold = self._config.scheduler.market_order_conviction_threshold
        use_market = (
            strategy_obj is not None
            and strategy_obj.uses_market_orders
            and top.ai_conviction >= mkt_threshold
        )
        order = Order(
            symbol=symbol,
            side=order_side,
            quantity=quantity,
            order_type="market" if use_market else "limit",
            limit_price=None if use_market else entry_price,
            time_in_force="day",
        )
        try:
            result = await self._broker.place_order(order)
        except Exception as exc:
            self._mark_broker_failure(exc)
            return False
        self._mark_broker_available()

        # Store intention so _fast_step_position_sync can attach it to the position on fill.
        # Also stash signal context for the trade journal (consumed when the position closes).
        self._pending_intentions[symbol] = {
            "stop": round(use_stop, 4),
            "target": round(use_target, 4),
            "strategy": strategy_name,
            "reasoning": top.reasoning,
            "direction": entry_direction,
            "_signals": dict(self._latest_indicators.get(symbol, {})),
            "_claude_conviction": float(top.ai_conviction),
            "_composite_score": float(top.composite_score),
            "_position_size_pct": float(top.position_size_pct),
        }

        now_iso = datetime.now(timezone.utc).isoformat()
        actual_order_type = "market" if use_market else "limit"
        # Strategy-specific limit order timeout: swing entries need more time to fill
        # at a tight spread than momentum scalps. Set per-order timeout_seconds so
        # get_stale_orders() can use it directly without a global switch.
        # To add a new strategy-specific timeout, add one entry to this dict.
        _strategy_timeouts = {
            "swing": self._config.scheduler.swing_limit_order_timeout_sec,
        }
        entry_timeout = _strategy_timeouts.get(strategy_name, self._config.scheduler.limit_order_timeout_sec)
        record = OrderRecord(
            order_id=result.order_id,
            symbol=symbol,
            side=order_side,
            quantity=quantity,
            order_type=actual_order_type,
            limit_price=None if use_market else entry_price,
            status="PENDING",
            created_at=now_iso,
            last_checked_at=now_iso,
            timeout_seconds=entry_timeout,
        )
        await self._fill_protection.record_order(record)
        # Phase 15: record order_pending outcome.
        self._recommendation_outcomes[symbol] = {
            **self._recommendation_outcomes.get(symbol, {}),
            "stage": "order_pending",
            "order_id": result.order_id,
            "claude_entry_target": top.suggested_entry,
            "attempt_time_utc": (
                self._recommendation_outcomes.get(symbol, {}).get("attempt_time_utc")
                or datetime.now(timezone.utc).isoformat()
            ),
        }
        # Order placed — clear any accumulated defer count for this symbol.
        self._entry_defer_counts.pop(symbol, None)
        if use_market:
            log.info(
                "Entry order placed (MARKET) — %s  qty=%d  conviction=%.2f>=%.2f  strategy=%s  "
                "score=%.3f  stop=%.4f  target=%.4f",
                symbol, quantity, top.ai_conviction, mkt_threshold,
                strategy_name, top.composite_score, use_stop, use_target,
            )
        else:
            log.info(
                "Entry order placed — %s  qty=%d  limit=%.2f  strategy=%s  score=%.3f  "
                "stop=%.4f  target=%.4f  (stop_src=%s  target_src=%s)",
                symbol, quantity, entry_price, strategy_name, top.composite_score,
                use_stop, use_target,
                "claude" if top.suggested_stop > 0 else "atr",
                "claude" if top.suggested_exit > 0 else "atr",
            )
        return True

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

            # Route to the strategy that opened this position so a swing position is
            # not evaluated by MomentumStrategy (and vice versa). Fall back to the
            # first active strategy if the original strategy has since been disabled.
            target_name = position.intention.strategy
            matching = [
                s for s in self._strategies
                if type(s).__name__.replace("Strategy", "").lower() == target_name
            ]
            eval_strategies = matching if matching else self._strategies[:1]

            for strategy in eval_strategies:
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

                # buy to close short, sell to close long
                exit_side = EXIT_SIDE[position.intention.direction]
                if exit_sug and exit_sug.order_type == "limit" and exit_sug.exit_price > 0:
                    exit_order = Order(
                        symbol=symbol,
                        side=exit_side,
                        quantity=position.shares,
                        order_type="limit",
                        limit_price=exit_sug.exit_price,
                        time_in_force="day",
                    )
                else:
                    exit_order = Order(
                        symbol=symbol,
                        side=exit_side,
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
                    side=exit_side,
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

            # EOD forced close for momentum shorts.
            # Swing shorts may be held overnight intentionally — excluded.
            # Only fires if no exit order was placed by strategy evaluation above.
            if (
                is_short(position.intention.direction)
                and position.intention.strategy == "momentum"
                and is_last_five_minutes()
                and self._fill_protection.can_place_order(symbol)
            ):
                log.info(
                    "Medium loop: EOD forced close — momentum short %s in last 5 minutes",
                    symbol,
                )
                exit_side = EXIT_SIDE[position.intention.direction]
                eod_order = Order(
                    symbol=symbol,
                    side=exit_side,
                    quantity=position.shares,
                    order_type="market",
                    time_in_force="day",
                )
                try:
                    result = await self._broker.place_order(eod_order)
                except Exception as exc:
                    self._mark_broker_failure(exc)
                    continue
                self._mark_broker_available()
                now_iso = datetime.now(timezone.utc).isoformat()
                record = OrderRecord(
                    order_id=result.order_id,
                    symbol=symbol,
                    side=exit_side,
                    quantity=position.shares,
                    order_type="market",
                    limit_price=None,
                    status="PENDING",
                    created_at=now_iso,
                    last_checked_at=now_iso,
                )
                await self._fill_protection.record_order(record)
                # Tag exit reason for trade journal (consumed in _dispatch_confirmed_fill)
                self._pending_exit_hints[symbol] = "eod_close"
                log.info(
                    "EOD short exit order placed — %s  order_id=%s", symbol, result.order_id,
                )

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
        if not self._is_market_open():
            return  # no Claude calls outside regular hours

        # Guard: don't call Claude until the medium loop has computed indicators at
        # least once. Without TA data the context is empty and Claude rejects everything.
        if not self._latest_indicators:
            log.debug("Slow loop: no indicator data yet — waiting for first medium loop cycle")
            return

        # Phase 17 (Fix 3): medium-loop gate — only fire when a new medium loop cycle
        # has completed since the last Claude call. This prevents Claude from reasoning
        # on the same stale indicators twice in a row (e.g. when a trigger fires faster
        # than the medium loop interval). If we have never called Claude, always proceed.
        if self._trigger_state.last_claude_call_utc is not None:
            last_medium = self._last_medium_loop_completed_utc
            if last_medium is None or last_medium <= self._trigger_state.last_claude_call_utc:
                log.debug(
                    "Slow loop: no new medium loop cycle since last Claude call — skipping"
                )
                return

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

    async def _check_triggers(self, now: datetime | None = None) -> list[str]:
        """
        Evaluate all slow-loop trigger conditions.
        Returns a list of trigger name strings (empty = no trigger).

        ``now`` is injectable for testing; defaults to current UTC time.
        """
        triggers: list[str] = []
        now = now or datetime.now(timezone.utc)
        ts = self._trigger_state

        # 1. Time ceiling: 60+ minutes since last Claude call
        if ts.last_claude_call_utc is None:
            triggers.append("no_previous_call")
            log.debug("Trigger: no_previous_call FIRED (first call)")
        else:
            elapsed_min = (now - ts.last_claude_call_utc).total_seconds() / 60
            if elapsed_min >= self._config.claude.max_reasoning_interval_min:
                triggers.append("time_ceiling")
                log.debug(
                    "Trigger: time_ceiling FIRED  elapsed=%.1fmin threshold=%.1fmin",
                    elapsed_min, self._config.claude.max_reasoning_interval_min,
                )
            else:
                log.debug(
                    "Trigger: time_ceiling skip  elapsed=%.1fmin threshold=%.1fmin",
                    elapsed_min, self._config.claude.max_reasoning_interval_min,
                )

        # 2. Price move: any Tier 1 symbol, open position, or macro index moved
        #    beyond threshold since last eval. Threshold read from config
        #    (slow_loop_price_move_threshold_pct). SPY/QQQ/IWM included for
        #    macro awareness even though they are not entry candidates.
        #    Phase 17: uses self._all_indicators (set by medium loop) for a
        #    single merged lookup across watchlist + context symbols.
        price_move_threshold = self._config.scheduler.slow_loop_price_move_threshold_pct / 100
        watchlist = await self._state_manager.load_watchlist()
        portfolio = await self._state_manager.load_portfolio()
        tracked = {e.symbol for e in watchlist.entries if e.priority_tier == 1}
        tracked |= {p.symbol for p in portfolio.positions}
        tracked |= {"SPY", "QQQ", "IWM"}
        # Use the medium-loop-cached merged dict; fall back to on-demand merge for
        # tests or early startup cycles where the medium loop hasn't run yet.
        all_ind = self._all_indicators or {
            **self._latest_indicators,
            **self._market_context_indicators,
        }
        for symbol in tracked:
            current_price = all_ind.get(symbol, {}).get("price")
            if current_price is None:
                # Fallback: try nested signals dict (context symbol format differs)
                sig = all_ind.get(symbol, {}).get("signals", {})
                current_price = sig.get("price")
            if current_price is None:
                continue
            last_price = ts.last_prices.get(symbol)
            if last_price and abs(current_price - last_price) / last_price > price_move_threshold:
                pct_chg = (current_price - last_price) / last_price * 100
                triggers.append(f"price_move:{symbol}")
                log.debug(
                    "Trigger: price_move:%s FIRED  price=%.4f last=%.4f chg=%.2f%% threshold=%.2f%%",
                    symbol, current_price, last_price, pct_chg, price_move_threshold * 100,
                )

        # 3. Position approaching target (within 1% of profit target or stop loss)
        # near_target has a cooldown: once Claude reviews and holds, the trigger is
        # suppressed for near_target_cooldown_sec to prevent repeated firing while
        # price oscillates near the target level.
        position_indicators = self._latest_indicators
        near_target_cooldown = self._config.scheduler.near_target_cooldown_sec
        for pos in portfolio.positions:
            targets = pos.intention.exit_targets
            current = position_indicators.get(pos.symbol, {}).get("price")
            if current is None:
                continue
            if targets.profit_target > 0:
                pct_to_target = abs(current - targets.profit_target) / targets.profit_target
                if pct_to_target <= 0.01:
                    last_fired = ts.last_near_target_time.get(pos.symbol, 0.0)
                    if time.monotonic() - last_fired >= near_target_cooldown:
                        triggers.append(f"near_target:{pos.symbol}")
                        ts.last_near_target_time[pos.symbol] = time.monotonic()
                        log.debug(
                            "Trigger: near_target:%s FIRED  price=%.4f target=%.4f pct_away=%.2f%%",
                            pos.symbol, current, targets.profit_target, pct_to_target * 100,
                        )
                    else:
                        log.debug(
                            "Trigger: near_target:%s suppressed (cooldown %.0fs remaining)",
                            pos.symbol,
                            near_target_cooldown - (time.monotonic() - last_fired),
                        )
            if targets.stop_loss > 0:
                pct_to_stop = abs(current - targets.stop_loss) / targets.stop_loss
                if pct_to_stop <= 0.01:
                    last_fired = ts.last_near_stop_time.get(pos.symbol, 0.0)
                    if time.monotonic() - last_fired >= near_target_cooldown:
                        triggers.append(f"near_stop:{pos.symbol}")
                        ts.last_near_stop_time[pos.symbol] = time.monotonic()
                        log.debug(
                            "Trigger: near_stop:%s FIRED  price=%.4f stop=%.4f pct_away=%.2f%%",
                            pos.symbol, current, targets.stop_loss, pct_to_stop * 100,
                        )
                    else:
                        log.debug(
                            "Trigger: near_stop:%s suppressed (cooldown %.0fs remaining)",
                            pos.symbol,
                            near_target_cooldown - (time.monotonic() - last_fired),
                        )

        # 4. Override exit occurred since last Claude call
        if self._override_exit_count > ts.last_override_exit_count:
            triggers.append("override_exit")
            log.debug(
                "Trigger: override_exit FIRED  count=%d last_seen=%d",
                self._override_exit_count, ts.last_override_exit_count,
            )

        # 4b. Open position has reached a meaningful unrealised gain.
        # Fires when gain_pct crosses the configured threshold, then re-arms each time
        # gain grows by another full interval — gives Claude a chance to tighten the
        # stop progressively as the position moves in our favour.
        # Direction-aware: shorts profit when price falls below avg_cost.
        profit_threshold = self._config.scheduler.position_profit_trigger_pct
        for pos in portfolio.positions:
            current = position_indicators.get(pos.symbol, {}).get("price")
            if current is None or pos.avg_cost <= 0:
                continue
            if is_short(pos.intention.direction):
                gain_pct = (pos.avg_cost - current) / pos.avg_cost
            else:
                gain_pct = (current - pos.avg_cost) / pos.avg_cost
            # Always update peak unrealized — even when below the trigger threshold.
            ctx = self._entry_contexts.setdefault(pos.symbol, {})
            prev_peak = ctx.get("peak_unrealized_pct", 0.0)
            if gain_pct * 100 > prev_peak:
                ctx["peak_unrealized_pct"] = round(gain_pct * 100, 4)

            if gain_pct < profit_threshold:
                continue
            last_trigger = ts.last_profit_trigger_gain.get(pos.symbol, 0.0)
            if gain_pct >= last_trigger + profit_threshold:
                triggers.append(f"position_in_profit:{pos.symbol}")
                log.debug(
                    "Trigger: position_in_profit:%s FIRED  gain=%.2f%% last_trigger=%.2f%% interval=%.2f%%",
                    pos.symbol, gain_pct * 100, last_trigger * 100, profit_threshold * 100,
                )
                await self._trade_journal.append({
                    "record_type": "snapshot",
                    "trade_id": self._entry_contexts.get(pos.symbol, {}).get("trade_id"),
                    "symbol": pos.symbol,
                    "trigger": "position_in_profit",
                    "unrealized_pnl_pct": round(gain_pct * 100, 4),
                    "peak_unrealized_pct": ctx.get("peak_unrealized_pct", round(gain_pct * 100, 4)),
                    "current_price": current,
                    "stop_price": pos.intention.exit_targets.stop_loss,
                    "target_price": pos.intention.exit_targets.profit_target,
                    "strategy": pos.intention.strategy,
                    "direction": pos.intention.direction,
                    "source": "live",
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })

        # 5. Market session transition (open at 9:30 ET, approaching close at 3:30 ET)
        current_session = get_current_session()
        last_session = ts.last_session
        if last_session != current_session.value:
            if current_session == Session.REGULAR_HOURS:
                triggers.append("session_open")
                log.debug("Trigger: session_open FIRED  prev_session=%s", last_session)
            elif current_session == Session.POST_MARKET and last_session == Session.REGULAR_HOURS:
                triggers.append("session_close")
                log.debug("Trigger: session_close FIRED")
            # Always update last_session regardless of whether we fire
            ts.last_session = current_session.value

        # Also fire ~30 min before close (3:30 PM ET) while still in regular hours
        if current_session == Session.REGULAR_HOURS:
            from datetime import time as _time
            from zoneinfo import ZoneInfo as _ZI
            et = now.astimezone(_ZI("America/New_York"))
            if _time(15, 28) <= et.time() <= _time(15, 32):
                # Only fire once in the ~5-min window; use time_ceiling or a flag
                if "session_open" not in triggers and "time_ceiling" not in triggers:
                    triggers.append("approaching_close")

        # 6. Watchlist critically small
        if len(watchlist.entries) < 10:
            triggers.append("watchlist_small")
            log.debug("Trigger: watchlist_small FIRED  size=%d", len(watchlist.entries))

        # 6b. Watchlist stale — periodic proactive refresh.
        # Fires when enough time has elapsed since the last watchlist build (both
        # watchlist_small and watchlist_stale paths update last_watchlist_build_utc).
        # interval_min = 0 disables this trigger entirely (no overhead).
        # To add a new time-based watchlist trigger: add an entry here and route it
        # through is_watchlist_build in _run_claude_cycle.
        interval_min = self._config.scheduler.watchlist_refresh_interval_min
        if interval_min > 0:
            last_build = ts.last_watchlist_build_utc
            elapsed_min = (
                (now - last_build).total_seconds() / 60
                if last_build is not None
                else float("inf")  # never built → fire immediately on first tick
            )
            if elapsed_min >= interval_min:
                triggers.append("watchlist_stale")
                log.debug(
                    "Trigger: watchlist_stale FIRED  elapsed_min=%.1f  interval=%d",
                    elapsed_min, interval_min,
                )

        # 7. Indicators seeded for the first time — fire once after the first medium
        #    loop cycle so Claude always has real TA data on its first call.
        if not ts.indicators_seeded and self._all_indicators:
            triggers.append("indicators_ready")
            ts.indicators_seeded = True
            # Phase 17: seed last_claude_call_prices from the current snapshot so that
            # macro_move / sector_move have a valid baseline from the first call forward.
            for sym, ind in all_ind.items():
                price = ind.get("price") or ind.get("signals", {}).get("price")
                if price is not None:
                    ts.last_claude_call_prices[sym] = price

        # Phase 17 (Fix 2): Macro move trigger --------------------------------
        # Fires when any SPY/QQQ/IWM index moves beyond macro_move_trigger_pct (1%)
        # from its price at the time of the last Claude call. Uses a separate
        # last_claude_call_prices baseline (not last_prices, which resets each tick)
        # so sustained intraday moves always fire even when last_prices catches up.
        if ts.last_claude_call_prices:
            macro_move_threshold = self._config.scheduler.macro_move_trigger_pct / 100
            for sym in self._config.scheduler.macro_move_symbols:
                ind = all_ind.get(sym, {})
                current_price = ind.get("price") or ind.get("signals", {}).get("price")
                if current_price is None:
                    continue
                baseline = ts.last_claude_call_prices.get(sym)
                if baseline:
                    pct_chg = (current_price - baseline) / baseline * 100
                    if abs(pct_chg) / 100 > macro_move_threshold:
                        triggers.append(f"market_move:{sym}")
                        log.debug(
                            "Trigger: market_move:%s FIRED  price=%.4f baseline=%.4f chg=%.2f%% threshold=%.2f%%",
                            sym, current_price, baseline, pct_chg, macro_move_threshold * 100,
                        )
                    else:
                        log.debug(
                            "Trigger: market_move:%s skip  chg=%.2f%% threshold=%.2f%%",
                            sym, pct_chg, macro_move_threshold * 100,
                        )

        # Phase 17 (Fix 2): Sector move trigger --------------------------------
        # Fires when a sector ETF moves beyond sector_move_trigger_pct (1.5%) from its
        # price at the last Claude call. When the portfolio has open exposure to a sector
        # (i.e. we hold a position in a symbol that maps to that ETF), the threshold is
        # tightened by sector_exposure_threshold_factor (0.7 → 1.05% instead of 1.5%).
        if ts.last_claude_call_prices:
            sector_move_threshold = self._config.scheduler.sector_move_trigger_pct / 100
            sector_factor = self._config.scheduler.sector_exposure_threshold_factor
            # Determine which sector ETFs the portfolio has exposure to.
            # _SECTOR_MAP[symbol] → sector ETF. Symbols absent from the map degrade
            # gracefully (base threshold used, not tightened threshold).
            exposed_sectors = {
                _SECTOR_MAP[pos.symbol]
                for pos in portfolio.positions
                if pos.symbol in _SECTOR_MAP
            }
            for etf in _CONTEXT_SECTOR_ETFS:
                # Skip if this ETF is a held position — price_move already covers it.
                if etf in {pos.symbol for pos in portfolio.positions}:
                    continue
                ind = all_ind.get(etf, {})
                current_price = ind.get("price") or ind.get("signals", {}).get("price")
                if current_price is None:
                    continue
                baseline = ts.last_claude_call_prices.get(etf)
                if not baseline:
                    continue
                threshold = (
                    sector_move_threshold * sector_factor
                    if etf in exposed_sectors
                    else sector_move_threshold
                )
                pct_chg = (current_price - baseline) / baseline * 100
                if abs(pct_chg) / 100 > threshold:
                    triggers.append(f"sector_move:{etf}")
                    log.debug(
                        "Trigger: sector_move:%s FIRED  price=%.4f baseline=%.4f chg=%.2f%% "
                        "threshold=%.2f%% exposed=%s",
                        etf, current_price, baseline, pct_chg, threshold * 100,
                        etf in exposed_sectors,
                    )
                else:
                    log.debug(
                        "Trigger: sector_move:%s skip  chg=%.2f%% threshold=%.2f%% exposed=%s",
                        etf, pct_chg, threshold * 100, etf in exposed_sectors,
                    )

        # Phase 17 (Fix 2): Market RSI extreme trigger -------------------------
        # Fires when SPY RSI crosses into panic (< macro_rsi_panic_threshold) or
        # euphoria (> macro_rsi_euphoria_threshold) territory. Re-arm band prevents
        # rapid re-firing: once triggered, RSI must recover by macro_rsi_rearm_band
        # points before the trigger can fire again.
        # Note: the TA module key is "rsi" (not "rsi_14" as spec draft said).
        spy_sig = all_ind.get("SPY", {})
        spy_rsi = (spy_sig.get("signals") or spy_sig).get("rsi")
        if spy_rsi is not None:
            cfg_s = self._config.scheduler
            # Panic (low RSI): market selloff
            if spy_rsi < cfg_s.macro_rsi_panic_threshold and not ts.rsi_extreme_fired_low:
                triggers.append("market_rsi_extreme")
                ts.rsi_extreme_fired_low = True
                log.debug(
                    "Trigger: market_rsi_extreme FIRED (panic)  spy_rsi=%.1f threshold=%.1f",
                    spy_rsi, cfg_s.macro_rsi_panic_threshold,
                )
            elif spy_rsi > cfg_s.macro_rsi_panic_threshold + cfg_s.macro_rsi_rearm_band:
                if ts.rsi_extreme_fired_low:
                    log.debug(
                        "Trigger: market_rsi_extreme re-armed (panic)  spy_rsi=%.1f", spy_rsi,
                    )
                ts.rsi_extreme_fired_low = False  # re-arm when RSI recovers
            # Euphoria (high RSI): market overheating
            if spy_rsi > cfg_s.macro_rsi_euphoria_threshold and not ts.rsi_extreme_fired_high:
                triggers.append("market_rsi_extreme")
                ts.rsi_extreme_fired_high = True
                log.debug(
                    "Trigger: market_rsi_extreme FIRED (euphoria)  spy_rsi=%.1f threshold=%.1f",
                    spy_rsi, cfg_s.macro_rsi_euphoria_threshold,
                )
            elif spy_rsi < cfg_s.macro_rsi_euphoria_threshold - cfg_s.macro_rsi_rearm_band:
                if ts.rsi_extreme_fired_high:
                    log.debug(
                        "Trigger: market_rsi_extreme re-armed (euphoria)  spy_rsi=%.1f", spy_rsi,
                    )
                ts.rsi_extreme_fired_high = False  # re-arm when RSI normalises
        else:
            log.debug("Trigger: market_rsi_extreme skip — SPY RSI unavailable")

        # 9. Candidates exhausted — all current Claude recommendations suppressed.
        # Fires at most once per reasoning cache generation: guarded by
        # last_exhaustion_trigger_utc so a new reasoning cycle must complete before
        # this can fire again (prevents rapid-fire if the new cycle also produces
        # immediately-suppressable candidates).
        if ts.candidates_exhausted:
            last_exhausted = ts.last_exhaustion_trigger_utc
            last_call = ts.last_claude_call_utc
            already_fired_this_generation = (
                last_exhausted is not None
                and last_call is not None
                and last_exhausted >= last_call
            )
            if not already_fired_this_generation:
                triggers.append("candidates_exhausted")
                ts.candidates_exhausted = False
                ts.last_exhaustion_trigger_utc = now
                log.debug("Trigger: candidates_exhausted FIRED")

        return triggers

    async def _update_trigger_prices(self) -> None:
        """Snapshot current prices into last_prices for next trigger comparison."""
        # Phase 17: use self._all_indicators (already merged by medium loop).
        for symbol, ind in self._all_indicators.items():
            price = ind.get("price")
            if price is None:
                price = ind.get("signals", {}).get("price")
            if price is not None:
                self._trigger_state.last_prices[symbol] = price

    async def _build_market_context(self, acct, pdt_remaining: int) -> dict:
        """
        Build the market_data context dict for Claude reasoning calls.

        Derives macro trend summary from _market_context_indicators (populated
        each medium cycle) and fetches tier-1 watchlist news concurrently.
        """
        ctx = self._market_context_indicators

        def _classify_trend(sym: str) -> str:
            """Map a context symbol's TA signals to a simple trend label."""
            signals = ctx.get(sym, {}).get("signals", {})
            ts   = signals.get("trend_structure", "")
            vwap = signals.get("vwap_position", "")
            if ts == "bullish_aligned" and vwap in ("above", "at"):
                return "bullish"
            if ts == "bearish_aligned" and vwap in ("below", "at"):
                return "bearish"
            if ts or vwap:
                return "mixed"
            return "unknown"

        spy_rsi = ctx.get("SPY", {}).get("signals", {}).get("rsi")

        bullish_count = sum(
            1 for sym in _CONTEXT_SYMBOLS
            if ctx.get(sym, {}).get("signals", {}).get("trend_structure") == "bullish_aligned"
        )
        market_breadth = f"{bullish_count}/{len(_CONTEXT_SYMBOLS)} context instruments bullish-aligned"

        _SECTOR_ETF_NAMES = {
            "XLK": "Technology",
            "XLF": "Financials",
            "XLE": "Energy",
            "XLV": "Healthcare",
            "XLI": "Industrials",
            "XLY": "Consumer Discretionary",
            "XLC": "Communication Services",
            "ITA": "Aerospace & Defense",
            "XBI": "Biotechnology",
        }
        sector_performance = []
        for etf, sector in _SECTOR_ETF_NAMES.items():
            ind = ctx.get(etf)
            if not ind:
                continue
            sector_performance.append({
                "sector":          sector,
                "etf":             etf,
                "trend":           ind.get("signals", {}).get("trend_structure", "unknown"),
                "composite_score": ind.get("composite_technical_score", 0.0),
            })
        sector_performance.sort(key=lambda x: x["composite_score"], reverse=True)

        # Fetch news for tier-1 watchlist symbols concurrently (best-effort).
        watchlist = await self._state_manager.load_watchlist()
        tier1 = [e.symbol for e in watchlist.entries if e.priority_tier == 1]
        max_items = self._config.claude.news_max_items_per_symbol
        max_age = self._config.claude.news_max_age_hours
        news_results = await asyncio.gather(
            *[
                self._data_adapter.fetch_news(s, max_items=max_items, max_age_hours=max_age)
                for s in tier1
            ],
            return_exceptions=True,
        )
        watchlist_news: dict[str, list] = {}
        for sym, result in zip(tier1, news_results):
            if isinstance(result, Exception):
                continue
            if result:
                watchlist_news[sym] = result

        market_ctx: dict = {
            "spy_trend":         _classify_trend("SPY"),
            "spy_rsi":           spy_rsi,
            "qqq_trend":         _classify_trend("QQQ"),
            "market_breadth":    market_breadth,
            "sector_performance": sector_performance,
            "watchlist_news":    watchlist_news,
            "trading_session":   get_current_session().value,
            "pdt_trades_remaining": max(0, pdt_remaining),
            "account_equity":    acct.equity,
            "buying_power":      acct.buying_power,
            # Active strategies from config — Claude must only recommend strategies in this list.
            "active_strategies": self._config.strategy.active_strategies,
        }

        # Daily-bar macro regime context — added when _daily_indicators is populated.
        # These are daily signals for SPY/QQQ; useful for multi-day swing thesis evaluation.
        # spy_rsi above remains the intraday 5-min signal; spy_daily.rsi_14d is the daily view.
        _spy_daily = self._daily_indicators.get("SPY")
        if _spy_daily:
            market_ctx["spy_daily"] = {
                "rsi_14d":       _spy_daily.get("rsi_14d"),
                "daily_trend":   _spy_daily.get("daily_trend"),
                "roc_5d":        _spy_daily.get("roc_5d"),
                "ema20_vs_ema50": _spy_daily.get("ema20_vs_ema50"),
            }
        _qqq_daily = self._daily_indicators.get("QQQ")
        if _qqq_daily:
            market_ctx["qqq_daily"] = {
                "rsi_14d":     _qqq_daily.get("rsi_14d"),
                "daily_trend": _qqq_daily.get("daily_trend"),
            }

        return market_ctx

    async def _run_claude_cycle(self, trigger_name: str) -> None:
        """
        Assembles context, calls Claude, processes the response, and
        updates state accordingly. On API failure, enters quantitative-only
        mode with exponential backoff.
        """
        # Phase 15: purge stale recommendation outcome entries (date != today UTC).
        today_utc = datetime.now(timezone.utc).date()
        stale_symbols: list[str] = []
        for sym, rec in list(self._recommendation_outcomes.items()):
            ts = rec.get("attempt_time_utc")
            if not ts:
                continue
            try:
                entry_date = datetime.fromisoformat(ts).date()
            except Exception:
                continue
            if entry_date != today_utc:
                stale_symbols.append(sym)
        for sym in stale_symbols:
            del self._recommendation_outcomes[sym]
        if stale_symbols:
            log.debug(
                "Slow loop: purged %d stale recommendation outcome(s): %s",
                len(stale_symbols), stale_symbols,
            )

        portfolio = await self._state_manager.load_portfolio()
        watchlist = await self._state_manager.load_watchlist()
        orders_state = await self._state_manager.load_orders()
        indicators = getattr(self, "_latest_indicators", {})

        # Build market_data context block
        try:
            acct = await self._broker.get_account()
            self._last_known_equity = acct.equity
        except Exception as exc:
            self._mark_broker_failure(exc)
            log.warning("Slow loop: cannot fetch account for Claude context — skipping cycle")
            return
        self._mark_broker_available()

        # PDT rules only apply below the equity floor; above it the broker permits
        # unlimited day trades regardless of PDT designation.
        if acct.equity < self._config.risk.min_equity_for_trading:
            pdt_remaining = 3 - self._config.risk.pdt_buffer - \
                self._pdt_guard.count_day_trades(orders_state.orders, portfolio)
        else:
            pdt_remaining = 3

        market_data = await self._build_market_context(acct, pdt_remaining)

        self._latest_market_context = market_data  # store for medium loop thesis challenge

        # -- Daily-bar fetch for swing position reviews and macro context -----
        # Fetch daily bars for all open swing positions + SPY + QQQ.
        # Failures are logged and silently skipped — stale/missing daily indicators
        # degrade gracefully; they never block the reasoning call.
        _daily_symbols: list[str] = ["SPY", "QQQ"]
        for _pos in portfolio.positions:
            if getattr(_pos.intention, "strategy", None) == "swing":
                if _pos.symbol not in _daily_symbols:
                    _daily_symbols.append(_pos.symbol)

        async def _fetch_daily(sym: str) -> tuple[str, dict]:
            try:
                df = await self._data_adapter.fetch_bars(sym, interval="1d", period="3mo")
                return sym, generate_daily_signal_summary(sym, df)
            except Exception as exc:
                log.warning("Daily bar fetch failed for %s: %s", sym, exc)
                return sym, {}

        _daily_results = await asyncio.gather(
            *[_fetch_daily(s) for s in _daily_symbols],
            return_exceptions=True,
        )
        for _item in _daily_results:
            if isinstance(_item, Exception):
                continue
            _sym, _dsig = _item
            if _dsig:
                self._daily_indicators[_sym] = _dsig

        # -- Phase 18: watchlist_small → dedicated build cycle ----------------
        # When watchlist_small fires, run the universe scanner and a focused
        # watchlist build (with live RVOL candidates + optional Brave Search).
        # If watchlist_small is the only trigger, apply results and return early
        # (skipping the heavier run_reasoning_cycle). If other triggers also
        # fired, the build runs first and then reasoning continues with the
        # freshly-updated watchlist.
        triggers_list = [t for t in trigger_name.split("|") if t]
        # Both watchlist_small and watchlist_stale route through the same build path.
        # To add another watchlist-build trigger: add it to this condition and to _check_triggers.
        is_watchlist_build = "watchlist_small" in triggers_list or "watchlist_stale" in triggers_list
        other_triggers = [t for t in triggers_list if t not in ("watchlist_small", "watchlist_stale")]

        if is_watchlist_build:
            # Universe scan (with session cache)
            if self._config.universe_scanner.enabled and self._universe_scanner is not None:
                cache_age_min = (time.monotonic() - self._last_universe_scan_time) / 60
                if cache_age_min > self._config.universe_scanner.cache_ttl_min or not self._last_universe_scan:
                    existing_symbols = {e.symbol for e in watchlist.entries}
                    blacklist_symbols = set(self._config.ranker.no_entry_symbols)
                    try:
                        self._last_universe_scan = await self._universe_scanner.get_top_candidates(
                            n=self._config.universe_scanner.max_candidates,
                            exclude=existing_symbols,
                            blacklist=blacklist_symbols,
                        )
                        self._last_universe_scan_time = time.monotonic()
                        log.info(
                            "Universe scan: %d candidates (top RVOL: %s)",
                            len(self._last_universe_scan),
                            [c["symbol"] for c in self._last_universe_scan[:5]],
                        )
                    except Exception as exc:
                        log.warning("Universe scan failed — proceeding without candidates: %s", exc)
                else:
                    log.debug("Universe scan: cache still fresh (%.1f min old)", cache_age_min)

            # Derive trigger name for logging (may be watchlist_small, watchlist_stale, or both)
            wl_build_triggers = [t for t in triggers_list if t in ("watchlist_small", "watchlist_stale")]
            log.info(
                "Slow loop: running watchlist build [trigger=%s]  "
                "candidates=%d  search=%s",
                "|".join(wl_build_triggers),
                len(self._last_universe_scan),
                "enabled" if (self._search_adapter and self._search_adapter.enabled) else "disabled",
            )
            try:
                # Slice to max_candidates_to_claude before passing to Claude.
                # The scanner ranks by RVOL descending; the slice keeps the highest-
                # activity names and reduces prompt size / token pressure.
                _n = self._config.universe_scanner.max_candidates_to_claude
                _candidates_for_claude = (self._last_universe_scan or [])[:_n] or None
                wl_result = await self._claude.run_watchlist_build(
                    market_context=market_data,
                    current_watchlist=watchlist,
                    target_count=self._config.claude.watchlist_build_target,
                    candidates=_candidates_for_claude,
                    search_adapter=self._search_adapter,
                    no_entry_symbols=self._config.ranker.no_entry_symbols,
                )
                if wl_result is not None:
                    open_symbols = {p.symbol for p in portfolio.positions}
                    await self._apply_watchlist_changes(
                        watchlist, wl_result.watchlist, [], open_symbols
                    )
                    log.info(
                        "Slow loop: watchlist build complete — %d suggestions applied",
                        len(wl_result.watchlist),
                    )
                    # Reload so subsequent reasoning call sees the updated watchlist
                    watchlist = await self._state_manager.load_watchlist()
                # Stamp the build timestamp regardless of result — even a failed/empty build
                # should reset the cooldown so watchlist_stale doesn't re-fire every tick.
                self._trigger_state.last_watchlist_build_utc = datetime.now(timezone.utc)
                log.debug("Watchlist build complete — last_watchlist_build_utc updated")
            except Exception as exc:
                log.error("Watchlist build failed: %s", exc, exc_info=True)
                # Back-date last_watchlist_build_utc so watchlist_stale re-fires after the
                # circuit-breaker probe interval (circuit_breaker_probe_min) rather than on
                # every slow-loop tick. Without this, a sustained 529 outage generates one
                # failed build attempt per minute until the probe clears.
                probe_sec = self._config.ai_fallback.circuit_breaker_probe_min * 60
                interval_sec = self._config.scheduler.watchlist_refresh_interval_min * 60
                self._trigger_state.last_watchlist_build_utc = (
                    datetime.now(timezone.utc) - timedelta(seconds=interval_sec - probe_sec)
                )
                log.debug(
                    "Watchlist build failed — next retry in ~%d min",
                    self._config.ai_fallback.circuit_breaker_probe_min,
                )

            if not other_triggers:
                # Watchlist build was the only trigger — update prices and return.
                # Do NOT set last_claude_call_utc (watchlist build is not a reasoning call;
                # the time-ceiling trigger should still fire normally).
                await self._update_trigger_prices()
                return

            # Other triggers also fired — continue with run_reasoning_cycle below,
            # using the refreshed watchlist.
            # Reset the inter-call gap so the reasoning call doesn't wait 3 seconds for
            # the watchlist build to expire. Both calls are sequential steps in the same
            # cycle — the min_call_interval_sec guard is intended to prevent rapid bursts
            # across independent cycles, not within a single _run_claude_cycle invocation.
            self._claude._last_call_end_time = 0.0
            trigger_name = "|".join(other_triggers)

        log.info(
            "Slow loop: calling Claude reasoning [trigger=%s]  "
            "positions=%d  watchlist=%d  session=%s",
            trigger_name,
            len(portfolio.positions),
            len(watchlist.entries),
            market_data["trading_session"],
        )

        # Phase 15: pre-compute execution history before the (sync) assemble_reasoning_context call.
        recent_executions = await self._trade_journal.load_recent(
            self._config.claude.recent_executions_count
        )
        execution_stats = await self._trade_journal.compute_session_stats(
            min_trades=self._config.claude.execution_stats_min_trades
        )

        try:
            result = await self._claude.run_reasoning_cycle(
                portfolio=portfolio,
                watchlist=watchlist,
                market_data=market_data,
                indicators=indicators,
                trigger=trigger_name,
                skip_cache=True,   # slow loop always wants a fresh call
                recommendation_outcomes=self._recommendation_outcomes,
                recent_executions=recent_executions,
                execution_stats=execution_stats,
                session_suppressed=self._filter_suppressed,
                claude_soft_rejections=self._claude_soft_rejections,
                daily_indicators=self._daily_indicators,
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

        # --- Implicit rejection detection -----------------------------------
        # Directional (non-"either") tier-1 symbols that were sent to Claude but
        # appear in neither new_opportunities nor rejected_opportunities were silently
        # omitted. Log and journal them so we can measure the pattern over time.
        # This is observability only — no corrective action is taken here.
        _mentioned_syms = (
            {o.get("symbol") for o in (result.new_opportunities or []) if o.get("symbol")}
            | {r.get("symbol") for r in (result.rejected_opportunities or []) if r.get("symbol")}
        )
        _sent_tier1 = set(self._claude.last_sent_tier1_symbols)
        _watchlist_dir = {
            e.symbol: e.expected_direction
            for e in watchlist.entries
            if e.priority_tier == 1 and getattr(e, "expected_direction", "either") != "either"
        }
        for _sym, _dir in _watchlist_dir.items():
            if _sym in _sent_tier1 and _sym not in _mentioned_syms:
                log.info(
                    "Implicit rejection: %s (expected_direction=%s) sent to Claude but absent "
                    "from both new_opportunities and rejected_opportunities",
                    _sym, _dir,
                )
                await self._trade_journal.append({
                    "record_type": "implicit_rejection",
                    "symbol": _sym,
                    "expected_direction": _dir,
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })

        # Update consecutive soft-rejection counts.
        # A symbol is penalised each cycle it appears in rejected_opportunities.
        # Reset when Claude enters it or it was absent from the reasoning window.
        rejected_syms = {r["symbol"] for r in (result.rejected_opportunities or []) if r.get("symbol")}
        for entry in watchlist.entries:
            sym = entry.symbol
            if sym in rejected_syms:
                self._claude_soft_rejections[sym] = self._claude_soft_rejections.get(sym, 0) + 1
            else:
                self._claude_soft_rejections.pop(sym, None)
        # Phase 17 (Fix 2): snapshot prices anchored to this call for macro/sector move triggers.
        for sym, ind in self._all_indicators.items():
            price = ind.get("price") or ind.get("signals", {}).get("price")
            if price is not None:
                self._trigger_state.last_claude_call_prices[sym] = price
        # Fresh reasoning arrived — unlock all consumed symbols and reset defer counts
        # so new opportunities from this cycle can be entered on a clean slate.
        # Before clearing: journal any deferred entries Claude just abandoned so we
        # can measure how often this happens and whether price moved after abandonment.
        if self._entry_defer_counts:
            new_opp_syms = {o.get("symbol") for o in (result.new_opportunities or []) if o.get("symbol")}
            for sym, defer_count in self._entry_defer_counts.items():
                if sym in new_opp_syms:
                    continue  # re-recommended — not abandoned
                outcome = self._recommendation_outcomes.get(sym, {})
                if outcome.get("stage") not in ("conditions_waiting",):
                    continue  # wasn't in deferred state
                rejection = next(
                    (r for r in (result.rejected_opportunities or []) if r.get("symbol") == sym),
                    None,
                )
                price_now = (
                    self._all_indicators.get(sym, {}).get("price")
                    or self._latest_indicators.get(sym, {}).get("price")
                )
                await self._trade_journal.append({
                    "record_type": "deferred_abandoned",
                    "symbol": sym,
                    "defer_count": defer_count,
                    "price_at_abandonment": price_now,
                    "abandonment_type": "explicit_rejection" if rejection else "not_re_recommended",
                    "rejection_reason": rejection.get("rejection_reason") if rejection else None,
                    "conditions": outcome.get("stage_detail"),
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })
                log.info(
                    "Deferred entry abandoned: %s  defer_count=%d  type=%s  price=%.4f",
                    sym, defer_count,
                    "explicit_rejection" if rejection else "not_re_recommended",
                    price_now or 0.0,
                )
        self._cycle_consumed_symbols.clear()
        self._entry_defer_counts.clear()
        self._no_opportunity_streak = 0

        # Snapshot current unrealised gain for each open position so the
        # position_in_profit trigger re-arms from the reviewed level, not from
        # the original entry price (which would fire again immediately next cycle).
        for pos in portfolio.positions:
            current = indicators.get(pos.symbol, {}).get("price")
            if current is None or pos.avg_cost <= 0:
                continue
            if is_short(pos.intention.direction):
                gain_pct = (pos.avg_cost - current) / pos.avg_cost
            else:
                gain_pct = (current - pos.avg_cost) / pos.avg_cost
            self._trigger_state.last_profit_trigger_gain[pos.symbol] = gain_pct

        # Snapshot current prices after a successful cycle
        await self._update_trigger_prices()

        # -- Apply watchlist changes ------------------------------------------
        changes = result.watchlist_changes
        add_list    = changes.get("add", [])
        remove_list = changes.get("remove", [])
        open_symbols = {p.symbol for p in portfolio.positions}
        # Always call — enforces the size cap even when Claude suggests no changes.
        actual_adds = await self._apply_watchlist_changes(watchlist, add_list, remove_list, open_symbols)

        # -- Apply position review notes -------------------------------------
        if result.position_reviews:
            await self._apply_position_reviews(result.position_reviews)

        # Persist recommendation_outcomes so rejection counts survive restarts within
        # the same trading day. Load a fresh portfolio to avoid clobbering any
        # concurrent fast-loop or medium-loop writes.
        try:
            _pf_for_outcomes = await self._state_manager.load_portfolio()
            _pf_for_outcomes.recommendation_outcomes = dict(self._recommendation_outcomes)
            await self._state_manager.save_portfolio(_pf_for_outcomes)
        except Exception as _exc:
            log.warning("Slow loop: failed to persist recommendation_outcomes: %s", _exc)

        opp_symbols = [o.get("symbol") or o.get("ticker", "?") for o in result.new_opportunities]
        log.info(
            "Slow loop: Claude cycle complete — %d new opportunities %s  "
            "%d watchlist adds %s  %d removes %s  %d position reviews",
            len(result.new_opportunities),
            opp_symbols or "",
            actual_adds,
            [e.get("symbol") for e in add_list[:actual_adds]] if actual_adds else "",
            len(remove_list),
            remove_list or "",
            len(result.position_reviews),
        )

    def _compute_cache_max_age(self) -> int:
        """
        Phase 17 (Fix 4): Adaptive reasoning cache TTL based on SPY RSI regime.

        Returns the cache max-age in minutes for the current market environment:
        - Panic (SPY RSI ≤ cache_panic_rsi_low):    shorter TTL → Claude re-reasons sooner
        - Stress (SPY RSI ≤ cache_stress_rsi_low):  medium TTL
        - Euphoria (SPY RSI ≥ cache_euphoria_rsi_high): shorter TTL → Claude reassesses risk
        - Normal: default TTL (cache_max_age_default_min, 60 min)

        Falls back to cache_max_age_default_min when SPY RSI is unavailable.
        """
        cfg = self._config.claude
        spy_ind = self._market_context_indicators.get("SPY", {})
        spy_rsi = (spy_ind.get("signals") or spy_ind).get("rsi")
        if spy_rsi is None:
            return cfg.cache_max_age_default_min
        if spy_rsi <= cfg.cache_panic_rsi_low:
            return cfg.cache_max_age_panic_min
        if spy_rsi <= cfg.cache_stress_rsi_low:
            return cfg.cache_max_age_stressed_min
        if spy_rsi >= cfg.cache_euphoria_rsi_high:
            return cfg.cache_max_age_euphoria_min
        return cfg.cache_max_age_default_min

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
        open_symbols: set[str] | None = None,
    ) -> int:  # returns count of symbols actually added
        """Apply Claude-suggested watchlist additions and removals, then enforce the size cap.

        ``open_symbols`` is the set of symbols with active positions — these are
        never pruned regardless of score or rank.  Pass an empty set when no
        positions are open.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        from ozymandias.core.state_manager import WatchlistEntry

        # Non-tradeable index tickers that Alpaca cannot order (config.json entry would be
        # overkill for a static safety blacklist — these never change).
        _INDEX_BLACKLIST = {
            "VIX", "VXN", "SPX", "NDX", "RUT", "DJI", "COMP",
            "INDU", "NYA", "XAX", "OEX", "MID", "SML",
        }

        existing_symbols = {e.symbol for e in watchlist.entries}
        added = 0

        for item in add_list:
            # Claude may return plain strings ("SPY") or dicts ({"symbol": "SPY", ...})
            if isinstance(item, str):
                symbol = item.strip().upper()
                reason = "Added by Claude"
                tier = 1
                strategy = "both"
                expected_direction = "either"
            else:
                symbol = item.get("symbol", "").upper()
                reason = item.get("reason", "Added by Claude")
                tier = item.get("priority_tier", 1)
                strategy = item.get("strategy", "both")
                # Phase 15: extract expected_direction from Claude's add-item dict.
                # Default "either" when absent for backward compatibility.
                _raw_ed = item.get("expected_direction", "either")
                if _raw_ed not in {"long", "short", "either"}:
                    log.warning(
                        "Watchlist add %s: Claude returned invalid expected_direction %r — using 'either'",
                        symbol, _raw_ed,
                    )
                    _raw_ed = "either"
                expected_direction = _raw_ed
            if not symbol or symbol in existing_symbols:
                continue
            if symbol in _INDEX_BLACKLIST or symbol.startswith("^"):
                log.warning("Watchlist: rejected non-tradeable index ticker %s", symbol)
                continue
            watchlist.entries.append(WatchlistEntry(
                symbol=symbol,
                date_added=now_iso,
                reason=reason,
                priority_tier=tier,
                strategy=strategy,
                expected_direction=expected_direction,
            ))
            existing_symbols.add(symbol)
            added += 1
            log.info("Watchlist: added %s (tier=%s direction=%s)", symbol, tier, expected_direction)

        if remove_list:
            before = len(watchlist.entries)
            watchlist.entries = [
                e for e in watchlist.entries if e.symbol not in remove_list
            ]
            removed = before - len(watchlist.entries)
            if removed:
                log.info("Watchlist: removed %d symbol(s): %s", removed, remove_list)

        # Hard size cap — prune lowest-scoring entries beyond the limit.
        # Open positions are always protected; entries with no recent TA data
        # (score=0.0) are pruned first, followed by weakest-signal entries.
        # Phase 15: direction-aware scoring — symbols with an expected_direction use
        # the direction-adjusted composite score so a short-thesis symbol is not
        # evicted because its long score is weak.
        max_entries = self._config.claude.watchlist_max_entries
        if len(watchlist.entries) > max_entries:
            # Newly-added symbols are protected for this cycle: they haven't been
            # through a medium loop scan yet so _latest_indicators has no data for
            # them, giving a score of 0.0 that would cause immediate eviction.
            newly_added = {
                (item if isinstance(item, str) else item.get("symbol", "")).upper()
                for item in add_list
            } - {""}
            protected = (open_symbols or set()) | newly_added

            def _prune_score(e) -> float:
                ind = self._latest_indicators.get(e.symbol, {})
                raw = ind.get("signals") or {}
                if raw:
                    ed = getattr(e, "expected_direction", "either")
                    if ed != "either":
                        # Direction-adjusted: use thesis direction for scoring.
                        return compute_composite_score(raw, direction=ed)
                    return max(
                        compute_composite_score(raw, direction="long"),
                        compute_composite_score(raw, direction="short"),
                    )
                return ind.get("composite_technical_score", 0.0)

            keep_protected = [e for e in watchlist.entries if e.symbol in protected]
            prunable = [e for e in watchlist.entries if e.symbol not in protected]
            slots = max(0, max_entries - len(keep_protected))
            prunable.sort(key=_prune_score, reverse=True)
            pruned = [e.symbol for e in prunable[slots:]]
            watchlist.entries = keep_protected + prunable[:slots]
            if pruned:
                log.info(
                    "Watchlist: pruned %d entries over cap=%d: %s",
                    len(pruned), max_entries, pruned,
                )

        await self._state_manager.save_watchlist(watchlist)
        return added

    async def _apply_position_reviews(
        self,
        reviews: list[dict],
    ) -> None:
        """Append Claude's review notes and act on exit recommendations.

        Always loads a fresh portfolio snapshot so stop/target adjustments are
        not silently lost when a concurrent fast-loop or medium-loop save writes
        the disk between the slow loop's initial load and this function's save.
        """
        portfolio = await self._state_manager.load_portfolio()
        now_iso = datetime.now(timezone.utc).isoformat()
        changed = False
        for review in reviews:
            symbol = review.get("symbol", "")
            action = review.get("action", "hold")
            note = review.get("updated_reasoning") or review.get("notes", "")
            log.info(
                "Position review: %s — action=%s — %s",
                symbol, action, note or "(no rationale provided)",
            )
            for pos in portfolio.positions:
                if pos.symbol != symbol:
                    continue

                # Journal this review event before any mutations so the record
                # captures the state Claude reasoned about (current stop/target
                # before adjustment) plus what Claude recommended.
                _review_price = getattr(self, "_latest_indicators", {}).get(symbol, {}).get("price")
                _pos_is_short = is_short(pos.intention.direction)
                if _review_price and pos.avg_cost > 0:
                    _gain = (
                        (pos.avg_cost - _review_price) / pos.avg_cost
                        if _pos_is_short
                        else (_review_price - pos.avg_cost) / pos.avg_cost
                    )
                    _unrealized_pnl_pct = round(_gain * 100, 4)
                else:
                    _unrealized_pnl_pct = None
                await self._trade_journal.append({
                    "record_type": "review",
                    "trade_id": self._entry_contexts.get(symbol, {}).get("trade_id"),
                    "symbol": symbol,
                    "strategy": pos.intention.strategy,
                    "direction": pos.intention.direction,
                    "action": action,
                    "note": review.get("updated_reasoning") or review.get("notes", ""),
                    "current_price": _review_price,
                    "unrealized_pnl_pct": _unrealized_pnl_pct,
                    "current_stop": pos.intention.exit_targets.stop_loss,
                    "current_target": pos.intention.exit_targets.profit_target,
                    "thesis_intact": review.get("thesis_intact"),
                    "adjusted_targets": review.get("adjusted_targets"),
                    "source": "live",
                    "prompt_version": self._config.claude.prompt_version,
                    "bot_version": self._config.claude.model,
                })

                if note:
                    pos.intention.review_notes.append(f"[{now_iso}] {note}")
                    changed = True
                # Apply adjusted targets if provided
                adj = review.get("adjusted_targets") or {}
                if adj.get("profit_target"):
                    old_target = pos.intention.exit_targets.profit_target
                    new_target = float(adj["profit_target"])
                    if new_target == old_target:
                        log.debug("Position review: %s target no-op (%.4f unchanged)", symbol, old_target)
                    else:
                        pos.intention.exit_targets.profit_target = new_target
                        changed = True
                        log.info(
                            "Position review: %s target adjusted  %.4f → %.4f",
                            symbol, old_target, new_target,
                        )
                if adj.get("stop_loss"):
                    new_stop = float(adj["stop_loss"])
                    # Guard: reject a stop adjustment that would put the stop on the
                    # wrong side of current price — it would trigger an immediate exit
                    # on the next fast-loop cycle rather than protecting future gains.
                    # This happened with XOM when Claude raised the stop to $162 while
                    # price was $161.25, forcing an instant exit at +1.7%.
                    _cur = _review_price  # already fetched above; None if unavailable
                    # _from_dict_position normalises "sell_short" → "short" at load
                    # time, so _pos_is_short (from is_short()) is reliable here.
                    _is_short_dir = _pos_is_short
                    _would_trigger = (
                        _cur is not None and (
                            (not _is_short_dir and new_stop >= _cur)
                            or (_is_short_dir and new_stop <= _cur)
                        )
                    )
                    old_stop = pos.intention.exit_targets.stop_loss
                    if new_stop == old_stop:
                        log.debug("Position review: %s stop no-op (%.4f unchanged)", symbol, old_stop)
                    elif _would_trigger:
                        log.warning(
                            "Stop adjustment rejected for %s: new_stop=%.4f would "
                            "immediately trigger at current_price=%.4f — keeping "
                            "existing stop=%.4f",
                            symbol, new_stop, _cur,
                            old_stop,
                        )
                    else:
                        pos.intention.exit_targets.stop_loss = new_stop
                        changed = True
                        log.info(
                            "Position review: %s stop adjusted  %.4f → %.4f  price=%.4f",
                            symbol, old_stop, new_stop, _cur or 0.0,
                        )
                # Claude recommends exiting — place a market exit order immediately.
                # thesis_intact=False is a hard signal; action="exit" is sufficient.
                if action == "exit":
                    # Guard: swing positions must be held for a minimum time before
                    # Claude review exits are honoured. Intraday signal deterioration
                    # is expected noise for a multi-day strategy; acting on it causes
                    # same-session exits that defeat the swing thesis entirely.
                    # Positions with strategy="unknown" (broker-adopted) are treated
                    # as swing for this gate — conservative default.
                    swing_strategies = {"swing", "unknown"}
                    if pos.intention.strategy in swing_strategies:
                        min_hold = self._config.strategy.swing_min_hold_hours
                        try:
                            entry_dt = datetime.fromisoformat(pos.entry_date)
                            hold_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                        except Exception:
                            hold_hours = 9999.0  # unparseable date → assume held long enough
                        if hold_hours < min_hold:
                            log.info(
                                "Claude review exit blocked for %s (%s) — "
                                "held %.1fh < swing_min_hold_hours=%.1fh",
                                symbol, pos.intention.strategy, hold_hours, min_hold,
                            )
                            break
                    # Guard against the override-vs-Claude race: if this symbol was
                    # closed by the fast loop (override/stop) while Claude's API call
                    # was in flight, the position no longer exists — skip the exit.
                    if symbol in self._recently_closed:
                        log.info(
                            "Claude position review: exit for %s skipped — "
                            "position already closed by fast loop (%.0fs ago)",
                            symbol,
                            time.monotonic() - self._recently_closed[symbol],
                        )
                        break
                    if not self._fill_protection.can_place_order(symbol):
                        log.warning(
                            "Claude position review: exit for %s blocked — pending order exists",
                            symbol,
                        )
                        break
                    exit_side = EXIT_SIDE[pos.intention.direction]
                    exit_order = Order(
                        symbol=symbol,
                        side=exit_side,
                        quantity=pos.shares,
                        order_type="market",
                        time_in_force="day",
                    )
                    try:
                        result = await self._broker.place_order(exit_order)
                        order_record = OrderRecord(
                            order_id=result.order_id,
                            symbol=symbol,
                            side=exit_side,
                            quantity=pos.shares,
                            order_type="market",
                            limit_price=None,
                            status="PENDING",
                            created_at=now_iso,
                            last_checked_at=now_iso,
                        )
                        await self._fill_protection.record_order(order_record)
                        log.info(
                            "Claude position review exit: %s  order_id=%s  "
                            "reason=%s",
                            symbol, result.order_id,
                            review.get("updated_reasoning", "")[:120],
                        )
                    except Exception as exc:
                        log.error(
                            "Failed to place Claude review exit for %s: %s",
                            symbol, exc,
                        )
                    break  # done with this position
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

    def _is_market_open(self) -> bool:
        """Return True if market is open, or if the market-hours bypass is active."""
        if self._config.scheduler.bypass_market_hours:
            return True
        return is_market_open()

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
        if not creds_path.exists():
            raise RuntimeError(
                f"Credentials file not found: {creds_path}\n"
                f"Create it with:  python scripts/encrypt_credentials.py"
            )
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

        # Inject Gemini key into env if present and not already set externally
        gemini_key = creds.get("gemini_api_key")
        if gemini_key and not os.environ.get("GEMINI_API_KEY"):
            os.environ["GEMINI_API_KEY"] = gemini_key
            log.debug("GEMINI_API_KEY set from credentials file")
        elif os.environ.get("GEMINI_API_KEY"):
            log.debug("GEMINI_API_KEY already set in environment — credentials file value ignored")

        # Inject Brave Search key into env if present and not already set externally
        brave_key = creds.get("brave_search_api_key")
        if brave_key and not os.environ.get("BRAVE_SEARCH_API_KEY"):
            os.environ["BRAVE_SEARCH_API_KEY"] = brave_key
            log.debug("BRAVE_SEARCH_API_KEY set from credentials file")
        elif os.environ.get("BRAVE_SEARCH_API_KEY"):
            log.debug("BRAVE_SEARCH_API_KEY already set in environment — credentials file value ignored")

        api_key = creds.get("api_key") or creds.get("APCA_API_KEY_ID")
        secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError(
                f"credentials file at {creds_path} must contain "
                f"'api_key' and 'secret_key'"
            )
        return api_key, secret_key

    def _build_strategies(self) -> dict[str, Strategy]:
        """Instantiate strategies from config via the registry.

        To add a new strategy: register it in get_strategy() in base_strategy.py
        and add its name to config.json active_strategies + strategy_params.
        No changes needed here.
        """
        return {
            name: get_strategy(name, self._config.strategy.strategy_params.get(name, {}))
            for name in self._config.strategy.active_strategies
        }
