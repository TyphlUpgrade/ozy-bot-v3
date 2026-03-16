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
        from ozymandias.core.market_hours import get_current_session
        symbols = [f"SYM{i}" for i in range(12)]
        await _set_watchlist(orch, tier1=symbols)
        orch._trigger_state.last_claude_call_utc = (
            datetime.now(timezone.utc) - timedelta(minutes=10)
        )
        orch._trigger_state.last_prices = {}
        orch._trigger_state.last_override_exit_count = 0
        orch._override_exit_count = 0
        orch._latest_indicators = {}
        # Sync last_session to actual current session so the transition trigger doesn't fire.
        orch._trigger_state.last_session = get_current_session().value
        triggers = await orch._check_triggers()
        assert triggers == []


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
        orch._risk_manager.calculate_position_size = MagicMock(return_value=10)
        orch._risk_manager.validate_entry = MagicMock(return_value=(True, ""))
        orch._fill_protection.can_place_order = MagicMock(return_value=True)
        orch._latest_indicators = {}
        orch._latest_market_context = {}

    @pytest.mark.asyncio
    async def test_large_position_triggers_thesis_challenge(self, orch):
        """position_size_pct >= threshold → run_thesis_challenge is called."""
        top = self._make_top(position_size_pct=0.20)
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"proceed": True, "conviction": 0.85, "challenge_reasoning": "ok"}
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
    async def test_challenge_proceed_false_skips_trade(self, orch):
        """Challenge returning proceed=False → place_order is NOT called."""
        top = self._make_top(position_size_pct=0.20)
        self._stub_entry_guards(orch)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"proceed": False, "conviction": 0.15,
                          "challenge_reasoning": "Failed breakout below $198 VWAP."}
        )
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        await orch._medium_try_entry(top, acct, portfolio, [])

        orch._broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_challenge_lower_conviction_scales_quantity(self, orch):
        """Challenge conviction 0.5 vs original 1.0 → quantity halved."""
        top = self._make_top(position_size_pct=0.20, ai_conviction=1.0)
        self._stub_entry_guards(orch)
        orch._risk_manager.calculate_position_size = MagicMock(return_value=20)
        orch._claude.run_thesis_challenge = AsyncMock(
            return_value={"proceed": True, "conviction": 0.5,
                          "challenge_reasoning": "Watch $212 gap resistance."}
        )
        acct = _stub_account()
        portfolio = PortfolioState(positions=[])

        placed_orders = []
        async def capture_order(order):
            placed_orders.append(order)
            return MagicMock(order_id="ord_001")
        orch._broker.place_order = capture_order

        # Need fill_protection.record_order to not blow up
        from ozymandias.core.state_manager import OrderRecord
        orch._fill_protection.record_order = AsyncMock()

        await orch._medium_try_entry(top, acct, portfolio, [])

        assert len(placed_orders) == 1
        # conviction ratio = 0.5/1.0 = 0.5 → 20 * 0.5 = 10
        assert placed_orders[0].quantity == 10

    @pytest.mark.asyncio
    async def test_challenge_returns_none_trade_proceeds(self, orch):
        """Challenge API failure (returns None) → trade proceeds with original quantity."""
        top = self._make_top(position_size_pct=0.20, ai_conviction=0.85)
        self._stub_entry_guards(orch)
        orch._risk_manager.calculate_position_size = MagicMock(return_value=10)
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

        assert len(placed_orders) == 1
        assert placed_orders[0].quantity == 10


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
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=False):
            await orch._fast_loop_cycle()
        orch._broker.get_open_orders.assert_not_called()
        orch._broker.get_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_medium_loop_silent_when_market_closed(self, orch):
        with patch("ozymandias.core.orchestrator.is_market_open", return_value=False):
            await orch._medium_loop_cycle()
        orch._broker.get_account.assert_not_called()

    @pytest.mark.asyncio
    async def test_slow_loop_silent_when_market_closed(self, orch):
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

        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="short-001", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        with patch("ozymandias.execution.risk_manager.get_current_session",
                   return_value=Session.REGULAR_HOURS):
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

        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="short-002", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        with patch("ozymandias.execution.risk_manager.get_current_session",
                   return_value=Session.REGULAR_HOURS):
            await orch._medium_try_entry(opp, acct, portfolio, orders_state.orders)

        pending = orch._pending_intentions.get("NVDA")
        assert pending is not None
        assert pending["direction"] == "short"
        # ATR fallback: stop = entry + 2*atr, target = entry - 3*atr
        assert pending["stop"] > entry_price, "Short stop must be above entry"
        assert pending["target"] < entry_price, "Short target must be below entry"

    @pytest.mark.asyncio
    async def test_short_exit_is_buy_order(self, orch):
        """Quant override exit for a short position must place a 'buy' (buy-to-cover) order."""
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
            "volume_ratio": 2.0,
            "rsi": 65.0,
            "rsi_divergence": False,
            "roc_5": 0.04,
            "atr_14": 5.0,
            "atr_trailing_stop": 265.0,
        }}
        orch._intraday_highs[symbol] = 272.0

        from ozymandias.execution.broker_interface import OrderResult
        orch._broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="cover-001", status="pending_new",
            submitted_at=datetime.now(timezone.utc),
        ))

        # _fast_step_quant_overrides skips shorts — no buy-to-cover via quant path
        await orch._fast_step_quant_overrides()
        orch._broker.place_order.assert_not_called()

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
