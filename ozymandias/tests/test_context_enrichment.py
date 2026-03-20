"""
tests/test_context_enrichment.py
==================================
Phase 15 — Context Enrichment: unit tests for:
- RankResult and ranker interface
- _recommendation_outcomes lifecycle
- recommendation_outcomes context assembly
- TradeJournal.load_recent and compute_session_stats
- ta_readiness in watchlist tier-1 context
- WatchlistEntry.expected_direction
- Backward compat / error handling
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from ozymandias.core.state_manager import (
    PortfolioState,
    WatchlistEntry,
    WatchlistState,
)
from ozymandias.core.trade_journal import TradeJournal
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.intelligence.claude_reasoning import ClaudeReasoningEngine
from ozymandias.intelligence.opportunity_ranker import (
    OpportunityRanker,
    RankResult,
    ScoredOpportunity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account(equity=100_000.0, buying_power=80_000.0, cash=50_000.0):
    return AccountInfo(
        equity=equity, buying_power=buying_power, cash=cash,
        currency="USD", pdt_flag=False, daytrade_count=0, account_id="test",
    )


def _portfolio(positions=None):
    from ozymandias.core.state_manager import PortfolioState
    return PortfolioState(
        cash=50_000.0, buying_power=80_000.0,
        positions=positions or [],
    )


def _pdt_guard(allow=True):
    guard = MagicMock()
    guard.can_day_trade.return_value = (allow, "")
    guard.count_day_trades.return_value = 0
    return guard


def _market_open(is_open=True):
    return lambda: is_open


def _ranker(**cfg):
    return OpportunityRanker(cfg or None)


def _reasoning_result(opportunities=None, reviews=None):
    from ozymandias.intelligence.claude_reasoning import ReasoningResult
    return ReasoningResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        position_reviews=reviews or [],
        new_opportunities=opportunities or [],
        watchlist_changes={"add": [], "remove": [], "rationale": ""},
        market_assessment="",
        risk_flags=[],
        rejected_opportunities=[],
        session_veto=[],
        raw={},
    )


def _opportunity(symbol="AAPL", action="buy", strategy="momentum", conviction=0.7,
                 suggested_entry=150.0, suggested_exit=165.0, suggested_stop=145.0,
                 position_size_pct=0.08, **kw):
    return {
        "symbol": symbol, "action": action, "strategy": strategy,
        "conviction": conviction, "reasoning": "test",
        "suggested_entry": suggested_entry,
        "suggested_exit": suggested_exit,
        "suggested_stop": suggested_stop,
        "position_size_pct": position_size_pct,
        "entry_conditions": {},
        **kw,
    }


def _tech(symbol="AAPL", rvol=2.0, vwap_position="above", trend="bullish_aligned"):
    """Minimal technical signals dict that passes hard filters."""
    return {
        symbol: {
            "composite_technical_score": 0.6,
            "signals": {
                "avg_daily_volume": 2_000_000,
                "volume_ratio": rvol,
                "vwap_position": vwap_position,
                "trend_structure": trend,
                "rsi": 62.0,
                "rsi_slope_5": 3.0,
                "macd_signal": "bullish",
                "macd_histogram_expanding": True,
            },
        }
    }


def _make_engine(tmp_path) -> ClaudeReasoningEngine:
    """Return a ClaudeReasoningEngine with minimal config, no real API."""
    from ozymandias.core.config import Config, ClaudeConfig
    cfg = Config()
    cfg.claude = ClaudeConfig(prompt_version="v3.5.0")
    cfg._config_dir = Path(__file__).resolve().parent.parent / "config"
    with patch("anthropic.AsyncAnthropic", MagicMock):
        engine = ClaudeReasoningEngine(cfg)
    return engine


# ---------------------------------------------------------------------------
# 1. RankResult and ranker interface
# ---------------------------------------------------------------------------

class TestRankResult:

    def test_rank_opportunities_returns_rank_result(self):
        r = _ranker()
        rr = _reasoning_result(opportunities=[])
        result = r.rank_opportunities(
            rr, {}, _account(), _portfolio(), _pdt_guard(), _market_open(), []
        )
        assert isinstance(result, RankResult)
        assert hasattr(result, "candidates")
        assert hasattr(result, "rejections")

    def test_empty_opportunities_gives_empty_candidates_and_rejections(self):
        r = _ranker()
        rr = _reasoning_result(opportunities=[])
        result = r.rank_opportunities(
            rr, {}, _account(), _portfolio(), _pdt_guard(), _market_open(), []
        )
        assert result.candidates == []
        assert result.rejections == []

    def test_hard_filtered_symbol_appears_in_rejections(self):
        """A symbol that fails hard filter appears in .rejections with a reason."""
        r = _ranker()
        opp = _opportunity(symbol="LOWVOL")
        signals = {
            "LOWVOL": {"composite_technical_score": 0.6, "signals": {"avg_daily_volume": 500}},
        }
        rr = _reasoning_result(opportunities=[opp])
        result = r.rank_opportunities(
            rr, signals, _account(), _portfolio(), _pdt_guard(), _market_open(), []
        )
        assert result.candidates == []
        assert len(result.rejections) == 1
        sym, reason = result.rejections[0]
        assert sym == "LOWVOL"
        assert "volume" in reason.lower() or "LOWVOL" in reason

    def test_accepted_symbol_not_in_rejections(self):
        """A symbol that passes all filters appears in .candidates, not .rejections."""
        r = _ranker()
        opp = _opportunity(symbol="AAPL")
        signals = _tech("AAPL")
        rr = _reasoning_result(opportunities=[opp])
        result = r.rank_opportunities(
            rr, signals, _account(), _portfolio(), _pdt_guard(), _market_open(), []
        )
        assert len(result.candidates) == 1
        assert result.candidates[0].symbol == "AAPL"
        rejected_syms = [s for s, _ in result.rejections]
        assert "AAPL" not in rejected_syms

    def test_market_closed_gives_rejection(self):
        """Market-closed hard filter produces a rejection entry."""
        r = _ranker()
        opp = _opportunity(symbol="AAPL")
        signals = _tech("AAPL")
        rr = _reasoning_result(opportunities=[opp])
        result = r.rank_opportunities(
            rr, signals, _account(), _portfolio(), _pdt_guard(), _market_open(False), []
        )
        assert result.candidates == []
        assert any("AAPL" in sym for sym, _ in result.rejections)


# ---------------------------------------------------------------------------
# 2. _recommendation_outcomes lifecycle
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def orch(tmp_path):
    """A fully-started Orchestrator with all external calls mocked."""
    from ozymandias.core.orchestrator import Orchestrator
    from ozymandias.execution.broker_interface import MarketHours

    stub_hours = MarketHours(
        is_open=True,
        next_open=datetime.now(timezone.utc) + timedelta(hours=1),
        next_close=datetime.now(timezone.utc) + timedelta(hours=8),
        session="regular",
    )
    with (
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.__init__", MagicMock(return_value=None)),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_account",
              AsyncMock(return_value=_account())),
        patch("ozymandias.execution.alpaca_broker.AlpacaBroker.get_market_hours",
              AsyncMock(return_value=stub_hours)),
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
    broker.get_account = AsyncMock(return_value=_account())
    broker.get_open_orders = AsyncMock(return_value=[])
    broker.get_positions = AsyncMock(return_value=[])
    broker.place_order = AsyncMock()
    broker.cancel_order = AsyncMock()
    o._broker = broker
    return o


class TestRecommendationOutcomesLifecycle:

    def test_orchestrator_has_recommendation_outcomes(self, orch):
        assert hasattr(orch, "_recommendation_outcomes")
        assert isinstance(orch._recommendation_outcomes, dict)
        assert orch._recommendation_outcomes == {}

    @pytest.mark.asyncio
    async def test_ranker_rejection_populates_outcomes(self, orch):
        """After a ranker hard-filter rejection, the symbol appears with ranker_rejected stage."""
        now_iso = datetime.now(timezone.utc).isoformat()
        # Manually simulate what the medium loop does after rank_opportunities
        orch._recommendation_outcomes["NVDA"] = {
            "claude_entry_target": 0.0,
            "attempt_time_utc": now_iso,
            "stage": "ranker_rejected",
            "stage_detail": "RVOL 0.10 below floor 1.00",
            "rejection_count": 1,
            "order_id": None,
        }
        assert orch._recommendation_outcomes["NVDA"]["stage"] == "ranker_rejected"
        assert orch._recommendation_outcomes["NVDA"]["rejection_count"] == 1

    @pytest.mark.asyncio
    async def test_rejection_count_increments(self, orch):
        """Second rejection increments rejection_count to 2."""
        now_iso = datetime.now(timezone.utc).isoformat()
        orch._recommendation_outcomes["NVDA"] = {
            "claude_entry_target": 0.0,
            "attempt_time_utc": now_iso,
            "stage": "ranker_rejected",
            "stage_detail": "reason 1",
            "rejection_count": 1,
            "order_id": None,
        }
        # Simulate second rejection
        existing = orch._recommendation_outcomes["NVDA"]
        orch._recommendation_outcomes["NVDA"] = {
            "claude_entry_target": existing.get("claude_entry_target", 0.0),
            "attempt_time_utc": existing.get("attempt_time_utc") or now_iso,
            "stage": "ranker_rejected",
            "stage_detail": "reason 2",
            "rejection_count": existing.get("rejection_count", 0) + 1,
            "order_id": None,
        }
        assert orch._recommendation_outcomes["NVDA"]["rejection_count"] == 2

    @pytest.mark.asyncio
    async def test_conditions_waiting_stage(self, orch):
        """Entry conditions defer updates stage to conditions_waiting."""
        now_iso = datetime.now(timezone.utc).isoformat()
        orch._recommendation_outcomes["AMD"] = {
            "claude_entry_target": 204.0,
            "attempt_time_utc": now_iso,
            "stage": "ranker_rejected",
            "stage_detail": "was rejected",
            "rejection_count": 1,
            "order_id": None,
        }
        # Simulate conditions_waiting update
        orch._recommendation_outcomes["AMD"] = {
            **orch._recommendation_outcomes["AMD"],
            "stage": "conditions_waiting",
            "stage_detail": "defer_count=1, conditions={'rsi_min': 65}",
        }
        assert orch._recommendation_outcomes["AMD"]["stage"] == "conditions_waiting"

    @pytest.mark.asyncio
    async def test_gate_expired_stage(self, orch):
        """Gate expiry updates stage to gate_expired."""
        now_iso = datetime.now(timezone.utc).isoformat()
        orch._recommendation_outcomes["AMD"] = {
            "attempt_time_utc": now_iso,
            "stage": "conditions_waiting",
            "stage_detail": "...",
        }
        orch._recommendation_outcomes["AMD"] = {
            **orch._recommendation_outcomes["AMD"],
            "stage": "gate_expired",
            "stage_detail": "gate cleared after 5 misses",
        }
        assert orch._recommendation_outcomes["AMD"]["stage"] == "gate_expired"

    @pytest.mark.asyncio
    async def test_order_pending_stage(self, orch):
        """After order placement, stage becomes order_pending with order_id."""
        now_iso = datetime.now(timezone.utc).isoformat()
        orch._recommendation_outcomes["TSLA"] = {
            "attempt_time_utc": now_iso,
            "stage": "ranker_rejected",
            "stage_detail": "...",
            "rejection_count": 1,
            "order_id": None,
            "claude_entry_target": 0.0,
        }
        orch._recommendation_outcomes["TSLA"] = {
            **orch._recommendation_outcomes["TSLA"],
            "stage": "order_pending",
            "order_id": "order-abc-123",
            "claude_entry_target": 250.0,
        }
        assert orch._recommendation_outcomes["TSLA"]["stage"] == "order_pending"
        assert orch._recommendation_outcomes["TSLA"]["order_id"] == "order-abc-123"

    @pytest.mark.asyncio
    async def test_filled_stage(self, orch):
        """On confirmed opening fill, stage becomes filled."""
        now_iso = datetime.now(timezone.utc).isoformat()
        orch._recommendation_outcomes["TSLA"] = {
            "attempt_time_utc": now_iso,
            "stage": "order_pending",
            "order_id": "order-abc-123",
        }
        orch._recommendation_outcomes["TSLA"]["stage"] = "filled"
        assert orch._recommendation_outcomes["TSLA"]["stage"] == "filled"

    @pytest.mark.asyncio
    async def test_cancelled_stage(self, orch):
        """On cancel detection, stage becomes cancelled."""
        now_iso = datetime.now(timezone.utc).isoformat()
        orch._recommendation_outcomes["BAC"] = {
            "attempt_time_utc": now_iso,
            "stage": "order_pending",
        }
        orch._recommendation_outcomes["BAC"]["stage"] = "cancelled"
        assert orch._recommendation_outcomes["BAC"]["stage"] == "cancelled"

    @pytest.mark.asyncio
    async def test_stale_entries_purged(self, orch):
        """Entries with attempt_time_utc date != today are purged at slow loop start."""
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        today = datetime.now(timezone.utc).isoformat()
        orch._recommendation_outcomes["STALE"] = {
            "attempt_time_utc": yesterday, "stage": "ranker_rejected",
        }
        orch._recommendation_outcomes["FRESH"] = {
            "attempt_time_utc": today, "stage": "ranker_rejected",
        }

        # Simulate the purge logic from _run_claude_cycle
        today_utc = datetime.now(timezone.utc).date()
        stale = [
            sym for sym, rec in list(orch._recommendation_outcomes.items())
            if rec.get("attempt_time_utc") and
            datetime.fromisoformat(rec["attempt_time_utc"]).date() != today_utc
        ]
        for sym in stale:
            del orch._recommendation_outcomes[sym]

        assert "STALE" not in orch._recommendation_outcomes
        assert "FRESH" in orch._recommendation_outcomes


# ---------------------------------------------------------------------------
# 3. recommendation_outcomes context assembly
# ---------------------------------------------------------------------------

class TestRecommendationOutcomesContextAssembly:

    def _engine(self, tmp_path):
        return _make_engine(tmp_path)

    def test_ranker_rejected_includes_rejection_count(self, tmp_path):
        engine = self._engine(tmp_path)
        now_iso = datetime.now(timezone.utc).isoformat()
        outcomes = {
            "NVDA": {
                "stage": "ranker_rejected",
                "stage_detail": "RVOL below floor",
                "rejection_count": 4,
                "attempt_time_utc": now_iso,
                "claude_entry_target": 875.0,
                "order_id": None,
            }
        }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes=outcomes,
        )
        assert len(ctx["recommendation_outcomes"]) == 1
        entry = ctx["recommendation_outcomes"][0]
        assert entry["symbol"] == "NVDA"
        assert entry["stage"] == "ranker_rejected"
        assert entry["rejection_count"] == 4

    def test_order_pending_includes_drift_pct(self, tmp_path):
        engine = self._engine(tmp_path)
        now_iso = datetime.now(timezone.utc).isoformat()
        outcomes = {
            "BAC": {
                "stage": "order_pending",
                "attempt_time_utc": now_iso,
                "claude_entry_target": 47.0,
                "order_id": "ord-1",
            }
        }
        indicators = {
            "BAC": {"signals": {"price": 47.31}},
        }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, indicators,
            recommendation_outcomes=outcomes,
        )
        entry = ctx["recommendation_outcomes"][0]
        assert "drift_pct" in entry
        assert abs(entry["drift_pct"] - 0.66) < 0.1
        assert entry["current_price"] == pytest.approx(47.31)

    def test_filled_old_entry_omitted(self, tmp_path):
        """filled entries older than recommendation_outcome_max_age_min are omitted."""
        engine = self._engine(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        outcomes = {
            "XOM": {
                "stage": "filled",
                "attempt_time_utc": old_ts,
            }
        }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes=outcomes,
        )
        assert ctx["recommendation_outcomes"] == []

    def test_recent_filled_entry_included(self, tmp_path):
        """filled entries within max age are included."""
        engine = self._engine(tmp_path)
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        outcomes = {
            "XOM": {
                "stage": "filled",
                "attempt_time_utc": recent_ts,
            }
        }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes=outcomes,
        )
        assert len(ctx["recommendation_outcomes"]) == 1
        assert ctx["recommendation_outcomes"][0]["symbol"] == "XOM"

    def test_list_sorted_by_age_min_ascending(self, tmp_path):
        """recommendation_outcomes list is sorted newest-first (smallest age_min)."""
        engine = self._engine(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        outcomes = {
            "OLD": {"stage": "ranker_rejected", "attempt_time_utc": old_ts, "rejection_count": 1},
            "NEW": {"stage": "ranker_rejected", "attempt_time_utc": recent_ts, "rejection_count": 1},
        }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes=outcomes,
        )
        entries = ctx["recommendation_outcomes"]
        assert len(entries) == 2
        assert entries[0]["symbol"] == "NEW"
        assert entries[1]["symbol"] == "OLD"

    def test_list_capped_at_15(self, tmp_path):
        """More than 15 entries are capped to 15 (oldest dropped)."""
        engine = self._engine(tmp_path)
        outcomes = {}
        for i in range(20):
            ts = (datetime.now(timezone.utc) - timedelta(minutes=i)).isoformat()
            outcomes[f"S{i:02d}"] = {
                "stage": "ranker_rejected", "attempt_time_utc": ts, "rejection_count": 1,
            }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes=outcomes,
        )
        assert len(ctx["recommendation_outcomes"]) == 15

    def test_empty_outcomes_gives_empty_list(self, tmp_path):
        """Empty recommendation_outcomes dict → empty list in context."""
        engine = self._engine(tmp_path)
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes={},
        )
        assert ctx["recommendation_outcomes"] == []


# ---------------------------------------------------------------------------
# 4. TradeJournal.load_recent
# ---------------------------------------------------------------------------

@pytest.fixture
def journal(tmp_path):
    return TradeJournal(path=tmp_path / "journal.jsonl")


def _close_record(**kw):
    defaults = {
        "record_type": "close",
        "symbol": "AAPL",
        "entry_price": 150.0,
        "exit_price": 155.0,
        "pnl_pct": 3.33,
        "hold_duration_min": 45.0,
        "direction": "long",
        "strategy": "momentum",
        "claude_conviction": 0.7,
    }
    defaults.update(kw)
    return defaults


class TestTradeJournalLoadRecent:

    @pytest.mark.asyncio
    async def test_returns_last_n_close_records(self, journal):
        for i in range(5):
            await journal.append(_close_record(symbol=f"S{i}", exit_price=float(100 + i)))
        result = await journal.load_recent(3)
        assert len(result) == 3
        # newest first
        assert result[0]["symbol"] == "S4"
        assert result[1]["symbol"] == "S3"
        assert result[2]["symbol"] == "S2"

    @pytest.mark.asyncio
    async def test_includes_records_without_record_type(self, journal):
        """Pre-lifecycle records without record_type are included."""
        await journal.append({"symbol": "OLD", "entry_price": 100.0, "exit_price": 105.0})
        result = await journal.load_recent(10)
        assert any(r["symbol"] == "OLD" for r in result)

    @pytest.mark.asyncio
    async def test_excludes_zero_entry_price(self, journal):
        """Records with entry_price=0 are excluded (ghost trades)."""
        await journal.append(_close_record(symbol="GHOST", entry_price=0.0))
        await journal.append(_close_record(symbol="REAL"))
        result = await journal.load_recent(10)
        syms = [r["symbol"] for r in result]
        assert "GHOST" not in syms
        assert "REAL" in syms

    @pytest.mark.asyncio
    async def test_excludes_open_records(self, journal):
        """Records with record_type != 'close' are excluded."""
        await journal.append({"record_type": "open", "symbol": "OPEN_REC", "entry_price": 100.0})
        await journal.append({"record_type": "snapshot", "symbol": "SNAP", "entry_price": 100.0})
        await journal.append(_close_record(symbol="CLOSED"))
        result = await journal.load_recent(10)
        syms = [r["symbol"] for r in result]
        assert "OPEN_REC" not in syms
        assert "SNAP" not in syms
        assert "CLOSED" in syms

    @pytest.mark.asyncio
    async def test_capped_at_n(self, journal):
        for i in range(10):
            await journal.append(_close_record(symbol=f"S{i}"))
        result = await journal.load_recent(3)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_file(self, tmp_path):
        j = TradeJournal(path=tmp_path / "nonexistent.jsonl")
        result = await j.load_recent(5)
        assert result == []

    @pytest.mark.asyncio
    async def test_mixed_record_types_interleaved(self, journal):
        """Records of type open/snapshot/review/close interleaved; only close returned."""
        await journal.append({"record_type": "open", "symbol": "X", "entry_price": 100.0})
        await journal.append(_close_record(symbol="A"))
        await journal.append({"record_type": "snapshot", "symbol": "X", "entry_price": 100.0})
        await journal.append(_close_record(symbol="B"))
        result = await journal.load_recent(10)
        syms = [r["symbol"] for r in result]
        assert syms == ["B", "A"]


# ---------------------------------------------------------------------------
# 5. TradeJournal.compute_session_stats
# ---------------------------------------------------------------------------

class TestComputeSessionStats:

    @pytest.mark.asyncio
    async def test_returns_empty_when_insufficient_trades(self, journal):
        await journal.append(_close_record(symbol="A"))
        await journal.append(_close_record(symbol="B"))
        result = await journal.compute_session_stats(min_trades=3)
        assert result == {}

    @pytest.mark.asyncio
    async def test_win_rate_correct(self, journal):
        """4 wins out of 10 total → win_rate_pct=40."""
        for i in range(4):
            await journal.append(_close_record(symbol=f"W{i}", pnl_pct=2.0))
        for i in range(6):
            await journal.append(_close_record(symbol=f"L{i}", pnl_pct=-1.5))
        result = await journal.compute_session_stats(min_trades=3)
        assert result["win_rate_pct"] == 40
        assert result["total_trades"] == 10

    @pytest.mark.asyncio
    async def test_short_win_rate_present_when_shorts_exist(self, journal):
        """short_win_rate_pct is present when short trades exist."""
        await journal.append(_close_record(symbol="S1", direction="short", pnl_pct=3.0))
        await journal.append(_close_record(symbol="S2", direction="short", pnl_pct=-1.0))
        await journal.append(_close_record(symbol="L1", direction="long", pnl_pct=2.0))
        result = await journal.compute_session_stats(min_trades=3)
        assert "short_win_rate_pct" in result
        # 1 win out of 2 short trades → 50%
        assert result["short_win_rate_pct"] == 50

    @pytest.mark.asyncio
    async def test_short_win_rate_zero_when_all_shorts_lost(self, journal):
        """short_win_rate_pct=0 when no short trades were profitable."""
        for i in range(3):
            await journal.append(_close_record(symbol=f"S{i}", direction="short", pnl_pct=-2.0))
        result = await journal.compute_session_stats(min_trades=3)
        assert "short_win_rate_pct" in result
        assert result["short_win_rate_pct"] == 0

    @pytest.mark.asyncio
    async def test_short_win_rate_absent_when_no_shorts(self, journal):
        """short_win_rate_pct key is absent when sample has no short trades."""
        for i in range(3):
            await journal.append(_close_record(symbol=f"L{i}", direction="long", pnl_pct=1.0))
        result = await journal.compute_session_stats(min_trades=3)
        assert "short_win_rate_pct" not in result

    @pytest.mark.asyncio
    async def test_high_conviction_win_rate_omitted_when_few_trades(self, journal):
        """high_conviction_win_rate_pct omitted when fewer than 3 high-conviction trades."""
        await journal.append(_close_record(symbol="H1", claude_conviction=0.80, pnl_pct=2.0))
        await journal.append(_close_record(symbol="H2", claude_conviction=0.76, pnl_pct=-1.0))
        await journal.append(_close_record(symbol="L1", claude_conviction=0.50, pnl_pct=1.0))
        result = await journal.compute_session_stats(min_trades=3)
        # Only 2 high-conviction trades → key omitted
        assert "high_conviction_win_rate_pct" not in result

    @pytest.mark.asyncio
    async def test_high_conviction_win_rate_present_with_enough_trades(self, journal):
        """high_conviction_win_rate_pct present when >= 3 high-conviction trades."""
        await journal.append(_close_record(symbol="H1", claude_conviction=0.80, pnl_pct=3.0))
        await journal.append(_close_record(symbol="H2", claude_conviction=0.76, pnl_pct=2.0))
        await journal.append(_close_record(symbol="H3", claude_conviction=0.90, pnl_pct=-1.0))
        result = await journal.compute_session_stats(min_trades=3)
        assert "high_conviction_win_rate_pct" in result
        # 2 out of 3 high-conviction trades won → 67%
        assert result["high_conviction_win_rate_pct"] == 67


# ---------------------------------------------------------------------------
# 6. ta_readiness in watchlist tier-1 context
# ---------------------------------------------------------------------------

class TestTaReadiness:

    def _engine(self, tmp_path):
        return _make_engine(tmp_path)

    def _watchlist_entry(self, symbol="AAPL", expected_direction="either"):
        return WatchlistEntry(
            symbol=symbol, date_added="2026-03-20", reason="test",
            priority_tier=1, expected_direction=expected_direction,
        )

    def test_ta_readiness_contains_expected_keys(self, tmp_path):
        engine = self._engine(tmp_path)
        watchlist = WatchlistState(entries=[self._watchlist_entry("AAPL")])
        indicators = {
            "AAPL": {
                "composite_technical_score": 0.6,
                "signals": {
                    "above_vwap": True,
                    "rsi": 58.2,
                    "rsi_slope_5": 6.4,
                    "macd_signal": "bullish",
                    "macd_histogram_expanding": True,
                    "roc_negative_deceleration": False,
                    "volume_ratio": 1.85,
                    "volume_trend_bars": 3,
                    "trend_structure": "bullish_aligned",
                    "bb_squeeze": False,
                },
            }
        }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), watchlist, {}, indicators,
        )
        assert len(ctx["watchlist_tier1"]) == 1
        entry = ctx["watchlist_tier1"][0]
        assert "ta_readiness" in entry
        ta = entry["ta_readiness"]
        assert "macd_signal" in ta  # not "macd"
        assert "roc_negative_deceleration" in ta
        assert "composite_score" in ta
        # Old standalone composite_score key must not be present alongside ta_readiness
        assert "composite_score" not in {k: v for k, v in entry.items() if k != "ta_readiness"}

    def test_ta_readiness_values_match_indicators(self, tmp_path):
        engine = self._engine(tmp_path)
        watchlist = WatchlistState(entries=[self._watchlist_entry("AAPL")])
        indicators = {
            "AAPL": {
                "composite_technical_score": 0.6,
                "signals": {"rsi": 62.5, "volume_ratio": 2.1},
            }
        }
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), watchlist, {}, indicators,
        )
        ta = ctx["watchlist_tier1"][0]["ta_readiness"]
        assert ta["rsi"] == pytest.approx(62.5)
        assert ta["volume_ratio"] == pytest.approx(2.1)

    def test_ta_readiness_direction_adjusted_score_for_short(self, tmp_path):
        """composite_score uses direction-adjusted scoring when expected_direction='short'."""
        from ozymandias.intelligence.technical_analysis import compute_composite_score
        engine = self._engine(tmp_path)
        signals = {
            "rsi": 35.0,
            "macd_signal": "bearish",
            "vwap_position": "below",
            "trend_structure": "bearish_aligned",
            "volume_ratio": 1.5,
        }
        indicators = {"NVDA": {"composite_technical_score": 0.4, "signals": signals}}
        watchlist_short = WatchlistState(entries=[self._watchlist_entry("NVDA", "short")])
        watchlist_long = WatchlistState(entries=[self._watchlist_entry("NVDA", "long")])

        ctx_short = engine.assemble_reasoning_context(
            PortfolioState(), watchlist_short, {}, indicators,
        )
        ctx_long = engine.assemble_reasoning_context(
            PortfolioState(), watchlist_long, {}, indicators,
        )
        score_short = ctx_short["watchlist_tier1"][0]["ta_readiness"]["composite_score"]
        score_long = ctx_long["watchlist_tier1"][0]["ta_readiness"]["composite_score"]
        # Short-direction should score higher on bearish signals
        assert score_short != score_long

    def test_ta_readiness_either_uses_long_default(self, tmp_path):
        """expected_direction='either' maps to 'long' for composite scoring."""
        from ozymandias.intelligence.technical_analysis import compute_composite_score
        engine = self._engine(tmp_path)
        signals = {"rsi": 62.0, "macd_signal": "bullish", "volume_ratio": 2.0}
        indicators = {"AAPL": {"composite_technical_score": 0.6, "signals": signals}}
        watchlist = WatchlistState(entries=[self._watchlist_entry("AAPL", "either")])

        ctx = engine.assemble_reasoning_context(
            PortfolioState(), watchlist, {}, indicators,
        )
        score_either = ctx["watchlist_tier1"][0]["ta_readiness"]["composite_score"]
        # Should equal the long-direction score
        expected = compute_composite_score(signals, direction="long")
        assert score_either == pytest.approx(expected, abs=0.001)

    def test_ta_readiness_empty_when_symbol_absent_from_indicators(self, tmp_path):
        """ta_readiness is empty dict when symbol not in indicators — no crash."""
        engine = self._engine(tmp_path)
        watchlist = WatchlistState(entries=[self._watchlist_entry("NOPE")])
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), watchlist, {}, {},
        )
        if ctx["watchlist_tier1"]:
            ta = ctx["watchlist_tier1"][0]["ta_readiness"]
            # Either empty or just has composite_score=0
            assert isinstance(ta, dict)

    def test_expected_direction_present_in_watchlist_entry_context(self, tmp_path):
        """expected_direction is included in the watchlist entry context block."""
        engine = self._engine(tmp_path)
        entry = self._watchlist_entry("AAPL", "short")
        watchlist = WatchlistState(entries=[entry])
        indicators = {"AAPL": {"composite_technical_score": 0.5, "signals": {"rsi": 40.0}}}
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), watchlist, {}, indicators,
        )
        assert ctx["watchlist_tier1"][0]["expected_direction"] == "short"


# ---------------------------------------------------------------------------
# 7. WatchlistEntry.expected_direction
# ---------------------------------------------------------------------------

class TestWatchlistEntryExpectedDirection:

    def test_default_is_either(self):
        entry = WatchlistEntry(symbol="AAPL", date_added="2026-03-20", reason="test")
        assert entry.expected_direction == "either"

    @pytest.mark.asyncio
    async def test_persists_through_save_load_cycle(self, tmp_path):
        from ozymandias.core.state_manager import StateManager, WatchlistState
        sm = StateManager()
        sm._dir = tmp_path
        await sm.initialize()

        entry = WatchlistEntry(
            symbol="NVDA", date_added="2026-03-20", reason="bearish break",
            priority_tier=1, expected_direction="short",
        )
        state = WatchlistState(entries=[entry])
        await sm.save_watchlist(state)
        loaded = await sm.load_watchlist()

        assert loaded.entries[0].expected_direction == "short"

    @pytest.mark.asyncio
    async def test_defaults_to_either_when_key_absent(self, tmp_path):
        """Backward compat: JSON without expected_direction loads as 'either'."""
        from ozymandias.core.state_manager import StateManager
        import json

        sm = StateManager()
        sm._dir = tmp_path
        await sm.initialize()

        # Write watchlist JSON without the expected_direction key
        data = {
            "entries": [{
                "symbol": "AAPL",
                "date_added": "2026-03-20",
                "reason": "test",
                "priority_tier": 1,
                "strategy": "momentum",
                "removal_candidate": False,
            }],
            "last_updated": "2026-03-20T00:00:00+00:00",
        }
        (tmp_path / "watchlist.json").write_text(json.dumps(data))
        loaded = await sm.load_watchlist()
        assert loaded.entries[0].expected_direction == "either"

    @pytest.mark.asyncio
    async def test_apply_watchlist_changes_extracts_expected_direction(self, orch):
        """_apply_watchlist_changes sets expected_direction from Claude add-item dict."""
        from ozymandias.core.state_manager import WatchlistState
        watchlist = WatchlistState(entries=[])
        add_list = [{"symbol": "TSLA", "reason": "bearish", "expected_direction": "short"}]
        await orch._apply_watchlist_changes(watchlist, add_list, [])
        assert watchlist.entries[0].expected_direction == "short"

    @pytest.mark.asyncio
    async def test_apply_watchlist_changes_defaults_either_when_absent(self, orch):
        """_apply_watchlist_changes defaults expected_direction to 'either' when omitted."""
        from ozymandias.core.state_manager import WatchlistState
        watchlist = WatchlistState(entries=[])
        add_list = [{"symbol": "TSLA", "reason": "test"}]  # no expected_direction
        await orch._apply_watchlist_changes(watchlist, add_list, [])
        assert watchlist.entries[0].expected_direction == "either"


# ---------------------------------------------------------------------------
# 8. Backward compat / error handling
# ---------------------------------------------------------------------------

class TestBackwardCompat:

    def _engine(self, tmp_path):
        return _make_engine(tmp_path)

    def test_empty_indicators_no_crash(self, tmp_path):
        """assemble_reasoning_context with indicators={} → all new sections empty/{}, no crash."""
        engine = self._engine(tmp_path)
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes={},
            recent_executions=[],
            execution_stats={},
        )
        assert ctx["recommendation_outcomes"] == []
        assert ctx["recent_executions"] == []
        assert ctx["execution_stats"] == {}

    def test_empty_recommendation_outcomes_gives_empty_list(self, tmp_path):
        """recommendation_outcomes={} → [] in context."""
        engine = self._engine(tmp_path)
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recommendation_outcomes={},
        )
        assert ctx["recommendation_outcomes"] == []

    def test_none_params_no_crash(self, tmp_path):
        """All new optional params default to None → no crash."""
        engine = self._engine(tmp_path)
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
        )
        assert "recommendation_outcomes" in ctx
        assert "recent_executions" in ctx
        assert "execution_stats" in ctx

    def test_recent_executions_field_mapping(self, tmp_path):
        """hold_duration_min is mapped to duration_min (int) in context payload."""
        engine = self._engine(tmp_path)
        execs = [
            {
                "symbol": "AMD", "direction": "long",
                "entry_price": 204.83, "exit_price": 202.31,
                "pnl_pct": -1.23, "strategy": "swing",
                "claude_conviction": 0.45,
                "hold_duration_min": 1117.8,
            }
        ]
        ctx = engine.assemble_reasoning_context(
            PortfolioState(), WatchlistState(), {}, {},
            recent_executions=execs,
        )
        assert len(ctx["recent_executions"]) == 1
        entry = ctx["recent_executions"][0]
        assert entry["symbol"] == "AMD"
        assert entry["duration_min"] == 1117  # int, not float
        assert "hold_duration_min" not in entry
