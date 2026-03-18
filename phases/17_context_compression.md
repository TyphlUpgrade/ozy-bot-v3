# Phase 17: Context Compression Layer — Haiku Pre-Screener for Watchlist Quality Sorting

Read the Phase 16 section of DRIFT_LOG.md and the `Post-MVP Roadmap` section of CLAUDE.md before starting. This phase assumes Phases 11, 14, 15, and 16 are complete.

The strategic reasoning cycle currently selects its input symbols from the tier1 watchlist in insertion order, ignoring tier2 entirely. As the watchlist grows, insertion order is a poor proxy for signal quality: a tier1 name added weeks ago may have weaker current TA than a tier2 name added yesterday with a strong developing setup. This phase inserts a cheap Haiku pre-screener that evaluates all watchlist symbols together and returns a quality-ranked shortlist before the strategic agent assembles its context. The pre-screener does not reduce opportunity count — it replaces arbitrary insertion-order selection with signal-strength-ranked selection.

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

New prompt version: `v3.5.0`. Create `ozymandias/config/prompts/v3.5.0/` by copying all files from the current prompt version directory and adding `compress.txt`.

Update `config.json`: `claude.prompt_version` → `"v3.5.0"`.

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
compressor_max_symbols_out: int = 12   # defaults to equal tier1_max_symbols
compressor_max_tokens: int = 512
```

In `ozymandias/config/config.json`, add to the `claude` section:
```json
"compressor_enabled": true,
"compressor_model": "claude-haiku-4-5-20251001",
"compressor_max_symbols_out": 12,
"compressor_max_tokens": 512
```

`compressor_max_symbols_out` defaults to match `tier1_max_symbols` (12). Raising it adds symbols to the strategic agent's context; lowering it reduces opportunity count (not recommended without a clear reason).

## 4. Integration into `run_reasoning_cycle()`

In `claude_reasoning.py`, `run_reasoning_cycle()` is already `async` — the compressor call fits naturally here.

### Helper: `_build_all_candidates()`

Add a private method to `ClaudeReasoning`:

```python
def _build_all_candidates(
    self,
    watchlist: dict[str, WatchlistEntry],
    indicators: dict[str, dict],
) -> list[dict]:
```

Iterates all entries in `watchlist` (tier1 and tier2). For each symbol:
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

Before the `assemble_reasoning_context()` call, add:

```python
all_candidates = self._build_all_candidates(watchlist, self._latest_indicators)
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

### Modify `assemble_reasoning_context()`

`assemble_reasoning_context()` is currently synchronous — it stays synchronous. Add a new optional parameter:

```python
def assemble_reasoning_context(
    self,
    watchlist: dict[str, WatchlistEntry],
    ...
    selected_symbols: list[str] | None = None,
) -> str:
```

Existing behavior: when building the tier1 watchlist section, it takes symbols where `entry.priority_tier == 1`, up to `tier1_max_symbols`, in insertion order.

New behavior with `selected_symbols` provided:
- Use `selected_symbols` as the ordered list of symbols for the watchlist section, regardless of `priority_tier`
- Apply the same per-symbol data assembly (indicators, positions, etc.) as before — only the selection and ordering changes
- Still cap at `len(selected_symbols)` (already pre-capped by `compressor_max_symbols_out`)
- If a symbol in `selected_symbols` is not found in `watchlist` or `indicators`, skip it with a DEBUG log

When `selected_symbols is None`: existing behavior unchanged.

## 5. Backward Compatibility

- `compressor_enabled=False`: `selected_symbols` remains `None` throughout; `assemble_reasoning_context()` uses existing tier1 insertion-order logic. Zero behavioral change.
- Watchlist count ≤ `tier1_max_symbols`: compressor gate condition is False; no call made regardless of `compressor_enabled`.
- Haiku call failure: fallback returns deterministic sorted list; main reasoning cycle proceeds normally.
- Prompt version gate: `compress.txt` is only loaded when the compressor fires. If the prompt version directory lacks `compress.txt`, the compressor must raise a clear `FileNotFoundError` (caught by the broad `try/except`, triggers fallback).

## 6. Tests to Write

Create `tests/test_context_compressor.py`:

- **Normal path**: mock Haiku returning valid JSON with `actionable` array → `CompressorResult.symbols` matches expected order, `from_fallback=False`
- **Respects `max_out` cap**: Haiku returns 15 symbols but `max_out=12` → result has exactly 12 symbols
- **API failure → fallback**: Haiku raises exception → `from_fallback=True`, symbols sorted by `composite_score` desc
- **Malformed JSON → fallback**: Haiku returns unparseable string → `from_fallback=True`, deterministic sort
- **Empty candidates**: `candidates=[]`, `max_out=12` → returns `CompressorResult(symbols=[], ..., from_fallback=False or True)` without crashing
- **Fallback sort order**: candidates with scores `[0.3, 0.9, 0.6]` → fallback returns symbols in order `[0.9, 0.6, 0.3]`
- **Fallback respects `max_out`**: 20 candidates, `max_out=5` → fallback returns 5 symbols

Create `tests/test_context_compression_integration.py`:

- **Compressor fires when over threshold**: watchlist has 15 symbols, `tier1_max_symbols=12`, `compressor_enabled=True` → `compress()` is called, `assemble_reasoning_context` receives `selected_symbols`
- **Compressor skips when under threshold**: watchlist has 10 symbols, `tier1_max_symbols=12` → `compress()` not called, `selected_symbols=None`
- **Compressor skips when disabled**: `compressor_enabled=False`, 20-symbol watchlist → `compress()` not called
- **`assemble_reasoning_context` uses `selected_symbols`**: pass `selected_symbols=["NVDA", "TSLA"]` → context string includes those symbols in that order, excludes others
- **`assemble_reasoning_context` unchanged when `selected_symbols=None`**: existing tier1-only insertion-order behavior
- **Symbol not in watchlist skipped**: `selected_symbols` contains a symbol absent from watchlist → skipped silently, remaining symbols assembled normally

## Done When

- All existing tests pass; all new tests in `test_context_compressor.py` and `test_context_compression_integration.py` pass
- `ozymandias/intelligence/context_compressor.py` exists with `ContextCompressor` class and `CompressorResult` dataclass
- `config/prompts/v3.5.0/` directory exists with `compress.txt` and all files copied from prior version
- `config.json` updated: `claude.prompt_version = "v3.5.0"` and all four compressor keys present
- `ClaudeConfig` has all four new fields with correct defaults
- `run_reasoning_cycle()` calls compressor before `assemble_reasoning_context()` when conditions are met
- `assemble_reasoning_context()` accepts `selected_symbols` param and routes correctly
- `compressor_enabled=False` passes integration test confirming existing behavior unchanged
- DRIFT_LOG.md has a Phase 17 entry covering: new `ContextCompressor` module, new `ClaudeConfig` fields, `assemble_reasoning_context` signature change, prompt version bump to v3.5.0
