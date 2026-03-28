# Phase 21: Durability and Regime Response

Read the Phase 20 section of DRIFT_LOG.md before starting. This phase assumes Phases 19 and 20
are complete.

---

## Motivation

With Sonnet emitting `regime_assessment` and `sector_regimes` (Phase 19), the bot can now
act on regime changes rather than just detecting them. Phase 21 closes the remaining gaps:

1. **Watchlist rebuilds on regime change** — when the regime flips, evict candidates that
   conflict with the new regime and add aligned replacements
2. **Regime-aware universe scanner** — surface sector-appropriate candidates from Yahoo screeners
   (e.g., `day_losers` for correcting sectors)
3. **Session suppression hygiene** — clear direction-dependent suppression when regime flips,
   unblocking symbols that may be valid under the new direction
4. **Pruner eviction priority** — stop evicting intentional oversold/swing entries simply because
   their intraday composite score is low

These are all mechanical responses to regime data already available from Phase 19. No new Claude
calls are added.

---

## 1. Regime-Reset Watchlist Build

### Detection

In `_slow_loop_cycle`, after loading the reasoning cache, compare `result.regime_assessment` to
the previously stored regime. Track on orchestrator:

```python
self._last_regime: str | None = None           # from previous Sonnet cycle
self._last_sector_regimes: dict | None = None  # from previous Sonnet cycle (already used in Ph20)
```

A **regime change** is detected when any of the following is true:
- `result.regime_assessment["regime"]` differs from `_last_regime`
- Any sector in `result.sector_regimes` has a different `regime` value from `_last_sector_regimes`

Update `_last_regime` and `_last_sector_regimes` at the end of each Sonnet cycle.

### Trigger

When a regime change is detected, call `_trigger_regime_reset_build(changed_sectors, broad_panic)`:
- `changed_sectors`: list of sector ETF strings whose regime changed
- `broad_panic`: True if new `regime_assessment.regime == "risk-off panic"` regardless of sectors

This runs a watchlist build with full-rebuild semantics (no `watchlist_build_target` cap).

### Conflict eviction

Before calling `run_watchlist_build`, scan the current watchlist and call
`_evict_regime_conflicts(changed_sectors, broad_panic, sector_regimes)`:

```python
def _evict_regime_conflicts(
    self,
    changed_sectors: list[str],
    broad_panic: bool,
    sector_regimes: dict,
) -> list[str]:  # returns list of evicted symbols (for logging)
```

**Eviction rules:**

1. **Correcting/downtrend sector + long candidate:**
   - Symbol's sector ETF (via `_SECTOR_MAP`) is in `changed_sectors`
   - Symbol's `sector_regimes[etf]["regime"]` is `"correcting"` or `"downtrend"`
   - Symbol's watchlist `expected_direction == "long"`
   - AND the entry does NOT have a `catalyst_driven` flag set (idiosyncratic override)
   - → Evict

2. **Breaking-out sector + short candidate:**
   - Sector regime is `"breaking_out"` or `"uptrend"`
   - `expected_direction == "short"`
   - → Evict

3. **Broad risk-off panic:**
   - All swing long entries without `catalyst_driven` flag
   - Direction: `expected_direction == "long"` AND `strategy == "swing"`
   - → Evict regardless of sector

Eviction is performed by removing entries from `WatchlistState` via `StateManager` before the
build call. Log each evicted symbol at INFO: `"Regime conflict eviction: {symbol} ({reason})"`.

**`catalyst_driven` flag:** Add `catalyst_driven: bool = False` to `WatchlistEntry` in
`state_manager.py`. Claude can set this in the watchlist build output to mark idiosyncratic setups
that should survive regime-based eviction. Add to `watchlist.txt` prompt: include a
`catalyst_driven` field in the add/update symbol JSON schema.

### `run_watchlist_build` regime-reset path

Add `full_rebuild: bool = False` parameter to `run_watchlist_build`. When `True`:
- Ignores `watchlist_build_target` cap (passes `target_count=20`)
- Notes to Claude (in prompt context) that a regime change occurred and explains what was evicted

Update `watchlist.txt` (v3.10.0) with a new section rendered when `full_rebuild=True`:

```
REGIME RESET BUILD:
A market regime change was detected. The following symbols were evicted from the watchlist due
to direction conflicts with the new regime:
{evicted_symbols_json}

Build a fresh set of candidates aligned with the new regime. Target count: {target_count} additions.
```

---

## 2. Session Suppression Clearing on Regime Reset

`_filter_suppressed` stores symbols with their suppression reason. On a regime reset build,
direction-dependent suppression entries should be cleared for affected sectors so symbols are
re-evaluated under the new directional thesis.

Add helper:

```python
def _clear_directional_suppression(self, affected_sectors: list[str]) -> None:
```

**Logic:**
1. Build set of symbols in `affected_sectors` (symbols whose `_SECTOR_MAP` entry is in the
   affected sector list).
2. For each symbol in `_filter_suppressed` that is in this set:
   - If suppression reason matches a direction-dependent pattern → remove from `_filter_suppressed`
   - If suppression reason is direction-neutral → leave intact

**Direction-dependent reasons** (substring match):
- `"composite_score"` (entry scored too low — direction-sensitive metric)
- `"rvol"` (RVOL below threshold — firing when thesis was wrong direction, not a data problem)
- `"conviction"` or `"conviction_cap"` (conviction too low for the old thesis)
- `"entry_defer_exhausted"` (deferred on a thesis that no longer applies)

**Direction-neutral reasons** (leave intact):
- `"fetch_failure"` (data problem, not direction)
- `"no_entry_blacklist"` (explicit block, not direction)
- `"earnings_imminent"` (risk management, not direction)

Log at INFO: `"Cleared {n} directional suppressions for {affected_sectors} after regime reset"`

Call `_clear_directional_suppression(changed_sectors)` inside `_trigger_regime_reset_build`
before the watchlist build fires.

---

## 3. Regime-Aware Universe Scanner

`UniverseScanner` currently always uses `most_actives` + `day_gainers`. Phase 21 makes screener
source selection sector-granular based on `sector_regimes` from Sonnet's cache.

### Interface change

```python
async def get_top_candidates(
    n: int,
    exclude: set[str],
    blacklist: set[str],
    sector_regimes: dict | None = None,   # new optional param
) -> list[dict]:
```

### Per-sector screener selection

After fetching the universe from `UniverseFetcher.get_universe()`, apply sector-based filtering:

```python
# Determine if day_losers screener should be added
correcting_sectors = {
    etf for etf, data in (sector_regimes or {}).items()
    if data.get("regime") in ("correcting", "downtrend")
}
```

If `correcting_sectors` is non-empty: fetch `day_losers` screener in addition to the existing
`most_actives` + `day_gainers` sources. Add the returned symbols to the universe (same merge
logic as other sources).

For candidates from correcting sectors (resolved via `_SECTOR_MAP`): when computing the
candidate dict, add `"sector_bias": "short"` field. This signals to Claude and the compressor
that these candidates were surfaced as short setups.

**Broad panic path:** When `regime_assessment.regime == "risk-off panic"`, raise
`min_price_move_pct_for_candidate` threshold by 50% (i.e., multiply config value by 1.5) for the
duration of this scan call. This filters out minor moves and surfaces only significant dislocations.
Do not modify config — apply as a local override within `get_top_candidates`.

**Breaking-out sector path:** Existing `day_gainers` + `most_actives` behavior. No change.

**Neutral sector path:** Existing behavior. No change.

### `_SECTOR_MAP` access

The scanner does not own `_SECTOR_MAP`. Pass sector mapping as a parameter:

```python
async def get_top_candidates(
    n: int,
    exclude: set[str],
    blacklist: set[str],
    sector_regimes: dict | None = None,
    sector_map: dict[str, str] | None = None,  # symbol → ETF
) -> list[dict]:
```

The orchestrator passes `self._SECTOR_MAP`. When `sector_map=None`, sector-aware behavior is
skipped (existing behavior preserved).

### `day_losers` screener endpoint

```
https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved
  ?formatted=true&scrIds=day_losers&count=25
```

Same fetch pattern as `most_actives` — `asyncio.to_thread` GET request, returns `[]` on failure.

### Orchestrator call site update

```python
self._last_universe_scan = await self._universe_scanner.get_top_candidates(
    n=self._config.universe_scanner.max_candidates,
    exclude=existing,
    blacklist=blacklist,
    sector_regimes=self._last_sector_regimes,   # from Phase 19/20 cache
    sector_map=self._SECTOR_MAP,
)
```

---

## 4. Multi-Tier Watchlist Pruner Eviction

**Problem:** The pruner evicts by lowest intraday composite score when `watchlist_max_entries` is
hit. Swing entries added by Claude for oversold mean-reversion theses have deliberately low intraday
RSI and composite scores — the pruner systematically evicts exactly what Claude added intentionally.

**Fix:** Multi-tier eviction priority in `WatchlistManager.prune_to_max`:

```python
def prune_to_max(
    self,
    watchlist: WatchlistState,
    max_entries: int,
    sector_regimes: dict | None = None,
) -> WatchlistState:
```

When the watchlist exceeds `max_entries`, remove entries in this priority order:

1. **Tier-2 entries first** — tier-2 is exploratory; tier-1 has established conviction.
   Evict lowest-composite-score tier-2 entries first until count ≤ `max_entries` or no tier-2 remains.

2. **Regime-conflicting tier-1 entries** — when `sector_regimes` provided:
   - Tier-1 entries whose `expected_direction` conflicts with their sector's regime bias
   - (Same conflict definition as Section 1 eviction rules)
   - Sort by conflict severity: `"downtrend"` sector before `"correcting"` sector
   - Evict lowest composite among conflicting entries first

3. **Remaining tier-1 by composite score** — existing behavior, applied only after 1 and 2.

Pass `sector_regimes=self._last_sector_regimes` from orchestrator at the pruner call site.

Log each pruner eviction at DEBUG: `"Pruner evicted {symbol} (tier={tier}, reason={reason})"`

---

## 5. Persist `regime_assessment` Across Restarts

Add `regime_assessment: dict | None` to `BotState` (or a new lightweight state structure).

In `StateManager`:
- `save_regime_assessment(assessment: dict | None) -> None`
- `load_regime_assessment() -> dict | None`

Stored in existing `state/bot_state.json` (extend schema, do not create new file). Use atomic
write (write temp + rename) per existing state file conventions.

Orchestrator loads on startup:
```python
self._last_regime = state.regime_assessment.get("regime") if state.regime_assessment else None
self._last_sector_regimes = state.regime_assessment.get("sector_regimes") if state.regime_assessment else None
```

Saved after each Sonnet cycle that returns a `regime_assessment`:
```python
await self._state_manager.save_regime_assessment({
    "regime": result.regime_assessment,
    "sector_regimes": result.sector_regimes,
    "saved_at": datetime.now(timezone.utc).isoformat(),
})
```

This allows regime-reset logic to fire correctly on restart if the session resumes mid-panic.

---

## 6. Files Changed

| File | Change |
|------|--------|
| `ozymandias/core/orchestrator.py` | Regime change detection; `_trigger_regime_reset_build()`; `_evict_regime_conflicts()`; `_clear_directional_suppression()`; `_last_regime`/`_last_sector_regimes` tracking; pass `sector_regimes` to pruner and scanner; load/save regime on startup/cycle |
| `ozymandias/intelligence/universe_scanner.py` | `get_top_candidates` accepts `sector_regimes` + `sector_map`; `day_losers` source for correcting sectors; `sector_bias` field in candidate dict; broad panic threshold multiplier |
| `ozymandias/intelligence/watchlist_manager.py` | `prune_to_max` multi-tier eviction order; accepts `sector_regimes` |
| `ozymandias/intelligence/claude_reasoning.py` | `run_watchlist_build` accepts `full_rebuild: bool`; passes evicted symbols context to prompt when True |
| `ozymandias/core/state_manager.py` | `save_regime_assessment` / `load_regime_assessment`; extend `BotState` schema |
| `ozymandias/data/adapters/universe_fetcher.py` | `day_losers` screener added alongside existing sources |
| `ozymandias/core/config.py` | No new fields required; existing `watchlist_build_target` used |
| `ozymandias/config/prompts/v3.10.0/watchlist.txt` | `catalyst_driven` field in symbol schema; `REGIME RESET BUILD` section rendered when `full_rebuild=True` |
| `ozymandias/core/state_manager.py` | `catalyst_driven: bool = False` added to `WatchlistEntry` |

---

## 7. Tests to Write

Add to `tests/test_watchlist_manager.py`:

- `prune_to_max`: tier-2 evicted before tier-1 when over limit
- `prune_to_max`: regime-conflicting tier-1 evicted before neutral tier-1
- `prune_to_max`: `sector_regimes=None` → existing composite-score sort (no regression)
- `prune_to_max`: count already at or below limit → no eviction, state unchanged

Add to `tests/test_universe_scanner.py`:

- `get_top_candidates` with `sector_regimes={"XLK": {"regime": "correcting"}}` → fetches `day_losers`
- `get_top_candidates` with no correcting sectors → does not fetch `day_losers`
- `sector_bias="short"` present on candidates from correcting sector
- Broad panic path: `min_price_move_pct_for_candidate` threshold raised 50%

Add to `tests/test_orchestrator_regime.py` (new file):

- Regime change detection: different `regime` value → `_trigger_regime_reset_build` called
- Sector regime change: one sector flips → only that sector's conflicts evicted
- No regime change: same regime as previous cycle → no eviction fired
- `_evict_regime_conflicts`: long entry in correcting sector evicted
- `_evict_regime_conflicts`: long entry with `catalyst_driven=True` NOT evicted
- `_evict_regime_conflicts`: short entry in breaking-out sector evicted
- `_evict_regime_conflicts`: broad panic evicts all swing longs without catalyst flag
- `_clear_directional_suppression`: composite_score reason cleared for affected sector symbol
- `_clear_directional_suppression`: fetch_failure reason preserved (direction-neutral)
- `_clear_directional_suppression`: symbol in non-affected sector not cleared

Add to `tests/test_state_manager.py`:

- `save_regime_assessment` persists to `bot_state.json`; `load_regime_assessment` returns it
- `load_regime_assessment` returns `None` when field absent from state (backward compat)

---

## Done When

- Regime change detection in orchestrator fires `_trigger_regime_reset_build` when `regime` or any
  `sector_regimes` entry changes; unit tests pass
- `_evict_regime_conflicts` correctly evicts per rules; `catalyst_driven` entries preserved; tests pass
- `_clear_directional_suppression` clears direction-sensitive reasons; preserves neutral ones; tests pass
- `UniverseScanner.get_top_candidates` accepts `sector_regimes`; fetches `day_losers` for correcting
  sectors; `sector_bias` field present; tests pass
- `prune_to_max` multi-tier eviction: tier-2 first, then conflicts, then score; tests pass
- `regime_assessment` persisted to `bot_state.json`; loaded on restart; backward compat test passes
- `WatchlistEntry.catalyst_driven` field added; existing state loads without error
- `watchlist.txt` has `catalyst_driven` field in symbol schema and `REGIME RESET BUILD` section
- All existing tests pass
- DRIFT_LOG.md has a Phase 21 entry covering: regime-reset watchlist build trigger, conflict
  eviction rules, `catalyst_driven` flag, `_clear_directional_suppression` reason categories,
  universe scanner sector-aware screener selection, pruner multi-tier eviction, `regime_assessment`
  persistence in `bot_state.json`
