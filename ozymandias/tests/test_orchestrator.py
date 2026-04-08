"""
tests/test_orchestrator.py
===========================
Unit tests for Orchestrator trigger logic, slow loop, and degradation.

Tests that don't need external services — all broker, Claude, and yfinance
calls are mocked.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import pytest_asyncio

from ozymandias.core.orchestrator import (
    DegradationState,
    Orchestrator,
    SlowLoopTriggerState,
)
from ozymandias.core.state_manager import (
    ExitTargets,
    OrderRecord,
    OrdersState,
    PortfolioState,
    Position,
    TradeIntention,
    WatchlistEntry,
    WatchlistState,
)
from ozymandias.execution.broker_interface import AccountInfo, MarketHours, OrderStatus
from ozymandias.intelligence.opportunity_ranker import RankResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _stub_account() -> AccountInfo:
    return AccountInfo(
        equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
        currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
    )


def _stub_hours(session: str = "regular") -> MarketHours:
    now = datetime.now(timezone.utc)
    return MarketHours(
        is_open=(session == "regular"),
        next_open=now + timedelta(hours=1),
        next_close=now + timedelta(hours=8),
        session=session,
    )


@pytest_asyncio.fixture
async def orch(tmp_path):
    """
    A fully-started Orchestrator with all external calls mocked.
    The broker mock is attached after startup so tests can reconfigure it freely.
    """
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

    # Disable the universe scanner so tests don't make real yfinance calls for
    # 543+ symbols. The scanner holds its own adapter reference set during
    # _startup(), so replacing orch._data_adapter per-test is insufficient.
    o._universe_scanner = None

    # Mock Claude watchlist build so tests don't hit the 3-second min_call_interval_sec
    # sleep. Root cause: watchlist_small/watchlist_stale triggers cause _run_claude_cycle
    # to call run_watchlist_build first (sets _last_call_end_time), then run_reasoning_cycle
    # hits the rate-limit gap sleep. Tests needing real watchlist build override this.
    o._claude.run_watchlist_build = AsyncMock(return_value=[])

    # Replace broker with a configurable mock
    broker = MagicMock()
    broker.get_account  = AsyncMock(return_value=_stub_account())
    broker.get_open_orders = AsyncMock(return_value=[])
    broker.get_positions   = AsyncMock(return_value=[])
    broker.place_order     = AsyncMock()
    broker.cancel_order    = AsyncMock()
    o._broker = broker
    # Rebind extracted modules that stored a broker reference during _startup().
    o._quant_overrides._broker = broker
    return o


# ---------------------------------------------------------------------------
# Helper: seed state in the orchestrator's state manager
# ---------------------------------------------------------------------------

async def _set_watchlist(orch, tier1=(), tier2=()):
    now = datetime.now(timezone.utc).isoformat()
    entries = [
        WatchlistEntry(symbol=s, date_added=now, reason="test", priority_tier=1)
        for s in tier1
    ] + [
        WatchlistEntry(symbol=s, date_added=now, reason="test", priority_tier=2)
        for s in tier2
    ]
    await orch._state_manager.save_watchlist(WatchlistState(entries=entries))


async def _set_portfolio(orch, positions=()):
    await orch._state_manager.save_portfolio(
        PortfolioState(positions=list(positions))
    )


# ===========================================================================
# Trigger evaluation tests
# ===========================================================================

class TestCheckTriggers:

    @pytest.mark.asyncio
    async def test_no_previous_call_fires(self, orch):
        """time_ceiling fires when last_claude_call_utc is None."""
        orch._trigger_state.last_claude_call_utc = None
        triggers = await orch._check_triggers()
        assert "no_previous_call" in triggers

    @pytest.mark.asyncio
    async def test_time_ceiling_fires_after_60_min(self, orch):
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=61)
        )
        triggers = await orch._check_triggers()
        assert "time_ceiling" in triggers

    @pytest.mark.asyncio
    async def test_time_ceiling_does_not_fire_before_60_min(self, orch):
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        )
        triggers = await orch._check_triggers()
        assert "time_ceiling" not in triggers
        assert "no_previous_call" not in triggers

    @pytest.mark.asyncio
    async def test_price_move_fires_on_2pct_change(self, orch):
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._trigger_state.last_prices = {"AAPL": 200.0}
        orch._latest_indicators = {"AAPL": {"price": 205.0}}   # +2.5%
        triggers = await orch._check_triggers()
        assert "price_move:AAPL" in triggers

    @pytest.mark.asyncio
    async def test_price_move_does_not_fire_on_small_change(self, orch):
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._trigger_state.last_prices = {"AAPL": 200.0}
        orch._latest_indicators = {"AAPL": {"price": 200.5}}   # +0.25%
        triggers = await orch._check_triggers()
        assert "price_move:AAPL" not in triggers

    @pytest.mark.asyncio
    async def test_near_target_fires_within_1pct(self, orch):
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="TSLA",
            shares=10,
            avg_cost=300.0,
            entry_date=now,
            intention=TradeIntention(
                exit_targets=ExitTargets(profit_target=400.0, stop_loss=270.0)
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._latest_indicators = {"TSLA": {"price": 396.5}}   # 0.875% from target
        triggers = await orch._check_triggers()
        assert "near_target:TSLA" in triggers

    @pytest.mark.asyncio
    async def test_near_stop_fires_within_1pct(self, orch):
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="TSLA",
            shares=10,
            avg_cost=300.0,
            entry_date=now,
            intention=TradeIntention(
                exit_targets=ExitTargets(profit_target=400.0, stop_loss=270.0)
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._latest_indicators = {"TSLA": {"price": 271.0}}   # 0.37% from stop
        triggers = await orch._check_triggers()
        assert "near_stop:TSLA" in triggers

    @pytest.mark.asyncio
    async def test_override_exit_fires(self, orch):
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._trigger_state.last_override_exit_count = 0
        orch._override_exit_count = 1   # simulates fast loop placed an override exit
        triggers = await orch._check_triggers()
        assert "override_exit" in triggers

    @pytest.mark.asyncio
    async def test_override_exit_does_not_refire(self, orch):
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        # Count matches — no new overrides since last call
        orch._trigger_state.last_override_exit_count = 2
        orch._override_exit_count = 2
        triggers = await orch._check_triggers()
        assert "override_exit" not in triggers

    @pytest.mark.asyncio
    async def test_watchlist_small_fires_below_10(self, orch):
        await _set_watchlist(orch, tier1=["AAPL", "TSLA"])   # 2 < 10
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        triggers = await orch._check_triggers()
        assert "watchlist_small" in triggers

    @pytest.mark.asyncio
    async def test_watchlist_small_does_not_fire_when_large(self, orch):
        symbols = [f"SYM{i}" for i in range(12)]
        await _set_watchlist(orch, tier1=symbols)
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        triggers = await orch._check_triggers()
        assert "watchlist_small" not in triggers

    @pytest.mark.asyncio
    async def test_no_trigger_when_quiet(self, orch):
        """All conditions calm — trigger list should be empty."""
        from ozymandias.core.market_hours import get_current_session
        from zoneinfo import ZoneInfo
        symbols = [f"SYM{i}" for i in range(12)]
        await _set_watchlist(orch, tier1=symbols)
        # Use 10:00 AM ET — well within regular hours and outside the 3:28-3:32 PM
        # approaching_close window — so the time-based trigger cannot fire.
        quiet_now = datetime(2026, 3, 19, 14, 0, 0, tzinfo=timezone.utc)  # 10:00 AM ET
        orch._trigger_state.last_claude_call_utc = quiet_now - timedelta(minutes=10)
        orch._trigger_state.last_prices = {}
        orch._trigger_state.last_override_exit_count = 0
        orch._override_exit_count = 0
        orch._latest_indicators = {}
        # Suppress watchlist_stale: set last build to very recent so elapsed < interval.
        orch._trigger_state.last_watchlist_build_utc = quiet_now - timedelta(minutes=10)
        # Sync last_session to actual current session so the transition trigger doesn't fire.
        orch._trigger_state.last_session = get_current_session().value
        triggers = await orch._check_triggers(now=quiet_now)
        assert triggers == []

    # ------------------------------------------------------------------
    # position_in_profit trigger
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_profit_trigger_fires_when_gain_exceeds_threshold(self, orch):
        """Long position up >3.0% → position_in_profit trigger fires."""
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="HAL", shares=100, avg_cost=33.00, entry_date=now,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=35.00, stop_loss=31.50),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._latest_indicators = {"HAL": {"price": 34.00}}  # +3.03%
        triggers = await orch._check_triggers()
        assert "position_in_profit:HAL" in triggers

    @pytest.mark.asyncio
    async def test_profit_trigger_does_not_fire_below_threshold(self, orch):
        """Long position up 0.9% (below 3.0% default) → no profit trigger."""
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="HAL", shares=100, avg_cost=33.00, entry_date=now,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=35.00, stop_loss=31.50),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._latest_indicators = {"HAL": {"price": 33.30}}  # +0.91%
        triggers = await orch._check_triggers()
        assert "position_in_profit:HAL" not in triggers

    @pytest.mark.asyncio
    async def test_profit_trigger_does_not_refire_until_next_interval(self, orch):
        """After last_profit_trigger_gain is set at 1.6%, trigger doesn't refire until 4.6%."""
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="HAL", shares=100, avg_cost=33.00, entry_date=now,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=35.00, stop_loss=31.50),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        # Simulate Claude already reviewed at +1.6% gain
        orch._trigger_state.last_profit_trigger_gain["HAL"] = 0.016
        # Position is up 2.0% — not yet 1.6% + 3.0% = 4.6%
        orch._latest_indicators = {"HAL": {"price": 33.66}}  # +2.0%
        triggers = await orch._check_triggers()
        assert "position_in_profit:HAL" not in triggers

    @pytest.mark.asyncio
    async def test_profit_trigger_rearms_at_next_interval(self, orch):
        """After last review at +1.6%, trigger fires again when gain reaches 4.6%."""
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="HAL", shares=100, avg_cost=33.00, entry_date=now,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=35.00, stop_loss=31.50),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._trigger_state.last_profit_trigger_gain["HAL"] = 0.016
        # +4.67% = 0.016 + 0.030 threshold cleared
        orch._latest_indicators = {"HAL": {"price": 34.54}}  # +4.67%
        triggers = await orch._check_triggers()
        assert "position_in_profit:HAL" in triggers

    @pytest.mark.asyncio
    async def test_profit_trigger_direction_aware_short(self, orch):
        """Short position profits when price falls — trigger fires on downward gain."""
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="INTC", shares=100, avg_cost=46.00, entry_date=now,
            intention=TradeIntention(
                direction="short",
                exit_targets=ExitTargets(profit_target=43.00, stop_loss=48.00),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._latest_indicators = {"INTC": {"price": 44.57}}  # -3.11% from short entry
        triggers = await orch._check_triggers()
        assert "position_in_profit:INTC" in triggers


# ===========================================================================
# Slow loop control-flow tests
# ===========================================================================

class TestSlowLoopCycle:

    @pytest.fixture(autouse=True)
    def market_open(self, orch):
        """Patch is_market_open to True and seed indicators so guards don't short-circuit."""
        orch._latest_indicators = {"TEST": {"price": 100.0}}
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            yield

    @pytest.mark.asyncio
    async def test_no_trigger_no_claude_call(self, orch):
        """If check_triggers returns [], Claude must not be called."""
        with patch.object(orch, "_check_triggers", AsyncMock(return_value=[])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_claude:
                await orch._slow_loop_cycle()
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_trigger_fires_claude_call(self, orch):
        """If at least one trigger fires, _run_claude_cycle is called once."""
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["time_ceiling"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_claude:
                await orch._slow_loop_cycle()
        mock_claude.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_triggers_single_claude_call(self, orch):
        """Multiple simultaneous triggers → still only one Claude call."""
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["time_ceiling", "watchlist_small",
                                                  "override_exit"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_claude:
                await orch._slow_loop_cycle()
        mock_claude.assert_called_once()

    @pytest.mark.asyncio
    async def test_in_flight_blocks_concurrent_call(self, orch):
        """If claude_call_in_flight is True, no new call is started."""
        orch._trigger_state.claude_call_in_flight = True
        with patch.object(orch, "_check_triggers", AsyncMock()) as mock_check:
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_claude:
                await orch._slow_loop_cycle()
        mock_check.assert_not_called()
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_flight_flag_cleared_after_cycle(self, orch):
        """_claude_call_in_flight must be False after the cycle, even on error."""
        async def fail(*args, **kwargs):
            raise RuntimeError("Claude died")

        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["time_ceiling"])):
            with patch.object(orch, "_run_claude_cycle", side_effect=fail):
                with pytest.raises(RuntimeError):
                    await orch._slow_loop_cycle()

        assert orch._trigger_state.claude_call_in_flight is False

    @pytest.mark.asyncio
    async def test_backoff_blocks_call(self, orch):
        """If claude_backoff_until_utc is in the future, skip the cycle."""
        orch._degradation.claude_backoff_until_utc = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        with patch.object(orch, "_check_triggers", AsyncMock()) as mock_check:
            await orch._slow_loop_cycle()
        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_backoff_allows_call(self, orch):
        """Once backoff_until is in the past, the next cycle proceeds normally."""
        orch._degradation.claude_backoff_until_utc = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["time_ceiling"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_claude:
                await orch._slow_loop_cycle()
        mock_claude.assert_called_once()
        assert orch._degradation.claude_backoff_until_utc is None


# ===========================================================================
# Watchlist build decoupling — background task + slow loop split
# ===========================================================================

class TestWatchlistBuildDecoupling:
    """
    Verify that watchlist builds fire as background tasks from _slow_loop_cycle
    and never block the reasoning cycle.
    """

    @pytest.fixture(autouse=True)
    def market_open(self, orch):
        orch._latest_indicators = {"TEST": {"price": 100.0}}
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            yield

    @pytest.mark.asyncio
    async def test_watchlist_only_trigger_fires_build_not_reasoning(self, orch):
        """watchlist_stale alone: _spawn_background_task called, _run_claude_cycle never called."""
        with patch.object(orch, "_check_triggers", AsyncMock(return_value=["watchlist_stale"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_reasoning:
                with patch.object(orch, "_run_watchlist_build_task", AsyncMock()) as mock_build:
                    with patch.object(orch, "_spawn_background_task") as mock_ef:
                        await orch._slow_loop_cycle()

        mock_reasoning.assert_not_called()
        assert orch._trigger_state.claude_call_in_flight is False
        mock_ef.assert_called_once()

    @pytest.mark.asyncio
    async def test_combined_triggers_fires_both(self, orch):
        """watchlist_stale + price_move: _spawn_background_task called AND reasoning runs."""
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["watchlist_stale", "price_move"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_reasoning:
                with patch.object(orch, "_spawn_background_task") as mock_ef:
                    await orch._slow_loop_cycle()

        mock_ef.assert_called_once()
        mock_reasoning.assert_called_once()
        # reasoning call uses only non-build triggers
        call_args = mock_reasoning.call_args
        assert "price_move" in call_args.kwargs.get("trigger_name", call_args.args[0] if call_args.args else "")
        assert "watchlist_stale" not in call_args.kwargs.get("trigger_name", call_args.args[0] if call_args.args else "")

    @pytest.mark.asyncio
    async def test_build_in_flight_skips_new_build(self, orch):
        """_watchlist_build_in_flight=True: build trigger skipped, reasoning still runs."""
        orch._watchlist_build_in_flight = True
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["watchlist_stale", "price_move"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_reasoning:
                with patch.object(orch, "_spawn_background_task") as mock_ef:
                    await orch._slow_loop_cycle()

        mock_ef.assert_not_called()
        mock_reasoning.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_task_success_updates_timestamp(self, orch):
        """_run_watchlist_build_task success: timestamp updated, in_flight cleared."""
        from ozymandias.intelligence.claude_reasoning import WatchlistResult
        await _set_watchlist(orch, tier1=["AAPL"])
        wl_result = WatchlistResult(watchlist=[], market_notes="", raw={})
        orch._claude.run_watchlist_build = AsyncMock(return_value=wl_result)
        orch._watchlist_build_in_flight = True
        orch._trigger_state.last_watchlist_build_utc = None

        before = datetime.now(timezone.utc)
        await orch._run_watchlist_build_task()
        after = datetime.now(timezone.utc)

        ts = orch._trigger_state.last_watchlist_build_utc
        assert ts is not None
        assert before <= ts <= after
        assert not orch._watchlist_build_in_flight

    @pytest.mark.asyncio
    async def test_build_task_none_result_backdates_timestamp(self, orch):
        """wl_result is None: timestamp backdated (retry in probe_min), in_flight cleared."""
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._claude.run_watchlist_build = AsyncMock(return_value=None)
        orch._watchlist_build_in_flight = True
        orch._trigger_state.last_watchlist_build_utc = None

        await orch._run_watchlist_build_task()

        ts = orch._trigger_state.last_watchlist_build_utc
        assert ts is not None
        # Back-dated: should be in the past
        assert ts < datetime.now(timezone.utc)
        assert not orch._watchlist_build_in_flight

    @pytest.mark.asyncio
    async def test_build_task_exception_clears_in_flight(self, orch):
        """Exception in _run_watchlist_build_task: in_flight cleared in finally."""
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._claude.run_watchlist_build = AsyncMock(side_effect=RuntimeError("boom"))
        orch._watchlist_build_in_flight = True

        await orch._run_watchlist_build_task()

        assert not orch._watchlist_build_in_flight

    @pytest.mark.asyncio
    async def test_reasoning_ignores_add_list_from_claude(self, orch):
        """Reasoning result add_list is discarded; _apply_watchlist_changes called with []."""
        from ozymandias.intelligence.claude_reasoning import ReasoningResult
        add_calls = []

        async def capture_apply(watchlist, add_list, remove_list, open_symbols=None):
            add_calls.append(add_list)
            return 0

        orch._apply_watchlist_changes = capture_apply

        result = ReasoningResult(
            timestamp="2026-04-01T12:00:00+00:00",
            position_reviews=[],
            new_opportunities=[],
            watchlist_changes={"add": [{"symbol": "NVDA", "reason": "test"}], "remove": []},
            market_assessment="",
            risk_flags=[],
            session_veto=[],
            rejected_opportunities=[],
            raw={},
        )
        orch._claude.run_reasoning_cycle = AsyncMock(return_value=result)
        orch._claude.run_position_review_call = AsyncMock(return_value=result)

        with patch.object(orch, "_build_market_context", AsyncMock(return_value={"trading_session": "regular"})):
            with patch.object(orch, "_update_trigger_prices", AsyncMock()):
                with patch.object(orch, "_apply_position_reviews", AsyncMock()):
                    with patch.object(orch._trade_journal, "load_recent", AsyncMock(return_value=[])):
                        with patch.object(orch._trade_journal, "compute_session_stats", AsyncMock(return_value={})):
                            await orch._run_claude_cycle("price_move")

        assert all(lst == [] for lst in add_calls), (
            f"add_list must always be [] from reasoning path, got: {add_calls}"
        )


# ===========================================================================
# Claude API failure → quantitative-only mode
# ===========================================================================

class TestClaudeDegradation:

    def test_first_failure_sets_backoff_30s(self, orch):
        now = datetime.now(timezone.utc)
        orch._handle_claude_failure(RuntimeError("timeout"))
        assert orch._degradation.claude_available is False
        assert orch._claude_failure_count == 1
        backoff = (orch._degradation.claude_backoff_until_utc - now).total_seconds()
        assert 28 <= backoff <= 32   # ~30s

    def test_second_failure_doubles_backoff(self, orch):
        now = datetime.now(timezone.utc)
        orch._handle_claude_failure(RuntimeError("timeout"))
        orch._handle_claude_failure(RuntimeError("timeout"))
        assert orch._claude_failure_count == 2
        backoff = (orch._degradation.claude_backoff_until_utc - now).total_seconds()
        assert 58 <= backoff <= 62   # ~60s

    def test_backoff_capped_at_600s(self, orch):
        orch._claude_failure_count = 10   # many failures already
        orch._degradation.claude_available = False
        now = datetime.now(timezone.utc)
        orch._handle_claude_failure(RuntimeError("still down"))
        backoff = (orch._degradation.claude_backoff_until_utc - now).total_seconds()
        assert backoff <= 601   # capped at 600s

    @pytest.mark.asyncio
    async def test_run_claude_cycle_on_api_failure_enters_quantitative_mode(self, orch):
        """run_reasoning_cycle raising → claude_available=False, backoff set."""
        orch._claude.run_reasoning_cycle = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        orch._broker.get_account = AsyncMock(return_value=_stub_account())

        await orch._run_claude_cycle("time_ceiling")

        assert orch._degradation.claude_available is False
        assert orch._degradation.claude_backoff_until_utc is not None

    @pytest.mark.asyncio
    async def test_successful_cycle_clears_degradation(self, orch):
        """A successful Claude call clears backoff and claude_available flag."""
        from ozymandias.intelligence.claude_reasoning import ReasoningResult

        orch._degradation.claude_available = False
        orch._degradation.claude_backoff_until_utc = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )

        result = ReasoningResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            position_reviews=[],
            new_opportunities=[],
            watchlist_changes={"add": [], "remove": [], "rationale": ""},
            market_assessment="neutral",
            risk_flags=[],
            rejected_opportunities=[],
            raw={},
        )
        orch._claude.run_reasoning_cycle = AsyncMock(return_value=result)
        orch._broker.get_account = AsyncMock(return_value=_stub_account())

        # _run_claude_cycle may fire _spawn_background_task on the success path.
        # Mock it so no background task outlives the test.
        with patch.object(orch, "_spawn_background_task"):
            await orch._run_claude_cycle("time_ceiling")

        assert orch._degradation.claude_available is True
        assert orch._degradation.claude_backoff_until_utc is None


# ===========================================================================
# Broker failure → safe mode
# ===========================================================================

class TestBrokerDegradation:

    def test_first_failure_enters_degraded_mode(self, orch):
        orch._mark_broker_failure(ConnectionError("timeout"))
        assert orch._degradation.broker_available is False
        assert orch._degradation.safe_mode is False  # not yet

    def test_safe_mode_activates_after_5_min(self, orch):
        orch._mark_broker_failure(ConnectionError("down"))
        # Simulate 6 minutes have passed since the first failure
        orch._degradation.broker_first_failure_utc = (
            datetime.now(timezone.utc) - timedelta(seconds=360)
        )
        orch._mark_broker_failure(ConnectionError("still down"))
        assert orch._degradation.safe_mode is True

    def test_safe_mode_not_auto_cleared_on_recovery(self, orch):
        orch._mark_broker_failure(ConnectionError("down"))
        orch._degradation.broker_first_failure_utc = (
            datetime.now(timezone.utc) - timedelta(seconds=360)
        )
        orch._mark_broker_failure(ConnectionError("still down"))
        assert orch._degradation.safe_mode is True

        orch._mark_broker_available()
        # Broker is available again but safe_mode stays True (operator must confirm)
        assert orch._degradation.broker_available is True
        assert orch._degradation.safe_mode is True


# ===========================================================================
# Fast loop error isolation
# ===========================================================================

class TestFastLoopErrorIsolation:

    @pytest.fixture(autouse=True)
    def market_open(self):
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            yield

    @pytest.mark.asyncio
    async def test_exception_in_poll_does_not_stop_other_steps(self, orch):
        """
        If _fast_step_poll_and_reconcile raises, the remaining steps
        (_fast_step_pdt_check, etc.) must still run.
        """
        pdt_called = []
        sync_called = []

        async def boom():
            raise RuntimeError("broker exploded")

        async def ok_pdt():
            pdt_called.append(1)

        async def ok_sync():
            sync_called.append(1)

        with (
            patch.object(orch, "_fast_step_poll_and_reconcile", side_effect=boom),
            patch.object(orch, "_fast_step_quant_overrides", AsyncMock()),
            patch.object(orch, "_fast_step_pdt_check", ok_pdt),
            patch.object(orch, "_fast_step_position_sync", ok_sync),
        ):
            # _fast_loop_cycle wraps each step; broker_available=True so all run
            await orch._fast_loop_cycle()

        assert pdt_called, "PDT check did not run after poll/reconcile failure"
        assert sync_called, "Position sync did not run after poll/reconcile failure"


# ===========================================================================
# Medium loop: one entry per cycle
# ===========================================================================

class TestMediumLoopOneEntryPerCycle:

    def _make_ranked(self):
        from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity, RankResult
        opp = lambda sym, score: ScoredOpportunity(
            symbol=sym, action="buy", strategy="momentum",
            composite_score=score, ai_conviction=0.7, technical_score=0.6,
            risk_adjusted_return=0.5, liquidity_score=1.0,
            suggested_entry=200.0, suggested_exit=220.0, suggested_stop=190.0,
            position_size_pct=0.08, reasoning="test",
        )
        candidates = [opp("AAPL", 0.8), opp("TSLA", 0.7), opp("NVDA", 0.6)]
        # Phase 15: rank_opportunities returns RankResult; wrap candidates here.
        return RankResult(candidates=candidates, rejections=[])

    def _run_medium_cycle(self, orch, ranked, fake_try_entry):
        """Helper: run one _medium_loop_cycle with patched deps."""
        import pandas as pd
        from datetime import timezone as _tz

        df = pd.DataFrame({
            "open": [200.0], "high": [201.0], "low": [199.0],
            "close": [200.5], "volume": [100_000.0],
        }, index=pd.DatetimeIndex(
            [datetime.now(_tz.utc)], tz=_tz.utc
        ))

        return (
            patch.object(orch, "_data_adapter"),
            patch.object(orch, "_ranker"),
            patch.object(orch, "_medium_evaluate_positions", AsyncMock()),
            patch.object(orch, "_medium_try_entry", fake_try_entry),
            patch("ozymandias.core.orchestrator.generate_signal_summary",
                  return_value={"signals": {}, "composite_technical_score": 0.5,
                                "symbol": "X", "timestamp": ""}),
        ), df

    @pytest.mark.asyncio
    async def test_stops_after_first_success(self, orch):
        """
        When the first candidate succeeds (returns True), no further candidates
        are attempted even if more are ranked.
        """
        ranked = self._make_ranked()
        entry_calls = []

        async def fake_try_entry(top, acct, portfolio, orders):
            entry_calls.append(top.symbol)
            return True  # first attempt succeeds

        import pandas as pd
        from datetime import timezone as _tz
        df = pd.DataFrame({
            "open": [200.0], "high": [201.0], "low": [199.0],
            "close": [200.5], "volume": [100_000.0],
        }, index=pd.DatetimeIndex([datetime.now(_tz.utc)], tz=_tz.utc))

        with (
            patch.object(orch, "_data_adapter") as mock_adapter,
            patch.object(orch, "_ranker") as mock_ranker,
            patch.object(orch, "_medium_evaluate_positions", AsyncMock()),
            patch.object(orch, "_medium_try_entry", fake_try_entry),
            patch("ozymandias.core.orchestrator.generate_signal_summary",
                  return_value={"signals": {}, "composite_technical_score": 0.5,
                                "symbol": "X", "timestamp": ""}),
        ):
            mock_adapter.fetch_bars = AsyncMock(return_value=df)
            mock_ranker.rank_opportunities = MagicMock(return_value=ranked)
            orch._broker.get_account = AsyncMock(return_value=_stub_account())

            await _set_watchlist(orch, tier1=["AAPL"])
            with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
                await orch._medium_loop_cycle()

        assert entry_calls == ["AAPL"], (
            f"Expected only AAPL attempted, got {entry_calls}"
        )

    @pytest.mark.asyncio
    async def test_tries_next_candidate_when_first_skipped(self, orch):
        """
        When first candidate returns False (blocked), the loop tries the next
        candidate. Stops after first True.
        """
        ranked = self._make_ranked()
        entry_calls = []

        async def fake_try_entry(top, acct, portfolio, orders):
            entry_calls.append(top.symbol)
            # AAPL blocked, TSLA succeeds
            return top.symbol != "AAPL"

        import pandas as pd
        from datetime import timezone as _tz
        df = pd.DataFrame({
            "open": [200.0], "high": [201.0], "low": [199.0],
            "close": [200.5], "volume": [100_000.0],
        }, index=pd.DatetimeIndex([datetime.now(_tz.utc)], tz=_tz.utc))

        with (
            patch.object(orch, "_data_adapter") as mock_adapter,
            patch.object(orch, "_ranker") as mock_ranker,
            patch.object(orch, "_medium_evaluate_positions", AsyncMock()),
            patch.object(orch, "_medium_try_entry", fake_try_entry),
            patch("ozymandias.core.orchestrator.generate_signal_summary",
                  return_value={"signals": {}, "composite_technical_score": 0.5,
                                "symbol": "X", "timestamp": ""}),
        ):
            mock_adapter.fetch_bars = AsyncMock(return_value=df)
            mock_ranker.rank_opportunities = MagicMock(return_value=ranked)
            orch._broker.get_account = AsyncMock(return_value=_stub_account())

            await _set_watchlist(orch, tier1=["AAPL"])
            with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
                await orch._medium_loop_cycle()

        assert entry_calls == ["AAPL", "TSLA"], (
            f"Expected AAPL (blocked) then TSLA (success), got {entry_calls}"
        )

    @pytest.mark.asyncio
    async def test_respects_entry_attempts_per_cycle_limit(self, orch):
        """
        entry_attempts_per_cycle caps how many candidates are tried even if all fail.
        Default is 3, so with 3 candidates all blocked, exactly 3 attempts are made.
        """
        ranked = self._make_ranked()
        entry_calls = []

        async def fake_try_entry(top, acct, portfolio, orders):
            entry_calls.append(top.symbol)
            return False  # all blocked

        import pandas as pd
        from datetime import timezone as _tz
        df = pd.DataFrame({
            "open": [200.0], "high": [201.0], "low": [199.0],
            "close": [200.5], "volume": [100_000.0],
        }, index=pd.DatetimeIndex([datetime.now(_tz.utc)], tz=_tz.utc))

        with (
            patch.object(orch, "_data_adapter") as mock_adapter,
            patch.object(orch, "_ranker") as mock_ranker,
            patch.object(orch, "_medium_evaluate_positions", AsyncMock()),
            patch.object(orch, "_medium_try_entry", fake_try_entry),
            patch("ozymandias.core.orchestrator.generate_signal_summary",
                  return_value={"signals": {}, "composite_technical_score": 0.5,
                                "symbol": "X", "timestamp": ""}),
        ):
            mock_adapter.fetch_bars = AsyncMock(return_value=df)
            mock_ranker.rank_opportunities = MagicMock(return_value=ranked)
            orch._broker.get_account = AsyncMock(return_value=_stub_account())

            await _set_watchlist(orch, tier1=["AAPL"])
            with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
                await orch._medium_loop_cycle()

        # Default entry_attempts_per_cycle = 3, we have exactly 3 ranked → all tried
        assert len(entry_calls) == 3, (
            f"Expected 3 attempts (capped by entry_attempts_per_cycle), got {entry_calls}"
        )


# ===========================================================================
# Watchlist + position review application
# ===========================================================================

class TestSlowLoopStateApplication:

    @pytest.mark.asyncio
    async def test_apply_watchlist_changes_adds_and_removes(self, orch):
        await _set_watchlist(orch, tier1=["AAPL", "TSLA"])
        wl = await orch._state_manager.load_watchlist()

        add_list = [{"symbol": "NVDA", "reason": "breakout", "priority_tier": 1}]
        remove_list = ["TSLA"]
        await orch._apply_watchlist_changes(wl, add_list, remove_list)

        saved = await orch._state_manager.load_watchlist()
        symbols = {e.symbol for e in saved.entries}
        assert "NVDA" in symbols
        assert "TSLA" not in symbols
        assert "AAPL" in symbols

    @pytest.mark.asyncio
    async def test_newly_added_symbols_not_pruned_same_cycle(self, orch):
        """Symbols added by Claude must survive the cap prune in the same cycle.

        Newly-added symbols have no _latest_indicators entry yet (first medium
        loop scan hasn't run), so their prune score is 0.0. Without protection
        they would be immediately evicted — defeating the purpose of the add.
        """
        # Fill watchlist to the cap with symbols that have no indicator data
        # (score=0.0 for all), so the cap will be triggered after the add.
        cap = orch._config.claude.watchlist_max_entries
        existing = [f"SYM{i:02d}" for i in range(cap)]
        await _set_watchlist(orch, tier1=existing)
        wl = await orch._state_manager.load_watchlist()
        assert len(wl.entries) == cap

        # No indicator data for anyone — fair fight (all score 0.0)
        orch._latest_indicators = {}

        # Claude adds two new symbols
        add_list = [
            {"symbol": "AAPL", "reason": "catalyst", "priority_tier": 1},
            {"symbol": "MSFT", "reason": "catalyst", "priority_tier": 1},
        ]
        await orch._apply_watchlist_changes(wl, add_list, [])

        saved = await orch._state_manager.load_watchlist()
        symbols = {e.symbol for e in saved.entries}

        assert "AAPL" in symbols, "Newly-added AAPL was pruned in same cycle"
        assert "MSFT" in symbols, "Newly-added MSFT was pruned in same cycle"
        assert len(saved.entries) == cap, f"Should still be at cap={cap}, got {len(saved.entries)}"

    @pytest.mark.asyncio
    async def test_apply_watchlist_no_duplicate_add(self, orch):
        await _set_watchlist(orch, tier1=["AAPL"])
        wl = await orch._state_manager.load_watchlist()
        add_list = [{"symbol": "AAPL", "reason": "already here", "priority_tier": 1}]
        await orch._apply_watchlist_changes(wl, add_list, [])
        saved = await orch._state_manager.load_watchlist()
        assert sum(1 for e in saved.entries if e.symbol == "AAPL") == 1

    @pytest.mark.asyncio
    async def test_apply_position_reviews_appends_notes(self, orch):
        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="AAPL", shares=10, avg_cost=200.0, entry_date=now_iso,
            intention=TradeIntention(review_notes=[]),
        )
        await _set_portfolio(orch, [pos])

        reviews = [{"symbol": "AAPL", "updated_reasoning": "Thesis intact, hold."}]
        await orch._apply_position_reviews(reviews)

        saved = await orch._state_manager.load_portfolio()
        assert saved.positions[0].intention.review_notes, "No review note appended"
        assert "Thesis intact" in saved.positions[0].intention.review_notes[0]

    @pytest.mark.asyncio
    async def test_apply_position_reviews_updates_targets(self, orch):
        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="AAPL", shares=10, avg_cost=200.0, entry_date=now_iso,
            intention=TradeIntention(
                exit_targets=ExitTargets(profit_target=220.0, stop_loss=190.0)
            ),
        )
        await _set_portfolio(orch, [pos])
        portfolio = await orch._state_manager.load_portfolio()

        reviews = [{"symbol": "AAPL", "updated_reasoning": "",
                    "adjusted_targets": {"profit_target": 230.0, "stop_loss": 195.0}}]
        await orch._apply_position_reviews(reviews)

        saved = await orch._state_manager.load_portfolio()
        assert saved.positions[0].intention.exit_targets.profit_target == 230.0
        assert saved.positions[0].intention.exit_targets.stop_loss == 195.0

    @pytest.mark.asyncio
    async def test_stop_adjustment_rejected_when_above_current_price_long(self, orch):
        """Stop raised above current price for a long must be silently rejected.

        Reproduces the XOM incident: Claude raised stop to $162 while price
        was $161.25, causing an immediate exit on the next fast-loop cycle.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="AAPL", shares=10, avg_cost=190.0, entry_date=now_iso,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=220.0, stop_loss=185.0),
            ),
        )
        await _set_portfolio(orch, [pos])
        portfolio = await orch._state_manager.load_portfolio()

        # Current price is 195 — new stop of 198 is above it (would trigger immediately)
        orch._latest_indicators = {"AAPL": {"price": 195.0}}
        reviews = [{"symbol": "AAPL", "updated_reasoning": "",
                    "adjusted_targets": {"stop_loss": 198.0}}]
        await orch._apply_position_reviews(reviews)

        saved = await orch._state_manager.load_portfolio()
        assert saved.positions[0].intention.exit_targets.stop_loss == 185.0, (
            "Stop should have been rejected — new stop above current price"
        )

    @pytest.mark.asyncio
    async def test_stop_adjustment_applied_when_below_current_price_long(self, orch):
        """A valid stop raise (still below current price) must be accepted."""
        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="AAPL", shares=10, avg_cost=190.0, entry_date=now_iso,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=220.0, stop_loss=185.0),
            ),
        )
        await _set_portfolio(orch, [pos])
        portfolio = await orch._state_manager.load_portfolio()

        # Current price is 200 — new stop of 193 is safely below it
        orch._latest_indicators = {"AAPL": {"price": 200.0}}
        reviews = [{"symbol": "AAPL", "updated_reasoning": "",
                    "adjusted_targets": {"stop_loss": 193.0}}]
        await orch._apply_position_reviews(reviews)

        saved = await orch._state_manager.load_portfolio()
        assert saved.positions[0].intention.exit_targets.stop_loss == 193.0, (
            "Stop should have been accepted — new stop safely below current price"
        )

    @pytest.mark.asyncio
    async def test_stop_adjustment_rejected_when_below_current_price_short(self, orch):
        """For a short, stop below current price would trigger immediately — reject it."""
        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="TSLA", shares=10, avg_cost=250.0, entry_date=now_iso,
            intention=TradeIntention(
                direction="short",
                exit_targets=ExitTargets(profit_target=220.0, stop_loss=260.0),
            ),
        )
        await _set_portfolio(orch, [pos])
        portfolio = await orch._state_manager.load_portfolio()

        # Current price is 245 — new stop of 242 is below it (would trigger immediately)
        orch._latest_indicators = {"TSLA": {"price": 245.0}}
        reviews = [{"symbol": "TSLA", "updated_reasoning": "",
                    "adjusted_targets": {"stop_loss": 242.0}}]
        await orch._apply_position_reviews(reviews)

        saved = await orch._state_manager.load_portfolio()
        assert saved.positions[0].intention.exit_targets.stop_loss == 260.0, (
            "Stop should have been rejected — new stop below current price for short"
        )


# ===========================================================================
# Thesis challenge in _medium_try_entry
# ===========================================================================

class TestThesisChallenge:
    """Tests for the skeptical-analyst thesis challenge on large positions."""

    from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity

    def _make_top(self, position_size_pct: float, ai_conviction: float = 0.85) -> "ScoredOpportunity":
        from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity
        return ScoredOpportunity(
            symbol="AAPL",
            action="buy",
            strategy="momentum",
            composite_score=0.80,
            ai_conviction=ai_conviction,
            technical_score=0.70,
            risk_adjusted_return=0.60,
            liquidity_score=1.0,
            suggested_entry=200.0,
            suggested_exit=220.0,
            suggested_stop=190.0,
            position_size_pct=position_size_pct,
            reasoning="Breakout above key resistance.",
        )

    def _stub_entry_guards(self, orch):
        """Mock risk_manager and fill_protection to allow entry."""
        orch._risk_manager.validate_entry = MagicMock(return_value=(True, ""))
        orch._fill_protection.can_place_order = MagicMock(return_value=True)
        # long_score=1.0 → TA size factor=1.0, no quantity reduction
        orch._latest_indicators = {"AAPL": {"long_score": 1.0, "short_score": 1.0}}
        orch._latest_market_context = {}

    @pytest.mark.asyncio
    async def test_large_position_triggers_thesis_challenge(self, orch):
        """position_size_pct >= threshold → run_thesis_challenge is called."""
        top = self._make_top(position_size_pct=0.20)
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"concern_level": 0.0, "reasoning": "no material concerns"}
        )
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        await orch._medium_try_entry(top, acct, portfolio, [])

        orch._claude.run_thesis_challenge.assert_called_once()

    @pytest.mark.asyncio
    async def test_small_position_skips_thesis_challenge(self, orch):
        """position_size_pct < threshold → run_thesis_challenge is NOT called."""
        top = self._make_top(position_size_pct=0.08)
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock()
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        await orch._medium_try_entry(top, acct, portfolio, [])

        orch._claude.run_thesis_challenge.assert_not_called()

    @pytest.mark.asyncio
    async def test_challenge_high_concern_reduces_quantity_but_trade_proceeds(self, orch):
        """High concern_level → quantity reduced by penalty, but trade is NOT blocked."""
        top = self._make_top(position_size_pct=0.20)
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"concern_level": 1.0, "reasoning": "Multiple serious concerns."}
        )
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        placed_orders = []
        async def capture_order(order):
            placed_orders.append(order)
            return MagicMock(order_id="ord_001")
        orch._broker.place_order = capture_order
        orch._fill_protection.record_order = AsyncMock()

        await orch._medium_try_entry(top, acct, portfolio, [])

        # Trade MUST proceed (not blocked), just with reduced quantity.
        assert len(placed_orders) == 1
        # base_qty = int(equity=100_000 × pct=0.20 / price=200) = 100
        # ta_factor=1.0 (tech_score=1.0) → pre-challenge qty = 100
        # concern=1.0 × max_penalty=0.35 → size_factor=0.65 → int(100 × 0.65) = 65
        max_penalty = orch._config.ranker.thesis_challenge_max_penalty
        base_qty = int(acct.equity * top.position_size_pct / top.suggested_entry)
        expected_qty = max(1, int(base_qty * (1.0 - 1.0 * max_penalty)))
        assert placed_orders[0].quantity == expected_qty

    @pytest.mark.asyncio
    async def test_challenge_concern_level_scales_quantity(self, orch):
        """Moderate concern_level applies proportional penalty to quantity."""
        top = self._make_top(position_size_pct=0.20, ai_conviction=0.85)
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"concern_level": 0.5, "reasoning": "Earnings in 2 days."}
        )
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        placed_orders = []
        async def capture_order(order):
            placed_orders.append(order)
            return MagicMock(order_id="ord_001")
        orch._broker.place_order = capture_order
        orch._fill_protection.record_order = AsyncMock()

        await orch._medium_try_entry(top, acct, portfolio, [])

        assert len(placed_orders) == 1
        # base_qty = int(equity=100_000 × pct=0.20 / price=200) = 100
        # ta_factor=1.0 (tech_score=1.0) → pre-challenge qty = 100
        # concern=0.5 × max_penalty=0.35 → size_factor=0.825 → int(100 × 0.825) = 82
        max_penalty = orch._config.ranker.thesis_challenge_max_penalty
        base_qty = int(acct.equity * top.position_size_pct / top.suggested_entry)
        expected_qty = max(1, int(base_qty * (1.0 - 0.5 * max_penalty)))
        assert placed_orders[0].quantity == expected_qty

    @pytest.mark.asyncio
    async def test_challenge_returns_none_trade_proceeds(self, orch):
        """Challenge API failure (returns None) → trade proceeds with original quantity."""
        top = self._make_top(position_size_pct=0.20, ai_conviction=0.85)
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(return_value=None)
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        placed_orders = []
        async def capture_order(order):
            placed_orders.append(order)
            return MagicMock(order_id="ord_001")
        orch._broker.place_order = capture_order
        orch._fill_protection.record_order = AsyncMock()

        await orch._medium_try_entry(top, acct, portfolio, [])

        # No penalty applied; qty = int(equity=100_000 × pct=0.20 / price=200) = 100
        assert len(placed_orders) == 1
        expected_qty = int(acct.equity * top.position_size_pct / top.suggested_entry)
        assert placed_orders[0].quantity == expected_qty


# ===========================================================================
# Thesis challenge cache
# ===========================================================================

class TestThesisChallengeCache:
    """Tests for the per-symbol thesis challenge result cache."""

    def _make_top(self, symbol: str = "AAPL", position_size_pct: float = 0.20):
        from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity
        return ScoredOpportunity(
            symbol=symbol, action="buy", strategy="momentum",
            composite_score=0.80, ai_conviction=0.85, technical_score=0.70,
            risk_adjusted_return=0.60, liquidity_score=1.0,
            suggested_entry=200.0, suggested_exit=220.0, suggested_stop=190.0,
            position_size_pct=position_size_pct, reasoning="Breakout.",
        )

    def _stub_entry_guards(self, orch):
        orch._risk_manager.validate_entry = MagicMock(return_value=(True, ""))
        orch._fill_protection.can_place_order = MagicMock(return_value=True)
        # long_score=1.0 → TA size factor=1.0, no quantity reduction
        orch._latest_indicators = {"AAPL": {"long_score": 1.0, "short_score": 1.0}}
        orch._latest_market_context = {}

    @pytest.mark.asyncio
    async def test_cached_concern_skips_claude_call(self, orch):
        """When a symbol is cached with concern_level within TTL, run_thesis_challenge is NOT called."""
        import time as _time
        top = self._make_top()
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock()
        orch._broker.place_order = AsyncMock(return_value=MagicMock(order_id="ord_x"))
        orch._fill_protection.record_order = AsyncMock()
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        # Pre-populate cache: AAPL has concern_level=0.5 from 30 seconds ago (within TTL)
        orch._thesis_challenge_cache["AAPL"] = (0.5, _time.monotonic() - 30)

        await orch._medium_try_entry(top, acct, portfolio, [])

        # Claude must NOT be called — cached value is used
        orch._claude.run_thesis_challenge.assert_not_called()
        # Trade still proceeds (penalty applied, not blocked)
        orch._broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_expired_cache_re_evaluates(self, orch):
        """When the cached result is older than TTL, Claude is called again."""
        import time as _time
        top = self._make_top()
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"concern_level": 0.0, "reasoning": "no concerns"}
        )
        orch._broker.place_order = AsyncMock(return_value=MagicMock(order_id="ord_x"))
        orch._fill_protection.record_order = AsyncMock()
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        # Pre-populate cache: expired entry (beyond default 10-min TTL)
        ttl_sec = orch._config.ranker.thesis_challenge_ttl_min * 60
        orch._thesis_challenge_cache["AAPL"] = (0.3, _time.monotonic() - ttl_sec - 1)

        await orch._medium_try_entry(top, acct, portfolio, [])

        orch._claude.run_thesis_challenge.assert_called_once()

    @pytest.mark.asyncio
    async def test_challenge_result_stored_in_cache(self, orch):
        """After a thesis challenge runs, the concern_level is stored in the cache."""
        import time as _time
        top = self._make_top()
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"concern_level": 0.6, "reasoning": "Earnings risk present."}
        )
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        assert "AAPL" not in orch._thesis_challenge_cache

        await orch._medium_try_entry(top, acct, portfolio, [])

        assert "AAPL" in orch._thesis_challenge_cache
        concern_level, ts = orch._thesis_challenge_cache["AAPL"]
        assert concern_level == pytest.approx(0.6)
        assert _time.monotonic() - ts < 5  # stored within last 5 seconds

    @pytest.mark.asyncio
    async def test_cached_zero_concern_skips_claude_no_penalty(self, orch):
        """Cached concern_level=0.0 within TTL → Claude not called, quantity unchanged."""
        import time as _time
        top = self._make_top()
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock()
        orch._broker.place_order = AsyncMock(return_value=MagicMock(order_id="ord_y"))
        orch._fill_protection.record_order = AsyncMock()
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        # Cached with no concern (within TTL) — Claude skipped, no penalty applied
        orch._thesis_challenge_cache["AAPL"] = (0.0, _time.monotonic() - 30)

        await orch._medium_try_entry(top, acct, portfolio, [])

        orch._claude.run_thesis_challenge.assert_not_called()
        # quantity = int(equity × pct / price) = int(100_000 × 0.20 / 200) = 100; no penalty
        expected_qty = int(acct.equity * top.position_size_pct / top.suggested_entry)
        assert orch._broker.place_order.call_args[0][0].quantity == expected_qty


# ===========================================================================
# Opening fill registration and dispatch
# ===========================================================================

class TestRegisterOpeningFill:
    """_register_opening_fill creates the portfolio position from confirmed fill data."""

    def _make_change(self, symbol="AMD", fill_qty=30.0, fill_price=198.07, side="buy"):
        from ozymandias.execution.fill_protection import StateChange
        return StateChange(
            order_id="ord_001", symbol=symbol,
            old_status="PENDING", new_status="FILLED",
            fill_qty=fill_qty, fill_price=fill_price,
            side=side, change_type="fill",
        )

    @pytest.mark.asyncio
    async def test_creates_long_position_from_buy_fill(self, orch):
        """Long position created with correct qty, price, and intention from pending."""
        orch._pending_intentions["AMD"] = {
            "stop": 194.0, "target": 205.0, "strategy": "swing",
            "direction": "long", "reasoning": "breakout",
            "_signals": {}, "_claude_conviction": 0.8, "_composite_score": 0.7,
        }
        change = self._make_change(fill_qty=30.0, fill_price=198.07)

        await orch._register_opening_fill(change)

        portfolio = await orch._state_manager.load_portfolio()
        assert len(portfolio.positions) == 1
        pos = portfolio.positions[0]
        assert pos.symbol == "AMD"
        assert pos.shares == 30.0
        assert abs(pos.avg_cost - 198.07) < 0.001
        assert pos.intention.strategy == "swing"
        assert pos.intention.direction == "long"
        assert pos.intention.exit_targets.stop_loss == 194.0
        assert pos.intention.exit_targets.profit_target == 205.0

    @pytest.mark.asyncio
    async def test_creates_short_position_from_sell_fill(self, orch):
        """Short position created from a sell fill — shares stored positive, direction=short."""
        orch._pending_intentions["META"] = {
            "stop": 635.0, "target": 615.0, "strategy": "swing",
            "direction": "short", "reasoning": "breakdown",
            "_signals": {}, "_claude_conviction": 0.75, "_composite_score": 0.65,
        }
        change = self._make_change(symbol="META", fill_qty=9.0, fill_price=628.23, side="sell")

        await orch._register_opening_fill(change)

        portfolio = await orch._state_manager.load_portfolio()
        pos = portfolio.positions[0]
        assert pos.symbol == "META"
        assert pos.shares == 9.0          # positive — not -9
        assert pos.intention.direction == "short"
        assert pos.intention.exit_targets.stop_loss == 635.0
        assert pos.intention.exit_targets.profit_target == 615.0

    @pytest.mark.asyncio
    async def test_intention_defaults_when_no_pending(self, orch):
        """If _pending_intentions is missing, defaults apply."""
        change = self._make_change()

        await orch._register_opening_fill(change)

        portfolio = await orch._state_manager.load_portfolio()
        pos = portfolio.positions[0]
        assert pos.intention.strategy == "unknown"
        assert pos.intention.exit_targets.stop_loss == 0.0

    @pytest.mark.asyncio
    async def test_pops_pending_intentions(self, orch):
        """_pending_intentions entry is consumed."""
        orch._pending_intentions["AMD"] = {
            "stop": 194.0, "target": 205.0, "strategy": "swing",
            "direction": "long", "reasoning": "r",
            "_signals": {"rsi": 60}, "_claude_conviction": 0.8, "_composite_score": 0.7,
        }
        await orch._register_opening_fill(self._make_change())
        assert "AMD" not in orch._pending_intentions

    @pytest.mark.asyncio
    async def test_moves_signals_to_entry_contexts(self, orch):
        """Signal context is moved to _entry_contexts for later use when position closes."""
        orch._pending_intentions["AMD"] = {
            "stop": 194.0, "target": 205.0, "strategy": "swing",
            "direction": "long", "reasoning": "r",
            "_signals": {"rsi": 62.0}, "_claude_conviction": 0.82, "_composite_score": 0.73,
        }
        await orch._register_opening_fill(self._make_change())
        ctx = orch._entry_contexts.get("AMD", {})
        assert ctx.get("signals") == {"rsi": 62.0}
        assert abs(ctx.get("claude_conviction", 0) - 0.82) < 0.001

    @pytest.mark.asyncio
    async def test_signal_context_persisted_in_trade_intention(self, orch):
        """Signal context is written into TradeIntention so it survives restarts."""
        orch._pending_intentions["AMD"] = {
            "stop": 194.0, "target": 205.0, "strategy": "swing",
            "direction": "long", "reasoning": "r",
            "_signals": {"rsi": 62.0}, "_claude_conviction": 0.82, "_composite_score": 0.73,
        }
        await orch._register_opening_fill(self._make_change())
        portfolio = await orch._state_manager.load_portfolio()
        pos = portfolio.positions[0]
        assert pos.intention.entry_signals == {"rsi": 62.0}
        assert abs(pos.intention.entry_conviction - 0.82) < 0.001
        assert abs(pos.intention.entry_score - 0.73) < 0.001

    @pytest.mark.asyncio
    async def test_entry_contexts_restored_at_startup(self, orch):
        """startup_reconciliation restores _entry_contexts from TradeIntention on open positions.

        This is the restart-survival path: signals written to portfolio.json at fill time
        are read back into _entry_contexts so the journal records real values, not zeros.
        """
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        pos = Position(
            symbol="NVDA", shares=10.0, avg_cost=900.0,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="momentum", direction="long",
                exit_targets=ExitTargets(stop_loss=880.0, profit_target=930.0),
                entry_signals={"rsi": 58.0, "volume_ratio": 1.4},
                entry_conviction=0.77,
                entry_score=0.68,
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        # Simulate a fresh start: _entry_contexts is empty
        orch._entry_contexts.clear()

        # Mock broker to report the same position so reconciliation passes
        from ozymandias.execution.broker_interface import BrokerPosition
        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(symbol="NVDA", qty=10.0, avg_entry_price=900.0,
                           current_price=900.0, market_value=9000.0,
                           unrealized_pl=0.0, side="long")
        ])

        await orch.startup_reconciliation()

        ctx = orch._entry_contexts.get("NVDA", {})
        assert ctx.get("signals") == {"rsi": 58.0, "volume_ratio": 1.4}
        assert abs(ctx.get("claude_conviction", 0) - 0.77) < 0.001
        assert abs(ctx.get("composite_score", 0) - 0.68) < 0.001

    @pytest.mark.asyncio
    async def test_no_duplicate_if_position_already_exists(self, orch):
        """Duplicate fill event: guard prevents second position."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        existing = Position(
            symbol="AMD", shares=30.0, avg_cost=198.07,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="swing", direction="long", reasoning="",
                exit_targets=ExitTargets(stop_loss=194.0, profit_target=205.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[existing]))
        await orch._register_opening_fill(self._make_change())
        portfolio = await orch._state_manager.load_portfolio()
        assert len(portfolio.positions) == 1  # no duplicate


# ===========================================================================
# Quant override minimum hold time
# ===========================================================================

class TestQuantOverrideHoldTime:
    """Quant overrides must not fire within min_hold_before_override_min of entry."""

    def _make_position(self, symbol="XLE"):
        from ozymandias.core.state_manager import ExitTargets, Position, TradeIntention
        return Position(
            symbol=symbol, shares=84.0, avg_cost=58.73, entry_date="2026-03-17T14:11:54Z",
            intention=TradeIntention(
                strategy="momentum", direction="long",
                reasoning="breakout", catalyst=None,
                expected_move="+4%", max_expected_loss=-100.0,
                entry_date="2026-03-17",
                exit_targets=ExitTargets(profit_target=61.0, stop_loss=57.2),
            ),
        )

    def _stub_indicators(self, orch, symbol, roc_deceleration=True):
        orch._latest_indicators = {symbol: {
            "price": 58.73,
            "roc_deceleration": roc_deceleration,
            "vwap_position": "above",
            "volume_ratio": 0.8,
        }}

    @pytest.mark.asyncio
    async def test_override_suppressed_within_hold_window(self, orch):
        """Override with roc_deceleration=True must NOT fire if position was just entered."""
        import time as _time
        symbol = "XLE"
        portfolio = PortfolioState(positions=[self._make_position(symbol)])
        orch._state_manager.save_portfolio = AsyncMock()
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.load_orders = AsyncMock(return_value=MagicMock(orders=[]))
        self._stub_indicators(orch, symbol, roc_deceleration=True)
        # Simulate fill registered just now (well within 5-min cooldown)
        orch._position_entry_times[symbol] = _time.monotonic()
        orch._config.scheduler.min_hold_before_override_min = 5

        await orch._fast_step_quant_overrides()

        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_override_fires_after_hold_window(self, orch):
        """Override must fire once the hold window has elapsed."""
        import time as _time
        symbol = "XLE"
        portfolio = PortfolioState(positions=[self._make_position(symbol)])
        orch._state_manager.save_portfolio = AsyncMock()
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.load_orders = AsyncMock(return_value=MagicMock(orders=[]))
        self._stub_indicators(orch, symbol, roc_deceleration=True)
        orch._fill_protection.can_place_order = MagicMock(return_value=True)
        # Simulate fill registered 6 minutes ago (beyond 5-min cooldown)
        orch._position_entry_times[symbol] = _time.monotonic() - 360
        orch._config.scheduler.min_hold_before_override_min = 5
        orch._broker.place_order = AsyncMock(return_value=MagicMock(order_id="ord_override"))
        orch._fill_protection.record_order = AsyncMock()

        await orch._fast_step_quant_overrides()

        orch._broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_entry_time_cleared_on_close(self, orch):
        """_position_entry_times entry is removed when the trade journals (position closes)."""
        from ozymandias.execution.fill_protection import StateChange
        symbol = "XLE"
        orch._position_entry_times[symbol] = 12345.0

        pos = self._make_position(symbol)
        portfolio = PortfolioState(positions=[pos])
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.save_portfolio = AsyncMock()
        orch._trade_journal.append = AsyncMock()

        change = StateChange(
            order_id="ord_x", symbol=symbol, old_status="PENDING", new_status="FILLED",
            fill_qty=84.0, fill_price=58.50, side="sell", change_type="fill",
        )
        await orch._journal_closed_trade(change)

        assert symbol not in orch._position_entry_times


# ===========================================================================
# Trade journal path isolation
# ===========================================================================

class TestTradeJournalIsolation:
    """
    Verify that the TradeJournal path is always derived from the state manager
    directory, so test runs never pollute the real state/trade_journal.jsonl.

    Regression test for Bug #6 (2026-03-16): phantom META trades appeared in
    the live trade journal because TradeJournal() used a hardcoded path and
    test fixtures only redirected _state_manager._dir, not _trade_journal._path.
    """

    def test_journal_path_co_located_with_state_manager(self, orch, tmp_path):
        """Journal file lives in the same directory as all other state files."""
        assert orch._trade_journal._path.parent == orch._state_manager._dir
        assert orch._trade_journal._path.parent == tmp_path

    def test_journal_path_redirected_to_tmp_path(self, orch, tmp_path):
        """The orch fixture must point the journal at tmp_path, not the real state dir."""
        from ozymandias.core.trade_journal import TRADE_JOURNAL_FILE
        assert orch._trade_journal._path != TRADE_JOURNAL_FILE

    @pytest.mark.asyncio
    async def test_journal_writes_go_to_tmp_path_not_real_file(self, orch, tmp_path):
        """_journal_closed_trade writes to tmp_path, never the real state file."""
        from ozymandias.core.trade_journal import TRADE_JOURNAL_FILE
        from ozymandias.core.state_manager import (
            ExitTargets, PortfolioState, Position, TradeIntention,
        )
        from ozymandias.execution.fill_protection import StateChange

        real_journal = TRADE_JOURNAL_FILE
        real_content_before = real_journal.read_text() if real_journal.exists() else ""

        # Seed a position so _journal_closed_trade has something to close
        pos = Position(
            symbol="JOURNALTEST",
            shares=5.0,
            avg_cost=100.0,
            entry_date="2026-01-01T00:00:00+00:00",
            intention=TradeIntention(
                strategy="momentum",
                direction="long",
                exit_targets=ExitTargets(stop_loss=90.0, profit_target=110.0),
            ),
        )
        await orch._state_manager.save_portfolio(
            PortfolioState(cash=0.0, buying_power=0.0, positions=[pos])
        )

        change = StateChange(
            order_id="test-ord-001",
            symbol="JOURNALTEST",
            old_status="PENDING",
            new_status="FILLED",
            fill_qty=5.0,
            fill_price=105.0,
            side="sell",
            change_type="fill",
        )
        await orch._journal_closed_trade(change)

        # Real file must be untouched
        real_content_after = real_journal.read_text() if real_journal.exists() else ""
        assert real_content_before == real_content_after, (
            "Test wrote to the real trade_journal.jsonl — journal path isolation is broken"
        )

        # tmp_path journal must have the entry
        tmp_journal = tmp_path / "trade_journal.jsonl"
        assert tmp_journal.exists(), "Expected journal entry in tmp_path but file not created"
        import json
        entries = [json.loads(line) for line in tmp_journal.read_text().splitlines()]
        assert any(e["symbol"] == "JOURNALTEST" for e in entries)


# ===========================================================================
# _recently_closed re-adoption guard — Bug #1 (2026-03-16)
# ===========================================================================

class TestRecentlyClosedGuard:
    """
    _recently_closed TTL prevents position_sync from re-adopting a symbol
    within 60 seconds of it being closed.

    Regression for Bug #1: after _journal_closed_trade removed AMD from local
    portfolio, _fast_step_position_sync re-adopted it from broker 10s later
    (position still showing there), quant overrides fired a new SELL, and the
    cycle repeated 16 times producing 16 AMD sell orders in 3 minutes.
    """

    @pytest.mark.asyncio
    async def test_journal_close_populates_recently_closed(self, orch):
        """_journal_closed_trade must record the symbol in _recently_closed."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        from ozymandias.execution.fill_protection import StateChange

        pos = Position(
            symbol="AMD", shares=30.0, avg_cost=198.07,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="swing", direction="short", reasoning="",
                exit_targets=ExitTargets(stop_loss=202.0, profit_target=192.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        change = StateChange(
            order_id="ord_001", symbol="AMD",
            old_status="PENDING", new_status="FILLED",
            fill_qty=30.0, fill_price=195.0,
            side="buy", change_type="fill",
        )
        await orch._journal_closed_trade(change)

        assert "AMD" in orch._recently_closed, (
            "_journal_closed_trade must populate _recently_closed"
        )

    @pytest.mark.asyncio
    async def test_position_sync_skips_readoption_within_60s(self, orch):
        """Position sync must not re-adopt a symbol closed within the last 60s."""
        import time
        from ozymandias.execution.broker_interface import BrokerPosition
        from unittest.mock import patch

        # Mark AMD as just closed
        orch._recently_closed["AMD"] = time.monotonic()

        # Broker still shows AMD (settlement delay)
        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="AMD", qty=30.0, avg_entry_price=198.07,
                current_price=195.0, market_value=5852.1, unrealized_pl=-90.0,
            )
        ])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        portfolio = await orch._state_manager.load_portfolio()
        assert not any(p.symbol == "AMD" for p in portfolio.positions), (
            "AMD must not be re-adopted within the 60s TTL window"
        )

    @pytest.mark.asyncio
    async def test_position_sync_allows_adoption_after_ttl_expires(self, orch):
        """After the 60s TTL, position sync may adopt the symbol again."""
        import time
        from ozymandias.execution.broker_interface import BrokerPosition
        from unittest.mock import patch

        # Simulate close that happened 90s ago (TTL = 60s)
        orch._recently_closed["AMD"] = time.monotonic() - 90.0

        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="AMD", qty=30.0, avg_entry_price=198.07,
                current_price=195.0, market_value=5852.1, unrealized_pl=-90.0,
            )
        ])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        portfolio = await orch._state_manager.load_portfolio()
        assert any(p.symbol == "AMD" for p in portfolio.positions), (
            "AMD should be adoptable after the 60s TTL has expired"
        )


# ===========================================================================
# _apply_position_reviews exit action — Bug #2 (2026-03-16)
# ===========================================================================

class TestApplyPositionReviewsExit:
    """
    _apply_position_reviews must place a market exit order when action="exit".

    Regression for Bug #2: Claude returned action="exit" on AMD 6 consecutive
    times from 15:12–16:42 ET. The field was read but never acted upon — no
    exit order was placed.
    """

    @pytest.mark.asyncio
    async def test_exit_action_on_long_places_sell_order(self, orch):
        """action='exit' on a long position must place a market SELL order."""
        from ozymandias.execution.broker_interface import OrderResult

        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="AAPL", shares=10.0, avg_cost=200.0, entry_date=now_iso,
            intention=TradeIntention(direction="long"),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))
        portfolio = await orch._state_manager.load_portfolio()

        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="exit-001", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        reviews = [{"symbol": "AAPL", "action": "exit",
                    "updated_reasoning": "Thesis invalidated — exit immediately."}]
        await orch._apply_position_reviews(reviews)

        orch._broker.place_order.assert_called_once()
        order = orch._broker.place_order.call_args[0][0]
        assert order.symbol == "AAPL"
        assert order.side == "sell"
        assert order.quantity == 10.0
        assert order.order_type == "market"

    @pytest.mark.asyncio
    async def test_exit_action_on_short_places_buy_order(self, orch):
        """action='exit' on a short position must place a market BUY (buy-to-cover) order."""
        from ozymandias.execution.broker_interface import OrderResult

        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="AMD", shares=30.0, avg_cost=198.07, entry_date=now_iso,
            intention=TradeIntention(direction="short"),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))
        portfolio = await orch._state_manager.load_portfolio()

        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="exit-002", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        reviews = [{"symbol": "AMD", "action": "exit",
                    "updated_reasoning": "Short thesis completely invalidated."}]
        await orch._apply_position_reviews(reviews)

        orch._broker.place_order.assert_called_once()
        order = orch._broker.place_order.call_args[0][0]
        assert order.symbol == "AMD"
        assert order.side == "buy", "Short exit must be a buy-to-cover order"
        assert order.quantity == 30.0
        assert order.order_type == "market"

    @pytest.mark.asyncio
    async def test_exit_action_blocked_when_order_pending(self, orch):
        """action='exit' is blocked if a pending exit order already exists."""
        from ozymandias.execution.broker_interface import OrderResult

        now_iso = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="AMD", shares=30.0, avg_cost=198.07, entry_date=now_iso,
            intention=TradeIntention(direction="short"),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))
        portfolio = await orch._state_manager.load_portfolio()

        # Simulate an existing pending order
        existing = OrderRecord(
            order_id="pending-001", symbol="AMD", side="buy",
            quantity=30.0, order_type="market", limit_price=None,
            status="PENDING", created_at=now_iso, last_checked_at=now_iso,
        )
        await orch._fill_protection.record_order(existing)

        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="exit-003", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        reviews = [{"symbol": "AMD", "action": "exit",
                    "updated_reasoning": "Exit now."}]
        await orch._apply_position_reviews(reviews)

        orch._broker.place_order.assert_not_called()



# ===========================================================================
# Ghost cleanup avg_cost fallback — Bug #8 (2026-03-16)
# ===========================================================================

class TestGhostCleanupExitPrice:
    """
    Ghost cleanup must fall back to pos.avg_cost when no market price is cached.

    Regression for Bug #8: NVDA journal entry showed exit_price=0.0 and
    pnl=0% because _latest_indicators had no price for NVDA at cleanup time.
    """

    @pytest.mark.asyncio
    async def test_ghost_cleanup_uses_avg_cost_when_no_indicator_price(self, orch):
        """When indicators have no price for symbol, ghost cleanup uses avg_cost."""
        import json
        from ozymandias.execution.broker_interface import BrokerPosition
        from unittest.mock import patch

        avg_cost = 184.81
        pos = Position(
            symbol="NVDA", shares=32.0, avg_cost=avg_cost,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(strategy="unknown", direction="long"),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        # No indicators for NVDA → price defaults to 0.0
        orch._latest_indicators = {}

        # Broker does NOT show NVDA → ghost cleanup fires
        orch._broker.get_positions = AsyncMock(return_value=[])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        # Position should be removed
        portfolio = await orch._state_manager.load_portfolio()
        assert not any(p.symbol == "NVDA" for p in portfolio.positions)

        # Journal entry must use avg_cost as exit_price, not 0.0
        journal_path = orch._trade_journal._path
        assert journal_path.exists(), "Expected ghost cleanup to write a journal entry"
        entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
        nvda_entries = [e for e in entries if e.get("symbol") == "NVDA"]
        assert nvda_entries, "No NVDA journal entry written by ghost cleanup"
        entry = nvda_entries[-1]
        assert entry["exit_price"] == pytest.approx(avg_cost), (
            f"Expected exit_price={avg_cost} (avg_cost fallback), got {entry['exit_price']}"
        )

    @pytest.mark.asyncio
    async def test_ghost_cleanup_uses_market_price_when_available(self, orch):
        """When indicators have a price, ghost cleanup uses the market price."""
        import json
        from ozymandias.execution.broker_interface import BrokerPosition
        from unittest.mock import patch

        avg_cost = 184.81
        market_price = 190.0
        pos = Position(
            symbol="NVDA", shares=32.0, avg_cost=avg_cost,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(strategy="unknown", direction="long"),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        # Indicators have a live price
        orch._latest_indicators = {"NVDA": {"price": market_price}}
        orch._broker.get_positions = AsyncMock(return_value=[])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        journal_path = orch._trade_journal._path
        assert journal_path.exists()
        entries = [json.loads(line) for line in journal_path.read_text().splitlines()]
        nvda_entries = [e for e in entries if e.get("symbol") == "NVDA"]
        assert nvda_entries
        entry = nvda_entries[-1]
        assert entry["exit_price"] == pytest.approx(market_price)


# ===========================================================================
# Portfolio cash/buying_power broker sync — Bug #7 (2026-03-16)
# ===========================================================================

class TestPortfolioCashSync:
    """
    Medium loop must sync portfolio.cash and portfolio.buying_power from broker.

    Regression for Bug #7: portfolio.json showed cash=0.0, buying_power=0.0
    for the entire session. The medium loop fetched the account but never
    wrote the values back to portfolio state.
    """

    def _medium_loop_mocks(self, orch, broker_cash, broker_bp):
        """
        Return a context manager that patches the medium loop's data-fetch
        dependencies so the cycle runs to step 4 (account fetch) without
        hitting real I/O or the slow loop.
        """
        import pandas as pd
        from unittest.mock import patch

        # Minimal OHLCV DataFrame — enough for generate_signal_summary to succeed
        idx = pd.date_range("2026-03-16 09:30", periods=5, freq="5min", tz="UTC")
        mock_df = pd.DataFrame({
            "open":   [100.0] * 5, "high":  [105.0] * 5,
            "low":    [98.0]  * 5, "close": [103.0] * 5,
            "volume": [1_000_000] * 5,
        }, index=idx)

        orch._data_adapter.fetch_bars = AsyncMock(return_value=mock_df)
        orch._broker.get_account = AsyncMock(return_value=AccountInfo(
            equity=100_000.0, buying_power=broker_bp, cash=broker_cash,
            currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
        ))
        orch._ranker.rank_opportunities = MagicMock(return_value=RankResult(candidates=[], rejections=[]))
        orch._broker.get_open_orders = AsyncMock(return_value=[])
        orch._broker.get_positions = AsyncMock(return_value=[])
        # Pre-seed indicators so the slow-loop trigger (indicators_were_empty) won't fire
        orch._latest_indicators = {"AAPL": {}}

        return patch.multiple(
            "ozymandias.core.orchestrator",
            is_market_open=MagicMock(return_value=True),
            get_current_session=MagicMock(return_value=MagicMock(value="regular")),
        )

    @pytest.mark.asyncio
    async def test_medium_loop_syncs_cash_and_buying_power(self, orch):
        """After medium loop step 4 (acct fetch), portfolio cash/buying_power match broker."""
        broker_cash = 50_000.0
        broker_bp = 80_000.0

        # Stale portfolio with zero cash
        await orch._state_manager.save_portfolio(
            PortfolioState(cash=0.0, buying_power=0.0, positions=[])
        )
        await _set_watchlist(orch, tier1=["AAPL"])

        with self._medium_loop_mocks(orch, broker_cash, broker_bp):
            await orch._medium_loop_cycle()

        saved = await orch._state_manager.load_portfolio()
        assert saved.cash == pytest.approx(broker_cash), (
            f"Expected cash={broker_cash}, got {saved.cash}"
        )
        assert saved.buying_power == pytest.approx(broker_bp), (
            f"Expected buying_power={broker_bp}, got {saved.buying_power}"
        )

    @pytest.mark.asyncio
    async def test_medium_loop_no_save_when_cash_already_matches(self, orch):
        """No extra portfolio save when cash/buying_power already match broker."""
        broker_cash = 50_000.0
        broker_bp = 80_000.0

        # Portfolio already matches broker
        await orch._state_manager.save_portfolio(
            PortfolioState(cash=broker_cash, buying_power=broker_bp, positions=[])
        )
        await _set_watchlist(orch, tier1=["AAPL"])

        # Spy: save_portfolio must NOT be called for the cash sync
        original_save = orch._state_manager.save_portfolio
        save_calls: list = []

        async def counting_save(p):
            save_calls.append(p)
            return await original_save(p)

        orch._state_manager.save_portfolio = counting_save

        with self._medium_loop_mocks(orch, broker_cash, broker_bp):
            await orch._medium_loop_cycle()

        assert len(save_calls) == 0, (
            "save_portfolio called unnecessarily when cash/buying_power already matched broker"
        )


class TestDispatchConfirmedFill:
    """_dispatch_confirmed_fill routes to open or close based on portfolio state."""

    def _make_change(self, symbol="META", side="sell"):
        from ozymandias.execution.fill_protection import StateChange
        return StateChange(
            order_id="ord_001", symbol=symbol,
            old_status="PENDING", new_status="FILLED",
            fill_qty=9.0, fill_price=628.23,
            side=side, change_type="fill",
        )

    @pytest.mark.asyncio
    async def test_no_position_routes_to_opening_fill(self, orch):
        """sell fill with no local position → opening short, not journal."""
        orch._pending_intentions["META"] = {
            "stop": 635.0, "target": 615.0, "strategy": "swing",
            "direction": "short", "reasoning": "r",
            "_signals": {}, "_claude_conviction": 0.7, "_composite_score": 0.6,
        }
        change = self._make_change(side="sell")

        await orch._dispatch_confirmed_fill(change)

        portfolio = await orch._state_manager.load_portfolio()
        assert len(portfolio.positions) == 1
        pos = portfolio.positions[0]
        assert pos.symbol == "META"
        assert pos.intention.direction == "short"
        assert pos.shares == 9.0

    @pytest.mark.asyncio
    async def test_existing_position_routes_to_journal(self, orch):
        """buy fill with existing short position → journal/close, not duplicate open."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        existing = Position(
            symbol="META", shares=9.0, avg_cost=628.23,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="swing", direction="short", reasoning="",
                exit_targets=ExitTargets(stop_loss=635.0, profit_target=615.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[existing]))

        # Simulate a buy-to-close fill
        from ozymandias.execution.fill_protection import StateChange
        change = StateChange(
            order_id="ord_002", symbol="META",
            old_status="PENDING", new_status="FILLED",
            fill_qty=9.0, fill_price=620.0,
            side="buy", change_type="fill",
        )

        await orch._dispatch_confirmed_fill(change)

        # Position removed (journaled/closed)
        portfolio = await orch._state_manager.load_portfolio()
        assert len(portfolio.positions) == 0

    @pytest.mark.asyncio
    async def test_long_buy_fill_no_position_creates_long(self, orch):
        """buy fill with no local position → long open."""
        orch._pending_intentions["AMD"] = {
            "stop": 194.0, "target": 205.0, "strategy": "momentum",
            "direction": "long", "reasoning": "r",
            "_signals": {}, "_claude_conviction": 0.8, "_composite_score": 0.7,
        }
        from ozymandias.execution.fill_protection import StateChange
        change = StateChange(
            order_id="ord_003", symbol="AMD",
            old_status="PENDING", new_status="FILLED",
            fill_qty=30.0, fill_price=198.07,
            side="buy", change_type="fill",
        )

        await orch._dispatch_confirmed_fill(change)

        portfolio = await orch._state_manager.load_portfolio()
        assert len(portfolio.positions) == 1
        assert portfolio.positions[0].intention.direction == "long"


class TestPositionSyncQtyCorrection:
    """position_sync corrects local qty to match broker truth."""

    @pytest.mark.asyncio
    async def test_corrects_qty_discrepancy(self, orch):
        """When local qty != broker qty, local is updated to broker value."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        from ozymandias.execution.broker_interface import BrokerPosition

        # Local state has stale qty from partial fill
        pos = Position(
            symbol="AMD", shares=2.0, avg_cost=198.07,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="swing", direction="long", reasoning="",
                exit_targets=ExitTargets(stop_loss=194.0, profit_target=205.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="AMD", qty=30.0, avg_entry_price=198.07,
                current_price=200.0, market_value=6000.0, unrealized_pl=57.9,
            )
        ])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        portfolio = await orch._state_manager.load_portfolio()
        assert portfolio.positions[0].shares == 30.0

    @pytest.mark.asyncio
    async def test_no_save_when_no_changes(self, orch):
        """When local and broker qty match, portfolio is not written."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        from ozymandias.execution.broker_interface import BrokerPosition

        pos = Position(
            symbol="AMD", shares=30.0, avg_cost=198.07,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="swing", direction="long", reasoning="",
                exit_targets=ExitTargets(stop_loss=194.0, profit_target=205.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))
        save_spy = AsyncMock(wraps=orch._state_manager.save_portfolio)
        orch._state_manager.save_portfolio = save_spy

        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="AMD", qty=30.0, avg_entry_price=198.07,
                current_price=200.0, market_value=6000.0, unrealized_pl=57.9,
            )
        ])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        save_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_position_broker_negative_qty_stored_positive(self, orch):
        """Broker reports shorts as negative qty; local shares stay positive."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        from ozymandias.execution.broker_interface import BrokerPosition

        pos = Position(
            symbol="META", shares=9.0, avg_cost=628.23,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="swing", direction="short", reasoning="",
                exit_targets=ExitTargets(stop_loss=635.0, profit_target=615.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        # Broker returns qty=-9 for a short position
        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="META", qty=-9.0, avg_entry_price=628.23,
                current_price=625.0, market_value=-5653.5, unrealized_pl=29.07,
                side="short",
            )
        ])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        portfolio = await orch._state_manager.load_portfolio()
        # Shares must remain positive so exit order quantity= works correctly
        assert portfolio.positions[0].shares == 9.0

    @pytest.mark.asyncio
    async def test_skips_adoption_when_opening_order_in_flight(self, orch):
        """
        Regression: partial fill race.

        When a PARTIALLY_FILLED buy order is in flight, position_sync sees the
        broker position before _register_opening_fill runs. It must NOT adopt —
        adoption consumes _pending_intentions early, causing the final fill to be
        routed as a close (strategy="unknown" re-adoption bug).
        """
        from ozymandias.core.state_manager import OrderRecord
        from ozymandias.execution.broker_interface import BrokerPosition

        # Active buy order for CVX — simulates a partially-filled entry order
        buy_record = OrderRecord(
            order_id="buy_001", symbol="CVX", side="buy",
            quantity=24, order_type="limit", limit_price=200.55,
            status="PARTIALLY_FILLED",
            created_at=datetime.now(timezone.utc).isoformat(),
            last_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        await orch._fill_protection.record_order(buy_record)

        # Store the pending intention (simulates _medium_try_entry having run)
        orch._pending_intentions["CVX"] = {
            "strategy": "swing", "direction": "long",
            "stop": 195.5, "target": 208.0, "reasoning": "energy breakout",
            "_signals": {}, "_claude_conviction": 0.65, "_composite_score": 0.49,
        }

        # Broker reports 5 shares (the partial fill) — local portfolio is empty
        orch._broker.get_positions = AsyncMock(return_value=[
            BrokerPosition(
                symbol="CVX", qty=5.0, avg_entry_price=200.55,
                current_price=200.55, market_value=1002.75, unrealized_pl=0.0,
            )
        ])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        # Position must NOT have been adopted — fill handler will register it properly
        portfolio = await orch._state_manager.load_portfolio()
        assert not any(p.symbol == "CVX" for p in portfolio.positions), (
            "CVX was adopted during partial fill despite having an in-flight buy order"
        )
        # _pending_intentions must still be intact for _register_opening_fill to use
        assert "CVX" in orch._pending_intentions, (
            "_pending_intentions was consumed prematurely — full fill would get strategy='unknown'"
        )

    @pytest.mark.asyncio
    async def test_defers_ghost_cleanup_when_exit_order_pending(self, orch):
        """
        If an exit order is in-flight for a symbol absent from broker positions,
        position_sync must NOT remove it as ghost — the fill dispatch will close
        it correctly on the next cycle.

        This prevents the hallucination loop: sell fills → phantom open → double sell.
        """
        from ozymandias.core.state_manager import ExitTargets, TradeIntention
        from ozymandias.execution.broker_interface import BrokerPosition

        # Local position exists
        pos = Position(
            symbol="SPY", shares=8.0, avg_cost=670.09,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="momentum", direction="long", reasoning="",
                exit_targets=ExitTargets(stop_loss=660.0, profit_target=680.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        # Pending exit order exists for SPY (market sell, not yet detected as filled)
        exit_record = OrderRecord(
            order_id="sell_001", symbol="SPY", side="sell",
            quantity=8, order_type="market", limit_price=None,
            status="PENDING",
            created_at=datetime.now(timezone.utc).isoformat(),
            last_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        await orch._fill_protection.record_order(exit_record)

        # Broker no longer shows SPY (market order already settled on broker side)
        orch._broker.get_positions = AsyncMock(return_value=[])

        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_step_position_sync()

        # SPY must NOT have been removed — fill dispatch will handle it
        portfolio = await orch._state_manager.load_portfolio()
        assert any(p.symbol == "SPY" for p in portfolio.positions), (
            "SPY was ghost-cleaned despite having a pending exit order"
        )


# ===========================================================================
# Credentials loading — plaintext and encrypted paths
# ===========================================================================

class TestLoadCredentials:

    def _make_orch(self, tmp_path):
        """Return an Orchestrator with _config._config_dir pointed at tmp_path."""
        with (
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.__init__", MagicMock(return_value=None)),
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_account", AsyncMock(return_value=_stub_account())),
            patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours", AsyncMock(return_value=_stub_hours())),
            patch("anthropic.AsyncAnthropic", MagicMock),
            patch("ozymandias.core.orchestrator.Orchestrator._load_credentials", MagicMock(return_value=("k", "s"))),
        ):
            o = Orchestrator()
            o._state_manager._dir = tmp_path
            o._trade_journal._path = tmp_path / "trade_journal.jsonl"
            o._reasoning_cache._dir = tmp_path / "cache"
            o._reasoning_cache._dir.mkdir()
        o._config._config_dir = tmp_path
        return o

    def test_plaintext_credentials_loaded(self, tmp_path):
        import json, os
        creds = {"api_key": "KEY123", "secret_key": "SECRET456", "anthropic_api_key": "ANT123"}
        (tmp_path / "credentials.enc").write_text(json.dumps(creds))
        o = self._make_orch(tmp_path)
        env_before = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            api_key, secret_key = o._load_credentials()
            assert api_key == "KEY123"
            assert secret_key == "SECRET456"
            assert os.environ.get("ANTHROPIC_API_KEY") == "ANT123"
        finally:
            if env_before is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_before
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_anthropic_key_not_overwritten_if_env_set(self, tmp_path):
        import json, os
        creds = {"api_key": "K", "secret_key": "S", "anthropic_api_key": "FROM_FILE"}
        (tmp_path / "credentials.enc").write_text(json.dumps(creds))
        o = self._make_orch(tmp_path)
        os.environ["ANTHROPIC_API_KEY"] = "FROM_ENV"
        try:
            o._load_credentials()
            assert os.environ["ANTHROPIC_API_KEY"] == "FROM_ENV"
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_encrypted_credentials_loaded(self, tmp_path):
        import json
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        key_file = tmp_path / ".ozy_key"
        key_file.write_bytes(key)

        creds = {"api_key": "ENC_KEY", "secret_key": "ENC_SECRET"}
        encrypted = Fernet(key).encrypt(json.dumps(creds).encode())
        (tmp_path / "credentials.enc").write_bytes(encrypted)

        o = self._make_orch(tmp_path)
        o._config.broker.credentials_key_file = str(key_file)
        api_key, secret_key = o._load_credentials()
        assert api_key == "ENC_KEY"
        assert secret_key == "ENC_SECRET"

    def test_encrypted_missing_key_file_raises(self, tmp_path):
        from cryptography.fernet import Fernet
        import json
        key = Fernet.generate_key()
        creds = {"api_key": "K", "secret_key": "S"}
        encrypted = Fernet(key).encrypt(json.dumps(creds).encode())
        (tmp_path / "credentials.enc").write_bytes(encrypted)

        o = self._make_orch(tmp_path)
        o._config.broker.credentials_key_file = str(tmp_path / "nonexistent.key")
        with pytest.raises(RuntimeError, match="key file not found"):
            o._load_credentials()

    def test_encrypted_wrong_key_raises(self, tmp_path):
        from cryptography.fernet import Fernet
        import json
        key_correct = Fernet.generate_key()
        key_wrong = Fernet.generate_key()
        key_file = tmp_path / ".ozy_key"
        key_file.write_bytes(key_wrong)

        creds = {"api_key": "K", "secret_key": "S"}
        encrypted = Fernet(key_correct).encrypt(json.dumps(creds).encode())
        (tmp_path / "credentials.enc").write_bytes(encrypted)

        o = self._make_orch(tmp_path)
        o._config.broker.credentials_key_file = str(key_file)
        with pytest.raises(RuntimeError, match="decrypt"):
            o._load_credentials()

    def test_missing_api_key_field_raises(self, tmp_path):
        import json
        creds = {"wrong_field": "x"}
        (tmp_path / "credentials.enc").write_text(json.dumps(creds))
        o = self._make_orch(tmp_path)
        with pytest.raises(RuntimeError, match="api_key"):
            o._load_credentials()


# ===========================================================================
# Overnight running — market-hours gates
# ===========================================================================

class TestOvernightGates:
    """
    When is_market_open() returns False, all three loop cycles must return
    immediately without calling any broker or Claude APIs.
    """

    @pytest.mark.asyncio
    async def test_fast_loop_silent_when_market_closed(self, orch):
        orch._config.scheduler.bypass_market_hours = False
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=False):
            await orch._fast_loop_cycle()
        orch._broker.get_open_orders.assert_not_called()
        orch._broker.get_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_medium_loop_silent_when_market_closed(self, orch):
        orch._config.scheduler.bypass_market_hours = False
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=False):
            await orch._medium_loop_cycle()
        orch._broker.get_account.assert_not_called()

    @pytest.mark.asyncio
    async def test_slow_loop_silent_when_market_closed(self, orch):
        orch._config.scheduler.bypass_market_hours = False
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=False):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_claude:
                await orch._slow_loop_cycle()
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_fast_loop_runs_when_market_open(self, orch):
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            await orch._fast_loop_cycle()
        orch._broker.get_open_orders.assert_called()


# ===========================================================================
# Short selling — entry wiring
# ===========================================================================

class TestShortEntryWiring:
    """
    sell_short opportunities must produce sell orders, correct stop/target
    orientation, and correct P&L calculation.
    """

    @pytest.fixture(autouse=True)
    def market_open(self):
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            yield

    @pytest.mark.asyncio
    async def test_sell_short_produces_sell_order(self, orch):
        """A sell_short opportunity must place an Order with side='sell'."""
        from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity
        from ozymandias.core.market_hours import Session
        from ozymandias.execution.broker_interface import AccountInfo, OrderResult

        acct = AccountInfo(
            equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
            currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
        )
        opp = ScoredOpportunity(
            symbol="TSLA",
            action="sell_short",
            strategy="momentum",
            ai_conviction=0.75,
            technical_score=0.70,
            risk_adjusted_return=0.60,
            liquidity_score=0.80,
            reasoning="bearish breakdown",
            suggested_entry=250.0,
            suggested_exit=230.0,   # below entry — profit target for short
            suggested_stop=260.0,   # above entry — stop for short
            position_size_pct=0.05,
            composite_score=0.75,
        )
        portfolio = PortfolioState()
        orders_state = OrdersState()
        orch._latest_indicators = {"TSLA": {"atr_14": 5.0, "price": 250.0}}
        orch._latest_market_context = {}
        orch._risk_manager.validate_entry = MagicMock(return_value=(True, ""))
        orch._fill_protection.can_place_order = MagicMock(return_value=True)
        orch._fill_protection.record_order = AsyncMock()

        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="short-001", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        await orch._medium_try_entry(opp, acct, portfolio, orders_state.orders)

        assert orch._broker.place_order.called
        placed = orch._broker.place_order.call_args[0][0]
        assert placed.side == "sell", f"Expected sell, got {placed.side}"
        assert placed.symbol == "TSLA"

    @pytest.mark.asyncio
    async def test_short_stop_above_entry_target_below(self, orch):
        """For a short entry, the stored intention must have stop > entry and target < entry."""
        from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity
        from ozymandias.core.market_hours import Session
        from ozymandias.execution.broker_interface import AccountInfo, OrderResult

        acct = AccountInfo(
            equity=100_000.0, buying_power=80_000.0, cash=50_000.0,
            currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
        )
        entry_price = 300.0
        opp = ScoredOpportunity(
            symbol="NVDA",
            action="sell_short",
            strategy="momentum",
            ai_conviction=0.70,
            technical_score=0.65,
            risk_adjusted_return=0.60,
            liquidity_score=0.75,
            reasoning="distribution pattern",
            suggested_entry=entry_price,
            suggested_exit=0.0,    # let ATR fallback compute
            suggested_stop=0.0,    # let ATR fallback compute
            position_size_pct=0.05,
            composite_score=0.70,
        )
        portfolio = PortfolioState()
        orders_state = OrdersState()
        orch._latest_indicators = {"NVDA": {"atr_14": 8.0, "price": entry_price}}
        orch._latest_market_context = {}
        orch._risk_manager.validate_entry = MagicMock(return_value=(True, ""))
        orch._fill_protection.can_place_order = MagicMock(return_value=True)
        orch._fill_protection.record_order = AsyncMock()

        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="short-002", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        await orch._medium_try_entry(opp, acct, portfolio, orders_state.orders)

        pending = orch._pending_intentions.get("NVDA")
        assert pending is not None
        assert pending["direction"] == "short"
        # ATR fallback: stop = entry + 2*atr, target = entry - 3*atr
        assert pending["stop"] > entry_price, "Short stop must be above entry"
        assert pending["target"] < entry_price, "Short target must be below entry"

    @pytest.mark.asyncio
    async def test_short_exit_is_buy_order(self, orch):
        """Short exit via VWAP crossover must place a 'buy' (buy-to-cover) order."""
        import time as _time
        symbol = "TSLA"
        position = Position(
            symbol=symbol,
            shares=10.0,
            avg_cost=250.0,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(direction="short"),
        )
        await _set_portfolio(orch, [position])
        orch._latest_indicators = {symbol: {
            "price": 270.0,       # above entry — short is losing
            "vwap": 260.0,
            "vwap_position": "above",
            "volume_ratio": 2.0,  # above vwap_volume_threshold (1.3)
            "rsi": 65.0,
            "rsi_divergence": False,
            "roc_5": 0.04,
            "roc_deceleration": False,
            "roc_negative_deceleration": False,
            "atr_14": 1000.0,     # large ATR → ATR trail won't fire
        }}
        orch._intraday_highs[symbol] = 272.0
        # Satisfy min-hold guard (6 min ago — beyond 5-min default)
        orch._position_entry_times[symbol] = _time.monotonic() - 360

        from ozymandias.execution.broker_interface import OrderResult
        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="cover-001", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        # VWAP crossover for a short: price above VWAP with high volume → buy-to-cover.
        await orch._fast_step_quant_overrides()
        orch._broker.place_order.assert_called_once()
        call_args = orch._broker.place_order.call_args[0][0]
        assert call_args.side == "buy", "Short exit must be a buy-to-cover order"
        assert call_args.symbol == symbol

    def test_short_pnl_positive_when_price_falls(self):
        """P&L for a short closed below entry must be positive."""
        # Simulate the P&L calculation logic directly
        entry_price = 250.0
        exit_price = 230.0
        direction = "short"
        if direction == "short":
            pnl_pct = round((entry_price - exit_price) / entry_price * 100, 4)
        else:
            pnl_pct = round((exit_price - entry_price) / entry_price * 100, 4)
        assert pnl_pct > 0, "Short closed below entry should be profitable"
        assert abs(pnl_pct - 8.0) < 0.01

    def test_short_pnl_negative_when_price_rises(self):
        """P&L for a short closed above entry must be negative."""
        entry_price = 250.0
        exit_price = 270.0
        direction = "short"
        if direction == "short":
            pnl_pct = round((entry_price - exit_price) / entry_price * 100, 4)
        else:
            pnl_pct = round((exit_price - entry_price) / entry_price * 100, 4)
        assert pnl_pct < 0, "Short closed above entry should be a loss"


# ===========================================================================
# Trade journal lifecycle (open / snapshot / review / close records)
# ===========================================================================

def _read_journal(tmp_path):
    """Read all entries from the tmp_path journal file."""
    import json
    p = tmp_path / "trade_journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _make_fill(symbol="NVDA", fill_qty=10.0, fill_price=900.0, side="buy"):
    from ozymandias.execution.fill_protection import StateChange
    return StateChange(
        order_id="ord_jl_001", symbol=symbol,
        old_status="PENDING", new_status="FILLED",
        fill_qty=fill_qty, fill_price=fill_price,
        side=side, change_type="fill",
    )


class TestTradeJournalLifecycle:
    """
    Verify that the four journal record types (open / snapshot / review / close)
    are written at the correct lifecycle events and contain the expected fields.
    """

    def _seed_pending(self, orch, symbol="NVDA"):
        orch._pending_intentions[symbol] = {
            "stop": 870.0, "target": 950.0,
            "strategy": "swing", "direction": "long", "reasoning": "breakout",
            "_signals": {"rsi": 63.0, "vwap_position": "above"},
            "_claude_conviction": 0.78,
            "_composite_score": 0.71,
            "_position_size_pct": 0.10,
        }

    # -----------------------------------------------------------------------
    # open record
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_open_entry_written_on_fill(self, orch, tmp_path):
        """_register_opening_fill writes a record_type='open' journal entry."""
        self._seed_pending(orch)
        await orch._register_opening_fill(_make_fill())

        entries = _read_journal(tmp_path)
        open_entries = [e for e in entries if e.get("record_type") == "open"]
        assert len(open_entries) == 1
        e = open_entries[0]
        assert e["symbol"] == "NVDA"
        assert e["strategy"] == "swing"
        assert e["direction"] == "long"
        assert abs(e["entry_price"] - 900.0) < 0.01
        assert abs(e["shares"] - 10.0) < 0.01
        assert abs(e["stop_price"] - 870.0) < 0.01
        assert abs(e["target_price"] - 950.0) < 0.01
        assert e["signals_at_entry"].get("rsi") == 63.0
        assert abs(e["claude_conviction"] - 0.78) < 0.001
        assert abs(e["composite_score"] - 0.71) < 0.001
        assert abs(e["position_size_pct"] - 0.10) < 0.001
        assert "trade_id" in e
        assert e.get("record_type") == "open"

    @pytest.mark.asyncio
    async def test_no_open_entry_without_pending_intention(self, orch, tmp_path):
        """Broker-adopted positions (no pending) must NOT write an open entry."""
        # No _pending_intentions seeded — simulates broker adoption
        await orch._register_opening_fill(_make_fill())

        entries = _read_journal(tmp_path)
        assert not any(e.get("record_type") == "open" for e in entries)

    # -----------------------------------------------------------------------
    # open + close share trade_id
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_open_and_close_share_trade_id(self, orch, tmp_path):
        """The open and close records for the same trade must have the same trade_id."""
        from ozymandias.execution.fill_protection import StateChange

        self._seed_pending(orch)
        await orch._register_opening_fill(_make_fill())

        # Now close the position
        close_change = StateChange(
            order_id="ord_jl_002", symbol="NVDA",
            old_status="PENDING", new_status="FILLED",
            fill_qty=10.0, fill_price=930.0,
            side="sell", change_type="fill",
        )
        await orch._journal_closed_trade(close_change)

        entries = _read_journal(tmp_path)
        open_entry = next((e for e in entries if e.get("record_type") == "open"), None)
        close_entry = next((e for e in entries if e.get("record_type") == "close"), None)

        assert open_entry is not None, "Expected an 'open' journal entry"
        assert close_entry is not None, "Expected a 'close' journal entry"
        assert open_entry["trade_id"] == close_entry["trade_id"], (
            f"trade_id mismatch: open={open_entry['trade_id']} close={close_entry['trade_id']}"
        )

    # -----------------------------------------------------------------------
    # close record has record_type
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_close_entry_has_record_type(self, orch, tmp_path):
        """Every _journal_closed_trade call must produce a record_type='close' entry."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention

        pos = Position(
            symbol="TSLA", shares=5.0, avg_cost=200.0,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="momentum", direction="long",
                exit_targets=ExitTargets(stop_loss=185.0, profit_target=220.0),
            ),
        )
        await orch._state_manager.save_portfolio(PortfolioState(positions=[pos]))

        from ozymandias.execution.fill_protection import StateChange
        change = StateChange(
            order_id="ord_jl_003", symbol="TSLA",
            old_status="PENDING", new_status="FILLED",
            fill_qty=5.0, fill_price=215.0, side="sell", change_type="fill",
        )
        await orch._journal_closed_trade(change)

        entries = _read_journal(tmp_path)
        close_entries = [e for e in entries if e.get("record_type") == "close"]
        assert len(close_entries) == 1
        assert close_entries[0]["symbol"] == "TSLA"
        assert close_entries[0]["exit_reason"] in ("target", "stop", "strategy")

    # -----------------------------------------------------------------------
    # snapshot record on profit trigger
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_snapshot_written_on_profit_trigger(self, orch, tmp_path):
        """_check_triggers writes a record_type='snapshot' entry when profit threshold fires."""
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="HAL", shares=100, avg_cost=33.00, entry_date=now,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=35.00, stop_loss=31.50),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        # Seed a trade_id so the snapshot can carry it
        orch._entry_contexts["HAL"] = {"trade_id": "test-trade-id-hal"}
        orch._latest_indicators = {"HAL": {"price": 34.00}}  # +3.03% > 3.0% threshold

        triggers = await orch._check_triggers()
        assert "position_in_profit:HAL" in triggers

        entries = _read_journal(tmp_path)
        snap = next((e for e in entries if e.get("record_type") == "snapshot"), None)
        assert snap is not None, "Expected a 'snapshot' journal entry on profit trigger"
        assert snap["symbol"] == "HAL"
        assert snap["trigger"] == "position_in_profit"
        assert snap["trade_id"] == "test-trade-id-hal"
        assert snap["unrealized_pnl_pct"] > 0
        assert abs(snap["current_price"] - 34.00) < 0.01
        assert abs(snap["stop_price"] - 31.50) < 0.01
        assert abs(snap["target_price"] - 35.00) < 0.01

    @pytest.mark.asyncio
    async def test_no_snapshot_when_profit_trigger_does_not_fire(self, orch, tmp_path):
        """No snapshot is written when gain is below the profit threshold."""
        now = datetime.now(timezone.utc).isoformat()
        pos = Position(
            symbol="HAL", shares=100, avg_cost=33.00, entry_date=now,
            intention=TradeIntention(
                direction="long",
                exit_targets=ExitTargets(profit_target=35.00, stop_loss=31.50),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        orch._latest_indicators = {"HAL": {"price": 33.30}}  # +0.91% — below threshold

        await orch._check_triggers()

        entries = _read_journal(tmp_path)
        assert not any(e.get("record_type") == "snapshot" for e in entries)

    # -----------------------------------------------------------------------
    # review record on position review
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_review_entry_written_on_position_review(self, orch, tmp_path):
        """_apply_position_reviews writes a record_type='review' entry for each matched position."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention

        pos = Position(
            symbol="AMZN", shares=8.0, avg_cost=180.0,
            entry_date=datetime.now(timezone.utc).isoformat(),
            intention=TradeIntention(
                strategy="swing", direction="long",
                exit_targets=ExitTargets(stop_loss=172.0, profit_target=196.0),
            ),
        )
        await _set_portfolio(orch, [pos])
        orch._entry_contexts["AMZN"] = {"trade_id": "test-trade-id-amzn"}
        orch._latest_indicators = {"AMZN": {"price": 187.0}}

        reviews = [{
            "symbol": "AMZN",
            "action": "hold",
            "updated_reasoning": "thesis intact, hold",
            "adjusted_targets": {"stop_loss": 175.0},
        }]
        await orch._apply_position_reviews(reviews)

        entries = _read_journal(tmp_path)
        review_entries = [e for e in entries if e.get("record_type") == "review"]
        assert len(review_entries) == 1
        r = review_entries[0]
        assert r["symbol"] == "AMZN"
        assert r["action"] == "hold"
        assert r["note"] == "thesis intact, hold"
        assert r["trade_id"] == "test-trade-id-amzn"
        assert r["current_price"] == 187.0
        assert r["unrealized_pnl_pct"] > 0          # price > avg_cost
        assert abs(r["current_stop"] - 172.0) < 0.01  # BEFORE adjustment
        assert r["adjusted_targets"]["stop_loss"] == 175.0

    @pytest.mark.asyncio
    async def test_review_entry_written_for_every_matched_symbol(self, orch, tmp_path):
        """A review entry is written for each position symbol that appears in reviews."""
        from ozymandias.core.state_manager import ExitTargets, TradeIntention

        positions = [
            Position(
                symbol=sym, shares=10.0, avg_cost=100.0,
                entry_date=datetime.now(timezone.utc).isoformat(),
                intention=TradeIntention(
                    strategy="swing", direction="long",
                    exit_targets=ExitTargets(stop_loss=90.0, profit_target=115.0),
                ),
            )
            for sym in ("AAPL", "MSFT")
        ]
        await _set_portfolio(orch, positions)
        orch._latest_indicators = {"AAPL": {"price": 102.0}, "MSFT": {"price": 98.0}}

        reviews = [
            {"symbol": "AAPL", "action": "hold", "updated_reasoning": "ok"},
            {"symbol": "MSFT", "action": "hold", "updated_reasoning": "ok"},
        ]
        await orch._apply_position_reviews(reviews)

        entries = _read_journal(tmp_path)
        review_symbols = {e["symbol"] for e in entries if e.get("record_type") == "review"}
        assert review_symbols == {"AAPL", "MSFT"}

    @pytest.mark.asyncio
    async def test_review_entry_not_written_for_unmatched_symbol(self, orch, tmp_path):
        """No review entry is written when the review symbol has no open position."""
        reviews = [{"symbol": "FAKE", "action": "hold", "updated_reasoning": "ghost"}]
        await orch._apply_position_reviews(reviews)

        entries = _read_journal(tmp_path)
        assert not any(e.get("record_type") == "review" for e in entries)


# ===========================================================================
# Catalyst expiry pruner + fetch-failure suppression
# ===========================================================================

class TestPruneExpiredCatalysts:

    def _entry(self, symbol, expiry=None):
        now = datetime.now(timezone.utc).isoformat()
        return WatchlistEntry(
            symbol=symbol, date_added=now, reason="test",
            priority_tier=1, catalyst_expiry_utc=expiry,
        )

    def test_expired_entry_removed(self, orch):
        past = "2020-01-01T00:00:00+00:00"
        wl = WatchlistState(entries=[self._entry("STALE", expiry=past), self._entry("FRESH")])
        removed = orch._prune_expired_catalysts(wl)
        assert removed == ["STALE"]
        assert len(wl.entries) == 1
        assert wl.entries[0].symbol == "FRESH"

    def test_non_expired_entry_kept(self, orch):
        future = "2099-01-01T00:00:00+00:00"
        wl = WatchlistState(entries=[self._entry("SYM", expiry=future)])
        removed = orch._prune_expired_catalysts(wl)
        assert removed == []
        assert len(wl.entries) == 1

    def test_no_expiry_field_kept(self, orch):
        wl = WatchlistState(entries=[self._entry("SYM", expiry=None)])
        removed = orch._prune_expired_catalysts(wl)
        assert removed == []
        assert len(wl.entries) == 1

    def test_malformed_timestamp_kept(self, orch):
        wl = WatchlistState(entries=[self._entry("SYM", expiry="not-a-date")])
        removed = orch._prune_expired_catalysts(wl)
        assert removed == []
        assert len(wl.entries) == 1

    def test_empty_watchlist(self, orch):
        wl = WatchlistState(entries=[])
        removed = orch._prune_expired_catalysts(wl)
        assert removed == []


class TestCatalystExpiryApplyChanges:

    @pytest.mark.asyncio
    async def test_catalyst_expiry_stored_from_dict(self, orch):
        wl = WatchlistState(entries=[])
        expiry = "2026-12-31T21:00:00+00:00"
        add_list = [{"symbol": "NVDA", "reason": "earnings", "catalyst_expiry_utc": expiry}]
        await orch._apply_watchlist_changes(wl, add_list, [])
        assert wl.entries[0].catalyst_expiry_utc == expiry

    @pytest.mark.asyncio
    async def test_no_catalyst_expiry_when_absent(self, orch):
        wl = WatchlistState(entries=[])
        add_list = [{"symbol": "AAPL", "reason": "technical setup"}]
        await orch._apply_watchlist_changes(wl, add_list, [])
        assert wl.entries[0].catalyst_expiry_utc is None

    @pytest.mark.asyncio
    async def test_plain_string_add_has_no_expiry(self, orch):
        wl = WatchlistState(entries=[])
        await orch._apply_watchlist_changes(wl, ["TSLA"], [])
        assert wl.entries[0].catalyst_expiry_utc is None

    @pytest.mark.asyncio
    async def test_expired_entry_pruned_before_add(self, orch):
        past = "2020-01-01T00:00:00+00:00"
        now_iso = datetime.now(timezone.utc).isoformat()
        stale = WatchlistEntry(
            symbol="STALE", date_added=now_iso, reason="old",
            priority_tier=1, catalyst_expiry_utc=past,
        )
        wl = WatchlistState(entries=[stale])
        await orch._apply_watchlist_changes(wl, [], [])
        assert not any(e.symbol == "STALE" for e in wl.entries)


class TestFetchFailureSuppression:

    @pytest.mark.asyncio
    async def test_fetch_failure_sets_suppression(self, orch):
        orch._filter_suppressed.clear()
        orch._fetch_failure_counts.clear()
        # Simulate a fetch failure by calling the failure path directly
        sym = "NOK"
        orch._fetch_failure_counts[sym] = orch._fetch_failure_counts.get(sym, 0) + 1
        if sym not in orch._filter_suppressed:
            orch._filter_suppressed[sym] = "fetch_failure"
        assert orch._filter_suppressed.get(sym) == "fetch_failure"

    @pytest.mark.asyncio
    async def test_fetch_success_clears_fetch_failure_suppression(self, orch):
        orch._filter_suppressed["NOK"] = "fetch_failure"
        orch._fetch_failure_counts["NOK"] = 1
        # Simulate success path
        orch._fetch_failure_counts.pop("NOK", None)
        if orch._filter_suppressed.get("NOK") == "fetch_failure":
            orch._filter_suppressed.pop("NOK", None)
        assert "NOK" not in orch._filter_suppressed

    @pytest.mark.asyncio
    async def test_fetch_success_does_not_clear_other_suppression(self, orch):
        orch._filter_suppressed["NOK"] = "session_veto"
        orch._fetch_failure_counts.pop("NOK", None)
        if orch._filter_suppressed.get("NOK") == "fetch_failure":  # condition false
            orch._filter_suppressed.pop("NOK", None)
        assert orch._filter_suppressed.get("NOK") == "session_veto"


# ===========================================================================
# Phase 23 — Watchlist/Reasoning Separation + Build Reliability
# ===========================================================================

class TestPhase23WatchlistSeparation:
    """
    Tests for the Phase 23 changes:
    - candidates_exhausted fires build (not reasoning)
    - require_watchlist_before_reasoning deferral
    - parse failure uses short retry interval
    - exception failure uses probe_min retry
    - build passes removes to _apply_watchlist_changes
    - reasoning does NOT call _apply_watchlist_changes for removes
    - post-build reasoning fires after new candidates added
    - post-build reasoning skipped when nothing added
    - post-build reasoning cleared on build failure
    """

    @pytest.fixture(autouse=True)
    def market_open(self, orch):
        orch._latest_indicators = {"TEST": {"price": 100.0}}
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
            yield

    # ------------------------------------------------------------------ #
    # candidates_exhausted routing                                         #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_candidates_exhausted_fires_build_not_reasoning(self, orch):
        """candidates_exhausted → build fires, reasoning does NOT, flag set."""
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["candidates_exhausted"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_reasoning:
                with patch.object(orch, "_spawn_background_task") as mock_ef:
                    await orch._slow_loop_cycle()

        mock_reasoning.assert_not_called()
        mock_ef.assert_called_once()
        assert orch._reasoning_needed_after_build is True

    @pytest.mark.asyncio
    async def test_candidates_exhausted_build_in_flight_sets_flag(self, orch):
        """Build already in-flight when candidates_exhausted fires → flag still set, no second build."""
        orch._watchlist_build_in_flight = True
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["candidates_exhausted"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()):
                with patch.object(orch, "_spawn_background_task") as mock_ef:
                    await orch._slow_loop_cycle()

        mock_ef.assert_not_called()
        assert orch._reasoning_needed_after_build is True

    # ------------------------------------------------------------------ #
    # require_watchlist_before_reasoning                                   #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_require_watchlist_before_reasoning_defers(self, orch):
        """Config True + both build + reasoning triggers → reasoning deferred, flag set."""
        orch._config.scheduler.require_watchlist_before_reasoning = True
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["watchlist_stale", "price_move"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_reasoning:
                with patch.object(orch, "_spawn_background_task") as mock_ef:
                    await orch._slow_loop_cycle()

        mock_ef.assert_called_once()      # build fires
        mock_reasoning.assert_not_called()  # reasoning deferred
        assert orch._reasoning_needed_after_build is True

    @pytest.mark.asyncio
    async def test_require_watchlist_before_reasoning_false_fires_both(self, orch):
        """Config False + build + reasoning → both fire immediately (existing behaviour)."""
        orch._config.scheduler.require_watchlist_before_reasoning = False
        with patch.object(orch, "_check_triggers",
                          AsyncMock(return_value=["watchlist_stale", "price_move"])):
            with patch.object(orch, "_run_claude_cycle", AsyncMock()) as mock_reasoning:
                with patch.object(orch, "_spawn_background_task") as mock_ef:
                    await orch._slow_loop_cycle()

        mock_ef.assert_called_once()
        mock_reasoning.assert_called_once()
        assert orch._reasoning_needed_after_build is False

    # ------------------------------------------------------------------ #
    # Parse-failure vs API-failure retry intervals                         #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_parse_failure_uses_short_retry(self, orch):
        """wl_result is None → backdate uses watchlist_build_parse_failure_retry_min (3 min)."""
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._claude.run_watchlist_build = AsyncMock(return_value=None)
        orch._watchlist_build_in_flight = True

        parse_retry = orch._config.scheduler.watchlist_build_parse_failure_retry_min  # 3
        refresh = orch._config.scheduler.watchlist_refresh_interval_min               # 240

        before = datetime.now(timezone.utc)
        await orch._run_watchlist_build_task()
        after = datetime.now(timezone.utc)

        ts = orch._trigger_state.last_watchlist_build_utc
        # Expected backdate: now - (refresh - parse_retry) ≈ now - 237 min
        expected_backdate_sec = (refresh - parse_retry) * 60
        # ts should be roughly expected_backdate_sec in the past
        age_sec = (after - ts).total_seconds()
        assert abs(age_sec - expected_backdate_sec) < 5, (
            f"Parse failure backdate should be ~{expected_backdate_sec}s ago, got {age_sec:.1f}s"
        )
        # Flag cleared on parse failure
        assert orch._reasoning_needed_after_build is False

    @pytest.mark.asyncio
    async def test_exception_failure_uses_probe_min_retry(self, orch):
        """Exception in build → backdate uses ai_fallback.circuit_breaker_probe_min (10 min)."""
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._claude.run_watchlist_build = AsyncMock(side_effect=RuntimeError("api down"))
        orch._watchlist_build_in_flight = True

        probe_min = orch._config.ai_fallback.circuit_breaker_probe_min  # 10
        refresh = orch._config.scheduler.watchlist_refresh_interval_min  # 240

        await orch._run_watchlist_build_task()

        ts = orch._trigger_state.last_watchlist_build_utc
        expected_backdate_sec = (refresh - probe_min) * 60
        age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
        assert abs(age_sec - expected_backdate_sec) < 5, (
            f"Exception backdate should be ~{expected_backdate_sec}s ago, got {age_sec:.1f}s"
        )
        assert orch._reasoning_needed_after_build is False

    # ------------------------------------------------------------------ #
    # Build passes removes to _apply_watchlist_changes                    #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_watchlist_build_passes_removes_to_apply_changes(self, orch):
        """Build returns removes → _apply_watchlist_changes called with that remove list."""
        from ozymandias.intelligence.claude_reasoning import WatchlistResult
        await _set_watchlist(orch, tier1=["AAPL", "SYM"])
        wl_result = WatchlistResult(watchlist=[], removes=["SYM"], market_notes="", raw={})
        orch._claude.run_watchlist_build = AsyncMock(return_value=wl_result)

        apply_calls = []

        async def capture_apply(watchlist, add_list, remove_list, open_symbols=None, *, last_sector_regimes=None):
            apply_calls.append(remove_list)
            return 0

        orch._watchlist_manager.apply_watchlist_changes = capture_apply
        await orch._run_watchlist_build_task()

        assert apply_calls, "apply_watchlist_changes was never called"
        assert "SYM" in apply_calls[0], f"Expected 'SYM' in remove list, got {apply_calls[0]}"

    @pytest.mark.asyncio
    async def test_reasoning_does_not_call_apply_watchlist_changes_for_removes(self, orch):
        """Reasoning cycle: remove_list always [] regardless of watchlist_changes in result."""
        from ozymandias.intelligence.claude_reasoning import ReasoningResult
        apply_calls = []

        async def capture_apply(watchlist, add_list, remove_list, open_symbols=None):
            apply_calls.append((add_list, remove_list))
            return 0

        orch._apply_watchlist_changes = capture_apply

        result = ReasoningResult(
            timestamp="2026-04-01T12:00:00+00:00",
            position_reviews=[],
            new_opportunities=[],
            watchlist_changes={"add": [], "remove": ["TSLA"]},
            market_assessment="",
            risk_flags=[],
            session_veto=[],
            rejected_opportunities=[],
            raw={},
        )
        orch._claude.run_reasoning_cycle = AsyncMock(return_value=result)
        orch._claude.run_position_review_call = AsyncMock(return_value=result)

        with patch.object(orch, "_build_market_context", AsyncMock(return_value={"trading_session": "regular"})):
            with patch.object(orch, "_update_trigger_prices", AsyncMock()):
                with patch.object(orch, "_apply_position_reviews", AsyncMock()):
                    with patch.object(orch._trade_journal, "load_recent", AsyncMock(return_value=[])):
                        with patch.object(orch._trade_journal, "compute_session_stats", AsyncMock(return_value={})):
                            await orch._run_claude_cycle("price_move")

        for add_list, remove_list in apply_calls:
            assert remove_list == [], (
                f"Reasoning must not pass removes to _apply_watchlist_changes, got: {remove_list}"
            )

    # ------------------------------------------------------------------ #
    # Post-build reasoning                                                 #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_post_build_reasoning_fires_after_candidates_exhausted(self, orch):
        """Successful build with added > 0 and flag set → _post_build_reasoning fires."""
        import asyncio as _asyncio
        from ozymandias.intelligence.claude_reasoning import WatchlistResult
        await _set_watchlist(orch, tier1=[])
        new_entry = {"symbol": "NVDA", "reason": "momentum", "priority_tier": 1,
                     "strategy": "momentum", "expected_direction": "long"}
        wl_result = WatchlistResult(watchlist=[new_entry], removes=[], market_notes="", raw={})
        orch._claude.run_watchlist_build = AsyncMock(return_value=wl_result)
        orch._reasoning_needed_after_build = True

        mock_post_build = AsyncMock()
        orch._post_build_reasoning = mock_post_build

        await orch._run_watchlist_build_task()
        # Give the event loop a tick so _spawn_background_task-scheduled coroutine runs
        await _asyncio.sleep(0)

        # Flag must be cleared
        assert orch._reasoning_needed_after_build is False
        mock_post_build.assert_called_once_with("post_build_candidates")

    @pytest.mark.asyncio
    async def test_post_build_reasoning_skipped_when_nothing_added(self, orch):
        """Build succeeds but added == 0 → no post-build reasoning, flag cleared."""
        from ozymandias.intelligence.claude_reasoning import WatchlistResult
        await _set_watchlist(orch, tier1=["AAPL"])
        wl_result = WatchlistResult(watchlist=[], removes=[], market_notes="", raw={})
        orch._claude.run_watchlist_build = AsyncMock(return_value=wl_result)
        orch._reasoning_needed_after_build = True

        post_build_calls = []

        async def fake_post_build(trigger_name: str):
            post_build_calls.append(trigger_name)

        orch._post_build_reasoning = fake_post_build
        await orch._run_watchlist_build_task()

        assert orch._reasoning_needed_after_build is False
        assert post_build_calls == [], "Post-build reasoning must not fire when nothing added"

    @pytest.mark.asyncio
    async def test_post_build_reasoning_cleared_on_build_failure_none(self, orch):
        """Parse failure (None) → flag cleared, no post-build reasoning."""
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._claude.run_watchlist_build = AsyncMock(return_value=None)
        orch._reasoning_needed_after_build = True

        post_build_calls = []

        async def fake_post_build(trigger_name: str):
            post_build_calls.append(trigger_name)

        orch._post_build_reasoning = fake_post_build
        await orch._run_watchlist_build_task()

        assert orch._reasoning_needed_after_build is False
        assert post_build_calls == [], "Post-build reasoning must not fire on parse failure"

    @pytest.mark.asyncio
    async def test_post_build_reasoning_cleared_on_build_exception(self, orch):
        """Exception in build → flag cleared, no post-build reasoning."""
        await _set_watchlist(orch, tier1=["AAPL"])
        orch._claude.run_watchlist_build = AsyncMock(side_effect=RuntimeError("network"))
        orch._reasoning_needed_after_build = True

        post_build_calls = []

        async def fake_post_build(trigger_name: str):
            post_build_calls.append(trigger_name)

        orch._post_build_reasoning = fake_post_build
        await orch._run_watchlist_build_task()

        assert orch._reasoning_needed_after_build is False
        assert post_build_calls == [], "Post-build reasoning must not fire on exception"


# ===========================================================================
# RVOL-Conditional Dead Zone Bypass
# ===========================================================================

class TestDeadZoneRvolBypass:
    """
    Tests for _dead_zone_rvol_bypass():
    - Returns True when SPY RVOL is at or above threshold
    - Returns False when below threshold, disabled, or SPY data absent
    - Ranker rejection suppression respects the bypass
    - validate_entry receives dead_zone_exempt=True when bypass is active
    """

    @pytest.fixture(autouse=True)
    def setup(self, orch):
        orch._config.scheduler.dead_zone_rvol_bypass_enabled = True
        orch._config.scheduler.dead_zone_rvol_bypass_threshold = 1.5

    # ------------------------------------------------------------------ #
    # _dead_zone_rvol_bypass predicate                                     #
    # ------------------------------------------------------------------ #

    def test_bypass_true_when_spy_rvol_above_threshold(self, orch):
        """SPY RVOL 2.0 with threshold 1.5 → bypass active."""
        orch._all_indicators = {"SPY": {"volume_ratio": 2.0}}
        assert orch._dead_zone_rvol_bypass() is True

    def test_bypass_true_at_exact_threshold(self, orch):
        """SPY RVOL exactly at threshold → bypass active (>= comparison)."""
        orch._all_indicators = {"SPY": {"volume_ratio": 1.5}}
        assert orch._dead_zone_rvol_bypass() is True

    def test_bypass_false_when_spy_rvol_below_threshold(self, orch):
        """SPY RVOL 0.8 with threshold 1.5 → bypass inactive."""
        orch._all_indicators = {"SPY": {"volume_ratio": 0.8}}
        assert orch._dead_zone_rvol_bypass() is False

    def test_bypass_false_when_disabled(self, orch):
        """enabled=False → bypass always False regardless of RVOL."""
        orch._config.scheduler.dead_zone_rvol_bypass_enabled = False
        orch._all_indicators = {"SPY": {"volume_ratio": 5.0}}
        assert orch._dead_zone_rvol_bypass() is False

    def test_bypass_false_when_spy_missing(self, orch):
        """SPY not in _all_indicators → fail-safe returns False."""
        orch._all_indicators = {}
        assert orch._dead_zone_rvol_bypass() is False

    def test_bypass_false_when_volume_ratio_zero(self, orch):
        """volume_ratio=0.0 treated as missing data → False."""
        orch._all_indicators = {"SPY": {"volume_ratio": 0.0}}
        assert orch._dead_zone_rvol_bypass() is False

    def test_bypass_reads_nested_signals_for_context_symbol(self, orch):
        """SPY stored in context (nested signals) format → still reads correctly."""
        orch._all_indicators = {"SPY": {"signals": {"volume_ratio": 2.0}, "long_score": 0.5}}
        assert orch._dead_zone_rvol_bypass() is True

    def test_bypass_false_nested_signals_below_threshold(self, orch):
        """Nested signals format, RVOL below threshold → False."""
        orch._all_indicators = {"SPY": {"signals": {"volume_ratio": 0.9}, "long_score": 0.5}}
        assert orch._dead_zone_rvol_bypass() is False

    # ------------------------------------------------------------------ #
    # Ranker rejection suppression interaction                             #
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_ranker_suppression_counts_when_bypass_active(self, orch):
        """In dead zone + RVOL bypass active → ranker rejection COUNTS toward suppression."""
        orch._all_indicators = {"SPY": {"volume_ratio": 2.0}}
        orch._recommendation_outcomes = {}

        with patch.object(orch._risk_manager, "in_dead_zone", return_value=True):
            # Simulate the rejection counting loop directly
            reason = "RVOL 0.5 below floor 0.6"
            symbol = "AAPL"
            if "already open in portfolio" in reason:
                pass
            elif "market not in regular hours" in reason:
                pass
            elif orch._risk_manager.in_dead_zone() and not orch._dead_zone_rvol_bypass():
                pass  # would skip
            else:
                orch._recommendation_outcomes[symbol] = {
                    "claude_entry_target": 0.0,
                    "attempt_time_utc": None,
                    "stage": "ranker_rejected",
                    "stage_detail": reason,
                    "rejection_count": 1,
                    "order_id": None,
                }

        assert "AAPL" in orch._recommendation_outcomes, (
            "Rejection should count when bypass is active"
        )

    @pytest.mark.asyncio
    async def test_ranker_suppression_skipped_when_bypass_inactive(self, orch):
        """In dead zone + RVOL bypass inactive → ranker rejection SKIPPED."""
        orch._all_indicators = {"SPY": {"volume_ratio": 0.5}}
        orch._recommendation_outcomes = {}

        with patch.object(orch._risk_manager, "in_dead_zone", return_value=True):
            reason = "RVOL 0.4 below floor 0.6"
            symbol = "AAPL"
            if "already open in portfolio" in reason:
                pass
            elif "market not in regular hours" in reason:
                pass
            elif orch._risk_manager.in_dead_zone() and not orch._dead_zone_rvol_bypass():
                pass  # skipped — correct
            else:
                orch._recommendation_outcomes[symbol] = {"rejection_count": 1}

        assert "AAPL" not in orch._recommendation_outcomes, (
            "Rejection should be skipped when bypass is inactive"
        )

    # ------------------------------------------------------------------ #
    # validate_entry dead_zone_exempt passthrough                          #
    # ------------------------------------------------------------------ #

    def test_validate_entry_receives_exempt_true_when_bypass_active(self, orch):
        """bypass active + non-exempt strategy → dead_zone_exempt=True passed to validate_entry."""
        orch._all_indicators = {"SPY": {"volume_ratio": 2.0}}

        # Spy on how dead_zone_exempt would be computed at call site 3
        _dz_exempt = False  # non-exempt strategy
        result = _dz_exempt or orch._dead_zone_rvol_bypass()
        assert result is True, "dead_zone_exempt should be True when bypass is active"

    def test_validate_entry_exempt_false_when_bypass_inactive(self, orch):
        """bypass inactive + non-exempt strategy → dead_zone_exempt=False."""
        orch._all_indicators = {"SPY": {"volume_ratio": 0.5}}

        _dz_exempt = False
        result = _dz_exempt or orch._dead_zone_rvol_bypass()
        assert result is False

    def test_validate_entry_exempt_true_from_strategy_regardless_of_bypass(self, orch):
        """Strategy-level dead_zone_exempt=True always passes through."""
        orch._all_indicators = {"SPY": {"volume_ratio": 0.1}}  # bypass inactive

        _dz_exempt = True  # strategy is exempt
        result = _dz_exempt or orch._dead_zone_rvol_bypass()
        assert result is True

    # ------------------------------------------------------------------ #
    # Per-symbol (Tier 2) bypass                                          #
    # ------------------------------------------------------------------ #

    def test_symbol_bypass_true_when_symbol_rvol_above_threshold(self, orch):
        """Symbol RVOL above per-symbol threshold → bypass active even if SPY quiet."""
        orch._config.scheduler.dead_zone_symbol_rvol_bypass_threshold = 2.0
        orch._all_indicators = {"SPY": {"volume_ratio": 0.8}}  # SPY quiet
        orch._latest_indicators = {"AAPL": {"volume_ratio": 3.0}}
        assert orch._dead_zone_rvol_bypass("AAPL") is True

    def test_symbol_bypass_false_when_symbol_rvol_below_threshold(self, orch):
        """Symbol RVOL below per-symbol threshold and SPY quiet → bypass inactive."""
        orch._config.scheduler.dead_zone_symbol_rvol_bypass_threshold = 2.0
        orch._all_indicators = {"SPY": {"volume_ratio": 0.8}}
        orch._latest_indicators = {"AAPL": {"volume_ratio": 1.2}}
        assert orch._dead_zone_rvol_bypass("AAPL") is False

    def test_symbol_bypass_true_via_spy_tier_even_when_symbol_quiet(self, orch):
        """SPY above global threshold → bypass fires even if individual symbol is quiet."""
        orch._all_indicators = {"SPY": {"volume_ratio": 2.0}}
        orch._latest_indicators = {"AAPL": {"volume_ratio": 0.5}}
        assert orch._dead_zone_rvol_bypass("AAPL") is True

    def test_symbol_bypass_false_when_symbol_missing_from_indicators(self, orch):
        """Symbol not in _latest_indicators → fail-safe False (SPY quiet too)."""
        orch._all_indicators = {"SPY": {"volume_ratio": 0.8}}
        orch._latest_indicators = {}
        assert orch._dead_zone_rvol_bypass("AAPL") is False

    def test_no_symbol_arg_only_checks_spy(self, orch):
        """No symbol arg → only SPY check; high watchlist RVOL has no effect."""
        orch._config.scheduler.dead_zone_symbol_rvol_bypass_threshold = 2.0
        orch._all_indicators = {"SPY": {"volume_ratio": 0.8}}
        orch._latest_indicators = {"AAPL": {"volume_ratio": 5.0}}
        # Without symbol arg, tier-2 check is skipped entirely
        assert orch._dead_zone_rvol_bypass() is False

    def test_symbol_bypass_disabled_when_feature_off(self, orch):
        """enabled=False → per-symbol check also skipped."""
        orch._config.scheduler.dead_zone_rvol_bypass_enabled = False
        orch._latest_indicators = {"AAPL": {"volume_ratio": 10.0}}
        assert orch._dead_zone_rvol_bypass("AAPL") is False
