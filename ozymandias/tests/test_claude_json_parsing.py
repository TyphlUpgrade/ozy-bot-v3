"""
Tests for parse_claude_response() — the defensive JSON parsing pipeline.

These are pure unit tests: no API calls, no mocking, no async.
The parser must never crash regardless of input.
"""
from __future__ import annotations

import json
import pytest

from ozymandias.intelligence.claude_reasoning import parse_claude_response

# A realistic Claude response matching the reasoning output schema (spec §4.3)
_REALISTIC_RESPONSE = json.dumps({
    "timestamp": "2026-03-11T14:30:00Z",
    "position_reviews": [
        {
            "symbol": "NVDA",
            "action": "hold",
            "thesis_intact": True,
            "updated_reasoning": "Momentum still strong, volume confirming. Hold for target.",
            "adjusted_targets": None,
        }
    ],
    "new_opportunities": [
        {
            "symbol": "TSLA",
            "action": "buy",
            "strategy": "momentum",
            "timeframe": "short_term",
            "conviction": 0.78,
            "reasoning": "Delivery beat catalyst + FSD momentum.",
            "suggested_entry": 244.0,
            "suggested_exit": 268.0,
            "suggested_stop": 235.0,
            "position_size_pct": 0.12,
        }
    ],
    "watchlist_changes": {
        "add": ["PLTR", "COIN"],
        "remove": ["XOM"],
        "rationale": "Rotating into high-beta tech.",
    },
    "market_assessment": "Bullish bias, CPI tomorrow is a risk event.",
    "risk_flags": [],
})


class TestCleanInput:
    def test_clean_json_parses(self):
        raw = '{"key": "value", "num": 42}'
        result = parse_claude_response(raw)
        assert result == {"key": "value", "num": 42}

    def test_realistic_response_parses(self):
        result = parse_claude_response(_REALISTIC_RESPONSE)
        assert result is not None
        assert result["position_reviews"][0]["symbol"] == "NVDA"
        assert result["new_opportunities"][0]["conviction"] == 0.78
        assert result["watchlist_changes"]["add"] == ["PLTR", "COIN"]

    def test_nested_json_parses(self):
        raw = '{"outer": {"inner": [1, 2, 3]}, "flag": true}'
        result = parse_claude_response(raw)
        assert result["outer"]["inner"] == [1, 2, 3]

    def test_whitespace_around_json(self):
        raw = '   \n\n  {"key": "value"}  \n  '
        result = parse_claude_response(raw)
        assert result == {"key": "value"}


class TestMarkdownFences:
    def test_json_in_backtick_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        result = parse_claude_response(raw)
        assert result == {"key": "value"}

    def test_json_in_plain_backtick_fence(self):
        raw = '```\n{"key": "value"}\n```'
        result = parse_claude_response(raw)
        assert result == {"key": "value"}

    def test_fence_with_no_newline(self):
        raw = '```json{"key": "value"}```'
        result = parse_claude_response(raw)
        assert result == {"key": "value"}

    def test_realistic_response_in_fence(self):
        raw = f"```json\n{_REALISTIC_RESPONSE}\n```"
        result = parse_claude_response(raw)
        assert result is not None
        assert "position_reviews" in result


class TestLeadingAndTrailingText:
    def test_leading_text_before_json(self):
        raw = 'Here is my analysis:\n\n{"key": "value"}'
        result = parse_claude_response(raw)
        assert result == {"key": "value"}

    def test_trailing_text_after_closing_brace(self):
        raw = '{"key": "value"}\n\nLet me know if you need anything else.'
        result = parse_claude_response(raw)
        assert result == {"key": "value"}

    def test_text_before_and_after(self):
        raw = 'Reasoning:\n{"key": "value"}\nEnd of response.'
        result = parse_claude_response(raw)
        assert result == {"key": "value"}

    def test_realistic_response_with_preamble(self):
        raw = f"Based on my analysis:\n\n{_REALISTIC_RESPONSE}\n\nI hope this helps."
        result = parse_claude_response(raw)
        assert result is not None
        assert "new_opportunities" in result


class TestMalformedInput:
    def test_completely_malformed_returns_none(self):
        assert parse_claude_response("this is not json at all") is None

    def test_empty_string_returns_none(self):
        assert parse_claude_response("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_claude_response("   \n\t  ") is None

    def test_truncated_json_returns_none(self):
        assert parse_claude_response('{"key": "val') is None

    def test_json_array_returns_none(self):
        # Parser expects a dict, not a list
        assert parse_claude_response('[1, 2, 3]') is None

    def test_json_string_returns_none(self):
        assert parse_claude_response('"just a string"') is None

    def test_json_number_returns_none(self):
        assert parse_claude_response('42') is None

    def test_none_input_handled(self):
        # None is not a valid input type but shouldn't crash
        # (In practice the engine always passes a str, but be defensive)
        assert parse_claude_response(None) is None  # type: ignore[arg-type]


class TestCommentHandling:
    def test_json_with_trailing_comma_returns_none(self):
        # Standard json.loads rejects trailing commas — regex fallback also fails
        assert parse_claude_response('{"key": "value",}') is None

    def test_json_embedded_in_explanation(self):
        """
        Claude sometimes wraps the JSON in an explanation block.
        The regex extractor should recover the object.
        """
        raw = (
            "I'll provide the trade analysis in the requested format:\n\n"
            '{"market_assessment": "Bullish", "risk_flags": []}\n\n'
            "This covers the current market conditions."
        )
        result = parse_claude_response(raw)
        assert result == {"market_assessment": "Bullish", "risk_flags": []}

    def test_multiple_json_objects_extracts_outermost(self):
        """
        If there are nested objects, re.search(r'{.*}', re.DOTALL) finds the
        outermost match (from first { to last }). This test verifies that
        the greedy match captures the full structure.
        """
        raw = 'prefix {"outer": {"inner": "value"}} suffix'
        result = parse_claude_response(raw)
        assert result == {"outer": {"inner": "value"}}

    def test_fence_then_text_then_json(self):
        """Fence stripped, then text before JSON — regex fallback recovers it."""
        raw = '```\nHere is the JSON output:\n{"key": "ok"}\n```'
        result = parse_claude_response(raw)
        assert result == {"key": "ok"}
