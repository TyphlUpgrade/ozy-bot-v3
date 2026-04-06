# Engineering Notes

Permanent record of open concerns, deferred work, and architectural analyses.
Not a session log — session-specific observations belong here only if they surface a lasting concern.

**Status labels:** `open` · `deferred` · `resolved` · `won't fix`

---

## Open Concerns

### CONCERN-2: Entry conditions path bypasses composite score floor
**Status:** `open`  
**Severity:** Low (inconsistency, not a bug)

A candidate in `new_opportunities` with `entry_conditions` set is evaluated by `_medium_try_entry` using Claude's conviction as a score proxy (no composite floor check). A freshly-evaluated candidate in the normal ranker path must clear `min_composite_score` (default 0.30). PFE demonstrated this: it scored 0.56–0.58 via the conviction path while scoring 0.00 via the fresh ranker path after being pruned from Claude's output.

The inconsistency allows stale thesis candidates to compete for entries indefinitely as long as they remain in `new_opportunities`, regardless of live TA deterioration. The correct fix is to apply the composite floor check before entry even when entry_conditions are present.

**First observed:** 2026-03-25 session log (FINDING-11)

---

---

### DEFERRED-1: DRIFT_LOG File Index needs to be built
**Status:** `deferred`  
**Effort:** ~30–45 minutes

`DRIFT_LOG.md` has 47 sections with no index. Finding what's relevant to a given file currently requires reading across multiple sections. A File Index table (file → relevant section names) is stubbed at the top of DRIFT_LOG with a maintenance instruction, but the table itself is empty.

Build the index by reading each section and mapping file references. The instruction to update it on each new entry is already in place. Focus on the files developers actually touch — `orchestrator.py`, `risk_manager.py`, `claude_reasoning.py`, `opportunity_ranker.py`, `strategy` modules — not test files or one-off prompt entries. `orchestrator.py` and `config.py` will list nearly every section; for those, consider whether a row is useful or just noise.

**Do at the start of a fresh session — not as an end-of-day addition.**

---

### CONCERN-3: Slope/accel indicators underutilised in entry conditions
**Status:** `open`  
**Severity:** Medium (suboptimal entry gates, not a correctness bug)

`rsi_slope_5` and `rsi_accel_3` are computed, not in `_TA_EXCLUDED`, and visible to Claude in `ta_readiness`. The prompt mandates slope conditions for momentum entries (reasoning.txt line 70). In practice Claude ignores this mandate for swing entries and inconsistently for momentum — defaulting to simpler gates (`rsi_max`, `require_below_vwap`) that require less calibration work.

The FISV `rsi_slope_max=0.5` case (2026-04-02 session) is the clearest symptom: Claude used the slope condition but got the sign wrong, and the feedback loop gave it no signal that the condition was structurally invalid. The `last_block_reason` fix (2026-04-02) addresses this for the invalid-value case.

**Remaining gap:** Prompt design. The mandate approach has not worked — making it louder won't either. The correct approach is to tie condition selection to the setup type Claude already declared rather than mandating specific fields categorically. For swing entries specifically:

> *"Your entry_conditions must be consistent with the setup description you wrote. Breakdown/momentum continuation → rsi_slope_max is the primary gate (check ta_readiness.rsi_slope_5). Extended/fade short → rsi_accel_max is the primary gate (check ta_readiness.rsi_accel_3). If neither fits, explain why."*

This asks for internal consistency rather than rule compliance, which is a more natural ask for a reasoning model and preserves judgment on atypical setups.

**Precondition:** Run one or two sessions with `last_block_reason` live first. If Claude starts self-correcting invalid conditions, the feedback loop is functioning and the prompt change is worth making. If not, the problem is deeper than salience.

**Do not:** Add escalating-skepticism signals to deferral counts ("you've been waiting N cycles, consider revising"). This creates pressure to lower entry standards mid-session, which is the opposite of what's needed. The 15-defer limit is the correct abandonment mechanism.

**First observed:** 2026-04-02 session log analysis

---

## Resolved Concerns

Resolved items are deleted after one session. See `DRIFT_LOG.md` for the permanent record of what was implemented and why.

---

## Engineering Analyses

### 2026-04-02 — Orchestrator God Object: Analysis and Disentanglement Path

`orchestrator.py` is 5,305 lines and 58 methods. It currently owns: startup/shutdown lifecycle, all three loop bodies, fill handling, entry execution, trigger evaluation, Claude cycle orchestration, market context assembly, watchlist lifecycle, regime management, position review application, degradation/broker failure state, and PDT management. CLAUDE.md deliberately encodes "only the orchestrator knows about all other modules" — that rule is doing real work and should be preserved. The question is whether everything currently living inside the class needs to be there to honour it.

**What makes this hard to split**

The loops share mutable state inline: `_filter_suppressed`, `_recommendation_outcomes`, `_entry_defer_counts`, `_all_indicators`, `_trigger_state`, `_degradation`. The fast loop writes fill state that the medium loop reads for suppression. The medium loop writes indicator state that the slow loop trigger check reads. This isn't accidental coupling — it's a consequence of the loops being designed to share a consistent world-view within a single asyncio event loop. Any extraction that doesn't account for this will introduce subtle ordering bugs.

**What can be extracted cleanly today**

Three clusters have low coupling to the shared mutable state and could move without risk:

1. **`TriggerEngine`** → `core/trigger_engine.py`  
   `SlowLoopTriggerState` + `_check_triggers` + `_update_trigger_prices`. Pure evaluation logic — reads state, returns a list, sets flags on a dataclass. Already unit-testable in isolation (the trigger tests prove this). Orchestrator holds the engine and calls `engine.check(now)`. This is the safest first extraction.

2. **`MarketContextBuilder`** → stays in `intelligence/` or `core/`  
   `_build_market_context` is ~150 lines of data assembly. No loop state is written by it. It takes account/PDT/indicators/session as inputs and returns a dict. Stateless and pure once extracted.

3. **`WatchlistLifecycle`** → `core/watchlist_manager.py`  
   `_apply_watchlist_changes`, `_prune_expired_catalysts`, `_clear_directional_suppression`, `_regime_reset_build`. These share cohesion around watchlist state and don't depend on fill/entry state. The orchestrator calls them with the watchlist object and gets the mutated result back.

**What should stay in orchestrator for now**

`_medium_try_entry` (~500 lines), the fill pipeline (`_dispatch_confirmed_fill`, `_register_opening_fill`, `_journal_closed_trade`), and `_fast_step_quant_overrides` all depend heavily on the shared mutable state and on each other's ordering. Extracting them now would mean threading `_filter_suppressed`, `_recommendation_outcomes`, `_entry_defer_counts` through every call — either as a god-context object (different shape, same problem) or as per-call arguments (verbose and fragile). The benefit doesn't justify the risk yet.

**The right long-term shape**

If this ever gets a full refactor, the correct pattern is a `SessionContext` dataclass that holds all cross-cutting mutable state and is passed by reference to extracted components. The orchestrator becomes a thin loop dispatcher. But this is a significant rewrite — the right time to do it is when a specific loop body needs to be independently tested or deployed, not before.

**Recommendation:** Extract `TriggerEngine` first (lowest risk, already tested, would clean up `_check_triggers` which is the most self-contained heavy method). See if that creates a good template for the others. Do not attempt `_medium_try_entry` or the fill pipeline without a clear motivation beyond cleanliness.

---

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
