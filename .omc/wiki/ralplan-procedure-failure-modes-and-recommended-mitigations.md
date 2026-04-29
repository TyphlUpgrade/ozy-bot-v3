---
title: "RALPLAN Procedure — Failure Modes and Recommended Mitigations"
tags: ["ralplan", "consensus", "planning", "process", "anti-pattern", "lessons-learned"]
created: 2026-04-27T03:58:37.037Z
updated: 2026-04-27T03:58:37.037Z
sources: []
links: ["phase-e-agent-perspective-discord-rendering-intended-features.md", "harness-ts-architecture.md"]
category: pattern
confidence: medium
schemaVersion: 1
---

# RALPLAN Procedure — Failure Modes and Recommended Mitigations

# RALPLAN Procedure — Failure Modes and Recommended Mitigations

**Captured:** 2026-04-26 from operator-driven RALPLAN consensus runs on Phase E (Discord agent-perspective rendering) and Wave E-α (deterministic identity + templates).

## Context

RALPLAN-DR is a consensus planning protocol: Planner → Architect → Critic with iterative re-review until APPROVE (max 5 iter per skill). Designed to produce verified plans before implementation.

Two consecutive RALPLAN sessions exhibited convergence failure:
1. **Phase E (full scope, 8 sub-phases)** — 4 iter without APPROVE; iter 4 planner over-corrected by silently dropping 5 sub-phases (scope collapse). Operator halted, wave-split, restarted.
2. **Wave E-α (narrow scope, 5 deliverables)** — also 4 iter without APPROVE; halted with 33 accumulated requireds across iter. Manual integration needed.

Both sessions burned ~$1.50-2 + 30 min orchestration time each. Manual integration of accumulated requireds took 5-10 min. Net inefficiency: ~25-30 min wasted vs operator-cancel at iter 3.

## Five Specific Failure Modes Observed

### 1. Lossy iter-prompt summarization

**Pattern:** Each iter's prompt is composed by the orchestrator (operator's main agent) summarizing prior iter outputs. Planner/Architect/Critic do NOT share state — they rely on the orchestrator's distillation.

**Failure:** Type names drifted across iter (`NotifierEvent` vs actual `OrchestratorEvent`). Decision rationale lost. Each iter accumulated paraphrasing errors that compounded.

**Mitigation:**
- Use SendMessage to continue same agent across iter (preserves full context naturally).
- Write iter-N plan to disk as DRAFT before iter-N+1 review so subsequent agents read canonical source not paraphrase.
- Embed verbatim source-of-truth (type unions, function signatures, file paths) in each iter prompt, with citation `filename:line`.

### 2. Planner fabrication despite explicit allow-lists

**Pattern:** Iter 2 + iter 4 planners hallucinated event names (`phase_started`/`phase_succeeded`/`phase_failed` in iter 2; `review_arbitration_resolved`/`architect_phase_start`/`architect_phase_end` in iter 4) DESPITE explicit verbatim 27-event allow-list embedded in iter-3 + iter-4 prompts.

**Failure:** LLM attention/recall problem — when asked to produce dense per-row tables, model fills cells with plausible-sounding names rather than skipping unmappable rows. Allow-list was *referenced* but planner regenerated from "what feels right".

**Mitigation:**
- Force planner to emit allow-list verbatim FIRST as the first deliverable, then derive-only-from. Structural rather than referential pattern.
- Architect+Critic must verify against allow-list at every iter — they catch fabrication when planner doesn't.
- Add automated `comm` / `grep` allow-list verification step in plan acceptance criteria (AC7 pattern from Wave E-α plan).

### 3. Critic adversarial mode without convergence threshold

**Pattern:** Critic finds ~5-7 NEW issues every pass regardless of plan quality. Iter 1 found gaps in v1; iter 2 found different gaps in v2; iter 3 found new gaps in v3. Most were NOT regressions of prior fixes — they were genuinely new abstractions critic noticed as plan got more detailed.

**Failure:** As plan grew more detailed, critic noticed more interaction surface. No "good enough" threshold proportional to spec density. Recursive scrutiny without convergence gate.

**Mitigation:**
- Critic should have explicit severity-budget at iter 3+: only CRITICAL severity blocks; MAJOR/MINOR list separately as follow-up to be auto-included in implementation.
- At iter 3+, critic must self-check: "are NEW findings of similar severity to prior, or strictly less?" If not strictly less, escalate to operator with "halt + manual integrate" recommendation.

### 4. Cross-deliverable O(N²) interaction surface

**Pattern:** Even at narrow scope (Wave E-α had 5 deliverables), pairwise interactions = ~10. Each fix in one deliverable required spec adjustment in others (e.g. markPhaseSuccess emit shape change → D4 fixture asserts new fields → AC1 audit must verify them → renderEpistle template must render them).

**Failure:** Iter 4 surfaced 4 interaction gaps that weren't visible until iter 3 changes propagated. Not avoidable without exhaustive interaction matrix at iter 1 — but matrix at iter 1 is 5 deliverables × ~5 design decisions each = 25 decisions, exceeding consensus pass cognitive budget.

**Mitigation:**
- Limit single ralplan pass to ≤3 deliverables for tractable interaction surface.
- Wave-split early: anything >3 sub-phases gets explicit wave decomposition before consensus.
- Architect at iter 1 must produce explicit cross-deliverable interaction matrix as part of review.

### 5. Planner over-correction → scope collapse

**Pattern:** Iter 4 planner of Phase E (full) narrowed scope to JUST LLM addendum (Phase C only) — silently dropped E.2/E.3/E.5/E.6/E.8 entirely. Triggered by iter-3 critic feedback "too many gaps". Planner interpreted as "scope too big" rather than "spec details missing".

**Failure:** Planner has no "do not regress prior scope" memory. Silent scope drops happen when planner is under pressure to address many issues.

**Mitigation:**
- Mandatory closure tables at iter 2+ — every revision lists prior requireds with resolution citations. Catches silent drops + scope regressions.
- Operator review at iter 3 boundary — if scope shrinks vs iter 1, halt + clarify.
- Planner prompt must explicitly state "scope LOCKED — additions only, no removals; restore if drift detected".

## What RALPLAN-DR Does Well

- Architecture invariants LOCKED as Principle 1 — held verbatim across all iter consistently
- Verbatim allow-list mostly held when explicitly embedded (events) when planner cooperated
- Pin sweep + field-source citation enforced verifiability
- 2-commit atomic split kept rollback granular
- Architect APPROVED earlier than Critic (architect satisfied at iter 4; critic still ITERATEd) — multi-perspective review catches different defect classes

## Where Wave-Split Helped vs Didn't

**Solved by wave-split:**
- Stopped scope-collapse panic (planner won't drop sub-phases when only 5 deliverables exist)
- Bounded interaction surface from O(8²)=64 to O(5²)=25
- Made critic feedback specific to one wave's deliverables

**NOT solved by wave-split:**
- Planner fabrication
- Critic convergence threshold
- Lossy iter-prompt summarization
- Cross-deliverable interactions still O(N²) within wave

## Operator Decision Tree for Future RALPLAN Runs

```
Pre-flight: scope > 3 sub-phases?
  YES → wave-split first; ralplan one wave at a time
  NO → proceed

Iter 1: Architect+Critic verdicts?
  Both APPROVE → write plan, proceed to ralph
  ITERATE: count required changes
    < 5 → revise + iter 2
    5-10 → revise + iter 2 with explicit "no scope changes" directive
    > 10 → operator review; consider halt + manual integrate

Iter 2: same as iter 1
  Additional check: did planner silently drop any iter-1 requireds? If yes, halt.

Iter 3: critic ITERATE with new findings?
  Findings strictly less severe than iter 2 → revise + iter 4
  Findings similar severity → operator review; default halt + manual integrate
  Findings more severe → operator review; possible REJECT + restart

Iter 4: penultimate iter
  Skip iter 5 if expected convergence < 80%; manual integrate now
  Manual integrate cost: ~5-10 min vs iter 5 cost: ~10-15 min + likely also ITERATE

Iter 5: skill auto-stops with "best version"
  Always requires manual integration anyway
  Net wasted iter: 4-5 vs iter-3 cancel
```

## Caveman Mode and Orchestration

Caveman mode (terse style) does NOT directly affect agent tool prompts (those use full English). But terse summarization in operator-facing orchestration commentary creates risk:
- Compressed summaries hide nuance like silent scope drops
- Self-checks reduced surface — orchestrator may miss own errors
- Lead engineer postmortems compress important caveats

**Recommendation:** Disable caveman for long-running multi-agent orchestration tasks where orchestrator's meta-reasoning quality matters. Keep enabled for routine code work.

## Cost Reference

- Phase E full ralplan (4 iter, halted): ~$1.80, 35 min
- Wave E-α ralplan (4 iter, halted): ~$1.50, 30 min
- Manual integration each: ~5-10 min, $0.10
- Net per session: ~25-30 min wasted vs operator-cancel at iter 3

## Cross-refs

- [[phase-e-agent-perspective-discord-rendering-intended-features]] — Phase E intended features
- [[harness-ts-architecture]] — harness-ts wave history
- `.omc/plans/2026-04-26-discord-wave-e-alpha.md` — Wave E-α plan written via manual integration after iter-4 halt
- `.omc/plans/2026-04-26-discord-conversational-output.md` — Phase A+B plan (succeeded in 3 iter — within tractable scope)

