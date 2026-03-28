"""
Unit tests for ContextCompressor (Phase 20).

Tests cover:
- Fallback sort (deterministic composite-score sort)
- Symbol validation (only accepts symbols from all_candidates)
- needs_sonnet handling and per-cycle guard
- Gate: no Haiku call when candidates <= max_symbols_out
- Haiku parse success path (mocked API)
- Haiku parse failure fallback
- assemble_reasoning_context: selected_symbols path
- Config fields (ClaudeConfig compressor fields)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from ozymandias.intelligence.context_compressor import (
    CompressorResult,
    ContextCompressor,
    NEEDS_SONNET_REASONS,
    _sym,
    _attr,
)
from ozymandias.core.config import ClaudeConfig
from ozymandias.core.state_manager import WatchlistEntry, WatchlistState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_entry(symbol: str, tier: int = 1, ed: str = "either", reason: str = "") -> WatchlistEntry:
    return WatchlistEntry(
        symbol=symbol,
        date_added="2026-03-27",
        reason=reason or f"thesis for {symbol}",
        priority_tier=tier,
        expected_direction=ed,
    )


def make_indicators(symbols: list[str], rsi: float = 60.0, rvol: float = 1.2) -> dict:
    return {
        sym: {
            "signals": {
                "rsi": rsi,
                "volume_ratio": rvol,
                "vwap_position": "above",
                "trend_structure": "bullish_aligned",
                "roc_5": 2.0,
                "price": 100.0,
            }
        }
        for sym in symbols
    }


def make_cfg() -> ClaudeConfig:
    cfg = ClaudeConfig()
    cfg.compressor_enabled = True
    cfg.compressor_model = "claude-haiku-4-5-20251001"
    cfg.compressor_max_symbols_out = 5
    cfg.compressor_max_tokens = 512
    return cfg


# ---------------------------------------------------------------------------
# TestHelperFunctions
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    def test_sym_with_entry_object(self):
        e = make_entry("AAPL")
        assert _sym(e) == "AAPL"

    def test_sym_with_dict(self):
        assert _sym({"symbol": "GOOG"}) == "GOOG"

    def test_sym_missing(self):
        assert _sym({}) == ""

    def test_attr_entry_object(self):
        e = make_entry("X", ed="long")
        assert _attr(e, "expected_direction", "either") == "long"

    def test_attr_dict(self):
        assert _attr({"priority_tier": 2}, "priority_tier", 1) == 2

    def test_attr_missing_returns_default(self):
        assert _attr({}, "missing_field", "default") == "default"


# ---------------------------------------------------------------------------
# TestFallbackSort
# ---------------------------------------------------------------------------

class TestFallbackSort:
    def setup_method(self):
        self.compressor = ContextCompressor(make_cfg())

    def test_empty_candidates_returns_empty(self):
        result = self.compressor._fallback_sort([], {}, 5)
        assert result.symbols == []
        assert result.from_fallback is True

    def test_sorts_by_composite_score_desc(self):
        # Use directional expected_directions so ranking is deterministic.
        # B=long with bullish signals ranks highest; C=long with bearish signals ranks lowest.
        entries = [
            make_entry("A", ed="long"),
            make_entry("B", ed="long"),
            make_entry("C", ed="long"),
        ]
        indicators = {
            "A": {"signals": {"rsi": 55, "volume_ratio": 0.9, "vwap_position": "above", "trend_structure": "neutral", "roc_5": 0.5}},
            "B": {"signals": {"rsi": 65, "volume_ratio": 1.5, "vwap_position": "above", "trend_structure": "bullish_aligned", "roc_5": 2.0}},
            "C": {"signals": {"rsi": 40, "volume_ratio": 0.5, "vwap_position": "below", "trend_structure": "bearish_aligned", "roc_5": -1.0}},
        }
        result = self.compressor._fallback_sort(entries, indicators, 5)
        # B should rank highest (strong bullish signals for a long), C lowest
        assert result.symbols[0] == "B"
        assert result.symbols[-1] == "C"
        assert result.from_fallback is True

    def test_respects_max_symbols_out(self):
        entries = [make_entry(f"SYM{i}") for i in range(10)]
        indicators = make_indicators([f"SYM{i}" for i in range(10)])
        result = self.compressor._fallback_sort(entries, indicators, 3)
        assert len(result.symbols) == 3

    def test_direction_adjusted_score_long(self):
        entries = [make_entry("L", ed="long"), make_entry("S", ed="short")]
        indicators = {
            "L": {"signals": {"rsi": 65, "volume_ratio": 1.5, "vwap_position": "above", "trend_structure": "bullish_aligned", "roc_5": 3.0}},
            "S": {"signals": {"rsi": 65, "volume_ratio": 1.5, "vwap_position": "above", "trend_structure": "bullish_aligned", "roc_5": 3.0}},
        }
        # Both have same raw signals but L is long-aligned, S is short-aligned against bullish signals
        result = self.compressor._fallback_sort(entries, indicators, 5)
        # L should rank higher than S (long candidate with bullish signals)
        assert result.symbols[0] == "L"

    def test_no_indicators_falls_back_to_zero(self):
        entries = [make_entry("X"), make_entry("Y")]
        result = self.compressor._fallback_sort(entries, {}, 5)
        assert len(result.symbols) == 2
        assert result.from_fallback is True


# ---------------------------------------------------------------------------
# TestParseResponse
# ---------------------------------------------------------------------------

class TestParseResponse:
    def setup_method(self):
        self.compressor = ContextCompressor(make_cfg())
        self.candidates = [make_entry(s) for s in ["AAPL", "GOOG", "MSFT", "AMZN", "META"]]
        self.indicators = make_indicators(["AAPL", "GOOG", "MSFT", "AMZN", "META"])

    def _parse(self, raw_text: str, cycle_id: str = "test-cycle") -> CompressorResult:
        return self.compressor._parse_response(
            raw_text, self.candidates, self.indicators, 5, cycle_id
        )

    def test_valid_response_returns_symbols(self):
        raw = json.dumps({
            "selected_symbols": ["AAPL", "GOOG", "MSFT"],
            "rationale": {"AAPL": "strong RVOL"},
            "notes": "Tech names with high volume",
            "needs_sonnet": False,
            "sonnet_reason": None,
        })
        result = self._parse(raw)
        assert result.symbols == ["AAPL", "GOOG", "MSFT"]
        assert result.from_fallback is False
        assert result.needs_sonnet is False
        assert result.notes == "Tech names with high volume"

    def test_filters_unknown_symbols(self):
        raw = json.dumps({
            "selected_symbols": ["AAPL", "UNKNOWN_XYZ", "GOOG"],
            "needs_sonnet": False,
            "sonnet_reason": None,
        })
        result = self._parse(raw)
        assert "UNKNOWN_XYZ" not in result.symbols
        assert "AAPL" in result.symbols

    def test_enforces_max_symbols_out(self):
        raw = json.dumps({
            "selected_symbols": ["AAPL", "GOOG", "MSFT", "AMZN", "META"],
            "needs_sonnet": False,
            "sonnet_reason": None,
        })
        result = self._parse(raw)
        assert len(result.symbols) <= 5

    def test_valid_needs_sonnet_reason(self):
        raw = json.dumps({
            "selected_symbols": ["AAPL"],
            "needs_sonnet": True,
            "sonnet_reason": "regime_shift",
        })
        result = self._parse(raw, cycle_id="cycle-A")
        assert result.needs_sonnet is True
        assert result.sonnet_reason == "regime_shift"

    def test_unknown_sonnet_reason_suppressed(self):
        raw = json.dumps({
            "selected_symbols": ["AAPL"],
            "needs_sonnet": True,
            "sonnet_reason": "invented_reason",
        })
        result = self._parse(raw)
        assert result.needs_sonnet is False
        assert result.sonnet_reason is None

    def test_per_cycle_needs_sonnet_guard(self):
        raw = json.dumps({
            "selected_symbols": ["AAPL"],
            "needs_sonnet": True,
            "sonnet_reason": "all_candidates_failing",
        })
        # First call fires
        result1 = self._parse(raw, cycle_id="cycle-B")
        assert result1.needs_sonnet is True
        # Second call same cycle — suppressed
        result2 = self._parse(raw, cycle_id="cycle-B")
        assert result2.needs_sonnet is False
        # Different cycle — fires again
        result3 = self._parse(raw, cycle_id="cycle-C")
        assert result3.needs_sonnet is True

    def test_unparseable_json_falls_back(self):
        result = self._parse("this is not json at all")
        assert result.from_fallback is True

    def test_empty_selected_symbols_falls_back(self):
        raw = json.dumps({"selected_symbols": [], "needs_sonnet": False, "sonnet_reason": None})
        result = self._parse(raw)
        assert result.from_fallback is True

    def test_code_fence_stripped(self):
        raw = "```json\n" + json.dumps({
            "selected_symbols": ["AAPL", "GOOG"],
            "needs_sonnet": False,
            "sonnet_reason": None,
        }) + "\n```"
        result = self._parse(raw)
        assert result.symbols == ["AAPL", "GOOG"]
        assert result.from_fallback is False


# ---------------------------------------------------------------------------
# TestCompressGate
# ---------------------------------------------------------------------------

class TestCompressGate:
    """Tests the gate: no Haiku call when candidates <= max_symbols_out."""

    def setup_method(self):
        self.compressor = ContextCompressor(make_cfg())

    @pytest.mark.asyncio
    async def test_gate_skips_haiku_when_candidates_small(self):
        """When len(all_candidates) <= max_symbols_out, returns fallback without API call."""
        entries = [make_entry(f"S{i}") for i in range(3)]  # 3 < max_symbols_out=5
        indicators = make_indicators([f"S{i}" for i in range(3)])

        with patch.object(self.compressor._client.messages, "create") as mock_create:
            result = await self.compressor.compress(
                all_candidates=entries,
                indicators=indicators,
                market_data={},
                regime_assessment=None,
                sector_regimes=None,
                max_symbols_out=5,
            )
        mock_create.assert_not_called()
        assert result.from_fallback is True

    @pytest.mark.asyncio
    async def test_gate_skips_haiku_when_no_prompt_template(self):
        """Without a compress.txt, falls back to deterministic sort."""
        entries = [make_entry(f"S{i}") for i in range(10)]
        indicators = make_indicators([f"S{i}" for i in range(10)])
        # _prompts_dir=None → _load_prompt returns None
        compressor = ContextCompressor(make_cfg(), prompts_dir=None)

        with patch.object(compressor._client.messages, "create") as mock_create:
            result = await compressor.compress(
                all_candidates=entries,
                indicators=indicators,
                market_data={},
                regime_assessment=None,
                sector_regimes=None,
                max_symbols_out=5,
            )
        mock_create.assert_not_called()
        assert result.from_fallback is True

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_empty(self):
        result = await self.compressor.compress(
            all_candidates=[],
            indicators={},
            market_data={},
            regime_assessment=None,
            sector_regimes=None,
            max_symbols_out=5,
        )
        assert result.symbols == []
        assert result.from_fallback is True


# ---------------------------------------------------------------------------
# TestCompressWithMockedHaiku
# ---------------------------------------------------------------------------

class TestCompressWithMockedHaiku:
    """Tests the Haiku call path using a mocked prompt template."""

    def setup_method(self):
        self.prompts_dir = MagicMock(spec=Path)
        mock_prompt_path = MagicMock()
        mock_prompt_path.read_text.return_value = (
            "Select top {max_symbols} symbols. Candidates: {candidates_json}\n"
            "Respond with JSON: {\"selected_symbols\": [...], \"needs_sonnet\": false, \"sonnet_reason\": null}"
        )
        self.prompts_dir.__truediv__ = MagicMock(return_value=mock_prompt_path)
        self.compressor = ContextCompressor(make_cfg(), prompts_dir=self.prompts_dir)
        self.entries = [make_entry(s) for s in ["AAPL", "GOOG", "MSFT", "AMZN", "META", "NVDA", "AMD"]]
        self.indicators = make_indicators(["AAPL", "GOOG", "MSFT", "AMZN", "META", "NVDA", "AMD"])

    def _make_haiku_response(self, symbols: list[str]) -> MagicMock:
        response = MagicMock()
        content_block = MagicMock()
        content_block.text = json.dumps({
            "selected_symbols": symbols,
            "rationale": {s: f"good signals for {s}" for s in symbols},
            "notes": "Selected by Haiku",
            "needs_sonnet": False,
            "sonnet_reason": None,
        })
        response.content = [content_block]
        return response

    @pytest.mark.asyncio
    async def test_haiku_response_used_when_successful(self):
        haiku_symbols = ["NVDA", "AMD", "AAPL"]
        with patch.object(
            self.compressor._client.messages, "create",
            new=AsyncMock(return_value=self._make_haiku_response(haiku_symbols))
        ):
            result = await self.compressor.compress(
                all_candidates=self.entries,
                indicators=self.indicators,
                market_data={},
                regime_assessment=None,
                sector_regimes=None,
                max_symbols_out=5,
            )
        assert result.symbols == ["NVDA", "AMD", "AAPL"]
        assert result.from_fallback is False

    @pytest.mark.asyncio
    async def test_api_failure_falls_back_to_deterministic(self):
        with patch.object(
            self.compressor._client.messages, "create",
            side_effect=Exception("API timeout")
        ):
            result = await self.compressor.compress(
                all_candidates=self.entries,
                indicators=self.indicators,
                market_data={},
                regime_assessment=None,
                sector_regimes=None,
                max_symbols_out=5,
            )
        assert result.from_fallback is True
        assert len(result.symbols) <= 5
        # All returned symbols must be from the candidate pool
        known = {_sym(e) for e in self.entries}
        for sym in result.symbols:
            assert sym in known

    @pytest.mark.asyncio
    async def test_haiku_cannot_inject_unknown_symbols(self):
        """Symbols not in all_candidates are filtered out."""
        bad_response = MagicMock()
        content_block = MagicMock()
        content_block.text = json.dumps({
            "selected_symbols": ["AAPL", "EVIL_INJECTION", "GOOG"],
            "needs_sonnet": False,
            "sonnet_reason": None,
        })
        bad_response.content = [content_block]

        with patch.object(
            self.compressor._client.messages, "create",
            new=AsyncMock(return_value=bad_response)
        ):
            result = await self.compressor.compress(
                all_candidates=self.entries,
                indicators=self.indicators,
                market_data={},
                regime_assessment=None,
                sector_regimes=None,
                max_symbols_out=5,
            )
        assert "EVIL_INJECTION" not in result.symbols


# ---------------------------------------------------------------------------
# TestAssembleContextSelectedSymbols
# ---------------------------------------------------------------------------

class TestAssembleContextSelectedSymbols:
    """Tests the selected_symbols path in assemble_reasoning_context."""

    def _make_engine(self):
        from ozymandias.intelligence.claude_reasoning import ClaudeReasoningEngine
        from ozymandias.core.config import Config
        prompts_dir = Path(__file__).resolve().parent.parent / "config" / "prompts" / "v3.10.0"
        cfg = Config()
        cfg.claude.compressor_enabled = False  # disable compressor for direct context assembly tests
        return ClaudeReasoningEngine(cfg, prompts_dir=prompts_dir)

    def _make_portfolio(self, position_symbols: list[str] = None):
        from ozymandias.core.state_manager import PortfolioState
        return PortfolioState(cash=100000.0, buying_power=100000.0, positions=[])

    def _make_watchlist(self, symbols: list[str], tiers: dict[str, int] | None = None) -> WatchlistState:
        entries = []
        for sym in symbols:
            tier = (tiers or {}).get(sym, 1)
            entries.append(WatchlistEntry(
                symbol=sym,
                date_added="2026-03-27",
                reason=f"test {sym}",
                priority_tier=tier,
            ))
        return WatchlistState(entries=entries)

    def test_selected_symbols_overrides_composite_sort(self):
        engine = self._make_engine()
        portfolio = self._make_portfolio()
        watchlist = self._make_watchlist(["AAPL", "GOOG", "MSFT", "AMZN", "META"])
        indicators = make_indicators(["AAPL", "GOOG", "MSFT", "AMZN", "META"])

        # AMZN and META have low scores but are in selected_symbols — should appear
        ctx = engine.assemble_reasoning_context(
            portfolio, watchlist,
            market_data={"trading_session": "market_open"},
            indicators=indicators,
            selected_symbols=["META", "AMZN"],
        )
        tier1_syms = [e["symbol"] for e in ctx["watchlist_tier1"]]
        # META and AMZN should be first (in that order) per selected_symbols
        assert "META" in tier1_syms
        assert "AMZN" in tier1_syms

    def test_selected_symbols_respects_slots_limit(self):
        engine = self._make_engine()
        engine._claude_cfg.tier1_max_symbols = 3
        portfolio = self._make_portfolio()
        watchlist = self._make_watchlist(["A", "B", "C", "D", "E"])
        indicators = make_indicators(["A", "B", "C", "D", "E"])

        ctx = engine.assemble_reasoning_context(
            portfolio, watchlist,
            market_data={"trading_session": "market_open"},
            indicators=indicators,
            selected_symbols=["A", "B", "C", "D", "E"],
        )
        assert len(ctx["watchlist_tier1"]) <= 3

    def test_selected_symbols_none_uses_composite_sort(self):
        """selected_symbols=None falls back to composite-score sort (all tier1 symbols present)."""
        engine = self._make_engine()
        portfolio = self._make_portfolio()
        watchlist = self._make_watchlist(["A", "B", "C"])
        indicators = make_indicators(["A", "B", "C"])
        ctx = engine.assemble_reasoning_context(
            portfolio, watchlist,
            market_data={"trading_session": "market_open"},
            indicators=indicators,
            selected_symbols=None,
        )
        tier1_syms = [e["symbol"] for e in ctx["watchlist_tier1"]]
        # All tier1 symbols should appear (composite sort — no filtering)
        assert set(tier1_syms) == {"A", "B", "C"}

    def test_selected_symbols_from_tier2_are_included(self):
        """Haiku can surface tier-2 symbols; selected_symbols path looks up all tiers."""
        engine = self._make_engine()
        portfolio = self._make_portfolio()
        watchlist = self._make_watchlist(
            ["TIER1A", "TIER2B"],
            tiers={"TIER1A": 1, "TIER2B": 2},
        )
        indicators = make_indicators(["TIER1A", "TIER2B"])

        ctx = engine.assemble_reasoning_context(
            portfolio, watchlist,
            market_data={"trading_session": "market_open"},
            indicators=indicators,
            selected_symbols=["TIER2B", "TIER1A"],  # tier2 first
        )
        tier1_syms = [e["symbol"] for e in ctx["watchlist_tier1"]]
        assert "TIER2B" in tier1_syms
        assert "TIER1A" in tier1_syms


# ---------------------------------------------------------------------------
# TestCompressorConfigFields
# ---------------------------------------------------------------------------

class TestCompressorConfigFields:
    def test_default_values(self):
        cfg = ClaudeConfig()
        assert cfg.compressor_enabled is True
        assert cfg.compressor_model == "claude-haiku-4-5-20251001"
        assert cfg.compressor_max_symbols_out == 18
        assert cfg.compressor_max_tokens == 512

    def test_engine_creates_compressor_when_enabled(self):
        from ozymandias.intelligence.claude_reasoning import ClaudeReasoningEngine
        from ozymandias.core.config import Config
        prompts_dir = Path(__file__).resolve().parent.parent / "config" / "prompts" / "v3.10.0"
        cfg = Config()
        cfg.claude.compressor_enabled = True
        engine = ClaudeReasoningEngine(cfg, prompts_dir=prompts_dir)
        assert engine._compressor is not None

    def test_engine_skips_compressor_when_disabled(self):
        from ozymandias.intelligence.claude_reasoning import ClaudeReasoningEngine
        from ozymandias.core.config import Config
        prompts_dir = Path(__file__).resolve().parent.parent / "config" / "prompts" / "v3.10.0"
        cfg = Config()
        cfg.claude.compressor_enabled = False
        engine = ClaudeReasoningEngine(cfg, prompts_dir=prompts_dir)
        assert engine._compressor is None

    def test_needs_sonnet_reasons_set(self):
        assert "regime_shift" in NEEDS_SONNET_REASONS
        assert "all_candidates_failing" in NEEDS_SONNET_REASONS
        assert "position_thesis_breach" in NEEDS_SONNET_REASONS
        assert "watchlist_stale" in NEEDS_SONNET_REASONS
