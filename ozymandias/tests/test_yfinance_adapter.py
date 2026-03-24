"""
Tests for data/adapters/yfinance_adapter.py.

yfinance calls are fully mocked — this test suite never hits the real API.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from ozymandias.data.adapters.base import Fundamentals, Quote
from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bar_df(uppercase: bool = True) -> pd.DataFrame:
    """Return a minimal OHLCV DataFrame as yfinance would."""
    if uppercase:
        cols = {'Open': [100.0], 'High': [101.0], 'Low': [99.0],
                'Close': [100.5], 'Volume': [1_000_000],
                'Dividends': [0.0], 'Stock Splits': [0.0]}
    else:
        cols = {'open': [100.0], 'high': [101.0], 'low': [99.0],
                'close': [100.5], 'volume': [1_000_000]}
    return pd.DataFrame(cols, index=pd.DatetimeIndex(['2025-01-02'], tz='UTC'))


def _make_fast_info(last_price: float = 150.0) -> MagicMock:
    fi = MagicMock()
    fi.last_price = last_price
    fi.bid = last_price - 0.05
    fi.ask = last_price + 0.05
    fi.last_volume = 500_000
    return fi


def _make_ticker_info() -> dict:
    return {
        'marketCap': 3_000_000_000_000,
        'trailingPE': 28.5,
        'sector': 'Technology',
        'industry': 'Consumer Electronics',
        'averageVolume': 60_000_000,
        'dividendYield': 0.005,
        'beta': 1.2,
        'fiftyTwoWeekHigh': 200.0,
        'fiftyTwoWeekLow': 120.0,
        'forwardPE': 25.0,
        'priceToBook': 40.0,
    }


# ---------------------------------------------------------------------------
# Suppress the per-fetch stagger sleep so tests don't accumulate 0–0.5s delays
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_fetch_stagger(monkeypatch):
    """Zero out the random stagger sleep in the adapter so tests run fast."""
    monkeypatch.setattr(
        "ozymandias.data.adapters.yfinance_adapter.random.uniform",
        lambda a, b: 0,
    )


# ---------------------------------------------------------------------------
# fetch_bars
# ---------------------------------------------------------------------------

class TestFetchBars:
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_columns_normalized_to_lowercase(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = _make_bar_df(uppercase=True)

        adapter = YFinanceAdapter()
        df = await adapter.fetch_bars('AAPL', '1d', '5d')

        assert 'close' in df.columns
        assert 'open' in df.columns
        assert 'high' in df.columns
        assert 'low' in df.columns
        assert 'volume' in df.columns

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_extra_columns_dropped(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = _make_bar_df(uppercase=True)

        adapter = YFinanceAdapter()
        df = await adapter.fetch_bars('AAPL', '1d', '5d')

        assert 'dividends' not in df.columns
        assert 'stock splits' not in df.columns
        assert set(df.columns) == {'open', 'high', 'low', 'close', 'volume'}

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_raises_on_empty_dataframe(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = pd.DataFrame()

        adapter = YFinanceAdapter()
        with pytest.raises(ValueError):
            await adapter.fetch_bars('FAKE', '1d', '5d')

    @patch('ozymandias.data.adapters.yfinance_adapter.asyncio.to_thread',
           wraps=asyncio.to_thread)
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_uses_to_thread(self, mock_ticker_cls, mock_to_thread):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = _make_bar_df()

        adapter = YFinanceAdapter()
        await adapter.fetch_bars('AAPL', '1d', '5d')

        mock_to_thread.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_quote
# ---------------------------------------------------------------------------

class TestFetchQuote:
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_returns_quote_dataclass(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.fast_info = _make_fast_info(150.0)

        adapter = YFinanceAdapter()
        quote = await adapter.fetch_quote('AAPL')

        assert isinstance(quote, Quote)
        assert quote.symbol == 'AAPL'
        assert quote.last == pytest.approx(150.0)
        assert quote.bid < quote.ask

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_volume_is_integer(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.fast_info = _make_fast_info()

        adapter = YFinanceAdapter()
        quote = await adapter.fetch_quote('AAPL')
        assert isinstance(quote.volume, int)

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_timestamp_is_utc(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.fast_info = _make_fast_info()

        adapter = YFinanceAdapter()
        quote = await adapter.fetch_quote('AAPL')
        assert quote.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# fetch_fundamentals
# ---------------------------------------------------------------------------

class TestFetchFundamentals:
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_returns_fundamentals_dataclass(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.info = _make_ticker_info()

        adapter = YFinanceAdapter()
        fund = await adapter.fetch_fundamentals('AAPL')

        assert isinstance(fund, Fundamentals)
        assert fund.sector == 'Technology'
        assert fund.market_cap == 3_000_000_000_000
        assert fund.pe_ratio == pytest.approx(28.5)

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_missing_fields_are_none(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.info = {}  # empty — no fields

        adapter = YFinanceAdapter()
        fund = await adapter.fetch_fundamentals('AAPL')

        assert fund.market_cap is None
        assert fund.pe_ratio is None
        assert fund.sector is None

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_na_string_treated_as_none(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.info = {'sector': 'N/A', 'trailingPE': 'N/A'}

        adapter = YFinanceAdapter()
        fund = await adapter.fetch_fundamentals('AAPL')

        assert fund.sector is None
        assert fund.pe_ratio is None


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

class TestIsAvailable:
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_returns_true_when_healthy(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.fast_info.last_price = 450.0

        adapter = YFinanceAdapter()
        assert await adapter.is_available() is True

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_returns_false_on_exception(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        type(mock_ticker.fast_info).last_price = property(
            lambda self: (_ for _ in ()).throw(ConnectionError("timeout"))
        )

        adapter = YFinanceAdapter()
        assert await adapter.is_available() is False

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker',
           side_effect=Exception("no network"))
    async def test_returns_false_on_ticker_exception(self, mock_ticker_cls):
        adapter = YFinanceAdapter()
        assert await adapter.is_available() is False


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestCaching:
    @patch('ozymandias.data.adapters.yfinance_adapter.time.monotonic')
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_second_call_within_ttl_uses_cache(self, mock_ticker_cls, mock_mono):
        mock_mono.return_value = 0.0
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.fast_info = _make_fast_info()

        adapter = YFinanceAdapter(quote_ttl=60)
        await adapter.fetch_quote('AAPL')
        await adapter.fetch_quote('AAPL')

        # yf.Ticker should only have been created once (second call hit cache)
        assert mock_ticker_cls.call_count == 1

    @patch('ozymandias.data.adapters.yfinance_adapter.time.monotonic')
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_call_after_ttl_expiry_re_fetches(self, mock_ticker_cls, mock_mono):
        mock_mono.return_value = 0.0
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.fast_info = _make_fast_info()

        adapter = YFinanceAdapter(quote_ttl=30)

        # First call at t=0
        await adapter.fetch_quote('AAPL')
        assert mock_ticker_cls.call_count == 1

        # Advance time past TTL + max jitter (ttl * 1.15 + margin)
        mock_mono.return_value = 36.0

        # Second call after TTL should hit the source again
        await adapter.fetch_quote('AAPL')
        assert mock_ticker_cls.call_count == 2

    @patch('ozymandias.data.adapters.yfinance_adapter.time.monotonic')
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_different_symbols_cached_separately(self, mock_ticker_cls, mock_mono):
        mock_mono.return_value = 0.0
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.fast_info = _make_fast_info()

        adapter = YFinanceAdapter(quote_ttl=60)
        await adapter.fetch_quote('AAPL')
        await adapter.fetch_quote('TSLA')

        # Two distinct tickers created
        assert mock_ticker_cls.call_count == 2

    @patch('ozymandias.data.adapters.yfinance_adapter.time.monotonic')
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_bars_cache_key_includes_interval_and_period(
        self, mock_ticker_cls, mock_mono
    ):
        mock_mono.return_value = 0.0
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.return_value = _make_bar_df()

        adapter = YFinanceAdapter(bars_ttl=300)
        await adapter.fetch_bars('AAPL', '1d', '5d')
        await adapter.fetch_bars('AAPL', '5m', '1d')   # different key — cache miss

        # Two distinct fetch calls
        assert mock_ticker_cls.call_count == 2


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------

class TestErrorPropagation:
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_fetch_bars_propagates_exception(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.history.side_effect = RuntimeError("network error")

        adapter = YFinanceAdapter()
        with pytest.raises(RuntimeError):
            await adapter.fetch_bars('AAPL', '1d', '5d')

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_fetch_quote_propagates_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = RuntimeError("no internet")

        adapter = YFinanceAdapter()
        with pytest.raises(RuntimeError):
            await adapter.fetch_quote('AAPL')


# ---------------------------------------------------------------------------
# fetch_news
# ---------------------------------------------------------------------------

class TestFetchNews:
    def _make_news_items(self, ages_hours: list[float]) -> list[dict]:
        """Build fake yfinance news items with publish times relative to now."""
        now = time.time()
        return [
            {
                "title":               f"Headline {i}",
                "publisher":           "TestWire",
                "providerPublishTime": int(now - age * 3600),
            }
            for i, age in enumerate(ages_hours)
        ]

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_returns_headlines_within_24h(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.news = self._make_news_items([1.0, 5.0, 10.0])

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('AAPL', max_items=5)

        assert len(result) == 3
        assert all(item["age_hours"] <= 24.0 for item in result)
        assert result[0]["title"] == "Headline 0"
        assert result[0]["publisher"] == "TestWire"

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_filters_items_older_than_max_age_hours(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        # Mix: 2 recent (1h, 2h), 2 older (25h, 48h)
        mock_ticker.news = self._make_news_items([1.0, 25.0, 2.0, 48.0])

        adapter = YFinanceAdapter()
        # Explicit 24h window — only the two recent items pass
        result = await adapter.fetch_news('AAPL', max_items=5, max_age_hours=24.0)

        assert len(result) == 2
        assert all(item["age_hours"] <= 24.0 for item in result)

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_extended_age_window_returns_older_news(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        # 4 items: 1h, 25h, 2h, 48h — all within 168h
        mock_ticker.news = self._make_news_items([1.0, 25.0, 2.0, 48.0])

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('AAPL', max_items=5, max_age_hours=168.0)

        assert len(result) == 4
        assert all(item["age_hours"] <= 168.0 for item in result)

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_respects_max_items_cap(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.news = self._make_news_items([1.0, 2.0, 3.0, 4.0, 5.0])

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('AAPL', max_items=3)

        assert len(result) == 3

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_returns_empty_list_on_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = RuntimeError("network failure")

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('AAPL')

        assert result == []

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_returns_empty_list_when_no_news(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.news = None

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('AAPL')

        assert result == []

    @patch('ozymandias.data.adapters.yfinance_adapter.time.monotonic')
    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_second_call_within_ttl_uses_cache(self, mock_ticker_cls, mock_mono):
        mock_mono.return_value = 0.0
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.news = self._make_news_items([1.0])

        adapter = YFinanceAdapter(news_ttl=900)
        await adapter.fetch_news('AAPL')
        await adapter.fetch_news('AAPL')

        assert mock_ticker_cls.call_count == 1

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_result_contains_no_links(self, mock_ticker_cls):
        """News items sent to Claude must not include URLs (token budget)."""
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        mock_ticker.news = [
            {
                "title": "Test Headline",
                "publisher": "Reuters",
                "link": "https://example.com/article",
                "providerPublishTime": int(time.time() - 3600),
            }
        ]

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('AAPL')

        assert len(result) == 1
        assert "link" not in result[0]
        assert set(result[0].keys()) == {"title", "publisher", "age_hours"}

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_new_nested_schema_parsed_correctly(self, mock_ticker_cls):
        """yfinance ≥ 0.2.54 returns nested content dicts with ISO-8601 pubDate."""
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        # New-style nested schema (pubDate 2h ago)
        pub_iso = datetime.fromtimestamp(
            time.time() - 2 * 3600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        mock_ticker.news = [
            {
                "id": "abc-123",
                "content": {
                    "title": "NVDA GTC Keynote",
                    "pubDate": pub_iso,
                    "provider": {"displayName": "Yahoo Finance"},
                },
            }
        ]

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('NVDA', max_items=5, max_age_hours=168.0)

        assert len(result) == 1
        assert result[0]["title"] == "NVDA GTC Keynote"
        assert result[0]["publisher"] == "Yahoo Finance"
        assert result[0]["age_hours"] == pytest.approx(2.0, abs=0.1)

    @patch('ozymandias.data.adapters.yfinance_adapter.yf.Ticker')
    async def test_new_schema_filtered_by_age(self, mock_ticker_cls):
        """New nested schema items beyond the age window are still rejected."""
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker
        old_iso = datetime.fromtimestamp(
            time.time() - 200 * 3600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        mock_ticker.news = [
            {
                "id": "old-1",
                "content": {
                    "title": "Stale headline",
                    "pubDate": old_iso,
                    "provider": {"displayName": "Reuters"},
                },
            }
        ]

        adapter = YFinanceAdapter()
        result = await adapter.fetch_news('NVDA', max_items=5, max_age_hours=168.0)

        assert result == []
