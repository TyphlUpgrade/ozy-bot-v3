"""
tests/test_universe_scanner.py
================================
Unit tests for UniverseScanner.get_top_candidates().

All network and TA calls are mocked.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from ozymandias.intelligence.universe_scanner import UniverseScanner, _fetch_earnings_calendar
from ozymandias.intelligence.universe_fetcher import UniverseFetcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    scan_concurrency: int = 5,
    max_candidates: int = 10,
    min_rvol: float = 0.8,
    cache_ttl_min: int = 60,
):
    from ozymandias.intelligence.universe_scanner import UniverseScannerConfig
    return UniverseScannerConfig(
        enabled=True,
        scan_concurrency=scan_concurrency,
        max_candidates=max_candidates,
        min_rvol_for_candidate=min_rvol,
        cache_ttl_min=cache_ttl_min,
    )


def _make_summary(symbol: str, rvol: float = 1.5, bars: int = 100, price: float = 100.0) -> dict:
    """Minimal generate_signal_summary output."""
    return {
        "signals": {
            "price": price,
            "volume_ratio": rvol,
            "rsi": 60.0,
            "vwap_position": "above",
            "macd_signal": "bullish",
            "trend_structure": "bullish_aligned",
            "roc_5": 1.2,
        },
        "composite_technical_score": 0.65,
        "bars_available": bars,
    }


def _make_scanner(universe: list[str], summaries: dict[str, dict]) -> UniverseScanner:
    """Build a scanner whose universe + TA results are mocked."""
    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))

    cfg = _make_config()
    scanner = UniverseScanner(adapter, cfg)

    # Mock the fetcher and TA
    scanner._fetcher = MagicMock()
    scanner._fetcher.get_universe = AsyncMock(return_value=universe)

    async def _mock_scan(sym):
        async with MagicMock():
            pass
        return sym, summaries.get(sym)

    return scanner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetTopCandidates:

    @pytest.mark.asyncio
    async def test_sorted_by_rvol_descending(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["A", "B", "C"])

        summaries = {
            "A": _make_summary("A", rvol=1.0),
            "B": _make_summary("B", rvol=3.0),
            "C": _make_summary("C", rvol=2.0),
        }

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=3)

        symbols = [r["symbol"] for r in results]
        rvols = [r["rvol"] for r in results]
        assert rvols == sorted(rvols, reverse=True), "Should be sorted by RVOL descending"
        assert symbols[0] == "B"

    @pytest.mark.asyncio
    async def test_exclude_set_skipped(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["AAPL", "MSFT"])

        summaries = {"AAPL": _make_summary("AAPL"), "MSFT": _make_summary("MSFT")}

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=5, exclude={"AAPL"})

        symbols = [r["symbol"] for r in results]
        assert "AAPL" not in symbols
        assert "MSFT" in symbols

    @pytest.mark.asyncio
    async def test_low_bars_filtered(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["A", "B"])

        summaries = {
            "A": _make_summary("A", bars=2, rvol=2.0),  # low bars → filtered
            "B": _make_summary("B", bars=100, rvol=2.0),
        }

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=5)

        assert all(r["symbol"] != "A" for r in results)

    @pytest.mark.asyncio
    async def test_low_rvol_filtered(self):
        adapter = MagicMock()
        cfg = _make_config(min_rvol=1.0)
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["A", "B"])

        summaries = {
            "A": _make_summary("A", rvol=0.5),  # below threshold → filtered
            "B": _make_summary("B", rvol=2.0),
        }

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=5)

        assert all(r["symbol"] != "A" for r in results)

    @pytest.mark.asyncio
    async def test_candidate_count_capped_at_n(self):
        adapter = MagicMock()
        cfg = _make_config(max_candidates=10)
        scanner = UniverseScanner(adapter, cfg)
        universe = [f"SYM{i}" for i in range(20)]
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=universe)

        summaries = {sym: _make_summary(sym, rvol=float(i + 1)) for i, sym in enumerate(universe)}

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=5)

        assert len(results) <= 5

    @pytest.mark.asyncio
    async def test_empty_universe_returns_empty(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=[])

        results = await scanner.get_top_candidates(n=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_recent_news_capped_at_2(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["AAPL"])

        summaries = {"AAPL": _make_summary("AAPL")}
        many_news = [{"title": f"News {i}", "publisher": "Reuters", "age_hours": i + 1.0} for i in range(5)]

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=many_news)
            results = await scanner.get_top_candidates(n=5)

        assert len(results) == 1
        assert len(results[0]["recent_news"]) <= 2

    @pytest.mark.asyncio
    async def test_recent_news_empty_when_no_news(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["AAPL"])

        summaries = {"AAPL": _make_summary("AAPL")}

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=5)

        assert results[0]["recent_news"] == []

    @pytest.mark.asyncio
    async def test_earnings_within_days_populated(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["NVDA"])

        summaries = {"NVDA": _make_summary("NVDA")}

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=3),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=5)

        assert results[0]["earnings_within_days"] == 3

    @pytest.mark.asyncio
    async def test_earnings_within_days_none_on_exception(self):
        adapter = MagicMock()
        cfg = _make_config()
        scanner = UniverseScanner(adapter, cfg)
        scanner._fetcher = MagicMock()
        scanner._fetcher.get_universe = AsyncMock(return_value=["NVDA"])

        summaries = {"NVDA": _make_summary("NVDA")}

        async def _fake_to_thread(fn, *args):
            if len(args) == 2:  # TA call: (sym, df)
                return summaries.get(args[0])
            return fn(*args)  # earnings call: (sym,) — call patched fn directly

        with (
            patch("ozymandias.intelligence.universe_scanner.asyncio.to_thread", side_effect=_fake_to_thread),
            patch("ozymandias.intelligence.universe_scanner._fetch_earnings_calendar", return_value=None),
        ):
            adapter.fetch_bars = AsyncMock(return_value=pd.DataFrame({"close": [1.0]}))
            adapter.fetch_news = AsyncMock(return_value=[])
            results = await scanner.get_top_candidates(n=5)

        assert results[0]["earnings_within_days"] is None


class TestFetchEarningsCalendar:

    def test_returns_none_on_exception(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("network")):
            result = _fetch_earnings_calendar("AAPL")
        assert result is None

    def test_returns_days_within_window(self):
        from datetime import date
        future = date.today() + timedelta(days=3)

        mock_dt = MagicMock()
        mock_dt.date.return_value = future

        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Earnings Date": [mock_dt]}

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = _fetch_earnings_calendar("NVDA")
        assert result == 3

    def test_returns_none_when_beyond_10_days(self):
        from datetime import date
        future = date.today() + timedelta(days=15)

        mock_dt = MagicMock()
        mock_dt.date.return_value = future

        mock_ticker = MagicMock()
        mock_ticker.calendar = {"Earnings Date": [mock_dt]}

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = _fetch_earnings_calendar("AAPL")
        assert result is None
