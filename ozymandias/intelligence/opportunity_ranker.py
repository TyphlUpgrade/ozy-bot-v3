"""
intelligence/opportunity_ranker.py
===================================
Bridges Claude's reasoning output and the technical analysis signals into a
prioritised queue of trade actions.

Composite scoring formula (weights configurable via config):
    composite = ai_conviction * W_ai
              + technical_score * W_tech
              + risk_adjusted_return * W_risk
              + liquidity_score * W_liq

Hard filters are applied before scoring — any failure removes the opportunity
from consideration entirely.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ozymandias.core.market_hours import is_market_open
from ozymandias.core.state_manager import PortfolioState
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.execution.pdt_guard import PDTGuard
from ozymandias.intelligence.claude_reasoning import ReasoningResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------
_W_AI = 0.35
_W_TECH = 0.30
_W_RISK = 0.20
_W_LIQ = 0.15

_MAX_POSITIONS = 8
_MIN_AVG_DAILY_VOLUME = 100_000
_MAX_REWARD_RISK_RATIO = 5.0


# ---------------------------------------------------------------------------
# Output data types
# ---------------------------------------------------------------------------

@dataclass
class ScoredOpportunity:
    """A ranked trade entry candidate."""
    symbol: str
    action: str                 # "buy" | "sell_short"
    strategy: str               # "momentum" | "swing" | etc.
    composite_score: float      # 0.0 – 1.0 (higher = better)
    ai_conviction: float        # raw Claude conviction 0.0 – 1.0
    technical_score: float      # composite TA score 0.0 – 1.0
    risk_adjusted_return: float # normalised reward:risk 0.0 – 1.0
    liquidity_score: float      # normalised volume score 0.0 – 1.0
    suggested_entry: float
    suggested_exit: float
    suggested_stop: float
    position_size_pct: float    # fraction of portfolio (e.g. 0.10 = 10%)
    reasoning: str


@dataclass
class ExitAction:
    """A prioritised hold / exit / adjust recommendation for an open position."""
    symbol: str
    action: str                 # "hold" | "exit" | "adjust"
    urgency: float              # 0.0 – 1.0 (higher = act sooner)
    reasoning: str
    adjusted_targets: dict | None = None  # {"profit_target": ..., "stop_loss": ...}


# ---------------------------------------------------------------------------
# Ranker
# ---------------------------------------------------------------------------

class OpportunityRanker:
    """
    Converts Claude's :class:`ReasoningResult` and TA signals into ranked
    :class:`ScoredOpportunity` and :class:`ExitAction` lists.

    Parameters
    ----------
    config:
        Optional mapping with weight overrides::

            {"w_ai": 0.35, "w_tech": 0.30, "w_risk": 0.20, "w_liq": 0.15}
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._w_ai = float(cfg.get("w_ai", _W_AI))
        self._w_tech = float(cfg.get("w_tech", _W_TECH))
        self._w_risk = float(cfg.get("w_risk", _W_RISK))
        self._w_liq = float(cfg.get("w_liq", _W_LIQ))
        self._max_positions = int(cfg.get("max_positions", _MAX_POSITIONS))
        self._min_volume = float(cfg.get("min_avg_daily_volume", _MIN_AVG_DAILY_VOLUME))

    # ------------------------------------------------------------------
    # Sub-scores
    # ------------------------------------------------------------------

    def _risk_adjusted_return(
        self, entry: float, exit_: float, stop: float
    ) -> float:
        """
        reward-to-risk ratio, capped at 5:1 and normalised to [0, 1].

        Returns 0.0 when the setup is invalid (stop >= entry).
        """
        if stop >= entry or entry <= 0:
            return 0.0
        ratio = (exit_ - entry) / (entry - stop)
        if ratio <= 0:
            return 0.0
        return min(ratio / _MAX_REWARD_RISK_RATIO, 1.0)

    def _liquidity_score(self, avg_daily_volume: float | None) -> float:
        """
        Normalise average daily volume against a 1 M-share benchmark.

        Returns 0.5 (neutral) when volume is unknown.
        """
        if avg_daily_volume is None:
            return 0.5
        return min(avg_daily_volume / 1_000_000, 1.0)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score_opportunity(
        self,
        opportunity: dict,
        technical_signals: dict[str, dict],
        account_info: AccountInfo,
        portfolio: PortfolioState,
    ) -> ScoredOpportunity:
        """
        Compute and return a :class:`ScoredOpportunity` for one candidate.

        Parameters
        ----------
        opportunity:
            One entry from :attr:`ReasoningResult.new_opportunities`.
        technical_signals:
            Mapping ``symbol → signal_dict`` from ``generate_signal_summary``.
            Keys used: ``composite_technical_score``, ``avg_daily_volume`` (optional).
            The full output of ``generate_signal_summary()`` is expected per symbol
            (top-level dict with ``composite_technical_score`` and nested ``signals``).
        """
        symbol = opportunity["symbol"]
        # Full generate_signal_summary() output: top-level has composite_technical_score;
        # per-indicator values live inside the nested "signals" sub-dict.
        sig_summary = technical_signals.get(symbol, {})
        nested_signals = sig_summary.get("signals", {})

        ai_conviction = float(opportunity.get("conviction", 0.5))
        # composite_technical_score is at the top level of the summary dict.
        technical_score = float(sig_summary.get("composite_technical_score", 0.0))

        entry = float(opportunity.get("suggested_entry", 0.0))
        exit_ = float(opportunity.get("suggested_exit", 0.0))
        stop = float(opportunity.get("suggested_stop", 0.0))
        rar = self._risk_adjusted_return(entry, exit_, stop)

        avg_vol = nested_signals.get("avg_daily_volume")
        liq = self._liquidity_score(avg_vol)

        composite = (
            ai_conviction * self._w_ai
            + technical_score * self._w_tech
            + rar * self._w_risk
            + liq * self._w_liq
        )
        composite = max(0.0, min(1.0, composite))

        return ScoredOpportunity(
            symbol=symbol,
            action=opportunity.get("action", "buy"),
            strategy=opportunity.get("strategy", ""),
            composite_score=composite,
            ai_conviction=ai_conviction,
            technical_score=technical_score,
            risk_adjusted_return=rar,
            liquidity_score=liq,
            suggested_entry=entry,
            suggested_exit=exit_,
            suggested_stop=stop,
            position_size_pct=float(opportunity.get("position_size_pct", 0.05)),
            reasoning=opportunity.get("reasoning", ""),
        )

    # ------------------------------------------------------------------
    # Hard filters
    # ------------------------------------------------------------------

    def apply_hard_filters(
        self,
        opportunity: dict,
        account_info: AccountInfo,
        portfolio: PortfolioState,
        pdt_guard: PDTGuard,
        market_hours_fn=None,
        orders: list | None = None,
        technical_signals: dict[str, dict] | None = None,
    ) -> tuple[bool, str]:
        """
        Run pre-scoring hard filters.  Any failure removes the opportunity.

        Parameters
        ----------
        market_hours_fn:
            Callable that returns True when the market is in regular hours.
            Defaults to :func:`ozymandias.core.market_hours.is_market_open`.
        orders:
            Full order list forwarded to :meth:`PDTGuard.can_day_trade`.
            When omitted, an empty list is used (best-effort PDT check).
        technical_signals:
            Optional signal map; used only for the volume filter.

        Returns
        -------
        (passes, rejection_reason)
        """
        symbol = opportunity.get("symbol", "")
        _is_open = market_hours_fn if market_hours_fn is not None else is_market_open

        # 1. Regular-hours check
        if not _is_open():
            return False, f"{symbol}: market not in regular hours"

        # 2. Buying power
        position_size_pct = float(opportunity.get("position_size_pct", 0.05))
        required = account_info.equity * position_size_pct
        if account_info.buying_power < required:
            return (
                False,
                f"{symbol}: insufficient buying power "
                f"({account_info.buying_power:.2f} < {required:.2f} required)",
            )

        # 3. Max concurrent positions
        if len(portfolio.positions) >= self._max_positions:
            return (
                False,
                f"{symbol}: max concurrent positions ({self._max_positions}) reached",
            )

        # 4. PDT check
        allowed, reason = pdt_guard.can_day_trade(
            symbol, orders or [], portfolio
        )
        if not allowed:
            return False, f"{symbol}: PDT limit — {reason}"

        # 5. Average daily volume
        # avg_daily_volume lives inside the nested "signals" sub-dict of the summary output.
        sig_summary = (technical_signals or {}).get(symbol, {})
        nested = sig_summary.get("signals", {})
        avg_vol = nested.get("avg_daily_volume") or opportunity.get("avg_daily_volume")
        if avg_vol is not None and avg_vol < self._min_volume:
            return (
                False,
                f"{symbol}: avg daily volume {avg_vol:,.0f} < "
                f"minimum {self._min_volume:,.0f}",
            )

        return True, ""

    # ------------------------------------------------------------------
    # Pipeline entry points
    # ------------------------------------------------------------------

    def rank_opportunities(
        self,
        reasoning_result: ReasoningResult,
        technical_signals: dict[str, dict],
        account_info: AccountInfo,
        portfolio: PortfolioState,
        pdt_guard: PDTGuard,
        market_hours_fn=None,
        orders: list | None = None,
    ) -> list[ScoredOpportunity]:
        """
        Full ranking pipeline:
        1. Extract candidates from Claude's reasoning output.
        2. Apply hard filters.
        3. Score remaining candidates.
        4. Sort by composite score descending.

        Parameters
        ----------
        market_hours_fn:
            Callable → bool for market-hours check.  Defaults to
            :func:`~ozymandias.core.market_hours.is_market_open`.
        orders:
            Order list forwarded to the PDT guard.
        """
        scored: list[ScoredOpportunity] = []
        for opp in reasoning_result.new_opportunities:
            symbol = opp.get("symbol", "?")
            passes, reason = self.apply_hard_filters(
                opp,
                account_info,
                portfolio,
                pdt_guard,
                market_hours_fn,
                orders,
                technical_signals,
            )
            if not passes:
                logger.debug("Hard filter rejected %s: %s", symbol, reason)
                continue
            scored.append(
                self.score_opportunity(opp, technical_signals, account_info, portfolio)
            )

        scored.sort(key=lambda s: s.composite_score, reverse=True)
        logger.info(
            "rank_opportunities: %d candidates, %d passed filters",
            len(reasoning_result.new_opportunities),
            len(scored),
        )
        return scored

    def rank_exit_actions(
        self,
        reasoning_result: ReasoningResult,
        technical_signals: dict[str, dict],
    ) -> list[ExitAction]:
        """
        Convert Claude's position reviews into prioritised :class:`ExitAction` list.

        Urgency heuristic:
        - ``exit``   → 1.0 (always act immediately)
        - ``adjust`` → 0.5 + 0.5 × (1 − technical_score)  (higher when TA is weak)
        - ``hold``   → max(0, 1 − technical_score)         (lower when TA is strong)

        The list is sorted by urgency descending.
        """
        actions: list[ExitAction] = []
        for review in reasoning_result.position_reviews:
            symbol = review.get("symbol", "")
            action = review.get("action", "hold").lower()

            signals = technical_signals.get(symbol, {})
            tech_score = float(signals.get("composite_score", 0.5))

            if action == "exit":
                urgency = 1.0
            elif action == "adjust":
                urgency = 0.5 + 0.5 * (1.0 - tech_score)
            else:  # hold (or anything unexpected)
                urgency = max(0.0, 1.0 - tech_score)

            urgency = max(0.0, min(1.0, urgency))

            actions.append(
                ExitAction(
                    symbol=symbol,
                    action=action,
                    urgency=urgency,
                    reasoning=review.get(
                        "updated_reasoning", review.get("reasoning", "")
                    ),
                    adjusted_targets=review.get("adjusted_targets"),
                )
            )

        actions.sort(key=lambda a: a.urgency, reverse=True)
        return actions
