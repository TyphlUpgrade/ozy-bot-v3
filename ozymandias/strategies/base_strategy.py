"""
strategies/base_strategy.py
============================
Abstract base class for all trading strategies, shared data types, and the
strategy registry.

Signal flow::

    indicators (nested signals sub-dict from generate_signal_summary())
        ↓
    Strategy.generate_signals()   → list[Signal]    (entry candidates)
    Strategy.evaluate_position()  → PositionEval    (hold / scale / exit decision)
    Strategy.suggest_exit()       → ExitSuggestion  (specific exit order params)

Orchestrator note: strategies receive the *nested* signals sub-dict from
``generate_signal_summary()['signals']``, not the full output dict.  This is
the same convention used by ``RiskManager.evaluate_overrides()``.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ozymandias.core.state_manager import Position

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output data types
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """An entry signal produced by a strategy for a specific symbol."""
    symbol: str
    direction: str          # "long" (only direction currently supported)
    strength: float         # 0.0 – 1.0
    entry_price: float      # suggested entry (typically latest close)
    stop_price: float       # stop-loss price
    target_price: float     # profit target
    timeframe: str          # "short" | "medium"
    reasoning: str


@dataclass
class PositionEval:
    """Evaluation of an open position: what to do next."""
    symbol: str
    action: str             # "hold" | "scale_in" | "scale_out" | "exit"
    confidence: float       # 0.0 – 1.0
    reasoning: str
    adjusted_targets: dict | None = None  # {"profit_target": ..., "stop_loss": ...}


@dataclass
class ExitSuggestion:
    """Specific exit order parameters suggested by a strategy."""
    symbol: str
    exit_price: float       # 0.0 means use market price (market order)
    order_type: str         # "market" | "limit"
    urgency: float          # 0.0 – 1.0 (1.0 = execute immediately)
    reasoning: str


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class Strategy(ABC):
    """
    Common interface for all trading strategy implementations.

    Subclasses must implement :meth:`generate_signals`,
    :meth:`evaluate_position`, and :meth:`suggest_exit`.

    Parameters are stored in ``self._params`` and can be updated at runtime
    via :meth:`set_parameters`.
    """

    #: Default parameters — subclasses override this dict.
    _DEFAULT_PARAMS: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self._params: dict[str, Any] = dict(self._DEFAULT_PARAMS)
        if params:
            self._params.update(params)

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    @abstractmethod
    async def generate_signals(
        self,
        symbol: str,
        market_data: pd.DataFrame,
        indicators: dict,
    ) -> list[Signal]:
        """
        Produce entry signals for *symbol* given current OHLCV data and
        technical indicators.

        Parameters
        ----------
        symbol:
            Ticker.
        market_data:
            OHLCV DataFrame (lowercase columns).
        indicators:
            Nested signals sub-dict from ``generate_signal_summary()['signals']``.
        """

    @abstractmethod
    async def evaluate_position(
        self,
        position: Position,
        market_data: pd.DataFrame,
        indicators: dict,
    ) -> PositionEval:
        """
        Evaluate whether an open position should be held, scaled, or exited.
        """

    @abstractmethod
    async def suggest_exit(
        self,
        position: Position,
        market_data: pd.DataFrame,
        indicators: dict,
    ) -> ExitSuggestion:
        """
        Suggest specific exit parameters (price, order type, urgency) for an
        open position.
        """

    # ------------------------------------------------------------------
    # Concrete helpers
    # ------------------------------------------------------------------

    def applicable_override_signals(self) -> frozenset[str]:
        """
        Return the set of RiskManager override signal names that apply to
        positions opened by this strategy.

        Override in subclasses to restrict which fast-loop quant signals can
        trigger an exit for this strategy's positions.  The default returns
        all known signals so that any new strategy is safe by default.

        Known signal names (from RiskManager.evaluate_overrides):
            "vwap_crossover", "roc_deceleration", "momentum_score_flip",
            "atr_trailing_stop", "rsi_divergence"
        """
        return frozenset({
            "vwap_crossover",
            "roc_deceleration",
            "momentum_score_flip",
            "atr_trailing_stop",
            "rsi_divergence",
        })

    def get_parameters(self) -> dict[str, Any]:
        """Return a copy of the current strategy parameters."""
        return dict(self._params)

    def set_parameters(self, params: dict[str, Any]) -> None:
        """Update strategy parameters at runtime (merges into existing)."""
        self._params.update(params)
        log.debug("%s parameters updated: %s", self.__class__.__name__, params)

    def _p(self, key: str) -> Any:
        """Shorthand parameter accessor."""
        return self._params[key]


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

def get_strategy(name: str, params: dict | None = None) -> Strategy:
    """
    Return a strategy instance by name.

    Parameters
    ----------
    name:
        One of ``"momentum"`` or ``"swing"``.
    params:
        Optional parameter overrides forwarded to the strategy constructor.

    Raises
    ------
    ValueError
        If *name* is not a registered strategy.
    """
    # Import here to avoid circular imports at module load time.
    from ozymandias.strategies.momentum_strategy import MomentumStrategy
    from ozymandias.strategies.swing_strategy import SwingStrategy

    registry: dict[str, type[Strategy]] = {
        "momentum": MomentumStrategy,
        "swing": SwingStrategy,
    }
    if name not in registry:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {sorted(registry)}"
        )
    return registry[name](params)
