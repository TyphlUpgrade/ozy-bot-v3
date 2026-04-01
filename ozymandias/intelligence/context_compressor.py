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
from ozymandias.intelligence.technical_analysis import compute_directional_scores

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
            if raw:
                _scores = compute_directional_scores(raw)
                score = _scores["long"] if ed == "long" else _scores["short"] if ed == "short" else max(_scores["long"], _scores["short"])
            else:
                _pre = ind.get("long_score" if ed == "long" else "short_score", None)
                score = float(_pre) if _pre is not None else max(float(ind.get("long_score", 0.0)), float(ind.get("short_score", 0.0)))

            item: dict = {
                "symbol": sym,
                "expected_direction": ed,
                "tier": tier,
                "directional_score": round(score, 3),
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
            ed = _attr(entry, "expected_direction", "either") or "either"
            if raw:
                _scores = compute_directional_scores(raw)
                if ed == "long":  return _scores["long"]
                if ed == "short": return _scores["short"]
                return max(_scores["long"], _scores["short"])
            if ed == "long":  return float(ind.get("long_score",  0.0))
            if ed == "short": return float(ind.get("short_score", 0.0))
            return max(float(ind.get("long_score", 0.0)), float(ind.get("short_score", 0.0)))

        sorted_entries = sorted(all_candidates, key=_score, reverse=True)[:max_symbols_out]
        return CompressorResult(
            symbols=[_sym(e) for e in sorted_entries],
            from_fallback=True,
        )

    # ------------------------------------------------------------------
    # Position thesis monitoring (Haiku-based)
    # ------------------------------------------------------------------

    async def check_position_theses(
        self,
        positions: list,
        active_theses: Optional[list[dict]],
        indicators: dict,
        daily_indicators: Optional[dict],
        market_data: dict,
        regime_assessment: Optional[dict],
        sector_regimes: Optional[dict],
        cycle_id: str = "",
    ) -> Optional[CompressorResult]:
        """
        Evaluate open position thesis_breaking_conditions using Haiku.

        Called from the medium loop once per cycle. Haiku receives live signals,
        recent news, and regime context so it can evaluate both numeric conditions
        ("composite_score falls below 0.45") and narrative/event conditions
        ("Iran ceasefire confirmed", "deal terminated").

        Returns a CompressorResult with needs_sonnet=True if any condition is
        clearly met, so the orchestrator can fire a targeted Sonnet review cycle.

        Per-cycle guard: cycle_id prevents re-triggering within the same Sonnet cycle.
        On any Haiku failure the method returns None (conservative — don't fire on error).
        """
        if not active_theses or not positions:
            return None

        prompt_template = self._load_thesis_check_prompt()
        if not prompt_template:
            log.debug("ContextCompressor: no thesis_check.txt — skipping thesis monitoring")
            return None

        positions_payload, regime_payload, market_payload = self._build_thesis_check_payload(
            positions, active_theses, indicators, daily_indicators, market_data,
            regime_assessment, sector_regimes,
        )

        if not positions_payload or positions_payload == "[]":
            return None  # no positions matched any active thesis

        context = {
            "positions_json": positions_payload,
            "regime_json": regime_payload,
            "market_context_json": market_payload,
        }
        prompt = self._fill_template(prompt_template, context)

        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._cfg.compressor_model,
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=30.0,
            )
            raw_text = response.content[0].text if response.content else ""
        except Exception as exc:
            log.warning("ContextCompressor: thesis check Haiku call failed (%s) — skipping", exc)
            return None

        # Parse response — conservative: return None on any failure
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
            log.warning(
                "ContextCompressor: thesis check unparseable response — skipping | raw=%s",
                raw_text[:200],
            )
            return None

        if not bool(parsed.get("needs_sonnet", False)):
            return None

        breach_detail = parsed.get("breach")
        if not isinstance(breach_detail, str):
            breach_detail = "position_thesis_breach (no detail)"

        # Per-cycle guard: only fire once per Sonnet cycle
        if cycle_id and self._last_needs_sonnet_cycle == cycle_id:
            log.debug(
                "ContextCompressor: thesis breach suppressed (already fired cycle %s)", cycle_id
            )
            return None
        if cycle_id:
            self._last_needs_sonnet_cycle = cycle_id

        log.info("Position thesis breach detected — %s", breach_detail)
        return CompressorResult(
            symbols=[],
            notes=breach_detail,
            needs_sonnet=True,
            sonnet_reason="position_thesis_breach",
        )

    def _load_thesis_check_prompt(self) -> Optional[str]:
        if self._prompts_dir is None:
            return None
        path = self._prompts_dir / "thesis_check.txt"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _build_thesis_check_payload(
        self,
        positions: list,
        active_theses: list[dict],
        indicators: dict,
        daily_indicators: Optional[dict],
        market_data: dict,
        regime_assessment: Optional[dict],
        sector_regimes: Optional[dict],
    ) -> tuple[str, str, str]:
        """
        Build three JSON strings for the thesis_check.txt prompt:
        positions_json, regime_json, market_context_json.

        positions_json: enriched list of open positions that have an active thesis.
        Each entry includes thesis, conditions, live signals, and recent news.
        """
        # Build symbol → thesis lookup from active_theses
        thesis_map: dict[str, dict] = {}
        for thesis in active_theses:
            if not isinstance(thesis, dict):
                continue
            sym = thesis.get("symbol", "")
            if sym:
                thesis_map[sym] = thesis

        daily = daily_indicators or {}
        watchlist_news: dict = market_data.get("watchlist_news") or {}

        entries: list[dict] = []
        for pos in positions:
            sym = pos.symbol if hasattr(pos, "symbol") else pos.get("symbol", "")
            if not sym or sym not in thesis_map:
                continue

            thesis = thesis_map[sym]
            ind = indicators.get(sym, {})
            raw_signals = ind.get("signals") or {}
            daily_sig = daily.get(sym, {})

            # Live signals — include only non-None values to stay compact
            live: dict = {}
            _signal_keys = ("rsi", "volume_ratio", "vwap_position", "trend_structure", "roc_5")
            for k in _signal_keys:
                v = raw_signals.get(k)
                if v is not None:
                    live[k] = v
            # directional scores for thesis monitoring context
            if raw_signals:
                _ds = compute_directional_scores(raw_signals, daily_sig)
                live["long_score"]  = round(_ds["long"],  3)
                live["short_score"] = round(_ds["short"], 3)
            # price
            price = raw_signals.get("price") or ind.get("price")
            if price is not None:
                live["price"] = price
            # daily_trend from daily_indicators
            dt = daily_sig.get("daily_trend")
            if dt is not None:
                live["daily_trend"] = dt

            # News — up to 3 recent headlines for this symbol
            news_items = watchlist_news.get(sym, [])
            recent_news = [
                {"title": n.get("title", ""), "publisher": n.get("publisher", ""),
                 "age_hours": n.get("age_hours")}
                for n in news_items[:3]
                if isinstance(n, dict)
            ]

            entry: dict = {
                "symbol": sym,
                "thesis": (thesis.get("thesis") or "")[:150],
                "thesis_breaking_conditions": thesis.get("thesis_breaking_conditions") or [],
                "live_signals": live,
            }
            if recent_news:
                entry["recent_news"] = recent_news
            entries.append(entry)

        positions_json = json.dumps(entries, default=str)

        regime_json = json.dumps(
            {
                "regime": regime_assessment.get("regime") if regime_assessment else None,
                "confidence": regime_assessment.get("confidence") if regime_assessment else None,
                "sector_regimes": sector_regimes,
            },
            default=str,
        )

        # Macro news: up to 2 SPY headlines + 1 QQQ headline
        macro_raw: dict = market_data.get("macro_news") or {}
        macro_news = (
            [{"title": n.get("title", ""), "publisher": n.get("publisher", ""),
              "age_hours": n.get("age_hours")} for n in macro_raw.get("SPY", [])[:2]
             if isinstance(n, dict)]
            + [{"title": n.get("title", ""), "publisher": n.get("publisher", ""),
                "age_hours": n.get("age_hours")} for n in macro_raw.get("QQQ", [])[:1]
               if isinstance(n, dict)]
        )

        market_context_json = json.dumps(
            {
                "spy_trend": market_data.get("spy_trend"),
                "spy_rsi": market_data.get("spy_rsi"),
                "qqq_trend": market_data.get("qqq_trend"),
                "spy_daily": market_data.get("spy_daily"),
                "macro_news": macro_news,
            },
            default=str,
        )

        return positions_json, regime_json, market_context_json

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
            log.warning(
                "ContextCompressor: unparseable response — using fallback sort | raw=%s",
                raw_text[:500],
            )
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
