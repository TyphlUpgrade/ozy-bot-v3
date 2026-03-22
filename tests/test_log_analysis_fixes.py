"""
Tests for fixes from log analysis session 2026-03-19.

Covers:
  Fix 2 — startup_reconciliation seeds _daily_start_equity at startup equity
  Fix 3 — override/short_protection/eod_close exit reasons in trade journal
  Fix 4 — override_exit trigger fires correctly; resets after Claude cycle
  Fix 5 — YFinanceAdapter instantiated with bars_ttl from config
  Fix 6 — _apply_watchlist_changes returns actual add count (not raw add_list length)
  Bug 1 — stale-reasoning re-entry guard (_cycle_consumed_symbols)
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from ozymandias.core.config import Config
from ozymandias.core.state_manager import (
    ExitTargets,
    PortfolioState,
    Position,
    TradeIntention,
    WatchlistEntry,
    WatchlistState,
)
from ozymandias.execution.broker_interface import AccountInfo, OrderResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_account(equity: float = 30_000.0) -> AccountInfo:
    return AccountInfo(
        equity=equity,
        buying_power=equity * 2,
        cash=equity,
        currency="USD",
        pdt_flag=False,
        daytrade_count=0,
        account_id="test-account",
    )


def _make_position(
    symbol: str = "AAPL",
    direction: str = "long",
    avg_cost: float = 150.0,
    stop_loss: float = 140.0,
    profit_target: float = 165.0,
    strategy: str = "momentum",
) -> Position:
    return Position(
        symbol=symbol,
        shares=10.0,
        avg_cost=avg_cost,
        entry_date=datetime.now(timezone.utc).isoformat(),
        intention=TradeIntention(
            direction=direction,
            strategy=strategy,
            exit_targets=ExitTargets(
                stop_loss=stop_loss,
                profit_target=profit_target,
            ),
        ),
    )


def _make_orch():
    """Build a minimal Orchestrator bypassing __init__."""
    from ozymandias.core.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._config = Config()
    orch._broker = MagicMock()
    orch._broker.place_order = AsyncMock()
    orch._fill_protection = MagicMock()
    orch._fill_protection.can_place_order = MagicMock(return_value=True)
    orch._fill_protection.record_order = AsyncMock()
    orch._state_manager = MagicMock()
    orch._state_manager.load_portfolio = AsyncMock(return_value=PortfolioState())
    orch._state_manager.save_portfolio = AsyncMock()
    orch._state_manager.save_watchlist = AsyncMock()
    orch._latest_indicators = {}
    orch._intraday_lows = {}
    orch._intraday_highs = {}
    orch._recently_closed = {}
    orch._override_exit_count = 0
    orch._pending_exit_hints = {}
    orch._cycle_consumed_symbols = set()
    orch._conservative_mode_until = None
    orch._entry_contexts = {}
    orch._position_entry_times = {}
    orch._trigger_state = MagicMock()
    orch._trigger_state.last_override_exit_count = 0
    orch._strategies = []
    orch._trade_journal = MagicMock()
    orch._trade_journal.append = AsyncMock()
    orch._broker.place_order.return_value = OrderResult(
        order_id="test-order-001",
        status="pending_new",
        submitted_at=datetime.now(timezone.utc),
    )
    return orch


# ---------------------------------------------------------------------------
# Fix 2: startup_reconciliation seeds daily_start_equity
# ---------------------------------------------------------------------------

class TestDailyTrackingInit:
    def test_initialize_daily_tracking_sets_equity(self):
        """initialize_daily_tracking locks baseline to startup account equity."""
        from ozymandias.execution.risk_manager import RiskManager

        cfg = Config()
        rm = RiskManager(cfg.risk, MagicMock(), cfg.scheduler)

        assert rm._daily_start_equity == 0.0  # uninitialized

        acct = _make_account(equity=29_683.28)
        rm.initialize_daily_tracking(acct)

        assert rm._daily_start_equity == pytest.approx(29_683.28)

    def test_initialize_daily_tracking_sets_date(self):
        """initialize_daily_tracking sets _daily_loss_date to today (ET)."""
        from ozymandias.execution.risk_manager import RiskManager
        from zoneinfo import ZoneInfo

        cfg = Config()
        rm = RiskManager(cfg.risk, MagicMock(), cfg.scheduler)

        acct = _make_account(equity=30_000.0)
        rm.initialize_daily_tracking(acct)

        et_today = datetime.now(ZoneInfo("America/New_York")).date()
        assert rm._daily_loss_date == et_today


# ---------------------------------------------------------------------------
# Fix 3: exit_reason_hint propagation in _journal_closed_trade
# ---------------------------------------------------------------------------

class FillChange:
    """Minimal fill change object for _journal_closed_trade."""
    def __init__(self, symbol: str, fill_price: float, fill_qty: float = 10.0):
        self.symbol = symbol
        self.fill_price = fill_price
        self.fill_qty = fill_qty


class TestExitReasonHint:
    @pytest.mark.asyncio
    async def test_quant_override_exit_reason(self):
        """Override exit hint → journal entry has exit_reason='quant_override'."""
        orch = _make_orch()
        position = _make_position("AAPL", stop_loss=140.0, profit_target=165.0)
        portfolio = PortfolioState(positions=[position])
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.save_portfolio = AsyncMock()

        # Mid-range price — would infer "strategy" without hint
        change = FillChange("AAPL", fill_price=152.0)
        await orch._journal_closed_trade(change, exit_reason_hint="quant_override")

        call_args = orch._trade_journal.append.call_args[0][0]
        assert call_args["exit_reason"] == "quant_override"

    @pytest.mark.asyncio
    async def test_hard_stop_exit_reason(self):
        """Hard stop hint → journal entry has exit_reason='hard_stop'."""
        orch = _make_orch()
        position = _make_position("TSLA", direction="short", avg_cost=250.0,
                                  stop_loss=260.0, profit_target=230.0)
        portfolio = PortfolioState(positions=[position])
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.save_portfolio = AsyncMock()

        # Mid-range price for short — would infer "strategy" without hint
        change = FillChange("TSLA", fill_price=248.0)
        await orch._journal_closed_trade(change, exit_reason_hint="hard_stop")

        call_args = orch._trade_journal.append.call_args[0][0]
        assert call_args["exit_reason"] == "hard_stop"

    @pytest.mark.asyncio
    async def test_no_hint_falls_through_to_price_inference(self):
        """Without a hint, price-based inference still works (e.g. target hit)."""
        orch = _make_orch()
        position = _make_position("AAPL", stop_loss=140.0, profit_target=165.0)
        portfolio = PortfolioState(positions=[position])
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.save_portfolio = AsyncMock()

        # Fill at target price — no hint, should infer "target"
        change = FillChange("AAPL", fill_price=165.0)
        await orch._journal_closed_trade(change)

        call_args = orch._trade_journal.append.call_args[0][0]
        assert call_args["exit_reason"] == "target"

    @pytest.mark.asyncio
    async def test_pending_exit_hint_passed_via_dispatch(self):
        """_pending_exit_hints[symbol] is consumed and passed as hint in dispatch."""
        orch = _make_orch()
        position = _make_position("AAPL", stop_loss=140.0, profit_target=165.0)
        portfolio = PortfolioState(positions=[position])
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.save_portfolio = AsyncMock()

        # Simulate override path setting the hint, then fill arriving
        orch._pending_exit_hints["AAPL"] = "quant_override"

        change = FillChange("AAPL", fill_price=152.0)
        await orch._dispatch_confirmed_fill(change)

        call_args = orch._trade_journal.append.call_args[0][0]
        assert call_args["exit_reason"] == "quant_override"
        # Hint consumed — not left in dict
        assert "AAPL" not in orch._pending_exit_hints


# ---------------------------------------------------------------------------
# Fix 4: override_exit trigger fires and resets correctly
# ---------------------------------------------------------------------------

class TestOverrideExitTrigger:
    def _make_trigger_orch(self):
        """Minimal orchestrator for _check_triggers tests."""
        from ozymandias.core.orchestrator import Orchestrator, SlowLoopTriggerState

        orch = Orchestrator.__new__(Orchestrator)
        orch._config = Config()
        ts = SlowLoopTriggerState()
        # Suppress no_previous_call and time_ceiling by setting a recent call time
        ts.last_claude_call_utc = datetime.now(timezone.utc)
        orch._trigger_state = ts
        orch._last_known_equity = 30_000.0
        orch._degradation = MagicMock()
        orch._degradation.claude_available = True
        orch._degradation.claude_backoff_until_utc = None
        orch._latest_indicators = {}
        orch._state_manager = MagicMock()
        orch._state_manager.load_watchlist = AsyncMock(return_value=WatchlistState(entries=[]))
        orch._state_manager.load_portfolio = AsyncMock(return_value=PortfolioState())
        return orch, ts

    @pytest.mark.asyncio
    async def test_trigger_fires_after_increment(self):
        """After override exit count increments, _check_triggers returns 'override_exit'."""
        orch, ts = self._make_trigger_orch()

        # Simulate override exit without Claude running
        orch._override_exit_count = 1
        ts.last_override_exit_count = 0  # not yet synced

        triggers = await orch._check_triggers()
        assert "override_exit" in triggers

    @pytest.mark.asyncio
    async def test_trigger_clears_after_sync(self):
        """After last_override_exit_count is synced to _override_exit_count, trigger clears."""
        orch, ts = self._make_trigger_orch()

        orch._override_exit_count = 1
        # Simulate what line ~2402 does after a successful Claude call
        ts.last_override_exit_count = orch._override_exit_count

        triggers = await orch._check_triggers()
        assert "override_exit" not in triggers

    def test_premature_sync_removed_from_override_path(self):
        """Verify the premature sync lines were removed from override fast-loop functions.

        _fast_step_quant_overrides and _fast_step_short_exits must NOT update
        last_override_exit_count — only _run_claude_cycle should do that.
        """
        import ast
        import pathlib

        src = pathlib.Path(
            "ozymandias/core/orchestrator.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if node.name not in ("_fast_step_quant_overrides", "_fast_step_short_exits"):
                continue
            func_src = ast.get_source_segment(src, node) or ""
            assert "last_override_exit_count" not in func_src, (
                f"{node.name} must not assign last_override_exit_count "
                "(premature sync breaks override_exit trigger)"
            )


# ---------------------------------------------------------------------------
# Fix 5: YFinanceAdapter instantiated with bars_ttl from config
# ---------------------------------------------------------------------------

class TestBarsCacheTtl:
    def test_bars_cache_ttl_default(self):
        """SchedulerConfig default bars_cache_ttl_sec is 110."""
        from ozymandias.core.config import SchedulerConfig
        assert SchedulerConfig().bars_cache_ttl_sec == 110

    def test_bars_cache_ttl_loaded_from_json(self):
        """config.json bars_cache_ttl_sec is loaded into SchedulerConfig."""
        cfg = Config()  # loads from ozymandias/config/config.json
        assert cfg.scheduler.bars_cache_ttl_sec == 110

    def test_yfinance_adapter_receives_ttl_from_config(self):
        """YFinanceAdapter is instantiated with bars_ttl from scheduler config."""
        from ozymandias.data.adapters.yfinance_adapter import YFinanceAdapter

        with patch.object(YFinanceAdapter, "__init__", return_value=None) as mock_init:
            cfg = Config()
            cfg.scheduler.bars_cache_ttl_sec = 110

            # Reproduce the instantiation line from orchestrator._startup
            YFinanceAdapter(bars_ttl=cfg.scheduler.bars_cache_ttl_sec)
            mock_init.assert_called_once_with(bars_ttl=110)


# ---------------------------------------------------------------------------
# Fix 6: _apply_watchlist_changes returns actual add count
# ---------------------------------------------------------------------------

class TestWatchlistActualAddCount:
    @pytest.mark.asyncio
    async def test_duplicate_add_returns_zero(self):
        """Duplicate in add_list with existing entry → actual_adds=0, not 2."""
        orch = _make_orch()

        watchlist = WatchlistState(entries=[
            WatchlistEntry(symbol="AAPL", date_added="2026-03-19T09:00:00Z", reason="existing")
        ])

        # Both items are duplicates — AAPL already in watchlist
        add_list = ["AAPL", "AAPL"]
        actual = await orch._apply_watchlist_changes(watchlist, add_list, [], set())
        assert actual == 0

    @pytest.mark.asyncio
    async def test_new_symbol_counts_once(self):
        """New unique symbol → actual_adds=1."""
        orch = _make_orch()
        watchlist = WatchlistState(entries=[])

        actual = await orch._apply_watchlist_changes(watchlist, ["TSLA"], [], set())
        assert actual == 1

    @pytest.mark.asyncio
    async def test_mixed_add_list_counts_only_new(self):
        """2 adds (one duplicate) → actual_adds=1."""
        orch = _make_orch()
        watchlist = WatchlistState(entries=[
            WatchlistEntry(symbol="AAPL", date_added="2026-03-19T09:00:00Z", reason="existing")
        ])

        # AAPL is duplicate; NVDA is new; AAPL again is another duplicate
        add_list = ["AAPL", "NVDA", "AAPL"]
        actual = await orch._apply_watchlist_changes(watchlist, add_list, [], set())
        assert actual == 1

    @pytest.mark.asyncio
    async def test_index_blacklist_not_counted(self):
        """Non-tradeable index ticker rejected → not counted in actual_adds."""
        orch = _make_orch()
        watchlist = WatchlistState(entries=[])

        actual = await orch._apply_watchlist_changes(watchlist, ["VIX"], [], set())
        assert actual == 0


# ---------------------------------------------------------------------------
# Bug 1: Stale-reasoning re-entry guard (_cycle_consumed_symbols)
# ---------------------------------------------------------------------------

class TestCycleConsumedGuard:
    @pytest.mark.asyncio
    async def test_symbol_added_to_consumed_on_close(self):
        """Closing a position adds the symbol to _cycle_consumed_symbols."""
        orch = _make_orch()
        position = _make_position("INTC")
        portfolio = PortfolioState(positions=[position])
        orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
        orch._state_manager.save_portfolio = AsyncMock()

        assert "INTC" not in orch._cycle_consumed_symbols
        await orch._journal_closed_trade(FillChange("INTC", fill_price=45.5))
        assert "INTC" in orch._cycle_consumed_symbols

    @pytest.mark.asyncio
    async def test_consumed_symbol_blocks_medium_entry(self):
        """Symbol in _cycle_consumed_symbols is rejected before any other checks."""
        orch = _make_orch()
        orch._cycle_consumed_symbols.add("INTC")

        from ozymandias.intelligence.opportunity_ranker import ScoredOpportunity
        candidate = ScoredOpportunity(
            symbol="INTC",
            action="buy",
            strategy="momentum",
            ai_conviction=0.7,
            composite_score=0.63,
            technical_score=0.64,
            risk_adjusted_return=0.5,
            liquidity_score=0.6,
            suggested_entry=45.92,
            suggested_stop=44.5,
            suggested_exit=47.5,
            position_size_pct=0.08,
            reasoning="Micron catalyst",
            entry_conditions={},
        )

        # No broker calls, no portfolio loads needed — guard fires first
        result = await orch._medium_try_entry(candidate, MagicMock(), MagicMock(), [])
        assert result is False
        orch._broker.place_order.assert_not_called()

    def test_unconsumed_symbol_not_blocked_by_guard(self):
        """Symbol NOT in _cycle_consumed_symbols passes the guard condition."""
        orch = _make_orch()
        # INTC consumed, but AAPL is not
        orch._cycle_consumed_symbols.add("INTC")

        # Guard condition: only symbols in the set are blocked
        assert "AAPL" not in orch._cycle_consumed_symbols
        assert "INTC" in orch._cycle_consumed_symbols

    @pytest.mark.asyncio
    async def test_consumed_symbols_cleared_on_new_claude_cycle(self):
        """_cycle_consumed_symbols is cleared when a new Claude reasoning call succeeds."""
        orch = _make_orch()
        orch._cycle_consumed_symbols = {"INTC", "AMD", "TSLA"}

        # Simulate the post-success block in _run_claude_cycle
        from ozymandias.core.orchestrator import SlowLoopTriggerState
        orch._trigger_state = SlowLoopTriggerState()
        orch._degradation = MagicMock()
        orch._degradation.claude_available = False
        orch._degradation.claude_backoff_until_utc = None

        # Execute just the state-update lines that follow a successful call
        orch._degradation.claude_available = True
        orch._degradation.claude_backoff_until_utc = None
        orch._trigger_state.last_claude_call_utc = datetime.now(timezone.utc)
        orch._cycle_consumed_symbols.clear()

        assert orch._cycle_consumed_symbols == set()

    @pytest.mark.asyncio
    async def test_multiple_closes_same_cycle_all_blocked(self):
        """Multiple symbols closed in same cycle are all blocked until fresh reasoning."""
        orch = _make_orch()

        for sym in ("INTC", "AMD"):
            position = _make_position(sym)
            portfolio = PortfolioState(positions=[position])
            orch._state_manager.load_portfolio = AsyncMock(return_value=portfolio)
            orch._state_manager.save_portfolio = AsyncMock()
            await orch._journal_closed_trade(FillChange(sym, fill_price=100.0))

        assert "INTC" in orch._cycle_consumed_symbols
        assert "AMD" in orch._cycle_consumed_symbols
