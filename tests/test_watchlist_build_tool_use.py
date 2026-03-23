"""
tests/test_watchlist_build_tool_use.py
========================================
Unit tests for call_claude_with_tools and the updated run_watchlist_build.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ozymandias.core.config import load_config
from ozymandias.core.state_manager import WatchlistState
from ozymandias.intelligence.claude_reasoning import ClaudeReasoningEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(tmp_path) -> ClaudeReasoningEngine:
    """Build a ClaudeReasoningEngine with a minimal prompts dir."""
    cfg = load_config()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "watchlist.txt").write_text(
        "DATE:{current_date} CTX:{market_context} WL:{current_watchlist} "
        "N:{target_count} CANDS:{candidates}"
    )
    (prompts_dir / "reasoning.txt").write_text("dummy")
    return ClaudeReasoningEngine(config=cfg, prompts_dir=prompts_dir)


def _watchlist_json() -> str:
    return json.dumps({"watchlist": [{"symbol": "AAPL", "reason": "momentum", "priority_tier": 1, "strategy": "momentum"}], "market_notes": "ok"})


def _make_tool_use_response(tool_name: str, tool_input: dict, tool_use_id: str = "tu_001"):
    """Build a mock Anthropic response that requests a tool call."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    block.id = tool_use_id

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [block]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)
    return response


def _make_text_response(text: str):
    """Build a mock Anthropic response with a text block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    block.id = "msg_001"

    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [block]
    response.usage = MagicMock(input_tokens=100, output_tokens=80)
    return response


# ---------------------------------------------------------------------------
# Tests: call_claude_with_tools
# ---------------------------------------------------------------------------

class TestCallClaudeWithTools:

    @pytest.mark.asyncio
    async def test_tool_use_round_then_text_response(self, tmp_path):
        engine = _make_engine(tmp_path)
        tool_response = _make_tool_use_response("web_search", {"query": "NVIDIA earnings"})
        text_response = _make_text_response(_watchlist_json())

        call_count = 0

        async def _fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return tool_response
            return text_response

        engine._client.messages.create = _fake_create

        tool_executor = AsyncMock(return_value="Search result text")
        result = await engine.call_claude_with_tools(
            "DATE:{current_date} CTX:{market_context} WL:{current_watchlist} N:{target_count} CANDS:{candidates}",
            {"current_date": "2026-03-23", "market_context": "{}", "current_watchlist": "none", "target_count": 20, "candidates": "none"},
            tools=[engine._WEB_SEARCH_TOOL],
            tool_executor=tool_executor,
            max_tool_rounds=3,
        )

        assert _watchlist_json() in result
        tool_executor.assert_called_once_with("web_search", {"query": "NVIDIA earnings"})

    @pytest.mark.asyncio
    async def test_rounds_exhausted_forces_final_call(self, tmp_path):
        engine = _make_engine(tmp_path)
        # Always returns tool_use, never end_turn
        tool_response = _make_tool_use_response("web_search", {"query": "q"})
        text_response = _make_text_response(_watchlist_json())

        call_count = 0

        async def _fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            # After max_tool_rounds calls, final forced call has tool_choice=none
            if kwargs.get("tool_choice") == {"type": "none"} or not kwargs.get("tools"):
                return text_response
            return tool_response

        engine._client.messages.create = _fake_create

        tool_executor = AsyncMock(return_value="results")
        result = await engine.call_claude_with_tools(
            "DATE:{current_date} CTX:{market_context} WL:{current_watchlist} N:{target_count} CANDS:{candidates}",
            {"current_date": "2026-03-23", "market_context": "{}", "current_watchlist": "none", "target_count": 20, "candidates": "none"},
            tools=[engine._WEB_SEARCH_TOOL],
            tool_executor=tool_executor,
            max_tool_rounds=2,
        )

        assert isinstance(result, str)
        # Tool executor called max_tool_rounds times (once per round)
        assert tool_executor.call_count == 2

    @pytest.mark.asyncio
    async def test_no_tool_use_returns_text_immediately(self, tmp_path):
        engine = _make_engine(tmp_path)
        text_response = _make_text_response(_watchlist_json())

        engine._client.messages.create = AsyncMock(return_value=text_response)

        tool_executor = AsyncMock()
        result = await engine.call_claude_with_tools(
            "DATE:{current_date} CTX:{market_context} WL:{current_watchlist} N:{target_count} CANDS:{candidates}",
            {"current_date": "2026-03-23", "market_context": "{}", "current_watchlist": "none", "target_count": 20, "candidates": "none"},
            tools=[engine._WEB_SEARCH_TOOL],
            tool_executor=tool_executor,
        )

        assert _watchlist_json() in result
        tool_executor.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: run_watchlist_build routing
# ---------------------------------------------------------------------------

class TestRunWatchlistBuildRouting:

    @pytest.mark.asyncio
    async def test_uses_call_claude_with_tools_when_search_enabled(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.call_claude_with_tools = AsyncMock(return_value=_watchlist_json())
        engine.call_claude = AsyncMock(return_value=_watchlist_json())

        search_adapter = MagicMock()
        search_adapter.enabled = True

        await engine.run_watchlist_build(
            market_context={},
            current_watchlist=WatchlistState(entries=[]),
            candidates=[{"symbol": "NVDA", "rvol": 2.0}],
            search_adapter=search_adapter,
        )

        engine.call_claude_with_tools.assert_called_once()
        engine.call_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_call_claude_when_search_disabled(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.call_claude_with_tools = AsyncMock(return_value=_watchlist_json())
        engine.call_claude = AsyncMock(return_value=_watchlist_json())

        search_adapter = MagicMock()
        search_adapter.enabled = False

        await engine.run_watchlist_build(
            market_context={},
            current_watchlist=WatchlistState(entries=[]),
            candidates=[],
            search_adapter=search_adapter,
        )

        engine.call_claude.assert_called_once()
        engine.call_claude_with_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_call_claude_when_no_search_adapter(self, tmp_path):
        engine = _make_engine(tmp_path)
        engine.call_claude_with_tools = AsyncMock(return_value=_watchlist_json())
        engine.call_claude = AsyncMock(return_value=_watchlist_json())

        await engine.run_watchlist_build(
            market_context={},
            current_watchlist=WatchlistState(entries=[]),
            candidates=None,
            search_adapter=None,
        )

        engine.call_claude.assert_called_once()
        engine.call_claude_with_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_executor_calls_search_adapter(self, tmp_path):
        engine = _make_engine(tmp_path)

        captured_executor = {}

        async def _capture_tool_call(*args, **kwargs):
            captured_executor["executor"] = kwargs.get("tool_executor") or args[3]
            return _watchlist_json()

        engine.call_claude_with_tools = _capture_tool_call

        search_adapter = MagicMock()
        search_adapter.enabled = True
        search_adapter.search = AsyncMock(return_value=[
            {"title": "NVDA news", "url": "https://example.com", "description": "strong Q1"}
        ])

        await engine.run_watchlist_build(
            market_context={},
            current_watchlist=WatchlistState(entries=[]),
            candidates=None,
            search_adapter=search_adapter,
        )

        # Call the captured tool executor
        executor = captured_executor["executor"]
        result = await executor("web_search", {"query": "NVDA momentum"})
        assert "NVDA news" in result
        search_adapter.search.assert_called_once_with("NVDA momentum", n_results=engine._cfg.search.result_count_per_query)

    @pytest.mark.asyncio
    async def test_tool_executor_unknown_tool_returns_message(self, tmp_path):
        engine = _make_engine(tmp_path)

        captured_executor = {}

        async def _capture(*args, **kwargs):
            captured_executor["executor"] = kwargs.get("tool_executor") or args[3]
            return _watchlist_json()

        engine.call_claude_with_tools = _capture
        search_adapter = MagicMock()
        search_adapter.enabled = True

        await engine.run_watchlist_build(
            market_context={},
            current_watchlist=WatchlistState(entries=[]),
            candidates=None,
            search_adapter=search_adapter,
        )

        executor = captured_executor["executor"]
        result = await executor("unknown_tool", {})
        assert "Unknown" in result

    @pytest.mark.asyncio
    async def test_tool_executor_no_results_returns_message(self, tmp_path):
        engine = _make_engine(tmp_path)

        captured_executor = {}

        async def _capture(*args, **kwargs):
            captured_executor["executor"] = kwargs.get("tool_executor") or args[3]
            return _watchlist_json()

        engine.call_claude_with_tools = _capture
        search_adapter = MagicMock()
        search_adapter.enabled = True
        search_adapter.search = AsyncMock(return_value=[])

        await engine.run_watchlist_build(
            market_context={},
            current_watchlist=WatchlistState(entries=[]),
            candidates=None,
            search_adapter=search_adapter,
        )

        executor = captured_executor["executor"]
        result = await executor("web_search", {"query": "empty"})
        assert "No results" in result

    @pytest.mark.asyncio
    async def test_existing_parse_behavior_unchanged(self, tmp_path):
        """Existing watchlist build parse + return behavior is unchanged."""
        engine = _make_engine(tmp_path)
        engine.call_claude = AsyncMock(return_value=_watchlist_json())

        result = await engine.run_watchlist_build(
            market_context={},
            current_watchlist=WatchlistState(entries=[]),
        )

        assert result is not None
        assert len(result.watchlist) == 1
        assert result.watchlist[0]["symbol"] == "AAPL"
        assert result.market_notes == "ok"
