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
        o._reasoning_cache._dir = tmp_path / "cache"
        o._reasoning_cache._dir.mkdir()
        await o._startup()

    # Replace broker with a configurable mock
    broker = MagicMock()
    broker.get_account  = AsyncMock(return_value=_stub_account())
    broker.get_open_orders = AsyncMock(return_value=[])
    broker.get_positions   = AsyncMock(return_value=[])
    broker.place_order     = AsyncMock()
    broker.cancel_order    = AsyncMock()
    o._broker = broker
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
        symbols = [f"SYM{i}" for i in range(12)]
        await _set_watchlist(orch, tier1=symbols)
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        )
        orch._trigger_state.last_prices = {}
        orch._trigger_state.last_override_exit_count = 0
        orch._override_exit_count = 0
        orch._latest_indicators = {}
        triggers = await orch._check_triggers()
        assert triggers == []


# ===========================================================================
# Slow loop control-flow tests
# ===========================================================================

class TestSlowLoopCycle:

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
            raw={},
        )
        orch._claude.run_reasoning_cycle = AsyncMock(return_value=result)
        orch._broker.get_account = AsyncMock(return_value=_stub_account())

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

    @pytest.mark.asyncio
    async def test_only_one_entry_per_cycle(self, orch):
        """
        Even if the ranker returns 3 scored opportunities, only the top-ranked
        one should be attempted per cycle.
        """
        from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity

        opp = lambda sym, score: ScoredOpportunity(
            symbol=sym, action="buy", strategy="momentum",
            composite_score=score, ai_conviction=0.7, technical_score=0.6,
            risk_adjusted_return=0.5, liquidity_score=1.0,
            suggested_entry=200.0, suggested_exit=220.0, suggested_stop=190.0,
            position_size_pct=0.08, reasoning="test",
        )
        ranked = [opp("AAPL", 0.8), opp("TSLA", 0.7), opp("NVDA", 0.6)]

        entry_calls = []

        async def fake_try_entry(top, acct, portfolio, orders):
            entry_calls.append(top.symbol)

        with (
            patch.object(orch, "_data_adapter") as mock_adapter,
            patch.object(orch, "_ranker") as mock_ranker,
            patch.object(orch, "_medium_evaluate_positions", AsyncMock()),
            patch.object(orch, "_medium_try_entry", fake_try_entry),
            patch("ozymandias.core.orchestrator.generate_signal_summary",
                  return_value={"signals": {}, "composite_technical_score": 0.5,
                                "symbol": "X", "timestamp": ""}),
        ):
            import pandas as pd
            import numpy as np
            from datetime import timezone
            df = pd.DataFrame({
                "open": [200.0], "high": [201.0], "low": [199.0],
                "close": [200.5], "volume": [100_000.0],
            }, index=pd.DatetimeIndex(
                [datetime.now(timezone.utc)], tz=timezone.utc
            ))
            mock_adapter.fetch_bars = AsyncMock(return_value=df)
            mock_ranker.rank_opportunities = MagicMock(return_value=ranked)
            orch._broker.get_account = AsyncMock(return_value=_stub_account())

            await _set_watchlist(orch, tier1=["AAPL"])
            with patch("ozymandias.core.orchestrator.is_market_open", return_value=True):
                await orch._medium_loop_cycle()

        assert len(entry_calls) == 1, (
            f"Expected exactly 1 entry attempt, got {len(entry_calls)}: {entry_calls}"
        )
        assert entry_calls[0] == "AAPL", f"Expected top-ranked AAPL, got {entry_calls[0]}"


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
        portfolio = await orch._state_manager.load_portfolio()

        reviews = [{"symbol": "AAPL", "updated_reasoning": "Thesis intact, hold."}]
        await orch._apply_position_reviews(portfolio, reviews)

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
        await orch._apply_position_reviews(portfolio, reviews)

        saved = await orch._state_manager.load_portfolio()
        assert saved.positions[0].intention.exit_targets.profit_target == 230.0
        assert saved.positions[0].intention.exit_targets.stop_loss == 195.0
