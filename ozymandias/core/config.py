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


@dataclass
class SchedulerConfig:
    fast_loop_sec: int = 10                          # interval for fill polling, quant overrides, position sync
    medium_loop_sec: int = 120                       # interval for TA scans, ranking, and order execution
    slow_loop_check_sec: int = 300                   # how often to evaluate slow-loop triggers (not how often Claude runs)
    slow_loop_max_interval_sec: int = 3600           # time-ceiling trigger: Claude runs at least this often during market hours
    slow_loop_price_move_threshold_pct: float = 2.0  # price-move trigger: fires Claude if any tier-1 symbol moves this much since last call
    conservative_startup_mode_min: int = 10          # no new entries for this many minutes after reconciliation errors on startup
    # Dead zone: block new entries during midday low-volume window (ET times, "HH:MM" format)
    dead_zone_start_et: str = "11:30"
    dead_zone_end_et: str = "14:30"
    entry_attempts_per_cycle: int = 3                # max ranked candidates to attempt per medium cycle before giving up
    limit_order_timeout_sec: int = 300               # cancel unfilled limit orders after this many seconds (default 5 min)
    market_order_conviction_threshold: float = 0.80  # use market order (immediate fill) for momentum entries at or above this conviction
    min_hold_before_override_min: int = 5            # quant overrides cannot fire within this many minutes of position entry
    bypass_market_hours: bool = False                # when True, skip all market-hours gates (loop guards, dead zone, session check); for off-hours testing only
    disable_conservative_mode: bool = False          # when True, skip conservative startup mode; use after manually closing broker positions that triggered reconciliation errors


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
    max_tokens_per_cycle: int = 4096           # token budget per reasoning call; thesis challenge uses 512 via override
    prompt_version: str = "v3.3.0"            # versioned subdirectory under config/prompts/ loaded for all templates
    tier1_max_symbols: int = 12               # max tier-1 watchlist symbols passed to Claude with full indicator detail
    tier2_max_symbols: int = 28               # max tier-2 symbols passed as watchlist replenishment candidates
    watchlist_max_entries: int = 30           # hard cap on total watchlist entries; lowest-scoring entries pruned first; open positions always protected
    max_reasoning_interval_min: int = 60      # time-ceiling trigger: Claude runs at least this often during market hours
    news_max_age_hours: int = 168             # age gate for watchlist_news passed to Claude; adapter filters to this window (default 7 days)
    news_max_items_per_symbol: int = 3        # headline cap per symbol sent to Claude; controls token budget


@dataclass
class RankerConfig:
    # Opportunity composite score weights — must sum to 1.0
    weight_ai: float = 0.35           # Claude conviction component of composite opportunity score
    weight_technical: float = 0.30    # composite TA score component
    weight_risk: float = 0.20         # risk-adjusted expected value component
    weight_liquidity: float = 0.15    # volume/liquidity quality component
    min_conviction_threshold: float = 0.10   # sanity floor: rejects degenerate zero-conviction Claude output
    thesis_challenge_size_threshold: float = 0.20  # position_size_pct >= this triggers adversarial Claude review before entry
    thesis_challenge_ttl_min: int = 10  # minutes to cache a thesis challenge result before re-evaluating the same symbol
    thesis_challenge_max_penalty: float = 0.35  # max fractional size reduction from thesis challenge (0.35 = up to 35% smaller)
    max_entry_drift_pct: float = 0.015   # skip long buy if current price > suggested_entry × (1 + this); avoids chasing
    max_adverse_drift_pct: float = 0.020  # skip long buy if current price < suggested_entry × (1 - this); entry level broken
    min_technical_score: float = 0.30    # hard filter floor: composite_technical_score below this rejects entry regardless of conviction
    ta_size_factor_min: float = 0.60     # at composite_technical_score=0, enter at this fraction of risk-sized qty; scales linearly to 1.0
    momentum_min_rvol: float = 1.0           # momentum hard gate: reject if current volume_ratio < this (ensures participation)
    momentum_require_vwap_above: bool = True  # momentum hard gate: reject if price is below VWAP at entry
    swing_block_bearish_trend: bool = True    # swing hard gate: reject if trend_structure is bearish_aligned
    no_entry_symbols: list = field(default_factory=lambda: [
        # Broad-market and volatility ETFs used as market-context monitors only.
        # These may appear on the watchlist (tier 2) but must never be entered as trades.
        "SPY", "QQQ", "IWM", "DIA",        # major broad-market ETFs
        "VXX", "UVXY", "SVXY", "VIXY",    # volatility products (too mean-reverting / decay-prone)
        "TLT", "GLD", "SLV", "USO",        # macro/commodity context instruments
    ])


@dataclass
class StrategyConfig:
    active_strategies: list[str] = field(default_factory=lambda: ["momentum", "swing"])  # must match keys in get_strategy() registry
    strategy_params: dict[str, dict] = field(default_factory=dict)
    # Maps strategy name → param overrides dict.  Add one entry here per new strategy;
    # no code changes needed.  Example: {"momentum": {"min_rvol": 1.2}, "scalp": {...}}
    swing_min_hold_hours: float = 4.0  # block Claude review exits for swing positions held less than this many hours; prevents same-session exits on intraday signal noise


@dataclass
class Config:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    ai_fallback: AIFallbackConfig = field(default_factory=AIFallbackConfig)
    ranker: RankerConfig = field(default_factory=RankerConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
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
