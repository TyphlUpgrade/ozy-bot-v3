# Engineering Notes

Permanent record of open concerns, deferred work, and architectural analyses.
Not a session log — session-specific observations belong here only if they surface a lasting concern.

**Status labels:** `open` · `deferred` · `resolved` · `won't fix`

---

## Open Concerns

### CONCERN-1: No-opportunity streak WARN does not distinguish zero-Claude-output vs. ranker-blocked
**Status:** `resolved`  
**Severity:** Low (logging quality / diagnostic clarity)

When Claude returns zero new candidates, the streak WARN message says "no hard-filter rejections (zero Claude candidates?)". When Claude returns candidates that all fail the ranker, it says the same thing. These are different problems requiring different interventions:
- Zero Claude output → watchlist quality issue or Claude calibration
- Ranker blocking all Claude candidates → filter thresholds may be too tight

The parenthetical `(zero Claude candidates?)` is a guess, not a diagnosis. The WARN should inspect `_opp_by_symbol` at WARN time and emit a distinct label for each case.

**Note (2026-04-02):** The Phase 23 `candidates_exhausted` → build routing partially mitigates the symptom. When Claude outputs zero candidates and the streak reaches threshold, `candidates_exhausted` now triggers a fresh build instead of more reasoning. However, the log message itself still doesn't distinguish the two cases at WARN time. Fixed 2026-04-02.

**First observed:** 2026-03-25 session log (FINDING-1, FINDING-9)

---

### CONCERN-2: Entry conditions path bypasses composite score floor
**Status:** `open`  
**Severity:** Low (inconsistency, not a bug)

A candidate in `new_opportunities` with `entry_conditions` set is evaluated by `_medium_try_entry` using Claude's conviction as a score proxy (no composite floor check). A freshly-evaluated candidate in the normal ranker path must clear `min_composite_score` (default 0.30). PFE demonstrated this: it scored 0.56–0.58 via the conviction path while scoring 0.00 via the fresh ranker path after being pruned from Claude's output.

The inconsistency allows stale thesis candidates to compete for entries indefinitely as long as they remain in `new_opportunities`, regardless of live TA deterioration. The correct fix is to apply the composite floor check before entry even when entry_conditions are present.

**First observed:** 2026-03-25 session log (FINDING-11)

---

## Resolved Concerns

Resolved items are deleted after one session. See `DRIFT_LOG.md` for the permanent record of what was implemented and why.

---

## Engineering Analyses

### 2026-04-01 — Phase 23 Validation (post-market run)

Session `2026-04-01T20:40:47Z` was the first run with all Phase 23 changes active.

**Build decoupling confirmed:** Watchlist was 67 minutes old at startup (stale threshold typically 60 min), but `no_previous_call|indicators_ready` fired as a reasoning-only trigger. The Claude call started at 20:40:50Z and completed at 20:42:04Z (74.3s). No build blocking — under the old architecture this cycle would have run a 30-120s build first.

**Regime and output:** risk-off panic at confidence 0.72. Five candidates returned: NKE, LLY, NVO, XOM, CVX — all shorts or energy plays consistent with the regime. Four rejections (CAT, V, MS, WMT) with coherent rationale. LLY hard-filtered on RSI 38.8 (momentum at oversold RSI — prompt fixed). filter_adjustments applied min_rvol=0.55 for LLY/NKE/NVO catalysts; consistent across all three medium loop cycles.

**No entries — expected:** 7-minute post-market session. NKE deferred on `rsi_slope_max=0.00` (slope 5.10 — RSI still rising, correct gate for a short). NVO deferred on `require_macd_bearish` with signal='bullish' (correct). CVX blocked by wrong rsi_accel gate (prompt fixed). Session ended manually after 3 cycles.

**Token usage:** 11,924 input / 3,751 output. No truncation warnings in Call B context.

---

### 2026-04-01 — Slow Loop Latency

A full slow loop cycle with all triggers active made **four sequential Claude round-trips** before returning:

```
account fetch (500ms)
→ daily bars, parallel gather (2s)
→ watchlist build if stale        ← Claude call 1: 30–120s  [FIXED — now background]
→ position reviews, split Call A  ← Claude call 2: 2–5s     [FIXED — skipped when no positions]
→ Haiku pre-screen                ← Claude call 3: 2–3s
→ Sonnet reasoning, Call B        ← Claude call 4: 15–45s
```

Worst-case was ~200s in a single blocking cycle. Root cause: `_run_claude_cycle` handled both the watchlist build and reasoning paths sequentially. When `watchlist_stale` co-fired with any reasoning trigger (common given a 60-minute max interval), the build ran to completion — including web search tool-use rounds — before position reviews or opportunity discovery began.

**Resolution:** Phase 23 decoupled the build into `_run_watchlist_build_task()` as a background task. Call A is skipped when `portfolio.positions` is empty. The remaining two calls (Haiku, Sonnet) are sequential by design — Haiku pre-screens before Sonnet sees candidates.

### 2026-04-01 — Watchlist Churn Analysis

Three distinct churn sources identified:
1. **Time-bounded catalyst entries with no expiry** — WRB held for 109 hours after catalyst window. Fixed by `catalyst_expiry_utc`.
2. **Data-unavailable symbols in tier-1 context** — yfinance failures produced `long_score: 0.0` entries that Claude still spent reasoning budget on. Fixed by `fetch_failure` suppression.
3. **`_regime_reset_build` overshoot** — fixed; now uses `watchlist_build_target` (config default 8) instead of 20. New entries have no TA data and were pruned on the next pass under the old value.
