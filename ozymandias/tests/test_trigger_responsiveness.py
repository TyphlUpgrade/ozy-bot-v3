"""
tests/test_trigger_responsiveness.py
=====================================
Tests for Phase 17 — Trigger Responsiveness & Data Freshness.

Covers:
  Fix 1: parallel medium loop fetch (_all_indicators, _last_medium_loop_completed_utc)
  Fix 2: macro/sector/RSI extreme triggers
  Fix 3: medium-loop gate in _slow_loop_cycle
  Fix 4: adaptive cache TTL (_compute_cache_max_age, load_latest_if_fresh override)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pandas as pd
import pytest
import pytest_asyncio

from ozymandias.core.orchestrator import Orchestrator, SlowLoopTriggerState
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import (
    PortfolioState,
    Position,
    TradeIntention,
    ExitTargets,
    WatchlistEntry,
    WatchlistState,
)
from ozymandias.execution.broker_interface import AccountInfo, MarketHours


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _stub_account() -> AccountInfo:
    return AccountInfo(
        equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
        currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
    )


def _stub_hours() -> MarketHours:
    now = datetime.now(timezone.utc)
    return MarketHours(
        is_open=True,
        next_open=now + timedelta(hours=1),
        next_close=now + timedelta(hours=8),
        session="regular",
    )


def _minimal_bars(n: int = 30, base: float = 100.0) -> pd.DataFrame:
    """Return a minimal OHLCV DataFrame sufficient to compute TA signals."""
    import numpy as np
    closes = base + np.sin(np.linspace(0, 4, n)) * 2
    df = pd.DataFrame({
        "open":   closes - 0.1,
        "high":   closes + 0.3,
        "low":    closes - 0.3,
        "close":  closes,
        "volume": [1_000_000] * n,
    })
    return df


@pytest_asyncio.fixture
async def orch(tmp_path):
    with (
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.__init__",
              MagicMock(return_value=None)),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_account",
              AsyncMock(return_value=_stub_account())),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours",
              AsyncMock(return_value=_stub_hours())),
        patch("anthropic.AsyncAnthropic", MagicMock),
        patch("ozymandias.core.orchestrator.Orchestrator._load_credentials",
              MagicMock(return_value=("k", "s"))),
    ):
        o = Orchestrator()
        o._state_manager._dir = tmp_path
        o._trade_journal._path = tmp_path / "trade_journal.jsonl"
        o._reasoning_cache._dir = tmp_path / "cache"
        o._reasoning_cache._dir.mkdir()
        await o._startup()

    broker = MagicMock()
    broker.get_account = AsyncMock(return_value=_stub_account())
    broker.get_open_orders = AsyncMock(return_value=[])
    broker.get_positions = AsyncMock(return_value=[])
    broker.place_order = AsyncMock()
    broker.cancel_order = AsyncMock()
    o._broker = broker
    return o


async def _seed_watchlist(orch, symbols, tier=1):
    now = datetime.now(timezone.utc).isoformat()
    entries = [
        WatchlistEntry(symbol=s, date_added=now, reason="test", priority_tier=tier)
        for s in symbols
    ]
    await orch._state_manager.save_watchlist(WatchlistState(entries=entries))


async def _seed_portfolio(orch, positions=()):
    await orch._state_manager.save_portfolio(PortfolioState(positions=list(positions)))


def _make_position(symbol: str, shares: float = 10.0, avg_cost: float = 100.0) -> Position:
    return Position(
        symbol=symbol,
        shares=shares,
        avg_cost=avg_cost,
        entry_date=datetime.now(timezone.utc).isoformat(),
        intention=TradeIntention(
            strategy="momentum",
            direction="long",
            exit_targets=ExitTargets(profit_target=110.0, stop_loss=95.0),
        ),
    )


# ===========================================================================
# Fix 1 — Parallel Medium Loop Fetch
# ===========================================================================

class TestParallelMediumLoop:

    @pytest.mark.asyncio
    async def test_all_indicators_set_after_medium_loop(self, orch):
        """_all_indicators is populated after _medium_loop_cycle completes."""
        await _seed_watchlist(orch, ["NVDA", "AMD"])
        await _seed_portfolio(orch)

        bars = _minimal_bars()
        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = AsyncMock(return_value=bars)
        orch._data_adapter.fetch_news = AsyncMock(return_value=[])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._medium_loop_cycle()

        assert "NVDA" in orch._all_indicators or "AMD" in orch._all_indicators
        # _all_indicators must be non-empty (at least one symbol succeeded)
        assert orch._all_indicators

    @pytest.mark.asyncio
    async def test_last_medium_loop_completed_utc_set(self, orch):
        """_last_medium_loop_completed_utc is stamped at the end of each medium cycle."""
        assert orch._last_medium_loop_completed_utc is None

        await _seed_watchlist(orch, ["SPY"])
        await _seed_portfolio(orch)

        bars = _minimal_bars()
        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = AsyncMock(return_value=bars)
        orch._data_adapter.fetch_news = AsyncMock(return_value=[])

        before = datetime.now(timezone.utc)
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._medium_loop_cycle()

        assert orch._last_medium_loop_completed_utc is not None
        assert orch._last_medium_loop_completed_utc >= before

    @pytest.mark.asyncio
    async def test_all_indicators_merges_context_symbols(self, orch):
        """_all_indicators contains both watchlist symbols and context instruments."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        bars = _minimal_bars()
        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = AsyncMock(return_value=bars)
        orch._data_adapter.fetch_news = AsyncMock(return_value=[])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._medium_loop_cycle()

        # Both a watchlist symbol and at least one context symbol should be present
        assert "NVDA" in orch._all_indicators
        # At least one of the broad-market context symbols should appear
        context_present = any(
            s in orch._all_indicators for s in ["SPY", "QQQ", "IWM", "XLK"]
        )
        assert context_present

    @pytest.mark.asyncio
    async def test_partial_failures_still_complete(self, orch):
        """When some fetch_bars calls fail, the successful ones still populate indicators."""
        await _seed_watchlist(orch, ["NVDA", "AMD", "TSLA"])
        await _seed_portfolio(orch)

        bars = _minimal_bars()
        call_count = {"n": 0}

        async def flaky_fetch(symbol, **kwargs):
            call_count["n"] += 1
            if symbol == "AMD":
                raise RuntimeError("network error")
            return bars

        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = flaky_fetch
        orch._data_adapter.fetch_news = AsyncMock(return_value=[])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._medium_loop_cycle()

        # AMD failed but others should still populate
        assert "AMD" not in orch._latest_indicators
        assert "NVDA" in orch._latest_indicators or "TSLA" in orch._latest_indicators


# ===========================================================================
# Fix 2 — Macro / Sector / RSI Extreme Triggers
# ===========================================================================

class TestMacroMoveTrigger:

    @pytest.mark.asyncio
    async def test_market_move_fires_when_spy_moves_over_threshold(self, orch):
        """market_move:SPY fires when SPY moves >1% from last_claude_call_prices."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        # Seed last Claude call prices with SPY at 500
        orch._trigger_state.last_claude_call_prices = {"SPY": 500.0}
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        # Current SPY at 506 (+1.2%) — over 1% threshold
        orch._market_context_indicators = {
            "SPY": {"signals": {"price": 506.0, "rsi": 55.0}}
        }
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        triggers = await orch._check_triggers()

        assert "market_move:SPY" in triggers

    @pytest.mark.asyncio
    async def test_market_move_does_not_fire_below_threshold(self, orch):
        """market_move:SPY does NOT fire when move is under 1%."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        orch._trigger_state.last_claude_call_prices = {"SPY": 500.0}
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        # SPY at 504 (+0.8%) — under 1% threshold
        orch._market_context_indicators = {
            "SPY": {"signals": {"price": 504.0, "rsi": 55.0}}
        }
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        triggers = await orch._check_triggers()

        assert "market_move:SPY" not in triggers

    @pytest.mark.asyncio
    async def test_market_move_no_fire_without_baseline(self, orch):
        """market_move does NOT fire when last_claude_call_prices is empty."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        # No baseline prices
        orch._trigger_state.last_claude_call_prices = {}
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        orch._market_context_indicators = {
            "SPY": {"signals": {"price": 510.0, "rsi": 55.0}}
        }
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        triggers = await orch._check_triggers()

        assert not any(t.startswith("market_move:") for t in triggers)


class TestSectorMoveTrigger:

    @pytest.mark.asyncio
    async def test_sector_move_fires_at_base_threshold(self, orch):
        """sector_move:XLE fires when XLE moves >1.5% and no XLE-sector exposure."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)  # no positions → no exposure

        orch._trigger_state.last_claude_call_prices = {"XLE": 80.0}
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        # XLE at 81.3 = +1.625% — over 1.5% base threshold
        orch._market_context_indicators = {"XLE": {"signals": {"price": 81.3, "rsi": 55.0}}}
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        triggers = await orch._check_triggers()

        assert "sector_move:XLE" in triggers

    @pytest.mark.asyncio
    async def test_sector_move_fires_at_tightened_threshold_when_exposed(self, orch):
        """sector_move:XLE fires at 1.05% (=1.5%×0.7) when we hold an XLE-sector position."""
        await _seed_watchlist(orch, ["NVDA"])
        # Hold XOM (maps to XLE in _SECTOR_MAP) → exposure to XLE sector
        await _seed_portfolio(orch, [_make_position("XOM")])

        orch._trigger_state.last_claude_call_prices = {"XLE": 80.0}
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        # XLE at 80.9 = +1.125% — over tightened 1.05% threshold, under base 1.5%
        orch._market_context_indicators = {"XLE": {"signals": {"price": 80.9, "rsi": 55.0}}}
        orch._latest_indicators = {"XOM": {"price": 110.0}}

        triggers = await orch._check_triggers()

        assert "sector_move:XLE" in triggers

    @pytest.mark.asyncio
    async def test_sector_move_does_not_fire_below_tightened_threshold(self, orch):
        """sector_move:XLE does NOT fire at 0.9% even with exposure (< 1.05%)."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch, [_make_position("XOM")])  # XLE exposure

        orch._trigger_state.last_claude_call_prices = {"XLE": 80.0}
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        # XLE at 80.72 = +0.9% — under tightened 1.05% threshold
        orch._market_context_indicators = {"XLE": {"signals": {"price": 80.72, "rsi": 55.0}}}
        orch._latest_indicators = {"XOM": {"price": 110.0}}

        triggers = await orch._check_triggers()

        assert "sector_move:XLE" not in triggers

    @pytest.mark.asyncio
    async def test_sector_move_skips_directly_held_etf(self, orch):
        """sector_move does NOT fire for an ETF that is itself an open position (price_move covers it)."""
        await _seed_watchlist(orch, ["XLE"])
        await _seed_portfolio(orch, [_make_position("XLE")])  # holding the ETF directly

        orch._trigger_state.last_claude_call_prices = {"XLE": 80.0}
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        orch._market_context_indicators = {"XLE": {"signals": {"price": 82.0, "rsi": 55.0}}}
        orch._latest_indicators = {"XLE": {"price": 82.0}}

        triggers = await orch._check_triggers()

        # sector_move should NOT fire for directly held ETF; price_move handles it
        assert "sector_move:XLE" not in triggers


class TestRsiExtremeTrigger:

    @pytest.mark.asyncio
    async def test_panic_trigger_fires_below_threshold(self, orch):
        """market_rsi_extreme fires when SPY RSI < 25."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        orch._market_context_indicators = {
            "SPY": {"signals": {"price": 480.0, "rsi": 22.0}}
        }
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        triggers = await orch._check_triggers()

        assert "market_rsi_extreme" in triggers
        assert orch._trigger_state.rsi_extreme_fired_low is True

    @pytest.mark.asyncio
    async def test_euphoria_trigger_fires_above_threshold(self, orch):
        """market_rsi_extreme fires when SPY RSI > 72."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        orch._market_context_indicators = {
            "SPY": {"signals": {"price": 520.0, "rsi": 75.0}}
        }
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        triggers = await orch._check_triggers()

        assert "market_rsi_extreme" in triggers
        assert orch._trigger_state.rsi_extreme_fired_high is True

    @pytest.mark.asyncio
    async def test_panic_trigger_does_not_refire_before_rearm(self, orch):
        """market_rsi_extreme:panic does not re-fire until RSI recovers by rearm_band (5)."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        # First fire
        orch._trigger_state.rsi_extreme_fired_low = False
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        orch._market_context_indicators = {"SPY": {"signals": {"price": 480.0, "rsi": 20.0}}}
        orch._latest_indicators = {"NVDA": {"price": 200.0}}
        triggers1 = await orch._check_triggers()
        assert "market_rsi_extreme" in triggers1
        assert orch._trigger_state.rsi_extreme_fired_low is True

        # Still in panic zone — should NOT re-fire
        triggers2 = await orch._check_triggers()
        assert "market_rsi_extreme" not in triggers2

    @pytest.mark.asyncio
    async def test_panic_trigger_rearms_after_recovery(self, orch):
        """market_rsi_extreme:panic re-arms when RSI recovers above threshold + rearm_band."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        orch._trigger_state.rsi_extreme_fired_low = True
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        # RSI has recovered: 25 + 5 (rearm_band) = 30, so > 30 rearms
        orch._market_context_indicators = {"SPY": {"signals": {"price": 490.0, "rsi": 32.0}}}
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        await orch._check_triggers()  # rearm evaluates

        assert orch._trigger_state.rsi_extreme_fired_low is False

    @pytest.mark.asyncio
    async def test_normal_rsi_fires_no_extreme_trigger(self, orch):
        """market_rsi_extreme does NOT fire when SPY RSI is in normal range."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc) - timedelta(minutes=10)
        orch._market_context_indicators = {"SPY": {"signals": {"price": 500.0, "rsi": 55.0}}}
        orch._latest_indicators = {"NVDA": {"price": 200.0}}

        triggers = await orch._check_triggers()

        assert "market_rsi_extreme" not in triggers

    @pytest.mark.asyncio
    async def test_last_claude_call_prices_seeded_on_indicators_ready(self, orch):
        """last_claude_call_prices is seeded from current indicators when indicators_ready fires."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        # Indicators not seeded yet
        orch._trigger_state.indicators_seeded = False
        orch._trigger_state.last_claude_call_prices = {}
        orch._all_indicators = {
            "NVDA": {"price": 875.0},
            "SPY": {"signals": {"price": 500.0, "rsi": 55.0}},
        }
        orch._latest_indicators = {"NVDA": {"price": 875.0}}
        orch._market_context_indicators = {"SPY": {"signals": {"price": 500.0, "rsi": 55.0}}}

        await orch._check_triggers()

        assert "NVDA" in orch._trigger_state.last_claude_call_prices
        assert orch._trigger_state.last_claude_call_prices["NVDA"] == 875.0


# ===========================================================================
# Fix 3 — Medium-Loop Gate
# ===========================================================================

class TestMediumLoopGate:

    @pytest.mark.asyncio
    async def test_slow_loop_skips_when_no_medium_loop_completed(self, orch):
        """_slow_loop_cycle skips Claude when _last_medium_loop_completed_utc is None."""
        orch._latest_indicators = {"NVDA": {"price": 100.0}}
        orch._last_medium_loop_completed_utc = None  # medium loop never ran
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._slow_loop_cycle()

        assert not orch._claude._client.messages.create.called

    @pytest.mark.asyncio
    async def test_slow_loop_skips_when_medium_loop_older_than_last_call(self, orch):
        """_slow_loop_cycle skips when _last_medium_loop_completed_utc <= last_claude_call_utc."""
        orch._latest_indicators = {"NVDA": {"price": 100.0}}
        last_call = datetime.now(timezone.utc) - timedelta(minutes=5)
        orch._trigger_state.last_claude_call_utc = last_call
        # Medium loop ran before the last Claude call
        orch._last_medium_loop_completed_utc = last_call - timedelta(minutes=2)

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._slow_loop_cycle()

        assert not orch._claude._client.messages.create.called

    @pytest.mark.asyncio
    async def test_slow_loop_proceeds_when_medium_loop_newer_than_last_call(self, orch):
        """_slow_loop_cycle proceeds when medium loop completed after last Claude call."""
        orch._latest_indicators = {"NVDA": {"price": 100.0}}
        orch._all_indicators = {"NVDA": {"price": 100.0}}
        last_call = datetime.now(timezone.utc) - timedelta(minutes=10)
        orch._trigger_state.last_claude_call_utc = last_call
        # Medium loop ran AFTER the last Claude call
        orch._last_medium_loop_completed_utc = last_call + timedelta(minutes=5)

        # time_ceiling trigger should fire (>60 min backdated? No — only 10 min ago.
        # Use no_previous_call by setting last_claude_call_utc=None after the gate check.
        # Instead, backdate sufficiently:
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )
        orch._last_medium_loop_completed_utc = datetime.now(timezone.utc)

        # Patch Claude to return None (no response) — we only check the gate passes
        orch._claude.run_reasoning_cycle = AsyncMock(return_value=None)

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._slow_loop_cycle()

        # Gate passed — Claude was called (even though it returned None)
        assert orch._claude.run_reasoning_cycle.called

    @pytest.mark.asyncio
    async def test_slow_loop_allows_first_call_when_no_previous_claude_call(self, orch):
        """Gate is bypassed when last_claude_call_utc is None (first call ever)."""
        orch._latest_indicators = {"NVDA": {"price": 100.0}}
        orch._all_indicators = {"NVDA": {"price": 100.0}}
        orch._trigger_state.last_claude_call_utc = None
        orch._last_medium_loop_completed_utc = None  # gate condition

        orch._claude.run_reasoning_cycle = AsyncMock(return_value=None)

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._slow_loop_cycle()

        # When last_claude_call_utc is None, medium-loop gate does not apply
        assert orch._claude.run_reasoning_cycle.called


# ===========================================================================
# Fix 4 — Adaptive Cache TTL
# ===========================================================================

class TestAdaptiveCacheTtl:

    def test_load_latest_if_fresh_accepts_max_age_override(self, tmp_path):
        """load_latest_if_fresh respects max_age_min override."""
        import json, time

        cache = ReasoningCache(cache_dir=tmp_path)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": "test",
            "session_id": "2026-03-20",
            "input_context_hash": "sha256:abc",
            "input_tokens": 100,
            "output_tokens": 50,
            "raw_response": "{}",
            "parsed_response": {"market_assessment": "neutral"},
            "parse_success": True,
        }
        cache.save("test", {}, "{}", record["parsed_response"])

        # Default 60-min window → finds the fresh record
        result = cache.load_latest_if_fresh()
        assert result is not None

        # Override with 0 minutes → record is too old (even if just written)
        result_zero = cache.load_latest_if_fresh(max_age_min=0)
        assert result_zero is None

    def test_load_latest_if_fresh_default_unchanged(self, tmp_path):
        """Calling load_latest_if_fresh() with no args uses the default 60-min window."""
        cache = ReasoningCache(cache_dir=tmp_path)
        cache.save("test", {}, "{}", {"market_assessment": "neutral"})

        result = cache.load_latest_if_fresh()
        assert result is not None
        # With explicit None → same behavior
        result2 = cache.load_latest_if_fresh(max_age_min=None)
        assert result2 is not None

    def test_compute_cache_max_age_normal(self, orch):
        """_compute_cache_max_age returns default in normal RSI regime."""
        orch._market_context_indicators = {"SPY": {"signals": {"rsi": 55.0}}}
        assert orch._compute_cache_max_age() == orch._config.claude.cache_max_age_default_min

    def test_compute_cache_max_age_stress(self, orch):
        """_compute_cache_max_age returns stressed TTL when RSI ≤ cache_stress_rsi_low."""
        rsi_floor = orch._config.claude.cache_stress_rsi_low  # 30
        orch._market_context_indicators = {"SPY": {"signals": {"rsi": float(rsi_floor)}}}
        assert orch._compute_cache_max_age() == orch._config.claude.cache_max_age_stressed_min

    def test_compute_cache_max_age_panic(self, orch):
        """_compute_cache_max_age returns panic TTL when RSI ≤ cache_panic_rsi_low."""
        rsi_floor = orch._config.claude.cache_panic_rsi_low  # 25
        orch._market_context_indicators = {"SPY": {"signals": {"rsi": float(rsi_floor)}}}
        assert orch._compute_cache_max_age() == orch._config.claude.cache_max_age_panic_min

    def test_compute_cache_max_age_euphoria(self, orch):
        """_compute_cache_max_age returns euphoria TTL when RSI ≥ cache_euphoria_rsi_high."""
        rsi_ceil = orch._config.claude.cache_euphoria_rsi_high  # 72
        orch._market_context_indicators = {"SPY": {"signals": {"rsi": float(rsi_ceil) + 1}}}
        assert orch._compute_cache_max_age() == orch._config.claude.cache_max_age_euphoria_min

    def test_compute_cache_max_age_no_spy_data(self, orch):
        """_compute_cache_max_age returns default when SPY indicators unavailable."""
        orch._market_context_indicators = {}
        assert orch._compute_cache_max_age() == orch._config.claude.cache_max_age_default_min

    @pytest.mark.asyncio
    async def test_medium_loop_passes_max_age_to_cache(self, orch):
        """Medium loop calls load_latest_if_fresh with a max_age_min kwarg (not None)."""
        await _seed_watchlist(orch, ["NVDA"])
        await _seed_portfolio(orch)

        bars = _minimal_bars()
        orch._data_adapter = MagicMock()
        orch._data_adapter.fetch_bars = AsyncMock(return_value=bars)
        orch._data_adapter.fetch_news = AsyncMock(return_value=[])

        with (
            patch("ozymandias.core.orchestrator.is_market_open", return_value=True),
            patch.object(
                orch._reasoning_cache,
                "load_latest_if_fresh",
                wraps=orch._reasoning_cache.load_latest_if_fresh,
            ) as mock_load,
        ):
            await orch._medium_loop_cycle()

        # load_latest_if_fresh must have been called with an explicit max_age_min
        assert mock_load.called
        call_kwargs = mock_load.call_args
        # Verify the kwarg was passed (not relying on the default)
        passed_max_age = (
            call_kwargs.args[0] if call_kwargs.args
            else call_kwargs.kwargs.get("max_age_min")
        )
        # max_age_min must be an integer (one of the TTL config values)
        assert isinstance(passed_max_age, int)
        assert passed_max_age > 0
