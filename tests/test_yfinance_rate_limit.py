"""
Tests for yfinance rate-limit resilience in YFinanceAdapter.

The strategy is proactive staggering rather than reactive retrying:
  - fetch_bars sleeps a random amount before each cache-miss request
  - _set_cache adds TTL jitter so cached symbols expire at different times

Covers:
  1. fetch_bars sleeps before fetching on cache miss
  2. fetch_bars does NOT sleep on cache hit
  3. Stagger sleep is within [0, fetch_stagger_max_sec]
  4. _set_cache TTL jitter spreads expiry times
  5. fetch_stagger_max_sec=0 disables stagger (e.g. in tests)
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(stagger: float = 0.0) -> YFinanceAdapter:
    """Return adapter with stagger disabled by default so tests run instantly."""
    return YFinanceAdapter(fetch_stagger_max_sec=stagger)


def _minimal_df() -> pd.DataFrame:
    idx = pd.date_range("2024-01-02", periods=2, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9],
         "close": [1.05, 2.05], "volume": [1000, 2000]},
        index=idx,
    )


# ---------------------------------------------------------------------------
# 1. fetch_bars sleeps before fetching on cache miss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_bars_sleeps_before_fetch_on_cache_miss():
    adapter = _make_adapter(stagger=1.0)
    df_ok = _minimal_df()

    sleep_calls: list[float] = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    with patch.object(adapter, "_download_bars", return_value=df_ok), \
         patch("ozymandias.data.adapters.yfinance_adapter.asyncio.sleep", side_effect=_fake_sleep):
        await adapter.fetch_bars("AAPL", "5m", "1d")

    assert len(sleep_calls) == 1
    assert 0.0 <= sleep_calls[0] <= 1.0


# ---------------------------------------------------------------------------
# 2. fetch_bars does NOT sleep on cache hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_bars_no_sleep_on_cache_hit():
    adapter = _make_adapter(stagger=1.0)
    df_ok = _minimal_df()

    # Pre-populate cache
    with patch.object(adapter, "_download_bars", return_value=df_ok):
        await adapter.fetch_bars("AAPL", "5m", "1d")

    # Second call should hit cache — no sleep, no download
    sleep_calls: list[float] = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    download_mock = MagicMock()
    with patch.object(adapter, "_download_bars", download_mock), \
         patch("ozymandias.data.adapters.yfinance_adapter.asyncio.sleep", side_effect=_fake_sleep):
        result = await adapter.fetch_bars("AAPL", "5m", "1d")

    assert result is not None
    download_mock.assert_not_called()
    assert len(sleep_calls) == 0


# ---------------------------------------------------------------------------
# 3. Stagger sleep is always within [0, fetch_stagger_max_sec]
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_bars_stagger_within_bounds():
    """Stagger sleep values across multiple calls are within [0, max]."""
    stagger_max = 2.0
    adapter = _make_adapter(stagger=stagger_max)
    df_ok = _minimal_df()

    sleep_calls: list[float] = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    symbols = [f"SYM{i}" for i in range(10)]
    with patch.object(adapter, "_download_bars", return_value=df_ok), \
         patch("ozymandias.data.adapters.yfinance_adapter.asyncio.sleep", side_effect=_fake_sleep):
        await asyncio.gather(*[adapter.fetch_bars(s, "5m", "1d") for s in symbols])

    assert len(sleep_calls) == 10
    for s in sleep_calls:
        assert 0.0 <= s <= stagger_max, f"Sleep {s:.3f} out of bounds [0, {stagger_max}]"


# ---------------------------------------------------------------------------
# 4. _set_cache TTL jitter spreads expiry times
# ---------------------------------------------------------------------------

def test_set_cache_jitter_spreads_expiry():
    adapter = _make_adapter()
    ttl = 110

    for i in range(30):
        adapter._set_cache(f"key:{i}", {"data": i}, ttl)

    expiry_times = [adapter._cache[f"key:{i}"].expires_at for i in range(30)]
    distinct_values = len(set(round(t, 3) for t in expiry_times))
    assert distinct_values >= 3, (
        f"Expected jitter to produce at least 3 distinct expiry times, got {distinct_values}"
    )

    # All expiry times should be in range [ttl, ttl * 1.15]
    now = time.monotonic()
    for exp in expiry_times:
        offset = exp - now
        assert ttl <= offset <= ttl * 1.15 + 0.1, f"Expiry offset {offset:.2f} out of expected range"


# ---------------------------------------------------------------------------
# 5. fetch_stagger_max_sec=0 disables stagger entirely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_bars_zero_stagger_skips_sleep():
    adapter = _make_adapter(stagger=0.0)
    df_ok = _minimal_df()

    sleep_calls: list[float] = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    with patch.object(adapter, "_download_bars", return_value=df_ok), \
         patch("ozymandias.data.adapters.yfinance_adapter.asyncio.sleep", side_effect=_fake_sleep):
        await adapter.fetch_bars("AAPL", "5m", "1d")

    assert len(sleep_calls) == 0
