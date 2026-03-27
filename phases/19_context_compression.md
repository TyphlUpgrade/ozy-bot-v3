# Phase 19: Context Compression Layer — Haiku Pre-Screener for Watchlist Quality Sorting

Read the Phase 16 section of DRIFT_LOG.md and the `Post-MVP Roadmap` section of CLAUDE.md before starting. This phase assumes Phases 11, 14, 15, 16, 17, and 18 are complete.

Phase 15 replaced insertion-order symbol selection with composite-score sorting within the tier1 watchlist. This works well when all candidates are tier1, but Phase 18 introduces a dynamic universe that populates tier2 with live RVOL-ranked candidates from the broader market. Once tier2 is populated, the existing tier1-only score sort misses high-quality tier2 names in favor of stale tier1 entries with weaker current signals. This phase inserts a cheap Haiku pre-screener that evaluates all watchlist symbols across both tiers and returns a quality-ranked shortlist before the strategic agent assembles its context. The pre-screener does not reduce opportunity count — it replaces single-tier score sorting with cross-tier signal-strength ranking.

## 1. New Module: `intelligence/context_compressor.py`

Create `ozymandias/intelligence/context_compressor.py`.

### Data structures

```python
@dataclass
class CompressorResult:
    symbols: list[str]           # ordered by priority — these become the selected candidates
    rationale: dict[str, str]    # symbol → one-line rationale (for debug logging only)
    notes: str                   # compressor's overall market observation
    from_fallback: bool          # True if deterministic fallback was used instead of Haiku
```

### Class interface

```python
class ContextCompressor:
    def __init__(self, claude_client, config: "ClaudeConfig", logger) -> None: ...

    async def compress(
        self,
        candidates: list[dict],   # all symbols: {symbol, tier, composite_score, signals, strategy, reason}
        market_data: dict,
        max_out: int,
    ) -> CompressorResult: ...
```

### `compress()` implementation

1. Build the prompt using the template at `config/prompts/{prompt_version}/compress.txt`. Pass `{candidates_json}` (compact JSON of the candidates list), `{market_context_json}` (subset of `market_data`), and `{max_symbols}` (equal to `max_out`).
2. Call Haiku with `max_tokens = config.compressor_max_tokens` (default 512).
3. Parse the response through the same 4-step defensive pipeline used elsewhere: strip fences → `json.loads` → regex extract → fallback. On any parse failure, fall through to the deterministic fallback.
4. Extract `actionable` array from parsed JSON; take the `symbol` field from each entry in order, capped at `max_out`.
5. Return `CompressorResult(symbols=..., rationale={...}, notes=..., from_fallback=False)`.

**Deterministic fallback** (used on any exception or parse failure):
- Sort `candidates` by `composite_score` descending.
- Take top `max_out` symbols in that order.
- Return `CompressorResult(symbols=..., rationale={}, notes="", from_fallback=True)`.
- Log `WARNING: context compressor fallback — {exception description}`.

The fallback path must never raise. Wrap the entire non-fallback path in a broad `try/except Exception`.

### Candidate dict structure

Each entry in `candidates` has:
- `symbol: str`
- `tier: str` — `"tier1"` or `"tier2"`
- `composite_score: float`
- `signals: str` — compact human-readable summary matching `_make_technical_summary()` output, e.g. `"RSI 62, VWAP above, MACD bullish_cross, vol×1.8, composite 0.78"`
- `strategy: str` — e.g. `"momentum"`, `"mean_reversion"`
- `reason: str` — the Claude-assigned reason this symbol is on the watchlist (from `WatchlistEntry.reason`)

At 40 symbols × ~80 chars each, the candidates JSON is approximately 500 input tokens — well within Haiku budget.

## 2. New Prompt Template: `compress.txt`

Add `compress.txt` to the existing `config/prompts/v3.9.0/` directory (current prompt version as of Phase 19). No new prompt version directory or version bump is needed — this adds a new template file to the current version without changing any existing prompt content.

**`compress.txt` content:**

```
You are a technical pre-screener for a momentum/swing trading bot. Your job is to select the {max_symbols} highest-quality setups from the candidate list below based on current TA signals.

Candidates (tier1 = established conviction, tier2 = newer addition):
{candidates_json}

Market context:
{market_context_json}

Select the {max_symbols} best setups ranked by current TA signal strength. When signals are roughly equal across candidates, prefer tier1 symbols — they carry established strategic conviction. For each selected symbol, provide one sentence of rationale citing specific signal values from the candidates data.

Respond with only valid JSON matching this schema exactly:
{
  "actionable": [
    {"symbol": "NVDA", "rationale": "VWAP reclaim + RSI 62 with 1.8× volume confirms momentum entry"}
  ],
  "notes": "Optional single sentence about overall market conditions affecting selection."
}

Do not include symbols not present in the candidates list. Output only the JSON object.
```

## 3. New `ClaudeConfig` Fields

In `ozymandias/core/config.py`, add to `ClaudeConfig`:

```python
compressor_enabled: bool = True
compressor_model: str = "claude-haiku-4-5-20251001"
compressor_max_symbols_out: int = 18   # defaults to equal tier1_max_symbols
compressor_max_tokens: int = 512
```

In `ozymandias/config/config.json`, add to the `claude` section:
```json
"compressor_enabled": true,
"compressor_model": "claude-haiku-4-5-20251001",
"compressor_max_symbols_out": 18,
"compressor_max_tokens": 512
```

`compressor_max_symbols_out` defaults to match `tier1_max_symbols` (currently 18). Raising it adds symbols to the strategic agent's context; lowering it reduces opportunity count (not recommended without a clear reason).

## 4. Integration into `run_reasoning_cycle()`

In `claude_reasoning.py`, `run_reasoning_cycle()` is already `async` — the compressor call fits naturally here.

### Helper: `_build_all_candidates()`

Add a private method to `ClaudeReasoning`:

```python
def _build_all_candidates(
    self,
    watchlist: WatchlistState,
    indicators: dict[str, dict],
) -> list[dict]:
```

Iterates all entries in `watchlist.entries` (tier1 and tier2). For each symbol:
- Looks up `indicators.get(symbol, {})` for `composite_score` and raw signals
- Builds the compact `signals` string matching `_make_technical_summary()` format
- Returns list of candidate dicts (schema from Section 1)
- Symbols with no indicator data are included with `composite_score=0.0` and `signals="no data"` — the compressor or fallback will rank them last

### Instantiate compressor

In `ClaudeReasoning.__init__`, instantiate:
```python
self._compressor = ContextCompressor(self._client, self._config, self._log)
```

Import `ContextCompressor` from `intelligence/context_compressor.py`.

### Pre-screening call in `run_reasoning_cycle()`

Add `all_indicators: dict | None = None` as a new optional parameter to `run_reasoning_cycle()` (same pattern as `daily_indicators`). The orchestrator passes `self._all_indicators` here.

`_all_indicators` lives on the orchestrator (set by Phase 17 at the end of each `_medium_loop_cycle` as the merged dict of `_latest_indicators` + `_market_context_indicators`). It does NOT exist as `self._all_indicators` on `ClaudeReasoning` — it must be passed in as a parameter. This ensures macro-tracked symbols (SPY, QQQ, sector ETFs) that also appear on the watchlist are not marked `signals="no data"` in the candidate list.

Before the `assemble_reasoning_context()` call, add:

```python
all_candidates = self._build_all_candidates(watchlist, all_indicators or {})
selected_symbols: list[str] | None = None

if (
    self._config.compressor_enabled
    and len(all_candidates) > self._config.tier1_max_symbols
):
    compressor_result = await self._compressor.compress(
        all_candidates,
        market_data,
        self._config.compressor_max_symbols_out,
    )
    selected_symbols = compressor_result.symbols
    if compressor_result.from_fallback:
        self._log.warning(
            "Compressor fallback used — proceeding with deterministic ranking"
        )
    else:
        self._log.debug(
            "Compressor selected %d symbols: %s | notes: %s",
            len(selected_symbols),
            selected_symbols,
            compressor_result.notes,
        )
```

Pass `selected_symbols` to `assemble_reasoning_context()`.

The orchestrator's `run_reasoning_cycle` call already passes `daily_indicators=self._daily_indicators`; add `all_indicators=self._all_indicators` alongside it.

### Modify `assemble_reasoning_context()`

`assemble_reasoning_context()` is currently synchronous — it stays synchronous. Add a new optional parameter:

```python
def assemble_reasoning_context(
    self,
    watchlist: WatchlistState,
    ...
    selected_symbols: list[str] | None = None,
) -> dict:
```

Existing behavior: when building the tier1 watchlist section, it takes symbols where `entry.priority_tier == 1`, up to `tier1_max_symbols`, sorted by composite score (Phase 15 behavior).

New behavior with `selected_symbols` provided:
- Use `selected_symbols` as the ordered list of symbols for the watchlist section, regardless of `priority_tier`
- Apply the same per-symbol data assembly (indicators, positions, etc.) as before — only the selection and ordering changes
- Still cap at `len(selected_symbols)` (already pre-capped by `compressor_max_symbols_out`)
- If a symbol in `selected_symbols` is not found in `watchlist` or `indicators`, skip it with a DEBUG log

When `selected_symbols is None`: existing behavior unchanged.

## 5. Backward Compatibility

- `compressor_enabled=False`: `selected_symbols` remains `None` throughout; `assemble_reasoning_context()` uses the Phase 15 tier1 composite-score sort. Zero behavioral change.
- Watchlist is all-tier1 (pre-Phase 18): the compressor gate fires for any watchlist larger than `tier1_max_symbols` (18) and selects the best 18 from the tier1 pool — providing marginal improvement over Phase 15's same-tier score sort. Full cross-tier value is realized once Phase 18 populates tier2.
- Watchlist count ≤ `tier1_max_symbols`: compressor gate condition is False; no call made regardless of `compressor_enabled`.
- Haiku call failure: fallback returns deterministic sorted list; main reasoning cycle proceeds normally.
- Prompt version gate: `compress.txt` is only loaded when the compressor fires. If the prompt version directory lacks `compress.txt`, the compressor must raise a clear `FileNotFoundError` (caught by the broad `try/except`, triggers fallback).

## 6. Suppression Exhaustion Trigger (Orchestrator)

**Problem:** When all Claude-recommended candidates are suppressed by the ranker's hard filters, the active candidate pool drops to zero. No recovery mechanism exists — the bot sits idle until an external slow-loop trigger fires (price move, time ceiling, session transition). In a low-volatility bearish session, this can mean no new entries for the entire remaining session.

**Fix:** In `_medium_try_entry` (or just after `_update_filter_suppression` in the medium loop), detect when suppression has consumed all current Claude recommendations. When this occurs, set a `_candidates_exhausted_pending` flag. The slow loop trigger check (`_check_triggers`) should fire `"candidates_exhausted"` once when this flag is set, then clear it.

**Condition for firing:**
- The reasoning cache has at least one opportunity (i.e. Claude ran and produced candidates)
- Every symbol in the current reasoning cache's `new_opportunities` list is either hard-filter suppressed (`_filter_suppressed`) or defer-exhausted (session-suppressed)
- The trigger has not already fired for this reasoning cache generation (clear flag when a new reasoning cycle completes)

**Behavior:** Same as any other slow-loop trigger — calls `run_reasoning_cycle` immediately. Claude sees fresh market context and produces a new candidate set.

**Why not just lower the cache TTL?** TTL-based re-triggering would call Claude even when the existing cache is still valid (e.g. ATO is pending entry, cache is fine). The exhaustion trigger fires precisely when the cache is stale-by-suppression, not on a timer.

Add `"candidates_exhausted"` to the trigger logging output. No new config key needed — this trigger always fires when conditions are met (it self-arms on each new reasoning cycle).

## 7. Tests to Write

Create `tests/test_context_compressor.py`:

- **Normal path**: mock Haiku returning valid JSON with `actionable` array → `CompressorResult.symbols` matches expected order, `from_fallback=False`
- **Respects `max_out` cap**: Haiku returns 15 symbols but `max_out=12` → result has exactly 12 symbols
- **API failure → fallback**: Haiku raises exception → `from_fallback=True`, symbols sorted by `composite_score` desc
- **Malformed JSON → fallback**: Haiku returns unparseable string → `from_fallback=True`, deterministic sort
- **Empty candidates**: `candidates=[]`, `max_out=12` → returns `CompressorResult(symbols=[], ..., from_fallback=False or True)` without crashing
- **Fallback sort order**: candidates with scores `[0.3, 0.9, 0.6]` → fallback returns symbols in order `[0.9, 0.6, 0.3]`
- **Fallback respects `max_out`**: 20 candidates, `max_out=5` → fallback returns 5 symbols

Create `tests/test_context_compression_integration.py`:

- **Compressor fires when over threshold**: watchlist has 20 symbols, `tier1_max_symbols=18`, `compressor_enabled=True` → `compress()` is called, `assemble_reasoning_context` receives `selected_symbols`
- **Compressor skips when under threshold**: watchlist has 15 symbols, `tier1_max_symbols=18` → `compress()` not called, `selected_symbols=None`
- **Compressor skips when disabled**: `compressor_enabled=False`, 20-symbol watchlist → `compress()` not called
- **`assemble_reasoning_context` uses `selected_symbols`**: pass `selected_symbols=["NVDA", "TSLA"]` → context string includes those symbols in that order, excludes others
- **`assemble_reasoning_context` unchanged when `selected_symbols=None`**: existing tier1 composite-score sort behavior (Phase 15)
- **Symbol not in watchlist skipped**: `selected_symbols` contains a symbol absent from watchlist → skipped silently, remaining symbols assembled normally

## Done When

- All existing tests pass; all new tests in `test_context_compressor.py` and `test_context_compression_integration.py` pass
- `ozymandias/intelligence/context_compressor.py` exists with `ContextCompressor` class and `CompressorResult` dataclass
- `compress.txt` exists in `config/prompts/v3.9.0/` (current prompt version)
- `config.json` has all four compressor keys present (no prompt version change)
- `ClaudeConfig` has all four new fields with correct defaults
- `run_reasoning_cycle()` accepts `all_indicators` parameter and calls compressor before `assemble_reasoning_context()` when conditions are met
- `assemble_reasoning_context()` accepts `selected_symbols` param and routes correctly
- `compressor_enabled=False` passes integration test confirming existing behavior unchanged
- Orchestrator passes `all_indicators=self._all_indicators` to `run_reasoning_cycle()` alongside `daily_indicators`
- Suppression exhaustion trigger implemented in orchestrator; fires `"candidates_exhausted"` when all cache candidates are suppressed
- DRIFT_LOG.md has a Phase 19 entry covering: new `ContextCompressor` module, new `ClaudeConfig` fields, `run_reasoning_cycle` signature change (`all_indicators` param), `assemble_reasoning_context` signature change (`selected_symbols` param), suppression exhaustion trigger
