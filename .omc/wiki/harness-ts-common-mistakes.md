---
title: Harness-TS Common Architectural Mistakes (Repeated Patterns)
description: Actual mistakes from past sessions + how to avoid. Self-referential learning page.
category: pattern
tags: ["harness-ts", "anti-patterns", "mistakes", "lessons-learned", "session-history"]
updated: 2026-04-27
---

# Harness-TS Common Architectural Mistakes (Repeated Patterns)

**Read this page when planning OR implementing OR reviewing harness-ts work.** Each entry is a real mistake from past sessions with the specific context, root cause, and prevention.

If you find yourself about to make one of these mistakes, STOP. Re-read the prevention rule. Almost always there's an additive path.

---

## M-1 — Fabricating event/type names despite verbatim allow-lists

**Sessions affected:** Phase E RALPLAN iter 2 + iter 4 (4 separate hallucinations).

**Specific fabrications observed:**
- `phase_started`, `phase_succeeded`, `phase_failed` — invented during D4 fixture table
- `architect_phase_start`, `architect_phase_end` — invented during D2 identity switch
- `review_arbitration_resolved` — invented during D2 identity switch
- `NotifierEvent` — wrong type name (actual: `OrchestratorEvent`)
- `ambiguous_scope` for EscalationType — wrong (valid: `scope_unclear`)
- `formatFindingForOps` placed at `src/lib/review.ts` — wrong path (no such file; placed at `src/lib/review-format.ts`)

**Root cause:** when planner produces dense per-row tables under "fix everything" pressure, fills cells with plausible-sounding names rather than skipping unmappable rows. Allow-list referenced but not copied verbatim.

**Prevention:**
1. ALWAYS read [[harness-ts-types-reference-source-of-truth]] FIRST when about to cite event types.
2. When generating tables, EMIT VERBATIM allow-list FIRST as the first deliverable, then derive-only-from. Structural pattern, not referential.
3. AC7 grep verification at audit time: `comm -23` of fixture event types vs checked-in `allowed-events.txt`.
4. Trust ONLY actual `grep`/`Read` against source code. Don't trust your own memory of "what feels right".

---

## M-2 — Wrong precondition assumption for state-machine helpers

**Session affected:** Wave E-α RALPLAN iter 2.

**Mistake:** `markPhaseSuccess` precondition was first specified as throwing if task NOT in "merging" state. But orchestrator's case "merged" was already calling `transition(task.id, "done")` BEFORE the helper invocation — so precondition would fire every time.

**Root cause:** plan written without checking ACTUAL orchestrator emit-site sequence. Assumed transition happened inside helper; actually was at call site.

**Prevention:**
1. Before specifying preconditions on state-machine helpers, READ the ACTUAL call site in source. `grep -A 10 "case \"merged\"" src/orchestrator.ts` shows current sequence.
2. State transition sequences live in [[harness-ts-types-reference-source-of-truth]] — TaskState transitions table.
3. Specify the helper's PROVENANCE: "called BEFORE the transition" vs "called AFTER the transition" vs "collapses transition+updateTask atomically".

---

## M-3 — Silent scope drop during plan revision

**Session affected:** Wave E-α RALPLAN iter 3 (Planner).

**Mistake:** iter 3 planner silently dropped `formatFindingForOps` helper from D0/D2 scope (was in iter 1+2). Discovered post-integration when writing common-mistakes audit. Required follow-up commit to restore.

**Root cause:** planner under multi-iteration pressure to address many feedback items; "scope simplification" interpreted as "drop ambiguous items" instead of "specify them better".

**Prevention:**
1. **Closure tables MANDATORY at iter 2+** — every revision lists prior requireds with resolution citations. Catches silent drops.
2. **Operator review at iter 3 boundary** — if scope SHRINKS vs iter 1, halt + clarify.
3. **Planner prompt directive**: "scope LOCKED — additions only, no removals; restore if drift detected".

---

## M-4 — Lossy iter-prompt summarization (orchestrator-side)

**Session affected:** All RALPLAN iter loops, especially Wave E-α and Phase E.

**Mistake:** parent agent (me) composed each iter prompt summarizing prior iter outputs. Planner/Architect/Critic don't share state — relied on my distillation. Lossy paraphrasing introduced new ambiguities each cycle (e.g. `NotifierEvent` vs `OrchestratorEvent`).

**Root cause:** RALPLAN sub-agent invocations carry no shared context. Each iter restarts from prompt. Parent's summary becomes the de-facto plan source-of-truth, but is paraphrased English not source code.

**Prevention:**
1. Use `SendMessage` to continue same agent across iter (preserves full context).
2. Write iter-N plan to disk as DRAFT BEFORE iter-N+1 review so subsequent agents read canonical source not paraphrase.
3. Embed verbatim source-of-truth (type unions, function signatures, file paths) in each iter prompt with citation `filename:line`.

---

## M-5 — Critic adversarial inflation without convergence threshold

**Session affected:** All RALPLAN iter loops.

**Mistake:** Critic finds ~5-7 NEW issues every pass regardless of plan quality. As plan got more detailed, critic noticed more interaction surface. No "good enough" threshold proportional to spec density.

**Root cause:** Critic's mandate is to find quality issues. With increasing spec detail, more interactions to scrutinize. No convergence gate.

**Prevention:**
1. **Critic severity-budget at iter 3+**: only CRITICAL severity blocks; MAJOR/MINOR list separately as follow-ups (auto-included in implementation, not blockers).
2. **Critic self-check at iter 3+**: "are NEW findings of similar severity to prior, or strictly less? If not strictly less, escalate to operator with halt+manual-integrate recommendation."
3. **Iter cap 3 not 5** — diminishing returns set in by iter 3. Operator-decision-point at iter-3 ITERATE: continue (rare) or halt+integrate (default).
4. See [[ralplan-procedure-failure-modes-and-recommended-mitigations]] for full postmortem.

---

## M-6 — Cross-deliverable O(N²) interaction surface (scope creep within wave)

**Session affected:** Wave E-α RALPLAN iter 4.

**Mistake:** even at narrow scope (5 deliverables), pairwise interactions ≈ 10. Each fix in one deliverable required spec adjustment in others. Iter 4 surfaced 4 interaction gaps that weren't visible until iter 3 changes propagated.

**Root cause:** O(N²) interaction surface not avoidable without exhaustive interaction matrix at iter 1.

**Prevention:**
1. **Limit single ralplan pass to ≤3 deliverables** for tractable interaction surface.
2. **Wave-split early**: anything >3 sub-phases gets explicit wave decomposition before consensus.
3. **Architect at iter 1 produces explicit cross-deliverable interaction matrix** as part of review.

---

## M-7 — Conflating phase concepts with event types

**Session affected:** Wave E-α RALPLAN iter 2 (Planner).

**Mistake:** D4 fixture table used `phase_started`, `phase_succeeded`, `phase_failed` event names. Planner conflated PhaseStore state-machine outcomes (`markPhaseDone`/`markPhaseFailed` at orchestrator.ts:796-798) with event-bus signals. NO such events exist — phases flow through `project_completed`/`project_failed` events.

**Root cause:** "phase" word appears in BOTH state machine (PhaseStore phases of a project) AND event names (project_completed marks phase completion). Conflation.

**Prevention:**
1. PHASES are STATE-MACHINE entities (PhaseStore). EVENTS are signals on the event bus.
2. Phase outcomes flow through `cascadePhaseOutcome(task, "done"|"failed")` which emits `project_completed`/`project_failed` events.
3. There are NO phase-named events. Verify via grep: `grep "type: \"phase" src/orchestrator.ts` returns empty.

---

## M-8 — Type-name drift across iterations

**Session affected:** Wave E-α RALPLAN iter 4 (Planner).

**Mistake:** plan body used `NotifierEvent` throughout. Actual type is `OrchestratorEvent` (orchestrator.ts:107). `NotifierEvent` doesn't exist.

**Root cause:** iter-prompt summarization drift — parent agent's summary used "Notifier..." prefix and that propagated into planner's context.

**Prevention:**
1. ALWAYS verify type names via `grep "^export type\|^export interface" src/`.
2. Type names live in [[harness-ts-types-reference-source-of-truth]] — authoritative copy.
3. When plan body cites a type, include `filename:line` citation. Reviewer can grep to verify.

---

## M-9 — Setup recipe knowledge not externalized

**Session affected:** Tier 1 A operator dialogue smoke test (2026-04-27).

**Mistake:** parent attempted to run `live-bot-listen.ts` without realizing it requires `config/harness/project.toml` (production-mode config, no inline default). Wasted ~5 min discovering. Other live scripts (`live-project.ts`) inline-build config; live-bot-listen does NOT.

**Root cause:** no documented "how to set up live harness for operator dialogue" recipe in wiki.

**Prevention:**
1. See [[harness-ts-live-setup]] for canonical recipe.
2. When script requires config that doesn't exist in repo, ALWAYS check wiki for setup recipe first.

---

## M-10 — Hooks ignored / silently misleading "Edit operation failed"

**Session affected:** Multiple sessions including 2026-04-27.

**Mistake:** `PostToolUse:Edit hook additional context: Edit operation failed.` system-reminder fires SOMETIMES even when actual edit succeeded. Parent agent reads "failed" as actual failure and re-attempts edit.

**Root cause:** hook signal is independent of tool success. False "failed" reminders are noise.

**Prevention:**
1. Trust the TOOL RESULT (which says "updated successfully") OVER the post-tool hook reminder.
2. Verify via `grep` or `Read` if uncertain.
3. NEVER re-edit in response to hook alone — only re-edit if tool result itself failed.

---

## M-11 — Wave E-α task_done compact-vs-structured ambiguity

**Session affected:** Wave E-α commit 1 review.

**Mistake:** task_done renderer in epistle-templates.ts had structured `lines[]` array but returned compact `truncateBody(`Task ${id} complete${lvl}`)` — dead code. Code-reviewer flagged as MED.

**Root cause:** initial implementation built both forms but didn't decide which to return; left dead structured-form code with comment "structured form preferred but compact form preserved for backward compat".

**Prevention:**
1. When two render forms exist, BRANCH ON DATA PRESENCE explicitly (`if (event.summary || event.filesChanged?.length) { return structured } else { return compact }`).
2. Don't leave dead code as "future intent" — either branch on data OR remove unused branch.

---

## M-12 — Caveman mode hides orchestrator self-check signals

**Session affected:** Wave E-α post-RALPLAN (2026-04-26).

**Mistake:** parent agent (me) operated in caveman mode (terse) during multi-agent orchestration. Compressed summaries to user hid silent scope drops (M-3) and didn't catch own type-name drift (M-8).

**Root cause:** caveman mode applies to user-facing text. For routine code work, terse is fine. For long-running multi-agent orchestration where meta-reasoning quality matters, terse format reduces self-checking surface.

**Prevention:**
1. **Caveman mode is fine for routine code work.** Disable for long-running multi-agent orchestration tasks where parent's meta-reasoning matters.
2. Self-check before accepting iter outputs: did scope shrink? Did types match what's in repo?

---

## Cross-refs

- [[harness-ts-types-reference-source-of-truth]] — verbatim type signatures
- [[harness-ts-core-invariants]] — load-bearing invariants
- [[ralplan-procedure-failure-modes-and-recommended-mitigations]] — RALPLAN consensus failure modes (M-3, M-4, M-5, M-6 detail)
- [[harness-ts-live-setup]] — operational setup recipes
