"""
yfinance market data adapter.

yfinance is a synchronous library, so all calls are wrapped in
asyncio.to_thread() to keep them non-blocking.

Response caching with configurable TTL (simple dict + timestamp expiry):
  - Quotes:        30 seconds (default)
  - Bars:           5 minutes (default)
  - Fundamentals:  60 minutes (default)
  - News:          15 minutes (default)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
import yfinance as yf

from ozymandias.data.adapters.base import DataAdapter, Fundamentals, Quote

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal cache entry
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    data: Any
    expires_at: float    # time.monotonic() value


# ---------------------------------------------------------------------------
# Adapter implementation
# ---------------------------------------------------------------------------

class YFinanceAdapter(DataAdapter):
    """
    Market data adapter backed by yfinance.

    Args:
        quote_ttl:        Cache TTL for quotes in seconds (default: 30)
        bars_ttl:         Cache TTL for bars in seconds (default: 300)
        fundamentals_ttl: Cache TTL for fundamentals in seconds (default: 3600)
    """

    def __init__(
        self,
        quote_ttl: int = 30,
        bars_ttl: int = 300,
        fundamentals_ttl: int = 3600,
        news_ttl: int = 900,
    ) -> None:
        self._quote_ttl = quote_ttl
        self._bars_ttl = bars_ttl
        self._fundamentals_ttl = fundamentals_ttl
        self._news_ttl = news_ttl  # 15 min default — news freshness degrades slowly
        self._cache: dict[str, _CacheEntry] = {}

    # ------------------------------------------------------------------
    # DataAdapter interface
    # ------------------------------------------------------------------

    async def fetch_bars(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        """Fetch OHLCV bars with lowercase column names."""
        key = f"bars:{symbol}:{interval}:{period}"
        cached = self._get_cache(key)
        if cached is not None:
            log.debug("Cache hit: bars %s %s %s", symbol, interval, period)
            return cached

        log.debug("Fetching bars: %s interval=%s period=%s", symbol, interval, period)
        try:
            df = await asyncio.to_thread(self._download_bars, symbol, interval, period)
        except Exception as exc:
            log.warning("fetch_bars failed for %s: %s", symbol, exc)
            raise

        self._set_cache(key, df, self._bars_ttl)
        return df

    async def fetch_quote(self, symbol: str) -> Quote:
        """Fetch the latest quote for a symbol."""
        key = f"quote:{symbol}"
        cached = self._get_cache(key)
        if cached is not None:
            log.debug("Cache hit: quote %s", symbol)
            return cached

        log.debug("Fetching quote: %s", symbol)
        try:
            quote = await asyncio.to_thread(self._download_quote, symbol)
        except Exception as exc:
            log.warning("fetch_quote failed for %s: %s", symbol, exc)
            raise

        self._set_cache(key, quote, self._quote_ttl)
        return quote

    async def fetch_fundamentals(self, symbol: str) -> Fundamentals:
        """Fetch fundamental data for a symbol."""
        key = f"fundamentals:{symbol}"
        cached = self._get_cache(key)
        if cached is not None:
            log.debug("Cache hit: fundamentals %s", symbol)
            return cached

        log.debug("Fetching fundamentals: %s", symbol)
        try:
            fundamentals = await asyncio.to_thread(self._download_fundamentals, symbol)
        except Exception as exc:
            log.warning("fetch_fundamentals failed for %s: %s", symbol, exc)
            raise

        self._set_cache(key, fundamentals, self._fundamentals_ttl)
        return fundamentals

    async def fetch_news(
        self,
        symbol: str,
        max_items: int = 5,
        max_age_hours: float = 168.0,
    ) -> list[dict]:
        """
        Fetch recent news headlines for a symbol (best-effort).

        Returns a list of {title, publisher, age_hours} dicts for items
        published within max_age_hours, capped at max_items.
        Returns [] on any exception.

        Raw items (with age_hours attached) are cached before age filtering so
        callers with different max_age_hours windows share the same cache entry.
        """
        key = f"news:{symbol}"
        cached = self._get_cache(key)
        if cached is None:
            try:
                raw_items = await asyncio.to_thread(self._download_news, symbol)
            except Exception as exc:
                log.debug("fetch_news failed for %s: %s", symbol, exc)
                return []

            now_ts = time.time()
            annotated = []
            for item in raw_items:
                pub_ts = item.get("providerPublishTime", 0)
                age_hours = (now_ts - pub_ts) / 3600.0
                annotated.append({
                    "title":     item.get("title", ""),
                    "publisher": item.get("publisher", ""),
                    "age_hours": round(age_hours, 1),
                })
            self._set_cache(key, annotated, self._news_ttl)
            cached = annotated
        else:
            log.debug("Cache hit: news %s", symbol)

        return [item for item in cached if item["age_hours"] <= max_age_hours][:max_items]

    async def is_available(self) -> bool:
        """Lightweight health check — tries to fetch SPY fast_info."""
        try:
            await asyncio.to_thread(self._health_check)
            return True
        except Exception as exc:
            log.warning("YFinanceAdapter health check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Synchronous yfinance wrappers (run via asyncio.to_thread)
    # ------------------------------------------------------------------

    @staticmethod
    def _download_bars(symbol: str, interval: str, period: str) -> pd.DataFrame:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)

        if df.empty:
            raise ValueError(f"No bar data returned for {symbol}")

        # Normalize column names to lowercase
        df.columns = [c.lower() for c in df.columns]

        # Keep only OHLCV columns (drop Dividends, Stock Splits, etc.)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                raise ValueError(f"Missing column '{col}' in yfinance response for {symbol}")
        df = df[['open', 'high', 'low', 'close', 'volume']]

        # Ensure UTC DatetimeIndex
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')

        return df

    @staticmethod
    def _download_quote(symbol: str) -> Quote:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info

        # fast_info attribute names vary slightly across yfinance versions
        last = float(
            getattr(info, 'last_price', None)
            or getattr(info, 'regularMarketPrice', None)
            or 0.0
        )
        bid = float(getattr(info, 'bid', None) or last)
        ask = float(getattr(info, 'ask', None) or last)
        volume = int(
            getattr(info, 'last_volume', None)
            or getattr(info, 'regularMarketVolume', None)
            or 0
        )

        return Quote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last,
            volume=volume,
            timestamp=datetime.now(timezone.utc),
        )

    @staticmethod
    def _download_fundamentals(symbol: str) -> Fundamentals:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        def _get(key: str) -> Optional[Any]:
            val = info.get(key)
            return val if val not in (None, "N/A", "") else None

        return Fundamentals(
            market_cap=_get('marketCap'),
            pe_ratio=_get('trailingPE'),
            sector=_get('sector'),
            industry=_get('industry'),
            avg_volume=_get('averageVolume'),
            dividend_yield=_get('dividendYield'),
            beta=_get('beta'),
            fifty_two_week_high=_get('fiftyTwoWeekHigh'),
            fifty_two_week_low=_get('fiftyTwoWeekLow'),
            forward_pe=_get('forwardPE'),
            price_to_book=_get('priceToBook'),
        )

    @staticmethod
    def _download_news(symbol: str) -> list[dict]:
        """Return raw news items from yfinance, normalised to a flat dict.

        yfinance v0.2.x returned flat dicts with top-level ``providerPublishTime``
        (unix int), ``title``, and ``publisher`` fields.  Newer versions nest
        everything under a ``content`` sub-dict and use an ISO-8601 ``pubDate``
        string instead.  We normalise both formats here so the caller sees a
        consistent schema with ``providerPublishTime`` (unix int), ``title``, and
        ``publisher`` regardless of the installed yfinance version.
        """
        raw = yf.Ticker(symbol).news or []
        normalised = []
        for item in raw:
            content = item.get("content")
            if content:
                # New nested schema (yfinance ≥ 0.2.54 / 2026 API change)
                title     = content.get("title", "")
                publisher = (content.get("provider") or {}).get("displayName", "")
                pub_date  = content.get("pubDate", "")
                try:
                    from datetime import datetime, timezone as _tz
                    pub_ts = int(
                        datetime.fromisoformat(
                            pub_date.replace("Z", "+00:00")
                        ).timestamp()
                    )
                except Exception:
                    pub_ts = 0
            else:
                # Legacy flat schema
                title     = item.get("title", "")
                publisher = item.get("publisher", "")
                pub_ts    = item.get("providerPublishTime", 0)
            normalised.append({
                "title":               title,
                "publisher":           publisher,
                "providerPublishTime": pub_ts,
            })
        return normalised

    @staticmethod
    def _health_check() -> None:
        """Minimal yfinance call to verify the library can reach Yahoo Finance."""
        info = yf.Ticker("SPY").fast_info
        _ = info.last_price  # raises AttributeError / network error if unavailable

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _get_cache(self, key: str) -> Any:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._cache[key]
            return None
        return entry.data

    def _set_cache(self, key: str, data: Any, ttl: int) -> None:
        self._cache[key] = _CacheEntry(
            data=data,
            expires_at=time.monotonic() + ttl,
        )
