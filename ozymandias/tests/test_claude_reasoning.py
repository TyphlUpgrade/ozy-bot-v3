"""
Tests for ClaudeReasoningEngine.

All Anthropic API calls are mocked — no real API calls in tests.
ReasoningCache is given a temp directory so tests don't pollute production cache.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        # After trimming, must be under token target
        from ozymandias.intelligence.claude_reasoning import _TOKEN_TARGET_MAX, _CHARS_PER_TOKEN
        assert len(ctx_json) <= _TOKEN_TARGET_MAX * _CHARS_PER_TOKEN

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

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_proceed_true(self, tmp_path):
        engine = _engine(tmp_path)
        challenge_resp = json.dumps({
            "proceed": True,
            "conviction": 0.80,
            "challenge_reasoning": "Watch for rejection at $212 gap resistance.",
        })
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(challenge_resp)
            result = await engine.run_thesis_challenge(
                self._make_opportunity(), _market_data(), _indicators(["AAPL"])
            )
        assert result is not None
        assert result["proceed"] is True
        assert result["conviction"] == pytest.approx(0.80)

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_proceed_false(self, tmp_path):
        engine = _engine(tmp_path)
        challenge_resp = json.dumps({
            "proceed": False,
            "conviction": 0.20,
            "challenge_reasoning": "Failed breakout — price closed back below $198 VWAP reclaim.",
        })
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response(challenge_resp)
            result = await engine.run_thesis_challenge(
                self._make_opportunity(), _market_data(), {}
            )
        assert result is not None
        assert result["proceed"] is False

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_unparseable_returns_none(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
            m.return_value = _mock_api_response("this is not json at all")
            result = await engine.run_thesis_challenge(
                self._make_opportunity(), _market_data(), {}
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_run_thesis_challenge_loads_template(self, tmp_path):
        engine = _engine(tmp_path)
        with patch.object(engine, "_load_prompt", wraps=engine._load_prompt) as mock_load:
            with patch.object(engine._client.messages, "create", new_callable=AsyncMock) as m:
                m.return_value = _mock_api_response('{"proceed": true, "conviction": 0.9, "challenge_reasoning": "ok"}')
                await engine.run_thesis_challenge(
                    self._make_opportunity(), _market_data(), {}
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
