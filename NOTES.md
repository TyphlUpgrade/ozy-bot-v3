# Engineering Notes

Permanent record of open concerns, deferred work, and architectural analyses.
Not a session log — session-specific observations belong here only if they surface a lasting concern.

**Status labels:** `open` · `deferred` · `resolved` · `won't fix`

---

## Open Concerns

### CONCERN-1: No-opportunity streak WARN does not distinguish zero-Claude-output vs. ranker-blocked
**Status:** `open`  
**Severity:** Low (logging quality / diagnostic clarity)

When Claude returns zero new candidates, the streak WARN message says "no hard-filter rejections (zero Claude candidates?)". When Claude returns candidates that all fail the ranker, it says the same thing. These are different problems requiring different interventions:
- Zero Claude output → watchlist quality issue or Claude calibration
- Ranker blocking all Claude candidates → filter thresholds may be too tight

The parenthetical `(zero Claude candidates?)` is a guess, not a diagnosis. The WARN should inspect `_opp_by_symbol` at WARN time and emit a distinct label for each case.

**First observed:** 2026-03-25 session log (FINDING-1, FINDING-9)

---

### CONCERN-2: Entry conditions path bypasses composite score floor
**Status:** `open`  
**Severity:** Low (inconsistency, not a bug)

A candidate in `new_opportunities` with `entry_conditions` set is evaluated by `_medium_try_entry` using Claude's conviction as a score proxy (no composite floor check). A freshly-evaluated candidate in the normal ranker path must clear `min_composite_score` (default 0.30). PFE demonstrated this: it scored 0.56–0.58 via the conviction path while scoring 0.00 via the fresh ranker path after being pruned from Claude's output.

The inconsistency allows stale thesis candidates to compete for entries indefinitely as long as they remain in `new_opportunities`, regardless of live TA deterioration. The correct fix is to apply the composite floor check before entry even when entry_conditions are present.

**First observed:** 2026-03-25 session log (FINDING-11)

---

### CONCERN-3: `_regime_reset_build` target_count overshoots prune threshold
**Status:** `open`  
**Severity:** Low (minor churn)

`_regime_reset_build` rebuilds with `target_count=20` immediately after evicting direction-conflicting entries. The newly added symbols have no TA data yet (`long_score: 0.0`). On the next pruning pass, some are evicted for low score or direction conflict with an adjacent sector. The next reset adds 20 more. This creates an add→prune→add cycle.

The fix is trivial: lower `target_count` in the reset build to `watchlist_build_target` (config default 8) and let subsequent scheduled builds fill in as TA data arrives. The current `target_count=20` was chosen for "full rebuild semantics" but in practice overshoots the stable capacity.

**First observed:** 2026-04-01 engineering analysis (Concern 2 — watchlist churn)

---

## Resolved Concerns

Resolved items are deleted after one session. See `DRIFT_LOG.md` for the permanent record of what was implemented and why.

---

## Engineering Analyses

### 2026-04-01 — Slow Loop Latency

A full slow loop cycle with all triggers active made **four sequential Claude round-trips** before returning:

```
account fetch (500ms)
→ daily bars, parallel gather (2s)
→ watchlist build if stale        ← Claude call 1: 30–120s  [FIXED — now background]
→ position reviews, split Call A  ← Claude call 2: 2–5s
→ Haiku pre-screen                ← Claude call 3: 2–3s
→ Sonnet reasoning, Call B        ← Claude call 4: 15–45s
```

Worst-case was ~200s in a single blocking cycle. Root cause: `_run_claude_cycle` handled both the watchlist build and reasoning paths sequentially. When `watchlist_stale` co-fired with any reasoning trigger (common given a 60-minute max interval), the build ran to completion — including web search tool-use rounds — before position reviews or opportunity discovery began.

**Resolution:** Phase 23 decoupled the build into `_run_watchlist_build_task()` as a background task. The remaining three calls (Call A, Haiku, Call B) are already sequential by design — Call A and Call B are ordered by architecture (reviews before opportunities), and Haiku pre-screens before Sonnet sees candidates.

**Remaining latency note:** Call A still runs even when no positions are open. One-line guard to skip it would remove 5s per flat-book cycle — not yet implemented (CONCERN not opened; trivial when it becomes a real cost).

### 2026-04-01 — Watchlist Churn Analysis

Three distinct churn sources identified:
1. **Time-bounded catalyst entries with no expiry** — WRB held for 109 hours after catalyst window. Fixed by `catalyst_expiry_utc`.
2. **Data-unavailable symbols in tier-1 context** — yfinance failures produced `long_score: 0.0` entries that Claude still spent reasoning budget on. Fixed by `fetch_failure` suppression.
3. **`_regime_reset_build` overshoot** — rebuilds with target_count=20 immediately after eviction; new entries have no TA data and are pruned on the next pass. See CONCERN-3 (open).
