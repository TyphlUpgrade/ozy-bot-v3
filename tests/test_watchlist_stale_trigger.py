"""
Tests for the watchlist_stale time-based trigger and last_watchlist_build_utc tracking.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from ozymandias.core.config import Config
from ozymandias.core.state_manager import WatchlistState, PortfolioState
from ozymandias.core.orchestrator import Orchestrator, SlowLoopTriggerState


def _make_trigger_orch(watchlist_refresh_interval_min: int = 120):
    """Minimal orchestrator for _check_triggers tests."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._config = Config()
    orch._config.scheduler.watchlist_refresh_interval_min = watchlist_refresh_interval_min
    ts = SlowLoopTriggerState()
    # Suppress no_previous_call and time_ceiling by setting a recent call time
    ts.last_claude_call_utc = datetime.now(timezone.utc)
    orch._trigger_state = ts
    orch._last_known_equity = 30_000.0
    orch._degradation = MagicMock()
    orch._degradation.claude_available = True
    orch._degradation.claude_backoff_until_utc = None
    orch._latest_indicators = {}
    orch._all_indicators = {}
    orch._market_context_indicators = {}
    orch._override_exit_count = 0
    orch._state_manager = MagicMock()
    orch._state_manager.load_watchlist = AsyncMock(
        return_value=WatchlistState(entries=[])
    )
    orch._state_manager.load_portfolio = AsyncMock(return_value=PortfolioState())
    return orch, ts


class TestWatchlistStaleTrigger:
    @pytest.mark.asyncio
    async def test_watchlist_stale_fires_when_overdue(self):
        """watchlist_stale fires when last build was > interval ago."""
        orch, ts = _make_trigger_orch(watchlist_refresh_interval_min=120)
        ts.last_watchlist_build_utc = datetime.now(timezone.utc) - timedelta(hours=3)

        triggers = await orch._check_triggers()
        assert "watchlist_stale" in triggers

    @pytest.mark.asyncio
    async def test_watchlist_stale_does_not_fire_when_fresh(self):
        """watchlist_stale does NOT fire when last build was recent."""
        orch, ts = _make_trigger_orch(watchlist_refresh_interval_min=120)
        ts.last_watchlist_build_utc = datetime.now(timezone.utc) - timedelta(minutes=30)

        triggers = await orch._check_triggers()
        assert "watchlist_stale" not in triggers

    @pytest.mark.asyncio
    async def test_watchlist_stale_fires_when_never_built(self):
        """watchlist_stale fires on startup when last_watchlist_build_utc is None."""
        orch, ts = _make_trigger_orch(watchlist_refresh_interval_min=120)
        assert ts.last_watchlist_build_utc is None  # default state

        triggers = await orch._check_triggers()
        assert "watchlist_stale" in triggers

    @pytest.mark.asyncio
    async def test_watchlist_stale_disabled_when_interval_zero(self):
        """watchlist_stale trigger is never appended when interval is 0."""
        orch, ts = _make_trigger_orch(watchlist_refresh_interval_min=0)
        # Even with None (never built), trigger should not fire
        assert ts.last_watchlist_build_utc is None

        triggers = await orch._check_triggers()
        assert "watchlist_stale" not in triggers

    @pytest.mark.asyncio
    async def test_last_watchlist_build_utc_set_after_build(self):
        """After a watchlist build triggered by watchlist_stale, last_watchlist_build_utc
        is set and subsequent trigger check within the interval does not re-fire."""
        orch, ts = _make_trigger_orch(watchlist_refresh_interval_min=120)
        # Start with never built → stale fires
        assert ts.last_watchlist_build_utc is None

        # Simulate the build completing by setting the timestamp
        ts.last_watchlist_build_utc = datetime.now(timezone.utc)

        # Now a second check within the interval should NOT fire
        triggers = await orch._check_triggers()
        assert "watchlist_stale" not in triggers
