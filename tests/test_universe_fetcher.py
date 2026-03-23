"""
tests/test_universe_fetcher.py
==============================
Unit tests for UniverseFetcher.

All network calls are mocked — no real HTTP is issued.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ozymandias.intelligence.universe_fetcher import UniverseFetcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_screener_response(symbols: list[str]) -> bytes:
    """Build a minimal Yahoo Finance screener JSON response."""
    quotes = [{"symbol": s} for s in symbols]
    data = {"finance": {"result": [{"quotes": quotes}]}}
    return json.dumps(data).encode()


def _make_wikipedia_tables(symbols: list[str], column: str) -> list:
    """Build a minimal pd.read_html-style result for Wikipedia pages."""
    import pandas as pd
    return [pd.DataFrame({column: symbols})]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetUniverse:
    """get_universe() merges Source A and Source B, deduplicates, and returns strings."""

    @pytest.mark.asyncio
    async def test_merges_source_a_and_b(self):
        fetcher = UniverseFetcher()
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=["AAPL", "MSFT"])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=["MSFT", "NVDA"])),
        ):
            result = await fetcher.get_universe()
        # Source A first, deduplicated
        assert result == ["AAPL", "MSFT", "NVDA"]

    @pytest.mark.asyncio
    async def test_deduplicates_preserving_source_a_order(self):
        fetcher = UniverseFetcher()
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=["C", "B", "A"])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=["A", "D"])),
        ):
            result = await fetcher.get_universe()
        assert result == ["C", "B", "A", "D"]

    @pytest.mark.asyncio
    async def test_returns_list_of_strings(self):
        fetcher = UniverseFetcher()
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=["AAPL"])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=[])),
        ):
            result = await fetcher.get_universe()
        assert all(isinstance(s, str) for s in result)

    @pytest.mark.asyncio
    async def test_source_a_failure_returns_empty_for_a(self):
        fetcher = UniverseFetcher()
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=[])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=["NVDA"])),
        ):
            result = await fetcher.get_universe()
        assert result == ["NVDA"]

    @pytest.mark.asyncio
    async def test_source_b_failure_returns_empty_for_b(self):
        fetcher = UniverseFetcher()
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=["AAPL"])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=[])),
        ):
            result = await fetcher.get_universe()
        assert result == ["AAPL"]

    @pytest.mark.asyncio
    async def test_no_entry_symbols_filtered(self):
        fetcher = UniverseFetcher(no_entry_symbols=["SPY", "QQQ"])
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=["SPY", "AAPL"])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=["QQQ", "MSFT"])),
        ):
            result = await fetcher.get_universe()
        assert "SPY" not in result
        assert "QQQ" not in result
        assert "AAPL" in result
        assert "MSFT" in result

    @pytest.mark.asyncio
    async def test_non_alphabetic_symbols_filtered(self):
        fetcher = UniverseFetcher()
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=["BRK.B", "AAPL", "BF-A"])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=[])),
        ):
            result = await fetcher.get_universe()
        assert "BRK.B" not in result
        assert "BF-A" not in result
        assert "AAPL" in result

    @pytest.mark.asyncio
    async def test_both_sources_fail_returns_empty(self):
        fetcher = UniverseFetcher()
        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(return_value=[])),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=[])),
        ):
            result = await fetcher.get_universe()
        assert result == []

    @pytest.mark.asyncio
    async def test_source_a_exception_swallowed(self):
        fetcher = UniverseFetcher()

        async def _raise():
            raise RuntimeError("network error")

        with (
            patch.object(fetcher, "_fetch_source_a", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(fetcher, "_fetch_source_b", new=AsyncMock(return_value=["NVDA"])),
        ):
            # Should not raise
            result = await fetcher.get_universe()
        # Source A failed → only source B
        assert result == ["NVDA"]

    @pytest.mark.asyncio
    async def test_source_b_uses_cache(self):
        fetcher = UniverseFetcher()
        fetcher._source_b_cache = ["AAPL"]
        # Set expiry in the far future
        import time
        fetcher._source_b_expires = time.monotonic() + 9999

        with patch.object(fetcher, "_fetch_index_constituents") as mock_fetch:
            result = await fetcher._fetch_source_b()

        mock_fetch.assert_not_called()
        assert result == ["AAPL"]
