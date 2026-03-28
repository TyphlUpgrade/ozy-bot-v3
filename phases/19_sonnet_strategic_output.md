# Phase 19: Foundation — Richer Context + Sonnet Strategic Output

Read the Phase 18 section of DRIFT_LOG.md and the `Post-MVP Roadmap` section of CLAUDE.md before
starting. This phase assumes Phases 17 and 18 are complete.

---

## Motivation

On volatile sessions (e.g. 2026-03-27 panic day), the bot sat idle because:
- All candidates were session-suppressed; no recovery mechanism existed
- Claude had no regime context to decide "rebuild watchlist for shorts"
- The reasoning cache had no mechanism to signal "market regime changed"
- Quant trend gates silently blocked Claude-directed entries without feedback

This phase gives Sonnet the inputs and output fields needed to act as a periodic strategist rather
than a reactive responder: regime assessment, sector dispersion, filter feedback, and per-thesis
durability conditions. Haiku pre-screening (Phase 20) and regime-driven watchlist rebuilds (Phase 21)
consume these outputs.

---

## 1. New Input: `sector_dispersion`

### Computation

Add `compute_sector_dispersion(watchlist_entries, sector_map, daily_indicators)` to
`ozymandias/intelligence/technical_analysis.py`.

```python
def compute_sector_dispersion(
    watchlist_entries: list,            # list of WatchlistEntry
    sector_map: dict[str, str],         # symbol → sector ETF (e.g. "NVDA" → "XLK")
    daily_indicators: dict[str, dict],  # symbol → generate_daily_signal_summary output
) -> dict:
```

**Algorithm:**
1. For each sector ETF present in `daily_indicators`, get `sector_roc_5d = daily_indicators[etf].get("roc_5d")`. Skip sectors where ETF has no daily data.
2. For each watchlist entry whose symbol has a `sector_map` entry and has `daily_indicators[symbol].get("roc_5d")` available:
   - `vs_sector_1w = symbol_roc_5d - sector_roc_5d`
3. Per sector ETF, collect the (symbol, vs_sector_1w) pairs. Sort ascending.
   - `outperforming`: top 3 by `vs_sector_1w` (most positive) → `[{"symbol": ..., "vs_sector_1w": ...}]`
   - `underperforming`: bottom 3 by `vs_sector_1w` (most negative) → same schema
4. Return dict keyed by sector ETF:
```python
{
  "XLK": {
    "sector_1w_return": -2.3,
    "outperforming": [{"symbol": "CRWD", "vs_sector_1w": 1.4}, ...],
    "underperforming": [{"symbol": "INTC", "vs_sector_1w": -3.1}, ...]
  }
}
```
5. Omit sector ETFs where fewer than 2 watchlist symbols map to them (insufficient comparison basis).
6. Return `{}` if no sectors meet criteria.

### Daily bar fetch extension

Extend the slow-loop `_daily_indicators` fetch (added in Phase 18 two-profile layer) to cover all
open watchlist symbols, not just SPY, QQQ, and open swing positions. `_daily_indicators` is already
populated; this widens the symbol set.

Implementation: in `_slow_loop_cycle`, before `_build_market_context`, collect `all_watchlist_symbols
= [e.symbol for e in watchlist.entries]`. Add these to the set of symbols fetched into
`_daily_indicators`. Fetch failures logged at WARNING (existing behavior).

This also makes per-symbol `roc_5d` available for `compute_sector_dispersion`.

### Wiring into `_build_market_context`

```python
sector_dispersion = compute_sector_dispersion(
    watchlist.entries,
    self._SECTOR_MAP,           # already defined in orchestrator
    self._daily_indicators,
)
market_data["sector_dispersion"] = sector_dispersion
```

---

## 2. New Input: `recent_rejections`

Source: `_recommendation_outcomes` dict (added in Phase 15). Each entry tracks per-symbol rejection
counts and the most recent rejection reason.

In `_build_market_context`, add:

```python
rejections = [
    {
        "symbol": sym,
        "reason": data["stage_detail"],
        "cycles_rejected": data["rejection_count"],
    }
    for sym, data in self._recommendation_outcomes.items()
    if data.get("rejection_count", 0) >= 1
]
# Sort by cycles_rejected descending; cap at 10 entries
market_data["recent_rejections"] = sorted(
    rejections, key=lambda x: x["cycles_rejected"], reverse=True
)[:10]
```

---

## 3. New Input: `news_theme_synthesis`

Pure string aggregation — no new Claude calls.

In `_build_market_context`:

1. Collect all `WatchlistEntry.reason` strings (these contain Claude's cached rationale including
   any news context).
2. Group by sector (using `_SECTOR_MAP`).
3. For each sector, concatenate the first 2 reason snippets (first 80 chars each) as a brief theme.
4. Add as `market_data["news_themes"]`: `{"XLK": "...", "XLF": "..."}`.

This is best-effort. If no watchlist entries exist or no sector mapping applies, omit the field.

---

## 4. New Sonnet Output Fields

### `ReasoningResult` additions

In `ozymandias/intelligence/claude_reasoning.py`, add to `ReasoningResult`:

```python
regime_assessment: dict | None = None
# {
#   "regime": "risk-off panic" | "sector rotation" | "normal" | "euphoria",
#   "confidence": float,
#   "key_signals": list[str],
#   "valid_until_conditions": list[str],
#   "implications": str
# }

sector_regimes: dict | None = None
# {
#   "XLK": {"regime": "correcting", "bias": "short", "strength": "strong"},
#   ...
# }
# regime values: "breaking_out" | "uptrend" | "neutral" | "correcting" | "downtrend"
# bias values: "long" | "short" | "either"
# strength values: "strong" | "moderate" | "weak"

filter_adjustments: dict | None = None
# {
#   "min_rvol": float,
#   "min_composite_score": float,
#   "reason": str
# }

active_theses: list[dict] | None = None
# [{"symbol": str, "thesis": str, "thesis_breaking_conditions": list[str]}]
```

All four fields default to `None`. Existing `ReasoningResult` fields are unchanged.

### Parsing

In `_parse_reasoning_result` (or equivalent parsing site), extract each new field from the Claude
JSON output using `.get()`. Apply the same 4-step defensive pipeline as the rest of Claude output:
- If the field is present but malformed (wrong type, missing keys), set it to `None` and log DEBUG.
- If the field is absent, leave it as `None` (valid — Claude may not always produce these fields).
- Never raise on parse failure of any new field.

---

## 5. Storing and Applying `filter_adjustments`

### Storage

Add to orchestrator:
```python
self._filter_adjustments: dict | None = None
```

After each `run_reasoning_cycle` call that returns a `ReasoningResult`, update:
```python
self._filter_adjustments = result.filter_adjustments  # may be None
```

`_filter_adjustments` is reset to `None` at the start of each new reasoning cycle (before calling
`run_reasoning_cycle`), then populated from the result. Stale adjustments from a prior cycle never
persist across cycles.

### Config floors

Add to `RankerConfig` in `core/config.py`:
```python
filter_adj_min_rvol: float = 0.5        # absolute floor on Claude's min_rvol adjustment
filter_adj_min_composite: float = 0.35  # absolute floor on Claude's min_composite_score adjustment
```

Add corresponding keys to `config.json` under `ranker`.

### Application

**`min_composite_score`**: Applied in `_medium_try_entry` (in orchestrator) where the composite
floor is checked before sizing. `effective_min_composite` replaces the raw config value:

```python
effective_min_composite = max(
    config.filter_adj_min_composite,
    filter_adjustments.get("min_composite_score", config.min_composite_score) if filter_adjustments else config.min_composite_score
)
```

**`min_rvol`**: Applied in `apply_entry_gate` in strategy classes (the actual RVOL floor lives
at `self._p("min_rvol_for_entry")` in `MomentumStrategy`). `apply_entry_gate` gains a
`filter_adjustments: dict | None = None` parameter alongside `entry_conditions`. When present,
compute:

```python
effective_min_rvol = max(
    config.filter_adj_min_rvol,
    filter_adjustments.get("min_rvol", self._p("min_rvol_for_entry")) if filter_adjustments else self._p("min_rvol_for_entry")
)
```

Pass `filter_adjustments: dict | None` from orchestrator → ranker → `apply_entry_gate` call sites.
The orchestrator passes `self._filter_adjustments`.

---

## 6. Strategy Trend Gate Override

### Problem

`apply_entry_gate` in `MomentumStrategy` and `SwingStrategy` has hard trend-structure blocks
(`block_bearish_trend`, `block_bearish_aligned`) that fire regardless of Claude's expressed intent.
When Claude specifies `entry_conditions` for a trade, these quant gates silently override Claude's
deliberate decision — the opposite of intended behavior.

### Fix

`apply_entry_gate` receives the `entry_conditions` dict (already present in `ScoredOpportunity`).
When `entry_conditions` is non-empty (Claude explicitly specified TA gates for this entry):
- Skip `block_bearish_trend` / `block_bearish_aligned` checks
- Log at DEBUG: `"Trend gate yielded to Claude entry_conditions for {symbol}"`

When `entry_conditions` is empty or absent: existing trend gate behavior unchanged.

**Files to change:**
- `ozymandias/intelligence/strategies/momentum_strategy.py`
- `ozymandias/intelligence/strategies/swing_strategy.py`
- `ozymandias/intelligence/opportunity_ranker.py` (pass `entry_conditions` through to
  `apply_entry_gate` call site)

The `Strategy` ABC's `apply_entry_gate` signature gains two new parameters:
`entry_conditions: dict = {}` and `filter_adjustments: dict | None = None`.
All strategy subclasses update their signature accordingly.

---

## 7. Regime Condition Expiry

`regime_assessment.valid_until_conditions` is a list of strings describing conditions under which
the reasoning cache should be forcibly expired.

### Supported condition keys

Check on every slow-loop trigger evaluation (`_check_triggers`):

| Condition string | Evaluates true when |
|---|---|
| `"SPY daily RSI > N"` | `_daily_indicators.get("SPY", {}).get("rsi_14d", 0) > N` |
| `"SPY daily RSI < N"` | `_daily_indicators.get("SPY", {}).get("rsi_14d", 100) < N` |
| `"VIX < N"` | Market context VIX value < N (if tracked) |
| `"daily_trend == uptrend"` | `_daily_indicators.get("SPY", {}).get("daily_trend") == "uptrend"` |
| `"daily_trend == downtrend"` | `_daily_indicators.get("SPY", {}).get("daily_trend") == "downtrend"` |

Parse the condition string with a simple regex match — not LLM evaluation. Unknown condition
formats log at DEBUG and are ignored (do not expire the cache).

When any condition evaluates to true: call `_expire_reasoning_cache()` (or equivalent). Log at INFO:
`"Regime condition met — expiring reasoning cache: {condition}"`.

Add a helper: `_check_regime_conditions() -> bool` — returns True if any condition triggered.
Call it at the start of `_check_triggers` alongside the existing trigger checks.

---

## 8. Prompt v3.10.0

Create `config/prompts/v3.10.0/` by copying all files from `v3.9.0/`. Update `reasoning.txt` with:

**New input sections (add after existing market context section):**

```
SECTOR DISPERSION — WATCHLIST RELATIVE PERFORMANCE (1-WEEK VS SECTOR ETF):
{sector_dispersion_json}

RECENT CANDIDATE REJECTIONS (last cycle filter failures):
{recent_rejections_json}

MARKET NEWS THEMES BY SECTOR:
{news_themes_json}
```

**New output fields (add to existing JSON schema section):**

```
"regime_assessment": {
  "regime": "risk-off panic | sector rotation | normal | euphoria",
  "confidence": 0.0-1.0,
  "key_signals": ["list of 2-4 specific signals driving this assessment"],
  "valid_until_conditions": ["SPY daily RSI > 40", "VIX < 20"],
  "implications": "One sentence on what this means for trade selection"
},
"sector_regimes": {
  "<SECTOR_ETF>": {
    "regime": "breaking_out | uptrend | neutral | correcting | downtrend",
    "bias": "long | short | either",
    "strength": "strong | moderate | weak"
  }
},
"filter_adjustments": {
  "min_rvol": 0.6,
  "min_composite_score": 0.40,
  "reason": "Why you're adjusting these floors for current conditions"
},
"active_theses": [
  {
    "symbol": "BG",
    "thesis": "Brief thesis summary",
    "thesis_breaking_conditions": ["daily_trend becomes downtrend", "sector_1w_return < -5%"]
  }
]
```

**New instruction block (add to strategic reasoning section):**

```
REGIME ASSESSMENT:
- Assess the current market regime based on SPY/QQQ daily indicators, sector dispersion, and
  recent rejection patterns.
- Set valid_until_conditions to 2-4 measurable signals that would indicate the regime has changed.
  Use specific numeric thresholds (e.g. "SPY daily RSI > 40") not vague descriptions.
- If recent_rejections shows consistent failures in a sector, consider adjusting filter_adjustments
  to allow contrarian setups — but only if market context justifies it. Provide a specific reason.
- For active open positions, emit active_theses with thesis_breaking_conditions that would trigger
  a position review. These are checked mechanically — use specific, measurable conditions.
- sector_regimes: only include sectors that have symbols on the current watchlist. Omit others.
- All four fields are optional — omit any field if you have no meaningful assessment.
```

Update `config.json`: `claude.prompt_version` → `"v3.10.0"`.

---

## 9. Files Changed

| File | Change |
|------|--------|
| `ozymandias/intelligence/technical_analysis.py` | `compute_sector_dispersion()` helper |
| `ozymandias/core/orchestrator.py` | Daily bar fetch for all watchlist symbols; `sector_dispersion` + `recent_rejections` + `news_themes` in `_build_market_context`; `_filter_adjustments` stored + reset; regime condition expiry check in `_check_triggers` |
| `ozymandias/intelligence/claude_reasoning.py` | Four new fields on `ReasoningResult`; parsing in `_parse_reasoning_result` |
| `ozymandias/intelligence/opportunity_ranker.py` | `filter_adjustments` applied with floor guards; `entry_conditions` passed to `apply_entry_gate` |
| `ozymandias/intelligence/strategies/momentum_strategy.py` | `apply_entry_gate` yields trend block when `entry_conditions` non-empty |
| `ozymandias/intelligence/strategies/swing_strategy.py` | Same |
| `ozymandias/core/config.py` | `filter_adj_min_rvol`, `filter_adj_min_composite` in `RankerConfig` |
| `ozymandias/config/config.json` | Two new `ranker` keys; `prompt_version` → `v3.10.0` |
| `ozymandias/config/prompts/v3.10.0/` | New directory; updated `reasoning.txt`; all other files copied from v3.9.0 |

---

## 10. Tests to Write

Add to `tests/test_technical_analysis.py`:

- `compute_sector_dispersion` returns empty dict when fewer than 2 symbols map to any sector
- `compute_sector_dispersion` correctly computes `vs_sector_1w = symbol_roc - etf_roc`
- `outperforming` contains top 3 by `vs_sector_1w`; `underperforming` contains bottom 3
- Symbols with missing `roc_5d` in `daily_indicators` are skipped
- Sector ETFs with no daily data are omitted from result
- Returns `{}` when `watchlist_entries` is empty

Add to `tests/test_claude_reasoning.py` (or new `tests/test_reasoning_result_parsing.py`):

- `regime_assessment` parsed correctly when present in Claude JSON output
- `sector_regimes` parsed correctly; unknown regime value stored as-is (no validation crash)
- `filter_adjustments` parsed correctly; missing field → `None`
- `active_theses` parsed as list of dicts; malformed entry → field set to `None`, no raise
- All four fields absent from Claude output → all remain `None` in `ReasoningResult`

Add to `tests/test_opportunity_ranker.py`:

- `filter_adjustments` with `min_composite_score=0.40` overrides default when above floor
- `filter_adjustments` with `min_composite_score=0.30` is clamped to `filter_adj_min_composite=0.35`
- `filter_adjustments=None` → default thresholds used unchanged

Add to `tests/test_strategies.py`:

- `apply_entry_gate` with non-empty `entry_conditions` does not block on bearish trend signal
- `apply_entry_gate` with empty `entry_conditions` retains existing trend block behavior

---

## Implementation Notes

- **`filter_adjustments` lost on mid-cycle API error.** `_filter_adjustments` resets to `None`
  at cycle start and repopulates from the result. If the Claude call errors out, the reset fires
  but repopulation doesn't — thresholds silently fall back to config defaults until the next
  successful cycle. This is the correct safe behavior but will look like a bug when you see
  filters tighten immediately after a 529. Don't add recovery logic; just note it.

- **Trend gate yields on *any* non-empty `entry_conditions`, not just trend-related ones.**
  A Claude opportunity with only `rsi_min: 55` still bypasses `block_bearish_aligned`. This is
  intentional — Claude specifying conditions at all means it was reasoning deliberately about
  this entry. If strong-downtrend entries start slipping through unexpectedly, the fix is in
  the prompt, not the gate logic.

## Done When

- `compute_sector_dispersion` exists in `technical_analysis.py`; all sector_dispersion tests pass
- `recent_rejections` and `news_themes` populated in `_build_market_context` output
- `ReasoningResult` has all four new fields; all parse correctly without crashing on absent/malformed data
- `filter_adjustments` applied in ranker with config floor guards; clamping test passes
- `apply_entry_gate` yields trend block when `entry_conditions` non-empty; test passes
- Regime condition expiry: `_check_regime_conditions()` method exists; parses `"SPY daily RSI > N"`
  format and expires cache when condition true
- Prompt `v3.10.0` directory exists; `reasoning.txt` has all four new output fields documented
- `config.json`: `prompt_version = "v3.10.0"`, `filter_adj_min_rvol` and `filter_adj_min_composite` present
- All existing tests pass
- DRIFT_LOG.md has a Phase 19 entry covering: `compute_sector_dispersion`, four new `ReasoningResult`
  fields, `_filter_adjustments` on orchestrator, `filter_adj_min_*` config floors, trend gate
  `entry_conditions` override, regime condition expiry mechanism, prompt v3.10.0
