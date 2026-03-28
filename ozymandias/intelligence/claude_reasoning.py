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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import anthropic

from ozymandias.core.config import ClaudeConfig, Config
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import PortfolioState, Position, WatchlistState
from ozymandias.intelligence.technical_analysis import compute_composite_score

log = logging.getLogger(__name__)

# Rough chars-per-token estimate for context size guard.
# Accurate for structured JSON (context JSON estimated ≈ actual API tokens).
_CHARS_PER_TOKEN = 4
# Total token budget for the full API call (context JSON + prompt template).
# Context trim guard uses: context_budget = _TOTAL_TOKEN_BUDGET - prompt_template_tokens,
# both measured in chars/4 units. 25,000 keeps full-prompt cost well under $0.10/call
# while accommodating 30+ watchlist symbols without trimming.
_TOTAL_TOKEN_BUDGET = 25_000


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


@dataclass
class WatchlistResult:
    """Parsed output from a watchlist build cycle."""
    watchlist: list[dict]             # [{symbol, reason, priority_tier, strategy}]
    market_notes: str
    raw: dict


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


def _result_from_raw_reasoning(raw: dict) -> ReasoningResult:
    # session_veto must be a list of strings; guard against Claude returning wrong type
    raw_veto = raw.get("session_veto", [])
    session_veto = [str(v) for v in raw_veto] if isinstance(raw_veto, list) else []
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
    )


def _result_from_raw_watchlist(raw: dict) -> WatchlistResult:
    return WatchlistResult(
        watchlist=raw.get("watchlist", []),
        market_notes=raw.get("market_notes", ""),
        raw=raw,
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
        max_tier1 = self._claude_cfg.tier1_max_symbols
        now_utc = datetime.now(timezone.utc)

        # --- Current positions (always Tier 1) ---
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
        # Sort by current composite technical score descending so Claude always
        # sees the highest-signal symbols. Without this, the slice is insertion-
        # order, meaning the same seed symbols appear every cycle regardless of
        # what the rest of the watchlist is doing.
        slots = max(0, max_tier1 - len(position_entries))

        # Exclude symbols already held as open positions: they appear in the positions
        # block above and Claude reviews them there. Including them here causes Claude
        # to re-propose them as new entry candidates and triggers false "implicit
        # rejection" warnings in the orchestrator.
        # NOTE: if position scaling (adding to existing positions) is ever implemented,
        # this filter should be removed or conditioned on whether the strategy supports
        # pyramiding — held symbols would legitimately need to appear as candidates again.
        open_position_symbols = {pos.symbol for pos in portfolio.positions}
        all_tier1 = [
            e for e in watchlist.entries
            if e.priority_tier == 1 and e.symbol not in open_position_symbols
        ]
        total_tier1 = len(all_tier1)

        def _tier1_score(entry) -> float:
            ind = indicators.get(entry.symbol, {})
            raw = ind.get("signals") or {}
            if raw:
                # Phase 15: use direction-adjusted score when expected_direction is set;
                # fall back to max(long, short) for "either" so any directional setup
                # ranks appropriately regardless of bias.
                ed = getattr(entry, "expected_direction", "either")
                if ed != "either":
                    return compute_composite_score(raw, direction=ed)
                return max(
                    compute_composite_score(raw, direction="long"),
                    compute_composite_score(raw, direction="short"),
                )
            return ind.get("composite_technical_score", 0.0)

        tier1_watch = sorted(all_tier1, key=_tier1_score, reverse=True)[:slots]

        # Phase 15: build ta_readiness dict replacing technical_summary string.
        # ta_readiness is a direct pass-through of indicators[symbol]["signals"]
        # with a direction-adjusted composite_score added.
        # _make_technical_summary is retained for run_position_review (not removed).
        watchlist_tier1: list[dict] = []
        for entry in tier1_watch:
            sym = entry.symbol
            sig_summary = indicators.get(sym, {})
            raw_signals = sig_summary.get("signals", {})
            ed = getattr(entry, "expected_direction", "either")
            # Direction-adjusted composite score: mirror the tier-1 sort-key logic so
            # Claude sees the same score used for ranking. "either" takes the better of
            # long/short rather than defaulting to long (which understates bearish setups).
            if ed == "either":
                direction_score = (
                    max(
                        compute_composite_score(raw_signals, direction="long"),
                        compute_composite_score(raw_signals, direction="short"),
                    )
                    if raw_signals
                    else float(sig_summary.get("composite_technical_score") or 0.0)
                )
            else:
                direction_score = (
                    compute_composite_score(raw_signals, direction=ed)
                    if raw_signals
                    else float(sig_summary.get("composite_technical_score") or 0.0)
                )
            # ta_readiness: pass through all live signals + direction-adjusted composite_score
            ta_readiness: dict = dict(raw_signals) if raw_signals else {}
            ta_readiness["composite_score"] = round(direction_score, 4)

            watchlist_tier1.append({
                "symbol": sym,
                "latest_price": raw_signals.get("price") if raw_signals else sig_summary.get("price"),
                "ta_readiness": ta_readiness,
                "strategy": entry.strategy,
                "reason": entry.reason,
                "expected_direction": ed,
            })

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

        _suppressed = session_suppressed or {}
        context: dict[str, Any] = {
            "portfolio": {
                "cash": portfolio.cash,
                "buying_power": portfolio.buying_power,
                "positions": position_entries,
            },
            "watchlist_tier1": watchlist_tier1,
            "market_context": market_data,
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
        response = await asyncio.wait_for(
            self._fallback_client.generate_content_async(
                prompt,
                generation_config={"max_output_tokens": max_tokens},
            ),
            timeout=120.0,
        )
        text = response.text or ""
        log.info("Gemini fallback call succeeded (%d chars)", len(text))
        return text

    async def call_claude(
        self,
        prompt_template: str,
        context: dict,
        max_tokens_override: int | None = None,
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

                    while True:
                        try:
                            t0 = time.monotonic()
                            response = await asyncio.wait_for(
                                self._client.messages.create(
                                    model=self._claude_cfg.model,
                                    max_tokens=max_tokens,
                                    messages=[{"role": "user", "content": prompt}],
                                ),
                                timeout=120.0,
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
                            log.debug(
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
                            log.error("Claude API call timed out after 120s (attempt %d)", overload_attempt + server_attempt + 1)
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
    ) -> Optional[ReasoningResult]:
        """
        Full reasoning cycle: check cache → assemble context → call Claude
        → parse → cache result.

        On startup, if a fresh cached response from today exists (< 60 min old)
        and skip_cache is False, returns the cached result without an API call.

        Returns None if parsing fails — caller should skip this cycle and try
        again at the next trigger event.
        """
        if not skip_cache:
            cached = self._cache.load_latest_if_fresh()
            if cached and cached.get("parse_success") and cached.get("parsed_response"):
                log.info(
                    "Reasoning cycle: using cached response (%s)",
                    cached.get("timestamp", "unknown"),
                )
                return _result_from_raw_reasoning(cached["parsed_response"])

        context = self.assemble_reasoning_context(
            portfolio, watchlist, market_data, indicators,
            recommendation_outcomes=recommendation_outcomes,
            recent_executions=recent_executions,
            execution_stats=execution_stats,
            session_suppressed=session_suppressed,
            claude_soft_rejections=claude_soft_rejections,
            daily_indicators=daily_indicators,
        )
        context_json = json.dumps(context, default=str, indent=2)
        template = self._load_prompt("reasoning.txt")

        raw_text = await self.call_claude(template, {"context_json": context_json})
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
                            timeout=120.0,
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
                        log.error("Claude API call (tools) timed out after 120s")
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
        current_symbols = [e.symbol for e in current_watchlist.entries]
        watchlist_str = ", ".join(current_symbols) if current_symbols else "none"
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
                    "composite_score": sig_summary.get("composite_technical_score"),
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
