"""
Tests for ClaudeReasoningEngine.

All Anthropic API calls are mocked — no real API calls in tests.
ReasoningCache is given a temp directory so tests don't pollute production cache.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from ozymandias.core.config import ClaudeConfig, Config, RiskConfig
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.core.state_manager import (
    ExitTargets,
    PortfolioState,
    Position,
    TradeIntention,
    WatchlistEntry,
    WatchlistState,
)
from ozymandias.intelligence.claude_reasoning import (
    ClaudeReasoningEngine,
    ReasoningResult,
    ReviewResult,
    WatchlistResult,
    _make_technical_summary,
    parse_claude_response,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _config() -> Config:
    cfg = Config()
    cfg.claude.model = "claude-sonnet-4-6"
    cfg.claude.max_tokens_per_cycle = 4096
    cfg.claude.tier1_max_symbols = 12
    return cfg


def _prompts_dir() -> Path:
    """Return the real prompts directory so templates can be loaded."""
    here = Path(__file__).resolve().parent.parent
    return here / "config" / "prompts" / "v3.3.0"


def _cache(tmp_path: Path) -> ReasoningCache:
    return ReasoningCache(cache_dir=tmp_path)


def _engine(tmp_path: Path) -> ClaudeReasoningEngine:
    cfg = _config()
    cache = _cache(tmp_path)
    return ClaudeReasoningEngine(cfg, cache=cache, prompts_dir=_prompts_dir())


def _portfolio(n_positions: int = 0) -> PortfolioState:
    positions = []
    for i in range(n_positions):
        sym = f"SYM{i}"
        positions.append(Position(
            symbol=sym,
            shares=10.0,
            avg_cost=100.0,
            entry_date="2026-03-10",
            intention=TradeIntention(
                catalyst="Test catalyst",
                direction="long",
                strategy="momentum",
                expected_move="+5% over 3 days",
                reasoning="Strong momentum setup",
                exit_targets=ExitTargets(profit_target=110.0, stop_loss=95.0),
                max_expected_loss=-50.0,
                entry_date="2026-03-10",
            ),
            position_id=f"pos_{sym}",
        ))
    return PortfolioState(cash=50_000.0, buying_power=50_000.0, positions=positions)


def _watchlist(symbols: list[str], tier: int = 1) -> WatchlistState:
    entries = [
        WatchlistEntry(
            symbol=sym,
            date_added="2026-03-01",
            reason="Strong technical setup",
            priority_tier=tier,
            strategy="momentum",
        )
        for sym in symbols
    ]
    return WatchlistState(entries=entries)


def _market_data() -> dict:
    return {
        "spy_trend": "bullish, above 50 SMA",
        "vix": 16.5,
        "sector_rotation": "tech outperforming",
        "macro_events_today": ["Fed speakers at 2pm"],
        "trading_session": "regular_hours",
        "pdt_trades_remaining": 2,
    }


def _indicators(symbols: list[str] | None = None) -> dict:
    ind = {}
    for sym in (symbols or []):
        ind[sym] = {
            "signals": {
                "vwap_position": "above",
                "rsi": 62.0,
                "macd_signal": "bullish",
                "trend_structure": "bullish_aligned",
                "roc_5": 1.5,
                "volume_ratio": 1.2,
                "atr_14": 2.5,
                "price": 105.0,
            },
            "composite_technical_score": 0.72,
        }
    return ind


def _mock_api_response(text: str):
    """Build a mock Anthropic API response object."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage.input_tokens = 1200
    msg.usage.output_tokens = 400
    return msg


_VALID_REASONING_RESPONSE = json.dumps({
    "timestamp": "2026-03-11T14:30:00Z",
    "position_reviews": [],
    "new_opportunities": [
        {
            "symbol": "TSLA",
            "action": "buy",
            "strategy": "momentum",
            "timeframe": "short_term",
            "conviction": 0.75,
            "reasoning": "Strong momentum",
            "suggested_entry": 244.0,
            "suggested_exit": 268.0,
            "suggested_stop": 235.0,
            "position_size_pct": 0.10,
        }
    ],
    "watchlist_changes": {"add": [], "remove": [], "rationale": ""},
    "market_assessment": "Bullish bias.",
    "risk_flags": [],
})

_VALID_WATCHLIST_RESPONSE = json.dumps({
    "watchlist": [
        {"symbol": "NVDA", "reason": "AI momentum", "priority_tier": 1, "strategy": "momentum"},
        {"symbol": "TSLA", "reason": "EV demand", "priority_tier": 2, "strategy": "swing"},
    ],
    "market_notes": "Tech sector outperforming.",
})

_VALID_REVIEW_RESPONSE = json.dumps({
    "reviews": [
        {
            "symbol": "AAPL",
            "thesis_intact": True,
            "thesis_assessment": "Catalyst still in play.",
            "recommended_action": "hold",
            "adjusted_targets": None,
            "notes": "Momentum continues.",
        }
    ]
})


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

class TestAssembleReasoningContext:
    def test_empty_portfolio_and_watchlist(self, tmp_path):
        engine = _engine(tmp_path)
        ctx = engine.assemble_reasoning_context(
            _portfolio(), _watchlist([]), _market_data(), {}
        )
        assert ctx["portfolio"]["positions"] == []
        assert ctx["watchlist_tier1"] == []
        assert ctx["market_context"]["vix"] == 16.5

    def test_positions_always_included(self, tmp_path):
        engine = _engine(tmp_path)
        portfolio = _portfolio(n_positions=3)
        ctx = engine.assemble_reasoning_context(
            portfolio, _watchlist([]), _market_data(), _indicators(["SYM0", "SYM1", "SYM2"])
        )
        assert len(ctx["portfolio"]["positions"]) == 3
        assert ctx["portfolio"]["positions"][0]["symbol"] == "SYM0"

    def test_tier1_watchlist_fills_remaining_budget(self, tmp_path):
        engine = _engine(tmp_path)
        # 2 positions + up to 10 watchlist = 12 tier1 max
        portfolio = _portfolio(n_positions=2)
        watch = _watchlist([f"W{i}" for i in range(15)], tier=1)
        ctx = engine.assemble_reasoning_context(
            portfolio, watch, _market_data(), {}
        )
        # 2 position slots used → 10 watchlist slots remaining
        assert len(ctx["watchlist_tier1"]) == 10

    def test_tier1_limit_respected(self, tmp_path):
        engine = _engine(tmp_path)
        watch = _watchlist([f"W{i}" for i in range(20)], tier=1)
        ctx = engine.assemble_reasoning_context(
            _portfolio(), watch, _market_data(), {}
        )
        assert len(ctx["watchlist_tier1"]) <= engine._claude_cfg.tier1_max_symbols

    def test_tier2_watchlist_excluded(self, tmp_path):
        engine = _engine(tmp_path)
        # All entries are tier 2 — none should appear in tier1
        watch = _watchlist(["AAPL", "MSFT"], tier=2)
        ctx = engine.assemble_reasoning_context(
            _portfolio(), watch, _market_data(), {}
        )
        assert ctx["watchlist_tier1"] == []

    def test_price_from_indicators_included_in_position(self, tmp_path):
        engine = _engine(tmp_path)
        portfolio = _portfolio(n_positions=1)
        sym = "SYM0"
        ind = {sym: {"signals": {"price": 110.0}, "composite_technical_score": 0.65}}
        ctx = engine.assemble_reasoning_context(portfolio, _watchlist([]), _market_data(), ind)
        pos = ctx["portfolio"]["positions"][0]
        assert pos["current_price"] == 110.0
        # unrealized_pnl = (110 - 100) × 10 = 100
        assert pos["unrealized_pnl"] == 100.0

    def test_context_under_token_target_for_typical_input(self, tmp_path):
        engine = _engine(tmp_path)
        portfolio = _portfolio(n_positions=3)
        watch = _watchlist([f"W{i}" for i in range(9)], tier=1)
        ind = _indicators(["SYM0", "SYM1", "SYM2"] + [f"W{i}" for i in range(9)])
        ctx = engine.assemble_reasoning_context(portfolio, watch, _market_data(), ind)
        ctx_json = json.dumps(ctx, default=str)
        # Should be well under 8000-token ceiling (~32000 chars)
        assert len(ctx_json) < 32_000

    def test_oversized_context_trimmed(self, tmp_path):
        engine = _engine(tmp_path)
        # Force an oversized scenario: many watchlist entries with verbose data
        large_watch = WatchlistState(entries=[
            WatchlistEntry(
                symbol=f"TICK{i:04d}",
                date_added="2026-03-01",
                reason="X" * 500,   # verbose reason to inflate size
                priority_tier=1,
                strategy="momentum",
            )
            for i in range(50)
        ])
        ctx = engine.assemble_reasoning_context(
            _portfolio(), large_watch, _market_data(), {}
        )
        ctx_json = json.dumps(ctx, default=str)
        # After trimming, context must fit within context_token_budget (total budget − template tokens).
        from ozymandias.intelligence.claude_reasoning import _TOTAL_TOKEN_BUDGET, _CHARS_PER_TOKEN
        context_budget = _TOTAL_TOKEN_BUDGET - engine._prompt_template_tokens
        assert len(ctx_json) // _CHARS_PER_TOKEN <= context_budget

    def test_market_data_passed_through_unchanged(self, tmp_path):
        engine = _engine(tmp_path)
        md = _market_data()
        ctx = engine.assemble_reasoning_context(_portfolio(), _watchlist([]), md, {})
        assert ctx["market_context"] == md


# ---------------------------------------------------------------------------
# call_claude — mock API
# ---------------------------------------------------------------------------

class TestCallClaude:
    @pytest.mark.asyncio
    async def test_returns_raw_text_on_success(self, tmp_path):
        engine = _engine(tmp_path)
        template = "Say hello: {name}"
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = _mock_api_response("Hello!")
            result = await engine.call_claude(template, {"name": "world"})
        assert result == "Hello!"

    @pytest.mark.asyncio
    async def test_token_usage_recorded(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = _mock_api_response("ok")
            await engine.call_claude("{x}", {"x": "test"})
        assert engine._last_input_tokens == 1200
        assert engine._last_output_tokens == 400

    @pytest.mark.asyncio
    async def test_missing_template_key_raises_value_error(self, tmp_path):
        engine = _engine(tmp_path)
        with pytest.raises(ValueError, match="missing placeholder key"):
            await engine.call_claude("Hello {missing_key}", {})

    @pytest.mark.asyncio
    async def test_rate_limit_retried_with_backoff(self, tmp_path):
        engine = _engine(tmp_path)
        import anthropic as ant

        call_count = 0
        async def flaky_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ant.RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429, headers={}),
                    body={},
                )
            return _mock_api_response("success after retries")

        with patch.object(engine._client.messages, "create", side_effect=flaky_create):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await engine.call_claude("{x}", {"x": "test"})

        assert result == "success after retries"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_server_error_5xx_retried(self, tmp_path):
        engine = _engine(tmp_path)
        import anthropic as ant

        calls = 0
        async def flaky(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ant.APIStatusError(
                    message="server error",
                    response=MagicMock(status_code=503, headers={}),
                    body={},
                )
            return _mock_api_response("recovered")

        with patch.object(engine._client.messages, "create", side_effect=flaky):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await engine.call_claude("{x}", {"x": "test"})

        assert result == "recovered"

    @pytest.mark.asyncio
    async def test_client_error_4xx_not_retried(self, tmp_path):
        engine = _engine(tmp_path)
        import anthropic as ant

        calls = 0
        async def bad_auth(**kwargs):
            nonlocal calls
            calls += 1
            raise ant.APIStatusError(
                message="unauthorized",
                response=MagicMock(status_code=401, headers={}),
                body={},
            )

        with patch.object(engine._client.messages, "create", side_effect=bad_auth):
            with pytest.raises(ant.APIStatusError):
                await engine.call_claude("{x}", {"x": "test"})

        assert calls == 1  # not retried


# ---------------------------------------------------------------------------
# run_reasoning_cycle
# ---------------------------------------------------------------------------

class TestRunReasoningCycle:
    @pytest.mark.asyncio
    async def test_successful_cycle_returns_result(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(_VALID_REASONING_RESPONSE)
            result = await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}, trigger="test"
            )
        assert isinstance(result, ReasoningResult)
        assert result.new_opportunities[0]["symbol"] == "TSLA"
        assert result.market_assessment == "Bullish bias."

    @pytest.mark.asyncio
    async def test_malformed_response_returns_none(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response("this is not json")
            result = await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_result_saved_to_cache(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(_VALID_REASONING_RESPONSE)
            await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}
            )
        # Cache directory should now contain a file
        cache_files = list(tmp_path.glob("reasoning_*.json"))
        assert len(cache_files) == 1

    @pytest.mark.asyncio
    async def test_fresh_cache_used_on_startup(self, tmp_path):
        engine = _engine(tmp_path)

        # Pre-populate cache with a valid response
        engine._cache.save(
            trigger="price_move",
            input_context={},
            raw_response=_VALID_REASONING_RESPONSE,
            parsed_response=json.loads(_VALID_REASONING_RESPONSE),
            input_tokens=1000,
            output_tokens=300,
        )

        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            result = await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}, skip_cache=False
            )
            # API should NOT have been called
            m.assert_not_called()

        assert isinstance(result, ReasoningResult)
        assert result.new_opportunities[0]["symbol"] == "TSLA"

    @pytest.mark.asyncio
    async def test_skip_cache_forces_api_call(self, tmp_path):
        engine = _engine(tmp_path)

        # Pre-populate cache
        engine._cache.save(
            trigger="test",
            input_context={},
            raw_response=_VALID_REASONING_RESPONSE,
            parsed_response=json.loads(_VALID_REASONING_RESPONSE),
        )

        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(_VALID_REASONING_RESPONSE)
            await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}, skip_cache=True
            )
            m.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_parse_still_saved_to_cache(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response("not json")
            await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}
            )
        files = list(tmp_path.glob("reasoning_*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            record = json.load(f)
        assert record["parse_success"] is False
        assert record["parsed_response"] is None


# ---------------------------------------------------------------------------
# run_watchlist_build
# ---------------------------------------------------------------------------

class TestRunWatchlistBuild:
    @pytest.mark.asyncio
    async def test_successful_watchlist_build(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(_VALID_WATCHLIST_RESPONSE)
            result = await engine.run_watchlist_build(
                _market_data(), _watchlist([]), target_count=20
            )
        assert isinstance(result, WatchlistResult)
        assert len(result.watchlist) == 2
        assert result.watchlist[0]["symbol"] == "NVDA"
        assert result.market_notes == "Tech sector outperforming."

    @pytest.mark.asyncio
    async def test_malformed_watchlist_response_returns_none(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response("bad output")
            result = await engine.run_watchlist_build(_market_data(), _watchlist([]))
        assert result is None


# ---------------------------------------------------------------------------
# run_position_review
# ---------------------------------------------------------------------------

class TestRunPositionReview:
    def _make_position(self) -> Position:
        return Position(
            symbol="AAPL",
            shares=10.0,
            avg_cost=175.0,
            entry_date="2026-03-10",
            intention=TradeIntention(
                catalyst="iPhone supercycle",
                direction="long",
                strategy="swing",
                expected_move="+8% over 2 weeks",
                reasoning="Strong institutional buying",
                exit_targets=ExitTargets(profit_target=190.0, stop_loss=168.0),
                max_expected_loss=-70.0,
                entry_date="2026-03-10",
                review_notes=["Initial entry confirmed"],
            ),
            position_id="pos_AAPL",
        )

    @pytest.mark.asyncio
    async def test_successful_review_returns_result(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(_VALID_REVIEW_RESPONSE)
            result = await engine.run_position_review(
                self._make_position(), _market_data(),
                _indicators(["AAPL"])
            )
        assert isinstance(result, ReviewResult)
        assert result.reviews[0]["symbol"] == "AAPL"
        assert result.reviews[0]["recommended_action"] == "hold"

    @pytest.mark.asyncio
    async def test_malformed_review_response_returns_none(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response("not valid json")
            result = await engine.run_position_review(
                self._make_position(), _market_data(), {}
            )
        assert result is None


# ---------------------------------------------------------------------------
# Technical summary helper
# ---------------------------------------------------------------------------

class TestMakeTechnicalSummary:
    def test_full_signals(self):
        signals = {
            "vwap_position": "above",
            "rsi": 62.5,
            "macd_signal": "bullish_cross",
            "trend_structure": "bullish_aligned",
            "roc_5": 1.5,
            "volume_ratio": 1.3,
        }
        summary = _make_technical_summary(signals)
        assert "VWAP above" in summary
        assert "RSI 62" in summary
        assert "MACD bullish cross" in summary
        assert "ROC +1.5%" in summary

    def test_empty_signals(self):
        assert _make_technical_summary({}) == "no indicator data"

    def test_partial_signals_no_crash(self):
        # Only some fields present — should not crash
        result = _make_technical_summary({"rsi": 45.0})
        assert "RSI 45" in result


# ---------------------------------------------------------------------------
# rejected_opportunities in ReasoningResult
# ---------------------------------------------------------------------------

class TestRejectedOpportunities:

    @pytest.mark.asyncio
    async def test_reasoning_result_has_rejected_opportunities(self, tmp_path):
        engine = _engine(tmp_path)
        response = json.dumps({
            **json.loads(_VALID_REASONING_RESPONSE),
            "rejected_opportunities": [
                {
                    "symbol": "NVDA",
                    "considered_reason": "Strong momentum setup on daily.",
                    "rejection_reason": "Overhead resistance at $875 from Feb gap-fill.",
                }
            ],
        })
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(response)
            result = await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}, trigger="test"
            )
        assert len(result.rejected_opportunities) == 1
        assert result.rejected_opportunities[0]["symbol"] == "NVDA"

    @pytest.mark.asyncio
    async def test_rejected_opportunities_defaults_to_empty(self, tmp_path):
        engine = _engine(tmp_path)
        # _VALID_REASONING_RESPONSE has no rejected_opportunities key
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(_VALID_REASONING_RESPONSE)
            result = await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}, trigger="test"
            )
        assert result.rejected_opportunities == []

    @pytest.mark.asyncio
    async def test_position_review_updated_reasoning_passthrough(self, tmp_path):
        """updated_reasoning with adversarial text passes through unchanged."""
        engine = _engine(tmp_path)
        adversarial_text = (
            "Holding: momentum still intact BUT overhead resistance at $312 "
            "from March gap-fill could cap upside."
        )
        response = json.dumps({
            **json.loads(_VALID_REASONING_RESPONSE),
            "position_reviews": [
                {
                    "symbol": "AAPL",
                    "action": "hold",
                    "thesis_intact": True,
                    "updated_reasoning": adversarial_text,
                    "adjusted_targets": None,
                }
            ],
        })
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(response)
            result = await engine.run_reasoning_cycle(
                _portfolio(), _watchlist([]), _market_data(), {}, trigger="test"
            )
        assert result.raw["position_reviews"][0]["updated_reasoning"] == adversarial_text


# ---------------------------------------------------------------------------
# run_thesis_challenge
# ---------------------------------------------------------------------------

class TestRunThesisChallenge:

    def _make_opportunity(self, symbol="AAPL") -> dict:
        return {
            "symbol": symbol,
            "strategy": "momentum",
            "conviction": 0.85,
            "suggested_entry": 200.0,
            "suggested_exit": 220.0,
            "suggested_stop": 190.0,
            "position_size_pct": 0.18,
            "reasoning": "Strong breakout above key resistance.",
        }

    def _portfolio_summary(self, n: int = 0) -> dict:
        return {
            "open_positions": [{"symbol": f"SYM{i}", "direction": "long"} for i in range(n)],
            "position_count": n,
        }

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_no_concerns(self, tmp_path):
        """concern_level=0.0 → no penalty, trade proceeds at full size."""
        engine = _engine(tmp_path)
        challenge_resp = json.dumps({
            "concern_level": 0.0,
            "reasoning": "no material concerns",
        })
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(challenge_resp)
            result = await engine.run_thesis_challenge(
                self._make_opportunity(), _market_data(), self._portfolio_summary()
            )
        assert result is not None
        assert result["concern_level"] == pytest.approx(0.0)
        assert "reasoning" in result

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_moderate_concern(self, tmp_path):
        """concern_level between 0 and 1 is returned verbatim."""
        engine = _engine(tmp_path)
        challenge_resp = json.dumps({
            "concern_level": 0.45,
            "reasoning": "Earnings in 2 days creates binary risk for this momentum trade.",
        })
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(challenge_resp)
            result = await engine.run_thesis_challenge(
                self._make_opportunity(), _market_data(), self._portfolio_summary(2)
            )
        assert result is not None
        assert result["concern_level"] == pytest.approx(0.45)

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_unparseable_returns_none(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response("this is not json at all")
            result = await engine.run_thesis_challenge(
                self._make_opportunity(), _market_data(), self._portfolio_summary()
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_empty_portfolio_calls_claude(self, tmp_path):
        """Empty portfolio dict is valid — Claude is still called (no indicators guard removed)."""
        engine = _engine(tmp_path)
        challenge_resp = json.dumps({"concern_level": 0.0, "reasoning": "no concerns"})
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(challenge_resp)
            result = await engine.run_thesis_challenge(
                self._make_opportunity(), _market_data(), {}
            )
        assert result is not None
        m.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_loads_template(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine, "_load_prompt", wraps=engine._load_prompt) as mock_load:
            with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
                m.return_value = _mock_api_response('{"concern_level": 0.1, "reasoning": "minor drift risk"}')
                await engine.run_thesis_challenge(
                    self._make_opportunity(), _market_data(), self._portfolio_summary()
                )
        mock_load.assert_called_once_with("thesis_challenge.txt")

    @pytest.mark.asyncio
    async def test_call_claude_logs_warning_on_max_tokens_stop_reason(self, tmp_path, caplog):
        import logging
        engine = _engine(tmp_path)
        resp = _mock_api_response("hello")
        resp.stop_reason = "max_tokens"
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = resp
            with caplog.at_level(logging.WARNING, logger="ozymandias.intelligence.claude_reasoning"):
                await engine.call_claude("{x}", {"x": "test"})
        assert any("max_tokens" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# AI Fallback (Gemini)
# ---------------------------------------------------------------------------

class TestAIFallback:
    """Tests for the multi-provider fallback: 529 fast retries → Gemini Flash."""

    def _engine_with_fallback(self, tmp_path: Path) -> ClaudeReasoningEngine:
        cfg = _config()
        cfg.ai_fallback.enabled = True
        cfg.ai_fallback.overload_retries = 3
        cfg.ai_fallback.overload_base_sec = 0.0   # no sleeping in tests
        cfg.ai_fallback.overload_max_sec = 0.0
        cfg.ai_fallback.circuit_breaker_threshold = 3
        cfg.ai_fallback.circuit_breaker_probe_min = 10
        return ClaudeReasoningEngine(cfg, cache=_cache(tmp_path), prompts_dir=_prompts_dir())

    def _overload_error(self):
        return anthropic.APIStatusError(
            message="overloaded",
            response=MagicMock(status_code=529, headers={}),
            body={},
        )

    def _mock_gemini_response(self, text: str):
        resp = MagicMock()
        resp.text = text
        return resp

    @pytest.mark.asyncio
    async def test_sdk_max_retries_is_zero(self, tmp_path):
        engine = self._engine_with_fallback(tmp_path)
        assert engine._client.max_retries == 0

    @pytest.mark.asyncio
    async def test_529_retries_then_falls_back_to_gemini(self, tmp_path):
        """3 fast 529 retries → 4th attempt triggers Gemini fallback."""
        engine = self._engine_with_fallback(tmp_path)
        gemini_text = '{"fallback": true}'

        overload = self._overload_error()
        mock_gemini_resp = self._mock_gemini_response(gemini_text)

        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = [overload, overload, overload, overload]
            with patch.object(engine, "_call_gemini_fallback", new_callable=AsyncMock) as mock_gemini:
                mock_gemini.return_value = gemini_text
                result = await engine.call_claude("{x}", {"x": "val"})

        assert result == gemini_text
        assert mock_claude.call_count == 4  # 3 retries + 1 that triggers fallback
        mock_gemini.assert_called_once()
        assert engine._overload_fallback_count == 1

    @pytest.mark.asyncio
    async def test_529_recovers_on_second_retry(self, tmp_path):
        """Claude recovers on retry 2 → no fallback, circuit breaker stays at 0."""
        engine = self._engine_with_fallback(tmp_path)
        overload = self._overload_error()
        success_resp = _mock_api_response('{"ok": true}')

        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = [overload, overload, success_resp]
            with patch.object(engine, "_call_gemini_fallback", new_callable=AsyncMock) as mock_gemini:
                result = await engine.call_claude("{x}", {"x": "val"})

        assert result == '{"ok": true}'
        mock_gemini.assert_not_called()
        assert engine._overload_fallback_count == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_skips_claude_when_threshold_reached(self, tmp_path):
        """After 3 consecutive fallbacks, next call goes straight to Gemini without trying Claude."""
        engine = self._engine_with_fallback(tmp_path)
        engine._overload_fallback_count = 3
        engine._circuit_broken_since = time.monotonic()

        gemini_text = '{"from_gemini": true}'
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_claude:
            with patch.object(engine, "_call_gemini_fallback", new_callable=AsyncMock) as mock_gemini:
                mock_gemini.return_value = gemini_text
                result = await engine.call_claude("{x}", {"x": "val"})

        mock_claude.assert_not_called()
        mock_gemini.assert_called_once()
        assert result == gemini_text

    @pytest.mark.asyncio
    async def test_circuit_breaker_probes_claude_after_interval(self, tmp_path):
        """After probe interval, Claude is tried once; if it succeeds circuit resets."""
        engine = self._engine_with_fallback(tmp_path)
        engine._overload_fallback_count = 3
        # Set broken_since far enough in the past to trigger a probe
        engine._circuit_broken_since = time.monotonic() - (engine._cfg.ai_fallback.circuit_breaker_probe_min * 60 + 1)

        success_resp = _mock_api_response('{"probe_ok": true}')
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = success_resp
            with patch.object(engine, "_call_gemini_fallback", new_callable=AsyncMock) as mock_gemini:
                result = await engine.call_claude("{x}", {"x": "val"})

        mock_claude.assert_called_once()
        mock_gemini.assert_not_called()
        assert result == '{"probe_ok": true}'
        assert engine._overload_fallback_count == 0  # reset on success

    @pytest.mark.asyncio
    async def test_fallback_disabled_raises_on_exhausted_529(self, tmp_path):
        """When fallback is disabled, exhausting overload retries re-raises the error."""
        engine = self._engine_with_fallback(tmp_path)
        engine._cfg.ai_fallback.enabled = False

        overload = self._overload_error()
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = [overload, overload, overload, overload]
            with pytest.raises(Exception):
                await engine.call_claude("{x}", {"x": "val"})

    @pytest.mark.asyncio
    async def test_gemini_key_missing_raises_runtime_error(self, tmp_path, monkeypatch):
        """_call_gemini_fallback raises RuntimeError when GEMINI_API_KEY is absent."""
        engine = self._engine_with_fallback(tmp_path)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        # Mock genai import to avoid requiring the package to test key-missing path
        import sys
        mock_genai = MagicMock()
        mock_genai.configure = MagicMock()
        mock_genai.GenerativeModel = MagicMock(side_effect=RuntimeError("should not reach model init"))
        with patch.dict(sys.modules, {"google.generativeai": mock_genai}):
            with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
                await engine._call_gemini_fallback("test prompt", 1024)

    @pytest.mark.asyncio
    async def test_other_5xx_uses_slow_retry_not_gemini(self, tmp_path):
        """Non-529 server errors (500) use slow backoff and never trigger Gemini fallback."""
        engine = self._engine_with_fallback(tmp_path)
        cfg = engine._cfg.ai_fallback
        cfg.server_error_base_sec = 0.0  # no sleeping
        cfg.server_error_max_sec = 0.0

        server_err = anthropic.APIStatusError(
            message="internal server error",
            response=MagicMock(status_code=500, headers={}),
            body={},
        )
        success_resp = _mock_api_response('{"ok": true}')

        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = [server_err, server_err, success_resp]
            with patch.object(engine, "_call_gemini_fallback", new_callable=AsyncMock) as mock_gemini:
                result = await engine.call_claude("{x}", {"x": "val"})

        mock_gemini.assert_not_called()
        assert result == '{"ok": true}'
