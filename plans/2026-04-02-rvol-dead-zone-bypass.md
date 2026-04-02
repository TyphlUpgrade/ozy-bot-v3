# Plan: RVOL-Conditional Dead Zone Bypass

**Date:** 2026-04-02  
**Status:** Implemented (same session)  
**Commits:** bc37b77 (Phase 23 session), ad94af1

---

## Context

The dead zone (11:30–2:30 ET) blocks new entries unconditionally. It was built as a proxy for
"low volume = bad entries." Phase 23 fixed the suppression side (dead zone rejections no longer
accumulate toward session suppression). The entry block itself remained a static time gate.

On volatile days — macro events, Fed, sector catalysts — midday can be the most active period of
the session. A static block means missing valid setups for 3 hours. The real signal was always SPY
RVOL, not the clock. If SPY is trading at 1.5x+ normal volume, the dead zone premise doesn't hold.

---

## Design Principle: No risk_manager changes

The risk_manager is stateless with respect to market data — it doesn't hold indicators. All three
dead zone call sites live in the orchestrator, which owns `_all_indicators`. The bypass logic stays
in the orchestrator entirely. `risk_manager.py` is not touched.

The bypass is expressed to `validate_entry()` by OR-ing into the existing `dead_zone_exempt`
parameter, which already means "skip the dead zone check." The risk_manager doesn't need to know
why it's being skipped.

---

## SPY RVOL Access Pattern

SPY is always a context symbol stored in `_market_context_indicators`, which holds the raw
`generate_signal_summary()` return — `volume_ratio` is nested under `"signals"`. Watchlist symbols
are flat (signals expanded by `_latest_indicators`). Must use the try-flat-then-nested fallback
pattern consistent with `_check_triggers` (line 3315–3319) and `_update_trigger_prices` (line 3689–3691):

```python
spy_ind = self._all_indicators.get("SPY", {})
spy_rvol = spy_ind.get("volume_ratio", spy_ind.get("signals", {}).get("volume_ratio", 0.0))
```

Fail-safe: if SPY absent or `volume_ratio == 0.0`, bypass returns `False` (dead zone stays active).

---

## Two-Tier Implementation (extended from original plan)

The original plan covered only global SPY bypass. After the plan was approved, a per-symbol tier
was added in the same session (no additional cost to latency or prompts).

- **Tier 1 (global):** SPY RVOL ≥ `dead_zone_rvol_bypass_threshold` (default 1.5) → dead zone
  lifted for all symbols in that medium loop cycle
- **Tier 2 (per-symbol):** individual symbol RVOL ≥ `dead_zone_symbol_rvol_bypass_threshold`
  (default 2.0) from `_latest_indicators` (flat dict) → dead zone lifted for that symbol only

Tier 2 requires `symbol` parameter on `_dead_zone_rvol_bypass(symbol=None)`.

---

## Config

`SchedulerConfig` fields added:
```python
dead_zone_rvol_bypass_enabled: bool = True
dead_zone_rvol_bypass_threshold: float = 1.5
dead_zone_symbol_rvol_bypass_threshold: float = 2.0
```

---

## Changes

| File | Change |
|------|--------|
| `ozymandias/core/config.py` | 3 fields added to `SchedulerConfig` |
| `ozymandias/config/config.json` | 3 fields under `scheduler` |
| `ozymandias/core/orchestrator.py` | `_dead_zone_rvol_bypass(symbol=None)`; per-cycle log; 3 call site updates; ranker suppression guard |
| `ozymandias/tests/test_orchestrator.py` | `TestDeadZoneRvolBypass` — 19 tests (global bypass, per-symbol bypass, disabled, missing data, nested signals format, suppression counting, validate_entry passthrough) |
| `DRIFT_LOG.md` | Dead zone suppression fix entry + RVOL bypass entry |

**Not modified:** `risk_manager.py`, `technical_analysis.py`, any prompts.

---

## Call Sites Updated

1. **Ranker rejection suppression loop** — `if self._risk_manager.in_dead_zone() and not self._dead_zone_rvol_bypass(symbol):`  
   When bypass is active, suppression counts normally — failing RVOL/RSI in an active market is a genuine failure.

2. **Entry defer count guard in `_medium_try_entry`** — `_in_dead_zone` now includes `and not self._dead_zone_rvol_bypass(symbol)`

3. **`validate_entry()` call** — `dead_zone_exempt=_dz_exempt or _dz_bypass` where `_dz_bypass = self._dead_zone_rvol_bypass(symbol)`

---

## Verification

- All 1339 tests pass (`PYTHONPATH=. .venv/bin/pytest -q`)
- `dead_zone_rvol_bypass_threshold: 0.01` → bypass fires every cycle; log shows "Dead zone RVOL bypass active"
- `dead_zone_rvol_bypass_threshold: 99.0` → dead zone identical to before; no bypass log
- `dead_zone_rvol_bypass_enabled: false` → no bypass regardless of RVOL
