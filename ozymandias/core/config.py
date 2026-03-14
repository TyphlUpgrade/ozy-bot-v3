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
    name: str = "alpaca"
    environment: str = "paper"
    credentials_file: str = "credentials.enc"
    base_url_paper: str = "https://paper-api.alpaca.markets"
    base_url_live: str = "https://api.alpaca.markets"


@dataclass
class RiskConfig:
    max_position_pct: float = 0.20
    max_concurrent_positions: int = 8
    max_daily_loss_pct: float = 0.02
    per_trade_max_loss_pct: float = 0.03
    pdt_buffer: int = 1
    min_equity_for_trading: float = 25500.0


@dataclass
class SchedulerConfig:
    fast_loop_sec: int = 10
    medium_loop_sec: int = 120
    slow_loop_check_sec: int = 300
    slow_loop_max_interval_sec: int = 3600
    slow_loop_price_move_threshold_pct: float = 2.0


@dataclass
class ClaudeConfig:
    model: str = "claude-sonnet-4-20250514"
    max_tokens_per_cycle: int = 4096
    prompt_version: str = "v3.3.0"
    tier1_max_symbols: int = 12
    tier2_max_symbols: int = 28


@dataclass
class RankerConfig:
    weight_ai: float = 0.35
    weight_technical: float = 0.30
    weight_risk: float = 0.20
    weight_liquidity: float = 0.15


@dataclass
class StrategyConfig:
    active_strategies: list[str] = field(default_factory=lambda: ["momentum", "swing"])
    momentum_params: dict = field(default_factory=dict)
    swing_params: dict = field(default_factory=dict)


@dataclass
class Config:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    ranker: RankerConfig = field(default_factory=RankerConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    timezone: str = "America/New_York"

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
