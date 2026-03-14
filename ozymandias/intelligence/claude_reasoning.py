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
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import anthropic

from ozymandias.core.config import ClaudeConfig, Config
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import PortfolioState, Position, WatchlistState

log = logging.getLogger(__name__)

# Rough chars-per-token estimate for context size guard
_CHARS_PER_TOKEN = 4
_TOKEN_TARGET_MAX = 8_000


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
    raw: dict                         # full parsed Claude response


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
    if rsi is not None:
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
    return ReasoningResult(
        timestamp=raw.get("timestamp", datetime.now(timezone.utc).isoformat()),
        position_reviews=raw.get("position_reviews", []),
        new_opportunities=raw.get("new_opportunities", []),
        watchlist_changes=raw.get("watchlist_changes", {"add": [], "remove": [], "rationale": ""}),
        market_assessment=raw.get("market_assessment", ""),
        risk_flags=raw.get("risk_flags", []),
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
        self._client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
        self._prompts_dir = prompts_dir or config.prompts_dir
        self._last_input_tokens: int = 0
        self._last_output_tokens: int = 0

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
        """
        max_tier1 = self._claude_cfg.tier1_max_symbols

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
            position_entries.append({
                "symbol": pos.symbol,
                "shares": pos.shares,
                "avg_cost": pos.avg_cost,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
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
                    "entry_date": pos.intention.entry_date or pos.entry_date,
                    "review_notes": pos.intention.review_notes[-3:],
                },
            })

        # --- Tier 1 watchlist candidates (fill remaining budget) ---
        slots = max(0, max_tier1 - len(position_entries))
        tier1_watch = [e for e in watchlist.entries if e.priority_tier == 1][:slots]

        watchlist_tier1: list[dict] = []
        for entry in tier1_watch:
            sym = entry.symbol
            sig_summary = indicators.get(sym, {})
            signals = sig_summary.get("signals", sig_summary)
            watchlist_tier1.append({
                "symbol": sym,
                "latest_price": signals.get("price"),
                "technical_summary": _make_technical_summary(signals),
                "composite_score": sig_summary.get("composite_technical_score"),
                "strategy": entry.strategy,
                "reason": entry.reason,
            })

        context: dict[str, Any] = {
            "portfolio": {
                "cash": portfolio.cash,
                "buying_power": portfolio.buying_power,
                "positions": position_entries,
            },
            "watchlist_tier1": watchlist_tier1,
            "market_context": market_data,
        }

        # --- Token guard: trim watchlist until under target ---
        context_json = json.dumps(context, default=str)
        while _estimate_tokens(context_json) > _TOKEN_TARGET_MAX and context["watchlist_tier1"]:
            context["watchlist_tier1"].pop()
            context_json = json.dumps(context, default=str)

        log.debug(
            "Context: %d positions, %d tier1 watchlist, ~%d tokens",
            len(position_entries),
            len(context["watchlist_tier1"]),
            _estimate_tokens(context_json),
        )
        return context

    # ------------------------------------------------------------------
    # API call with retry
    # ------------------------------------------------------------------

    async def call_claude(self, prompt_template: str, context: dict) -> str:
        """
        Fill the prompt template with context values and call the Anthropic API.

        Retry policy:
          - RateLimitError (429): exponential backoff, base 30s, max 10 min.
          - APIStatusError 5xx: same backoff.
          - APIStatusError 4xx (non-429): re-raised immediately (not retryable).
          - TimeoutError (>120s): re-raised immediately.

        Logs a WARNING if a successful call takes longer than 60 seconds.

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

        base_delay = 30.0
        max_delay = 600.0
        attempt = 0

        while True:
            try:
                t0 = time.monotonic()
                response = await asyncio.wait_for(
                    self._client.messages.create(
                        model=self._claude_cfg.model,
                        max_tokens=self._claude_cfg.max_tokens_per_cycle,
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    timeout=120.0,
                )
                elapsed = time.monotonic() - t0
                if elapsed > 60:
                    log.warning("Claude API call took %.1fs (>60s threshold)", elapsed)
                else:
                    log.debug("Claude API call completed in %.1fs", elapsed)

                self._last_input_tokens = response.usage.input_tokens
                self._last_output_tokens = response.usage.output_tokens
                log.debug(
                    "Token usage: %d input, %d output",
                    self._last_input_tokens, self._last_output_tokens,
                )
                return response.content[0].text

            except asyncio.TimeoutError:
                log.error("Claude API call timed out after 120s (attempt %d)", attempt + 1)
                raise

            except anthropic.RateLimitError as exc:
                delay = min(base_delay * (2 ** attempt), max_delay)
                log.warning(
                    "Claude rate limit (attempt %d), retrying in %.0fs: %s",
                    attempt + 1, delay, exc,
                )
                await asyncio.sleep(delay)
                attempt += 1

            except anthropic.APIStatusError as exc:
                if exc.status_code < 500:
                    log.error("Claude API client error %d: %s", exc.status_code, exc)
                    raise
                delay = min(base_delay * (2 ** attempt), max_delay)
                log.warning(
                    "Claude server error %d (attempt %d), retrying in %.0fs: %s",
                    exc.status_code, attempt + 1, delay, exc,
                )
                await asyncio.sleep(delay)
                attempt += 1

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
            portfolio, watchlist, market_data, indicators
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

        log.info(
            "Reasoning cycle complete [trigger=%s]: %d reviews, %d opportunities",
            trigger,
            len(parsed.get("position_reviews", [])),
            len(parsed.get("new_opportunities", [])),
        )
        return _result_from_raw_reasoning(parsed)

    async def run_watchlist_build(
        self,
        market_context: dict,
        current_watchlist: WatchlistState,
        target_count: int = 20,
    ) -> Optional[WatchlistResult]:
        """
        Dedicated watchlist population cycle. Called on startup or when the
        watchlist has fewer than 10 tickers.

        Returns None if parsing fails.
        """
        current_symbols = [e.symbol for e in current_watchlist.entries]
        watchlist_str = ", ".join(current_symbols) if current_symbols else "none"
        market_ctx_str = json.dumps(market_context, default=str, indent=2)

        template = self._load_prompt("watchlist.txt")
        raw_text = await self.call_claude(template, {
            "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "market_context": market_ctx_str,
            "current_watchlist": watchlist_str,
            "target_count": target_count,
        })

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
