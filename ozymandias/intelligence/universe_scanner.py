"""
Universe Scanner — takes the live symbol universe and produces an activity-ranked
candidate list for Claude's watchlist build.

Pipeline:
  1. Fetch bars for all universe symbols (parallel, semaphore-bounded)
  2. Run TA (generate_signal_summary) in asyncio.to_thread (CPU-bound)
  3. Filter (OR gate — either path qualifies):
       - Volume path:     volume_ratio >= min_rvol_for_candidate (elevated activity)
       - Price-move path: abs(roc_5)   >= min_price_move_pct_for_candidate (significant
                          price displacement regardless of volume — captures breakdowns,
                          quiet distributions, and extended names for fades)
  4. Sort by volume_ratio descending (direction-neutral activity signal)
  5. For top min(n*2, 60) symbols: fetch news + earnings calendar concurrently
  6. Return top n as candidate dicts

Session-level cache: the orchestrator stores the result on
_last_universe_scan / _last_universe_scan_time. Re-scan only when
cache_ttl_min expires or the cache is empty.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ozymandias.core.config import UniverseScannerConfig
    from ozymandias.data.adapters.base import DataAdapter

from ozymandias.intelligence.universe_fetcher import UniverseFetcher
from ozymandias.intelligence.technical_analysis import generate_signal_summary, compute_composite_score

log = logging.getLogger(__name__)


@dataclass
class UniverseScannerConfig:
    """Mirrors core.config.UniverseScannerConfig — kept here for import convenience in tests."""
    enabled: bool = True
    scan_concurrency: int = 20
    max_candidates: int = 50
    min_rvol_for_candidate: float = 0.8
    min_price_move_pct_for_candidate: float = 1.5
    cache_ttl_min: int = 60


class UniverseScanner:
    """
    Scans the live universe and returns RVOL-ranked trade candidates.

    Extension point: to add a new candidate enrichment field (e.g. short interest),
    add it to _enrich_one() and the candidate dict schema below.
    """

    def __init__(
        self,
        data_adapter: "DataAdapter",
        config: "UniverseScannerConfig",
        no_entry_symbols: list[str] | None = None,
    ) -> None:
        self._adapter = data_adapter
        self._cfg = config
        self._fetcher = UniverseFetcher(no_entry_symbols=no_entry_symbols)

    async def get_top_candidates(
        self,
        n: int,
        exclude: set[str] | None = None,
        blacklist: set[str] | None = None,
    ) -> list[dict]:
        """
        Return up to n candidates sorted by RVOL descending.

        Args:
            n:         Maximum number of candidates to return.
            exclude:   Symbols already on the watchlist — skipped entirely.
            blacklist: No-entry symbols (ETFs, volatility products) — skipped entirely.
        """
        exclude = exclude or set()
        blacklist = blacklist or set()

        universe = await self._fetcher.get_universe()
        # Filter already-watched and no-entry symbols
        universe = [s for s in universe if s not in exclude and s not in blacklist]
        if not universe:
            log.info("Universe scanner: empty universe after filtering")
            return []

        log.info("Universe scanner: scanning %d symbols", len(universe))

        # -- Step 1+2: fetch bars + run TA in parallel -------------------------
        semaphore = asyncio.Semaphore(self._cfg.scan_concurrency)

        async def _scan_one(sym: str) -> tuple[str, dict | None]:
            async with semaphore:
                try:
                    df = await self._adapter.fetch_bars(sym, interval="5m", period="5d")
                    if df is None or df.empty:
                        return sym, None
                    summary = await asyncio.to_thread(generate_signal_summary, sym, df)
                    return sym, summary
                except Exception as exc:
                    log.debug("Universe scanner: TA failed for %s — %s", sym, exc)
                    return sym, None

        scan_results = await asyncio.gather(*[_scan_one(s) for s in universe])

        # -- Step 3: filter — OR gate (volume path OR price-move path) ---------
        # Volume path:     RVOL >= min_rvol_for_candidate
        # Price-move path: abs(roc_5) >= min_price_move_pct_for_candidate
        # Direction-agnostic: both long breakouts and short candidates (quiet
        # distributions, extended fades) can qualify via the price-move path
        # even when current-period volume is normal.
        # To add a new qualification path: add one condition to the OR below.
        scored: list[tuple[str, dict]] = []
        volume_path_count = 0
        price_move_path_count = 0
        for sym, summary in scan_results:
            if summary is None:
                continue
            signals = summary.get("signals", {})
            bars = summary.get("bars_available", 0)
            rvol = signals.get("volume_ratio", 0.0) or 0.0
            roc5 = signals.get("roc_5", 0.0) or 0.0
            if bars < 5:
                continue
            via_rvol = rvol >= self._cfg.min_rvol_for_candidate
            via_move = abs(roc5) >= self._cfg.min_price_move_pct_for_candidate
            if not (via_rvol or via_move):
                continue
            scored.append((sym, summary))
            if via_rvol:
                volume_path_count += 1
            if via_move and not via_rvol:
                price_move_path_count += 1

        # -- Step 4: sort by RVOL descending -----------------------------------
        # High-RVOL candidates rank first; price-move-only candidates appear at
        # the bottom but are still passed to Claude for evaluation.
        scored.sort(key=lambda x: x[1].get("signals", {}).get("volume_ratio", 0.0) or 0.0, reverse=True)

        # -- Step 5: enrich top symbols with news + earnings -------------------
        enrich_count = min(n * 2, 60)
        top_for_enrich = scored[:enrich_count]

        async def _enrich_one(sym: str, summary: dict) -> dict:
            signals = summary.get("signals", {})
            rvol = signals.get("volume_ratio", 0.0) or 0.0
            price = signals.get("price") or 0.0
            composite_long = compute_composite_score(signals, direction="long")
            composite_short = compute_composite_score(signals, direction="short")
            tech_summary = _make_technical_summary(signals)

            # fetch_news is on YFinanceAdapter (not the base ABC); degrade if absent
            _fetch_news = getattr(self._adapter, "fetch_news", None)
            news_coro = _fetch_news(sym, max_items=2) if _fetch_news else _empty_coro()
            earnings_task = asyncio.to_thread(_fetch_earnings_calendar, sym)

            news_raw, earnings_days = await asyncio.gather(
                news_coro,
                earnings_task,
                return_exceptions=True,
            )

            recent_news: list[dict] = []
            if isinstance(news_raw, list):
                for item in news_raw[:2]:
                    try:
                        # YFinanceAdapter.fetch_news returns {title, publisher, age_hours} dicts
                        if isinstance(item, dict):
                            recent_news.append({
                                "title": item.get("title", ""),
                                "publisher": item.get("publisher", ""),
                                "age_hours": item.get("age_hours"),
                            })
                        else:
                            recent_news.append({
                                "title": getattr(item, "headline", str(item)),
                                "publisher": getattr(item, "source", ""),
                                "age_hours": _age_hours(getattr(item, "published_at", None)),
                            })
                    except Exception:
                        pass

            earnings = None if isinstance(earnings_days, Exception) else earnings_days

            return {
                "symbol": sym,
                "rvol": round(rvol, 2),
                "technical_summary": tech_summary,
                "composite_score_long": round(composite_long, 3),
                "composite_score_short": round(composite_short, 3),
                "price": round(price, 2),
                "recent_news": recent_news,
                "earnings_within_days": earnings,
            }

        enriched = await asyncio.gather(*[_enrich_one(s, summ) for s, summ in top_for_enrich])

        # Return top n
        candidates = list(enriched)[:n]
        log.info(
            "Universe scanner: %d candidates (rvol_path=%d  move_path=%d)  top RVOL: %s",
            len(candidates),
            volume_path_count,
            price_move_path_count,
            [c["symbol"] for c in candidates[:5]],
        )
        return candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _empty_coro():
    """No-op coroutine used when fetch_news is unavailable on the adapter."""
    return []


def _make_technical_summary(signals: dict) -> str:
    """Compact one-line TA summary for Claude's candidate context."""
    import math
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
    return ", ".join(parts) if parts else "no indicator data"


def _fetch_earnings_calendar(symbol: str) -> int | None:
    """
    Synchronous earnings calendar fetch via yfinance — called via asyncio.to_thread.
    Returns days until next earnings if within 10 days, else None.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if not isinstance(cal, dict):
            return None
        dates = cal.get("Earnings Date")
        if not dates:
            return None
        # May be a list or a single value
        if not isinstance(dates, (list, tuple)):
            dates = [dates]
        today = date.today()
        for dt in dates:
            try:
                earnings_date = dt.date() if hasattr(dt, "date") else dt
                days = (earnings_date - today).days
                if 0 <= days <= 10:
                    return days
            except Exception:
                continue
        return None
    except Exception:
        return None


def _age_hours(published_at) -> float | None:
    """Return hours since publication, or None if unknown."""
    if published_at is None:
        return None
    try:
        from datetime import datetime, timezone
        if hasattr(published_at, "tzinfo") and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - published_at
        return round(delta.total_seconds() / 3600, 1)
    except Exception:
        return None
