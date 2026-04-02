"""
Claude AI reasoning engine.

Handles context assembly, API communication, defensive JSON parsing,
and reasoning cache integration. Called by the slow loop (event-driven,
~8-12 times per trading day).

Design principles:
- Token efficiency: only Tier 1 symbols (positions + top watchlist) go to Claude.
- Defensive parsing: ~5% of Claude responses are malformed JSON. Never crash.
- Retry with exponential backoff for rate limits and 5xx errors.
- All results cached for debugging and restart reuse.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import anthropic

from ozymandias.core.config import ClaudeConfig, Config
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import PortfolioState, Position, WatchlistState
from ozymandias.intelligence.context_compressor import ContextCompressor
from ozymandias.intelligence.technical_analysis import compute_directional_scores

log = logging.getLogger(__name__)

# Rough chars-per-token estimate for context size guard.
# Accurate for structured JSON (context JSON estimated ≈ actual API tokens).
_CHARS_PER_TOKEN = 4
# Total token budget for the full API call (context JSON + prompt template).
# Context trim guard uses: context_budget = _TOTAL_TOKEN_BUDGET - prompt_template_tokens,
# both measured in chars/4 units. 25,000 keeps full-prompt cost well under $0.10/call
# while accommodating 30+ watchlist symbols without trimming.
_TOTAL_TOKEN_BUDGET = 25_000

# ta_readiness fields excluded from per-symbol context sent to Sonnet.
# These have no corresponding entry_conditions schema keys in reasoning.txt and add
# token overhead without enabling structured gates. To re-enable a field, remove it here.
_TA_EXCLUDED = frozenset({
    "rsi_divergence", "roc_deceleration", "roc_negative_deceleration",
    "bollinger_position", "bb_squeeze", "avg_daily_volume", "vol_regime_ratio",
})


# ---------------------------------------------------------------------------
# Result dataclasses (mirror the JSON output schemas from spec §4.3)
# ---------------------------------------------------------------------------

@dataclass
class ReasoningResult:
    """Parsed output from a full reasoning cycle."""
    timestamp: str
    position_reviews: list[dict]      # [{symbol, action, thesis_intact, updated_reasoning, adjusted_targets}]
    new_opportunities: list[dict]     # [{symbol, action, strategy, timeframe, conviction, ...}]
    watchlist_changes: dict           # {add: [...], remove: [...], rationale: "..."}
    market_assessment: str
    risk_flags: list[str]
    rejected_opportunities: list[dict]  # [{symbol, considered_reason, rejection_reason}]
    session_veto: list[str] = field(default_factory=list)  # direction strings to suppress ("long"/"short"); enforced in rank_opportunities
    raw: dict = field(default_factory=dict)                # full parsed Claude response

    # Phase 19 strategic output fields — all optional (Claude may omit any)
    regime_assessment: dict | None = None
    # {regime, confidence, key_signals, valid_until_conditions, implications}
    sector_regimes: dict | None = None
    # {ETF: {regime, bias, strength}} — only sectors with watchlist symbols
    filter_adjustments: dict | None = None
    # {min_rvol, reason} — Claude-proposed RVOL relaxation only; score floor is non-adjustable
    active_theses: list[dict] | None = None
    # [{symbol, thesis, thesis_breaking_conditions}] — open position thesis durability


@dataclass
class WatchlistResult:
    """Parsed output from a watchlist build cycle."""
    watchlist: list[dict]             # [{symbol, reason, priority_tier, strategy}]
    market_notes: str
    raw: dict
    removes: list[str] = field(default_factory=list)  # symbols to remove from current watchlist


@dataclass
class ReviewResult:
    """Parsed output from a focused position review."""
    reviews: list[dict]               # [{symbol, thesis_intact, thesis_assessment, recommended_action, adjusted_targets, notes}]
    raw: dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_technical_summary(signals: dict) -> str:
    """Convert a signals dict from generate_signal_summary into a compact string."""
    parts: list[str] = []
    vwap = signals.get("vwap_position", "")
    if vwap:
        parts.append(f"VWAP {vwap}")
    rsi = signals.get("rsi")
    if rsi is not None and not (isinstance(rsi, float) and math.isnan(rsi)):
        parts.append(f"RSI {rsi:.0f}")
    macd = signals.get("macd_signal", "")
    if macd:
        parts.append(f"MACD {macd.replace('_', ' ')}")
    trend = signals.get("trend_structure", "")
    if trend:
        parts.append(f"trend {trend.replace('_', ' ')}")
    roc = signals.get("roc_5")
    if roc is not None:
        parts.append(f"ROC {roc:+.1f}%")
    vol = signals.get("volume_ratio")
    if vol is not None:
        parts.append(f"vol×{vol:.1f}")
    return ", ".join(parts) if parts else "no indicator data"


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _safe_dict(value: object) -> dict | None:
    """Return value if it is a non-empty dict, else None. Never raises."""
    if isinstance(value, dict) and value:
        return value
    return None


def _safe_list_of_dicts(value: object) -> list[dict] | None:
    """Return value if it is a list of dicts, else None. Never raises."""
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return value
    return None


def _result_from_raw_reasoning(raw: dict) -> ReasoningResult:
    # session_veto must be a list of strings; guard against Claude returning wrong type
    raw_veto = raw.get("session_veto", [])
    session_veto = [str(v) for v in raw_veto] if isinstance(raw_veto, list) else []

    # Phase 19: parse optional strategic output fields defensively
    # If present but malformed, set to None and log at DEBUG (never crash)
    regime_assessment = None
    try:
        regime_assessment = _safe_dict(raw.get("regime_assessment"))
    except Exception:
        log.debug("_result_from_raw_reasoning: failed to parse regime_assessment")

    sector_regimes = None
    try:
        sector_regimes = _safe_dict(raw.get("sector_regimes"))
    except Exception:
        log.debug("_result_from_raw_reasoning: failed to parse sector_regimes")

    filter_adjustments = None
    try:
        filter_adjustments = _safe_dict(raw.get("filter_adjustments"))
    except Exception:
        log.debug("_result_from_raw_reasoning: failed to parse filter_adjustments")

    active_theses = None
    try:
        active_theses = _safe_list_of_dicts(raw.get("active_theses"))
    except Exception:
        log.debug("_result_from_raw_reasoning: failed to parse active_theses")

    return ReasoningResult(
        timestamp=raw.get("timestamp", datetime.now(timezone.utc).isoformat()),
        position_reviews=raw.get("position_reviews", []),
        new_opportunities=raw.get("new_opportunities", []),
        watchlist_changes=raw.get("watchlist_changes", {"add": [], "remove": [], "rationale": ""}),
        market_assessment=raw.get("market_assessment", ""),
        risk_flags=raw.get("risk_flags", []),
        rejected_opportunities=raw.get("rejected_opportunities", []),
        session_veto=session_veto,
        raw=raw,
        regime_assessment=regime_assessment,
        sector_regimes=sector_regimes,
        filter_adjustments=filter_adjustments,
        active_theses=active_theses,
    )


def _result_from_raw_watchlist(raw: dict) -> WatchlistResult:
    removes = [s for s in raw.get("remove", []) if isinstance(s, str)]
    return WatchlistResult(
        watchlist=raw.get("watchlist", []),
        market_notes=raw.get("market_notes", ""),
        raw=raw,
        removes=removes,
    )


def _result_from_raw_review(raw: dict) -> ReviewResult:
    return ReviewResult(
        reviews=raw.get("reviews", []),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Defensive JSON parser (spec §4.3 — 4-step pipeline)
# ---------------------------------------------------------------------------

def parse_claude_response(raw_text: str) -> Optional[dict]:
    """
    Parse Claude's response into structured JSON. Returns None on failure.

    4-step pipeline per spec §4.3:
      1. Strip markdown code fences
      2. Attempt json.loads on cleaned text
      3. Regex-extract the outermost JSON object and retry
      4. Log and return None — caller skips this cycle
    """
    if not raw_text or not raw_text.strip():
        log.warning("parse_claude_response: empty response")
        return None

    # Step 1: strip markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", raw_text).strip()
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()

    # Step 2: direct parse
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
        log.warning(
            "parse_claude_response: parsed JSON is not a dict (type=%s)",
            type(result).__name__,
        )
        return None
    except json.JSONDecodeError:
        pass

    # Step 3: regex-extract outermost JSON object
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, dict):
                log.debug("parse_claude_response: recovered via regex extraction")
                return result
        except json.JSONDecodeError:
            pass

    # Step 4: unrecoverable
    log.warning(
        "parse_claude_response: all parsing steps failed. First 200 chars: %r",
        raw_text[:200],
    )
    return None


# ---------------------------------------------------------------------------
# Reasoning engine
# ---------------------------------------------------------------------------

class ClaudeReasoningEngine:
    """
    Strategic AI reasoning layer. Wraps the Anthropic API with:
    - Structured context assembly (Tier 1 / Tier 2 budgeting)
    - Retry with exponential backoff (rate limits and 5xx errors)
    - Defensive JSON parsing (4-step pipeline)
    - Reasoning cache integration

    Usage::

        engine = ClaudeReasoningEngine(config)
        result = await engine.run_reasoning_cycle(
            portfolio, watchlist, market_data, indicators
        )
        if result:
            process(result.new_opportunities)
    """

    def __init__(
        self,
        config: Config,
        cache: Optional[ReasoningCache] = None,
        prompts_dir: Optional[Path] = None,
    ) -> None:
        self._cfg = config
        self._claude_cfg: ClaudeConfig = config.claude
        self._cache = cache or ReasoningCache()
        self._client = anthropic.AsyncAnthropic(max_retries=0)  # reads ANTHROPIC_API_KEY from env; we own all retry logic
        self._prompts_dir = prompts_dir or config.prompts_dir
        self._last_input_tokens: int = 0
        self._last_output_tokens: int = 0
        # Measure prompt template overhead once at init so the context trim guard
        # can compute the correct context budget (total_budget - template_tokens).
        try:
            _template_text = (self._prompts_dir / "reasoning.txt").read_text(encoding="utf-8")
            self._prompt_template_tokens: int = len(_template_text) // _CHARS_PER_TOKEN
        except OSError:
            # Prompt dir not available yet (e.g. in unit tests using a custom prompts_dir).
            # Fall back to a conservative estimate; trim guard will be slightly aggressive.
            self._prompt_template_tokens = 6_000
        # Call spacing: serialize all API calls and enforce a minimum inter-call gap.
        # Prevents RPM rate-limit hits when position reviews, thesis challenges, and
        # reasoning cycles queue up in the same slow-loop tick.
        self._call_lock = asyncio.Lock()
        self._last_call_end_time: float = 0.0   # monotonic time the most recent call finished
        # Tracks which tier-1 symbols were actually sent in the last reasoning context
        # (after token-budget trimming). Used by the orchestrator to detect implicit rejections —
        # directional candidates that Claude silently omitted from both lists.
        self.last_sent_tier1_symbols: list[str] = []
        # Phase 20: Haiku context compressor — pre-screens candidates before Sonnet context assembly.
        # Disabled when compressor_enabled=False (falls back to deterministic composite sort).
        self._compressor: ContextCompressor | None = (
            ContextCompressor(config.claude, self._prompts_dir)
            if config.claude.compressor_enabled
            else None
        )
        # Fallback provider state
        self._fallback_client = None          # lazy-initialized Gemini client
        self._overload_fallback_count: int = 0  # session circuit breaker counter
        self._circuit_broken_since: Optional[float] = None  # monotonic time when circuit tripped
        # Note: GEMINI_API_KEY availability is checked lazily in _call_gemini_fallback.
        # The orchestrator injects it from credentials.enc before constructing this engine,
        # so checking os.environ here would produce a false alarm in any non-orchestrator context.

    # ------------------------------------------------------------------
    # Prompt loading
    # ------------------------------------------------------------------

    def _load_prompt(self, filename: str) -> str:
        path = self._prompts_dir / filename
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Cannot load prompt template {path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    # Verbose reasoning depth instructions for position_reviews.txt when
    # review_call_verbose=True. Controls the output depth of updated_reasoning.
    _REVIEW_VERBOSE_INSTRUCTIONS: str = (
        "Provide FULL reasoning depth: state the action rationale AND the strongest "
        "current argument against holding. Reference specific price levels, patterns, "
        "or catalysts — not generic risk statements. When the position has meaningful "
        "unrealised gains, explicitly assess whether to raise the stop_loss to protect "
        "them based on thesis milestone progress, not price movement alone."
    )
    _REVIEW_COMPACT_INSTRUCTIONS: str = (
        "Two sentences maximum: action rationale and the strongest current argument "
        "against holding. Be specific (name a price level or pattern) — not generic."
    )
    _REVIEW_VERBOSE_SCHEMA: str = (
        '"<action rationale — specific price levels, patterns, catalysts. '
        'Then: the strongest current argument against holding this position. '
        'If meaningful gains, assess stop-loss adjustment against thesis milestones.>"'
    )
    _REVIEW_COMPACT_SCHEMA: str = (
        '"<two sentences max: action rationale and strongest bear case>"'
    )

    def assemble_position_review_context(
        self,
        portfolio: "PortfolioState",
        indicators: dict,
        market_data: dict,
        daily_indicators: dict[str, dict] | None = None,
    ) -> dict:
        """
        Build compact context for the position review call (Phase 22 split mode).
        Contains only open position data and minimal account context — no watchlist
        candidates, no execution history, no market regime detail.
        """
        now_utc = datetime.now(timezone.utc)
        position_entries: list[dict] = []
        for pos in portfolio.positions:
            sig_summary = indicators.get(pos.symbol, {})
            signals = sig_summary.get("signals", sig_summary)
            current_price = signals.get("price")
            unrealized_pnl = (
                round((current_price - pos.avg_cost) * pos.shares, 2)
                if current_price is not None
                else None
            )
            entry_date_str = pos.intention.entry_date or pos.entry_date
            try:
                entry_dt = datetime.fromisoformat(entry_date_str)
                hold_hours = round(
                    (now_utc - entry_dt).total_seconds() / 3600, 1
                )
            except Exception:
                hold_hours = None
            # Review context uses 1 most-recent note (not 3) to limit input token cost.
            # The review call has current_price and exit_targets — full history is not needed.
            # Each note is also capped at 250 chars (verbose old-style notes can be 500+ chars).
            _recent_note = pos.intention.review_notes[-1:] if pos.intention.review_notes else []
            _recent_note = [n[:250] for n in _recent_note]

            pos_entry: dict = {
                "symbol": pos.symbol,
                "shares": pos.shares,
                "avg_cost": pos.avg_cost,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "hold_hours": hold_hours,
                "intention": {
                    "catalyst": pos.intention.catalyst,
                    "direction": pos.intention.direction,
                    "strategy": pos.intention.strategy,
                    "expected_move": pos.intention.expected_move,
                    "reasoning": pos.intention.reasoning,
                    "exit_targets": {
                        "profit_target": pos.intention.exit_targets.profit_target,
                        "stop_loss": pos.intention.exit_targets.stop_loss,
                    },
                    "max_expected_loss": pos.intention.max_expected_loss,
                    "entry_date": entry_date_str,
                    "last_review_note": _recent_note[0] if _recent_note else None,
                },
            }
            if (
                pos.intention.strategy == "swing"
                and daily_indicators
                and daily_indicators.get(pos.symbol)
            ):
                pos_entry["daily_signals"] = daily_indicators[pos.symbol]
            position_entries.append(pos_entry)

        return {
            "portfolio": {
                "cash": portfolio.cash,
                "buying_power": portfolio.buying_power,
                "positions": position_entries,
            },
            "market_context": {
                "trading_session": market_data.get("trading_session"),
                "pdt_trades_remaining": market_data.get("pdt_trades_remaining"),
                "equity": market_data.get("equity"),
                "spy_rsi": market_data.get("spy_rsi"),
                "spy_trend": market_data.get("spy_trend"),
                "spy_daily": market_data.get("spy_daily"),
            },
        }

    async def run_position_review_call(
        self,
        portfolio: "PortfolioState",
        indicators: dict,
        market_data: dict,
        daily_indicators: dict[str, dict] | None = None,
        breach_context: str | None = None,
    ) -> list[dict]:
        """
        Phase 22 split-call: compact position review using position_reviews.txt.

        Returns a list of position_review dicts on success, or [] on any failure.
        Never raises — the orchestrator continues to the opportunity call regardless.

        breach_context: if set, a thesis breach was detected by Haiku before this call.
        Injected as a prominent notice in the prompt so Sonnet knows which condition
        was detected and can re-examine the affected position instead of reaffirming
        its prior hold recommendation.
        """
        if not portfolio.positions:
            return []

        verbose = getattr(self._claude_cfg, "review_call_verbose", False)
        depth_instructions = (
            self._REVIEW_VERBOSE_INSTRUCTIONS if verbose else self._REVIEW_COMPACT_INSTRUCTIONS
        )
        reasoning_schema = (
            self._REVIEW_VERBOSE_SCHEMA if verbose else self._REVIEW_COMPACT_SCHEMA
        )

        thesis_breach_notice = ""
        if breach_context:
            thesis_breach_notice = (
                "⚠ THESIS BREACH DETECTED BY MONITORING SYSTEM:\n"
                f"{breach_context}\n\n"
                "A condition specified as thesis-breaking has been concretely detected in current "
                "signals or news. Re-examine the affected position carefully. Do not simply "
                "reaffirm your prior hold recommendation — evaluate whether this specific "
                "condition warrants exit, stop adjustment, or target revision given current "
                "price and context.\n\n"
            )

        try:
            context = self.assemble_position_review_context(
                portfolio, indicators, market_data, daily_indicators
            )
            context_json = json.dumps(context, default=str)
            template = self._load_prompt("position_reviews.txt")
            max_tokens = getattr(self._claude_cfg, "review_call_max_tokens", 2048)

            log.info(
                "Slow loop: Claude call [position review]  positions=%d  verbose=%s  breach=%s",
                len(portfolio.positions), verbose, bool(breach_context),
            )
            raw_text = await self.call_claude(
                template,
                {
                    "context_json": context_json,
                    "reasoning_depth_instructions": depth_instructions,
                    "updated_reasoning_schema": reasoning_schema,
                    "thesis_breach_notice": thesis_breach_notice,
                },
                max_tokens_override=max_tokens,
            )
            parsed = parse_claude_response(raw_text)
            if parsed is None:
                log.warning("Position review call: unparseable response — skipping reviews")
                return []
            reviews = parsed.get("position_reviews", [])
            log.info(
                "Position review call complete: %d reviews",
                len(reviews),
            )
            return reviews
        except Exception as exc:
            log.warning("Position review call failed: %s — skipping reviews this cycle", exc)
            return []

    def assemble_reasoning_context(
        self,
        portfolio: PortfolioState,
        watchlist: WatchlistState,
        market_data: dict,
        indicators: dict,
        recommendation_outcomes: dict | None = None,
        recent_executions: list | None = None,
        execution_stats: dict | None = None,
        session_suppressed: dict[str, str] | None = None,
        claude_soft_rejections: dict[str, int] | None = None,
        daily_indicators: dict[str, dict] | None = None,
        selected_symbols: list[str] | None = None,
        skip_position_daily_signals: bool = False,
        skip_context_fields: frozenset | None = None,
        max_symbols_override: int | None = None,
    ) -> dict:
        """
        Build the structured input context sent to Claude each cycle.

        Tier 1: current positions + top watchlist candidates (priority_tier=1),
                up to tier1_max_symbols total. Full context for each symbol.
        Tier 2: remaining watchlist — NOT sent to Claude, used locally for
                technical scanning only.

        If the assembled context exceeds the token target (~8,000 tokens),
        Tier 1 watchlist entries are trimmed until it fits.

        Args:
            portfolio:   Current portfolio state.
            watchlist:   Full watchlist; entries with priority_tier=1 are candidates.
            market_data: Market context dict — passed through directly to Claude.
                         Expected keys: spy_trend, vix, sector_rotation,
                         macro_events_today, trading_session, pdt_trades_remaining.
            indicators:  symbol → output dict from generate_signal_summary().
                         May also include a flat signals dict directly.
            recommendation_outcomes: Phase 15 — dict of symbol → outcome tracking
                         info from the orchestrator's _recommendation_outcomes store.
                         Assembled into a sorted list for Claude's context.
            recent_executions: Phase 15 — pre-computed list of recent close records
                         (from TradeJournal.load_recent). Passed in rather than awaited
                         here to keep this method sync.
            execution_stats: Phase 15 — pre-computed session stats dict
                         (from TradeJournal.compute_session_stats). Same rationale.
        """
        max_tier1 = max_symbols_override or self._claude_cfg.tier1_max_symbols
        now_utc = datetime.now(timezone.utc)

        # --- Current positions (always Tier 1) ---
        # When skip_position_daily_signals=True (split mode), positions appear as a
        # compact summary only — full reviews happen in the separate Call A.
        # Claude still needs to know which symbols are held to avoid re-proposing them.
        position_entries: list[dict] = []
        for pos in portfolio.positions:
            sig_summary = indicators.get(pos.symbol, {})
            signals = sig_summary.get("signals", sig_summary)
            current_price = signals.get("price")
            unrealized_pnl = (
                round((current_price - pos.avg_cost) * pos.shares, 2)
                if current_price is not None
                else None
            )

            if skip_position_daily_signals:
                # Compact summary for split mode — just enough for Claude to know what's held.
                pos_entry: dict = {
                    "symbol": pos.symbol,
                    "direction": pos.intention.direction,
                    "strategy": pos.intention.strategy,
                    "unrealized_pnl": unrealized_pnl,
                }
            else:
                entry_date_str = pos.intention.entry_date or pos.entry_date
                try:
                    entry_dt = datetime.fromisoformat(entry_date_str)
                    hold_hours = round(
                        (now_utc - entry_dt).total_seconds() / 3600, 1
                    )
                except Exception:
                    hold_hours = None
                pos_entry = {
                    "symbol": pos.symbol,
                    "shares": pos.shares,
                    "avg_cost": pos.avg_cost,
                    "current_price": current_price,
                    "unrealized_pnl": unrealized_pnl,
                    "hold_hours": hold_hours,
                    "intention": {
                        "catalyst": pos.intention.catalyst,
                        "direction": pos.intention.direction,
                        "strategy": pos.intention.strategy,
                        "expected_move": pos.intention.expected_move,
                        "reasoning": pos.intention.reasoning,
                        "exit_targets": {
                            "profit_target": pos.intention.exit_targets.profit_target,
                            "stop_loss": pos.intention.exit_targets.stop_loss,
                        },
                        "max_expected_loss": pos.intention.max_expected_loss,
                        "entry_date": entry_date_str,
                        "review_notes": pos.intention.review_notes[-3:],
                    },
                }
                # Swing positions get daily-bar signals so Claude evaluates thesis health
                # on the appropriate timeframe. Momentum positions use intraday signals only.
                if (
                    pos.intention.strategy == "swing"
                    and daily_indicators
                    and daily_indicators.get(pos.symbol)
                ):
                    pos_entry["daily_signals"] = daily_indicators[pos.symbol]
            position_entries.append(pos_entry)

        # --- Tier 1 watchlist candidates (fill remaining budget) ---
        slots = max(0, max_tier1 - len(position_entries))

        # Exclude symbols already held as open positions: they appear in the positions
        # block above and Claude reviews them there. Including them here causes Claude
        # to re-propose them as new entry candidates and triggers false "implicit
        # rejection" warnings in the orchestrator.
        # NOTE: if position scaling (adding to existing positions) is ever implemented,
        # this filter should be removed or conditioned on whether the strategy supports
        # pyramiding — held symbols would legitimately need to appear as candidates again.
        open_position_symbols = {pos.symbol for pos in portfolio.positions}
        _suppressed_set = set(session_suppressed or {})
        all_tier1 = [
            e for e in watchlist.entries
            if e.priority_tier == 1
            and e.symbol not in open_position_symbols
            and e.symbol not in _suppressed_set
        ]
        total_tier1 = len(all_tier1)

        if selected_symbols:
            # Phase 20: Haiku pre-screener provided a ranked list — use it directly.
            # Symbols may come from any watchlist tier (Haiku sees the full pool).
            # Build lookup across all watchlist entries (not just tier1).
            sym_to_entry = {
                e.symbol: e for e in watchlist.entries
                if e.symbol not in open_position_symbols
                and e.symbol not in _suppressed_set
            }
            tier1_watch = []
            for sym in selected_symbols:
                if len(tier1_watch) >= slots:
                    break
                entry = sym_to_entry.get(sym)
                if entry is not None:
                    tier1_watch.append(entry)
        else:
            # Fallback: sort by direction-adjusted composite score descending.
            # Without this, the slice is insertion-order, meaning the same seed symbols
            # appear every cycle regardless of what the rest of the watchlist is doing.
            def _tier1_score(entry) -> float:
                ind = indicators.get(entry.symbol, {})
                raw = ind.get("signals") or {}
                ed = getattr(entry, "expected_direction", "either")
                if raw:
                    _daily = (daily_indicators or {}).get(entry.symbol, {})
                    scores = compute_directional_scores(raw, _daily)
                    if ed == "long":  return scores["long"]
                    if ed == "short": return scores["short"]
                    return max(scores["long"], scores["short"])
                # Fall back to pre-computed intraday-only scores
                if ed == "long":  return float(ind.get("long_score",  0.0))
                if ed == "short": return float(ind.get("short_score", 0.0))
                return max(float(ind.get("long_score", 0.0)), float(ind.get("short_score", 0.0)))

            tier1_watch = sorted(all_tier1, key=_tier1_score, reverse=True)[:slots]

        # Phase 15: build ta_readiness dict replacing technical_summary string.
        # ta_readiness is a direct pass-through of indicators[symbol]["signals"].
        # Directional scores (long_score/short_score) are intentionally excluded —
        # Claude reasons from raw signals; scores are used by the ranker only.
        # _make_technical_summary is retained for run_position_review (not removed).
        watchlist_tier1: list[dict] = []
        for entry in tier1_watch:
            sym = entry.symbol
            sig_summary = indicators.get(sym, {})
            raw_signals = sig_summary.get("signals", {})
            ed = getattr(entry, "expected_direction", "either")
            # ta_readiness: live signals passed through for Claude's qualitative use.
            # Composite score is intentionally omitted — Claude reasons from raw signals;
            # the score is used by the ranker only (ranker has its own signal access).
            # Excluded fields defined at module level in _TA_EXCLUDED.
            # Token optimisations: strip False booleans, integer zeros; round floats to 2dp.
            ta_readiness: dict = {
                k: (round(v, 2) if isinstance(v, float) else v)
                for k, v in raw_signals.items()
                if k not in _TA_EXCLUDED
                and v is not False
                and not (isinstance(v, int) and not isinstance(v, bool) and v == 0)
            } if raw_signals else {}

            entry_dict: dict = {
                "symbol": sym,
                "latest_price": raw_signals.get("price") if raw_signals else sig_summary.get("price"),
                "ta_readiness": ta_readiness,
                "strategy": entry.strategy,
                "reason": entry.reason,
                "expected_direction": ed,
            }
            # Include last_view when present and fresher than last_view_max_age_days.
            # Gives Claude cross-session memory of its previous assessment without
            # re-deriving context from raw TA alone.
            _max_age = getattr(self._claude_cfg, "last_view_max_age_days", 7)
            _cutoff = (datetime.now(timezone.utc) - timedelta(days=_max_age)).date().isoformat()
            if entry.last_view and entry.last_view_date and entry.last_view_date >= _cutoff:
                entry_dict["last_view"] = entry.last_view
            watchlist_tier1.append(entry_dict)

        # --- Phase 15: recommendation_outcomes context list ---
        max_age_min = self._claude_cfg.recommendation_outcome_max_age_min
        _outcomes_raw = recommendation_outcomes or {}
        outcomes_list: list[dict] = []
        for symbol, rec in _outcomes_raw.items():
            stage = rec.get("stage", "")
            attempt_ts = rec.get("attempt_time_utc")
            age_min = 0.0
            if attempt_ts:
                try:
                    dt = datetime.fromisoformat(attempt_ts)
                    age_min = (now_utc - dt).total_seconds() / 60
                except Exception:
                    age_min = 0.0
            if stage in ("filled", "cancelled") and age_min > max_age_min:
                continue
            entry_dict = {
                "symbol": symbol,
                "stage": stage,
                "age_min": round(age_min, 1),
            }
            if rec.get("stage_detail"):
                entry_dict["stage_detail"] = rec["stage_detail"]
            if stage == "ranker_rejected":
                entry_dict["rejection_count"] = rec.get("rejection_count", 1)
                entry_dict["claude_entry_target"] = rec.get("claude_entry_target", 0.0)
            elif stage == "order_pending":
                entry_dict["claude_entry_target"] = rec.get("claude_entry_target", 0.0)
                ind_entry = indicators.get(symbol, {})
                sigs = ind_entry.get("signals", ind_entry)
                current_price = sigs.get("price")
                if current_price is not None:
                    entry_dict["current_price"] = current_price
                    target = rec.get("claude_entry_target", 0.0)
                    if target and target > 0:
                        entry_dict["drift_pct"] = round(
                            (current_price - target) / target * 100, 2
                        )
            elif stage in ("conditions_waiting", "gate_expired"):
                entry_dict["claude_entry_target"] = rec.get("claude_entry_target", 0.0)
            # Surface consecutive soft-rejection count when >= 2 so Claude can see
            # it accumulating; the prompt forces an explicit decision at >= 3.
            soft_count = (claude_soft_rejections or {}).get(symbol, 0)
            if soft_count >= 2:
                entry_dict["consecutive_claude_rejections"] = soft_count
            outcomes_list.append(entry_dict)

        outcomes_list.sort(key=lambda x: x.get("age_min", 0))
        outcomes_list = outcomes_list[:15]  # cap at 15

        # --- Phase 15: recent_executions context list ---
        _recent_execs = recent_executions or []
        recent_executions_context: list[dict] = []
        max_execs = self._claude_cfg.recent_executions_count
        for rec in _recent_execs[:max_execs]:
            exec_entry: dict = {
                "symbol": rec.get("symbol", ""),
                "direction": rec.get("direction", "long"),
                "entry_price": rec.get("entry_price", 0.0),
                "exit_price": rec.get("exit_price", 0.0),
                "pnl_pct": rec.get("pnl_pct", 0.0),
                "strategy": rec.get("strategy", ""),
                "claude_conviction": rec.get("claude_conviction", 0.0),
                "duration_min": int(rec.get("hold_duration_min", 0) or 0),
            }
            recent_executions_context.append(exec_entry)

        # Filter watchlist_news to only symbols being evaluated this cycle.
        # market_data contains news for all tier-1 symbols (fetched before Haiku screening);
        # symbols Haiku excluded won't be reasoned about, so their news is wasted tokens.
        _evaluated_symbols = {e["symbol"] for e in watchlist_tier1}
        _evaluated_symbols.update(pos.symbol for pos in portfolio.positions)
        _filtered_news = {
            sym: items
            for sym, items in (market_data.get("watchlist_news") or {}).items()
            if sym in _evaluated_symbols
        }
        market_data_filtered = {**market_data, "watchlist_news": _filtered_news}

        # Phase 22: drop fields excluded for reduced-context tiers (Tier 2 drops
        # last_view and sector_dispersion to shrink input by ~30%).
        if skip_context_fields:
            market_data_filtered = {
                k: v for k, v in market_data_filtered.items()
                if k not in skip_context_fields
            }
            # Also drop per-entry last_view when that field is excluded.
            if "last_view" in skip_context_fields:
                for entry in watchlist_tier1:
                    entry.pop("last_view", None)

        _suppressed = session_suppressed or {}
        context: dict[str, Any] = {
            "portfolio": {
                "cash": portfolio.cash,
                "buying_power": portfolio.buying_power,
                "positions": position_entries,
            },
            "watchlist_tier1": watchlist_tier1,
            "market_context": market_data_filtered,
            "recommendation_outcomes": outcomes_list,
            "recent_executions": recent_executions_context,
            "execution_stats": execution_stats or {},
            "session_suppressed": [
                {"symbol": s, "reason": r} for s, r in _suppressed.items()
            ],
        }

        # --- Token guard: trim watchlist until context fits within budget ---
        # Budget = total target minus what the prompt template will consume.
        # Both sides are in chars/4 (estimated) tokens — same unit, no conversion needed.
        context_token_budget = _TOTAL_TOKEN_BUDGET - self._prompt_template_tokens
        context_json = json.dumps(context, default=str)
        _tier1_before_trim = len(context["watchlist_tier1"])
        while _estimate_tokens(context_json) > context_token_budget and context["watchlist_tier1"]:
            context["watchlist_tier1"].pop()
            context_json = json.dumps(context, default=str)
        if not context["watchlist_tier1"] and _tier1_before_trim > 0:
            log.warning(
                "Token budget overflow: all watchlist_tier1 entries trimmed — "
                "Claude will reason with zero watchlist symbols this cycle. "
                "Consider reducing context size or raising _TOTAL_TOKEN_BUDGET."
            )

        # Record exactly which tier-1 symbols made it into context after trimming.
        # The orchestrator reads this after each reasoning cycle to detect implicit rejections.
        self.last_sent_tier1_symbols = [e["symbol"] for e in context["watchlist_tier1"]]

        # Tell Claude exactly how many tier-1 symbols exist and how many it's seeing.
        # Without this, Claude can't tell it's working from a sample and will keep
        # recommending adds for symbols already in the invisible tail.
        context["watchlist_tier1_shown"] = len(context["watchlist_tier1"])
        context["watchlist_tier1_total"] = total_tier1

        context_tokens = _estimate_tokens(context_json)
        log.debug(
            "Context: %d positions, %d/%d tier1 watchlist, "
            "~%d context tokens + ~%d template tokens = ~%d total "
            "(budget: %d context / %d total)",
            len(position_entries),
            context["watchlist_tier1_shown"],
            total_tier1,
            context_tokens,
            self._prompt_template_tokens,
            context_tokens + self._prompt_template_tokens,
            context_token_budget,
            _TOTAL_TOKEN_BUDGET,
        )
        return context

    # ------------------------------------------------------------------
    # API call with retry and multi-provider fallback
    # ------------------------------------------------------------------

    async def _call_gemini_fallback(self, prompt: str, max_tokens: int) -> str:
        """
        Call Google Gemini Flash as fallback when Claude is overloaded.
        Lazily initializes the Gemini client on first use.
        Raises RuntimeError if GEMINI_API_KEY is not available.
        """
        import os
        if self._fallback_client is None:
            try:
                import google.generativeai as genai  # type: ignore[import]
            except ImportError as exc:
                raise RuntimeError(
                    "google-generativeai package is not installed. "
                    "Run: pip install google-generativeai>=0.8"
                ) from exc
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "Claude is overloaded and GEMINI_API_KEY is not set. "
                    "Add 'gemini_api_key' to credentials.enc for AI fallback resilience."
                )
            genai.configure(api_key=api_key)
            self._fallback_client = genai.GenerativeModel(self._cfg.ai_fallback.fallback_model)

        fb = self._cfg.ai_fallback
        log.warning(
            "Using Gemini %s as fallback provider (Claude unavailable)",
            fb.fallback_model,
        )
        _timeout = getattr(self._claude_cfg, "api_call_timeout_sec", 200.0)
        response = await asyncio.wait_for(
            self._fallback_client.generate_content_async(
                prompt,
                generation_config={"max_output_tokens": max_tokens},
            ),
            timeout=_timeout,
        )
        text = response.text or ""
        log.info("Gemini fallback call succeeded (%d chars)", len(text))
        return text

    async def call_claude(
        self,
        prompt_template: str,
        context: dict,
        max_tokens_override: int | None = None,
        model_override: str | None = None,
    ) -> str:
        """
        Fill the prompt template with context values and call the Anthropic API.

        Retry policy:
          - 529 overload: fast retries (3s→6s→12s), then fall back to Gemini Flash.
          - RateLimitError (429) / other 5xx: exponential backoff, base 30s, max 10 min.
          - APIStatusError 4xx (non-429): re-raised immediately (not retryable).
          - TimeoutError (>120s): re-raised immediately.
          - Circuit breaker: after 3 consecutive overload fallbacks, skip Claude and go
            straight to Gemini; probes Claude every 10 minutes to restore primary.

        Logs a WARNING if a successful call takes longer than 60 seconds.
        Logs a WARNING if stop_reason is "max_tokens" (response was truncated).

        Args:
            max_tokens_override: If provided, overrides max_tokens_per_cycle for this call.

        Returns the raw response text string.
        """
        # Use identifier-only substitution so JSON examples inside the template
        # (which contain {" ... "} blocks) are not mistaken for placeholders.
        # Only replaces {word} tokens where the key is a plain Python identifier.
        missing: list[str] = []

        def _sub(m: re.Match) -> str:
            key = m.group(1)
            if key in context:
                return str(context[key])
            missing.append(key)
            return m.group(0)

        prompt = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, prompt_template)
        if missing:
            raise ValueError(f"Prompt template missing placeholder key(s): {missing}")

        fb = self._cfg.ai_fallback
        max_tokens = max_tokens_override or self._claude_cfg.max_tokens_per_cycle

        # BUG-002: Gemini fallback must NOT be called while holding _call_lock —
        # it can block for up to 120s and starves all concurrent callers.
        # Sentinel pattern: set _fallback_args inside the lock, call after releasing.
        _fallback_args: tuple | None = None

        async with self._call_lock:
            # Enforce minimum spacing between successive API calls.
            # Prevents RPM rate-limit hits when multiple calls queue up in the same
            # slow-loop tick (position reviews, thesis challenges, reasoning cycles).
            gap = self._claude_cfg.min_call_interval_sec - (time.monotonic() - self._last_call_end_time)
            if gap > 0:
                log.debug("Claude call spacing: sleeping %.1fs before next call", gap)
                await asyncio.sleep(gap)

            try:
                # ── Circuit breaker ────────────────────────────────────────────────
                # If Claude has overloaded N consecutive times this session, skip to
                # Gemini immediately — but probe Claude periodically to restore primary.
                circuit_broken = (
                    fb.enabled
                    and self._overload_fallback_count >= fb.circuit_breaker_threshold
                )
                if circuit_broken:
                    probe_interval = fb.circuit_breaker_probe_min * 60
                    since = time.monotonic() - (self._circuit_broken_since or 0)
                    if since >= probe_interval:
                        log.info(
                            "Circuit breaker: probing Claude after %.0fmin break",
                            since / 60,
                        )
                        circuit_broken = False  # allow one attempt through
                    else:
                        log.warning(
                            "Circuit breaker active (%d consecutive overload fallbacks) — "
                            "using Gemini directly (Claude probe in %.0fmin)",
                            self._overload_fallback_count,
                            (probe_interval - since) / 60,
                        )
                        _fallback_args = (prompt, max_tokens)  # released after finally

                if _fallback_args is None:
                    # ── Primary: Claude with fast overload retries then Gemini fallback ─
                    overload_attempt = 0
                    server_attempt = 0

                    _timeout = getattr(self._claude_cfg, "api_call_timeout_sec", 200.0)
                    while True:
                        try:
                            t0 = time.monotonic()
                            response = await asyncio.wait_for(
                                self._client.messages.create(
                                    model=model_override or self._claude_cfg.model,
                                    max_tokens=max_tokens,
                                    messages=[{"role": "user", "content": prompt}],
                                ),
                                timeout=_timeout,
                            )
                            elapsed = time.monotonic() - t0
                            if elapsed > 60:
                                log.warning("Claude API call took %.1fs (>60s threshold)", elapsed)
                            else:
                                log.debug("Claude API call completed in %.1fs", elapsed)

                            if response.stop_reason == "max_tokens":
                                log.warning(
                                    "Claude response truncated (stop_reason=max_tokens). "
                                    "Parsed JSON may be incomplete — defensive parser will handle."
                                )

                            self._last_input_tokens = response.usage.input_tokens
                            self._last_output_tokens = response.usage.output_tokens
                            log.info(
                                "Token usage: %d input, %d output",
                                self._last_input_tokens, self._last_output_tokens,
                            )
                            # Successful — reset circuit breaker
                            if self._overload_fallback_count > 0:
                                log.info(
                                    "Claude API recovered — resetting circuit breaker "
                                    "(was at %d consecutive fallbacks)",
                                    self._overload_fallback_count,
                                )
                                self._overload_fallback_count = 0
                                self._circuit_broken_since = None
                            return response.content[0].text

                        except asyncio.TimeoutError:
                            log.error("Claude API call timed out after %.0fs (attempt %d)", _timeout, overload_attempt + server_attempt + 1)
                            raise

                        except anthropic.RateLimitError as exc:
                            # 429 rate limit — use slow server-error curve (hard quota reset)
                            delay = min(fb.server_error_base_sec * (2 ** server_attempt), fb.server_error_max_sec)
                            log.warning(
                                "Claude rate limit (attempt %d), retrying in %.0fs: %s",
                                server_attempt + 1, delay, exc,
                            )
                            await asyncio.sleep(delay)
                            server_attempt += 1

                        except anthropic.APIStatusError as exc:
                            if exc.status_code < 500:
                                log.error("Claude API client error %d: %s", exc.status_code, exc)
                                raise

                            if exc.status_code == 529:
                                if overload_attempt < fb.overload_retries:
                                    delay = min(fb.overload_base_sec * (2 ** overload_attempt), fb.overload_max_sec)
                                    log.warning(
                                        "Claude overloaded (529) — attempt %d/%d, retrying in %.0fs "
                                        "(bot is alive, transient queue spike)",
                                        overload_attempt + 1, fb.overload_retries, delay,
                                    )
                                    await asyncio.sleep(delay)
                                    overload_attempt += 1
                                else:
                                    # Exhausted fast retries — schedule Gemini fallback after lock release
                                    self._overload_fallback_count += 1
                                    if self._circuit_broken_since is None:
                                        self._circuit_broken_since = time.monotonic()
                                    log.error(
                                        "Claude overloaded after %d retries — switching to Gemini fallback "
                                        "(session overload fallback count: %d)",
                                        fb.overload_retries, self._overload_fallback_count,
                                    )
                                    if not fb.enabled:
                                        raise
                                    _fallback_args = (prompt, max_tokens)
                                    break
                            else:
                                # Other 5xx — slow retries, no fallback limit
                                delay = min(fb.server_error_base_sec * (2 ** server_attempt), fb.server_error_max_sec)
                                log.warning(
                                    "Claude server error %d (attempt %d), retrying in %.0fs: %s",
                                    exc.status_code, server_attempt + 1, delay, exc,
                                )
                                await asyncio.sleep(delay)
                                server_attempt += 1
            finally:
                self._last_call_end_time = time.monotonic()

        if _fallback_args is not None:
            return await self._call_gemini_fallback(*_fallback_args)

    # ------------------------------------------------------------------
    # High-level reasoning methods
    # ------------------------------------------------------------------

    async def run_reasoning_cycle(
        self,
        portfolio: PortfolioState,
        watchlist: WatchlistState,
        market_data: dict,
        indicators: dict,
        trigger: str = "manual",
        skip_cache: bool = False,
        recommendation_outcomes: dict | None = None,
        recent_executions: list | None = None,
        execution_stats: dict | None = None,
        session_suppressed: dict[str, str] | None = None,
        claude_soft_rejections: dict[str, int] | None = None,
        daily_indicators: dict[str, dict] | None = None,
        all_indicators: dict | None = None,
        regime_assessment: dict | None = None,
        sector_regimes: dict | None = None,
        skip_position_reviews: bool = False,
        max_symbols_override: int | None = None,
        max_tokens_override: int | None = None,
        model_override: str | None = None,
        skip_context_fields: frozenset | None = None,
        use_emergency_prompt: bool = False,
        breach_context: str | None = None,
    ) -> Optional[ReasoningResult]:
        """
        Full reasoning cycle: check cache → [Haiku pre-screen] → assemble context
        → call Sonnet → parse → cache result.

        Phase 20: when compressor_enabled=True and the candidate pool exceeds
        tier1_max_symbols, Haiku pre-screens candidates and returns a ranked
        shortlist that replaces the deterministic composite-score sort.

        On startup, if a fresh cached response from today exists (< 60 min old)
        and skip_cache is False, returns the cached result without an API call.

        Returns None if parsing fails — caller should skip this cycle and try
        again at the next trigger event.

        Args:
            all_indicators:   Merged indicator dict (_all_indicators on orchestrator).
                              Used by the compressor so it sees all watchlist symbols.
                              Falls back to indicators if not provided.
            regime_assessment: Sonnet's latest regime assessment (from prior cycle).
                              Passed to the compressor for regime-aware ranking.
            sector_regimes:   Sonnet's latest sector regimes (from prior cycle).
        """
        if not skip_cache:
            cached = self._cache.load_latest_if_fresh()
            if cached and cached.get("parse_success") and cached.get("parsed_response"):
                log.info(
                    "Reasoning cycle: using cached response (%s)",
                    cached.get("timestamp", "unknown"),
                )
                return _result_from_raw_reasoning(cached["parsed_response"])

        # --- Phase 20: Haiku pre-screening -------------------------------------------
        # When the candidate pool exceeds tier1_max_symbols, Haiku ranks all candidates
        # and returns a shortlist. selected_symbols replaces composite-score sort in
        # assemble_reasoning_context. Falls back to deterministic sort on failure.
        selected_symbols: list[str] | None = None
        open_syms = {p.symbol for p in portfolio.positions}
        all_candidates = [
            e for e in watchlist.entries if e.symbol not in open_syms
        ]
        # Phase 22: in Tier 3 (Haiku is the reasoner), skip the compressor entirely.
        # Instead, take top N by composite score directly from all_candidates.
        effective_max = max_symbols_override or self._claude_cfg.tier1_max_symbols
        if model_override is not None:
            def _tier1_score_simple(entry) -> float:
                ind = (all_indicators or indicators).get(entry.symbol, {})
                raw = ind.get("signals") or {}
                ed = getattr(entry, "expected_direction", "either")
                if raw:
                    scores = compute_directional_scores(raw)
                    if ed == "long":  return scores["long"]
                    if ed == "short": return scores["short"]
                    return max(scores["long"], scores["short"])
                if ed == "long":  return float(ind.get("long_score",  0.0))
                if ed == "short": return float(ind.get("short_score", 0.0))
                return max(float(ind.get("long_score", 0.0)), float(ind.get("short_score", 0.0)))
            selected_symbols = [
                e.symbol for e in sorted(all_candidates, key=_tier1_score_simple, reverse=True)
                [:effective_max]
            ]
            log.info(
                "Tier 3 (Haiku): bypassing compressor — top %d by composite score",
                len(selected_symbols),
            )
        else:
            max_symbols_out = self._claude_cfg.compressor_max_symbols_out

        if model_override is None and self._compressor is not None and len(all_candidates) > max_symbols_out:
            try:
                comp_result = await self._compressor.compress(
                    all_candidates=all_candidates,
                    indicators=all_indicators or indicators,
                    market_data=market_data,
                    regime_assessment=regime_assessment,
                    sector_regimes=sector_regimes,
                    max_symbols_out=max_symbols_out,
                    cycle_id=trigger,
                )
                selected_symbols = comp_result.symbols
                if comp_result.from_fallback:
                    log.debug(
                        "ContextCompressor: fallback sort (%d → %d symbols)",
                        len(all_candidates), len(selected_symbols),
                    )
                else:
                    log.info(
                        "ContextCompressor: Haiku pre-screened %d → %d symbols%s",
                        len(all_candidates),
                        len(selected_symbols),
                        f" [needs_sonnet={comp_result.sonnet_reason}]"
                        if comp_result.needs_sonnet else "",
                    )
                if comp_result.needs_sonnet:
                    log.warning(
                        "ContextCompressor: needs_sonnet=%s (Sonnet cycle already running)",
                        comp_result.sonnet_reason,
                    )
            except Exception as exc:
                log.warning(
                    "ContextCompressor: unexpected error (%s) — proceeding without pre-screen", exc
                )
        # --- End Phase 20 pre-screening ---

        context = self.assemble_reasoning_context(
            portfolio, watchlist, market_data, indicators,
            recommendation_outcomes=recommendation_outcomes,
            recent_executions=recent_executions,
            execution_stats=execution_stats,
            session_suppressed=session_suppressed,
            claude_soft_rejections=claude_soft_rejections,
            daily_indicators=daily_indicators,
            selected_symbols=selected_symbols,
            skip_position_daily_signals=skip_position_reviews,
            skip_context_fields=skip_context_fields,
            max_symbols_override=max_symbols_override,
        )
        context_json = json.dumps(context, default=str)

        # Phase 22: select prompt and build context variables based on mode.
        if use_emergency_prompt:
            template = self._load_prompt("emergency_reasoning.txt")
            prompt_context: dict = {"context_json": context_json}
        else:
            template = self._load_prompt("reasoning.txt")
            if skip_position_reviews:
                position_review_notice = (
                    "⚠ SPLIT MODE — OVERRIDE INSTRUCTIONS STEP 1:\n"
                    "Position reviews are handled in a separate dedicated call that already ran.\n"
                    "DO NOT produce a position_reviews field. DO NOT review open positions.\n"
                    "The position_reviews key shown in the response format below MUST be omitted entirely.\n"
                    "Open positions appear below as a compact summary ONLY — their sole purpose is to\n"
                    "prevent you from re-proposing held symbols in new_opportunities or rejected_opportunities.\n"
                    "Skip directly to INSTRUCTIONS step 2 (evaluate watchlist_tier1 symbols).\n"
                )
            else:
                # Non-split mode: position reviews are embedded in this call.
                # Prepend any thesis breach notice so Sonnet knows which condition
                # triggered this cycle before reviewing positions.
                if breach_context:
                    position_review_notice = (
                        "⚠ THESIS BREACH DETECTED BY MONITORING SYSTEM:\n"
                        f"{breach_context}\n\n"
                        "A condition specified as thesis-breaking has been concretely detected in "
                        "current signals or news. Re-examine the affected position carefully. Do not "
                        "simply reaffirm your prior hold recommendation — evaluate whether this "
                        "specific condition warrants exit, stop adjustment, or target revision given "
                        "current price and context.\n\n"
                    )
                else:
                    position_review_notice = ""
            prompt_context = {
                "context_json": context_json,
                "position_review_notice": position_review_notice,
            }

        raw_text = await self.call_claude(
            template,
            prompt_context,
            max_tokens_override=max_tokens_override,
            model_override=model_override,
        )
        parsed = parse_claude_response(raw_text)

        self._cache.save(
            trigger=trigger,
            input_context=context,
            raw_response=raw_text,
            parsed_response=parsed,
            input_tokens=self._last_input_tokens,
            output_tokens=self._last_output_tokens,
        )

        if parsed is None:
            log.warning("Reasoning cycle: response unparseable — skipping cycle")
            return None

        result = _result_from_raw_reasoning(parsed)
        for rej in result.rejected_opportunities:
            log.info(
                "Rejected opportunity: %s — %s",
                rej.get("symbol", "?"), rej.get("rejection_reason", ""),
            )
        log.info(
            "Reasoning cycle complete [trigger=%s]: %d reviews, %d opportunities, %d rejected",
            trigger,
            len(parsed.get("position_reviews", [])),
            len(parsed.get("new_opportunities", [])),
            len(result.rejected_opportunities),
        )
        return result

    # ------------------------------------------------------------------
    # Tool-use call (used by watchlist build with web search)
    # ------------------------------------------------------------------

    # Web search tool definition — offered to Claude during watchlist builds
    # when Brave Search is configured.  Claude uses 2–3 queries to surface
    # near-term catalysts not visible in the screener candidates.
    # Extension point: to add a second tool, append another dict here and
    # handle it in the tool_executor passed to call_claude_with_tools.
    _WEB_SEARCH_TOOL: dict = {
        "name": "web_search",
        "description": (
            "Search the web for current financial news, earnings calendars, analyst "
            "actions, and market catalysts. Use 2–3 targeted queries per watchlist "
            "build. Focus on near-term catalysts: earnings this week, analyst upgrades, "
            "sector rotation, breakout setups with news backing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query string."}
            },
            "required": ["query"],
        },
    }

    async def call_claude_with_tools(
        self,
        prompt_template: str,
        context: dict,
        tools: list[dict],
        tool_executor,   # Callable[[str, dict], Awaitable[str]]
        max_tokens_override: int | None = None,
        max_tool_rounds: int = 3,
    ) -> str:
        """
        Multi-turn Claude call with tool use support.

        Fills the prompt template, then runs a conversation loop:
          1. Call Claude with tools offered.
          2. If stop_reason == "tool_use": execute each tool_use block and
             append results, then continue the loop.
          3. If stop_reason != "tool_use" (i.e. "end_turn" or "max_tokens"):
             extract and return the text from the final content block.
          4. If max_tool_rounds exhausted: force a final call without tools
             to get a text response.

        All retry/circuit-breaker logic from call_claude applies (529, 5xx,
        RateLimitError). Gemini fallback is used on the forced final call if
        the circuit is open.

        The caller does not need to know tool use happened — the method always
        returns a plain text string.
        """
        from typing import Callable, Awaitable

        missing: list[str] = []

        def _sub(m: re.Match) -> str:
            key = m.group(1)
            if key in context:
                return str(context[key])
            missing.append(key)
            return m.group(0)

        prompt = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, prompt_template)
        if missing:
            raise ValueError(f"Prompt template missing placeholder key(s): {missing}")

        max_tokens = max_tokens_override or self._claude_cfg.max_tokens_per_cycle
        messages = [{"role": "user", "content": prompt}]

        for round_num in range(max_tool_rounds):
            response = await self._call_claude_raw(messages, max_tokens, tools=tools)
            # Extract text blocks and tool_use blocks from response
            if response.stop_reason != "tool_use":
                # Done — extract text from last content block
                for block in reversed(response.content):
                    if hasattr(block, "text"):
                        return block.text
                return ""  # no text block (shouldn't happen)

            # Process tool calls and append results
            assistant_content = response.content
            tool_results = []
            for block in assistant_content:
                if block.type != "tool_use":
                    continue
                try:
                    tool_output = await tool_executor(block.name, block.input)
                except Exception as exc:
                    log.warning("Tool executor error for %r — %s", block.name, exc)
                    tool_output = f"Tool error: {exc}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": tool_output,
                })

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

        # Rounds exhausted — force a final response without tools
        log.warning(
            "call_claude_with_tools: tool use rounds exhausted (%d) — "
            "forcing final response without tools",
            max_tool_rounds,
        )
        response = await self._call_claude_raw(
            messages, max_tokens, tools=[], tool_choice={"type": "none"}
        )
        for block in reversed(response.content):
            if hasattr(block, "text"):
                return block.text
        return ""

    async def _call_claude_raw(self, messages: list, max_tokens: int, tools: list | None = None, tool_choice: dict | None = None):
        """
        Low-level Claude API call with full retry/fallback logic.
        Shared by call_claude (single-turn) and call_claude_with_tools (multi-turn).
        Returns the raw Anthropic response object.
        """
        fb = self._cfg.ai_fallback

        async with self._call_lock:
            gap = self._claude_cfg.min_call_interval_sec - (time.monotonic() - self._last_call_end_time)
            if gap > 0:
                log.debug("Claude call spacing (tools): sleeping %.1fs before next call", gap)
                await asyncio.sleep(gap)

            try:
                circuit_broken = (
                    fb.enabled
                    and self._overload_fallback_count >= fb.circuit_breaker_threshold
                )
                if circuit_broken:
                    probe_interval = fb.circuit_breaker_probe_min * 60
                    since = time.monotonic() - (self._circuit_broken_since or 0)
                    if since >= probe_interval:
                        circuit_broken = False
                    else:
                        raise RuntimeError(
                            f"Claude circuit breaker active — {self._overload_fallback_count} consecutive "
                            f"overload fallbacks. Tool-use calls cannot fall back to Gemini."
                        )

                overload_attempt = 0
                server_attempt = 0
                _timeout = getattr(self._claude_cfg, "api_call_timeout_sec", 200.0)
                kwargs: dict = {"model": self._claude_cfg.model, "max_tokens": max_tokens, "messages": messages}
                if tools is not None:
                    kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice

                while True:
                    try:
                        t0 = time.monotonic()
                        response = await asyncio.wait_for(
                            self._client.messages.create(**kwargs),
                            timeout=_timeout,
                        )
                        elapsed = time.monotonic() - t0
                        if elapsed > 60:
                            log.warning("Claude API call (tools) took %.1fs", elapsed)
                        self._last_input_tokens = response.usage.input_tokens
                        self._last_output_tokens = response.usage.output_tokens
                        if self._overload_fallback_count > 0:
                            self._overload_fallback_count = 0
                            self._circuit_broken_since = None
                        return response

                    except asyncio.TimeoutError:
                        log.error("Claude API call (tools) timed out after %.0fs", _timeout)
                        raise

                    except anthropic.RateLimitError as exc:
                        delay = min(fb.server_error_base_sec * (2 ** server_attempt), fb.server_error_max_sec)
                        log.warning("Claude rate limit (tools, attempt %d), retrying in %.0fs: %s", server_attempt + 1, delay, exc)
                        await asyncio.sleep(delay)
                        server_attempt += 1

                    except anthropic.APIStatusError as exc:
                        if exc.status_code < 500:
                            log.error("Claude API client error %d (tools): %s", exc.status_code, exc)
                            raise
                        if exc.status_code == 529:
                            if overload_attempt < fb.overload_retries:
                                delay = min(fb.overload_base_sec * (2 ** overload_attempt), fb.overload_max_sec)
                                log.warning("Claude overloaded (tools, 529) attempt %d/%d, retrying %.0fs", overload_attempt + 1, fb.overload_retries, delay)
                                await asyncio.sleep(delay)
                                overload_attempt += 1
                            else:
                                self._overload_fallback_count += 1
                                if self._circuit_broken_since is None:
                                    self._circuit_broken_since = time.monotonic()
                                log.error("Claude overloaded (tools) after %d retries — raising (no Gemini fallback for tool calls)", fb.overload_retries)
                                raise
                        else:
                            delay = min(fb.server_error_base_sec * (2 ** server_attempt), fb.server_error_max_sec)
                            log.warning("Claude server error %d (tools, attempt %d), retrying %.0fs", exc.status_code, server_attempt + 1, delay)
                            await asyncio.sleep(delay)
                            server_attempt += 1
            finally:
                self._last_call_end_time = time.monotonic()

    async def run_watchlist_build(
        self,
        market_context: dict,
        current_watchlist: WatchlistState,
        target_count: int = 8,
        candidates: list[dict] | None = None,
        search_adapter=None,   # SearchAdapter | None
        no_entry_symbols: list[str] | None = None,
    ) -> Optional[WatchlistResult]:
        """
        Dedicated watchlist population cycle. Called on startup or when the
        watchlist has fewer than 10 tickers.

        Args:
            candidates:     RVOL-ranked candidate list from UniverseScanner.
                            Serialized as JSON in the {candidates} template slot.
                            Pass None (or empty list) to fall back to the original
                            prompt behaviour (no screener data).
            search_adapter: SearchAdapter instance. When enabled, Claude is offered
                            the web_search tool so it can research catalysts live.
                            When None or disabled, behaves identically to pre-Phase-18.

        Returns None if parsing fails.
        """
        # Include expected_direction so the build can identify direction-conflicting entries
        # and remove them when the sector regime has flipped since they were added.
        def _fmt_wl_entry(e) -> str:
            return f"{e.symbol}(dir:{getattr(e, 'expected_direction', 'either')},tier:{e.priority_tier})"
        watchlist_str = ", ".join(_fmt_wl_entry(e) for e in current_watchlist.entries) if current_watchlist.entries else "none"
        market_ctx_str = json.dumps(market_context, default=str, indent=2)
        candidates_str = json.dumps(candidates, indent=2) if candidates else "none"

        no_entry_str = ", ".join(sorted(no_entry_symbols)) if no_entry_symbols else "none"
        ctx = {
            "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "market_context": market_ctx_str,
            "current_watchlist": watchlist_str,
            "target_count": target_count,
            "candidates": candidates_str,
            "no_entry_symbols": no_entry_str,
        }

        template = self._load_prompt("watchlist.txt")

        # Use tool-use path when Brave Search is configured; otherwise plain call.
        if search_adapter is not None and getattr(search_adapter, "enabled", False):
            async def _execute_search_tool(name: str, inputs: dict) -> str:
                if name != "web_search":
                    return "Unknown tool."
                results = await search_adapter.search(
                    inputs.get("query", ""),
                    n_results=self._cfg.search.result_count_per_query,
                )
                if not results:
                    return "No results returned."
                return "\n\n".join(
                    f"{r['title']}\n{r.get('description', '')}" for r in results
                )

            raw_text = await self.call_claude_with_tools(
                template, ctx,
                tools=[self._WEB_SEARCH_TOOL],
                tool_executor=_execute_search_tool,
                max_tool_rounds=self._cfg.search.max_searches_per_build,
            )
        else:
            raw_text = await self.call_claude(template, ctx)

        parsed = parse_claude_response(raw_text)
        if parsed is None:
            log.warning("Watchlist build: response unparseable — skipping")
            return None

        log.info(
            "Watchlist build complete: %d tickers suggested",
            len(parsed.get("watchlist", [])),
        )
        return _result_from_raw_watchlist(parsed)

    async def run_position_review(
        self,
        position: Position,
        market_context: dict,
        indicators: dict,
    ) -> Optional[ReviewResult]:
        """
        Focused single-position review. Called when a position triggers a review
        event (large price move, time ceiling, thesis review threshold).

        Returns None if parsing fails.
        """
        sig_summary = indicators.get(position.symbol, {})
        signals = sig_summary.get("signals", sig_summary)
        tech_summary = _make_technical_summary(signals)

        position_detail = json.dumps({
            "symbol": position.symbol,
            "shares": position.shares,
            "avg_cost": position.avg_cost,
            "entry_date": position.entry_date,
            "intention": {
                "catalyst": position.intention.catalyst,
                "direction": position.intention.direction,
                "strategy": position.intention.strategy,
                "expected_move": position.intention.expected_move,
                "reasoning": position.intention.reasoning,
                "exit_targets": {
                    "profit_target": position.intention.exit_targets.profit_target,
                    "stop_loss": position.intention.exit_targets.stop_loss,
                },
                "max_expected_loss": position.intention.max_expected_loss,
                "review_notes": position.intention.review_notes,
            },
        }, indent=2)

        template = self._load_prompt("review.txt")
        raw_text = await self.call_claude(template, {
            "position_detail": position_detail,
            "market_context": json.dumps(market_context, default=str, indent=2),
            "indicators": json.dumps(
                {
                    "symbol": position.symbol,
                    "signals": signals,
                    "long_score":  sig_summary.get("long_score"),
                    "short_score": sig_summary.get("short_score"),
                    "summary": tech_summary,
                },
                indent=2,
            ),
        })

        parsed = parse_claude_response(raw_text)
        if parsed is None:
            log.warning(
                "Position review for %s: response unparseable — skipping",
                position.symbol,
            )
            return None

        log.info("Position review complete: %s", position.symbol)
        return _result_from_raw_review(parsed)

    async def run_thesis_challenge(
        self,
        opportunity: dict,
        market_context: dict,
        portfolio: dict,
    ) -> Optional[dict]:
        """
        Concern-level review of a proposed large-position entry.

        Returns dict with {concern_level: float, reasoning: str} where concern_level
        is 0.0–1.0 (higher = more concern). The caller applies this as a bounded size
        penalty; the trade is never blocked by this call.

        Returns None if parsing fails (caller proceeds with original sizing).
        """
        symbol = opportunity.get("symbol", "?")
        template = self._load_prompt("thesis_challenge.txt")

        raw_text = await self.call_claude(
            template,
            {
                "opportunity_json": json.dumps(opportunity, indent=2),
                "market_context_json": json.dumps(market_context, default=str, indent=2),
                "portfolio_json": json.dumps(portfolio, default=str, indent=2),
            },
            max_tokens_override=256,
        )

        parsed = parse_claude_response(raw_text)
        if parsed is None:
            log.warning("Thesis challenge for %s: unparseable — using original sizing", symbol)
            return None

        concern = parsed.get("concern_level", 0.0)
        reasoning = parsed.get("reasoning", "")
        log.info(
            "Thesis challenge [%s]: concern_level=%.2f  reason=%s",
            symbol, concern, reasoning,
        )
        return parsed
