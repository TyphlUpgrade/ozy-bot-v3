# Phase 20: Haiku Operational Layer — Regime-Aware Pre-Screening

Read the Phase 19 section of DRIFT_LOG.md before starting. This phase assumes Phase 19 is complete.

---

## Motivation

Phase 19 gives Sonnet the ability to assess market regime and emit `regime_assessment` and
`sector_regimes`. Phase 20 inserts a cheap Haiku pre-screener that consumes those outputs each
medium loop, ranking candidates cross-tier before strategic context assembly and flagging conditions
that warrant immediate Sonnet re-evaluation. Haiku does not re-derive regime — it inherits Sonnet's
cached assessment and acts as a fast, cheap operator on top of it.

The `ContextCompressor` concept from the original Phase 19 spec is implemented here, now extended
with regime awareness and a `needs_sonnet` escalation path.

---

## 1. `ContextCompressor` Module

Create `ozymandias/intelligence/context_compressor.py`.

### Data structures

```python
@dataclass
class CompressorResult:
    symbols: list[str]           # ordered by priority
    rationale: dict[str, str]    # symbol → one-line rationale (debug logging only)
    notes: str                   # Haiku's overall observation
    from_fallback: bool          # True if deterministic fallback was used
    needs_sonnet: bool           # True if Haiku flags a condition requiring Sonnet
    sonnet_reason: str | None    # "regime_shift" | "all_candidates_failing" |
                                 # "position_thesis_breach" | "watchlist_stale" | None
```

### Typed `sonnet_reason` values

| Value | Meaning |
|---|---|
| `"regime_shift"` | Haiku sees signals inconsistent with Sonnet's current `regime_assessment` |
| `"all_candidates_failing"` | All candidates have weak signals vs. regime expectations |
| `"position_thesis_breach"` | A position's `thesis_breaking_conditions` are now met |
| `"watchlist_stale"` | Haiku finds the candidate pool irrelevant to current market context |

`needs_sonnet=True` from fallback path: always `False` (fallback has no regime awareness).

### Class interface

```python
class ContextCompressor:
    def __init__(self, claude_client, config: "ClaudeConfig", logger) -> None: ...

    async def compress(
        self,
        candidates: list[dict],           # all symbols across tier1 + tier2
        market_data: dict,
        regime_assessment: dict | None,   # from Sonnet's cached ReasoningResult
        sector_regimes: dict | None,      # from Sonnet's cached ReasoningResult
        active_theses: list[dict] | None, # from Sonnet's cached ReasoningResult
        max_out: int,
    ) -> CompressorResult: ...
```

### Candidate dict schema

Each entry in `candidates`:
- `symbol: str`
- `tier: str` — `"tier1"` or `"tier2"`
- `composite_score: float`
- `signals: str` — compact summary from `_make_technical_summary()` format
- `strategy: str`
- `reason: str` — watchlist entry reason
- `expected_direction: str | None` — from `WatchlistEntry.expected_direction`

Symbols with no indicator data: include with `composite_score=0.0`, `signals="no data"`.

### `compress()` implementation

1. Build prompt using `config/prompts/{prompt_version}/compress.txt`. Pass:
   - `{candidates_json}`: compact JSON of candidates
   - `{market_context_json}`: subset of `market_data` (SPY/QQQ daily, VIX if present)
   - `{regime_assessment_json}`: Sonnet's `regime_assessment` (or `"none"`)
   - `{sector_regimes_json}`: Sonnet's `sector_regimes` (or `"none"`)
   - `{active_theses_json}`: Sonnet's `active_theses` (or `"[]"`)
   - `{max_symbols}`: equal to `max_out`

2. Call Haiku with `max_tokens = config.compressor_max_tokens` (default 512).

3. Parse via 4-step defensive pipeline: strip fences → `json.loads` → regex extract → fallback.

4. Extract from parsed JSON:
   - `actionable`: list of `{symbol, rationale}` in priority order
   - `notes`: string
   - `needs_sonnet`: bool (default `false` if absent)
   - `sonnet_reason`: string or null

5. Cap symbol list at `max_out`.

6. Return `CompressorResult(symbols=..., rationale=..., notes=..., from_fallback=False,
   needs_sonnet=..., sonnet_reason=...)`.

**Deterministic fallback** (on any exception or parse failure):
- Sort `candidates` by `composite_score` descending
- Take top `max_out` symbols
- Return `CompressorResult(symbols=..., rationale={}, notes="", from_fallback=True,
  needs_sonnet=False, sonnet_reason=None)`
- Log `WARNING: context compressor fallback — {exception description}`
- Wrap entire non-fallback path in broad `try/except Exception`

### Regime-aware ranking behavior (Haiku instructions)

When `sector_regimes` is provided, Haiku deprioritizes:
- Candidates with `expected_direction == "long"` in sectors with `regime == "correcting"` or `"downtrend"`

And surfaces higher:
- Candidates with `expected_direction == "short"` in correcting sectors
- Candidates with `expected_direction == "long"` in `"breaking_out"` sectors

This makes pre-screening regime-aware without re-calling Sonnet. The ranking logic lives in the
Haiku prompt, not in Python — Haiku decides how to weight these signals.

### Thesis breach check

In the prompt, instruct Haiku: for each entry in `active_theses`, if any `thesis_breaking_condition`
for that symbol appears to be met given the current market data and signals, set `needs_sonnet=true`
and `sonnet_reason="position_thesis_breach"`.

**Per-generation guard:** `_needs_sonnet_fired: bool` flag on the orchestrator. Reset to `False`
when a new Sonnet cycle completes. If `True`, the compressor result's `needs_sonnet` is ignored
(Haiku cannot fire escalation twice per Sonnet cycle). This prevents Haiku from triggering Sonnet
on every medium loop while a position thesis breach is being reviewed.

---

## 2. Prompt Template: `compress.txt`

Add `compress.txt` to `config/prompts/v3.10.0/` (current prompt version after Phase 19).

```
You are a technical pre-screener for a momentum/swing trading bot. Your job is to select the
{max_symbols} highest-quality setups from the candidate list and flag any conditions requiring
strategic re-evaluation.

CURRENT REGIME ASSESSMENT (from strategic agent):
{regime_assessment_json}

SECTOR REGIMES:
{sector_regimes_json}

ACTIVE POSITION THESES:
{active_theses_json}

CANDIDATES (tier1 = established conviction, tier2 = newer addition):
{candidates_json}

MARKET CONTEXT:
{market_context_json}

INSTRUCTIONS:
1. Select the {max_symbols} best setups ranked by signal strength and regime alignment.
   - Prefer candidates whose expected_direction aligns with sector_regimes bias.
   - Deprioritize candidates whose direction conflicts with their sector's current regime.
   - When signals are roughly equal, prefer tier1 symbols.
2. Check active_theses: if any thesis_breaking_condition appears met for an open position
   based on current signals and market context, set needs_sonnet=true.
3. If the current market data appears inconsistent with the regime_assessment (e.g., SPY
   is recovering strongly but regime says "risk-off panic"), set needs_sonnet=true and
   sonnet_reason="regime_shift".
4. If all candidates have weak signals relative to what the current regime expects,
   set needs_sonnet=true and sonnet_reason="all_candidates_failing".

Respond with only valid JSON:
{
  "actionable": [
    {"symbol": "NVDA", "rationale": "VWAP reclaim + RSI 62, XLK breaking out"}
  ],
  "notes": "Optional single sentence on overall selection.",
  "needs_sonnet": false,
  "sonnet_reason": null
}

Do not include symbols not in the candidates list. Output only the JSON object.
```

---

## 3. New `ClaudeConfig` Fields

In `ozymandias/core/config.py`, add to `ClaudeConfig`:

```python
compressor_enabled: bool = True
compressor_model: str = "claude-haiku-4-5-20251001"
compressor_max_symbols_out: int = 18   # matches tier1_max_symbols
compressor_max_tokens: int = 512
```

In `config/config.json`, add to `claude` section:
```json
"compressor_enabled": true,
"compressor_model": "claude-haiku-4-5-20251001",
"compressor_max_symbols_out": 18,
"compressor_max_tokens": 512
```

---

## 4. `_build_all_candidates()` Helper

Add private method to `ClaudeReasoning`:

```python
def _build_all_candidates(
    self,
    watchlist: WatchlistState,
    indicators: dict[str, dict],
) -> list[dict]:
```

Iterates all entries in `watchlist.entries` (tier1 and tier2). For each symbol:
- Look up `indicators.get(symbol, {})` for `composite_score` and raw signals
- Build compact `signals` string matching `_make_technical_summary()` format
- Include `expected_direction` from `WatchlistEntry.expected_direction`
- Include `tier` (`"tier1"` if `entry.priority_tier == 1` else `"tier2"`)
- Symbols with no indicator data: `composite_score=0.0`, `signals="no data"`

Returns list of candidate dicts per schema in Section 1.

---

## 5. Integration into `run_reasoning_cycle()`

### Instantiation

In `ClaudeReasoning.__init__`:
```python
self._compressor = ContextCompressor(self._client, self._config, self._log)
```

### Signature change

Add `all_indicators: dict | None = None` as an optional parameter to `run_reasoning_cycle()`.
The orchestrator passes `self._all_indicators` here (same pattern as `daily_indicators`).

`_all_indicators` lives on the orchestrator — it is NOT stored on `ClaudeReasoning`. It must be
passed as a parameter so macro-tracked symbols (SPY, QQQ, sector ETFs) appear with real signals
rather than `"no data"` in the candidate list.

### Pre-screening call

Before `assemble_reasoning_context()`, add:

```python
all_candidates = self._build_all_candidates(watchlist, all_indicators or {})
selected_symbols: list[str] | None = None
compressor_result: CompressorResult | None = None

if (
    self._config.compressor_enabled
    and len(all_candidates) > self._config.tier1_max_symbols
):
    # Pass Sonnet's cached regime outputs from the previous cycle's result
    # (stored on ClaudeReasoning as _last_regime_assessment, etc. — see below)
    compressor_result = await self._compressor.compress(
        all_candidates,
        market_data,
        regime_assessment=self._last_regime_assessment,
        sector_regimes=self._last_sector_regimes,
        active_theses=self._last_active_theses,
        max_out=self._config.compressor_max_symbols_out,
    )
    selected_symbols = compressor_result.symbols
    if compressor_result.from_fallback:
        self._log.warning("Compressor fallback — proceeding with deterministic ranking")
    else:
        self._log.debug(
            "Compressor selected %d symbols: %s | needs_sonnet=%s | notes: %s",
            len(selected_symbols), selected_symbols,
            compressor_result.needs_sonnet, compressor_result.notes,
        )
```

After `run_reasoning_cycle` completes, store the new regime outputs on `ClaudeReasoning`:
```python
self._last_regime_assessment = result.regime_assessment
self._last_sector_regimes = result.sector_regimes
self._last_active_theses = result.active_theses
```

Initialize all three to `None` in `__init__`.

### Return `compressor_result`

`run_reasoning_cycle` must surface `compressor_result` to the orchestrator. Options:
- Add `compressor_result` to the return value (preferred: return a tuple or extend the result
  dataclass)
- OR store it on orchestrator directly via a side channel

**Preferred approach:** `run_reasoning_cycle` returns `tuple[ReasoningResult | None, CompressorResult | None]`. Update all call sites.

### `needs_sonnet` handling in orchestrator

After `run_reasoning_cycle` returns:

```python
result, compressor_result = await self._claude.run_reasoning_cycle(...)

if (
    compressor_result is not None
    and compressor_result.needs_sonnet
    and not self._needs_sonnet_fired
):
    self._needs_sonnet_fired = True
    self._log.info(
        "Haiku flagged needs_sonnet: %s — scheduling immediate Sonnet cycle",
        compressor_result.sonnet_reason,
    )
    # Fire an immediate Sonnet reasoning cycle
    await self._run_slow_loop_trigger("haiku_escalation")
```

Reset `self._needs_sonnet_fired = False` at the start of each new Sonnet cycle.

---

## 6. `assemble_reasoning_context()` Modification

Add optional parameter:

```python
def assemble_reasoning_context(
    self,
    watchlist: WatchlistState,
    ...
    selected_symbols: list[str] | None = None,
) -> dict:
```

When `selected_symbols` provided:
- Use as the ordered symbol list for the watchlist section, regardless of tier
- Apply the same per-symbol data assembly as before
- Cap at `len(selected_symbols)` (pre-capped by `compressor_max_symbols_out`)
- Skip symbols in `selected_symbols` not found in watchlist or indicators (log DEBUG)

When `selected_symbols is None`: existing Phase 15 tier1 composite-score sort (unchanged).

---

## 7. Suppression Exhaustion Trigger

**Problem:** When all Claude-recommended candidates are session-suppressed, the bot sits idle until
an external trigger fires. On a low-volatility day, this can mean no new entries for hours.

**Fix:** In the medium loop, after suppression filtering, detect when all current cache candidates
are suppressed. Set `self._candidates_exhausted_pending = True`. In `_check_triggers`, fire
`"candidates_exhausted"` once when this flag is set, then clear it.

**Condition for firing:**
- Reasoning cache has at least one opportunity (Claude ran and produced candidates)
- Every symbol in current `new_opportunities` is either in `_filter_suppressed` or defer-exhausted
- `_candidates_exhausted_pending` is True and has not fired for this reasoning cycle generation

**Behavior:** Same as any slow-loop trigger — calls `run_reasoning_cycle` immediately with fresh
context. Claude sees the suppression list via `recent_rejections` (Phase 19) and can respond
with regime-aligned candidates or `filter_adjustments` to unblock.

Add `"candidates_exhausted"` to trigger logging. No new config key — fires whenever conditions met.
Reset `_candidates_exhausted_pending` when a new Sonnet cycle completes.

Initialize `self._candidates_exhausted_pending: bool = False` in orchestrator `__init__`.

---

## 8. Backward Compatibility

- `compressor_enabled=False`: `selected_symbols` stays `None`; `assemble_reasoning_context` uses
  Phase 15 tier1 sort. Zero behavioral change.
- Watchlist count ≤ `tier1_max_symbols`: compressor gate condition false; no call made.
- Haiku call failure: fallback returns deterministic sorted list; `needs_sonnet=False`;
  main Sonnet cycle proceeds normally.
- Orchestrator: `_needs_sonnet_fired` guard ensures Haiku can only escalate once per Sonnet cycle.
- `run_reasoning_cycle` return type change: update all call sites in orchestrator.

---

## 9. Files Changed

| File | Change |
|------|--------|
| `ozymandias/intelligence/context_compressor.py` | New module: `ContextCompressor`, `CompressorResult` |
| `ozymandias/intelligence/claude_reasoning.py` | `_build_all_candidates()`; compressor instantiation; `run_reasoning_cycle` accepts `all_indicators`, returns `(result, compressor_result)`; `assemble_reasoning_context` accepts `selected_symbols`; `_last_regime_assessment/sector_regimes/active_theses` cache |
| `ozymandias/core/orchestrator.py` | Passes `all_indicators=self._all_indicators` to `run_reasoning_cycle`; handles `needs_sonnet` + `_needs_sonnet_fired` guard; `_candidates_exhausted_pending` exhaustion trigger |
| `ozymandias/core/config.py` | Four new `ClaudeConfig` compressor fields |
| `ozymandias/config/config.json` | Four compressor keys in `claude` section |
| `ozymandias/config/prompts/v3.10.0/compress.txt` | New Haiku prompt (add to existing v3.10.0 dir) |

---

## 10. Tests to Write

Create `tests/test_context_compressor.py`:

- Normal path: Haiku returns valid JSON with `actionable` array → `symbols` match expected order,
  `from_fallback=False`, `needs_sonnet` correctly parsed
- `needs_sonnet=True` with `sonnet_reason` returned correctly
- Respects `max_out` cap: Haiku returns 15 symbols, `max_out=12` → exactly 12
- API failure → fallback: `from_fallback=True`, `needs_sonnet=False`, symbols sorted by score desc
- Malformed JSON → fallback: deterministic sort, `needs_sonnet=False`
- Empty candidates → `CompressorResult(symbols=[], ...)` without crashing
- Fallback sort order: scores `[0.3, 0.9, 0.6]` → order `[0.9, 0.6, 0.3]`
- Fallback respects `max_out`: 20 candidates, `max_out=5` → 5 symbols

Create `tests/test_context_compression_integration.py`:

- Compressor fires when over threshold: 20 symbols, `tier1_max_symbols=18`, `enabled=True`
  → `compress()` called, `assemble_reasoning_context` receives `selected_symbols`
- Compressor skips when under threshold: 15 symbols → `compress()` not called
- Compressor skips when disabled: `compressor_enabled=False`, 20 symbols → not called
- `assemble_reasoning_context` uses `selected_symbols`: pass `["NVDA", "TSLA"]` → context includes
  those symbols in that order, excludes others
- `assemble_reasoning_context` unchanged when `selected_symbols=None`: Phase 15 tier1 sort
- Symbol not in watchlist skipped: `selected_symbols` has absent symbol → skipped, rest assembled
- `needs_sonnet=True` → orchestrator fires immediate Sonnet cycle (mock orchestrator trigger)
- `_needs_sonnet_fired` guard: second `needs_sonnet=True` in same cycle is ignored
- Candidates exhaustion trigger fires when all cache candidates are suppressed
- Exhaustion trigger does not fire when no candidates in cache

---

## Done When

- `ozymandias/intelligence/context_compressor.py` exists with `ContextCompressor` and `CompressorResult`
- `compress.txt` exists in `config/prompts/v3.10.0/`
- `ClaudeConfig` has all four compressor fields with correct defaults; `config.json` matches
- `run_reasoning_cycle` accepts `all_indicators` and returns `(ReasoningResult | None, CompressorResult | None)`
- `assemble_reasoning_context` accepts `selected_symbols` and routes correctly
- Orchestrator handles `needs_sonnet` with `_needs_sonnet_fired` guard
- Candidates exhaustion trigger implemented; `"candidates_exhausted"` appears in trigger log
- `compressor_enabled=False` passes integration test confirming unchanged behavior
- All existing tests pass; all new tests pass
- DRIFT_LOG.md has a Phase 20 entry covering: `ContextCompressor` module, `CompressorResult`
  (`needs_sonnet` + `sonnet_reason` fields), `run_reasoning_cycle` signature and return type change,
  `assemble_reasoning_context` `selected_symbols` param, `_needs_sonnet_fired` guard, candidates
  exhaustion trigger
