---
title: Ozymandias Engineering Analyses
tags: [ozymandias, analyses, trading-bot, engineering, archive]
category: reference
ceiling_override: frozen-archive
frozen: true
created: 2026-04-09
updated: 2026-04-09
---

# Ozymandias Engineering Analyses

Frozen archive of engineering analyses for the Ozymandias trading bot. These are completed historical analyses — not actively updated.

For open concerns see [[ozy-open-concerns]]. For the active drift log see [[ozy-drift-log]].

**Note:** The "Agentic Development Workflow Design" analysis is harness infrastructure, not Ozy trading. It remains in NOTES.md and is not migrated here.

---

### 2026-04-06 — Orchestrator Extraction (completed)

**Result:** orchestrator.py reduced from 5393 → 3605 lines (-33%) across two phases. 7 modules
extracted into `core/`. Reconciliation extraction intentionally skipped (too many scalar writes).
`_medium_try_entry` and `_run_claude_cycle` remain in orchestrator — too coupled to shared mutable
state. See COMPLETED_PHASES.md for full implementation details and `plans/2026-04-06-orchestrator-extraction-phase*.md` for the approved designs.

**Parallel work zones now available:**

| Zone | Files | Safe for parallel agents? |
|------|-------|-----------------------------|
| TA indicators | `technical_analysis.py` | Yes |
| Strategies | `strategies/*.py` | Yes |
| Trigger logic | `core/trigger_engine.py` | Yes |
| Context building | `core/market_context.py` | Yes |
| Watchlist mgmt | `core/watchlist_manager.py` | Yes |
| Fill handling | `core/fill_handler.py` | Yes |
| Position reviews | `core/position_manager.py` | Yes |
| Quant overrides | `core/quant_overrides.py` | Yes |
| Position sync | `core/position_sync.py` | Yes |
| Risk manager | `execution/risk_manager.py` | Yes |
| Broker | `execution/alpaca_broker.py` | Yes |
| Claude prompts | `config/prompts/` | **Serialize** |
| Orchestrator core | `core/orchestrator.py` | **Serialize** |

---

---

### 2026-04-06 — Trade Journal Performance Audit (68 trades, 2026-03-19 to 2026-04-06)

**Overall:** 68 completed trades, 42.6% win rate, +$576.76 / +9.80% total P&L. System is net profitable because average wins (+2.12%) are 1.52x average losses (-1.39%). Best trade: SLB +9.33% (swing/long). Worst: MKC -9.55% (swing/long, stop hit).

**Finding 1: Shorts are a significant drag.**
9 short trades, 11.1% win rate, -7.75% total P&L. Only one winner (UHS +0.69%). Swing/short is 1/7 (14%), momentum/short is 0/2. The system has no demonstrated edge on the short side. Claude is already citing "0% short win rate" to reject shorts in real-time, but continues proposing them each session.

**Finding 2: The edge is entirely in multi-day swing longs.**
Trades held 1-3 days: 67% win rate, +14.51%. Trades held 3+ days: 78% win rate, +23.09%. Everything under 24 hours is net negative. Momentum has 5 trades at 20% win rate (-0.80%). The system makes all its money when it holds swing longs for days and loses it on short-duration entries.

**Finding 3: Profit targets are nearly irrelevant.**
Only 2 of 68 trades (2.9%) hit their profit target. 73.5% of exits are Claude "strategy" exits (+18.22% total from those). Targets may be too ambitious. The 2 target hits produced +12.64% — huge when they land, but 66 other trades never reached them.

**Finding 4: Stop losses are the biggest P&L destroyer.**
10 stop exits totaled -20.57%. MKC lost 9.55% (stop at $51 was 4.8% below entry for a low-vol consumer staples stock — too wide). LNG lost 3.69% in 45 minutes. XOM lost 3.16% in 130 minutes. Some stops are calibrated for swing hold duration but applied to positions that should have been cut faster.

**Finding 5: 13:00 ET hour is toxic.**
7 trades entered at 13:00 ET: 14% win rate, -17.14% total P&L. This single hour accounts for nearly all gross losses. Early morning (09:00-10:00) also underperforms. Late afternoon (14:00-15:00) is the strongest window at 60%+ win rate and +20.53% combined.

**Finding 6: Ultra-short holds indicate entry quality problems.**
15 trades held under 10 minutes, 27% win rate. These are positions entered and immediately reversed — false entries where Claude or quant override killed the position before the thesis had time to play out.

**Finding 7: Prompt v3.10.1 is underperforming — but the v3.6.0 baseline is inflated.**
42 trades at 38% win rate and -8.26% total P&L. v3.6.0 was 8 trades at 88% win rate and +25.88% — but 96% of v3.6.0's dollar profit came from 4 energy swing longs (HAL, SLB, XLE, CVX) riding a single sector rally over 5-6 days. That's one good macro call, not a structurally superior prompt. Strip the energy cluster and system total P&L drops from +9.80% to roughly +$450 across 60 trades. v3.10.1's underperformance is real (net negative over 42 trades) but the comparison benchmark needs this asterisk.

---

### 2026-04-06 — Session Log Analysis (6 sessions, full trading day)

**Day result:** equity $30,056.46 → $30,020.12 (-$36.34, -0.12%). 3 completed trades (0 wins). 1 position held overnight (WBD long 121 shares @ $27.38, merger arb thesis).

**Completed trades — all losses, all thesis breach exits:**

| Symbol | Dir | Entry | PnL | Duration | Exit Reason |
|--------|-----|-------|-----|----------|-------------|
| NKE | short | $43.64 | -0.71% | 59 min | Zacks earnings upside surprise |
| ALB | short | $172.24 | -0.10% | 3 min | US supply chain initiative headline |
| TKO | long | $199.66 | -0.48% | 1 min | Daily downtrend deterioration |

**Claude API usage:** 15 Tier-1 reasoning calls (~240K input tokens, ~66K output), 10 position reviews (~13K input, ~1.9K output). All 15 Tier-1 calls exceeded the 60s warning threshold (range 64.8s–111.1s). Cache token logging shows 0 cache_read / 0 cache_create — the prompt restructuring from this session has not yet been deployed to a running bot instance.

**Dead zone behavior:** WBD blocked by dead zone ~20 times across sessions 3-5. Dead zone bypassed once at 12:55 ET when SPY RVOL hit 1.97 (≥ 1.50 threshold). After bypass, WBD entered and filled immediately. The bypass is working as designed. Note: swing entries were still being blocked because `SwingStrategy.dead_zone_exempt` was incorrectly returning `False` (fixed this session — restored to `True`).

**Claude calibration errors observed:**
- ALB: `rsi_slope_max=0.5` for a short (positive value, must be negative) — blocked 7+ times
- CTVA: `rsi_max=35` vs RSI 47-48 — entry impossible without massive RSI crash
- IVZ: `rsi_max=30` vs RSI 44-49 — same calibration error
- These are the same class of error documented in CONCERN-3

**Post-market order churn:** WING placed/cancelled 7 times (300s timeout each), L placed/cancelled 5 times. Extended hours with thin liquidity — bot kept trying stale limit prices that never filled. No mechanism to detect "this price isn't going to fill in extended hours" and stop trying.

**yfinance mass failure at 20:25Z:** 50+ symbols returned NoneType. TA cycle spiked to 52.3s (normally 2-3s). Auto-recovered in ~2 minutes. Expected behavior after market close.

**RVOL filter drift through the day:** min_rvol started at 1.2 (Claude raised it citing 0% short win rate), drifted to 0.7-0.8 by afternoon as most symbols fell below threshold. Claude is adjusting this filter reactively each call but the adjustments are not persistent — each new reasoning call re-evaluates from scratch, causing oscillation.

**Regime assessment:** Spent entire day in "sector rotation" (confidence 0.58-0.72) with one brief "risk-off panic" from cache at 17:46Z, then settled to "normal" by close.

---

---

### 2026-04-02 — Orchestrator God Object: Analysis and Disentanglement Path *(superseded by 2026-04-06 extraction)*

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

---

## Cross-References

- [[ozy-open-concerns]] — Active engineering concerns
- [[ozy-drift-log]] — Active drift log
- [[ozy-completed-phases]] — Phase narratives
- [[ozy-doc-index]] — Full routing table
