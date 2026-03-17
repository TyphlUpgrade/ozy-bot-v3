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
from ozymandias.intelligence.technical_analysis import compute_composite_score

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

# Maps broker action string → composite score direction.
# Add one entry here to support a new action type; scoring logic is unchanged.
_ACTION_TO_DIRECTION: dict[str, str] = {
    "buy":        "long",
    "sell_short": "short",
}

# Strategy gate lookup tables — maps action → the indicator value that disqualifies
# the entry.  To support a new action type, add one entry here; gate logic is unchanged.
_MOMENTUM_WRONG_VWAP: dict[str, str] = {
    "buy":        "below",   # longs need price above VWAP
    "sell_short": "above",   # shorts need price below VWAP
}
_SWING_WRONG_TREND: dict[str, str] = {
    "buy":        "bearish_aligned",   # longs avoid downtrends
    "sell_short": "bullish_aligned",   # shorts avoid uptrends
}


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
    require_strong_entry: bool = False  # if True, raise min_signals_for_entry by 1 for this trade


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
        self._min_conviction = float(cfg.get("min_conviction_threshold", 0.10))  # sanity floor
        self._min_technical_score = float(cfg.get("min_technical_score", 0.30))  # TA quality floor
        # Strategy-specific TA minimum gates — checked inside apply_hard_filters
        self._momentum_min_rvol = float(cfg.get("momentum_min_rvol", 1.0))
        self._momentum_require_vwap_above = bool(cfg.get("momentum_require_vwap_above", True))
        self._swing_block_bearish_trend = bool(cfg.get("swing_block_bearish_trend", True))
        # Symbols that may appear on the watchlist for market context but must never be entered.
        no_entry_raw = cfg.get("no_entry_symbols", [
            "SPY", "QQQ", "IWM", "DIA",
            "VXX", "UVXY", "SVXY", "VIXY",
            "TLT", "GLD", "SLV", "USO",
        ])
        self._no_entry_symbols: frozenset[str] = frozenset(s.upper() for s in no_entry_raw)

    # ------------------------------------------------------------------
    # Sub-scores
    # ------------------------------------------------------------------

    def _risk_adjusted_return(
        self, entry: float, exit_: float, stop: float
    ) -> float:
        """
        reward-to-risk ratio, capped at 5:1 and normalised to [0, 1].

        Geometry-agnostic: works for longs (stop < entry < exit_),
        shorts (exit_ < entry < stop), and any future direction.
        Returns 0.0 when the geometry is invalid (stop and exit on same side of entry).
        """
        if entry <= 0:
            return 0.0
        risk = abs(entry - stop)
        reward = abs(exit_ - entry)
        if risk == 0:
            return 0.0
        # Stop and exit must be on opposite sides of entry.
        if (stop > entry) == (exit_ > entry):
            return 0.0
        return min((reward / risk) / _MAX_REWARD_RISK_RATIO, 1.0)

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
        # Recompute composite score with direction so shorts are evaluated against
        # bearish signal strength.  Falls back to the cached long-biased score when
        # raw signals are unavailable (e.g. symbol missing from technical_signals).
        action = opportunity.get("action", "buy")
        direction = _ACTION_TO_DIRECTION.get(action, "long")
        technical_score = (
            compute_composite_score(nested_signals, direction=direction)
            if nested_signals
            else float(sig_summary.get("composite_technical_score", 0.0))
        )

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
            require_strong_entry=bool(opportunity.get("require_strong_entry", False)),
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
            Reserved for future use; not currently used inside this method.
        technical_signals:
            Optional signal map; used only for the volume filter.

        Returns
        -------
        (passes, rejection_reason)
        """
        symbol = opportunity.get("symbol", "")
        _is_open = market_hours_fn if market_hours_fn is not None else is_market_open

        # 0. No-entry symbol guard — broad-market/volatility ETFs used for context only
        if symbol.upper() in self._no_entry_symbols:
            return False, f"{symbol}: in no_entry_symbols list (market-context instrument, not tradeable)"

        # 1. Minimum conviction threshold
        conviction = float(opportunity.get("conviction", 0.0))
        if conviction < self._min_conviction:
            return False, f"{symbol}: conviction {conviction:.2f} below threshold {self._min_conviction:.2f}"

        # 2. Minimum composite technical score floor — computed with direction so
        #    short opportunities are evaluated against bearish signal strength,
        #    not penalised for the absence of bullish signals.
        if technical_signals is not None:
            sig_summary = technical_signals.get(symbol, {})
            raw_signals = sig_summary.get("signals", {})
            direction = _ACTION_TO_DIRECTION.get(opportunity.get("action", "buy"), "long")
            tech_score = (
                compute_composite_score(raw_signals, direction=direction)
                if raw_signals
                else float(sig_summary.get("composite_technical_score", 0.0))
            )
            if tech_score < self._min_technical_score:
                return (
                    False,
                    f"{symbol}: composite_technical_score {tech_score:.2f} below floor "
                    f"{self._min_technical_score:.2f}",
                )

        # 2b. Strategy-specific TA minimum gates
        # These are deterministic floors independent of the composite score:
        # - Momentum requires minimum volume participation and price above VWAP.
        # - Swing rejects entries when the long-term trend is fully bearish-aligned.
        if technical_signals is not None:
            strategy = opportunity.get("strategy", "")
            action = opportunity.get("action", "buy")
            sig_outer = technical_signals.get(symbol, {})
            sig = sig_outer.get("signals", {})

            if strategy == "momentum":
                rvol = sig.get("volume_ratio")
                if rvol is not None and rvol < self._momentum_min_rvol:
                    return (
                        False,
                        f"{symbol}: momentum RVOL {rvol:.2f} below floor "
                        f"{self._momentum_min_rvol:.2f} — no volume participation",
                    )
                if self._momentum_require_vwap_above:
                    wrong_vwap = _MOMENTUM_WRONG_VWAP.get(action)
                    if wrong_vwap and sig.get("vwap_position", "") == wrong_vwap:
                        return False, f"{symbol}: momentum {action} rejected — price {wrong_vwap} VWAP"

            elif strategy == "swing":
                if self._swing_block_bearish_trend:
                    wrong_trend = _SWING_WRONG_TREND.get(action)
                    if wrong_trend and sig.get("trend_structure", "") == wrong_trend:
                        return (
                            False,
                            f"{symbol}: swing {action} rejected — {wrong_trend} trend",
                        )

        # 3. Regular-hours check
        if not _is_open():
            return False, f"{symbol}: market not in regular hours"

        # 4. Buying power
        position_size_pct = float(opportunity.get("position_size_pct", 0.05))
        required = account_info.equity * position_size_pct
        if account_info.buying_power < required:
            return (
                False,
                f"{symbol}: insufficient buying power "
                f"({account_info.buying_power:.2f} < {required:.2f} required)",
            )

        # 5. Max concurrent positions
        if len(portfolio.positions) >= self._max_positions:
            return (
                False,
                f"{symbol}: max concurrent positions ({self._max_positions}) reached",
            )

        # 5a. Per-symbol duplicate guard — never enter a symbol already in portfolio
        if any(p.symbol == symbol for p in portfolio.positions):
            return False, f"{symbol}: position already open in portfolio"

        # 6. (PDT check removed — a new entry is never a day trade by itself.
        #     The day trade occurs only when the position is closed same-day.
        #     PDT gating happens in validate_entry at close time.)

        # 7. Average daily volume
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
                # Already-open rejections are expected every medium cycle while Claude's
                # reasoning result is stale — log at DEBUG to avoid repetitive INFO spam.
                level = logging.DEBUG if "already open in portfolio" in reason else logging.INFO
                logger.log(level, "Hard filter rejected %s: %s", symbol, reason)
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
            tech_score = float(signals.get("composite_technical_score", 0.5))

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
