# Slow Loop Over-triggering

**Date:** 2026-04-06  
**Motivated by:** Session 15:43Z — 3 Sonnet calls in 8 minutes; regime_condition fired 2m56s after previous cycle; NKE reviewed twice in 3 minutes by separate triggers.

---

## Problem Statement

Three distinct failure modes, all causing unnecessary slow loop triggers:

**FM1 — `valid_until_conditions` written with already-met values**  
`regime_condition` fired at 15:48:56, only 2m56s after the session_open cycle completed at 15:46:00. SPY was already in a daily downtrend at 15:46 when Claude wrote `valid_until_conditions: ["daily_trend == downtrend"]`. The condition was immediately true, so the next slow loop check (60s cadence) fired it. This is the same write-time verification failure as ALGN's rsi_max — Claude not checking current values before writing thresholds. The prompt explicitly prohibits it ("each condition must be a threshold that is NOT currently met") but Claude violates it under output pressure.

**FM2 — No cooldown on `regime_condition` trigger**  
Even with correct `valid_until_conditions`, the trigger has no minimum re-fire interval. If the signal is noisy or Claude miscalibrates again, it can fire on every check cycle. Unlike `approaching_close` (which has `approaching_close_fired`), there's no guard. The regime_condition trigger can chain: fires → cycle runs → new valid_until_conditions written → fires again immediately.

**FM3 — Thesis breach fires on a position just reviewed by full Sonnet cycle**  
NKE was reviewed at 15:50:44 (regime_condition cycle, Sonnet, held, thesis intact). At 15:52:10 — 86 seconds later — the Haiku thesis checker fired on NKE for "Zacks earnings preview reveals upside surprise catalyst." The orchestrator scheduled another Sonnet call. The Haiku checker has no awareness of recent Sonnet reviews. The news it fired on was already present at 15:50:44 — the full Sonnet review had just assessed it and said hold. The second call produced the same conclusion.

Secondary issue within FM3: the triggering headline was a speculative earnings *preview*, not an actual catalyst event. `thesis_breaking_conditions` that can be matched by speculative analyst previews are too broad.

---

## Design Decisions

### FM1: valid_until_conditions calibration

**Approach:** Same as entry_conditions fix in v3.10.2 — require Claude to state the current value of each signal before writing the condition. Add to the REGIME ASSESSMENT section in reasoning.txt:

> *"Before writing each `valid_until_conditions` entry, state the current value of that signal from market context (e.g., current spy_daily.daily_trend, current spy_daily.rsi_14d). A condition is only valid if it is NOT currently met. If you write 'daily_trend == downtrend' and the current daily_trend is already 'downtrend', that condition is immediately true and will fire a reasoning cycle within 60 seconds. Check current values before writing."*

No code change required — this is purely a prompt calibration issue.

### FM2: regime_condition cooldown

**Approach:** Add `last_regime_condition_utc` timestamp to `SlowLoopTriggerState`. After `regime_condition` fires, set the timestamp. In `_check_triggers`, suppress the trigger if less than `regime_condition_cooldown_min` minutes have elapsed since the last fire. Config-driven default: 20 minutes.

This is the same pattern as `approaching_close_fired` but time-based rather than boolean, since regime conditions can legitimately re-fire on the next session.

**Why 20 minutes:** The slow loop full cycle takes ~100s. After a regime_condition fires and a 100s Sonnet call runs, the new `valid_until_conditions` get written. If they're correct, the next fire should require a genuine regime change — which takes minutes to hours, not seconds. 20 minutes is conservative enough to prevent rapid-fire chaining while still allowing intraday regime reassessment if something material happens.

### FM3: "recently reviewed" suppression for thesis breach

**Approach:** Track `last_sonnet_review_utc` per position symbol in the orchestrator. After any Sonnet cycle that includes position reviews, stamp each reviewed symbol with the current UTC time. In the thesis breach scheduling path (before firing the Haiku check or before scheduling the Sonnet call), check: if the position was reviewed within `thesis_breach_review_cooldown_min` minutes (default: 15 minutes), suppress the breach trigger for that position.

The stamp goes in a dict on the orchestrator: `_last_position_review_utc: dict[str, datetime]`. Updated after every position review cycle (both full reasoning and position-review-only calls). Cleared on session transition.

**Why suppress at scheduling, not at Haiku check:** The Haiku check itself is cheap (1,195 tokens vs 15,000+ for Sonnet). The cost is the downstream Sonnet call the Haiku result triggers. Suppression belongs in the orchestrator's breach scheduling gate, not in the Haiku call itself.

### FM3 secondary: thesis_breaking_conditions specificity

**Approach:** Add to the active_theses instructions in reasoning.txt:

> *"thesis_breaking_conditions must describe concrete, verifiable events — not speculative analyst commentary. 'NKE reports earnings and beats EPS estimates' is valid. 'Earnings surprise catalyst emerges' is not — it can be matched by a preview article before earnings are reported. Conditions involving earnings must specify the actual reported result, not predictions or previews."*

---

## Implementation Plan

### Change 1: Prompt — valid_until_conditions calibration (FM1)
In `reasoning.txt` v3.10.2 → bump to `v3.10.3`. Add to REGIME ASSESSMENT section, immediately before the `valid_until_conditions` guidance.

### Change 2: Prompt — thesis_breaking_conditions specificity (FM3 secondary)
In `reasoning.txt` v3.10.3`. Add after `active_theses` description in REGIME ASSESSMENT.

### Change 3: Code — regime_condition cooldown (FM2)
- Add `last_regime_condition_utc: Optional[str] = None` to `SlowLoopTriggerState`
- Add `regime_condition_cooldown_min: int = 20` to `SchedulerConfig` in `config.py` + `config.json`
- In `_check_triggers`: before appending `regime_condition`, check elapsed time since `last_regime_condition_utc`; skip if within cooldown
- After appending `regime_condition`, set `last_regime_condition_utc = now.isoformat()`

### Change 4: Code — "recently reviewed" suppression for thesis breach (FM3)
- Add `_last_position_review_utc: dict[str, datetime]` to orchestrator `__init__`
- After each position review cycle completes (both Sonnet full and position-review-only paths), stamp reviewed symbols
- In thesis breach scheduling gate: if `symbol in _last_position_review_utc` and elapsed < `thesis_breach_review_cooldown_min`, skip scheduling the Sonnet call (log at INFO)
- Add `thesis_breach_review_cooldown_min: int = 15` to scheduler config

### Prompt versioning
Changes 1 and 2 require a prompt bump to `v3.10.3`. Copy `v3.10.2/` → `v3.10.3/`, apply changes to `reasoning.txt`, update `config.json`.

---

## Files touched

| File | Change |
|---|---|
| `ozymandias/config/prompts/v3.10.3/reasoning.txt` | Changes 1, 2 |
| `ozymandias/config/config.json` | `prompt_version` → `v3.10.3`, `regime_condition_cooldown_min`, `thesis_breach_review_cooldown_min` |
| `ozymandias/core/config.py` | `regime_condition_cooldown_min`, `thesis_breach_review_cooldown_min` in SchedulerConfig |
| `ozymandias/core/orchestrator.py` | `last_regime_condition_utc` in SlowLoopTriggerState, cooldown check in `_check_triggers`, `_last_position_review_utc` dict, suppression gate in thesis breach path |
| `ozymandias/tests/test_trigger_responsiveness.py` | Tests for regime_condition cooldown |
| `ozymandias/tests/test_orchestrator.py` | Tests for thesis breach suppression |

---

## What this does not fix

- The underlying cause of why NKE had a thesis_breaking_condition that could match a speculative preview — that requires reviewing what conditions Claude set at entry, which we cannot retroactively change for the open position. The prompt fix (Change 2) prevents this for future entries.
- Claude call latency (84-111s per full cycle) — separate infrastructure concern.
