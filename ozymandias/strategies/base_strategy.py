"""
strategies/base_strategy.py
============================
Abstract base class for all trading strategies, shared data types, and the
strategy registry.

Architecture note — entry pipeline
------------------------------------
All entry opportunities originate from Claude's ``new_opportunities`` output in
the slow-loop reasoning cache.  The strategy layer's production role is:

  1. ``apply_entry_gate()`` — TA veto on Claude's recommendations before scoring.
  2. ``evaluate_position()`` — hold/scale/exit decisions on open positions.
  3. ``suggest_exit()``      — specific exit order parameters.

``generate_signals()`` is defined here as an extension point for autonomous
TA-based entry generation but is **not currently wired into the medium loop**.
If a future phase adds TA signal generation as a supplementary entry source,
the wiring point is ``_medium_loop_cycle`` Step 3 in ``orchestrator.py``.
Until then, strategies that do not wish to implement it may rely on the default
no-op return.

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
    direction: str          # "long" | "short" — both supported by the type;
                            # current generate_signals() implementations are
                            # long-only. If short signal generation is added,
                            # stops and targets must be inverted accordingly.
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

    Subclasses must implement :meth:`apply_entry_gate`, :meth:`evaluate_position`,
    and :meth:`suggest_exit`.  These three methods are the active production path.

    :meth:`generate_signals` is an optional extension point for autonomous TA-based
    entry generation.  It is not called by the orchestrator today — see module
    docstring for the wiring note.  The default implementation returns ``[]``.

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

    async def generate_signals(
        self,
        symbol: str,
        market_data: pd.DataFrame,
        indicators: dict,
    ) -> list[Signal]:
        """
        Produce entry signals for *symbol* given current OHLCV data and
        technical indicators.

        **Not currently called by the orchestrator.** Entry opportunities come
        exclusively from Claude's reasoning cache.  This method is an extension
        point: if a future phase wires autonomous TA signal generation into the
        medium loop, override this method in the strategy subclass.  The default
        returns an empty list (no signals), which is the correct no-op behaviour
        for strategies that rely solely on Claude-directed entries.

        If short signal generation is added, stop/target calculations must be
        direction-aware (stops above entry for shorts, targets below).

        Parameters
        ----------
        symbol:
            Ticker.
        market_data:
            OHLCV DataFrame (lowercase columns).
        indicators:
            Nested signals sub-dict from ``generate_signal_summary()['signals']``.
        """
        return []

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

    # ---------------------------------------------------------------------------
    # Strategy behavioural traits — override in subclasses.
    # These properties are the single source of truth for strategy-specific
    # behaviour that the orchestrator, ranker, and risk manager need to know.
    # To add a new strategy behaviour, add one property here and implement it
    # in each concrete class; no other file needs to change.
    # ---------------------------------------------------------------------------

    @property
    def is_intraday(self) -> bool:
        """True if this strategy opens and closes within a single session.

        Intraday strategies are subject to PDT rules and get forced EOD close.
        Swing strategies that hold overnight return False.
        """
        return True  # safe default; swing overrides to False

    @property
    def uses_market_orders(self) -> bool:
        """True if this strategy may use market orders at high conviction.

        Strategies that require precise limit entries (e.g. swing) return False.
        """
        return False  # safe default; momentum overrides to True

    @property
    def blocks_eod_entries(self) -> bool:
        """True if new entries should be blocked in the last 5 minutes of session.

        Intraday momentum strategies block entries near close to avoid being
        forced into an EOD exit with no time to fill. Swing strategies may
        enter near close intentionally.
        """
        return False  # safe default; momentum overrides to True

    @property
    def dead_zone_exempt(self) -> bool:
        """True if this strategy is exempt from the midday dead zone (11:30–14:30 ET).

        The dead zone was designed to suppress intraday momentum entries during the
        low-volume noon lull, where whipsaw risk is highest. Multi-day swing theses
        are not affected by hour-of-day volatility patterns and should be exempt.
        To add a new strategy: return True here to bypass the dead zone gate.
        """
        return False  # safe default; swing overrides to True

    @abstractmethod
    def apply_entry_gate(
        self,
        action: str,
        signals: dict,
    ) -> tuple[bool, str]:
        """Strategy-specific hard filter evaluated before scoring.

        Called by OpportunityRanker.apply_hard_filters() for each opportunity.
        Returns (True, "") to pass, or (False, reason) to reject.

        The ranker passes the raw signals sub-dict (same as generate_signals()
        receives). The action string is the Claude action ("buy" / "sell_short").

        To add a new strategy-specific filter, implement it here — no changes
        to opportunity_ranker.py are needed.
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

        Signal names are direction-agnostic: RiskManager inverts the signal
        logic based on position direction (e.g. "vwap_crossover" fires on
        price-below-VWAP for longs, price-above-VWAP for shorts).

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

    def override_atr_multiplier(self) -> float:
        """ATR trailing stop multiplier for quant override exits.

        Reads override_atr_multiplier from strategy params; defaults to 2.0.
        Extension point: add to _DEFAULT_PARAMS and config.json strategy_params.
        """
        return float(self._params.get("override_atr_multiplier", 2.0))

    def override_vwap_volume_threshold(self) -> float:
        """Volume ratio floor for VWAP crossover override exit.

        Reads override_vwap_volume_threshold from strategy params; defaults to 1.3.
        Extension point: add to _DEFAULT_PARAMS and config.json strategy_params.
        """
        return float(self._params.get("override_vwap_volume_threshold", 1.3))

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

    Extension point: To add a new strategy, add one entry to *registry* below
    and register its class — no other files need to change.
    """
    # Import here to avoid circular imports at module load time.
    from ozymandias.strategies.momentum_strategy import MomentumStrategy
    from ozymandias.strategies.swing_strategy import SwingStrategy

    # To add a new strategy, add one entry here and implement the Strategy ABC.
    registry: dict[str, type[Strategy]] = {
        "momentum": MomentumStrategy,
        "swing": SwingStrategy,
    }
    if name not in registry:
        raise ValueError(
            f"Unknown strategy '{name}'. Available: {sorted(registry)}"
        )
    return registry[name](params)
