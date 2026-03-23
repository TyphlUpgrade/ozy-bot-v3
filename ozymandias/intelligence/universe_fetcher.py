"""
Universe Fetcher — builds a live candidate symbol universe from two sources:

  Source A: Yahoo Finance screener (most_actives + day_gainers) — today's activity
  Source B: S&P 500 + Nasdaq 100 from Wikipedia — structural bench of liquid names

Both sources run concurrently. Source A symbols come first (precedence for today's
movers). Source B adds depth for quiet days. Results are deduped, cleaned of
non-alphabetic tickers, and filtered against the no-entry blacklist.

Source B result is cached for 24 hours — index constituents change quarterly.
All failures are swallowed and return []; this module is best-effort.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ozymandias.core.config import RankerConfig

log = logging.getLogger(__name__)

# Yahoo Finance screener endpoint (no API key required)
_SCREENER_URL = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    "?formatted=true&scrIds={scr_id}&count={count}"
)
_SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ozymandias-bot/3.0)",
    "Accept": "application/json",
}

# Wikipedia index pages for structural universe
_SP500_URL  = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

# Source B TTL: 24 hours (index changes quarterly)
_SOURCE_B_TTL_SEC = 86_400


class UniverseFetcher:
    """
    Builds the live tradeable symbol universe for the universe scanner.

    Extension point: to add a new universe source, add a coroutine that returns
    list[str] and include it in the asyncio.gather call inside get_universe().
    """

    def __init__(self, no_entry_symbols: list[str] | None = None) -> None:
        # Set of symbols to always exclude (broad-market ETFs, volatility products, etc.)
        self._blacklist: frozenset[str] = frozenset(no_entry_symbols or [])
        # Source B cache
        self._source_b_cache: list[str] = []
        self._source_b_expires: float = 0.0

    async def get_universe(self) -> list[str]:
        """
        Return a merged, deduped, cleaned list of tradeable symbol candidates.

        Source A (today's active names) comes first to preserve recency priority.
        Source B (index constituents) fills depth for quiet sessions.
        """
        results = await asyncio.gather(
            self._fetch_source_a(),
            self._fetch_source_b(),
            return_exceptions=True,
        )
        source_a = results[0] if not isinstance(results[0], Exception) else []
        source_b = results[1] if not isinstance(results[1], Exception) else []
        if isinstance(results[0], Exception):
            log.warning("Universe fetcher: Source A raised — %s", results[0])
        if isinstance(results[1], Exception):
            log.warning("Universe fetcher: Source B raised — %s", results[1])
        # Merge: Source A first, dedup preserving order
        merged: list[str] = []
        seen: set[str] = set()
        for sym in list(source_a) + list(source_b):
            if sym and sym not in seen:
                seen.add(sym)
                merged.append(sym)
        # Filter blacklist and non-alphabetic symbols (ETF classes, foreign listings)
        result = [
            s for s in merged
            if s.isalpha() and s not in self._blacklist
        ]
        log.debug(
            "Universe fetcher: %d raw → %d after filter (source_a=%d source_b=%d)",
            len(merged), len(result), len(source_a), len(source_b),
        )
        return result

    # ------------------------------------------------------------------
    # Source A — Yahoo Finance screener (most_actives + day_gainers)
    # ------------------------------------------------------------------

    async def _fetch_source_a(self) -> list[str]:
        """Fetch today's most-active and top-gaining symbols from Yahoo Finance screener."""
        try:
            actives, gainers = await asyncio.gather(
                asyncio.to_thread(self._fetch_screener, "most_actives", 50),
                asyncio.to_thread(self._fetch_screener, "day_gainers", 25),
            )
            seen: set[str] = set()
            result: list[str] = []
            for sym in actives + gainers:
                if sym not in seen:
                    seen.add(sym)
                    result.append(sym)
            log.debug("Source A: %d symbols (%d actives, %d gainers)", len(result), len(actives), len(gainers))
            return result
        except Exception as exc:
            log.warning("Universe fetcher: Source A failed — %s", exc)
            return []

    @staticmethod
    def _fetch_screener(scr_id: str, count: int) -> list[str]:
        """Synchronous screener fetch — called via asyncio.to_thread."""
        url = _SCREENER_URL.format(scr_id=scr_id, count=count)
        req = urllib.request.Request(url, headers=_SCREENER_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        quotes = (
            data.get("finance", {})
                .get("result", [{}])[0]
                .get("quotes", [])
        )
        return [q["symbol"] for q in quotes if q.get("symbol")]

    # ------------------------------------------------------------------
    # Source B — Wikipedia S&P 500 + Nasdaq 100 (cached 24h)
    # ------------------------------------------------------------------

    async def _fetch_source_b(self) -> list[str]:
        """Fetch S&P 500 and Nasdaq 100 constituents from Wikipedia (24h cache)."""
        if time.monotonic() < self._source_b_expires and self._source_b_cache:
            log.debug("Source B: cache hit (%d symbols)", len(self._source_b_cache))
            return self._source_b_cache
        try:
            result = await asyncio.to_thread(self._fetch_index_constituents)
            self._source_b_cache = result
            self._source_b_expires = time.monotonic() + _SOURCE_B_TTL_SEC
            log.debug("Source B: fetched %d index constituents", len(result))
            return result
        except Exception as exc:
            log.warning("Universe fetcher: Source B failed — %s", exc)
            return []

    @staticmethod
    def _fetch_index_constituents() -> list[str]:
        """Synchronous Wikipedia table fetch — called via asyncio.to_thread."""
        import pandas as pd
        seen: set[str] = set()
        result: list[str] = []
        try:
            sp500_tables = pd.read_html(_SP500_URL, attrs={"id": "constituents"})
            for sym in sp500_tables[0]["Symbol"].tolist():
                s = str(sym).strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    result.append(s)
        except Exception as exc:
            log.warning("Source B: S&P 500 fetch failed — %s", exc)
        try:
            ndx_tables = pd.read_html(_NDX100_URL, attrs={"id": "constituents"})
            for sym in ndx_tables[0]["Ticker"].tolist():
                s = str(sym).strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    result.append(s)
        except Exception as exc:
            log.warning("Source B: Nasdaq-100 fetch failed — %s", exc)
        return result
