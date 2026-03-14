"""
Abstract base classes for market data and sentiment adapters.

All adapters must implement these interfaces to ensure consistent behaviour
regardless of the underlying data source.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Shared data types
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    """Latest quote snapshot for a symbol."""
    symbol: str
    bid: float
    ask: float
    last: float
    volume: int
    timestamp: datetime


@dataclass
class Fundamentals:
    """Fundamental company data. All fields are optional — not every source
    returns every field."""
    market_cap: Optional[float]
    pe_ratio: Optional[float]
    sector: Optional[str]
    industry: Optional[str]
    avg_volume: Optional[float]
    dividend_yield: Optional[float] = None
    beta: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None


@dataclass
class SentimentSignal:
    """A single sentiment signal from a sentiment data source."""
    symbol: str
    source: str
    score: float        # -1.0 (very bearish) to +1.0 (very bullish)
    timestamp: datetime


@dataclass
class NewsItem:
    """A news article related to a symbol."""
    headline: str
    source: str
    symbol: str
    published_at: datetime
    url: str
    sentiment_hint: Optional[float] = None   # pre-scored: -1.0 to 1.0


@dataclass
class CalendarEvent:
    """An economic or earnings calendar event."""
    event_type: str                 # e.g. "earnings", "fed_meeting", "cpi_release"
    date: date
    symbol_or_description: str


# ---------------------------------------------------------------------------
# Abstract interfaces
# ---------------------------------------------------------------------------

class DataAdapter(ABC):
    """
    Abstract interface for market data sources.

    Implementations must wrap synchronous library calls in asyncio.to_thread()
    as needed. All methods are async.
    """

    @abstractmethod
    async def fetch_bars(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        """
        Return OHLCV DataFrame with lowercase column names.

        Args:
            symbol:   Ticker symbol, e.g. "AAPL"
            interval: Bar interval — "1m", "5m", "1h", "1d"
            period:   Lookback period — "1d", "5d", "1mo"

        Returns:
            DataFrame with columns: open, high, low, close, volume.
            Index is a UTC DatetimeIndex.
        """
        ...

    @abstractmethod
    async def fetch_quote(self, symbol: str) -> Quote:
        """Return the latest quote for a symbol."""
        ...

    @abstractmethod
    async def fetch_fundamentals(self, symbol: str) -> Fundamentals:
        """Return fundamental data for a symbol."""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """
        Lightweight health check. Return False if the source is rate-limited
        or unreachable. Must not raise — catch all exceptions internally.
        """
        ...


class SentimentAdapter(ABC):
    """Abstract interface for news and sentiment data sources."""

    @abstractmethod
    async def poll(self, symbols: list[str]) -> list[SentimentSignal]:
        """Return the latest sentiment signals for the given symbols."""
        ...

    @abstractmethod
    async def get_news(self, symbol: str, since: datetime) -> list[NewsItem]:
        """Return news items for a symbol published after ``since``."""
        ...

    @abstractmethod
    async def get_calendar_events(
        self, date_range: tuple[date, date]
    ) -> list[CalendarEvent]:
        """Return calendar events within the given date range (inclusive)."""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Health check. Return False on any connectivity issue."""
        ...
