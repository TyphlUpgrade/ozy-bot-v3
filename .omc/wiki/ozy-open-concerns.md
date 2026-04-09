---
title: Ozymandias Open Concerns
tags: [ozymandias, concerns, trading-bot, engineering]
category: debugging
created: 2026-04-09
updated: 2026-04-09
---

# Ozymandias Open Concerns

Active engineering concerns for the Ozymandias trading bot. For harness/pipeline concerns see [[v5-harness-open-concerns]].

**Status labels:** `open` · `deferred` · `resolved` · `won't fix`

---

### CONCERN-2: Entry conditions path bypasses composite score floor
**Status:** open
**Severity:** Low (inconsistency, not a bug)
**First observed:** 2026-03-25 session log (FINDING-11)
**Area:** `intelligence/opportunity_ranker.py`, `core/orchestrator.py`

A candidate in `new_opportunities` with `entry_conditions` set is evaluated by `_medium_try_entry` using Claude's conviction as a score proxy (no composite floor check). A freshly-evaluated candidate in the normal ranker path must clear `min_composite_score` (default 0.30). PFE demonstrated this: it scored 0.56-0.58 via the conviction path while scoring 0.00 via the fresh ranker path after being pruned from Claude's output.

The inconsistency allows stale thesis candidates to compete for entries indefinitely as long as they remain in `new_opportunities`, regardless of live TA deterioration. The correct fix is to apply the composite floor check before entry even when entry_conditions are present.

---

### CONCERN-3: Slope/accel indicators underutilised in entry conditions
**Status:** open
**Severity:** Medium (suboptimal entry gates, not a correctness bug)
**First observed:** 2026-04-02 session log analysis
**Area:** `config/prompts/`, `intelligence/claude_reasoning.py`

`rsi_slope_5` and `rsi_accel_3` are computed, not in `_TA_EXCLUDED`, and visible to Claude in `ta_readiness`. The prompt mandates slope conditions for momentum entries. In practice Claude ignores this for swing entries and inconsistently for momentum — defaulting to simpler gates (`rsi_max`, `require_below_vwap`).

The FISV `rsi_slope_max=0.5` case is the clearest symptom: Claude used the slope condition but got the sign wrong, and the feedback loop gave no signal the condition was structurally invalid. The `last_block_reason` fix addresses the invalid-value case.

**Remaining gap:** Prompt design. Tie condition selection to setup type rather than mandating specific fields categorically. See NOTES.md CONCERN-3 for full analysis and proposed prompt wording.

**Precondition:** Run sessions with `last_block_reason` live first to see if Claude self-corrects.

---

### CONCERN-4: reasoning.txt context-unaware — sends full content regardless of session state
**Status:** open
**Severity:** Medium (wasted tokens, unfocused context)
**First observed:** 2026-04-06 reasoning.txt audit
**Area:** `intelligence/claude_reasoning.py`, `config/prompts/`

reasoning.txt is always sent in full (~30KB, ~187 lines). Several sections are irrelevant depending on session state: position review instructions when no open positions (~15 lines), swing daily signals block when no swing positions (~7 lines), full entry_conditions reference (~27 lines, ~580 tokens) when watchlist_tier1 is empty.

**Fix:** Conditional prompt assembly in `_build_reasoning_prompt()`. Already partially done via `{position_review_notice}` — extend to conditionally include/exclude sections based on `open_positions > 0`, `swing_positions > 0`, `len(watchlist_tier1) > 0`. ~30-40 lines in claude_reasoning.py.

---

### CONCERN-5: Prompt versioning scheme copies entire directory on each bump
**Status:** open
**Severity:** Low (maintenance overhead, no runtime cost)
**First observed:** 2026-04-06 reasoning.txt audit
**Area:** `config/prompts/`

Every prompt version bump copies all 8 files in `config/prompts/`. Between v3.10.1 and v3.10.3, reasoning.txt changed 3 times; the other 7 files are identical across all three versions.

**Fix:** Only version reasoning.txt. Move the 7 stable prompts to `config/prompts/` root (unversioned). Version bumps then touch only the file that changed.

**Precondition:** Do not do this mid-session when other changes are in flight.

---

## Cross-References

- [[ozy-drift-log]] — Spec deviations (Spec/Impl/Why)
- [[ozy-analyses]] — Engineering analyses
- [[v5-harness-open-concerns]] — Harness/pipeline concerns (separate namespace)
- [[ozy-doc-index]] — Full routing table
