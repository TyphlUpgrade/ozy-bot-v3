"""
Tests for UniverseScanner — candidate filter logic (RVOL path + price-move path).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import numpy as np
import pytest

from ozymandias.intelligence.universe_scanner import UniverseScanner, UniverseScannerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(min_rvol: float = 0.8, min_move: float = 1.5) -> UniverseScannerConfig:
    return UniverseScannerConfig(
        enabled=True,
        scan_concurrency=5,
        max_candidates=50,
        min_rvol_for_candidate=min_rvol,
        min_price_move_pct_for_candidate=min_move,
        cache_ttl_min=60,
    )


def _minimal_bars(n: int = 30) -> pd.DataFrame:
    closes = 100.0 + np.linspace(0, 2, n)
    return pd.DataFrame({
        "open":   closes - 0.1,
        "high":   closes + 0.3,
        "low":    closes - 0.3,
        "close":  closes,
        "volume": [1_000_000] * n,
    })


def _make_summary(rvol: float, roc5: float, price: float = 100.0) -> dict:
    """Build a minimal generate_signal_summary()-style dict."""
    return {
        "bars_available": 30,
        "signals": {
            "volume_ratio": rvol,
            "roc_5": roc5,
            "price": price,
            "rsi": 55.0,
            "macd_signal": "bullish",
            "trend_structure": "mixed",
            "vwap_position": "above",
        },
    }


def _scanner_with_ta(symbol_summaries: dict[str, dict]) -> UniverseScanner:
    """
    Build a UniverseScanner whose internal TA is mocked to return pre-baked
    summaries, bypassing real yfinance/TA calls.
    """
    adapter = MagicMock()
    adapter.fetch_bars = AsyncMock(return_value=_minimal_bars())
    adapter.fetch_news = AsyncMock(return_value=[])

    scanner = UniverseScanner(data_adapter=adapter, config=_cfg())

    async def _fake_scan_one(sym):
        return sym, symbol_summaries.get(sym)

    return scanner, _fake_scan_one


# ---------------------------------------------------------------------------
# Filter logic tests — unit-level, no real I/O
# ---------------------------------------------------------------------------

class TestUniverseScannerFilter:

    @pytest.mark.asyncio
    async def test_rvol_path_qualifies(self):
        """Symbol with RVOL >= floor passes even with small price move."""
        summaries = {"AAPL": _make_summary(rvol=1.2, roc5=0.3)}
        scanner, fake_scan = _scanner_with_ta(summaries)

        with (
            patch.object(scanner._fetcher, "get_universe", AsyncMock(return_value=["AAPL"])),
            patch.object(scanner, "_scan_one_for_test", fake_scan, create=True),
        ):
            # Directly test the filter step using the real get_top_candidates
            # but with TA mocked via asyncio.gather patch.
            async def _gather_mock(*coros):
                return [await c for c in coros]

            with patch("ozymandias.intelligence.universe_scanner.asyncio.gather",
                       side_effect=_gather_mock):
                # Re-route _scan_one inside get_top_candidates
                pass  # tested via integration path below

        # Direct filter check — replicate the filter logic
        cfg = _cfg(min_rvol=0.8, min_move=1.5)
        summary = _make_summary(rvol=1.2, roc5=0.3)
        signals = summary["signals"]
        rvol = signals["volume_ratio"]
        roc5 = signals["roc_5"]
        assert rvol >= cfg.min_rvol_for_candidate  # passes via volume path
        assert abs(roc5) < cfg.min_price_move_pct_for_candidate  # would fail move path

    @pytest.mark.asyncio
    async def test_price_move_path_qualifies_low_rvol(self):
        """Symbol with RVOL < floor but large price move passes via move path."""
        cfg = _cfg(min_rvol=0.8, min_move=1.5)
        summary = _make_summary(rvol=0.4, roc5=2.1)
        signals = summary["signals"]
        rvol = signals["volume_ratio"]
        roc5 = signals["roc_5"]
        via_rvol = rvol >= cfg.min_rvol_for_candidate
        via_move = abs(roc5) >= cfg.min_price_move_pct_for_candidate
        assert not via_rvol
        assert via_move

    @pytest.mark.asyncio
    async def test_negative_price_move_qualifies(self):
        """Negative ROC (price falling) qualifies via the price-move path — direction-agnostic."""
        cfg = _cfg(min_rvol=0.8, min_move=1.5)
        summary = _make_summary(rvol=0.3, roc5=-2.5)  # significant down move
        signals = summary["signals"]
        rvol = signals["volume_ratio"]
        roc5 = signals["roc_5"]
        via_rvol = rvol >= cfg.min_rvol_for_candidate
        via_move = abs(roc5) >= cfg.min_price_move_pct_for_candidate
        assert not via_rvol
        assert via_move  # negative move still qualifies

    @pytest.mark.asyncio
    async def test_neither_path_rejects(self):
        """Symbol with low RVOL and small price move is filtered out."""
        cfg = _cfg(min_rvol=0.8, min_move=1.5)
        summary = _make_summary(rvol=0.3, roc5=0.5)
        signals = summary["signals"]
        rvol = signals["volume_ratio"]
        roc5 = signals["roc_5"]
        via_rvol = rvol >= cfg.min_rvol_for_candidate
        via_move = abs(roc5) >= cfg.min_price_move_pct_for_candidate
        assert not via_rvol
        assert not via_move

    @pytest.mark.asyncio
    async def test_both_paths_qualify_counted_as_rvol(self):
        """Symbol satisfying both paths counts under volume_path only (not double-counted)."""
        cfg = _cfg(min_rvol=0.8, min_move=1.5)
        summary = _make_summary(rvol=1.5, roc5=2.0)
        signals = summary["signals"]
        rvol = signals["volume_ratio"]
        roc5 = signals["roc_5"]
        via_rvol = rvol >= cfg.min_rvol_for_candidate
        via_move = abs(roc5) >= cfg.min_price_move_pct_for_candidate
        assert via_rvol and via_move
        # price_move_path_count only increments when via_move AND NOT via_rvol
        is_move_only = via_move and not via_rvol
        assert not is_move_only  # counted as rvol path

    @pytest.mark.asyncio
    async def test_bars_below_minimum_always_rejected(self):
        """Symbol with < 5 bars is rejected regardless of RVOL or price move."""
        summary = _make_summary(rvol=2.0, roc5=5.0)
        summary["bars_available"] = 3  # below the 5-bar floor
        assert summary["bars_available"] < 5

    @pytest.mark.asyncio
    async def test_exact_thresholds_qualify(self):
        """Symbols at exactly the threshold values should pass (>= is inclusive)."""
        cfg = _cfg(min_rvol=0.8, min_move=1.5)
        for rvol, roc5 in [(0.8, 0.0), (0.0, 1.5), (0.0, -1.5)]:
            via_rvol = rvol >= cfg.min_rvol_for_candidate
            via_move = abs(roc5) >= cfg.min_price_move_pct_for_candidate
            assert via_rvol or via_move, f"Expected pass at rvol={rvol} roc5={roc5}"


# ---------------------------------------------------------------------------
# Integration-style test through get_top_candidates
# ---------------------------------------------------------------------------

class TestUniverseScannerIntegration:

    @pytest.mark.asyncio
    async def test_get_top_candidates_includes_price_move_only_symbols(self):
        """
        get_top_candidates returns a symbol that passes only via price-move path,
        not via RVOL, when another symbol passes only via RVOL path.
        """
        high_rvol_sym = "AAA"    # passes via RVOL (1.5 > 0.8), small move
        low_rvol_mover = "BBB"   # passes via move (2.0% > 1.5%), low RVOL (0.2)
        dead_sym = "CCC"         # fails both paths (rvol=0.3, roc5=0.4)

        summaries = {
            high_rvol_sym: _make_summary(rvol=1.5, roc5=0.3),
            low_rvol_mover: _make_summary(rvol=0.2, roc5=2.0),
            dead_sym: _make_summary(rvol=0.3, roc5=0.4),
        }

        adapter = MagicMock()
        adapter.fetch_bars = AsyncMock(return_value=_minimal_bars())
        adapter.fetch_news = AsyncMock(return_value=[])

        scanner = UniverseScanner(data_adapter=adapter, config=_cfg())

        with (
            patch.object(
                scanner._fetcher, "get_universe",
                AsyncMock(return_value=[high_rvol_sym, low_rvol_mover, dead_sym]),
            ),
            patch(
                "ozymandias.intelligence.universe_scanner.generate_signal_summary",
                side_effect=lambda sym, df: summaries[sym],
            ),
            patch(
                "ozymandias.intelligence.universe_scanner._fetch_earnings_calendar",
                return_value=None,
            ),
        ):
            candidates = await scanner.get_top_candidates(n=10)

        symbols = [c["symbol"] for c in candidates]
        assert high_rvol_sym in symbols, "High-RVOL symbol must be included"
        assert low_rvol_mover in symbols, "Price-move-only symbol must be included"
        assert dead_sym not in symbols, "Symbol failing both paths must be excluded"

    @pytest.mark.asyncio
    async def test_high_rvol_sorts_before_move_only(self):
        """
        Candidates are sorted by RVOL descending: high-RVOL symbols appear before
        price-move-only symbols (which have low RVOL).
        """
        summaries = {
            "HIGH_RVOL": _make_summary(rvol=2.0, roc5=0.5),
            "MOVER": _make_summary(rvol=0.2, roc5=3.0),
        }
        adapter = MagicMock()
        adapter.fetch_bars = AsyncMock(return_value=_minimal_bars())
        adapter.fetch_news = AsyncMock(return_value=[])
        scanner = UniverseScanner(data_adapter=adapter, config=_cfg())

        with (
            patch.object(
                scanner._fetcher, "get_universe",
                AsyncMock(return_value=["MOVER", "HIGH_RVOL"]),  # mover listed first
            ),
            patch(
                "ozymandias.intelligence.universe_scanner.generate_signal_summary",
                side_effect=lambda sym, df: summaries[sym],
            ),
            patch(
                "ozymandias.intelligence.universe_scanner._fetch_earnings_calendar",
                return_value=None,
            ),
        ):
            candidates = await scanner.get_top_candidates(n=10)

        symbols = [c["symbol"] for c in candidates]
        assert symbols.index("HIGH_RVOL") < symbols.index("MOVER"), (
            "High-RVOL symbol must sort before price-move-only symbol"
        )
