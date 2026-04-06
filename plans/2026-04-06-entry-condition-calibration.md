# Entry Condition Calibration & Prompt Precision

**Date:** 2026-04-06  
**Motivated by:** ALGN rsi_max miscalibration (session 14:02Z), ALB entry against stale thesis-breaking condition (session 14:50Z)

---

## Problem Statement

Two distinct failure modes observed today:

**Failure Mode 1 — rsi_max/rsi_min level miscalibration (ALGN)**  
Claude set `rsi_max: 40` for a short with live RSI at 46. This creates a "wait for RSI to fall to 40" gate that can never be satisfied from the current level without significant directional movement. Gate burned 15 defer cycles before expiry. Root cause: Claude writes entry_conditions without verifying them against live ta_readiness values. The feedback loop (`last_block_reason` in `stage_detail`) now surfaces the failure string — but the prompt gives no instruction on how to diagnose or self-correct from it.

**Failure Mode 2 — entry against already-triggered thesis condition (ALB)**  
Claude recommended ALB short at 14:57. The thesis-breaking condition it wrote — "U.S.-led supply chain initiative benefiting ALB" — was already corroborated by a 30+ hour old headline in `watchlist_news` at the time of recommendation. The Haiku thesis checker caught it 2 minutes after fill. Root cause: Claude processes `new_opportunities` and `active_theses` as separate tasks in a single output pass, with no instruction to cross-check whether current news already satisfies a breaking condition it is about to write. Secondary signal missed: `long_score: 0.5325 > short_score: 0.3625` at entry — TA was scoring ALB better as a long.

---

## Design Decisions

### What was considered and rejected

**Option A: Hardcode `rsi_max_calibration_error` detector in `evaluate_entry_conditions`**  
Would catch the specific rsi_max-below-current-RSI pattern at first defer. Rejected as primary fix because it patches one symptom of a broader miscalibration class (the same attention span problem exists for rsi_min, rsi_slope thresholds, rsi_accel thresholds). Valid as a safety net backstop only.

**Option B: `_basis` fields (e.g., `rsi_max_basis: "current RSI 46"`) in entry_conditions output**  
Self-documenting pattern — the calibration contradiction becomes visible in Claude's own output. Rejected because `_basis` fields are voluntary and Claude drops optional fields under output pressure across 8-15 opportunities in one call. Would produce inconsistent coverage. Also adds output tokens per opportunity.

**Option C (chosen): Require ta_readiness echo in `reasoning` field + safety net detector**  
The `reasoning` field is already required. Requiring Claude to state current rsi, rsi_slope_5, and rsi_accel_3 from ta_readiness in the `reasoning` field before writing entry_conditions:
- Forces verification into an existing required field (can't be skipped without a visible gap)
- Surfaces the calibration in logs for every entry, auditable without extra fields
- Addresses the attention span root cause, not just one symptom
- The safety net code detector remains as backstop for residual failures

---

## Implementation Plan

### Change 1: Prompt — require ta_readiness echo in `reasoning` (primary fix, FM1)

In `reasoning.txt`, add to the `new_opportunities` instructions after the entry_conditions section:

> *"For each opportunity, `reasoning` must state the current rsi, rsi_slope_5, and rsi_accel_3 values from ta_readiness before describing entry_conditions rationale. Format: 'rsi=X slope=Y accel=Z — [thesis]'. This is required, not optional — it ensures conditions are calibrated against live values, not estimated."*

**Why reasoning field:** Already required, already logged, already visible in `Rejected opportunity:` log lines. No new fields, no token overhead beyond the values themselves.

### Change 2: Prompt — pre-entry thesis/news cross-check (primary fix, FM2)

In `reasoning.txt`, add to the position review / new_opportunities instructions:

> *"Before finalising any opportunity in `new_opportunities`, check `watchlist_news` for that symbol. If a headline already concretely corroborates a condition you are about to list in `active_theses.thesis_breaking_conditions`, that condition is currently triggered — reject the opportunity (or treat the position as already requiring exit) rather than entering and monitoring. A thesis-breaking condition that is already true is a rejection reason, not a future monitoring item."*

### Change 3: Prompt — long_score > short_score gate for short entries (secondary fix, FM2)

Add to FIELD INSTRUCTIONS for `action`:

> *"For short entries: if `ta_readiness` shows `long_score > short_score` by more than 0.10, note this in `reasoning` and require a named catalyst that explicitly overrides the TA directional disagreement. Do not silently enter a short when composite TA favours longs."*

### Change 4: Code — safety net detector in `evaluate_entry_conditions` (backstop)

Add a general "condition can never be satisfied from current value" check in `opportunity_ranker.py:evaluate_entry_conditions`. Detects:
- `rsi_max` set more than a configurable margin below current RSI for a short
- `rsi_min` set more than a configurable margin above current RSI for a long

Returns a distinct failure reason `rsi_max_calibration_error` / `rsi_min_calibration_error` so `stage_detail` carries a diagnostic rather than a generic "rsi_max exceeded" string. Claude receives this on the next cycle and the prompt (via Change 1) now gives it actionable guidance.

The margin should be a config value (e.g., `entry_condition_rsi_level_tolerance: 5.0`) not a hardcoded constant.

### Change 5: Prompt — sector regime daily grounding (bonus, FM3)

Sector regimes flipped intraday (XLV: correcting/short → uptrend/long within 45 min). Add to REGIME ASSESSMENT:

> *"`sector_regimes` must be grounded in daily-bar data. Do not flip a sector between correcting and uptrend based on intraday ETF moves within a session — that is noise. Only update `sector_regimes` when `daily_trend` or multi-day technical structure has materially changed. Within-session regime flips without a named catalyst are almost always wrong."*

---

## Prompt versioning

Changes 1, 2, 3, 5 modify `reasoning.txt`. This warrants a prompt version bump to `v3.10.2`. Create `config/prompts/v3.10.2/` by copying `v3.10.1/` and applying the changes. Update `config.json` `prompt_version` field.

Change 4 (`opportunity_ranker.py`) is code-only, no prompt version required.

---

## Testing

- Unit test for Change 4: `evaluate_entry_conditions` with rsi_max 5+ points below current RSI returns `rsi_max_calibration_error` and not the generic message
- Unit test: rsi_max within tolerance (e.g., rsi_max=44, current_rsi=46, tolerance=5) does NOT trigger calibration error — standard rsi_max check applies
- Verify Change 4 does not affect rsi_min/rsi_max for longs or shorts where the condition is correctly set

No tests required for prompt changes — observable in Claude output logs.

---

## Files touched

| File | Change |
|---|---|
| `ozymandias/config/prompts/v3.10.2/reasoning.txt` | New versioned prompt (copy v3.10.1 + Changes 1, 2, 3, 5) |
| `ozymandias/config/config.json` | `prompt_version` → `v3.10.2`, add `entry_condition_rsi_level_tolerance` |
| `ozymandias/core/config.py` | Add `entry_condition_rsi_level_tolerance: float = 5.0` to ClaudeConfig or ranker config |
| `ozymandias/intelligence/opportunity_ranker.py` | Change 4: calibration error detector in `evaluate_entry_conditions` |
| `ozymandias/tests/test_opportunity_ranker.py` | Tests for Change 4 |

---

## What this does not fix

- Claude call latency (84-103s) — infrastructure/model issue, not prompt or code
- The broader class of slope/accel threshold miscalibration (CONCERN-3 in NOTES.md) — requires observing whether the `last_block_reason` feedback loop (now live) causes self-correction before investing in a larger prompt restructure
