"""
Haiku context compressor — pre-screener for strategic reasoning context.

Called before Sonnet's run_reasoning_cycle to rank and filter watchlist
candidates so Sonnet sees only the most actionable symbols.

Gate: only fires when len(all_candidates) > compressor_max_symbols_out.
Fallback: deterministic composite-score sort on any failure.

Extension point: to add a new needs_sonnet trigger reason, add it to
NEEDS_SONNET_REASONS below. Orchestrator handles these in _run_claude_cycle.

Phase 20.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic

from ozymandias.core.config import ClaudeConfig
from ozymandias.intelligence.technical_analysis import compute_composite_score

log = logging.getLogger(__name__)

# needs_sonnet trigger reasons (typed constants — one entry per reason type).
# To add a new reason: add one string here and handle it in orchestrator._run_claude_cycle.
NEEDS_SONNET_REASONS = frozenset({
    "regime_shift",           # Haiku sees signals inconsistent with current regime_assessment
    "all_candidates_failing", # All candidates have weak signals relative to regime expectations
    "position_thesis_breach", # A position's thesis_breaking_conditions are now met (Phase 21)
    "watchlist_stale",        # Candidate pool irrelevant to current market context
})


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CompressorResult:
    symbols: list[str]                           # ordered by Haiku priority (most actionable first)
    rationale: dict[str, str] = field(default_factory=dict)  # symbol → rationale string (debug)
    notes: str = ""
    from_fallback: bool = False                  # True if deterministic fallback was used
    needs_sonnet: bool = False                   # True if Haiku flagged a condition for Sonnet
    sonnet_reason: Optional[str] = None          # one of NEEDS_SONNET_REASONS or None


# ---------------------------------------------------------------------------
# Entry accessors (handle WatchlistEntry objects and plain dicts uniformly)
# ---------------------------------------------------------------------------

def _sym(entry) -> str:
    return entry.symbol if hasattr(entry, "symbol") else entry.get("symbol", "")


def _attr(entry, attr: str, default=None):
    if hasattr(entry, attr):
        return getattr(entry, attr)
    return entry.get(attr, default) if isinstance(entry, dict) else default


# ---------------------------------------------------------------------------
# Compressor
# ---------------------------------------------------------------------------

class ContextCompressor:
    """
    Haiku-based pre-screener that ranks watchlist candidates before Sonnet reasoning.

    Selects the most actionable symbols from the full candidate pool,
    routing them through Sonnet's context assembly in priority order.

    Usage::

        compressor = ContextCompressor(config.claude, prompts_dir)
        result = await compressor.compress(
            all_candidates, indicators, market_data,
            regime_assessment, sector_regimes, max_symbols_out
        )
        # result.symbols = ordered shortlist for Sonnet
        # result.from_fallback = True when deterministic sort was used
    """

    def __init__(
        self,
        config: ClaudeConfig,
        prompts_dir: Optional[Path] = None,
    ) -> None:
        self._cfg = config
        self._client = anthropic.AsyncAnthropic(max_retries=0)
        self._prompts_dir = prompts_dir
        # Per-cycle guard: prevents needs_sonnet from firing more than once
        # per Sonnet reasoning cycle. Keyed by caller-provided cycle_id.
        self._last_needs_sonnet_cycle: str = ""

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def compress(
        self,
        all_candidates: list,
        indicators: dict,
        market_data: dict,
        regime_assessment: Optional[dict],
        sector_regimes: Optional[dict],
        max_symbols_out: int,
        cycle_id: str = "",
    ) -> CompressorResult:
        """
        Rank and filter candidates using Haiku. Falls back to deterministic
        composite-score sort on any failure (API error, parse error, etc.).

        Args:
            all_candidates:   All watchlist entries (WatchlistEntry or dict).
                              Caller excludes open position symbols.
            indicators:       symbol → indicator dict (from _all_indicators).
            market_data:      Market context dict from _build_market_context.
            regime_assessment: Sonnet's latest regime_assessment (may be None).
            sector_regimes:   Sonnet's latest sector_regimes (may be None).
            max_symbols_out:  Number of symbols to return (= tier1_max_symbols).
            cycle_id:         Unique ID for this Sonnet cycle; used to gate
                              needs_sonnet so it fires at most once per cycle.

        Returns:
            CompressorResult with ordered symbol list.
        """
        if not all_candidates:
            return CompressorResult(symbols=[], from_fallback=True)

        # Gate: only run Haiku when candidate pool exceeds output cap.
        # Caller is responsible for checking this gate before calling, but
        # we enforce it here as well to avoid unnecessary API calls.
        if len(all_candidates) <= max_symbols_out:
            return self._fallback_sort(all_candidates, indicators, max_symbols_out)

        prompt_template = self._load_prompt()
        if not prompt_template:
            log.debug("ContextCompressor: no compress.txt — using fallback sort")
            return self._fallback_sort(all_candidates, indicators, max_symbols_out)

        candidate_payload = self._build_candidate_payload(all_candidates, indicators)
        context = {
            "candidates_json": json.dumps(candidate_payload, default=str),
            "market_context_json": json.dumps(
                {
                    k: v
                    for k, v in market_data.items()
                    if k in (
                        "spy_trend", "trading_session",
                        "spy_daily", "qqq_daily", "sector_dispersion",
                    )
                },
                default=str,
            ),
            "regime_json": json.dumps(
                {
                    "regime_assessment": regime_assessment,
                    "sector_regimes": sector_regimes,
                },
                default=str,
            ),
            "max_symbols": max_symbols_out,
        }

        prompt = self._fill_template(prompt_template, context)

        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._cfg.compressor_model,
                    max_tokens=self._cfg.compressor_max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=30.0,
            )
            raw_text = response.content[0].text if response.content else ""
            log.debug(
                "ContextCompressor: Haiku call complete (%d chars output)", len(raw_text)
            )
        except Exception as exc:
            log.warning(
                "ContextCompressor: Haiku call failed (%s) — using fallback sort", exc
            )
            return self._fallback_sort(all_candidates, indicators, max_symbols_out)

        return self._parse_response(
            raw_text, all_candidates, indicators, max_symbols_out, cycle_id
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_prompt(self) -> Optional[str]:
        if self._prompts_dir is None:
            return None
        path = self._prompts_dir / "compress.txt"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _build_candidate_payload(self, all_candidates: list, indicators: dict) -> list[dict]:
        """Build a compact per-symbol dict for the Haiku prompt."""
        payload: list[dict] = []
        for entry in all_candidates:
            sym = _sym(entry)
            if not sym:
                continue
            ed = _attr(entry, "expected_direction", "either") or "either"
            tier = _attr(entry, "priority_tier", 1) or 1
            ind = indicators.get(sym, {})
            raw = ind.get("signals") or {}
            if raw and ed != "either":
                score = compute_composite_score(raw, direction=ed)
            elif raw:
                score = max(
                    compute_composite_score(raw, direction="long"),
                    compute_composite_score(raw, direction="short"),
                )
            else:
                score = ind.get("composite_technical_score", 0.0) or 0.0

            item: dict = {
                "symbol": sym,
                "expected_direction": ed,
                "tier": tier,
                "composite_score": round(score, 3),
            }
            # Key signals only — avoids blowing out Haiku's context
            if raw:
                for k in ("rsi", "volume_ratio", "vwap_position", "trend_structure", "roc_5"):
                    v = raw.get(k)
                    if v is not None:
                        item[k] = v
            reason = _attr(entry, "reason", "") or ""
            if reason:
                item["reason"] = reason[:120]
            payload.append(item)
        return payload

    def _fallback_sort(
        self,
        all_candidates: list,
        indicators: dict,
        max_symbols_out: int,
    ) -> CompressorResult:
        """Deterministic fallback: sort candidates by direction-adjusted composite score."""

        def _score(entry) -> float:
            sym = _sym(entry)
            ind = indicators.get(sym, {})
            raw = ind.get("signals") or {}
            if raw:
                ed = _attr(entry, "expected_direction", "either") or "either"
                if ed != "either":
                    return compute_composite_score(raw, direction=ed)
                return max(
                    compute_composite_score(raw, direction="long"),
                    compute_composite_score(raw, direction="short"),
                )
            return ind.get("composite_technical_score", 0.0) or 0.0

        sorted_entries = sorted(all_candidates, key=_score, reverse=True)[:max_symbols_out]
        return CompressorResult(
            symbols=[_sym(e) for e in sorted_entries],
            from_fallback=True,
        )

    # ------------------------------------------------------------------
    # Phase 21: position thesis monitoring
    # ------------------------------------------------------------------

    def check_position_theses(
        self,
        positions: list,
        active_theses: Optional[list[dict]],
        indicators: dict,
        cycle_id: str = "",
    ) -> Optional[CompressorResult]:
        """
        Check whether any open position's thesis_breaking_conditions are now met.

        Called from the medium loop once per cycle. Returns a CompressorResult
        with `needs_sonnet=True` and `sonnet_reason="position_thesis_breach"` if
        any breach is detected, so the orchestrator can fire a Sonnet review cycle.

        Conditions are evaluated by substring match against live indicator values
        (same approach as _check_regime_conditions in the orchestrator):
          - "daily_trend becomes downtrend" → check daily_indicators.daily_trend == "downtrend"
          - "sector_1w_return < -5%" → not evaluated (no live data available here)
          - "rsi < 30" → check signals.rsi < 30
        Unrecognized conditions are skipped (conservative — only fire on known matches).

        Per-cycle guard: same `cycle_id` as the Haiku compress call prevents double-firing.

        Args:
            positions:      List of open Position objects (from PortfolioState.positions).
            active_theses:  Sonnet's latest active_theses list (may be None).
            indicators:     Merged indicator dict (_all_indicators), keyed by symbol.
            cycle_id:       Unique cycle ID for per-cycle guard (same as compress()).

        Returns:
            CompressorResult with needs_sonnet=True on breach, or None if no breach.
        """
        if not active_theses or not positions:
            return None

        # Build symbol → thesis_breaking_conditions lookup
        thesis_map: dict[str, list[str]] = {}
        for thesis in active_theses:
            if not isinstance(thesis, dict):
                continue
            sym = thesis.get("symbol", "")
            conditions = thesis.get("thesis_breaking_conditions", [])
            if sym and isinstance(conditions, list):
                thesis_map[sym] = [str(c) for c in conditions]

        if not thesis_map:
            return None

        open_syms = {p.symbol if hasattr(p, "symbol") else p.get("symbol", "") for p in positions}

        for sym in open_syms:
            conditions = thesis_map.get(sym)
            if not conditions:
                continue
            ind = indicators.get(sym, {})
            signals = ind.get("signals") or {}
            daily = ind.get("daily_signals") or {}

            for cond in conditions:
                if self._condition_met(cond, signals, daily):
                    log.info(
                        "Position thesis breach: %s — condition '%s' met",
                        sym, cond,
                    )
                    # Per-cycle guard: only fire once per cycle
                    if cycle_id and self._last_needs_sonnet_cycle == cycle_id:
                        log.debug(
                            "ContextCompressor: thesis breach suppressed (already fired cycle %s)",
                            cycle_id,
                        )
                        return None
                    if cycle_id:
                        self._last_needs_sonnet_cycle = cycle_id
                    return CompressorResult(
                        symbols=[],
                        notes=f"Position {sym}: condition '{cond}' met",
                        from_fallback=True,
                        needs_sonnet=True,
                        sonnet_reason="position_thesis_breach",
                    )
        return None

    def _condition_met(self, condition: str, signals: dict, daily: dict) -> bool:
        """
        Evaluate a thesis_breaking_condition string against live signal values.

        Supports simple numeric comparisons on known indicator keys.
        Returns False for unrecognized condition formats (conservative — don't fire on noise).
        """
        import re as _re
        # Known condition patterns:
        # "daily_trend becomes downtrend" → daily.daily_trend == "downtrend"
        # "rsi < 30" → signals.rsi < 30
        # "rsi > 70" → signals.rsi > 70
        # "volume_ratio < 0.5" → signals.volume_ratio < 0.5
        cond_lower = condition.lower().strip()

        # Pattern: "daily_trend becomes <trend_value>"
        # Handles all trend values (downtrend, uptrend, neutral, mixed) so both
        # long-thesis breaking conditions ("becomes downtrend") and short-thesis
        # breaking conditions ("becomes uptrend") are evaluated correctly.
        if "daily_trend" in cond_lower:
            for trend_val in ("downtrend", "uptrend", "neutral", "mixed"):
                if trend_val in cond_lower:
                    return daily.get("daily_trend") == trend_val
            return False  # daily_trend present but no recognized value

        # Pattern: "<indicator> <op> <value>"
        # Use search() instead of match() so leading whitespace (if any) is tolerated.
        m = _re.search(r"(\w+)\s*([<>]=?)\s*([\d.]+)", cond_lower)
        if m:
            key, op, val_str = m.group(1), m.group(2), m.group(3)
            # Check both intraday signals and daily signals
            live_val = signals.get(key) if signals.get(key) is not None else daily.get(key)
            if live_val is None:
                return False
            try:
                threshold = float(val_str)
                live = float(live_val)
                if op == "<":
                    return live < threshold
                if op == ">":
                    return live > threshold
                if op == "<=":
                    return live <= threshold
                if op == ">=":
                    return live >= threshold
            except (ValueError, TypeError):
                pass

        return False  # unrecognized — do not fire

    def _fill_template(self, template: str, context: dict) -> str:
        def _sub(m: re.Match) -> str:
            return str(context.get(m.group(1), m.group(0)))
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, template)

    def _parse_response(
        self,
        raw_text: str,
        all_candidates: list,
        indicators: dict,
        max_symbols_out: int,
        cycle_id: str,
    ) -> CompressorResult:
        """Parse Haiku JSON response. Falls back to deterministic sort on failure."""
        cleaned = re.sub(r"```(?:json)?\s*", "", raw_text).strip()
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

        parsed: Optional[dict] = None
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if not isinstance(parsed, dict):
            log.warning("ContextCompressor: unparseable response — using fallback sort")
            return self._fallback_sort(all_candidates, indicators, max_symbols_out)

        selected_raw = parsed.get("selected_symbols", [])
        if not isinstance(selected_raw, list):
            log.warning("ContextCompressor: selected_symbols is not a list — using fallback")
            return self._fallback_sort(all_candidates, indicators, max_symbols_out)

        # Validate: only include symbols actually in all_candidates
        known_syms = {_sym(e) for e in all_candidates}
        valid_symbols = [
            s for s in selected_raw
            if isinstance(s, str) and s in known_syms
        ][:max_symbols_out]

        if not valid_symbols:
            log.warning("ContextCompressor: no valid symbols in response — using fallback sort")
            return self._fallback_sort(all_candidates, indicators, max_symbols_out)

        # --- needs_sonnet handling ---
        # Per-cycle guard: fire at most once per Sonnet cycle.
        needs_sonnet = bool(parsed.get("needs_sonnet", False))
        sonnet_reason_raw = parsed.get("sonnet_reason")
        sonnet_reason: Optional[str] = None

        if needs_sonnet and isinstance(sonnet_reason_raw, str):
            if sonnet_reason_raw in NEEDS_SONNET_REASONS:
                if cycle_id and self._last_needs_sonnet_cycle == cycle_id:
                    # Already fired this cycle — suppress
                    log.debug(
                        "ContextCompressor: needs_sonnet suppressed (already fired cycle %s)",
                        cycle_id,
                    )
                    needs_sonnet = False
                else:
                    sonnet_reason = sonnet_reason_raw
                    if cycle_id:
                        self._last_needs_sonnet_cycle = cycle_id
            else:
                log.debug(
                    "ContextCompressor: unknown sonnet_reason %r — ignoring needs_sonnet",
                    sonnet_reason_raw,
                )
                needs_sonnet = False

        rationale = parsed.get("rationale", {})
        if not isinstance(rationale, dict):
            rationale = {}

        return CompressorResult(
            symbols=valid_symbols,
            rationale=rationale,
            notes=str(parsed.get("notes", "")),
            from_fallback=False,
            needs_sonnet=needs_sonnet,
            sonnet_reason=sonnet_reason,
        )
