# Phase 17: Trigger Responsiveness & Data Freshness

Read the Post-Phase-16 section of DRIFT_LOG.md and CLAUDE.md before starting. This phase
assumes Phases 12, 15, and 16 are complete. It does not depend on Phases 18 or 19.

The slow loop's trigger system is position-centric and blind to macro regime changes. The
reasoning cache reuse window allows Claude's strategic view to persist for up to 60 minutes
regardless of market conditions. The medium loop fetches data serially, making actual indicator
refresh slower than the configured interval implies. The slow loop can fire before fresh
indicators exist, wasting a Claude call on data Claude already saw. Together these create a
system that responds to individual stock events well but fails to adapt to fast-moving broad
market breakdowns — exactly the condition where Claude's qualitative reasoning adds the most
value.

This phase has four independent fixes, implementable in the order listed.

---

## Fix 1 — Parallel Medium Loop Fetch

**Location:** `core/orchestrator.py` — `_medium_loop_cycle()`

Replace the serial `for symbol in scan_symbols` loop with `asyncio.gather` bounded by a
semaphore. This is the same pattern specified in Phase 18's `UniverseScanner` and should be
extracted here first since it benefits the existing medium loop regardless of Phase 18.

### Implementation

```python
scan_semaphore = asyncio.Semaphore(self._config.scheduler.medium_loop_scan_concurrency)

async def _fetch_and_analyse(symbol: str) -> tuple[str, dict, object] | None:
    async with scan_semaphore:
        try:
            df = await self._data_adapter.fetch_bars(symbol, interval="5m", period="5d")
            if df is None or df.empty:
                log.warning("Medium loop: no bars returned for %s", symbol)
                return None
            summary = await asyncio.to_thread(generate_signal_summary, symbol, df)
            return symbol, summary, df
        except Exception as exc:
            log.warning("Medium loop: TA failed for %s: %s", symbol, exc)
            return None

results = await asyncio.gather(
    *[_fetch_and_analyse(s) for s in scan_symbols],
    return_exceptions=True,
)
for result in results:
    if isinstance(result, BaseException) or result is None:
        continue
    symbol, summary, df = result
    indicators[symbol] = summary
    bars[symbol] = df
```

Two key details:

1. `generate_signal_summary` is currently synchronous and CPU-bound. Wrap it in
   `asyncio.to_thread` inside `_fetch_and_analyse` so it does not block the event loop when
   multiple fetches complete simultaneously. Without this, 35 DataFrames arriving at once
   would trigger 35 sequential `generate_signal_summary` calls on the event loop — up to
   ~1.75 seconds of blocking, preventing fast-loop fill polls.

2. Use `return_exceptions=True` on `asyncio.gather` so a single yfinance failure does not
   cancel all other fetches. Filter `BaseException` instances from results before processing.

After all fetches complete, update the shared state and add a timestamp:

```python
self._latest_indicators = ...   # (existing assignment — unchanged)
self._all_indicators = {
    **self._latest_indicators,
    **getattr(self, "_market_context_indicators", {}),
}
self._last_medium_loop_completed_utc = datetime.now(timezone.utc)
```

`self._all_indicators` is the merged view of watchlist/position indicators plus
macro context indicators (SPY/QQQ/IWM and sector ETFs). It is set here once per
medium loop cycle so downstream code (Phase 19's compressor, trigger evaluation)
can read a single unified dict without re-merging. Set `_last_medium_loop_completed_utc`
last — it is the gate signal for Fix 3, so it must not advance until all indicator
state is final.

Initialize `self._all_indicators: dict[str, dict] = {}` in `Orchestrator.__init__`
alongside the other indicator attributes.

### New config key

In `SchedulerConfig` (`core/config.py`) and `config.json`:
```json
"medium_loop_scan_concurrency": 10
```

With 10 concurrent fetches across 35 symbols at ~1s each, scan time drops from 35–70s to
~4–7s. Effective indicator refresh rate improves from ~190s to ~130s.

---

## Fix 2 — Macro and Sector Triggers (Bidirectional)

**Location:** `core/orchestrator.py` — `_check_triggers()`, `_update_trigger_prices()`,
`SlowLoopTriggerState`, `_run_claude_cycle()`

### New trigger types

**`market_move:<symbol>`** — fires when SPY, QQQ, or IWM moves beyond a configurable
threshold in either direction since the last Claude call. A broad rally is as strategically
significant as a breakdown: it signals a regime shift, may put existing short theses at risk,
and surfaces long opportunities that weren't valid before. Both directions deserve Claude's
attention.

**`sector_move:<etf>`** — fires when any sector ETF in `_CONTEXT_SYMBOLS` moves beyond a
configurable threshold in either direction since the last Claude call. When an open position
belongs to that sector, the threshold is tightened via a configurable factor, making the bot
more sensitive to sector moves that directly affect held positions.

**`market_rsi_extreme`** — fires when SPY RSI crosses below a panic threshold or above a
euphoria threshold. Both extremes are actionable: a panic extreme may signal reversal
opportunity or further breakdown risk to existing longs; a euphoria extreme may mean momentum
shorts are setting up or existing long stops should be tightened.

### Baseline: `last_claude_call_prices`

The existing `last_prices` dict is updated on every no-trigger cycle and serves the current
`price_move` trigger (individual symbols). The new macro/sector triggers need a separate
baseline — anchored to the last Claude call, not the last no-trigger check — so that a
sustained 1% SPY move always fires within one slow loop check, regardless of how many
intermediate no-trigger checks have reset `last_prices`.

Add to `SlowLoopTriggerState`:

```python
last_claude_call_prices: dict[str, float] = field(default_factory=dict)
rsi_extreme_fired_low: bool = False   # True after market_rsi_extreme fires for panic
rsi_extreme_fired_high: bool = False  # True after market_rsi_extreme fires for euphoria
```

**Initialization:** `last_claude_call_prices` must be populated when `indicators_ready`
fires (i.e., when `_latest_indicators` is first seeded). If left empty, every macro and
sector symbol would appear to have moved infinitely from a 0.0 baseline — firing all new
triggers simultaneously on the first check. Add initialization to the `indicators_ready`
block in `_check_triggers`:

```python
if not ts.indicators_seeded and getattr(self, "_latest_indicators", {}):
    triggers.append("indicators_ready")
    ts.indicators_seeded = True
    # Seed macro baseline so new triggers have a valid starting point.
    all_ind = {
        **getattr(self, "_latest_indicators", {}),
        **getattr(self, "_market_context_indicators", {}),
    }
    for sym, ind in all_ind.items():
        price = ind.get("price") or ind.get("signals", {}).get("price")
        if price is not None:
            ts.last_claude_call_prices[sym] = price
```

Update `last_claude_call_prices` at the end of `_run_claude_cycle` alongside
`last_claude_call_utc`:

```python
self._trigger_state.last_claude_call_utc = datetime.now(timezone.utc)
self._trigger_state.last_override_exit_count = self._override_exit_count
# Snapshot prices so macro/sector triggers measure from this call forward.
all_ind = {
    **getattr(self, "_latest_indicators", {}),
    **getattr(self, "_market_context_indicators", {}),
}
for sym, ind in all_ind.items():
    price = ind.get("price") or ind.get("signals", {}).get("price")
    if price is not None:
        self._trigger_state.last_claude_call_prices[sym] = price
```

### Sector exposure map

Add a module-level constant to `orchestrator.py`. This is the extension point for sector
membership — adding a new symbol requires one entry here and nowhere else:

```python
# Sector membership map: symbol → sector ETF tracked in _CONTEXT_SYMBOLS.
# Extension point: to register a new symbol's sector, add one entry here.
# Symbols absent from this map degrade gracefully: their sector ETF fires
# at the base threshold rather than the tightened exposure threshold.
# Note: COIN and similar crypto-adjacent names have no clean ETF mapping
# and are intentionally left unmapped.
_SECTOR_MAP: dict[str, str] = {
    # Energy (XLE)
    "XLE": "XLE", "XOM": "XLE", "CVX": "XLE", "HAL": "XLE",
    "SLB": "XLE", "COP": "XLE", "EOG": "XLE", "OXY": "XLE",
    # Financials (XLF)
    "XLF": "XLF", "JPM": "XLF", "BAC": "XLF", "WFC": "XLF",
    "GS": "XLF", "MS": "XLF", "KRE": "XLF", "MA": "XLF", "V": "XLF",
    # Technology (XLK)
    "XLK": "XLK", "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK",
    "AMD": "XLK", "NOW": "XLK", "CRWD": "XLK", "SNOW": "XLK",
    "PLTR": "XLK", "SMCI": "XLK", "SOXX": "XLK", "SMH": "XLK",
    "MU": "XLK",
    # Consumer Discretionary (XLY)
    "XLY": "XLY", "TSLA": "XLY", "AMZN": "XLY", "RIVN": "XLY",
    # Healthcare (XLV)
    "XLV": "XLV", "UNH": "XLV",
    # Industrials (XLI)
    "XLI": "XLI",
    # Communications (XLC)
    "XLC": "XLC", "NFLX": "XLC",
}

# Sector ETFs tracked for sector_move triggers — subset of _CONTEXT_SYMBOLS
# excluding the three broad-market indices (handled by market_move triggers).
# Extension point: to add a new sector, add its ETF to _CONTEXT_SYMBOLS and here.
_CONTEXT_SECTOR_ETFS: list[str] = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLC"]
```

### Trigger logic in `_check_triggers`

Add after the existing `price_move` block. Use `_market_context_indicators` for macro/sector
prices (that is where they are stored by the medium loop):

```python
macro_ctx = getattr(self, "_market_context_indicators", {})

# macro_move: SPY/QQQ/IWM moved beyond threshold since last Claude call.
macro_threshold = self._config.scheduler.macro_move_trigger_pct / 100
for sym in self._config.scheduler.macro_move_symbols:
    ind = macro_ctx.get(sym, {})
    current = ind.get("price") or ind.get("signals", {}).get("price")
    last = ts.last_claude_call_prices.get(sym)
    if current and last and abs(current - last) / last > macro_threshold:
        triggers.append(f"market_move:{sym}")

# sector_move: sector ETFs moved beyond threshold since last Claude call.
# Tighten threshold for sectors where we have open exposure.
exposed_sectors = {
    _SECTOR_MAP[pos.symbol]
    for pos in portfolio.positions
    if pos.symbol in _SECTOR_MAP
}
sector_base = self._config.scheduler.sector_move_trigger_pct / 100
sector_factor = self._config.scheduler.sector_exposure_threshold_factor
for etf in _CONTEXT_SECTOR_ETFS:
    # Skip if this ETF is already a held position — price_move covers it.
    if etf in {pos.symbol for pos in portfolio.positions}:
        continue
    ind = macro_ctx.get(etf, {})
    current = ind.get("price") or ind.get("signals", {}).get("price")
    last = ts.last_claude_call_prices.get(etf)
    if not (current and last):
        continue
    threshold = sector_base * sector_factor if etf in exposed_sectors else sector_base
    if abs(current - last) / last > threshold:
        triggers.append(f"sector_move:{etf}")

# market_rsi_extreme: SPY RSI crosses into panic or euphoria territory.
spy_signals = macro_ctx.get("SPY", {}).get("signals") or macro_ctx.get("SPY", {})
spy_rsi = spy_signals.get("rsi_14")
if spy_rsi is not None:
    panic_thresh = self._config.scheduler.macro_rsi_panic_threshold
    euphoria_thresh = self._config.scheduler.macro_rsi_euphoria_threshold
    rearm_band = self._config.scheduler.macro_rsi_rearm_band
    if spy_rsi < panic_thresh and not ts.rsi_extreme_fired_low:
        triggers.append("market_rsi_extreme")
        ts.rsi_extreme_fired_low = True
    elif spy_rsi > panic_thresh + rearm_band:
        ts.rsi_extreme_fired_low = False   # rearm for next dip
    if spy_rsi > euphoria_thresh and not ts.rsi_extreme_fired_high:
        triggers.append("market_rsi_extreme")
        ts.rsi_extreme_fired_high = True
    elif spy_rsi < euphoria_thresh - rearm_band:
        ts.rsi_extreme_fired_high = False  # rearm for next spike
```

### New config keys

In `SchedulerConfig` (`core/config.py`) and `config.json`:

```json
"macro_move_trigger_pct": 1.0,
"macro_move_symbols": ["SPY", "QQQ", "IWM"],
"sector_move_trigger_pct": 1.5,
"sector_exposure_threshold_factor": 0.7,
"macro_rsi_panic_threshold": 25,
"macro_rsi_euphoria_threshold": 72,
"macro_rsi_rearm_band": 5
```

`sector_exposure_threshold_factor` is a multiplier applied to `sector_move_trigger_pct` when
we have open exposure to that sector. Values < 1.0 tighten the threshold (more sensitive);
values > 1.0 loosen it. Default 0.7 → exposed sectors trigger at 1.05% instead of 1.5%.
Calibration basis: XLE moves >1.0% on ~3 of 5 days; at 1.05% exposed threshold, this fires
roughly 2–3 times per week when holding energy positions — meaningful signal, not noise.

---

## Fix 3 — Medium-Loop-Gated Slow Loop

**Location:** `core/orchestrator.py` — `_slow_loop_cycle()`

The slow loop can fire before the medium loop has refreshed `_latest_indicators` since the
last Claude call, giving Claude the same data it already reasoned about. The fix: gate the
Claude call on the medium loop having completed at least once since the last Claude call.

### Implementation

`_last_medium_loop_completed_utc` is set by Fix 1. Add a guard at the top of
`_slow_loop_cycle`, after the existing `claude_call_in_flight` and backoff guards:

```python
# Gate: only call Claude if the medium loop has refreshed indicators since
# the last call. This guarantees Claude always sees data newer than its
# prior reasoning cycle. Triggers remain queued and re-evaluate on the next
# slow-loop check (slow_loop_check_sec seconds later).
last_medium = getattr(self, "_last_medium_loop_completed_utc", None)
last_claude = self._trigger_state.last_claude_call_utc
if last_medium is None:
    log.debug("Slow loop: no medium loop completed yet — waiting")
    return
if last_claude is not None and last_medium <= last_claude:
    log.debug(
        "Slow loop: medium loop has not refreshed since last Claude call "
        "(last_medium=%s, last_claude=%s) — waiting",
        last_medium.isoformat(), last_claude.isoformat(),
    )
    return
```

This is self-calibrating: if the medium loop runs every 120s and the slow loop checks every
60s, at most one slow loop check is skipped before the gate opens. The maximum added latency
from trigger-fired to Claude-called is one medium loop cycle (~130s with Fix 1). Triggers
accumulate during the wait and are all evaluated together when the gate opens.

Note: `last_claude` being `None` (first call ever) bypasses the gate — the `indicators_ready`
trigger already ensures we only call Claude after the first medium loop has run.

---

## Fix 4 — Reasoning Cache Adaptive TTL

**Location:** `core/reasoning_cache.py`, `core/orchestrator.py` — medium loop

The reasoning cache reuse window is a fixed 60 minutes. During an active market breakdown or
rally, Claude's 60-minute-old strategic view can be badly outdated. The fix: compute the
allowed cache age from current SPY RSI before deciding whether to reuse the cache.

### Interface change to `ReasoningCache.load_latest_if_fresh`

Add an optional override parameter:

```python
def load_latest_if_fresh(
    self,
    max_age_min: int | None = None,
) -> Optional[dict]:
    """
    Return the most recent cached response if it is from today and
    less than max_age_min old. Falls back to REUSE_MAX_AGE_MINUTES if
    max_age_min is not provided.
    """
    effective_max = max_age_min if max_age_min is not None else REUSE_MAX_AGE_MINUTES
    ...
```

### Adaptive TTL computation in the medium loop

In `_medium_loop_cycle`, before the `self._reasoning_cache.load_latest_if_fresh()` call
(search for it — the line number shifts after Fix 1 restructures the loop), compute the override:

```python
def _compute_cache_max_age(self) -> int:
    """Return the appropriate reasoning cache max-age based on current SPY RSI."""
    cfg = self._config.claude
    spy = getattr(self, "_market_context_indicators", {}).get("SPY", {})
    rsi = (spy.get("signals") or spy).get("rsi_14")
    if rsi is None:
        return cfg.cache_max_age_default_min
    if rsi < cfg.cache_panic_rsi_low:
        return cfg.cache_max_age_panic_min
    if rsi < cfg.cache_stress_rsi_low:
        return cfg.cache_max_age_stressed_min
    if rsi > cfg.cache_euphoria_rsi_high:
        return cfg.cache_max_age_euphoria_min
    return cfg.cache_max_age_default_min
```

Call sites (search by context — line numbers shift after Fix 1 restructures the medium loop):
- **Medium loop** (`_medium_loop_cycle`, the `cached_raw = self._reasoning_cache.load_latest_if_fresh()` call): use `self._compute_cache_max_age()` — this is the hot path where adaptive TTL matters.
- **Startup reconciliation** (two calls to `load_latest_if_fresh()` in the startup path, before `_medium_loop_cycle` ever runs): pass no override, use default 60-minute TTL. Startup cache reuse should not be gated on RSI — no live SPY data exists yet and the cached response is being checked for continuity, not market-regime alignment.

### New config keys

In `ClaudeConfig` (`core/config.py`) and `config.json`:

```json
"cache_max_age_default_min": 60,
"cache_max_age_stressed_min": 20,
"cache_max_age_panic_min": 10,
"cache_max_age_euphoria_min": 15,
"cache_stress_rsi_low": 30,
"cache_panic_rsi_low": 25,
"cache_euphoria_rsi_high": 72
```

### Cost note

Cost/benefit analysis conducted prior to this phase: at ~10 Claude calls/day average
(including all triggers and adaptive TTL), annual API cost is ~$181 at Sonnet pricing. A
single prevented bad hold or captured opportunity pays for months of calls. Cost is not a
constraint — call frequency is bounded by data freshness (Fix 3) rather than by a cost floor.

---

## State changes summary

### `SlowLoopTriggerState` additions

```python
last_claude_call_prices: dict[str, float] = field(default_factory=dict)
rsi_extreme_fired_low: bool = False
rsi_extreme_fired_high: bool = False
```

### `Orchestrator` instance variable additions

```python
self._last_medium_loop_completed_utc: datetime | None = None
```

Initialized to `None` in `__init__`. Set at end of each `_medium_loop_cycle`.

---

## Tests to write

**`test_orchestrator.py` additions:**

Fix 1:
- Parallel fetch produces identical `_latest_indicators` content to serial equivalent
- Single symbol fetch failure does not prevent other symbols from being scanned
- `_last_medium_loop_completed_utc` is set after each medium loop cycle
- `generate_signal_summary` called via `asyncio.to_thread` (mock verifies it's not blocking)

Fix 2:
- `market_move:SPY` fires when SPY moves >1% from `last_claude_call_prices` baseline
- `market_move:SPY` fires on upswing as well as downswing (bidirectional)
- `market_move` does not fire when move is < threshold
- `sector_move:XLE` fires at 1.5% base threshold when no energy positions held
- `sector_move:XLE` fires at 1.05% (1.5% × 0.7) when energy position is held
- `sector_move` skips ETFs that are also open positions (no duplicate triggers)
- `market_rsi_extreme` fires when SPY RSI < 25
- `market_rsi_extreme` fires when SPY RSI > 72
- `market_rsi_extreme` does not re-fire while RSI remains below panic threshold
- `market_rsi_extreme` rearms after RSI recovers past threshold + rearm_band
- `last_claude_call_prices` initialized from indicators when `indicators_ready` fires
- `last_claude_call_prices` updated after each Claude call in `_run_claude_cycle`

Fix 3:
- Slow loop skips trigger evaluation when `_last_medium_loop_completed_utc` is None
- Slow loop skips when last medium loop predates last Claude call
- Slow loop fires when last medium loop postdates last Claude call
- First-ever Claude call (no `last_claude_call_utc`) bypasses the medium-loop gate
- Trigger conditions are re-evaluated on the next slow-loop check after the gate opens; since each trigger condition is stateful (price move since last call, elapsed time, RSI level), the same triggers that were blocked will re-fire naturally when the gate opens

Fix 4:
- `load_latest_if_fresh(max_age_min=10)` rejects a cache entry that is 15 minutes old
- `load_latest_if_fresh(max_age_min=10)` accepts a cache entry that is 8 minutes old
- `load_latest_if_fresh()` (no override) uses `REUSE_MAX_AGE_MINUTES` default
- `_compute_cache_max_age` returns `cache_max_age_panic_min` when SPY RSI < 25
- `_compute_cache_max_age` returns `cache_max_age_stressed_min` when SPY RSI < 30
- `_compute_cache_max_age` returns `cache_max_age_euphoria_min` when SPY RSI > 72
- `_compute_cache_max_age` returns default when SPY RSI is not available
- Startup call sites (lines 305, 567) continue using default TTL regardless of RSI

---

## Done when

- All existing 922 tests pass; all new tests pass
- `_last_medium_loop_completed_utc` is set after every medium loop cycle and accessible in
  orchestrator state
- In session logs, `sector_move:XLE` appears as a trigger name when XLE moves >1% with
  energy positions held
- In session logs, `market_move:SPY` appears when SPY moves >1% since the last Claude call
- In session logs, slow loop shows "waiting — medium loop has not refreshed" at least once
  per session before firing, confirming the gate is active
- During a simulated panic session (SPY RSI < 25), Claude is called within one medium loop
  cycle of the RSI crossing the threshold
- DRIFT_LOG.md has a Phase 17 entry covering: parallel medium loop fetch,
  `_SECTOR_MAP`/`_CONTEXT_SECTOR_ETFS` constants, new trigger types, `SlowLoopTriggerState`
  additions, `_last_medium_loop_completed_utc`, `_all_indicators` instance attribute,
  `load_latest_if_fresh` signature change, new config keys in `SchedulerConfig` and `ClaudeConfig`
