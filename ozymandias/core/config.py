"""
Configuration loader with typed access via dataclasses.
Loads from config/config.json and provides defaults for missing keys.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Nested config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BrokerConfig:
    name: str = "alpaca"                               # broker backend; only "alpaca" is supported
    environment: str = "paper"                         # "paper" or "live"; selects which base_url is used
    credentials_file: str = "credentials.enc"          # Fernet-encrypted credentials file in config dir
    credentials_key_file: str = "~/.ozy_key"           # path to Fernet key file for credentials decryption
    base_url_paper: str = "https://paper-api.alpaca.markets"
    base_url_live: str = "https://api.alpaca.markets"


@dataclass
class RiskConfig:
    max_position_pct: float = 0.20          # max fraction of portfolio equity per single position
    max_concurrent_positions: int = 8       # hard cap on open positions; blocks new entries above this
    max_daily_loss_pct: float = 0.02        # halt trading for the day when loss exceeds this fraction of equity
    per_trade_max_loss_pct: float = 0.03    # max stop-loss distance as fraction of equity; used by position sizer
    pdt_buffer: int = 1                     # PDT day-trades to hold in reserve as emergency exits (of 3 allowed)
    min_equity_for_trading: float = 25500.0 # block all new entries below this equity; FINRA PDT floor is $25k
    # Short position fast-loop exit controls (symmetric to long-side ATR/VWAP overrides)
    short_atr_stop_multiplier: float = 2.0          # ATR trail for shorts: stop = intraday_low + ATR × multiplier
    short_vwap_exit_enabled: bool = True             # when True, price crossing above VWAP triggers buy-to-cover
    short_vwap_exit_volume_threshold: float = 1.3   # VWAP crossover exit requires volume_ratio above this level
    # ATR-based position size cap
    atr_position_size_cap_enabled: bool = True  # cap Claude's requested size when ATR-implied risk exceeds max_risk_per_trade_pct
    max_risk_per_trade_pct: float = 0.02        # max portfolio fraction at risk per trade (ATR stop-out scenario)


@dataclass
class SchedulerConfig:
    fast_loop_sec: int = 10                          # interval for fill polling, quant overrides, position sync
    medium_loop_sec: int = 120                       # interval for TA scans, ranking, and order execution
    slow_loop_check_sec: int = 60                    # how often to evaluate slow-loop triggers (not how often Claude runs); lower = faster trigger detection, zero extra cost
    slow_loop_max_interval_sec: int = 3600           # time-ceiling trigger: Claude runs at least this often during market hours
    slow_loop_price_move_threshold_pct: float = 1.5  # price-move trigger: fires Claude if any tier-1 symbol moves this much since last call
    conservative_startup_mode_min: int = 10          # no new entries for this many minutes after reconciliation errors on startup
    # Dead zone: block new entries during midday low-volume window (ET times, "HH:MM" format)
    dead_zone_start_et: str = "11:30"
    dead_zone_end_et: str = "14:30"
    # RVOL-conditional dead zone bypass: lifts the time gate when SPY volume is elevated,
    # indicating an active market session despite midday hours (Fed, macro events, catalysts).
    dead_zone_rvol_bypass_enabled: bool = True
    dead_zone_rvol_bypass_threshold: float = 1.5        # SPY RVOL must be >= this to lift the dead zone for all symbols
    dead_zone_symbol_rvol_bypass_threshold: float = 2.0  # per-symbol RVOL must be >= this to lift the dead zone for that symbol only (sector spikes, individual catalysts)
    entry_attempts_per_cycle: int = 3                # max ranked candidates to attempt per medium cycle before giving up
    limit_order_timeout_sec: int = 300               # cancel unfilled limit orders after this many seconds (default 5 min)
    swing_limit_order_timeout_sec: int = 1200        # longer timeout for swing strategy limit entries (default 20 min); swing theses are multi-day and need more time to fill at a tight spread
    market_order_conviction_threshold: float = 0.80  # use market order (immediate fill) for momentum entries at or above this conviction
    min_hold_before_override_min: int = 5            # quant overrides cannot fire within this many minutes of position entry
    bypass_market_hours: bool = False                # when True, skip all market-hours gates (loop guards, dead zone, session check); for off-hours testing only
    disable_conservative_mode: bool = False          # when True, skip conservative startup mode; use after manually closing broker positions that triggered reconciliation errors
    bars_cache_ttl_sec: int = 110                    # yfinance bars cache TTL; must be < medium_loop_sec to ensure fresh bars every cycle (medium_loop_sec=120)
    yfinance_fetch_stagger_max_sec: float = 0.5      # max random sleep (seconds) before each cache-miss fetch; spreads burst of parallel requests to avoid rate limits
    max_entry_defer_cycles: int = 5                  # drop a deferred opportunity after this many consecutive entry_conditions misses; prevents indefinite deferral on stale Claude thesis
    max_filter_rejection_cycles: int = 3             # suppress a symbol for the rest of the session after it fails hard filters this many times; stops Claude re-proposing RVOL/volume failures every cycle
    position_profit_trigger_pct: float = 0.015       # fire a Claude position review when unrealised gain exceeds this fraction of avg_cost; re-arms each time gain grows by another interval
    near_target_cooldown_sec: int = 1800             # suppress near_target re-firing for this many seconds after Claude reviews and holds; prevents repeated calls while price oscillates near the target level
    override_exit_cooldown_min: int = 20             # extended re-entry cooldown (minutes) after a quant-override exit; longer than re_entry_cooldown_min because the momentum signal structurally failed and needs time to reset
    fetch_failure_removal_threshold: int = 3         # auto-remove a watchlist symbol after this many consecutive medium-loop fetch failures; catches delisted or dead tickers without requiring Claude to see them
    # Phase 17 — parallel medium loop fetch
    medium_loop_scan_concurrency: int = 10           # max concurrent yfinance + TA worker tasks in the medium loop; balances throughput vs rate-limit pressure
    # Phase 17 — macro/sector move triggers
    macro_move_trigger_pct: float = 1.0              # SPY/QQQ/IWM move threshold (%) anchored to last Claude call; fires market_move trigger
    macro_move_symbols: list = field(default_factory=lambda: ["SPY", "QQQ", "IWM"])  # broad-market indices monitored for macro_move trigger
    sector_move_trigger_pct: float = 1.5             # sector ETF move threshold (%) anchored to last Claude call; fires sector_move trigger
    sector_exposure_threshold_factor: float = 0.7   # sector_move only fires when portfolio has ≥ this fraction of max_concurrent_positions in the sector
    macro_rsi_panic_threshold: int = 25              # SPY RSI ≤ this fires market_rsi_extreme:panic trigger (market selloff)
    macro_rsi_euphoria_threshold: int = 72           # SPY RSI ≥ this fires market_rsi_extreme:euphoria trigger (overheating)
    macro_rsi_rearm_band: int = 5                    # RSI must recover by this many points before the extreme trigger can re-fire
    watchlist_refresh_interval_min: int = 120        # proactive watchlist rebuild interval (minutes); 0 disables watchlist_stale trigger
    watchlist_rebuild_on_restart: bool = False       # if True, always rebuild watchlist on startup regardless of when the last build ran; overrides the persisted build timestamp
    watchlist_build_parse_failure_retry_min: int = 3  # retry interval after a parse failure (Claude returned prose instead of JSON); shorter than probe_min because parse failures are transient
    require_watchlist_before_reasoning: bool = False  # if True, defer reasoning until the watchlist build completes when both co-fire; ensures Claude reasons on fresh candidates
    no_opportunity_streak_warn_threshold: int = 8    # log a gate-breakdown WARN when this many consecutive medium loops produce zero ranked candidates; helps diagnose whether the watchlist or a specific gate is the bottleneck
    pre_market_warmup_min: int = 10                  # minutes before next market open to run a cache-warming Claude cycle; 0 disables; allows starting bot hours early with no penalty


@dataclass
class AIFallbackConfig:
    enabled: bool = True                        # whether to fall back to Gemini when Claude is unavailable
    fallback_model: str = "gemini-2.0-flash"   # Google Gemini Flash — fast, high-availability fallback
    overload_retries: int = 3                   # 529 retry attempts before switching to fallback (3s→6s→12s)
    overload_base_sec: float = 3.0              # initial delay for 529 retries
    overload_max_sec: float = 12.0              # max delay for 529 retries
    server_error_base_sec: float = 30.0         # initial delay for non-overload 5xx
    server_error_max_sec: float = 600.0         # max delay for non-overload 5xx
    circuit_breaker_threshold: int = 3          # consecutive overload fallbacks → skip Claude entirely
    circuit_breaker_probe_min: int = 10         # minutes between Claude probe attempts when circuit is open


@dataclass
class ClaudeConfig:
    model: str = "claude-sonnet-4-20250514"    # Anthropic model ID for all reasoning calls
    max_tokens_per_cycle: int = 8192           # hard ceiling for reasoning output — set to model max so it never truncates well-formed responses
    prompt_version: str = "v3.5.0"            # versioned subdirectory under config/prompts/ loaded for all templates
    tier1_max_symbols: int = 12               # max tier-1 watchlist symbols passed to Claude with full indicator detail
    tier2_max_symbols: int = 28               # max tier-2 symbols passed as watchlist replenishment candidates
    watchlist_max_entries: int = 60           # hard cap on total watchlist entries; lowest-scoring entries pruned first; open positions always protected
    watchlist_build_target: int = 8           # max new symbols Claude may add per watchlist build; lower = more selective, less daily churn
    max_reasoning_interval_min: int = 60      # time-ceiling trigger: Claude runs at least this often during market hours
    news_max_age_hours: int = 168             # age gate for watchlist_news passed to Claude; adapter filters to this window (default 7 days)
    news_max_items_per_symbol: int = 3        # headline cap per symbol sent to Claude; controls token budget
    # Phase 15: recommendation outcome tracker — max age for filled/cancelled entries before omitting from context
    recommendation_outcome_max_age_min: int = 60
    # Phase 15: number of recent close records included in Claude's execution history context
    recent_executions_count: int = 5
    # Phase 15: minimum trades required before compute_session_stats returns non-empty stats
    execution_stats_min_trades: int = 3
    min_call_interval_sec: float = 3.0   # minimum seconds between successive Claude API calls; proactively prevents RPM rate-limit hits from burst of position reviews or thesis challenges
    # Phase 17 — adaptive reasoning cache TTL (minutes) by market regime
    cache_max_age_default_min: int = 60    # normal market conditions
    cache_max_age_stressed_min: int = 20   # SPY RSI in stress zone (≤ cache_stress_rsi_low)
    cache_max_age_panic_min: int = 10      # SPY RSI in panic zone (≤ cache_panic_rsi_low)
    cache_max_age_euphoria_min: int = 15   # SPY RSI in euphoria zone (≥ cache_euphoria_rsi_high)
    cache_stress_rsi_low: int = 30         # SPY RSI floor for stress regime
    cache_panic_rsi_low: int = 25          # SPY RSI floor for panic regime
    cache_euphoria_rsi_high: int = 72      # SPY RSI ceiling for euphoria regime
    # Phase 20 — Haiku context compressor
    compressor_enabled: bool = True              # when True, Haiku pre-screens watchlist candidates before Sonnet context assembly
    compressor_model: str = "claude-haiku-4-5-20251001"  # Haiku model ID for pre-screening
    compressor_max_symbols_out: int = 18         # max symbols Haiku returns; should match tier1_max_symbols
    compressor_max_tokens: int = 512             # Haiku output token budget; compressor output is selected_symbols + notes + needs_sonnet flags
    last_view_max_age_days: int = 7              # max age (days) of WatchlistEntry.last_view before it is excluded from context as stale
    api_call_timeout_sec: float = 200.0          # asyncio.wait_for timeout for Claude API calls; 8192-token responses can take 150-180s
    macro_news_max_items: int = 2                # headline cap for SPY/QQQ macro_news; explains *why* broad indicators are moving (geopolitical, Fed, etc.)
    # Phase 22 — split-call architecture
    split_reasoning_enabled: bool = True         # when True, position reviews run as a separate compact call before opportunity discovery
    review_call_max_tokens: int = 4096           # output ceiling for the position review call; 4096 safely covers up to ~20 positions at compact depth
    review_call_verbose: bool = False            # when True, position review prompt requests full prose reasoning (stop rationale, bear case, gain-protection analysis); compact two-sentence schema when False
    # Phase 22 — graceful degradation tiers (opportunity call only)
    # NOTE: reasoning_tier* uses the "reasoning_tier" prefix to avoid collision with
    # tier2_max_symbols (line above), which controls watchlist tier-2 slot count.
    reasoning_tier2_max_symbols: int = 8         # Sonnet reduced-context symbol cap
    reasoning_tier3_max_symbols: int = 5         # Haiku emergency symbol cap
    reasoning_tier2_max_tokens: int = 4096       # Sonnet reduced output ceiling
    reasoning_tier3_max_tokens: int = 1024       # Haiku emergency output ceiling
    reasoning_tier3_model: str = "claude-haiku-4-5-20251001"  # emergency model for Tier 3
    tier_downgrade_failures: int = 2             # consecutive opportunity-call failures before dropping one tier
    tier_upgrade_probe_min: int = 15             # minutes since last degradation before attempting tier upgrade


@dataclass
class RankerConfig:
    # Opportunity composite score weights — must sum to 1.0
    weight_ai: float = 0.35           # Claude conviction component of composite opportunity score
    weight_technical: float = 0.30    # composite TA score component
    weight_risk: float = 0.20         # risk-adjusted expected value component
    weight_liquidity: float = 0.15    # volume/liquidity quality component
    min_conviction_threshold: float = 0.10   # sanity floor: rejects degenerate zero-conviction Claude output
    min_composite_score: float = 0.45        # composite score floor: rejects entries where multiple components are simultaneously weak; set below the individual gate thresholds' natural composite floor (~0.38) with margin; revisit after 30+ trades
    thesis_challenge_size_threshold: float = 0.20  # position_size_pct >= this triggers adversarial Claude review before entry
    thesis_challenge_ttl_min: int = 10  # minutes to cache a thesis challenge result before re-evaluating the same symbol
    thesis_challenge_max_penalty: float = 0.35  # max fractional size reduction from thesis challenge (0.35 = up to 35% smaller)
    max_entry_drift_pct: float = 0.015   # skip long buy if current price > suggested_entry × (1 + this); avoids chasing
    max_adverse_drift_pct: float = 0.020  # skip long buy if current price < suggested_entry × (1 - this); entry level broken
    min_technical_score: float = 0.30    # hard filter floor: directional score (long_score or short_score) below this rejects entry regardless of conviction
    ta_size_factor_min: float = 0.60     # at directional_score=0, enter at this fraction of risk-sized qty; scales linearly to 1.0
    momentum_min_rvol: float = 1.0           # momentum hard gate: reject if current volume_ratio < this (ensures participation)
    momentum_require_vwap_above: bool = True  # momentum hard gate: reject if price is below VWAP at entry
    swing_block_bearish_trend: bool = True    # swing hard gate: reject if trend_structure is bearish_aligned
    max_portfolio_deployment_pct: float = 0.85  # block new entries when buying_power/equity implies this fraction of capital is deployed; 0 = disabled. Allows more concurrent small positions without exceeding equity limits.
    # Phase 19: absolute floor on Claude's filter_adjustments RVOL relaxation.
    # Claude cannot lower min_rvol below this value regardless of filter_adjustments output.
    # The ranker composite score floor (min_composite_score) is NOT adjustable by Claude.
    filter_adj_min_rvol: float = 0.5
    # filter_adjustment_decay_cycles: after this many consecutive Claude cycles where
    # filter_adjustments were elevated AND no candidates passed the ranker, the
    # adjustments are discarded and thresholds revert to config defaults. Prevents
    # Claude from self-reinforcing an overly aggressive floor (seeing its own blocks
    # as market evidence and re-raising the floor each cycle).
    filter_adjustment_decay_cycles: int = 2

    no_entry_symbols: list = field(default_factory=lambda: [
        # Broad-market and volatility ETFs used as market-context monitors only.
        # These may appear on the watchlist (tier 2) but must never be entered as trades.
        "SPY", "QQQ", "IWM", "DIA",        # major broad-market ETFs
        "VXX", "UVXY", "SVXY", "VIXY",    # volatility products (too mean-reverting / decay-prone)
        "TLT", "GLD", "SLV", "USO",        # macro/commodity context instruments
    ])


@dataclass
class UniverseScannerConfig:
    enabled: bool = True
    scan_concurrency: int = 20          # parallel yfinance fetches for universe scan;
                                        # intentionally higher than medium_loop_scan_concurrency (10)
                                        # — universe scan covers 75+ symbols and is less latency-sensitive
    max_candidates: int = 50            # top N ranked by the scanner (full ranked pool)
    max_candidates_to_claude: int = 20  # how many of the top-ranked candidates to include in
                                        # the watchlist build prompt; smaller = less token pressure
                                        # + forces Claude to focus on highest-RVOL names
    min_rvol_for_candidate: float = 0.8 # pass if RVOL ≥ this (volume-activity path)
    min_price_move_pct_for_candidate: float = 1.5  # pass if abs(roc_5) ≥ this (price-move path);
                                        # direction-agnostic: captures breakdowns and fades that
                                        # have low RVOL but meaningful price displacement
    cache_ttl_min: int = 60             # reuse scan result within same session without re-scanning


@dataclass
class SearchConfig:
    enabled: bool = True
    max_searches_per_build: int = 5     # cap on tool_use rounds per watchlist build call
    result_count_per_query: int = 5     # results returned per Brave Search query
    search_429_retry_count: int = 2     # retry Brave Search calls that hit a 429 rate limit this many times
    search_429_retry_sec: float = 5.0   # seconds to wait between Brave Search 429 retries


@dataclass
class StrategyConfig:
    active_strategies: list[str] = field(default_factory=lambda: ["momentum", "swing"])  # must match keys in get_strategy() registry
    strategy_params: dict[str, dict] = field(default_factory=dict)
    # Maps strategy name → param overrides dict.  Add one entry here per new strategy;
    # no code changes needed.  Example: {"momentum": {"min_rvol": 1.2}, "scalp": {...}}


@dataclass
class Config:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    ai_fallback: AIFallbackConfig = field(default_factory=AIFallbackConfig)
    ranker: RankerConfig = field(default_factory=RankerConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    universe_scanner: UniverseScannerConfig = field(default_factory=UniverseScannerConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    timezone: str = "America/New_York"  # used by market hours logic; all internal timestamps remain UTC

    # Path to the config directory (set by loader)
    _config_dir: Optional[Path] = field(default=None, repr=False, compare=False)

    @property
    def credentials_path(self) -> Path:
        """Absolute path to the credentials file."""
        if self._config_dir is None:
            raise RuntimeError("Config not loaded from file; _config_dir is unknown.")
        return self._config_dir / self.broker.credentials_file

    @property
    def prompts_dir(self) -> Path:
        """Absolute path to the versioned prompts directory."""
        if self._config_dir is None:
            raise RuntimeError("Config not loaded from file; _config_dir is unknown.")
        return self._config_dir / "prompts" / self.claude.prompt_version


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _merge(dataclass_instance, data: dict) -> None:
    """Recursively overlay dict values onto a dataclass instance."""
    for key, value in data.items():
        if not hasattr(dataclass_instance, key):
            continue
        attr = getattr(dataclass_instance, key)
        if hasattr(attr, "__dataclass_fields__") and isinstance(value, dict):
            _merge(attr, value)
        else:
            setattr(dataclass_instance, key, value)


def load_config(config_path: Optional[Path] = None) -> Config:
    """
    Load configuration from config.json.

    Searches for the config file in this order:
    1. Explicit ``config_path`` argument.
    2. ``<project_root>/ozymandias/config/config.json``.
    3. ``<project_root>/config/config.json``.

    Missing keys fall back to dataclass defaults.
    """
    if config_path is None:
        # Walk up from this file to find the config
        here = Path(__file__).resolve().parent  # core/
        candidates = [
            here.parent / "config" / "config.json",       # ozymandias/config/
            here.parent.parent / "config" / "config.json", # project_root/config/
        ]
        for candidate in candidates:
            if candidate.exists():
                config_path = candidate
                break

    cfg = Config()

    if config_path is not None and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        _merge(cfg, raw)
        cfg._config_dir = Path(config_path).resolve().parent
    else:
        # No file found — use defaults, set config_dir to package config/
        default_dir = Path(__file__).resolve().parent.parent / "config"
        cfg._config_dir = default_dir

    _validate_config(cfg)
    return cfg


def _validate_config(cfg: Config) -> None:
    """Raise ValueError for obviously invalid config values."""
    if not 0 < cfg.risk.max_position_pct <= 1:
        raise ValueError(f"risk.max_position_pct must be in (0, 1], got {cfg.risk.max_position_pct}")
    if cfg.risk.max_concurrent_positions < 1:
        raise ValueError("risk.max_concurrent_positions must be >= 1")
    if not 0 < cfg.risk.max_daily_loss_pct <= 1:
        raise ValueError("risk.max_daily_loss_pct must be in (0, 1]")
    if cfg.scheduler.fast_loop_sec < 1:
        raise ValueError("scheduler.fast_loop_sec must be >= 1")
    total = cfg.ranker.weight_ai + cfg.ranker.weight_technical + cfg.ranker.weight_risk + cfg.ranker.weight_liquidity
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"Ranker weights must sum to 1.0, got {total:.4f}")
