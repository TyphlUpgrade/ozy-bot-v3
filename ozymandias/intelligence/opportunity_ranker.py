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

from ozymandias.core.direction import ACTION_TO_DIRECTION, direction_from_action
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
_MIN_AVG_DAILY_DOLLAR_VOLUME = 10_000_000  # $10M/day — prevents slippage on thin names
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
    entry_conditions: dict = field(default_factory=dict)  # Claude's per-trade TA gate; empty = no check


@dataclass
class RankResult:
    """Return type of rank_opportunities (Phase 15).

    Extension point: add new result fields here; callers that only use
    .candidates continue to work unchanged.
    """
    candidates: list[ScoredOpportunity]
    # (symbol, reason_string) tuples for every hard-filter rejection.
    # Populated inside rank_opportunities from apply_hard_filters results.
    rejections: list[tuple[str, str]]


@dataclass
class ExitAction:
    """A prioritised hold / exit / adjust recommendation for an open position."""
    symbol: str
    action: str                 # "hold" | "exit" | "adjust"
    urgency: float              # 0.0 – 1.0 (higher = act sooner)
    reasoning: str
    adjusted_targets: dict | None = None  # {"profit_target": ..., "stop_loss": ...}


# ---------------------------------------------------------------------------
# Entry conditions evaluator (Phase 14)
# ---------------------------------------------------------------------------

def evaluate_entry_conditions(conditions: dict | None, signals: dict) -> tuple[bool, str]:
    """Check Claude's per-trade entry conditions against current live signals.

    Parameters
    ----------
    conditions:
        The ``entry_conditions`` dict from a Claude opportunity.  May be
        ``None`` or empty — either case returns ``(True, "")``.
    signals:
        Flat signals dict from ``_latest_indicators[symbol]`` (same shape as
        ``generate_signal_summary()["signals"]``).

    Returns
    -------
    ``(True, "")`` if all specified conditions are satisfied.
    ``(False, reason)`` on the first failing condition.

    If a required signal key is absent from *signals*, the condition is
    treated as unmet: ``(False, "signal '<key>' unavailable")``.

    Extension point: to add a new condition key, add one branch below.
    """
    if not conditions:
        return True, ""

    # require_above_vwap — longs: price must be above VWAP at execution ------
    if conditions.get("require_above_vwap"):
        val = signals.get("vwap_position")
        if val is None:
            return False, "signal 'vwap_position' unavailable"
        if val != "above":
            return False, f"require_above_vwap not met: vwap_position={val!r}"

    # require_below_vwap — shorts: price must be below VWAP at execution ----
    if conditions.get("require_below_vwap"):
        val = signals.get("vwap_position")
        if val is None:
            return False, "signal 'vwap_position' unavailable"
        if val != "below":
            return False, f"require_below_vwap not met: vwap_position={val!r}"

    # rsi_min --------------------------------------------------------------
    if "rsi_min" in conditions:
        val = signals.get("rsi")
        if val is None:
            return False, "signal 'rsi' unavailable"
        if float(val) < float(conditions["rsi_min"]):
            return False, f"rsi_min not met: RSI {float(val):.1f} < {float(conditions['rsi_min']):.1f}"

    # rsi_max --------------------------------------------------------------
    if "rsi_max" in conditions:
        val = signals.get("rsi")
        if val is None:
            return False, "signal 'rsi' unavailable"
        if float(val) > float(conditions["rsi_max"]):
            return False, f"rsi_max exceeded: RSI {float(val):.1f} > {float(conditions['rsi_max']):.1f}"

    # rsi_slope_min — longs: RSI must be rising at least this fast ----------
    # Use rsi_slope_min with a positive value (e.g. 0.5) to confirm upward RSI momentum.
    if "rsi_slope_min" in conditions:
        val = signals.get("rsi_slope_5")
        if val is None:
            return False, "signal 'rsi_slope_5' unavailable"
        threshold = float(conditions["rsi_slope_min"])
        if threshold < 0:
            logger.warning(
                "entry_conditions rsi_slope_min=%s is negative (must be positive for longs) — blocking entry",
                threshold,
            )
            return False, f"entry_condition rsi_slope_min={threshold} is invalid (must be >= 0)"
        if float(val) < threshold:
            return False, f"rsi_slope_min not met: rsi_slope_5 {float(val):.2f} < {threshold:.2f}"

    # rsi_slope_max — shorts: RSI must be falling at least this fast ---------
    # Use rsi_slope_max with a negative value (e.g. -0.5) to confirm downward RSI momentum.
    if "rsi_slope_max" in conditions:
        val = signals.get("rsi_slope_5")
        if val is None:
            return False, "signal 'rsi_slope_5' unavailable"
        threshold = float(conditions["rsi_slope_max"])
        if threshold > 0:
            logger.warning(
                "entry_conditions rsi_slope_max=%s is positive (must be negative for shorts) — blocking entry",
                threshold,
            )
            return False, f"entry_condition rsi_slope_max={threshold} is invalid (must be <= 0)"
        if float(val) > threshold:
            return False, f"rsi_slope_max exceeded: rsi_slope_5 {float(val):.2f} > {threshold:.2f}"

    # rsi_accel_min — RSI acceleration must be at least this value ---------------
    # Use a positive value (e.g. 0.5) to confirm RSI momentum is still building.
    # Use 0.0 to simply require non-negative acceleration (slope not yet flattening).
    # Primary use: swing longs at elevated RSI — ensures the move hasn't peaked.
    if "rsi_accel_min" in conditions:
        val = signals.get("rsi_accel_3")
        if val is None:
            return False, "signal 'rsi_accel_3' unavailable"
        threshold = float(conditions["rsi_accel_min"])
        if float(val) < threshold:
            return False, f"rsi_accel_min not met: rsi_accel_3 {float(val):.2f} < {threshold:.2f}"

    # rsi_accel_max — RSI acceleration must not exceed this value ---------------
    # Use a negative value (e.g. -0.5) to confirm RSI deceleration.
    # Primary uses: (1) mean-reversion fade: require deceleration before shorting
    # an extended move; (2) block long entry into a decelerating overextended RSI.
    if "rsi_accel_max" in conditions:
        val = signals.get("rsi_accel_3")
        if val is None:
            return False, "signal 'rsi_accel_3' unavailable"
        threshold = float(conditions["rsi_accel_max"])
        if float(val) > threshold:
            return False, f"rsi_accel_max exceeded: rsi_accel_3 {float(val):.2f} > {threshold:.2f}"

    # require_volume_ratio_min — per-trade volume floor set by Claude -----------
    # Precedence: min_rvol_for_entry (strategy-level hard gate in apply_entry_gate)
    # runs BEFORE this check and cannot be deferred or expired. This condition is
    # the per-trade threshold Claude sets ABOVE that strategy floor (e.g. ≥2.0×
    # RVOL for a high-conviction catalyst play while the strategy floor is 1.0×).
    if "require_volume_ratio_min" in conditions:
        val = signals.get("volume_ratio")
        if val is None:
            return False, "signal 'volume_ratio' unavailable"
        if float(val) < float(conditions["require_volume_ratio_min"]):
            return False, (
                f"require_volume_ratio_min not met: "
                f"volume_ratio {float(val):.2f} < {float(conditions['require_volume_ratio_min']):.2f}"
            )

    # require_volume_trend_bars_min — minimum consecutive bars of increasing volume
    # Confirms participation is building (selling pressure for shorts, buying for longs).
    if "require_volume_trend_bars_min" in conditions:
        val = signals.get("volume_trend_bars")
        if val is None:
            return False, "signal 'volume_trend_bars' unavailable"
        if int(val) < int(conditions["require_volume_trend_bars_min"]):
            return False, (
                f"require_volume_trend_bars_min not met: "
                f"volume_trend_bars {int(val)} < {int(conditions['require_volume_trend_bars_min'])}"
            )

    # require_macd_bullish — longs: MACD must be in bullish state -----------
    if conditions.get("require_macd_bullish"):
        val = signals.get("macd_signal")
        if val is None:
            return False, "signal 'macd_signal' unavailable"
        if val not in ("bullish", "bullish_cross"):
            return False, f"require_macd_bullish not met: macd_signal={val!r}"

    # require_macd_bearish — shorts: MACD must be in bearish state ----------
    if conditions.get("require_macd_bearish"):
        val = signals.get("macd_signal")
        if val is None:
            return False, "signal 'macd_signal' unavailable"
        if val not in ("bearish", "bearish_cross"):
            return False, f"require_macd_bearish not met: macd_signal={val!r}"

    # require_macd_histogram_expanding — MACD histogram must be expanding ---
    # True when histogram absolute value grew bar-over-bar with unchanged sign
    # (momentum building in the current direction, not fading). Works for both
    # longs (bullish histogram growing) and shorts (bearish histogram deepening).
    if conditions.get("require_macd_histogram_expanding"):
        val = signals.get("macd_histogram_expanding")
        if val is None:
            return False, "signal 'macd_histogram_expanding' unavailable"
        if not bool(val):
            return False, "require_macd_histogram_expanding not met: MACD histogram contracting"

    return True, ""


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
        self._min_dollar_volume = float(cfg.get("min_avg_daily_dollar_volume", _MIN_AVG_DAILY_DOLLAR_VOLUME))
        self._min_conviction = float(cfg.get("min_conviction_threshold", 0.10))  # sanity floor
        # Max fraction of equity that may be deployed before new entries are blocked.
        # 0.0 = disabled. Complements max_positions: allows more concurrent small-sized
        # positions without exceeding equity limits when Claude sizes conservatively.
        # To add: adjust max_portfolio_deployment_pct in config.json ranker section.
        self._max_deployment_pct = float(cfg.get("max_portfolio_deployment_pct", 0.0))
        self._min_technical_score = float(cfg.get("min_technical_score", 0.30))  # TA quality floor
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
        direction = direction_from_action(action)
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
            entry_conditions=opportunity.get("entry_conditions") or {},
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
        strategy_lookup: dict | None = None,
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

        # 1b. Swing technical_only conviction cap — prompt instructs Claude to keep
        #     technical_only swing conviction ≤ 0.50; enforce it here as a hard floor
        #     so a miscalibrated model cannot bypass the cap.
        if (
            opportunity.get("strategy") == "swing"
            and opportunity.get("catalyst_type") == "technical_only"
            and conviction > 0.50
        ):
            return (
                False,
                f"{symbol}: swing technical_only conviction {conviction:.2f} exceeds cap 0.50 "
                f"(no identifiable catalyst — reduce conviction or provide catalyst_driven rationale)",
            )

        # 2. Minimum composite technical score floor — computed with direction so
        #    short opportunities are evaluated against bearish signal strength,
        #    not penalised for the absence of bullish signals.
        #    Applied to both intraday and swing strategies: even swing entries
        #    benefit from some intraday confirmation to avoid starting underwater.
        #    The composite is direction-adjusted, so a swing short scoring below
        #    the floor means bearish intraday signals aren't confirming the thesis —
        #    a valid reason to defer until setup improves.
        if technical_signals is not None:
            sig_summary = technical_signals.get(symbol, {})
            raw_signals = sig_summary.get("signals", {})
            direction = direction_from_action(opportunity.get("action", "buy"))
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

        # 2b. Strategy-specific TA gates — delegated to each Strategy via apply_entry_gate().
        # To add a new strategy-specific filter, implement apply_entry_gate() in the
        # strategy class — no changes here are needed.
        if technical_signals is not None:
            strategy_name = opportunity.get("strategy", "")
            action = opportunity.get("action", "buy")
            sig = technical_signals.get(symbol, {}).get("signals", {})

            # Resolve strategy object: prefer pre-built lookup for efficiency,
            # fall back to on-demand construction for callers that don't pass one.
            _lookup = strategy_lookup or {}
            strategy_obj = _lookup.get(strategy_name)
            if strategy_obj is None and strategy_name:
                try:
                    from ozymandias.strategies.base_strategy import get_strategy
                    strategy_obj = get_strategy(strategy_name)
                except (ValueError, ImportError):
                    pass

            if strategy_obj is not None:
                passed, reason = strategy_obj.apply_entry_gate(action, sig)
                if not passed:
                    return False, f"{symbol}: {reason}"

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

        # 5b. Portfolio deployment cap — block new entries when too much equity is deployed.
        # Uses buying_power / equity as a proxy for available capital. When buying_power
        # approaches zero relative to equity, the portfolio is fully deployed regardless
        # of position count. Set max_portfolio_deployment_pct=0 to disable.
        # To add: set max_portfolio_deployment_pct in config.json ranker section.
        if self._max_deployment_pct > 0 and account_info.equity > 0:
            deployed_pct = 1.0 - (account_info.buying_power / account_info.equity)
            if deployed_pct >= self._max_deployment_pct:
                return (
                    False,
                    f"{symbol}: portfolio {deployed_pct:.0%} deployed ≥ cap {self._max_deployment_pct:.0%} "
                    f"(buying_power={account_info.buying_power:.0f}, equity={account_info.equity:.0f})",
                )

        # 5a. Per-symbol duplicate guard — never enter a symbol already in portfolio
        if any(p.symbol == symbol for p in portfolio.positions):
            return False, f"{symbol}: position already open in portfolio"

        # 6. (PDT check removed — a new entry is never a day trade by itself.
        #     The day trade occurs only when the position is closed same-day.
        #     NOTE: exit paths bypass validate_entry, so PDT enforcement for closes
        #     is not currently implemented; the emergency buffer reserves 1 slot
        #     for exits as the safety net. See risk_manager.validate_entry step 6.)

        # 7. Average daily dollar volume (shares × price).
        # Expressed in dollars so the floor is meaningful across all price ranges —
        # a $10 stock at 100k shares/day ($1M) is far thinner than a $200 stock
        # at the same share count ($20M). avg_daily_volume lives inside the nested
        # "signals" sub-dict; price is also in signals.
        sig_summary = (technical_signals or {}).get(symbol, {})
        nested = sig_summary.get("signals", {})
        avg_vol = nested.get("avg_daily_volume") or opportunity.get("avg_daily_volume")
        price = nested.get("price") or opportunity.get("suggested_entry") or 0.0
        if avg_vol is not None and price and price > 0:
            avg_dollar_vol = avg_vol * price
            if avg_dollar_vol < self._min_dollar_volume:
                return (
                    False,
                    f"{symbol}: avg daily dollar volume ${avg_dollar_vol:,.0f} "
                    f"< minimum ${self._min_dollar_volume:,.0f}",
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
        strategy_lookup: dict | None = None,
        suppressed_symbols: dict[str, str] | None = None,
    ) -> RankResult:
        """
        Full ranking pipeline:
        1. Extract candidates from Claude's reasoning output.
        2. Drop session-suppressed symbols (repeated hard-filter failures).
        3. Drop any whose strategy is in session_veto.
        4. Apply hard filters.
        5. Score remaining candidates.
        6. Sort by composite score descending.

        Returns RankResult with .candidates (scored/sorted) and .rejections
        (list of (symbol, reason) tuples for all hard-filter rejections).
        Session-veto and suppressed-symbol skips are NOT included in rejections —
        only hard-filter failures that reach apply_hard_filters are recorded.

        Parameters
        ----------
        market_hours_fn:
            Callable → bool for market-hours check.  Defaults to
            :func:`~ozymandias.core.market_hours.is_market_open`.
        orders:
            Order list forwarded to the PDT guard.
        suppressed_symbols:
            symbol → rejection_reason dict of symbols suppressed for this session
            after exceeding max_filter_rejection_cycles. Checked before hard filters.
            To add a new suppression source: populate this dict in the orchestrator.
        """
        _suppressed = suppressed_symbols or {}
        # session_veto contains direction strings: "long", "short", or both.
        # Filtering by direction (not strategy) lets Claude block longs without
        # killing short entries that would profit from the same bearish conditions.
        # To add a new direction value, it flows automatically — no changes here.
        session_veto: set[str] = set(reasoning_result.session_veto or [])
        scored: list[ScoredOpportunity] = []
        rejections: list[tuple[str, str]] = []
        for opp in reasoning_result.new_opportunities:
            symbol = opp.get("symbol", "?")
            # Session suppression: symbol has failed hard filters too many times
            # this session — skip silently so it no longer pollutes logs or context.
            if symbol in _suppressed:
                logger.debug(
                    "rank_opportunities: %s skipped — session-suppressed (%s)",
                    symbol, _suppressed[symbol],
                )
                continue
            # Session veto: drop opportunities whose direction Claude has assessed
            # as structurally invalid for today's regime before scoring.
            opp_direction = direction_from_action(opp.get("action", "buy"))
            if opp_direction in session_veto:
                logger.info(
                    "rank_opportunities: %s (%s) skipped — session veto active for %s",
                    symbol, opp.get("action", "buy"), opp_direction,
                )
                continue
            passes, reason = self.apply_hard_filters(
                opp,
                account_info,
                portfolio,
                pdt_guard,
                market_hours_fn,
                orders,
                technical_signals,
                strategy_lookup,
            )
            if not passes:
                # Already-open rejections are expected every medium cycle while Claude's
                # reasoning result is stale — log at DEBUG to avoid repetitive INFO spam.
                level = logging.DEBUG if "already open in portfolio" in reason else logging.INFO
                logger.log(level, "Hard filter rejected %s: %s", symbol, reason)
                rejections.append((symbol, reason))
                continue
            scored.append(
                self.score_opportunity(opp, technical_signals, account_info, portfolio)
            )

        scored.sort(key=lambda s: s.composite_score, reverse=True)
        # Count only symbols that entered the filter pipeline (suppressed/vetoed symbols
        # are skipped via `continue` before scored/rejections are touched, so they must
        # not inflate this count — "1 candidates, 0 passed, 0 rejected" was misleading).
        evaluated = len(scored) + len(rejections)
        logger.info(
            "rank_opportunities: %d candidates, %d passed filters, %d rejected",
            evaluated,
            len(scored),
            len(rejections),
        )
        return RankResult(candidates=scored, rejections=rejections)

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
