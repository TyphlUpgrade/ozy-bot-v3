# Phase 12: Claude-Directed Entry Conditions — Per-Trade TA Gating

Read the Phase 11 section of DRIFT_LOG.md and the `Post-MVP Architecture` section of CLAUDE.md before starting. This phase assumes Phase 11 is complete.

The `entry_signals` computation in the medium loop (Step 3, lines 1206–1268 of `core/orchestrator.py`) generates TA strategy signals each cycle but they are never consumed — the entry path uses only Claude's composite score. The root problem is not that TA gating is wrong; it's that the existing TA gates are *context-blind*, applying identical thresholds to every symbol regardless of its historical behavior, beta, or current catalyst. The fix is to let Claude specify, per opportunity, the exact TA conditions it wants confirmed before the medium loop enters. Claude knows that NVDA in a momentum regime runs RSI 55–75; the blind gate does not.

## 1. Extend `new_opportunities` JSON Schema

Add an optional `entry_conditions` field to each opportunity dict in the reasoning prompt template.

New prompt template version: `v3.4.0`. Create `config/prompts/v3.4.0/` by copying `v3.3.0/` and modifying `reasoning.txt`:

- Update the `new_opportunities` array schema to include:
  ```json
  "entry_conditions": {
    "require_above_vwap": true,
    "rsi_min": 50,
    "rsi_max": 72,
    "require_volume_ratio_min": 1.4,
    "require_macd_bullish": false
  }
  ```
- All fields within `entry_conditions` are optional — absent = no requirement on that dimension
- Add an instruction: "For each opportunity, optionally specify entry_conditions: the exact TA conditions you want confirmed at execution time. Calibrate per-ticker — NVDA in a momentum regime may warrant rsi_max: 72 where a slower name warrants rsi_max: 62. The medium loop will confirm these conditions are live before entering. If omitted, no condition check is applied."
- Update `config.json`: `claude.prompt_version` → `"v3.4.0"`

## 2. Condition Evaluator Function

New function in `intelligence/opportunity_ranker.py`:

```python
def evaluate_entry_conditions(conditions: dict, signals: dict) -> tuple[bool, str]:
```

- `conditions`: the `entry_conditions` dict from Claude's opportunity (may be empty or absent)
- `signals`: the flat signals sub-dict from `generate_signal_summary()["signals"]` for the symbol
- Returns `(True, "")` if all conditions satisfied
- Returns `(False, rejection_reason)` on first failing condition
- Empty or absent conditions dict → always `(True, "")`

Conditions checked (keys, their type, and evaluation logic):
- `require_above_vwap: bool` — `signals["vwap_position"] == "above"`
- `rsi_min: float` — `signals["rsi"] >= rsi_min`
- `rsi_max: float` — `signals["rsi"] <= rsi_max`
- `require_volume_ratio_min: float` — `signals["volume_ratio"] >= require_volume_ratio_min`
- `require_macd_bullish: bool` — if True: `signals["macd_signal"] in ("bullish", "bullish_cross")`

Gracefully handle missing signal values — if a required signal key is absent from the dict, treat the condition as unmet with reason "signal '{key}' unavailable".

## 3. `entry_conditions` Field in `ScoredOpportunity`

Add to `ScoredOpportunity` dataclass (`intelligence/opportunity_ranker.py`):
```python
entry_conditions: dict = field(default_factory=dict)
```

In `score_opportunity()`: populate from `opportunity.get("entry_conditions", {})`.

This carries Claude's conditions through the ranker to `_medium_try_entry` intact.

## 4. Wire Condition Evaluation into Entry Path

In `_medium_try_entry` (`core/orchestrator.py`), after the fill protection check and before the thesis challenge block:

```python
# Check Claude's per-trade entry conditions against current signals
entry_conds = getattr(top, "entry_conditions", {}) or {}
if entry_conds:
    current_sigs = self._latest_indicators.get(symbol, {})
    conds_met, conds_reason = evaluate_entry_conditions(entry_conds, current_sigs)
    if not conds_met:
        log.info(
            "Entry conditions not met for %s: %s — will retry next cycle",
            symbol, conds_reason,
        )
        return False
```

Import `evaluate_entry_conditions` from `intelligence/opportunity_ranker.py`.

Log at INFO (not WARNING) — this is normal expected behavior, not an error.

## 5. Backward Compatibility

- Opportunities without `entry_conditions` in Claude's output → empty dict → condition check passes → no behavioral change
- Existing reasoning cache files without the new field → `_result_from_raw_reasoning` already uses `.get()` with defaults, so no change needed in the parser
- Prompt version gate: if the reasoning cache was written by v3.3.0 and loaded by v3.4.0 code, there are simply no `entry_conditions` in the opportunities — backward compat holds

## 6. Tests to Write

Create `tests/test_entry_conditions.py`:

- **All conditions met**: full conditions dict, all signals pass → `(True, "")`
- **rsi_min not met**: rsi_min=55, current RSI=48 → `(False, reason mentioning "rsi")`
- **rsi_max exceeded**: rsi_max=65, current RSI=71 → `(False, ...)`
- **require_above_vwap fails**: condition=True, vwap_position="below" → `(False, ...)`
- **volume_ratio below min**: require_volume_ratio_min=1.5, volume_ratio=1.1 → `(False, ...)`
- **require_macd_bullish fails**: condition=True, macd_signal="bearish" → `(False, ...)`
- **Empty conditions dict**: `{}` → always `(True, "")`
- **Missing conditions key entirely**: `None` treated as empty → `(True, "")`
- **Signal key absent**: condition requires `rsi_min` but signals dict has no `rsi` key → `(False, "signal 'rsi' unavailable")`
- **ScoredOpportunity propagation**: `score_opportunity()` correctly populates `entry_conditions` field
- **ScoredOpportunity default**: opportunity dict without `entry_conditions` → empty dict in field
- **Medium loop gate**: entry blocked when conditions fail, returns False
- **Medium loop pass**: entry proceeds when conditions met
- **No entry_conditions on opportunity**: entry proceeds without condition check

Extend `tests/test_opportunity_ranker.py`:
- `entry_conditions` field present and correctly populated after `score_opportunity()`

## Done When

- All existing tests pass; all new tests in `test_entry_conditions.py` pass
- `config/prompts/v3.4.0/reasoning.txt` exists, contains `entry_conditions` in schema and instructions
- `config.json` updated: `claude.prompt_version = "v3.4.0"`
- Entry conditions block entries in integration test when conditions aren't met
- Opportunities without `entry_conditions` still execute normally (backward compat confirmed)
- DRIFT_LOG.md has a Phase 12 entry covering `entry_conditions` schema addition and evaluator
