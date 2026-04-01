# Session Findings — 2026-03-25

**Session log:** `logs/session_2026-03-25T12-05-43Z.log`
**Coverage:** 8:05 AM ET (pre-market start) → 12:20 PM ET (log current — dead zone in progress)
**Equity at startup:** $30,637.08 | **Positions at startup:** 4 (XLF, BAC, CVX, NUE) | **Watchlist:** 40 (32 tier-1)

---

## Session Summary

| # | Time (ET) | Event |
|---|-----------|-------|
| 1 | 8:05 AM | Bot started pre-market. Clean reconciliation — all 4 positions matched broker. |
| 2 | 9:30 AM | Market open. TA seeded in 2.5s. 6 triggers fired simultaneously. |
| 3 | 9:32 AM | First watchlist build: 50 RVOL candidates, 20 added, 15 pruned over cap=40. |
| 4 | 9:33 AM | First Claude cycle: 4 reviews (all hold), 1 opportunity (CAR — immediately blocked RSI 84.4). |
| 5 | 9:40 AM | CVX exited: Claude correctly flagged thesis invalidation (oil diving on Trump peace plan). PnL: **-1.46%**. |
| 6 | 10:03 AM | ALNY entered: 9 shares @ $321.10, target $340, stop $305 (swing, 12% size). |
| 7 | 10:22–25 AM | KLAC entered: 1 share @ $1,565.00, target $1,650, stop $1,500 (swing, 10% size, 4 deferred attempts). |
| 8 | 11:13 AM | `time_ceiling` triggered Claude: 0 new opportunities, 5 position reviews (all hold). |
| 9 | 11:32–11:57 AM | Claude 529 overload cascade (dead zone overlap). ~12 watchlist build failures over 25 min. |
| 10 | 11:57 AM | Second watchlist build finally succeeded: added VRTX, SLB, EXAS, PFE, ORKA, PCG + 7 tier-2. |
| 11 | 12:14 PM | Second `time_ceiling` Claude cycle: 1 opportunity (MRK), then blocked by dead zone. |
| 12 | 12:20 PM | Log current. MRK still waiting on dead zone expiry (14:30 ET). |

---

## Positions

### Carried Overnight (opened prior sessions)

| Symbol | Strategy | Direction | Unrealized at Review | Stop | Target | Status |
|--------|----------|-----------|----------------------|------|--------|--------|
| XLF | swing | long | +$24.70 / +1.32% @ $49.87 | $47.80 | $51.50 | Holding — 118+ hours. Buffer to stop: $2.09. Buffer to target: $1.63. Sector composite degrading (0.605 → 0.365 → 0.44). |
| BAC | swing | long | +$24.57 / ~+0.5% @ $48.93 | $46.80 | $50.50 | Holding — 49 hours. Buffer to stop: $1.87 (narrow). |
| CVX | swing | long | closed at -1.46% | — | — | **EXITED** 9:40 AM. Oil price thesis inverted by Iran ceasefire news. |
| NUE | swing | long | +$37.72 / +1.78% @ $165.685 | $158.00 | $170.00 | Holding — 20+ hours. `position_in_profit` snapshot fired at open. Stop not raised despite $7+ buffer. |

### Opened Today

| Symbol | Strategy | Direction | Entry | Shares | Stop | Target | Size% | Conviction | Score |
|--------|----------|-----------|-------|--------|------|--------|-------|-----------|-------|
| ALNY | swing | long | $321.10 | 9 | $305.00 | $340.00 | 12% | 0.70 | 0.553 |
| KLAC | swing | long | $1,565.00 | 1 | $1,500.00 | $1,650.00 | 10% | 0.68 | 0.535 |

---

## Execution Quality

### CVX Exit — Correct
Claude recognized the geopolitical premium thesis was directly invalidated (peace plan news → oil price drop). Exit fired via market order, filled in ~5 seconds. Demonstrates thesis-based exit working as intended.

### ALNY Entry — RSI Warning
ALNY entered at **RSI 85.55**. This is above the 78 hard RSI ceiling — but that ceiling is enforced only for momentum entries, not swing. Claude classified this as swing with a clear biotech catalyst (AMVUTTRA ATTR-CM, raised 2026 TTR revenue outlook). The ranker did not block it. Whether this is correct behavior depends on thesis quality, but the entry data should be watched: RSI 85.55 is genuinely extended.

Additional concern: `volume_ratio` at entry was 0.86 (below 1.0). The swing RVOL floor was set to 0.0 last session specifically to allow low-RVOL swing entries. No system issue here — but ALNY doesn't have volume confirmation behind the move.

### KLAC Entry — RVOL Concern
KLAC entered with `volume_ratio: 0.1476` — essentially zero volume participation (15% of average). Claude's 4-cycle RSI gate (rsi_min: 52) deferred entry appropriately until RSI cleared. But RVOL is 0.15, which is far below even the removed 0.0 floor. Trend structure is `mixed` (not bullish-aligned). The entry qualifies mechanically but is weak on volume and trend.

Position size: 10% of equity at $1,565/share → 1 share = $1,565 (~5.1% effective). This is the integer rounding floor. Not a bug; just the consequence of high-priced stocks with modest conviction.

### DOW — Entry Conditions Working Correctly
Claude recommended DOW swing long with `rsi_slope_min: 0.30`. Slope was -1.43 at time of recommendation. The entry gate deferred correctly. Claude subsequently self-removed DOW from candidates at the next cycle ("repeated technical gate failures"). This is the entry conditions system working as designed.

### FSLR — Conviction Cap Enforced
FSLR proposed as `catalyst_type: technical_only` swing with conviction 0.72. Hard limit is 0.50 for technical_only swings. Correctly blocked 3× and suppressed for session. Claude correctly self-referenced this in later cycle rejections.

---

## No-Opportunity Streak

### First streak: 8 loops (10:13–10:33 AM ET)
Broken by KLAC entry. Primarily caused by CAR, SRPT, ARM, SATS, MRVL, CIFR, PDD, INTC all session-suppressed within ~40 minutes of open (RSI ceiling violations and RVOL failures). Watchlist was effectively depleted of actionable momentum candidates at open.

### Second streak: 8–30 loops (10:40 AM – 12:20 PM, ongoing)
The streak WARN message says "no hard-filter rejections (zero Claude candidates?)" — this is inaccurate. Throughout the dead zone, the ranker log shows "0 candidates, 0 passed filters, 0 rejected" which means Claude is returning zero new opportunities, not that rejections are missing. The streak message correctly signals a lack of Claude output, but the gate-breakdown phrasing is misleading. Claude stopped proposing entries after the `time_ceiling` cycle at 11:13 AM returned nothing, and the second `time_ceiling` at 12:14 PM proposed MRK (immediately dead-zone blocked). This is consistent with a low-momentum dead zone environment, not a system malfunction.

**Finding:** The gate-breakdown WARN message does not distinguish between "Claude sent candidates and ranker blocked them all" vs. "Claude sent zero candidates." Both show as "no hard-filter rejections." The former needs gate tuning; the latter needs watchlist/prompt attention. They are different problems and the WARN should identify which case applies.

---

## Claude Overload Cascade (11:32–11:57 AM ET)

At 15:32 UTC, `watchlist_stale` triggered a build. Claude returned 529 overloaded on all 3 retries. The circuit breaker then fired ("3 consecutive overload fallbacks") and blocked subsequent attempts until probe succeeded. The cascade lasted ~25 minutes and produced ~12 failed build attempts.

**Behavior observed:**
- `watchlist_stale` re-fires every ~60 seconds when `last_watchlist_build_utc` hasn't been updated (correct — it fires on failure because the timestamp isn't bumped).
- Circuit breaker correctly blocks repeated attempts once threshold hit.
- Circuit breaker probe timeout (`circuit_breaker_probe_min: 10`) eventually cleared and the build succeeded at 15:57 UTC.

**Issue: No backoff on watchlist_stale re-fire after failed build.** After a failure, the trigger fires again after just one slow-loop check (~60s). With a circuit breaker probe interval of 10 minutes, the trigger hammers the circuit breaker 9 times per probe window, all rejected. This is noisy but not harmful — no real Claude calls are made while the circuit breaker is active. However, it generates many error log lines and makes the log harder to read.

**Issue: Timing overlap with dead zone.** The watchlist build failures at 11:32 AM landed exactly at dead zone start. Watchlist builds are correctly not gated by the dead zone (they are not entries), but the proximity creates an operational pattern: dead zone = lower market activity = Claude more available. The 529s may be coincidental timing from earlier in the session rather than dead zone effects.

---

## Trigger Behavior

All triggers fired correctly:
- `session_open`: fired at first slow loop cycle after market open ✓
- `position_in_profit:NUE`: fired at 1.78% gain (threshold 1.5%) ✓
- `market_rsi_extreme`: fired at open (4/12 market breadth = 33%, panic RSI territory) ✓
- `price_move:ARM`, `price_move:MU`, `price_move:CIFR`, `price_move:PDD`, `price_move:ELVN`: all fired on appropriate price moves ✓
- `time_ceiling`: fired at 11:13 AM and 12:14 PM ✓
- `watchlist_stale`: fired at 120-min interval ✓

**Pre-market startup behavior worked correctly.** Bot idled cleanly from 8:05 AM to 9:30 AM. Session transition triggered correctly on first slow loop after market open.

---

## Issues and Findings

### FINDING-1: No-opportunity streak WARN does not distinguish zero-candidates vs. ranker-blocked
**Severity:** Low (logging quality)
**Description:** When Claude returns zero new candidates, the streak message says "no hard-filter rejections (zero Claude candidates?)" — the parenthetical is a guess rather than a definitive label. When Claude returns candidates that all fail the ranker, the message also says the same thing if the `_opp_by_symbol` dict is empty at the time of the breakdown scan. The two cases (no Claude output vs. ranker filtering) require different interventions and should be labeled distinctly.

### FINDING-2: ALNY entered at RSI 85.55 (swing, no RSI ceiling enforcement)
**Severity:** Medium (calibration risk)
**Description:** Swing entries have no hard RSI ceiling. The 78 ceiling applies only to momentum. ALNY at RSI 85.55 is more overextended than the worst momentum entries the ranker blocks. If ALNY pulls back, the $16.10 stop buffer ($321.10 - $305.00) may not hold a mean-reversion move. Watch this position.

### FINDING-3: KLAC entered with 0.15 RVOL and mixed trend structure
**Severity:** Low-Medium (calibration)
**Description:** Volume_ratio 0.1476 is essentially no participation. Trend structure is `mixed`. Claude cited AI infrastructure demand / StockStory analyst upgrades as the catalyst. With only 1 share entered (integer floor of 10% sizing on a $1,565 stock), risk exposure is low. Monitor.

### FINDING-4: Watchlist_stale hammers circuit breaker during Claude 529 outage
**Severity:** Low (operational noise)
**Description:** After 3 consecutive 529 failures, circuit breaker fires. But `watchlist_stale` still re-fires every 60s, hitting the circuit breaker repeatedly (all rejected instantly). With `circuit_breaker_probe_min: 10`, this produces ~9 error log lines per probe window. Consider adding a minimum delay to watchlist_stale re-fire after a failed build — something like "if last build failed, wait at least `circuit_breaker_probe_min` minutes before re-firing."

### FINDING-5: MRK pending entry blocked by dead zone (expected, not a bug)
**Severity:** Informational
**Description:** MRK (conviction 0.75, score 0.66, swing) recommended at the 12:14 PM `time_ceiling` cycle. Three consecutive entry attempts blocked by dead zone (11:30–14:30 ET). Will retry after 2:30 PM ET if still valid.

### FINDING-6: NUE stop not raised despite strong profit position
**Severity:** Informational / watch
**Description:** NUE is at +$7.685 above the $158 stop ($165.685 vs $158) with $4.315 remaining to target ($170). Claude reviewed NUE 8+ times today and each time cited "insufficient hold time" (18-20 hours) as the primary counter-argument. At 20+ hours, the thesis has held well. Claude did not raise the stop. The prompt instructs stop-raising when a "primary catalyst has triggered" or "thesis milestone reached" — Claude appears to be waiting for a clearer milestone signal.

---

---

## Afternoon Session (12:20 PM → 3:03 PM ET close)

### Session Timeline Update

| # | Time (ET) | Event |
|---|-----------|-------|
| 13 | 12:20–2:30 PM | Dead zone in effect. MRK blocked continuously (~20 loops). PFE deferred past max_entry_defer_cycles. |
| 14 | 1:57 PM | `watchlist_stale` fires. Universe scan: 45 candidates. |
| 15 | 1:58–1:59 PM | 429 rate limit during watchlist build (1 retry, 30s backoff). Build succeeded: 20 suggestions, 19 pruned (inc. MRK, PFE, ELVN). |
| 16 | 2:04 PM | PFE scores composite 0.00 (new ranker path post-watchlist-build). Hard-filtered 3×, session-suppressed. |
| 17 | 2:18–2:28 PM | TEL recommended at 2:16 PM time_ceiling cycle. Blocked 6 loops by dead zone (ends 2:30 ET). |
| 18 | 2:30 PM | Dead zone clears. TEL filled immediately: 14 shares @ $209.93. stop=$202, target=$220. |
| 19 | 2:30–3:03 PM | 16 consecutive empty medium loops. No new Claude candidates. No-opportunity WARN streams. |
| 20 | 3:03 PM | Session 1 interrupted. Session 2 started (6 positions: XLF, BAC, NUE, ALNY, KLAC, TEL). |
| 21 | 3:03 PM | position_in_profit:NUE and position_in_profit:ALNY triggers fired at session 2 open. |
| 22 | 3:03 PM | Session 2 immediately interrupted (minimal log). Session 3 immediately interrupted post-market. |

### Position Outcomes (session-end)

| Symbol | Entry | Last Price | Unrealized | Notes |
|--------|-------|-----------|-----------|-------|
| XLF | ~$49.19 | $49.41 | +0.38% | 118+ hours. Narrow buffer. |
| BAC | ~$48.32 | $48.70 | +0.76% | 51+ hours. Narrow buffer. |
| NUE | ~$162.78 | $165.95 | +1.95% | 22+ hours. Strong. Stop not raised. |
| ALNY | $321.10 | $326.39 | +1.64% | 4+ hours. RSI entry concern. |
| KLAC | $1,565.00 | $1,557.58 | -0.47% | 3.9 hours. Weak volume. |
| TEL | $209.93 | ~$210.00 | ~+0.0% | Just entered at dead zone clearance. |

CVX closed -1.49% at 9:40 AM. MRK was never entered (pruned from watchlist at 1:57 PM).

**Equity at day end: $30,708 (startup: $30,637). Net P&L: +$71 (+0.23%). Cash idle: ~$17,261.**

---

## Additional Findings (Afternoon Session)

### FINDING-7: PFE defer expiry bug — opportunity persists past max_entry_defer_cycles [FIXED 2026-03-25]
**Severity:** High (structural)
**Description:** `max_entry_defer_cycles: 5` is designed to remove an opportunity after 5 consecutive entry-condition failures, treating the thesis as stale. PFE accumulated **18 consecutive defers** before being cleared by the watchlist prune. The WARN message correctly said "expired after N consecutive defers (stale thesis gate; awaiting fresh Claude call)" starting at defer 5 — but the code continued ranking PFE, continued attempting entry, and continued incrementing the counter. The message "awaiting fresh Claude call" is also incorrect: no mechanism exists to trigger a Claude call specifically because an opportunity expired. PFE was eventually cleared by the watchlist rotation pruning it (17:59 UTC), not by the expiry logic.

**Root cause:** The expiry warning fires correctly at N ≥ `max_entry_defer_cycles`, but the opportunity is not removed from `_entry_defer_counts` or from the set of active candidates being ranked. The opportunity remains in Claude's `new_opportunities` output from a prior cycle, which gets re-scored and re-ranked indefinitely.

**Fix (implemented):** On expiry, symbol is now added to `_filter_suppressed` (same dict as hard-filter session suppression) with reason `"stale thesis gate: entry conditions expired after N consecutive defers"`. This immediately stops the ranker from evaluating it and prevents re-entry for the session. Claude will reconsider on the next watchlist build. The prior `top.entry_conditions = {}` approach was ineffective because `top` is rebuilt from the reasoning cache on each medium loop.

**Impact today:** PFE blocked 13 extra medium-loop cycles (defers 6–18) that could have been used to evaluate new candidates. Given the watchlist was otherwise quiet, impact was limited. But in an active session with multiple stale theses, this could lock the system into zombie candidates indefinitely.

---

### FINDING-8: MRK was dead-zone blocked then pruned — never given a chance to enter [FIXED 2026-03-25]
**Severity:** Medium (capital utilization)
**Description:** MRK (conviction 0.75, score 0.66, swing) was recommended at the 12:14 PM `time_ceiling` cycle. It was immediately blocked by the dead zone (11:30–2:30 PM ET). Before the dead zone could lift, the 1:57 PM watchlist build ran and pruned MRK from the watchlist (19 symbols rotated out). MRK was never entered despite being Claude's best recommendation for the afternoon session.

**Root cause:** Two behaviors interacted adversely: (1) dead zone blocking a valid swing entry for 2+ hours, and (2) watchlist rotation pruning mid-block. Swing entries are explicitly excluded from dead zone in the spec ("swing entries allowed 9:30 AM–4:00 PM"). But they are blocked anyway.

**Observation:** Dead zone was designed for momentum scalps to avoid whipsaw in the noon lull. Blocking swing entries during dead zone may be over-aggressive — a multi-day swing thesis doesn't become invalid because the clock says 12:45 PM. Today this cost us a 0.75-conviction entry.

**Fix (implemented):** Added `dead_zone_exempt` property to `Strategy` ABC (default `False`). `SwingStrategy.dead_zone_exempt = True`. `validate_entry` and `_check_market_hours` in `risk_manager.py` accept the flag and skip the dead zone check when set. Orchestrator passes `strategy_obj.dead_zone_exempt` at the call site. Same pattern as `blocks_eod_entries`.

---

### FINDING-9: No-opportunity streak WARN has no escalation — fires 16× at end of session
**Severity:** Low (operational noise / actionability)
**Description:** After PFE was session-suppressed at 2:04 PM, there were zero Claude candidates for the remaining ~60 minutes. The no-opportunity WARN message fired 16 consecutive times (8 WARN, 9 WARN, 10 WARN, ... 16 WARN). No action was taken — no Claude call, no watchlist reassessment, no operator alert. The warnings are correct but valueless without escalation.

**Observation:** In a "zero Claude candidates" state, the system has already exhausted the prior Claude cycle's output. The `watchlist_stale` timer (120 min) is the only mechanism to force a new Claude call. Between builds, the system can spend over an hour spinning with no actionable candidates and no path to create any. This is the primary driver of capital underutilization in afternoons.

**Note:** This is distinct from Finding-4 (watchlist_stale re-fire timing). This finding is about the state *after* a new watchlist has been built but Claude's output is already exhausted.

---

### FINDING-10: Rate limit 429 during watchlist build — retried and recovered
**Severity:** Low (operational resilience)
**Description:** At 17:58:10 UTC, tool-use rounds were exhausted (3), forcing a final response without tools. Immediately after at 17:58:12, a 429 rate limit error hit on the final Claude response attempt (not the tool-use call), retried in 30s, and the build completed at 17:59:11. Total impact: ~60s delay. Build produced 20 suggestions.

This is the same issue as LOG-FINDING-B in the plan (Brave Search 429s hitting tool-use rounds). The retry on the main Claude 429 worked correctly. The tool-use exhaustion (3 rounds) is a separate issue — Claude wanted more search rounds but hit the ceiling. The final forced response still produced a valid 20-ticker watchlist, so the round limit doesn't block completion, it just constrains Claude's research depth.

---

### FINDING-11: PFE composite score dropped to 0.00 after watchlist build
**Severity:** Low (unexpected behavior, worth understanding)
**Description:** Before the watchlist build (17:39–17:57), PFE was being scored at `score=0.56–0.58` and passing the ranker's soft threshold. After the watchlist build at 17:59, PFE immediately scored `composite_technical_score: 0.00` on the next medium loop and was hard-filtered.

**Analysis:** Before the build, PFE was reaching `_medium_try_entry` via the Claude new_opportunities path (it had entry_conditions that were being checked). The `score=0.58` came from Claude's `conviction × composite` estimate, not the live ranker composite. After the new watchlist build rotated PFE out of new_opportunities, PFE went through the normal ranker path where `composite_technical_score` was computed fresh from live indicators (RSI ~45–50, declining momentum). The 0.00 score correctly reflected dead momentum. The previous 0.58 appearance was an artifact of the entry_conditions path not requiring a passing composite score before attempting entry. This is a subtle inconsistency: a stale-thesis candidate can attempt entry with a conviction-proxied score indefinitely, while a freshly-evaluated candidate must clear the composite floor.

---

## Full-Day Findings Summary

### Strategy Issues
1. **ALNY entered at RSI 85.55** — swing has no RSI ceiling (FINDING-2). Needs `rsi_max` in entry_conditions when Claude observes elevated RSI.
2. **KLAC entered with RVOL 0.15 / mixed trend** — weak volume confirmation (FINDING-3). Position is small (1 share) so risk limited.
3. **Dead zone blocks swing entries** — MRK (conviction 0.75) was blocked for 2 hours then pruned. Swing entries should exempt from dead zone (FINDING-8). **Actionable: add `dead_zone_applies` flag to strategy ABC.**

### Structural Bugs
4. **PFE defer expiry bug** — opportunities persist indefinitely past `max_entry_defer_cycles` (FINDING-7). **Actionable: clear opportunity from candidates on expiry.** Priority: High.
5. **Entry conditions path bypasses composite floor** — stale candidates can score 0.58 (conviction proxy) while fresh candidates must clear 0.30 composite floor (FINDING-11). Low priority, but inconsistent.

### Operational / Logging
6. **No-opportunity streak WARN doesn't distinguish zero-candidates vs. ranker-blocked** (FINDING-1). Low priority; diagnostic quality only.
7. **watchlist_stale hammers circuit breaker during outages** — partially fixed by Problem A backdate (implemented this session). Residual issue: after a successful build, PFE cleared but session had nothing left in the pipeline (FINDING-9).
8. **Rate limit 429 during watchlist build (tool-use exhaustion)** — handled gracefully, build succeeded (FINDING-10). LOG-FINDING-B from plan remains open.

### Capital Utilization
- Day ended with $17,261 cash idle (56% of equity undeployed)
- 6 positions held, none above 12% size
- Primary cause: afternoon dead zone + watchlist rotation removed viable candidates before they could enter
- Secondary cause: PFE zombie kept the pipeline visually active but was unenterable; system didn't seek new Claude guidance

## Next Session Watchpoints

1. **ALNY**: RSI 85.55 entry is extended. Watch for reversal. Claude should tighten stop or set `rsi_max` in future conditions.
2. **KLAC**: Still -0.47% with weak RVOL and mixed trend. Below 4-hour minimum hold. Monitor thesis validity at first Claude cycle.
3. **TEL**: Brand new entry (14 shares @ $209.93). Swing, conviction 0.72. First review will be at next `time_ceiling` cycle.
4. **XLF and BAC**: Narrow stop buffers after 5+ day holds. Any sector rotation triggers stops.
5. **PFE defer expiry bug**: Should be fixed before next session to prevent zombie candidates consuming pipeline capacity.
6. **Dead zone / swing interaction**: If a good swing candidate appears in the morning `time_ceiling` cycle, verify it isn't going to be blocked and then pruned before 2:30 PM.

---

# Engineering Analysis — 2026-04-01

## Concern 1: Slow Loop Latency

### Root Cause

A full slow loop cycle with all triggers active can make **four sequential Claude round-trips** before returning:

```
account fetch (500ms)
→ daily bars, parallel gather (2s)
→ watchlist build if stale    ← Claude call 1: 30–120s
→ position reviews, split Call A  ← Claude call 2: 2–5s
→ Haiku pre-screen            ← Claude call 3: 2–3s
→ Sonnet reasoning, Call B    ← Claude call 4: 15–45s
```

Worst-case total: ~200s in a single blocking cycle.

### Two Main Offenders

**1. Watchlist build blocks reasoning when co-triggered.**
`_run_claude_cycle` handles both the watchlist build path and the reasoning path sequentially. When `watchlist_stale` co-fires with any reasoning trigger (the common case given a 60-minute max interval), the build call — including web search tool-use rounds — runs to completion before position reviews or opportunity discovery begins. The reasoning cycle waits behind a 60-second watchlist refresh even though it could proceed with the existing watchlist immediately.

The build correctly returns early when it's the *only* trigger. But when combined with other triggers, there's no short-circuit. The reasoning cycle has no awareness it's waiting behind a build.

**2. Split-call overhead when there are no positions.**
Call A (`run_position_review_call`) was designed to be compact and fast. It is — ~2–5s. But it runs unconditionally even when there are no open positions, in which case it makes an API handshake, waits, and returns an empty list. There is no guard that skips Call A when the portfolio is flat.

### Recommendation

- **Decouple watchlist builds from `_run_claude_cycle` entirely.** The build should always run as a background task scheduled independently. When a reasoning trigger fires and a build is already in progress, reasoning proceeds with the existing watchlist. The watchlist is 120 minutes stale either way — waiting another 30–60s doesn't improve the reasoning quality.
- **Short term (lower effort):** When reasoning triggers co-fire with `watchlist_stale`, defer the build to *after* reasoning returns, not before.
- **Skip Call A when no positions are open.** One-line guard, removes 5s of unnecessary overhead on every cycle with a flat book.

---

## Concern 2: Watchlist Churn

### Root Cause

The watchlist is treated as a scratchpad rather than a conviction ledger. Entries have no lifecycle semantics — no expiry, no minimum dwell time, no distinction between "actively monitoring" and "thesis expired." Three distinct churn sources:

**1. Time-bounded catalyst entries with no expiry.**
The canonical case: WRB held on an "imminent earnings catalyst" for 109 hours after the catalyst window passed. Claude kept including it as a valid tier-1 entry because nothing removed it. It competed for watchlist slots against live setups and consumed reasoning tokens every cycle. Without `catalyst_expiry_utc`, any time-sensitive entry lingers indefinitely.

**2. Data-unavailable symbols consuming tier-1 slots.**
When yfinance fails for a symbol, it appears to Claude with `latest_price: null` and `long_score: 0.0`. Claude still proposes it — it's on the watchlist, so it appears in `watchlist_tier1` context. The ranker rejects it immediately (zero score), but Claude spent reasoning budget on it and it blocks a slot that could go to a scorable candidate.

**3. Regime-reset overshooting.**
`_regime_reset_build` evicts direction-conflicting entries, then immediately rebuilds with `target_count=20`. The newly-added symbols haven't been scanned yet, so they enter the watchlist with `long_score: 0.0`. In the next pruning pass they're evicted because their score is 0 or their direction conflicts with an adjacent sector. The next regime reset adds 20 more. This is the active churn cycle: add 20 → scan → prune → add 20 again.

### Recommendation

**Priority 1 — Implement the plan already written** (`catalyst_expiry_utc` + fetch-failure suppression). These directly address churn sources 1 and 2 without architectural risk. The plan is documented and the implementation is straightforward.

**Priority 2 — Reduce `_regime_reset_build` target_count.** Rebuilding to 20 entries immediately after an eviction run overshoots. Lower to 8 (matching `watchlist_build_target`) and let subsequent builds fill in as TA data arrives. The current behavior overshoots, the pruner corrects, the next reset overshoots again.

### Priority Order

| # | Change | Impact | Effort |
|---|--------|--------|--------|
| 1 | `catalyst_expiry_utc` + fetch-failure suppression | Eliminates churn sources 1 and 2 | Medium |
| 2 | Skip `run_position_review_call` when no positions | Removes 5s overhead per cycle | Trivial |
| 3 | Defer watchlist build to after reasoning when co-triggered | Removes worst-case latency spike | Low |
| 4 | Decouple watchlist build from `_run_claude_cycle` entirely | Eliminates build-blocks-reasoning structurally | Medium |
| 5 | Lower `_regime_reset_build` target_count to 8 | Reduces overshoot/prune cycle | Trivial |
