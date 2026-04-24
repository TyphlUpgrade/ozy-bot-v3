# Three-Tier Architect Integration Plan (ralplan Phase 2B-3 + Deep-Interview Merge)

**Date:** 2026-04-23
**Status:** PLANNER REVISION 2 (DELIBERATE mode) — addressing Architect APPROVE_WITH_CONDITIONS + Critic ITERATE
**Depends:** Phase 2A (COMPLETE, 280 tests), Phase 1.5 (`resumeSession` verified)
**Supersedes in part:** `.omc/plans/ralplan-harness-ts-phase2b-3.md` (APPROVED 2026-04-12)
**Driven by:** `.omc/specs/deep-interview-harness-three-tier.md` (PASSED, 19% ambiguity, 2026-04-23)
**Mode:** DELIBERATE (promoted from SHORT per Architect directive — high-risk schema + orchestration change)

This plan integrates the three-tier Architect-Executor-Reviewer design (locked by deep-interview) into the already-approved Phase 2B-3 ralplan. The spec decisions are locked; this document reconciles them against the existing Phase 2B-3 plan, resolves integration ambiguities inline, and provides per-wave breakdowns for execution.

**Revision 2 changes (vs Revision 1):** All 4 BLOCKING open questions resolved inline (Sections A.1, A.3, C.1, C.4). Routing precedence documented as an explicit doc-comment block (Section C.2). `lib/arbitration.ts` layering fixed — returns discriminated union, orchestrator applies side effects (Wave C). Promoted to DELIBERATE mode with pre-mortem (Section H) and expanded test plan (Section I). Rollback / feature flag added (Section J). Test count corrected 273 → 280 throughout. Wave D sample size 20 → 50. Compaction summary schema specified (Section C.4 + Wave D). Observability surface (Section K, `!project <id> status`). New event variants carry `projectId`. `ArchitectManager` richer return types. Supersedes note written to phase2b-3 plan.

---

## A. Spec Audit Findings (resolutions inline)

For each locked decision in `deep-interview-harness-three-tier.md`, the Planner flags unstated assumptions, consistency issues, testability gaps, and non-goal leakage risk. **This revision resolves the four BLOCKING open questions inline**; remaining INFORMATIONAL items are listed in Section G.

### A.1 Locked decision: "Architect is a persistent OMC SDK session scoped to one project"

**Unstated assumptions (now resolved where possible):**
- SDK session persistence is *cheap enough* to hold open across N Executor spawns (potentially hours). Validated in Wave 1.75 check #8 (long-lived resume).
- `persistSession: true` on the Architect's session preserves tool state across spawns of *other* sessions (Executors). The SDK persistence model is session-scoped — other sessions in the same project don't perturb it — but Wave 1.75 check #8 exercises this in harness-ts.
- **RESOLVED — Architect `cwd` / worktree location.** The Architect runs in a dedicated worktree at `worktree_base/architect-{projectId}/` on branch `harness/architect-{projectId}`. Rationale: (a) symmetry with Executor worktrees, (b) gives Architect a git-isolated surface for reading prior phase diffs via `git log` / `git show` without contaminating main-repo working tree, (c) tmux cleanup pattern already matches `architect-{projectId}`. Lifecycle: created on `projectStore.createProject()` (Wave B), destroyed on `completeProject()` / `failProject()` (Wave B + rollback path). Worktree cleanup on project failure is called out explicitly in Section K / Wave B acceptance.

**Internal consistency issues (now resolved):**
- `NOT implementing ephemeral Architect-for-standalone-tasks in v1` vs. `settingSources: ["project"]` + `enabledPlugins` — Architect inherits the same `persistent-mode.cjs` hook risk as Executor. Wave B explicitly requires `hooks: {}` defense from Phase 2B-3 Item 2 for Architect sessions (added to Wave B acceptance and Wave 1.75 check #8).
- `Architect arbitration verdicts reduce to three values` + `Architect cannot override Reviewer` — **RESOLVED in Section C.3 (arbitration cap behavior)**: after `max_tier1_escalations` arbitration attempts on a single phase, escalate to operator; no third arbitration retry.
- Reviewer session budget is capped per-review at `reviewer.max_budget_usd`; all Reviewer spend rolls up into `project.totalCostUsd` (Section C.4).

**Testability gaps in acceptance criteria (now resolved):**
- **RESOLVED — Tier-1 resolution rate threshold X.** Locked to `X=60%` as the Wave D **acceptance gate** (not just a target). Wave D fails if rate < 60% over a 50-event sample (see Section I.2).
- **RESOLVED — "Cost per project under ceiling"** — enforced as live orchestrator-level precheck before every session spawn (Section C.4). Ceiling default `budgetCeilingUsd = 10 * pipeline.max_budget_usd`.
- **RESOLVED — "No infinite loops"** — translated to per-state bounded-iteration property tests (`tests/lib/state-bounded.test.ts`, Wave D) with disagreement-counter increment+cap.
- **RESOLVED — Compaction mechanism.** Orchestrator-driven abort-and-respawn with summary. Schema specified in Section C.4 and enforced via fidelity test in Wave D (Section I).

**Non-goals that may leak back in during implementation:**
- "NOT implementing plan-citation recusal in v1" — guardrail: explicit comment in `src/lib/arbitration.ts` saying *do not add recusal logic without measurable bias data from post-Wave D calibration*.
- "NOT auto-promoting standalone tasks to projects on escalation" — guardrail: project declaration requires an explicit discord `!project` command (or task-file `projectId` field); never auto-created by orchestrator.
- "NOT adding a new Wiki/Classify stage" — clarified: *no new stage in the merge path*. Decomposition is pre-pipeline.

### A.2 Locked decision: "Retry-only authority (cannot override Reviewer)"

**Unstated assumptions (resolved):**
- Operator override mechanism: deferred to post-v1. In v1, operator can manually merge via git if urgent; this is the sole override path. Documented as a known limitation (Wave A acceptance criteria + Section G informational Q10).
- `retry_with_directive` resumes via `resume: sessionId` — when the Executor has already written `completion.json`, the directive must explicitly tell it to revise its commit. Architect systemPrompt contract covers this (Section C.4 + architect-prompt.md draft).

**Testability:** arbitration-verdict parser rejects any fourth verdict type (e.g., `executor_correct`) as malformed. Arbitration schema tests catch this (Wave C).

### A.3 Locked decision: "Disagreement threshold 2, tier-1 cap 2"

**RESOLVED — `tier1EscalationCount` scope.** Two-level counter model:

1. **Per-task counter (unchanged from Phase 2A):** `TaskRecord.tier1EscalationCount` tracks failure-retry escalations per phase. Cap `max_tier1_escalations` (default 2) remains per-phase. Preserves Phase 2A behavior byte-identically for standalone tasks.
2. **Per-project aggregate (NEW):** `ProjectRecord.totalTier1EscalationCount` tracks total tier-1 escalations across ALL phases of a project. Cap `project.max_total_tier1_escalations` (default `5 * max_tier1_escalations` = 10 with defaults).

**Escalation trigger rule (Section C.3):**
- Either cap exceeded → escalate to operator immediately. Whichever trips first wins.
- Per-task counter increments on each arbitration attempt on THAT phase.
- Per-project aggregate increments on every arbitration attempt across any phase.

**Mitigates pre-mortem scenario (b):** 10 total arbitration attempts across a project give each phase headroom, while bounding operator-notification cost globally.

### A.4 Locked decision: "Executor sessions run with `persistSession: true`"

- Architect's own session: `persistSession: true` (for resumption across Executor spawns).
- Reviewer's session: `persistSession: false` (ephemeral — no cross-review state). Explicitly encoded in `review.ts`; regression test in Wave A.
- Executor session: `persistSession: true` (already fixed Phase 2A Wave 5; Phase 2B-3 Item 1 re-fixes `manager.ts:208`).

### A.5 Locked decision: "Standalone tasks bypass Architect entirely"

**Unstated assumptions (resolved):** `TaskRecord.projectId` field added in Wave 1.5b, participates in `KNOWN_KEYS` B7 contract. Standalone path = `task.projectId === undefined`.

**Consistency (see Section C.2 routing table):** `projectId + mode:"dialogue"` combination is rejected at ingest with `ValidationError`.

**Non-goal leakage guardrail:** `src/session/dialogue.ts` throws on `task.projectId` — project dialogue must route to Architect via `ArchitectManager.relayOperatorInput()` (Wave 6-split).

### A.6 Locked decision: "Additive only — 280 Phase 2A tests must stay green"

**Principle #1 relabeled:** "Additive-where-possible, extending-with-replace-for-review-and-dialogue". Wave A extends/replaces Phase 2B-3 Wave 5's ReviewGate trigger logic; Wave 6-split replaces Wave 6 with dual-path dialogue. Standalone-task behavior remains additively preserved.

**Tests** — three-tier adds `review_arbitration` state. Wave 1.5b extends `VALID_TRANSITIONS`. Phase 2A state-transition tests (29 in `state.test.ts`) must still pass unchanged — they don't test `review_arbitration` today, so additivity holds for those. New three-tier tests live in separate files (`tests/lib/state-arbitration.test.ts`, `tests/session/architect.test.ts`) to avoid perturbing existing test setups.

### A.7 Locked decision: "Architect systemPrompt instructs use of OMC architect/planner agents and /team for internal decomposition"

**Consistency:** `/team` spawns tmux panes. Phase 2B-3 Item 4 pattern-match expanded to include `architect-{projectId}` and `project-{projectId}` patterns (Wave 1 extension) plus `cleanupProject(projectId)` sweeper. Called on `completeProject` / `failProject` / operator `!project <id> abort`.

**Testability:** unit tests verify systemPrompt CONTAINS key directives; live validation in Wave D.

---

## B. Integration Map Against Approved Phase 2B-3 Ralplan

Abbreviations: KEEP-AS-IS, EXTEND, SUBSUME, SPLIT, DEFER.

| # | Item | Wave | Classification | Delta under three-tier |
|---|------|------|----------------|------------------------|
| 1 | OMC Plugin Loading via `Options.settings` | 1 | KEEP-AS-IS | Architect sessions also need these plugins; consumes the same `enabledPlugins` config. Reviewer needs them too. |
| 2 | Persistent-Mode Hook Defense (`hooks: {}`) | 1 | KEEP-AS-IS | Applies uniformly to Executor, Reviewer, AND Architect. No change. |
| 3 | Block Cron/Remote Triggers via `disallowedTools` | 1 | KEEP-AS-IS | Applies to Architect too — Architect must not escape its own lifecycle. |
| 4 | Tmux Cleanup in Worktree Lifecycle | 1 | **EXTEND** | Pattern match expanded from `task-{id}` / `harness-` to include `architect-{projectId}` and `project-{projectId}`. Add `cleanupProject(projectId)` sweeper. Also cleans Architect worktree. |
| 1.5a | `processTask()` decomposition | 1.5 | **EXTEND** | Phase 2B-3's 4 extracted methods plus one new: `routeByProject`. See Section C.2 routing precedence. |
| 1.5b | State Schema Updates for Dialogue + Review | 1.5 | **EXTEND** | Adds `projectId?`, `phaseId?`, `arbitrationCount?`, `reviewerRejectionCount?` on TaskRecord. Adds `review_arbitration` state. Adds `totalTier1EscalationCount` on ProjectRecord. |
| 1.75 | Pipeline Smoke Test | 1.75 | **EXTEND** | +1 long-lived resume check (#8), +1 concurrent-session check (#9) per Critic item 17. |
| 5 | Discord Notifier | 2 | **EXTEND** | New event types now carry `projectId` (Critic item 10): `project_declared`, `project_decomposed`, `architect_spawned`, `architect_arbitration_fired`, `arbitration_verdict { projectId }`, `review_arbitration_entered { projectId }`, `review_mandatory`, plus `project_completed`, `project_failed`, `project_status_requested`, `project_aborted`. |
| 6 | Webhook Per-Agent Identity | 2 | **EXTEND** | Add `architect` and `reviewer` agent identities. |
| 7 | Message Accumulator | 3 | KEEP-AS-IS | No change. |
| 8 | Operator Task Submission | 3 | **EXTEND** | New commands: `!project <name> <description>`, `!project <id> status`, `!project <id> abort`. `!status` returns project list when no taskId given. |
| 9 | Reaction Acknowledgments | 3 | KEEP-AS-IS | No change. |
| 10 | Escalation Response | 4 | **SPLIT** | Standalone KEEP-AS-IS; project path → Architect (tier-1 resolver). |
| 11 | Escalation Dialogue (Multi-Turn) | 4 | **SPLIT** | Standalone KEEP-AS-IS; project never enters multi-turn operator dialogue (Architect resolves silently). |
| 12 | External Review Gate | 5 | **EXTEND** | `ReviewGate` class unchanged in core; triggering conditional (project phase → mandatory). |
| 13 | Review Trigger Logic + Hard Gating | 5 | **EXTEND** | Trigger gains `task.projectId !== undefined → ALWAYS review`. On reject + `reviewerRejectionCount >= 2` → `review_arbitration`. |
| 14 | Dialogue Agent (proposal.json) | 6 | **SPLIT** | Standalone KEEP-AS-IS; Architect subsumes project decomposition. |
| 15 | Dialogue Discord Channel | 6 | **SPLIT** | Dual-dispatch: Architect session for projects, single-session dialogue for standalone. |

### Summary

- **KEEP-AS-IS:** Items 1, 2, 3, 7, 9 (5 items)
- **EXTEND:** Items 4, 1.5a, 1.5b, 1.75, 5, 6, 8, 12, 13 (9 items)
- **SPLIT:** Items 10, 11, 14, 15 (4 items)
- **SUBSUME / DEFER:** 0 items

Key observation: **no Phase 2B-3 item is fully invalidated by three-tier.** Supports Option 2 (rescope mid-flight) over Option 1 (ship then rework).

---

## C. Ambiguity Resolutions (Inline)

This section makes the BLOCKING resolutions explicit as implementable contracts. All four items listed as BLOCKING in Revision 1's Section G are resolved here. Critic ambiguity items 26-29 are also addressed.

### C.1 Architect cwd and worktree lifecycle

**Decision:** Architect runs in dedicated worktree `{worktree_base}/architect-{projectId}` on branch `harness/architect-{projectId}`.

**Lifecycle:**

| Event | Action on Architect worktree |
|---|---|
| `projectStore.createProject()` / `!project <name>` | `git worktree add {worktree_base}/architect-{projectId} -b harness/architect-{projectId}` from `main` |
| Architect session spawn | `Options.cwd = {worktree_base}/architect-{projectId}` |
| Compaction (abort + respawn, Section C.4) | Worktree preserved; new session reuses it with summary |
| `completeProject(projectId)` | `git worktree remove` + `git branch -D harness/architect-{projectId}` |
| `failProject(projectId, reason)` | same cleanup as complete |
| `!project <id> abort` (operator) | same cleanup as complete |
| Architect session crash mid-project | Worktree preserved; orchestrator respawns Architect on next poll (Section K) with summary reboot |
| Global shutdown (`shutdownAll`) | Worktrees preserved across restarts; on startup, `initProjectsFromState()` reattaches |

**Config:** `worktree_base` already exists (Phase 2A). No new config.

**Critic item 19 (worktree cleanup on failure):** Explicitly part of `failProject` path per table above. Wave B acceptance criterion.

### C.2 Routing precedence (doc-comment in `src/orchestrator.ts`)

Wave 1.5b adds the following doc-comment above `processTask()` and the ingest validator:

```typescript
/**
 * Routing precedence (evaluated in order):
 *
 *   1. task.projectId !== undefined
 *        → routeByProject (Wave B); mode/response_level subordinate.
 *        → All project behavior (mandatory review, Architect arbitration,
 *          project-channel dialogue) gates on this single boolean.
 *
 *   2. TaskFile.mode === "dialogue" && task.projectId === undefined
 *        → pre-pipeline dialogue (Wave 6-split); DialogueSession + proposal.json.
 *
 *   3. Otherwise
 *        → standard pipeline: Executor → shouldReview(task) → (review?) → merge.
 *        → responseLevel routes merge / review / escalation as per Phase 2A.
 *
 * Conflict: projectId + mode:"dialogue" combined → REJECTED at task ingest
 * (throws ValidationError). Project dialogue happens through the Architect,
 * not through the standalone dialogue session. See architect-prompt.md §3.
 */
```

**Ingest validator extension (Wave 1.5b):** `TaskFile` validator throws `ValidationError("projectId and mode:dialogue are mutually exclusive")` when both present. Test: `tests/orchestrator.test.ts` — "rejects projectId+mode:dialogue at ingest".

### C.3 tier1EscalationCount and arbitration cap semantics

Two counters:

```typescript
// src/lib/state.ts — TaskRecord
tier1EscalationCount?: number;          // per-task (Phase 2A, unchanged)
arbitrationCount?: number;              // per-task, new — count of Architect
                                        // arbitrations on THIS phase
reviewerRejectionCount?: number;        // per-task, new — count of Reviewer
                                        // rejects on THIS phase

// src/lib/project.ts — ProjectRecord
totalTier1EscalationCount: number;      // per-project aggregate, NEW
// Cap from config.project.max_total_tier1_escalations
// Default: 5 * pipeline.max_tier1_escalations (= 10 with defaults)
```

**Escalation cap check (orchestrator, before firing any arbitration):**

```typescript
function arbitrationCapExceeded(task, project, config): boolean {
  const perTask = (task.tier1EscalationCount ?? 0) >= config.pipeline.max_tier1_escalations;
  const perProj = (project.totalTier1EscalationCount ?? 0) >= config.project.max_total_tier1_escalations;
  return perTask || perProj;
}

// On any Architect arbitration attempt (success or failure):
//   task.tier1EscalationCount++
//   project.totalTier1EscalationCount++
//   Both persist (state.ts + project.ts atomic writes).
```

**Verdict path when cap exceeded:**
- Transition task to `escalation_wait` (NOT `failed`) with `reason: "tier1_cap_reached"`.
- Emit `escalation_needed` to Discord escalation channel with `projectId`, phase, rationale.
- Operator may resume via `!reply` (standalone path) OR via `!project <id> abort` (project path).

**Ambiguity Resolution #26 (Critic):** On `plan_amendment` verdict, always create a **fresh task.id** (UUID-generated) for the new phase. Never reuse the old task.id. Old task marked `failed` with `reason: "plan_amended"`. Tested in Wave C (`tests/lib/arbitration.test.ts` → "plan_amendment creates fresh task.id").

**Ambiguity Resolution #29 (Critic):** 60% tier-1 resolution rate is a **Wave D acceptance gate** (not target). Wave D fails if rate < 60% over a 50-event sample. Explicit assertion in `tests/e2e/project-lifecycle.test.ts`.

### C.4 Project budget ceiling + orchestrator-level enforcement

**Ceiling default:** `project.budgetCeilingUsd = 10 * pipeline.max_budget_usd` (defaults: 10 * $1.00 = $10.00). Override via `project.toml` per-project.

**Orchestrator-level precheck (before every session spawn — Executor, Reviewer, OR Architect):**

```typescript
// In orchestrator.spawnSessionFor(task):
if (task.projectId) {
  const project = this.projectStore.getProject(task.projectId);
  const projected = project.totalCostUsd + (session.maxBudgetUsd ?? DEFAULT_SESSION_BUDGET);
  if (projected > project.budgetCeilingUsd) {
    // Do NOT spawn. Escalate operator with reason.
    await this.emitEscalation(task, {
      reason: "budget_ceiling_reached",
      projectId: task.projectId,
      currentCostUsd: project.totalCostUsd,
      ceilingUsd: project.budgetCeilingUsd,
      projectedAfterSpawn: projected,
    });
    await this.stateManager.transition(task.id, "escalation_wait");
    return;  // orchestrator aborts spawn
  }
}
// Normal spawn path
```

**Not silent failure.** Escalation is the default route. Operator decides (via `!reply` or manual intervention): raise ceiling and resume, or abort project via `!project <id> abort`.

**Cost rollup:** Every SessionResult emits `totalCostUsd`. On Executor/Reviewer/Architect session completion, orchestrator calls `projectStore.incrementCost(projectId, costUsd)` — this moves the project's running total and triggers cap re-check on the next spawn.

### C.5 Compaction mechanism (orchestrator-driven) + summary schema

**Mechanism:** Orchestrator detects `architectSession.totalCostUsd >= 0.60 * project.budgetCeilingUsd`. Steps:

1. Orchestrator calls `architectManager.requestSummary(projectId)` — Architect produces summary per schema below.
2. Orchestrator aborts Architect session via its AbortController.
3. Orchestrator writes `architectSummary` + `compactedAt` to `projectStore`.
4. Orchestrator cleans any stale tmux / session state for the old sessionId.
5. Orchestrator respawns Architect on the SAME worktree, feeding summary as the first turn's context alongside the original project declaration.

**Summary schema (Critic item 16):**

```typescript
export interface ArchitectCompactionSummary {
  projectId: string;
  name: string;
  description: string;              // ORIGINAL operator-provided project description
  nonGoals: string[];               // VERBATIM from original project declaration
                                    // Enforced: must be present; project.ts validates
                                    // on createProject and re-reads on compaction.
  priorVerdicts: Array<{
    phaseId: string;
    verdict: "retry_with_directive" | "plan_amendment" | "escalate_operator";
    rationale: string;
    timestamp: string;
  }>;
  completedPhases: Array<{
    phaseId: string;
    taskId: string;
    state: "done" | "failed";
    finalCostUsd: number;
    finalVerdict?: string;
  }>;
  currentPhaseContext: {
    phaseId: string;
    taskId: string;
    state: string;
    reviewerRejectionCount: number;
    arbitrationCount: number;
    lastDirective?: string;
  };
  compactedAt: string;              // ISO timestamp
  compactionGeneration: number;     // monotonic, starts at 0, increments each compaction
}
```

**Validation (Wave D):**
- `nonGoals` MUST be preserved verbatim from project declaration. Failure = Wave D gate fails.
- Schema validated with `zod` (new dev dep — low weight, already in ecosystem) or a hand-rolled validator (no new dep). Planner recommends hand-rolled to avoid dep.
- `priorVerdicts` length must equal prior arbitration count.

**Post-compaction prompt:** First turn to fresh Architect session includes:

```
You are resuming project {projectId} after a context compaction at generation {N}.
The full project description and ORIGINAL non-goals follow — do not alter them.

PROJECT DESCRIPTION:
{description}

NON-GOALS (original, verbatim):
{nonGoals joined by \n- }

PRIOR VERDICTS (most recent last):
{priorVerdicts formatted}

COMPLETED PHASES:
{completedPhases formatted}

CURRENT PHASE CONTEXT:
{currentPhaseContext formatted}

Continue from the current phase. Your retry-only authority is unchanged.
```

### C.6 Wave A→C interim-window behavior (Critic ambiguity #27)

**Interpretation A (selected):** Task transitions to `review_arbitration` during Wave A's Reviewer integration, but the Architect listener that consumes this state is not wired until Wave C. During the Wave A→C window, tasks that reach `review_arbitration` **persist in state and block merge**. Orchestrator emits a one-time warning log per task: `WARN task={taskId} in review_arbitration but architect listener not yet wired (Wave A/B/C window); merge blocked`.

**Interim test (Critic item 8):** `tests/orchestrator.test.ts` — "review_arbitration persists during Wave A→C window, emits warning, blocks merge". Tests Wave A's state without Wave C's listener.

**Closure:** Wave C wires the listener; interim warning log is removed in Wave C's orchestrator edits.

---

## D. Revised Wave Sequence

```
Wave 1  (Pre-Reqs)                       [unchanged from 2B-3 except Item 4 extension]
Wave 1.5  (Decompose + Schema)           [EXTENDED: schema gains project + arbitration fields]
Wave 1.75 (Smoke Test)                   [EXTENDED: long-lived + concurrent checks]
Wave 2  (Discord Outbound)               [EXTENDED: new events w/ projectId]
Wave 3  (Discord Inbound)                [EXTENDED: !project, !project status, !project abort]
Wave A  (Reviewer Gate + arbitration state + mandatory-for-project)
Wave B  (Project lifecycle + Architect session + decomposition)
Wave B.5 (Architect smoke test — 5 mock escalations, must resolve 3+ — Critic item 25)
Wave 4  (Escalation routing: standalone direct, project→Architect)
Wave C  (Arbitration routing + resume-with-directive + plan-amendment)
Wave 6-split (Dialogue: standalone keeps proposal.json, project via Architect)
Wave D  (Compaction handoff + end-to-end project validation — mandatory e2e)
```

**Dependency graph (revised):**

```
Wave 1 → Wave 1.5 → Wave 1.75 ─┬→ Wave 2 → Wave 3 ─┬→ Wave A ─┐
                                │                   │          ├→ Wave B → Wave B.5 gate → Wave 4 → Wave C → Wave 6-split → Wave D
                                │                   └─(parallel)┘
                                └─(nothing else until 2)
```

Critical path: 1 → 1.5 → 1.75 → 2 → 3 → A → B → B.5 → 4 → C → 6-split → D (12 waves).
Parallel: Wave A may begin as soon as Wave 1.5 + 1.75 land.

---

## E. RALPLAN-DR Summary

### Principles (5)

1. **Additive-where-possible, extending-with-replace-for-review-and-dialogue.** The 280 Phase 2A tests stay green. Phase 2B-3's approved items are either extended or split by routing conditions. Only Waves 5 and 6 of Phase 2B-3 are *replaced* (by Waves A and 6-split respectively); all other Phase 2B-3 waves remain intact. The three-tier design runs *above* the current supervised-session model for project-tagged tasks only.

2. **Project-gated elevation.** Every three-tier behavior (Architect session, mandatory review, arbitration) activates ONLY when the task has a declared `projectId`. Standalone tasks see zero behavioral drift from Phase 2B-3 end-state.

3. **Retry-only authority is terminal.** Architect can direct the Executor to retry or spawn a fresh Executor against an amended phase spec. Architect cannot force-merge, cannot override Reviewer, cannot skip merge tests. Operator is the sole override path; in v1 that means "operator manually merges via git if truly needed" — no harness-level override command.

4. **Deterministic routing; LLM only for Discord NL.** Project-ness, arbitration routing, and verdict classification are all deterministic based on task fields and state. LLM classify stays where Phase 2B-3 put it (Discord NL intent, resolution detection).

5. **State machine is load-bearing, not decorative.** The new `review_arbitration` state isn't a tag — it's a hard gate. Entering it triggers Architect; exiting it requires a verdict. Bounded-iteration invariants (per-task cap 2, per-project cap 10) are enforced in orchestrator precheck + counter, not by prompt.

### Decision Drivers (Top 3)

1. **Phase 2B-3 approval momentum.** Phase 2B-3 was approved 2026-04-12 with explicit consensus. Discarding Waves 1-3 and restarting invalidates that approval. Preserving Waves 1-4 and extending 5-6 keeps the approval chain intact.

2. **Architect depends on Discord.** Three-tier's `escalate_operator` verdict requires Discord notification. Project lifecycle depends on `!project` command. Reviewer notifications flow through Discord. Three-tier before Phase 2B-3 Discord is infeasible unless we stub Discord — which is exactly what makes Option 1 tempting (ship 2B-3 first) but Option 2 tractable (merge mid-flight).

3. **`review_arbitration` state is foundational.** It must exist before any arbitration logic can be tested. Phase 2B-3 Wave 1.5b already adds schema fields — extending it once (now) is cheaper than doing a second schema migration later. Doing the schema work in a single wave minimizes state-file migration risk.

### Viable Options

#### Option 1: Ship Phase 2B-3 as-is first, then three-tier as separate Phase 4 — REJECTED

Complete all 6 Phase 2B-3 waves unchanged, treat three-tier as its own consensus cycle targeting Phase 4.

**Pros:** Preserves approval chain strictly. Phase 4 can rerun consensus with production data.
**Cons / concrete rework estimate (Critic item 13 engagement):**
- Item 12 `gates/review.ts`: ~15 lines (add rejection-counter field + `arbitrationThreshold` config + project-aware trigger). Arch counter-estimate of 15-20 lines matches here for `review.ts` itself.
- Item 13 orchestrator review-trigger logic: ~50 lines rework (threshold → mandatory-for-project branch + `review_arbitration` transition + rejectionCount). Materially > 15-20.
- Item 14 dialogue agent: partial dead code (`proposal.json` flow unused for project tasks) — no line edit, but creates a maintenance cost to preserve a shipped-but-bypassed path.
- Item 15 dialogue channel: ~30 lines rework (add project dispatch).
- `state.ts`: ~40 lines rework (second migration with `review_arbitration` state + three-tier fields; KNOWN_KEYS second update).
- Second ADR consensus cycle: planner + architect + critic loop = ~3 rework cycles at min.
- **Concrete total:** ~135 lines source + second consensus cycle. The "~200 lines of churn" figure in Revision 1 conflated source lines with consensus/process cost; the accurate source-only figure is ~135 + a second consensus cycle's overhead.

**Invalidation rationale:** The three-tier spec is LOCKED (PASSED at 19%). Shipping code that will be refactored within weeks violates build-what-you-know. Even the tightened ~135-line rework estimate is larger than Option 2's integration cost (Option 2 adds ~300 lines of dispatch + ~500 lines of net-new Architect/arbitration code, but replaces zero working code).

#### Option 2: Rescope Phase 2B-3 Waves 5-6 mid-flight to incorporate three-tier — SELECTED

Keep Waves 1, 1.5, 1.75, 2, 3, 4 from Phase 2B-3 largely as-approved. Replace Waves 5 and 6 with Waves A-D (Reviewer+arbitration state, project lifecycle, arbitration routing, compaction+validation). Items 12-15 get woven into Waves A-D.

**Pros:** Single consensus cycle. Schema migration happens once. No dead code. Approval chain extended.
**Cons:** Phase 2B-3's ADR must be amended (this document supersedes it in part — supersedes note written to phase 2B-3 plan per Critic item 14). Critic re-review burden. Added integration work: ~300 lines project-awareness dispatch + ~500 lines Architect/arbitration code + ~60 lines arbitration side-effects in orchestrator.

**Why chosen:** Matches construction sequence (schema → review gate → Architect → arbitration → dialogue split). Phase 2B-3 Waves 1-4 remain intact. Three-tier's A/B/C/D naturally slots after Wave 3.

#### Option 3: Replace Wave 6 entirely with three-tier, keep Waves 1-5 unchanged — REJECTED

Ships Review Gate at threshold-gated form before re-extending for project-mandatory.

**Cons:** Review Gate gets re-touched in Wave A within days of Wave 5 landing. Items 14-15 need partial preservation + partial replacement. Dependency inversion: Wave A's arbitration depends on reviewer rejection counter — a Wave A concern, not Wave 5.

#### Option 4: Freeze Phase 2B-3 after Wave 4, jump to three-tier — REJECTED

Ship Waves 1-4. Freeze. Jump to three-tier.

**Cons:** Standalone dialogue proposal.json path becomes perpetual "Wave D+1 item". Close to Option 2 but loses ship-order predictability for standalone dialogue.

### ADR

**Decision:** Adopt Option 2. Retain Phase 2B-3 Waves 1, 1.5, 1.75, 2, 3, 4 as approved with additive extensions to schema (Wave 1.5b) and events (Wave 2/3). Replace Phase 2B-3 Waves 5 and 6 with integrated Waves A, B, B.5, C, 6-split, D.

**Drivers:** Phase 2B-3 approval momentum. Architect depends on Discord. `review_arbitration` state is foundational.

**Alternatives considered:**
- Option 1 (ship then rework): concrete ~135 lines rework + second consensus cycle; exceeds integration cost of Option 2 once consensus overhead is counted.
- Option 3 (replace wave 6 only): creates retrofit churn on Review Gate; violates natural dependency.
- Option 4 (freeze at wave 4): defers standalone dialogue indefinitely.

**Why chosen:** Only option that ships both locked designs (Phase 2B-3 + three-tier) with a single consensus cycle, no rework, one schema migration, clear ordering of concerns.

**Consequences:**
- Phase 2B-3 ADR is amended (not preserved verbatim). Supersedes note written to `ralplan-harness-ts-phase2b-3.md` per Critic item 14.
- Schema migration for `TaskRecord` gains three-tier fields alongside Phase 2B-3 fields.
- `VALID_TRANSITIONS` extended for `review_arbitration` state.
- `tier1EscalationCount` field (Phase 2A) remains per-task; **new** `totalTier1EscalationCount` added per-project.
- Runtime dependencies: `discord.js@^14.x` (from 2B-3) + no three-tier-specific deps.
- Net new source lines: ~1,300 three-tier + ~1,100 Phase 2B-3-as-extended = ~2,400 total.
- Net new tests: ~95 three-tier + ~140 Phase 2B-3-as-extended = ~235. Existing 280 tests stay green.
- Config grows: `[project]` ceiling fields, `[architect]` section, `[reviewer]` section.
- Feature flag `enable_three_tier` (Section J) allows rollback without code revert.

**Follow-ups:**
- Post-Wave D: Critic pass on bias-in-arbitration (Architect judging its own plan). Spec deferred to "empirical after deploy" — first 20 arbitrations become calibration dataset.
- Post-Wave D: revisit plan-citation recusal if arbitration data shows Architect bias >10% over Reviewer's independent judgment on same ground.
- Phase 4 (observability): project-level cost dashboards, per-phase Reviewer verdict trending, Architect context-exhaustion alerts.
- Architect Prompt Iteration Protocol (Critic item 24): tune architect-prompt.md at Phase 4 based on observed tier-1 resolution rate; document Phase 4 target or mark out-of-scope for v1.
- Observation-only graduation mode (Critic item 15): consider shipping Wave A's project-Reviewer-mandatory as observation-only for first N runs. See Section L for rationale on deferring vs adopting.

---

## F. Per-Wave Breakdown

### Wave 1: Pre-Requisites

**Delta vs Phase 2B-3:** Identical to Phase 2B-3 Wave 1 except Item 4 (tmux cleanup) adds project patterns.

#### Item 4 extension (tmux cleanup for projects)

*Files:*
- MODIFY `src/session/manager.ts` — `cleanupWorktree()` expands pattern matching to include `architect-{projectId}` when a project task is cleaned; new `cleanupProject(projectId)` method for project-complete sweep. Also calls `git worktree remove` on the Architect worktree per Section C.1.

*Interface:*
```typescript
cleanupProject(projectId: string): Promise<void>;
// Extension inside cleanupWorktree(taskId: string):
// If task.projectId, also kill `architect-{task.projectId}` tmux pattern
// (but only if no other active phases on that project — guard via projectStore).
```

*Acceptance criteria:*
- Project-tagged tasks trigger Architect tmux cleanup on worktree removal only if no other active phases on the project (guarded by `projectStore.hasActivePhases()`).
- `cleanupProject()` sweeps all project-related tmux sessions + Architect worktree when project completes/fails/aborts.
- Existing taskId-only cleanup behavior preserved for standalone tasks.
- Architect worktree removed via `git worktree remove` per Section C.1.

*Tests:*
- `tests/session/manager.test.ts`: +3 tests — project-aware tmux cleanup preserves other-phase Architect; `cleanupProject` sweeps correctly; `cleanupProject` removes Architect worktree.

*Effort:* ~15 lines source beyond Phase 2B-3 Item 4. +3 tests.

**Dependencies:** None.

### Wave 1.5: Orchestrator Decomposition + State Schema Update (EXTENDED)

#### Item 1.5a: `processTask()` decomposition + `routeByProject`

*Files:*
- MODIFY `src/orchestrator.ts` — Phase 2B-3's 4 extracted methods plus `routeByProject`. Routing precedence doc-comment from Section C.2 added.

```typescript
/**
 * [Full doc-comment from Section C.2 inserted above processTask]
 */
private async routeByProject(task: TaskRecord): Promise<boolean>;
// Returns true if task was routed into project-phase handling (not used
// to bypass Executor — project phases still spawn Executors; this method
// handles pre-spawn project-bookkeeping like cost-ceiling precheck).
```

*Acceptance criteria:*
- All 41 Phase 2A orchestrator tests pass unchanged.
- Five private decomposition methods under 80 lines each.
- `routeByProject` is the sole project-awareness dispatch entry point.
- Doc-comment present and accurate.
- `projectId + mode:"dialogue"` rejected at ingest (Section C.2 ingest validator).

*Tests:*
- `tests/orchestrator.test.ts`: +1 test — "ingest rejects projectId+mode:dialogue". Others unchanged.

*Effort:* ~8 lines beyond Phase 2B-3 1.5a (one extracted method stub + ingest validator). +1 test.

#### Item 1.5b: State schema (EXTENDED)

*Files:*
- MODIFY `src/lib/state.ts` — add three-tier fields, new state, transitions, known keys (~50 lines).
- MODIFY `src/orchestrator.ts` — `TaskFile` gains `projectId?`, `phaseId?` (~3 lines).
- CREATE `src/lib/project.ts` — Project record interface + in-memory project store (~200 lines).

*`TaskRecord` full additions:*
```typescript
// Phase 2B-3 additions (retained)
dialogueMessages?: Array<{ role: "operator" | "agent"; content: string; timestamp: string }>;
dialoguePendingConfirmation?: boolean;
reviewResult?: { verdict: string; weightedRisk: number; findingCount: number };

// Three-tier additions
projectId?: string;
phaseId?: string;
arbitrationCount?: number;       // per-task Architect arbitrations
reviewerRejectionCount?: number; // per-task Reviewer rejections
```

*`TASK_STATES` addition:*
```typescript
export const TASK_STATES = [
  "pending", "active", "reviewing", "merging", "done", "failed",
  "shelved", "escalation_wait", "paused",
  "review_arbitration",   // NEW
] as const;
```

*`VALID_TRANSITIONS` additions:*
```typescript
reviewing: ["active", "merging", "done", "failed", "escalation_wait", "review_arbitration"],
review_arbitration: ["active", "merging", "failed", "escalation_wait"],
```

*`KNOWN_KEYS` additions:*
```typescript
const KNOWN_KEYS: ReadonlySet<string> = new Set([
  // Phase 2A existing 17 keys...
  // Phase 2B-3 additions: "dialogueMessages", "dialoguePendingConfirmation", "reviewResult"
  // Three-tier additions:
  "projectId", "phaseId", "arbitrationCount", "reviewerRejectionCount",
]);
```

*`TaskFile` extension:*
```typescript
export interface TaskFile {
  id?: string;
  prompt: string;
  priority?: number;
  mode?: "dialogue" | "reviewed";
  projectId?: string;  // if present, task is a project phase
  phaseId?: string;    // phase identifier within project; defaults to task.id
}
```

*New file `src/lib/project.ts`:*
```typescript
export interface ProjectRecord {
  id: string;
  name: string;
  description: string;
  nonGoals: string[];                       // verbatim from operator declaration
  state: "decomposing" | "executing" | "completed" | "failed" | "aborted";
  architectSessionId?: string;
  architectWorktreePath: string;            // from Section C.1
  architectSummary?: string;                // set on compaction
  compactionGeneration: number;             // monotonic, 0 = never compacted
  phases: Array<{
    id: string;
    taskId?: string;
    state: "pending" | "active" | "done" | "failed";
    spec: string;
    reviewerRejectionCount: number;
    arbitrationCount: number;
  }>;
  totalCostUsd: number;
  budgetCeilingUsd: number;                 // required, default 10 * max_budget_usd
  totalTier1EscalationCount: number;        // per-project aggregate (NEW)
  createdAt: string;
  updatedAt: string;
  completedAt?: string;
}

export class ProjectStore {
  constructor(statePath: string);
  createProject(name: string, description: string, nonGoals: string[]): ProjectRecord;
  getProject(projectId: string): ProjectRecord | undefined;
  addPhase(projectId: string, spec: string, phaseId?: string): string;
  attachTask(projectId: string, phaseId: string, taskId: string): void;
  incrementCost(projectId: string, costUsd: number): void;
  incrementTier1Escalation(projectId: string): number;  // returns new total
  markPhaseDone(projectId: string, phaseId: string): void;
  markPhaseFailed(projectId: string, phaseId: string, reason: string): void;
  hasActivePhases(projectId: string): boolean;
  completeProject(projectId: string): void;
  failProject(projectId: string, reason: string): void;
  abortProject(projectId: string): void;
  setArchitectSummary(projectId: string, summary: ArchitectCompactionSummary): void;
  getAllProjects(): ProjectRecord[];
}
```

*Persistence:* Atomic write pattern (UUID temp + rename). `projects.json` separate from task state.

*Acceptance criteria:*
- All Phase 2A + 2B-3 schema tests pass.
- `review_arbitration` transitions validated.
- KNOWN_KEYS B7: three-tier fields round-trip; unknown keys dropped.
- `TaskFile.projectId` and `phaseId` parsed.
- `ProjectStore` atomic writes verified.
- Project state transitions bounded.
- `nonGoals` required on createProject; empty-array permitted but `undefined` rejected.

*Tests:*
- `tests/lib/state.test.ts`: +5 tests.
- `tests/lib/project.test.ts` (new): 18 tests — create with/without nonGoals, addPhase, attachTask, incrementCost, incrementTier1Escalation, cost ceiling boundary, phase state transitions, persistence round-trip, corruption recovery, multi-project isolation, completeProject, failProject, abortProject, hasActivePhases, getAllProjects, setArchitectSummary, compactionGeneration increments.
- `tests/orchestrator.test.ts`: +2 tests — TaskFile.projectId+phaseId parsed; ingest rejects projectId+mode:dialogue.

*Effort:* ~50 lines `state.ts` + ~200 lines `project.ts` + ~3 lines orchestrator. +25 tests.

**Dependencies:** Wave 1.

### Wave 1.75: Pipeline Smoke Test (EXTENDED)

*Verify (existing 7 + 2 new):*
1-7. Phase 2B-3 Wave 1.75 checks — unchanged.
8. **Long-lived session resume.** Spawn session, abort, wait 15 min, resume via `resumeSession()`. Pass/fail informs Architect compaction implementation details.
9. **Concurrent-session contention (Critic item 17).** Spawn two concurrent SDK sessions in separate worktrees; verify both complete without session-ID collision, tmux collision, or state-write contention.

*Acceptance criteria:* Phase 2B-3 4 items + long-lived resume + concurrent contention.

*Effort:* 0 code lines (manual validation). +2 checks in smoke test plan.

**Dependencies:** Wave 1.5 complete.

### Wave 2: Discord Outbound (EXTENDED)

*Files:*
- MODIFY `src/orchestrator.ts` — `OrchestratorEvent` gains variants (all project-related events carry `projectId` per Critic item 10):

```typescript
| { type: "project_declared"; projectId: string; name: string }
| { type: "project_decomposed"; projectId: string; phaseCount: number }
| { type: "project_completed"; projectId: string; phaseCount: number; totalCostUsd: number }
| { type: "project_failed"; projectId: string; reason: string }
| { type: "project_aborted"; projectId: string; operatorId: string }
| { type: "architect_spawned"; projectId: string; sessionId: string }
| { type: "architect_respawned"; projectId: string; sessionId: string; reason: "compaction" | "crash_recovery" }
| { type: "architect_arbitration_fired"; taskId: string; projectId: string; cause: "escalation" | "review_disagreement" }
| { type: "arbitration_verdict"; taskId: string; projectId: string; verdict: "retry_with_directive" | "plan_amendment" | "escalate_operator"; rationale: string }
| { type: "review_arbitration_entered"; taskId: string; projectId: string; reviewerRejectionCount: number }
| { type: "review_mandatory"; taskId: string; projectId: string }
| { type: "budget_ceiling_reached"; projectId: string; currentCostUsd: number; ceilingUsd: number }
| { type: "compaction_fired"; projectId: string; generation: number }
```

- MODIFY `src/discord/notifier.ts` — handlers + routing:
```
project_declared         → dev_channel
project_decomposed       → dev_channel
project_completed        → dev_channel
project_failed           → ops_channel
project_aborted          → ops_channel
architect_spawned        → dev_channel
architect_respawned      → ops_channel
architect_arbitration_fired → ops_channel
arbitration_verdict      → ops_channel
review_arbitration_entered → escalation_channel
review_mandatory         → dev_channel
budget_ceiling_reached   → escalation_channel
compaction_fired         → dev_channel
```

- MODIFY `src/lib/config.ts`:
```typescript
"architect": { name: "Architect", avatar_url: "..." },
"reviewer":  { name: "Reviewer",  avatar_url: "..." },
```

*Acceptance criteria:*
- All Phase 2B-3 Wave 2 criteria.
- 13 new events routed to correct channels (up from 7 in Revision 1).
- All project-related events carry `projectId` (enables Discord thread-routing in Wave 3).
- Architect/reviewer agent identities resolve from config or fall back.

*Tests:*
- `tests/discord/notifier.test.ts`: +13 tests for new event handlers. Total 12 → 25.

*Effort:* ~65 lines beyond Phase 2B-3 Wave 2. +13 tests.

**Dependencies:** Wave 1.75 pass.

### Wave 3: Discord Inbound (EXTENDED)

**Delta vs Phase 2B-3:** Adds `!project` command family and project-aware status.

*Files:*
- MODIFY `src/discord/commands.ts` — `!project <name>`, `!project <id> status`, `!project <id> abort` (Critic item 21).

```typescript
// New CommandIntent variants
| { type: "declare_project"; name: string; description: string; nonGoals: string[] }
| { type: "project_status"; projectId: string }
| { type: "project_abort"; projectId: string; operatorId: string }

// !project <name>
// <multi-line description with NON-GOALS: bullet list>
// → parses NON-GOALS section (required), creates project, spawns Architect
async handleProjectCommand(args: string, channelId: string, operatorId: string): Promise<string>;

// !project <id> status
// → returns project state, phases list, arbitration history, cost/ceiling, Architect status
async handleProjectStatusCommand(args: string): Promise<string>;

// !project <id> abort
// → confirmation prompt, then aborts all phase tasks, terminates Architect, marks failed,
//    cleans worktrees. Emits project_aborted event.
async handleProjectAbortCommand(args: string, operatorId: string): Promise<string>;
```

**Status command output format (Critic item 9):**
```
Project {id} — "{name}"
State: {state}
Phases ({done}/{total}):
  - phase-01 (done, $0.42, reviewer rejections: 0, arbitration: 0)
  - phase-02 (active, $0.15, reviewer rejections: 1)
  ...
Arbitration history ({totalTier1EscalationCount}/{cap}):
  - 2026-04-23T10:00 phase-02 review_disagreement → retry_with_directive
Cost: ${totalCostUsd} / ${budgetCeilingUsd}
Architect: session={sessionId} generation={compactionGeneration}
```

*NL classification (`handleNaturalLanguage`):*
```typescript
/^(start|begin|kick ?off)\s+(a|the|new)?\s*project\b/i → declare_project
/^(status|progress|state)\s+of\s+project\s+(\S+)/i → project_status
/^(abort|kill|cancel)\s+project\s+(\S+)/i → project_abort
```

**Dialogue channel empty-state (Critic item 22):** NL messages in dialogue channel with no active project AND no active standalone dialogue are handled as: bot responds with "No active project or dialogue. Use `!task`, `!project`, or `!dialogue` to start one." (option a selected — prompt the operator rather than silent-ignore, because silent ignore is confusing).

*Acceptance criteria:*
- `!project <name>\n<description>\nNON-GOALS: ...` parses, creates project, emits `project_declared`.
- `!project <id> status` returns formatted status per spec above.
- `!project <id> abort` requires confirmation, aborts project, cleans worktrees, emits `project_aborted`.
- `!status` with no args returns project list + tasks; `!status <projectId>` returns project+phase summary; `!status <taskId>` unchanged.
- NL patterns classify correctly.
- Empty-state dialogue channel prompts operator.

*Tests:*
- `tests/discord/commands.test.ts`: +10 tests (declare project, status with project, abort project, abort confirms, NL project patterns × 3, invalid project syntax, non-goals required, dialogue empty-state). Total 12 → 22.

*Effort:* ~70 lines beyond Phase 2B-3 Wave 3. +10 tests.

**Dependencies:** Wave 2 (outbound notifies `project_declared`, `project_aborted`).

### Wave A: Reviewer Gate + Arbitration State + Mandatory-for-Project

**Replaces Phase 2B-3 Wave 5, extended.**

*What it does:* Ships the ReviewGate from Phase 2B-3 Item 12 unchanged in its core. Adds project-aware routing. Wires `review_arbitration` state transition. Architect arbitration integration is stubbed here (completed in Wave C). During Wave A→C window, tasks entering `review_arbitration` persist in state, emit one-time warning log, block merge (Section C.6).

*Files:*
- CREATE `src/gates/review.ts` — as Phase 2B-3 Item 12 (~120 lines) + rejection counter exposure.
- CREATE `config/harness/review-prompt.md` — as Phase 2B-3 Item 12 (~100 lines).
- MODIFY `src/orchestrator.ts` — review trigger logic (~55 lines):
  - `task.projectId !== undefined → ALWAYS fire review`.
  - On reject + project + `reviewerRejectionCount >= 2` → transition to `review_arbitration`; emit `review_arbitration_entered` with `projectId`; **during Wave A→C window, log warning "architect listener not wired" and block merge**.
  - On reject + project + count < 2 → increment, transition to `active`, emit `retry_scheduled`.
  - On reject + standalone → transition to `failed`.
- MODIFY `src/lib/config.ts` — add `[reviewer]` section.

*Interface:*
```typescript
export interface ReviewResult {
  verdict: "approve" | "reject" | "request_changes";
  riskScore: { correctness; integration; stateCorruption; performance; regression; weighted; };
  findings: Array<{ severity; file; line?; description; suggestion? }>;
  summary: string;
}

export interface ReviewGateConfig {
  model?: string;
  maxBudgetUsd?: number;           // per-review cap
  rejectThreshold?: number;
  timeoutMs?: number;
  arbitrationThreshold?: number;   // default 2 (NEW three-tier)
}
```

**`persistSession: false` regression test (Section A.4):** `tests/gates/review.test.ts` asserts Reviewer session is spawned with `persistSession: false`.

*Acceptance criteria:*
- All Phase 2B-3 Item 12 + 13 criteria for standalone.
- Project phase: Reviewer ALWAYS fires.
- Project phase Reviewer reject: increments `reviewerRejectionCount`; on 2nd reject transitions to `review_arbitration`.
- `review_arbitration_entered` event emitted with `projectId` + `reviewerRejectionCount`.
- Reviewer session spawned `persistSession: false` (regression test).
- Interim Wave A→C window: warning log emitted, merge blocked. (Removed in Wave C.)

*Tests:*
- `tests/gates/review.test.ts`: +11 tests (Phase 2B-3 10 + 1 persistSession regression).
- `tests/orchestrator.test.ts`: +16 tests — Phase 2B-3 10 + 6 three-tier (project mandatory, count increments, review_arbitration transition at threshold, standalone unchanged, interim Wave A→C window warning, Wave A→C window blocks merge).

*Effort:* ~120 lines `review.ts` + ~100 lines prompt + ~55 lines orchestrator + ~15 lines config. +27 tests.

**Dependencies:** Wave 1.5.

### Wave B: Project Lifecycle + Architect Session

**Three-tier core.**

*What it does:* Implements `!project` wiring → creates project record → creates Architect worktree → spawns Architect session → Architect decomposes into phase task files → orchestrator picks up phases via `scanForTasks()`.

*Files:*
- CREATE `src/session/architect.ts` (~260 lines):

```typescript
export interface ArchitectConfig {
  systemPromptPath: string;
  model?: string;                  // default "opus"
  maxBudgetUsd?: number;           // per-Architect-session cap
  compactionThresholdPct?: number; // default 0.60
  plugins?: Record<string, boolean>;
  arbitrationTimeoutMs?: number;   // default 300_000 (5 min) — Critic item 20
}

export interface ArchitectSession {
  projectId: string;
  sessionId: string;
  worktreePath: string;
  totalCostUsd: number;
  startedAt: string;
  lastActivityAt: string;
  compactionGeneration: number;
}

export class ArchitectManager {
  constructor(
    sdk: SDKClient,
    projectStore: ProjectStore,
    stateManager: StateManager,
    gitOps: GitOps,
    config: HarnessConfig,
    architectConfig: ArchitectConfig,
  );

  /**
   * Spawn Architect for new project. Creates dedicated worktree
   * at {worktree_base}/architect-{projectId}. Returns sessionId.
   */
  async spawn(projectId: string, name: string, description: string, nonGoals: string[]): Promise<{status: "success", sessionId: string} | {status: "failure", error: string}>;

  /**
   * Respawn after crash or compaction. Uses existing worktree.
   */
  async respawn(projectId: string, reason: "compaction" | "crash_recovery", summary?: ArchitectCompactionSummary): Promise<{status: "success", sessionId: string} | {status: "failure", error: string}>;

  /**
   * Decompose project into phases. Richer return type (Critic item 11).
   */
  async decompose(projectId: string): Promise<
    | { status: "success"; phases: Array<{ phaseId: string; taskFilePath: string }> }
    | { status: "failure"; error: string }
  >;

  /** Feed operator input to Architect session via resumeSession. */
  async relayOperatorInput(projectId: string, message: string): Promise<void>;

  /** Handle escalation (Wave C). Bounded by arbitrationTimeoutMs (Critic item 20). */
  async handleEscalation(task: TaskRecord, escalation: EscalationSignal): Promise<ArchitectVerdict>;

  /** Handle review arbitration (Wave C). Bounded by arbitrationTimeoutMs. */
  async handleReviewArbitration(task: TaskRecord, rejection: ReviewResult): Promise<ArchitectVerdict>;

  /**
   * Compact context. Richer return type (Critic item 11).
   */
  async compact(projectId: string): Promise<
    | { compacted: true; newSessionId: string; generation: number }
    | { compacted: false; reason: string }
  >;

  /**
   * Request summary (used internally by compact, also by crash_recovery).
   * Uses resumeSession on current Architect session to ask it to emit summary.
   */
  async requestSummary(projectId: string): Promise<ArchitectCompactionSummary>;

  /** Async shutdown for project. (Critic item 11 — was sync.) */
  async shutdown(projectId: string): Promise<void>;

  /** Async shutdown all. */
  async shutdownAll(): Promise<void>;
}
```

- CREATE `config/harness/architect-prompt.md` (~200 lines) — Architect systemPrompt. Sections:
  - §1 Role
  - §2 Project decomposition output contract
  - §3 Dialogue relay contract (operator input via resumeSession)
  - §4 Escalation resolver contract (tier-1)
  - §5 Review arbitration contract
  - §6 Retry-only authority guardrails
  - §7 OMC agent delegation
  - §8 Non-goals (no recusal, no standalone, no merge override, cannot issue `executor_correct` verdict — Critic item 23)
  - §9 Compaction-response contract (emit `ArchitectCompactionSummary` schema from Section C.5)

**Architect prompt draft review (Critic item 23):** Wave B acceptance includes "architect-prompt.md drafted, reviewed against retry-only guardrail via mock completion assertions: asserted that Architect instructions include the three verdict types and NO `executor_correct` variant. Prompt drafts reviewed by architect agent (OMC) for retry-only compliance before merge."

- MODIFY `src/orchestrator.ts` — wire `!project` to `architectManager.spawn()` then `decompose()`; Architect-crash recovery on poll (Critic item 18).

**Architect crash recovery (Critic item 18):** Orchestrator polls Architect health on every processTask cycle. If `architectSession.sessionId` is not alive (checked via SDK status), respawn via `architectManager.respawn(projectId, "crash_recovery", summary)`. Summary reconstructed from `project.architectSummary` if present, otherwise from `projectStore` state (phases + prior verdicts + non-goals + description).

**Architect stuck-timeout (Critic item 20):** `architectManager.handleEscalation` and `handleReviewArbitration` internally set `AbortController.abort()` after `arbitrationTimeoutMs` (default 300_000ms). Timeout promotes verdict to `{type: "escalate_operator", rationale: "architect_timeout"}`.

- MODIFY `src/discord/commands.ts` — `handleProjectCommand` calls `orchestrator.declareProject()`.
- MODIFY `src/lib/config.ts` — add `[architect]` section.

*Decomposition output protocol:* Architect writes one file per phase to `project.task_dir/`:
```json
{
  "id": "project-{projectId}-phase-{NN}",
  "prompt": "<phase-specific prompt>",
  "priority": 1,
  "projectId": "{projectId}",
  "phaseId": "phase-{NN}"
}
```

*Verdict type (used in Wave C):*
```typescript
export type ArchitectVerdict =
  | { type: "retry_with_directive"; directive: string }
  | { type: "plan_amendment"; updatedPhaseSpec: string; rationale: string }
  | { type: "escalate_operator"; rationale: string };
```

*Acceptance criteria:*
- `!project <name>` declares, spawns Architect, creates project record with `nonGoals`.
- Architect worktree created at `{worktree_base}/architect-{projectId}` on fresh branch.
- Architect session: `settingSources: ["project"]`, `enabledPlugins`, `hooks: {}`, `disallowedTools`, `persistSession: true`, `cwd = architectWorktreePath`.
- Architect prompt reviewed against retry-only guardrail (Critic item 23) — tests verify prompt contains three verdict types and excludes `executor_correct`.
- Architect decomposes into N phase task files with `projectId` + `phaseId`.
- `scanForTasks()` picks up phase tasks.
- Architect survives Executor spawns (Wave 1.75 check #8).
- Crash recovery: orchestrator detects dead Architect on poll, respawns with summary.
- `arbitrationTimeoutMs` enforced on handleEscalation / handleReviewArbitration stubs (stubs return timeout verdict after 300s).
- `project_declared`, `architect_spawned` emitted.
- `shutdown` / `shutdownAll` are async.

*Tests:*
- `tests/session/architect.test.ts` (new): 22 tests —
  1. spawn creates worktree + session
  2. spawn returns `{status: "success", sessionId}`
  3. spawn returns `{status: "failure", error}` on worktree failure
  4. respawn with crash_recovery reason
  5. respawn with compaction reason
  6. decompose returns `{status: "success", phases}`
  7. decompose writes files with correct schema
  8. decompose emits `project_decomposed`
  9. retry-only guardrail parse — rejects `executor_correct` verdict
  10. verdict schema validation (3 valid types)
  11. persistSession: true verified
  12. abort on shutdown
  13. shutdownAll iterates projects
  14. project budget cumulative tracking
  15. compact returns `{compacted: true, newSessionId, generation}`
  16. compact returns `{compacted: false, reason}` when threshold not crossed
  17. requestSummary schema conforms to `ArchitectCompactionSummary`
  18. requestSummary preserves nonGoals verbatim
  19. Architect cannot invoke merge-gate (no injection path)
  20. resumeSession behavior on relayOperatorInput
  21. arbitrationTimeoutMs produces `escalate_operator` verdict with `reason: "architect_timeout"`
  22. architect-prompt.md contains 3 verdict types; excludes `executor_correct`
- `tests/orchestrator.test.ts`: +5 tests — `!project` → Architect spawn, phase task pickup, project record on pickup, dead-Architect detection + respawn, budget-ceiling precheck blocks spawn.

*Effort:* ~260 lines `architect.ts` + ~200 lines prompt + ~60 lines orchestrator wiring + ~15 lines config + ~10 lines commands. +27 tests.

**Dependencies:** Wave A (`review_arbitration` state exists for handleReviewArbitration typing), Wave 3 (`!project` command exists).

### Wave B.5: Architect Smoke Gate (NEW — Critic item 25)

**Purpose:** Intermediate confidence gate before committing to 4 more waves. Manual smoke test validates Architect can resolve tier-1 escalations in practice before Wave C wires verdict application.

*Procedure:*
1. Start harness with feature flag `enable_three_tier: true`.
2. Declare a synthetic project with 3 phases.
3. Trigger 5 synthetic escalations (mock EscalationSignal values with varying content).
4. For each, call `architectManager.handleEscalation(task, escalation)` via a debug endpoint.
5. Inspect returned verdicts.

*Gate criteria:*
- Architect must return at least 3 of 5 with `type: "retry_with_directive"` or `type: "plan_amendment"` (vs blanket `escalate_operator`).
- Verdicts schema-valid (three-type union).
- Arbitration timeout (300s) not tripped in any of 5.

*If fails:* Architect prompt iteration cycle (tune architect-prompt.md). Repeat.

*Acceptance:*
- Wave B.5 pass gates Wave 4+C.

*Effort:* 0 code lines (manual + debug endpoint scripting).

**Dependencies:** Wave B complete.

### Wave 4: Escalation Response (EXTENDED — SPLIT PATH)

*Files:*
- MODIFY `src/discord/escalation-handler.ts` — unchanged for standalone; project routes via `architectManager.handleEscalation()`.
- MODIFY `src/orchestrator.ts` — `resolveEscalation(taskId, operatorResponse)` only for standalone; `resolveProjectEscalation(taskId, architectVerdict)` for project path (completed in Wave C).

*Routing:*
```typescript
if (task.projectId) {
  // Cap check BEFORE invoking Architect
  if (arbitrationCapExceeded(task, project, this.config)) {
    return this.escalateOperator(task, { reason: "tier1_cap_reached" });
  }
  const verdict = await this.architectManager.handleEscalation(task, escalation);
  return this.applyArchitectVerdict(task, verdict);  // Wave C
}
// Standalone — Phase 2B-3 flow
```

*Acceptance criteria:*
- Standalone escalation path identical to Phase 2B-3.
- Project escalation path skips Discord operator notification and calls `architectManager.handleEscalation()`.
- Arbitration cap check fires before Architect invocation.
- `applyArchitectVerdict` exists as method but only stubs in Wave 4 (Wave C completes).

*Tests:*
- `tests/discord/escalation-handler.test.ts`: Phase 2B-3 8 + 3 three-tier (project skips Discord; project calls architectManager; cap check stops before Architect).
- `tests/orchestrator.test.ts`: +2 tests.

*Effort:* ~20 lines escalation-handler + ~35 lines orchestrator. +5 tests.

**Dependencies:** Wave 3, Wave B, Wave B.5 gate.

### Wave C: Arbitration Routing + Resume-with-Directive + Plan-Amendment

**Pure arbitration logic + orchestrator side effects.**

*What it does:* Implements `applyArchitectVerdict()`. The arbitration library is pure — returns discriminated union actions; the orchestrator applies side effects (this is the Architect-flagged layering fix).

*Files:*
- CREATE `src/lib/arbitration.ts` (~80 lines) — **PURE**, does NOT import `Orchestrator`:

```typescript
export interface ArbitrationContext {
  taskId: string;
  projectId: string;
  phaseId: string;
  cause: "escalation" | "review_disagreement";
  escalation?: EscalationSignal;
  rejection?: ReviewResult;
  taskRecord: TaskRecord;
  projectRecord: ProjectRecord;
  config: HarnessConfig;
}

export type ArbitrationAction =
  | { type: "resume"; sessionId: string; directive: string; newArbitrationCount: number }
  | { type: "respawn"; amendedSpec: string; rationale: string; oldTaskId: string; newTaskId: string }
  | { type: "escalate"; reason: "verdict_escalate_operator" | "tier1_cap_reached" | "architect_timeout" | "budget_ceiling_reached"; rationale: string };

/**
 * Pure arbitration router — no dependencies on orchestrator.
 * Given a verdict and context, returns the action to apply.
 *
 * Caller (orchestrator) consumes the action:
 *   - "resume": calls sessionManager.resumeTask(taskId, directive)
 *   - "respawn": writes new task file, marks old task failed
 *   - "escalate": transitions to escalation_wait, emits Discord event
 *
 * DO NOT add orchestrator side effects here. Keep this function pure for testing.
 * DO NOT add plan-citation recusal logic without post-Wave D bias data.
 */
export function routeVerdict(
  ctx: ArbitrationContext,
  verdict: ArchitectVerdict,
): ArbitrationAction;
```

- MODIFY `src/orchestrator.ts` — `applyArchitectVerdict(task, verdict)` calls pure `routeVerdict()`, then dispatches on action type (~60 lines):

```typescript
private async applyArchitectVerdict(task: TaskRecord, verdict: ArchitectVerdict): Promise<void> {
  const project = this.projectStore.getProject(task.projectId!);
  const ctx = { taskId: task.id, projectId: task.projectId!, phaseId: task.phaseId!,
                cause: this.arbitrationCauseFor(task), taskRecord: task, projectRecord: project!,
                config: this.config };
  const action = routeVerdict(ctx, verdict);

  // Bump counters (always, regardless of action type)
  this.stateManager.update(task.id, { arbitrationCount: (task.arbitrationCount ?? 0) + 1 });
  this.projectStore.incrementTier1Escalation(task.projectId!);

  switch (action.type) {
    case "resume":
      await this.stateManager.transition(task.id, "active");
      return this.sessionManager.resumeTask(task, action.directive);
    case "respawn":
      await this.stateManager.transition(task.id, "failed", "plan_amended");
      this.projectStore.markPhaseFailed(task.projectId!, task.phaseId!, "plan_amended");
      await this.writeTaskFile({ ...this.derivePhaseTaskFile(project!, action.amendedSpec), id: action.newTaskId });
      return;
    case "escalate":
      await this.stateManager.transition(task.id, "escalation_wait");
      this.emit({ type: "escalation_needed", taskId: task.id, projectId: task.projectId!, reason: action.reason, rationale: action.rationale });
      return;
  }
}
```

- MODIFY `src/session/manager.ts` — `resumeTask(task, directivePrompt)` wraps SDK `resume: sessionId` (~30 lines).

**Interim-window warning removed (Section C.6):** Wave C's orchestrator edits remove the Wave A→C warning log.

*`applyArchitectVerdict` behavior matrix:*

| Verdict | Cap check | Action | Side effects |
|---|---|---|---|
| `retry_with_directive` | if per-task/per-project cap → `escalate` | `resume` | resumeTask; `review_arbitration → active` or `escalation_wait → active`; bump `arbitrationCount` + `totalTier1EscalationCount` |
| `plan_amendment` | n/a | `respawn` | Abort Executor; mark old task `failed (plan_amended)`; create new task with **fresh UUID** (Critic item 26); write new task file; mark old phase failed; new phase enters pending via scanForTasks |
| `escalate_operator` | n/a | `escalate` | Transition `escalation_wait`; emit `escalation_needed` with `projectId` + rationale |

*Acceptance criteria:*
- `retry_with_directive`: Executor resumed; `arbitrationCount` increments; `totalTier1EscalationCount` increments.
- `plan_amendment`: Executor aborted; new task file created with fresh UUID; old phase `failed (plan_amended)`; new phase spawns.
- `escalate_operator`: Discord notified; `totalTier1EscalationCount` increments; cap check honored.
- Per-task OR per-project cap exceeded → `escalate` action with `reason: "tier1_cap_reached"` regardless of verdict.
- Architect `handleReviewArbitration` returns verdict routes identically via `routeVerdict`.
- `review_arbitration → active` transition fires on `retry_with_directive`.
- **`lib/arbitration.ts` has NO import of `Orchestrator`** (verified via static analysis test).
- Wave A→C window warning log REMOVED.

*Tests:*
- `tests/lib/arbitration.test.ts` (new): 14 tests — verdict schema validation, `routeVerdict` for each of 3 verdicts, cap enforcement returns `escalate`, malformed verdict rejection, pure-function no-side-effect invariant, fresh UUID on plan_amendment, `arbitrationCount` bump computed but not applied in pure function, discriminated-union exhaustiveness, context validation. Plus a static import test: file does not import `Orchestrator`.
- `tests/session/manager.test.ts`: +3 tests — `resumeTask` basic, resume failure fallback to fresh spawn, resume with directive.
- `tests/session/architect.test.ts`: +4 tests — handleEscalation returns retry, plan_amendment, escalate; handleReviewArbitration returns verdict.
- `tests/orchestrator.test.ts`: +7 tests — applyArchitectVerdict for each verdict; cap triggers escalate; review_arbitration full flow; interim warning REMOVED.

*Effort:* ~80 lines `arbitration.ts` (PURE) + ~30 lines manager + ~60 lines orchestrator wiring + ~50 lines architect integration. +28 tests.

**Dependencies:** Wave B (Architect exists), Wave A (review_arbitration state), Wave 4 (standalone path).

### Wave 6-split: Dialogue (SPLIT PATH)

*Files:*
- CREATE `src/session/dialogue.ts` — as Phase 2B-3 Item 14 (~90 lines) + guard:
```typescript
if (task.projectId) {
  throw new Error(
    "Project phases must not enter standalone dialogue mode. " +
    "Route to Architect via ArchitectManager.relayOperatorInput().",
  );
}
```
- CREATE `src/discord/dialogue-channel.ts` (~100 lines):
```typescript
// DialogueChannelHandler.handleMessage — dispatch extension:
const activeProject = this.getActiveProjectForChannel(channelId);
if (activeProject) {
  return this.architectManager.relayOperatorInput(activeProject.id, msg.content);
}
const activeStandalone = this.getActiveStandaloneDialogueForChannel(channelId);
if (activeStandalone) {
  return this.handleStandaloneDialogue(msg);
}
// Empty-state per Critic item 22
return this.emitEmptyStatePrompt(channelId);
```
- MODIFY `src/orchestrator.ts` — `routeByResponseLevel` level 3-4 routes dialogue only if standalone; project phases route to Architect.

*Acceptance criteria:*
- Standalone dialogue: Phase 2B-3 Wave 6 behavior preserved.
- Project task: dialogue mode rejected; operator messages relay to Architect.
- Active project per channel tracked.
- Empty-state: bot prompts operator (Critic item 22).
- `!dialogue` starts standalone; `!project` starts project via Architect.

*Tests:*
- `tests/session/dialogue.test.ts`: Phase 2B-3 10 + 2 three-tier (project rejects; fall-back routing).
- `tests/discord/dialogue-channel.test.ts`: Phase 2B-3 7 + 4 three-tier (project to Architect; standalone path; channel dispatch; empty-state prompt).

*Effort:* ~15 lines `dialogue.ts` + ~25 lines `dialogue-channel.ts` + ~20 lines orchestrator. +6 tests.

**Dependencies:** Wave C, Wave B.

### Wave D: Compaction Handoff + End-to-End Project Validation (MANDATORY)

**Mandatory e2e (Critic item 7 — removed opt-in).** Gates Wave D completion.

*Files:*
- MODIFY `src/session/architect.ts` — `compact(projectId)` implementation (~80 lines):

```typescript
async compact(projectId: string): Promise<{compacted: true, newSessionId: string, generation: number} | {compacted: false, reason: string}> {
  const session = this.getSession(projectId);
  if (!session) return {compacted: false, reason: "no_active_session"};
  const costPct = session.totalCostUsd / (this.projectStore.getProject(projectId)!.budgetCeilingUsd);
  if (costPct < (this.config.compactionThresholdPct ?? 0.60)) {
    return {compacted: false, reason: "threshold_not_crossed"};
  }
  const summary = await this.requestSummary(projectId);
  // Validate summary schema (non-goals present, verbatim)
  this.validateSummary(summary, projectId);
  // Abort old session
  this.sdk.abortController(session.sessionId)?.abort();
  this.projectStore.setArchitectSummary(projectId, summary);
  // Respawn
  const respawn = await this.respawn(projectId, "compaction", summary);
  if (respawn.status === "failure") return {compacted: false, reason: respawn.error};
  return {compacted: true, newSessionId: respawn.sessionId, generation: summary.compactionGeneration};
}

private validateSummary(s: ArchitectCompactionSummary, projectId: string): void {
  const project = this.projectStore.getProject(projectId)!;
  if (!Array.isArray(s.nonGoals)) throw new Error("nonGoals missing from summary");
  const missing = project.nonGoals.filter(ng => !s.nonGoals.includes(ng));
  if (missing.length > 0) {
    throw new Error(`Compaction summary lost non-goals: ${JSON.stringify(missing)}`);
  }
  // Other schema checks...
}
```

- MODIFY `src/lib/project.ts` — `setArchitectSummary`, `compactionGeneration` handling.
- MODIFY `config/harness/architect-prompt.md` — §9 compaction-response contract with schema.
- CREATE `tests/e2e/project-lifecycle.test.ts` (~400 lines) — full project flow with mocked SDK, **now mandatory in `npm run test:integration`**.

*Validation matrix (per spec acceptance criteria):*

| Criterion | Validation |
|---|---|
| Tier-1 resolution rate >= 60% | E2E test runs **50 synthetic escalation events** (up from 20, Critic item 7); 30+ must resolve without operator notification. Fails Wave D if < 60%. |
| Multi-phase project end-to-end | E2E test with 5+ phase project completes. |
| Cost per project under ceiling | E2E test with `budgetCeilingUsd: 2.00`; total < ceiling; orchestrator-level precheck fires before ceiling breach. |
| No infinite loops | Bounded-iteration property test: reach terminal in ≤ `max_retries * max_tier1_escalations * max_phases` steps. |
| 280 Phase 2A tests pass | Full test run. |
| Architect survives full lifespan | E2E test spans 5 phases; compaction fires once; subsequent phases succeed. |
| Budget ceiling enforcement | Pathological test: phase cost breach → orchestrator aborts next spawn → `escalate_operator` with `reason: "budget_ceiling_reached"` (Critic item 7). |
| Compaction fidelity | **Separate test** from survival (Critic item 7): compact session with known non-goals → resume → assert non-goals in resumed context verbatim. |
| Forced compaction at phase 3 | Mock budget arithmetic so compaction fires during phase 3 of a 5-phase project. Assert fidelity (Critic item 7). |

*Tests:*
- `tests/session/architect.test.ts`: +5 tests — compaction fires at 60%, summary persisted, fresh session after compaction, summary-aware handleEscalation, validateSummary rejects missing nonGoals.
- `tests/lib/project.test.ts`: +2 tests — architectSummary persisted, compactionGeneration increments.
- `tests/e2e/project-lifecycle.test.ts`: 6 integration tests —
  1. full lifecycle (decompose → 5 phases → escalation resolved → review reject resolved → completion) with 50-escalation sample, asserting ≥60% tier-1 resolution
  2. forced compaction at phase 3, fidelity assertion
  3. budget ceiling breach → `escalate_operator`
  4. compaction fidelity (non-goals verbatim)
  5. bounded-iteration across mixed verdicts
  6. Architect crash mid-project → orchestrator respawn → project completes
- `tests/lib/state-bounded.test.ts` (new): 4 property tests for `review_arbitration`, project phase transitions, `tier1EscalationCount` cap, `totalTier1EscalationCount` cap.

*Acceptance criteria:*
- Compaction fires at 60% budget; summary schema validated; nonGoals verbatim preserved.
- Fresh Architect after compaction resolves escalations using summary context.
- E2E mandatory (`npm run test:integration`) passes all 6 scenarios.
- Tier-1 resolution rate ≥ 60% over 50-sample (GATE, not target).
- Budget ceiling enforcement test passes.
- Compaction fidelity test passes separately from survival test.
- All 280 Phase 2A tests green.

*Effort:* ~80 lines architect + ~10 lines project + ~50 lines prompt + ~400 lines e2e test. +17 tests.

**Dependencies:** Waves A, B, B.5, C, 6-split all landed.

---

## G. Open Questions (post-Revision 2)

All 4 BLOCKING items from Revision 1 are now resolved inline (Sections A, C). Remaining open questions are INFORMATIONAL (low-risk, can be resolved during execution). See `.omc/plans/open-questions.md` for the canonical list.

Summary (13 original — 4 resolved — 1 new for Phase 4 arch-prompt iteration = 10 remaining, all INFORMATIONAL):

1. **INFORMATIONAL — Architect session model for compaction SDK-nativization.** Currently orchestrator-driven (resolved in Section C.5). SDK-native would be preferred if/when SDK supports.
2. **INFORMATIONAL — Phase task-file naming convention.** `project-{projectId}-phase-{NN}.json`. Documentation pending.
3. **INFORMATIONAL — Concurrent projects v1.** Allowed; multi-project stress test deferred to Phase 4.
4. **INFORMATIONAL — Review session persistSession.** False; regression test in Wave A (Section A.4).
5. **INFORMATIONAL — Operator override path for reviewer rejection.** v1: manual git merge. Documented limitation.
6. **INFORMATIONAL — Architect prompt size.** Estimate 200 lines. Measure during Wave B; split if > 8k tokens.
7. **INFORMATIONAL — Standalone-dialogue → project promotion.** Spec non-goal. Operator cancels dialogue and uses `!project`.
8. **INFORMATIONAL — Arbitration verdict ambiguity (double retry rejected).** Test in Wave C asserts cap-reach → `escalate_operator`.
9. **INFORMATIONAL — Reviewer-only observation-gate (Critic item 15).** Deferred to Section L rationale.
10. **INFORMATIONAL — Architect Prompt Iteration Protocol (Critic item 24).** Target Phase 4 based on observed tier-1 resolution rate. Document as Phase 4 scope.

---

## H. Pre-Mortem (DELIBERATE mode — Architect directive 4)

Three failure scenarios with concrete mitigations baked into wave acceptance criteria.

### Scenario 1: Compaction loses non-goals → phase drift → project failure mid-flight

**Chain:** Compaction fires at phase 4 of a 5-phase project. Architect summary omits a non-goal ("do NOT change migration tooling"). Fresh Architect reads summary, encounters a phase whose spec is ambiguous about migrations, issues `retry_with_directive` that effectively reintroduces the forbidden behavior. Reviewer rejects (weighted risk high). After 2 rejections, `review_arbitration` fires. Architect arbitration retries directive — same problem, same verdict. Cap hit. Project fails mid-flight with ~$6-8 sunk cost before operator notified.

**Severity:** HIGH (wastes money, creates operator distrust).
**Likelihood:** MEDIUM (depends on Architect's summarization fidelity).

**Mitigations (baked in):**
- `ArchitectCompactionSummary` schema REQUIRES `nonGoals: string[]` field (Section C.5).
- `architect.validateSummary` (Wave D) asserts every original non-goal appears verbatim in `summary.nonGoals`; throws if missing.
- `tests/e2e/project-lifecycle.test.ts` scenario 4 (compaction fidelity): compact session with known non-goals → resume → grep resumed-context transcript for each non-goal verbatim.
- `tests/e2e/project-lifecycle.test.ts` scenario 2 (forced compaction at phase 3): fires compaction mid-project and verifies phases 4-5 complete without reintroducing forbidden behavior.
- `validateSummary` runs BEFORE respawn, so a bad summary aborts compaction rather than silently proceeding (operator sees `compaction_failed` event, can abort project cleanly with $X sunk cost rather than $X+$Y wasted retrying).

### Scenario 2: Shared `tier1EscalationCount` exhausts mid-project → operator-fatigue

**Chain:** Phase 2 has 2 failure-retries (Phase 2A escalations). Phase 5 hits first review disagreement. If `tier1EscalationCount` is shared between failure-retries and review-disagreements on a per-project basis, the cap is already exhausted, so every phase-5 review rejection escalates to operator. Operator fatigues from alerts and starts rubber-stamping.

**Severity:** MEDIUM (escalation system becomes noise).
**Likelihood:** MEDIUM (depends on phase count and failure rate).

**Mitigations (baked in):**
- **Two-counter model** (Section C.3): `tier1EscalationCount` stays per-task (preserves Phase 2A semantics); new `totalTier1EscalationCount` per-project with its own cap (default `5 × max_tier1_escalations` = 10).
- Either cap exceeded triggers escalation; they don't share a budget.
- Phase 5's first review disagreement hits per-task counter (0 → 1, under cap 2) AND per-project counter (N → N+1, under cap 10). Both under caps → Architect arbitrates. Counters grow correctly.
- `tests/orchestrator.test.ts` regression: "phase 2 failure-retries do not consume phase 5 arbitration budget".
- `tests/lib/state-bounded.test.ts`: 4 property tests including both caps.

### Scenario 3: Reviewer mandatory + budget ceiling → project fails despite working code

**Chain:** Architect decomposes project into 12 phases. Phase 6 Reviewer rejects twice. `review_arbitration` fires. Architect retries, fails, retries plan_amendment. Arbitration + respawn eats ~35% of ceiling. Phases 7-12 then cannot afford review spawns (each $0.10-0.30). Orchestrator precheck starts triggering `escalate_operator: "budget_ceiling_reached"` on every spawn. Project fails with 6 phases done and 6 blocked.

**Severity:** HIGH (wastes work; late-project failure mode).
**Likelihood:** LOW-MEDIUM (depends on phase complexity + review strictness).

**Mitigations (baked in):**
- Orchestrator-level precheck (Section C.4): catches ceiling approach BEFORE the fatal spawn. Escalates with `reason: "budget_ceiling_reached"` and exact numbers. Operator can raise ceiling by config edit and resume, or abort cleanly.
- `tests/e2e/project-lifecycle.test.ts` scenario 3 (budget ceiling): pathological test configures low ceiling, drives phase 6 into heavy arbitration, asserts subsequent spawn is prevented and escalation fires.
- Status command (`!project <id> status`, Wave 3) surfaces `totalCostUsd / budgetCeilingUsd` so operator sees the ratio climbing and can raise ceiling proactively.
- `compaction_fired` event visibility (Wave 2) signals that mid-project budget pressure is real; operator can plan ceiling bump before late-project runs dry.

---

## I. Expanded Test Plan (DELIBERATE mode — Architect directive 4)

Five layers (unit / integration / e2e / observability / metrics).

### I.1 Unit tests (per wave, in-process)

- Wave 1: +9 (tmux, settings, hooks, disallowedTools, project cleanup).
- Wave 1.5: +25 (state, project store, TaskFile ingest, routing-precedence doc-comment scan).
- Wave 2: +13 (event routing with projectId).
- Wave 3: +10 (commands, status, abort, NL, empty-state).
- Wave A: +27 (review gate, trigger, persistSession: false regression, interim warning).
- Wave B: +27 (Architect manager, spawn/respawn/decompose/compact stubs, worktree lifecycle, crash recovery stub, prompt guardrails).
- Wave 4: +5 (split path, cap check before invocation).
- Wave C: +28 (arbitration routing pure fn, resume, manager, orchestrator dispatch, fresh UUID on respawn, no-Orchestrator-import static test, interim warning removed).
- Wave 6-split: +6 (dialogue guard, channel dispatch, empty-state).
- Wave D: +7 (compaction, validateSummary, bounded).
- **Unit total: +157 tests** (up from ~144 in Revision 1).

### I.2 Integration tests (cross-module, no real Discord/SDK)

- `tests/e2e/project-lifecycle.test.ts` (Wave D): 6 scenarios.
- `tests/e2e/compaction-fidelity.test.ts` (separate from lifecycle, Wave D): 2 scenarios (verbatim preservation; post-compaction escalation).
- **Integration total: +8 scenarios.**

### I.3 E2E (mandatory; `npm run test:integration` target)

- Scenario 1: Full 5-phase project, 50 synthetic escalations, asserts ≥60% tier-1 resolution rate (GATE).
- Scenario 2: Forced compaction at phase 3; fidelity assertion.
- Scenario 3: Budget ceiling breach pathological test.
- Scenario 4: Compaction fidelity (standalone from lifecycle scenario).
- Scenario 5: Bounded-iteration with mixed verdicts.
- Scenario 6: Architect crash mid-project → respawn → completion.

**Gates Wave D completion.** Non-negotiable.

### I.4 Observability tests

- `tests/discord/notifier.test.ts` — every new event routes to correct channel with `projectId` present.
- `tests/discord/commands.test.ts` — `!project <id> status` output format matches spec in Wave 3.
- Event-log audit test (Wave D): JSONL log for a completed project contains `project_declared`, `project_decomposed`, `architect_spawned`, one `compaction_fired`, ≥1 `arbitration_verdict`, `project_completed`. Missing event fails audit.

### I.5 Metrics tests

- Tier-1 resolution rate measured in Wave D e2e scenario 1.
- Per-project cost variance: `tests/lib/project.test.ts` computes `totalCostUsd` across a simulated project; asserts < ceiling.
- Phase failure mode distribution: e2e scenario 5 records per-phase verdict outcomes.
- `compactionGeneration` monotonic: e2e scenario 2 and 4 assert.

---

## J. Rollback / Feature Flag (Critic item 5 — CRITICAL)

**Flag:** `enable_three_tier: boolean` in `config.toml` under `[pipeline]`. Default `true` (three-tier active).

**When `enable_three_tier = false`:**

| Surface | Behavior |
|---|---|
| `!project` command | Discord bot responds: "Three-tier projects disabled in this deployment. Use `!task` or `!dialogue`." No project record created. |
| Existing task files with `projectId` | **Fail-fast at ingest** with clear error: `ValidationError("projectId task received but enable_three_tier=false; remove projectId or enable the flag")`. Does NOT auto-migrate — explicit operator decision required. |
| In-flight projects (upgrade scenario with active state) | Orphan-recovery path (same pattern as `state.ts:124` B7 unknown-state → `failed`): on startup, any task with `projectId` + non-terminal state is marked `failed` with `reason: "three_tier_disabled"`. ProjectStore entries marked `state: "failed"` with same reason. Worktrees cleaned per `failProject`. |
| State schema | Unchanged. `review_arbitration` state stays in `TASK_STATES`; `projectId` etc. stay in `KNOWN_KEYS`. No migration needed on flag flip. |
| `review_arbitration` orphan recovery | On startup, any task in `review_arbitration` with flag-off → `failed` per B7 pattern. One-time WARN log per orphan. |
| Architect worktrees | If present on disk and flag is off, cleaned up on startup via `initialCleanup()` that walks `worktree_base/architect-*` patterns and removes (same safety as tmux cleanup). |
| Reviewer gate | Still available for standalone tasks at its Phase 2B-3 threshold-gated form. |
| Dialogue | Standalone dialogue unaffected. |

**Cost:** ~20 lines orchestrator (`if (!config.enable_three_tier) skip architect logic`), ~10 lines state.ts (orphan-recovery extension), ~5 lines commands.ts (flag check on `!project`). Total ~35 lines.

**Tests:**
- `tests/orchestrator.test.ts`: +3 tests — flag-off rejects projectId at ingest; flag-off orphan recovery marks review_arbitration → failed; flag-off `!project` returns disabled message.
- `tests/lib/state.test.ts`: +1 test — flag-off startup marks project-tagged tasks failed.
- `tests/session/manager.test.ts`: +1 test — flag-off startup cleans Architect worktrees.

**Documentation:** ADR follow-up section notes: "Flag defaults to `true` for rollout. Deployments may flip to `false` to halt three-tier mid-incident; incomplete projects fail cleanly, existing standalone tasks unaffected. Flag is operator-facing (config, not environment variable)."

---

## K. Observability Surface (Critic item 9)

In addition to the Discord event routing (Wave 2) and status command (Wave 3), Wave D adds:

**1. `!project <id> status` command (Wave 3):** On-demand. Format specified in Wave 3 acceptance.

**2. Project event-log view (Wave D):** Existing JSONL event log gains a convenience filter:

```bash
jq 'select(.projectId == "PROJECT_ID")' .harness/events.jsonl
```

All project-related events carry `projectId` (Wave 2). No new code required; this is a pattern affirmation.

**3. Project dashboard (Phase 4 follow-up):** Out of scope v1. Documented in ADR Follow-ups.

**4. Architect session-crash detection and respawn logic (Wave B + Critic item 18):** Each `processTask` cycle polls `architectSession` liveness; dead → respawn via `architectManager.respawn(projectId, "crash_recovery", summary)`. Summary reconstructed from `projectStore` state if `architectSummary` absent. Emits `architect_respawned` event with `reason: "crash_recovery"`.

**5. Architect stuck-arbitration timeout (Wave B + Critic item 20):** `arbitrationTimeoutMs` (default 300s) on `handleEscalation`/`handleReviewArbitration`. Timeout → verdict `{type: "escalate_operator", rationale: "architect_timeout"}`. Emits `arbitration_verdict` with that rationale.

**6. Operator project-abort signal (Wave 3 + Critic item 21):** `!project <id> abort` terminates all phase tasks, aborts Architect session, cleans worktrees, emits `project_aborted`.

---

## L. Reviewer Observation-Only Graduation (Critic item 15) — Deferred with rationale

Critic suggests shipping Wave A's project-Reviewer-mandatory as observation-only (emit `review_arbitration_entered` but don't block merge) for first N project runs, then promoting to hard gate.

**Planner assessment:** Defer to post-Wave D follow-up rather than bake into v1.

**Rationale:**
- Spec LOCKS "Reviewer mandatory for project phase merge". Observation-only would partially invalidate the lock.
- Three-tier's entire value proposition is Architect-arbitration of review disagreements. Observation-only skips the core loop; Wave D validation becomes vacuous.
- Graduation pattern from Phase 2A was for *informational signals with known calibration gaps* (response levels, checkpoints). Reviewer's reject criteria are prompt-defined with clearer semantics.
- Rollback for a bad reviewer calibration is cleaner via `enable_three_tier = false` (Section J) than via a half-gating mode.

**Follow-up:** If post-Wave D data shows Reviewer reject rate > acceptable threshold (TBD at Phase 4, candidate > 40% on project phases), consider adding `reviewer.observation_only: boolean` as a per-project override. Tracked as Phase 4 scope.

---

## M. File Manifest Totals

### New source files (17)

| File | Wave | Lines (est) |
|------|------|-------------|
| `src/discord/types.ts` | 2 | 30 |
| `src/discord/notifier.ts` | 2 | 140 |
| `src/discord/sender.ts` | 2 | 60 |
| `src/discord/accumulator.ts` | 3 | 50 |
| `src/discord/commands.ts` | 3 | 185 |
| `src/discord/client.ts` | 3 | 40 |
| `src/discord/escalation-handler.ts` | 4 | 130 |
| `src/discord/classify.ts` | 4 | 40 |
| `src/discord/dialogue-channel.ts` | 6-split | 100 |
| `src/gates/review.ts` | A | 120 |
| `src/session/dialogue.ts` | 6-split | 105 |
| `src/session/architect.ts` | B, D | 340 |
| `src/lib/project.ts` | 1.5, D | 210 |
| `src/lib/arbitration.ts` | C | 80 |
| `config/harness/review-prompt.md` | A | 100 |
| `config/harness/architect-prompt.md` | B | 200 |
| `tests/e2e/project-lifecycle.test.ts` | D | 400 |

### Modified source files (7)

| File | Waves | Cumulative changes |
|------|-------|--------------------|
| `src/session/sdk.ts` | 1 | +settings, +hooks, +enabledPlugins (~20) |
| `src/session/manager.ts` | 1, 4-ext, B, C | +disallowedTools, +tmuxOps, +plugin config, +cleanupProject, +Architect worktree removal, +resumeTask (~100) |
| `src/lib/config.ts` | 1, A, B, 6-split | +plugins, +review, +architect, +dialogue_channel, +project, +enable_three_tier (~70) |
| `src/lib/state.ts` | 1.5, J | +fields, +review_arbitration, +orphan recovery for flag-off (~60) |
| `src/orchestrator.ts` | 1.5, 2, 4, A, B, C, 6-split, J | +resolveEscalation, +review routing, +dialogue routing, +routeByProject, +applyArchitectVerdict, +events, +TaskFile extensions, +budget precheck, +Architect respawn polling, +flag check (~250) |
| `src/discord/commands.ts` | 3, 6-split | +!project, +!project status, +!project abort, +dialogue dispatch, +flag check (~50) |
| `src/session/sdk.ts` | C (resumeTask wrapper) | (already counted above) |

### Modified test files (5)

| File | Waves | Cumulative new tests |
|------|-------|----------------------|
| `tests/session/sdk.test.ts` | 1 | 6 |
| `tests/session/manager.test.ts` | 1, C, J | 10 (+3 resume, +1 flag-off cleanup) |
| `tests/lib/state.test.ts` | 1.5, J | 6 (+1 flag-off orphan) |
| `tests/orchestrator.test.ts` | A, B, C, 6-split, 4-ext, J | 39 (Phase 2B-3 10 + three-tier 26 + flag 3) |
| `tests/discord/commands.test.ts` | 3, 6-split | 22 (+4 dialogue dispatch) |

### New test files (15)

| File | Wave | Tests (est) |
|------|------|-------------|
| `tests/discord/notifier.test.ts` | 2 | 25 |
| `tests/discord/sender.test.ts` | 2 | 7 |
| `tests/discord/accumulator.test.ts` | 3 | 8 |
| `tests/discord/client.test.ts` | 3 | 5 |
| `tests/discord/escalation-handler.test.ts` | 4 | 19 |
| `tests/discord/classify.test.ts` | 4 | 5 |
| `tests/discord/dialogue-channel.test.ts` | 6-split | 11 |
| `tests/gates/review.test.ts` | A | 11 |
| `tests/session/dialogue.test.ts` | 6-split | 12 |
| `tests/session/architect.test.ts` | B, C, D | 31 |
| `tests/lib/project.test.ts` | 1.5, D | 20 |
| `tests/lib/arbitration.test.ts` | C | 14 |
| `tests/lib/state-bounded.test.ts` | D | 4 |
| `tests/e2e/project-lifecycle.test.ts` | D | 6 scenarios |
| `tests/e2e/compaction-fidelity.test.ts` | D | 2 scenarios |

### Totals

| Metric | Count |
|--------|-------|
| New source files | 17 (12 Phase 2B-3 + 5 three-tier: `architect.ts`, `project.ts`, `arbitration.ts`, `architect-prompt.md`, plus e2e test files listed separately) |
| Modified source files | 7 |
| New test files | 15 |
| Modified test files | 5 |
| New source lines (est) | ~2,430 (~1,115 Phase 2B-3 + ~1,315 three-tier, incl. rollback + observability) |
| Modified source lines (est) | ~550 |
| New test count (est) | ~180 unit + 8 integration + 6 e2e scenarios + 2 fidelity scenarios = ~196 test entries |
| New runtime dependencies | 1 (`discord.js@^14.x` from 2B-3) |
| Prompt files new | 2 (`review-prompt.md`, `architect-prompt.md`) |
| Prompt files modified | 1 (`system-prompt.md` dialogue contract addition) |
| Spike scripts | 1 (`scripts/verify-plugin-loading.ts`, deleted after Wave 1) |
| Existing tests preserved | 280 (Phase 2A — must all stay green) |
| Total tests after Wave D | ~476 |

### Wave dependencies (final)

```
Wave 1 → Wave 1.5 → Wave 1.75 ─┬→ Wave 2 → Wave 3 ─┬→ Wave A ─┐
                                │                   │          ├→ Wave B → Wave B.5 gate → Wave 4 → Wave C → Wave 6-split → Wave D
                                │                   └─(parallel)┘
                                └─(nothing else until 2)
```

Critical path: 1 → 1.5 → 1.75 → 2 → 3 → A → B → B.5 → 4 → C → 6-split → D (12 waves, B.5 is a gate).

---

## N. Appendix: Self-check against Planner Final Checklist

- [x] Only preference/scope/risk questions asked of user. Codebase facts resolved by planner.
- [x] Plan has 12 actionable waves each with acceptance criteria.
- [x] User explicitly requested plan via `/plan --consensus` / ralplan trigger.
- [x] Plan saved to `.omc/plans/ralplan-harness-ts-three-tier-architect.md`.
- [x] Open questions persisted to `.omc/plans/open-questions.md` with Resolved section + remaining INFORMATIONAL.
- [x] RALPLAN-DR summary present (Section E): 5 principles, top-3 drivers, 4 options with explicit invalidation rationale.
- [x] ADR present: Decision, Drivers, Alternatives, Why chosen, Consequences, Follow-ups.
- [x] Mode DELIBERATE: pre-mortem (Section H) + expanded test plan (Section I).
- [x] Rollback section present (Section J).
- [x] Observability surface documented (Section K).
- [x] Supersedes note written to phase2b-3 plan per Critic item 14.
- [x] Test count corrected 273 → 280 throughout.
- [x] `lib/arbitration.ts` is pure (returns action, orchestrator applies — Wave C).
- [x] Routing precedence table documented as doc-comment for Wave 1.5b (Section C.2).
- [x] 4 BLOCKING open questions resolved inline (Sections A.1, A.3, C.1, C.4).

*End of Revision 2 planner draft. Awaiting Architect + Critic re-review.*

---

## Section M: Spike Validation (Post-Approval Empirical Evidence)

**Added 2026-04-23 after post-approval empirical spike.** The spike harness at `harness-ts/spikes/architect-spike/` validated the locked design across 5 variants before any Wave execution began. Findings clarify several open questions and DOWNGRADE one major risk.

### M.1 Spike Matrix (5 variants × 5-10 escalations each)

| # | Variant | Resolution | Avg cost/call | Avg latency | Status |
|---|---------|-----------|---------------|-------------|--------|
| 1 | Bare opus ephemeral, 5 escalations | 60% | $0.526 | 20.8s | Opus over-spec'd; blew cost threshold |
| 2 | Bare sonnet ephemeral, 5 escalations | 80% | $0.096 | 28.7s | Cheap baseline |
| 3 | Sonnet + OMC ephemeral, 5 escalations | 80% | $0.149 | 44.8s | OMC dead weight — zero Task subagent invocations |
| 4 | Sonnet + caveman ephemeral, 5 escalations | 80% | $0.106 | 37.5s | Caveman bypasses JSON output; marginally worse |
| 5 | **Bare sonnet PERSISTENT, 10 escalations** | **90%** | **$0.058** | **37.1s** | **Winner** — cross-call memory verified, prompt caching reduces cost 40% vs ephemeral |

**Total spike cost:** $4.96

### M.2 Locked Model + Plugin Decisions (Wave B configuration)

```toml
[architect.arbitration]   # hot path — every escalation
model = "claude-sonnet-4-6"
max_budget_usd = 1.0       # per call (avg observed: $0.058, p99: $0.09)
max_turns = 10             # observed turns per call: 2 consistently
enabled_plugins = []       # bare — OMC + caveman both empirically counterproductive
persistSession = true      # persistent mode validated
disallowed_tools = ["Bash","Edit","WebFetch","WebSearch","CronCreate","CronDelete","CronList","RemoteTrigger","ScheduleWakeup","TaskCreate"]
allowed_tools = ["Read","Write"]  # minimal — JSON output contract only
```

```toml
[architect.decomposition]  # cold path — once per project, UNVALIDATED
model = "claude-sonnet-4-6"
max_budget_usd = 5.0
enabled_plugins = ["oh-my-claudecode@omc"]  # validation deferred to Wave B smoke test
```

Rationale:
- **Model:** sonnet beats opus on quality and cost in spike; opus 5.5× more expensive for equal or worse arbitration.
- **No OMC for arbitration:** across 5 OMC-enabled runs, Architect invoked 0 Task subagents. Plugin context inflates system prompt +55% cost with no verdict improvement.
- **No caveman:** caveman compresses prose, not structured tool-writes. Arbitration output is JSON via Write tool — caveman's rules exempt it. Adds turns without compressing output. +10% cost, higher latency.
- **Persistent mode:** cross-call memory demonstrably informs subsequent arbitrations (see M.4). SDK prompt caching makes persistent **cheaper per call** than ephemeral, not more expensive.

### M.3 Wave 1 Item 1 Blocker: RESOLVED

`settings.enabledPlugins: {"oh-my-claudecode@omc": true}` with `settingSources: ["project"]` successfully loads OMC plugin + skills + subagents. Verified in spike v3 (sonnet+OMC). Empirical spike script at `harness-ts/spikes/architect-spike/run.ts` can be reused as the Phase 2B-3 Wave 1 Item 1 verification script — rename to `scripts/verify-plugin-loading.ts` if desired, or delete since this plan now incorporates the finding.

### M.4 Cross-Call Memory Empirically Validated

Spike v5 (persistent, 10 calls, 1 project) included 4 phases designed to test whether Architect references prior arbitration decisions by phase ID:

- **p5 → p2 + p4:** Architect directive cited *"The p2 decision (per-alert mapper) does not formally constrain p5..."* and *"Bulk batching is a backfill concern (p4, already done)."* Correctly recalled both prior decisions.
- **p7 → p4:** Architect detected Executor completion signal that implied p4 chose Option B, but Architect held ground truth that p4 actually directed Option A. Directive: *"the snapshot-consistency test is premised on phase 4 choosing Option B... but the p4 arbitration directed Option A — that test is validating behavior that does not exist in the implementation and will false-pass."* Fresh Arbiter could not catch this without reading all prior arbitration files.
- **p9 → p6:** Architect recalled *"Phase 6 chose feature-flag per-rule gradual cutover"* and applied it directly to p9's in-flight question.
- **p10 → p1:** Architect recalled p1's `@pagerduty/pdjs` library choice and directed test coupling accordingly.

**Cross-call memory is not bias accumulation — it's accurate project context informing tighter directives.** Empirical refutation of the "ephemeral split" contrarian proposal that was considered and rejected post-data.

### M.5 Section C.5 Compaction — PRIORITY DOWNGRADE

Plan's Section C.5 (compaction mechanism) was treated as load-bearing because "Architect session context growth was unbounded." Spike v5 data changes the calculus:

- Per-call cost at persistent mode: $0.03–$0.09 (calls 2–10 all cached well)
- 10 calls cumulative: $0.58
- 28-phase Ozy-scale projection (~56 arbitrations): ~$3.25 arbitration cost
- Plus one decomposition (~$3–5): ~$6–8 total per project

Compaction still matters for context-budget hygiene (sonnet max context ~200k tokens). But it is NOT load-bearing against cost or quality degradation. Plan's Section C.5 stays as designed but priority drops from "MUST-FIX pre-execution" (Critic iteration 1) to "operational hygiene, validate in Wave D."

**Pre-mortem Scenario 1 (compaction drops non-goals → silent drift) remains relevant** — Wave D's compaction fidelity test + `validateSummary` verbatim non-goals assertion still applies. Just less urgent than previously scoped.

### M.6 Authority Discipline Validated

Across 25 total spike arbitrations (5 × 5-6 escalations), Architect issued:
- 0 `executor_correct` verdicts (doesn't exist; retry-only enforced)
- 16 `retry_with_directive`
- 0 `plan_amendment` (none of the spike scenarios required spec change)
- 9 `escalate_operator`

Retry-only authority held under pressure. Opus variant (v1) showed strictest discipline (escalated on plan ambiguity at e5 instead of second-guessing Reviewer). Sonnet variants showed slightly more confidence at correcting Reviewer factual errors in rationale, but NEVER issued override verdicts. Authority boundary preserved.

### M.7 Spike Regression Harness Designation

The spike directory `harness-ts/spikes/architect-spike/` is preserved as a regression harness for Wave B implementation. It contains:

- `prompt.md` — candidate production Architect systemPrompt (Wave B may refine)
- `escalations.json` — 5 mixed ephemeral scenarios (ArbitrationAction coverage)
- `escalations-persistent.json` — 10-phase coherent project with cross-call memory tests
- `run.ts` — runner supporting ephemeral + persistent modes, plugin flags
- `results-{variant}/` — archived results from all 5 spike runs
- `run-{variant}.log` — raw stdout per variant

**Wave B acceptance criteria additions (per 2a designation):**
- Wave B Architect implementation MUST pass all 10 escalations of `escalations-persistent.json` via the Wave B `ArchitectManager` (not the spike runner) with:
  - Resolution rate ≥ 80% (within 10% of spike v5's 90%)
  - Total cost ≤ $1.00 (≥40% headroom over spike's $0.58)
  - 0 parse errors
  - Cross-call memory demonstrated on ≥ 3 of 4 memory-test phases (p5, p7, p9, p10)
- Wave B Architect implementation MUST pass all 5 escalations of `escalations.json` via ephemeral one-shot invocation with resolution rate ≥ 60%.

Spike harness is the regression suite. Any change to architect-prompt.md or ArchitectManager re-runs these 15 scenarios before ship.

### M.8 Unvalidated: Decomposition Mode

Spike only tested arbitration mode (hot path). Decomposition (one-shot per project) is NOT spike-validated. Plan Wave B smoke gate (Section F lines 1012-1035) remains the first-validation checkpoint for decomposition — 5 mock escalations must resolve ≥3 to proceed. Architect prompt for decomposition mode is TBD, will be drafted in Wave B alongside arbitration prompt.

If operator wants additional pre-Wave-B confidence in decomposition, spike v6 can be added to `spikes/architect-spike/` — feed one project description, measure phase breakdown quality + whether OMC `/team`/`/ralplan` invocation materially improves decomposition. Not blocking execution.

### M.9 Budget Reality Check Against Ozy Scale

Concrete economics for a representative 28-phase Ozy-scale project:

| Component | Est calls | Avg cost | Subtotal |
|-----------|----------|----------|----------|
| Architect decomposition | 1 | $3.00 | $3.00 |
| Architect arbitrations | 56 (2/phase × 28) | $0.058 | $3.25 |
| Executor sessions | 28 | ~$0.50 | $14.00 |
| Reviewer sessions | 56 (2/phase avg) | ~$0.30 | $16.80 |
| **Total per project** | | | **~$37** |

Per-project budget ceiling default (`10 × max_budget_usd = $10`) needs revision. Recommend `$50` default for project ceiling. Plan Section C.4's budget precheck logic is correct; default numbers need adjustment based on spike data.

**Config amendment:** `project.budget_ceiling_usd` default changes from `10 × max_budget_usd` to `$50` absolute for Wave B.

### M.10 Summary of Plan Amendments from Spike

| Section | Change |
|---------|--------|
| C.5 (compaction) | Priority downgrade — operational hygiene, not load-bearing |
| C.4 (budget ceiling) | Default ceiling revised from `10 × max_budget_usd` to `$50` absolute |
| Wave B config | Lock `model = "claude-sonnet-4-6"`, `enabled_plugins = []` for arbitration |
| Wave B config | Separate decomposition config with OMC (UNVALIDATED — smoke gate validates) |
| Wave B acceptance | Must pass spike regression harness (15 scenarios, see M.7) |
| Wave B.5 smoke gate | Unchanged — still the decomposition-mode validation |
| Wave D | Compaction fidelity test DEFERRED from mandatory to operational |
| Pre-mortem Scenario 1 | Remains, but less urgent |
| Wave 1 Item 1 | RESOLVED empirically (M.3) |

No wave restructuring. No new waves. Total plan content amended: ~40 lines across sections, plus this Section M (~200 lines documentation).

---

### M.11 Reviewer Spike Validation (Pre-Wave-A)

**Added 2026-04-23 after Reviewer tier empirical validation.** Spike at `harness-ts/spikes/reviewer-spike/` validated Reviewer configuration before Wave A lands. 4 variants × 5 scenarios = 20 reviews, $3.78 total.

#### M.11.1 Spike Matrix

| Variant | Accuracy | Avg cost | Avg latency | Schema compliance | Subagents invoked |
|---------|----------|----------|-------------|-------------------|--------------------|
| bare sonnet ephemeral | 5/5* | $0.176 | 87.6s | 5/5 | N/A |
| sonnet + OMC | 5/5* | $0.221 | 99.4s | 5/5 | **0** |
| sonnet + caveman | 5/5* | $0.169 | 72.6s | 4/5 (s1 threshold slip) | N/A |
| sonnet + OMC + caveman | 5/5* | $0.189 | 72.5s | 5/5 | **0** |

\*All variants caught every planted defect across s2-s5 (off-by-one, SQL injection, O(n²), test coverage gap). s1 verdict disagreement resolved below.

#### M.11.2 Scenarios Tested

5 planted-defect review inputs:
- **s1** `clean_trivial_pr` (expected approve, designed as false-positive trap)
- **s2** `correctness_bug_off_by_one` — pagination spec says 1-indexed, code uses `skip = page * pageSize`
- **s3** `security_sql_injection` — `$queryRawUnsafe` with string-concat user input
- **s4** `performance_quadratic` — O(n²) nested loop + `.includes()` on 100k-row criterion
- **s5** `test_coverage_gap` — 1 test for 6 criteria + real memory leak in impl

Each variant used `permissionMode: "bypassPermissions"` with `allowedTools: ["Read", "Grep", "Write"]`. `Write` restricted to `.harness/review.json` output.

#### M.11.3 Critical Finding: Reviewer Escapes Sandbox via Read + Grep (BY DESIGN)

s1 was designed as a false-positive trap: trivial README addition, expected `approve`. All 4 variants issued `request_changes` — but **not falsely**. Reviewer used `Read` + `Grep` to inspect the real harness-ts repo (cwd was empty sandbox, but filesystem access extends up) and discovered Executor's stated assumption was wrong: `npm run lint:fix` does not exist in `package.json`. Reviewer correctly flagged that merging the README would ship documentation for a nonexistent command.

**This is production-desired behavior.** Reviewer does not blindly trust the Executor's "I assumed X" statements — it verifies against reality. Wave A Reviewer will have same Read + Grep access by design. Document explicitly in Wave A:

> Reviewer has read-only filesystem access to the entire repository during review, not just the diff. It MAY and SHOULD investigate Executor's completion-signal assumptions against actual file state. The diff is the focal point of review, but not the boundary of investigation.

Turn count evidence: s1 averaged 7 turns across variants (vs 3-4 for s2-s5) due to filesystem investigation. Extra latency well-spent.

#### M.11.4 OMC Subagents: Dead Weight for Reviewer (Same Pattern as Architect)

Across 10 OMC-enabled runs (5 × OMC variant + 5 × both variant), **zero Task tool invocations.** No `security-reviewer`, `code-reviewer`, `test-engineer`, or `analyst` spawn occurred — even on:
- s3 SQL injection (textbook security-reviewer trigger)
- s4 O(n²) against 100k criterion (code-reviewer/analyst trigger)
- s5 test coverage gap (test-engineer trigger)

Sonnet parent judges specialist subagents unnecessary for single-mode verdict reasoning. Plugin context inflates per-turn cost:
- bare: $0.042/turn
- caveman: $0.040/turn
- OMC: $0.058/turn (+38% overhead)
- both: $0.053/turn (+26% overhead)

Mirrors Architect spike exactly (Section M.1 variant 3). **OMC is not useful for structured-JSON-verdict agents.** Remove from Reviewer config.

#### M.11.5 Caveman Schema Slip on s1

caveman variant s1 issued `request_changes` despite:
- riskScore weighted 0.1075 (below 0.25 approve threshold)
- 0 critical findings (all warning/note severity)
- 3/3 acceptance criteria met

Prompt threshold rule: "Below 0.25 → approve unless critical finding forces lower threshold." caveman variant bypassed this logic — reasoning trace compressed, rule not applied. Both variant (caveman + OMC) did not have this problem; OMC's extra context preserved schema logic.

**Not a kill signal** (1 of 20 verdicts slipped) but documents caveman risk: compression bypasses threshold reasoning on edge cases.

#### M.11.6 Locked Wave A ReviewGate Configuration

```toml
[reviewer]
model = "claude-sonnet-4-6"
max_budget_usd = 0.5                  # headroom over observed $0.18 avg
max_turns = 10                         # observed 3-8 turns
enabled_plugins = []                   # bare — OMC dead weight, caveman schema risk
persistSession = false                 # ephemeral per review (plan Section C.5)
permission_mode = "bypassPermissions"
allowed_tools = ["Read", "Grep", "Write"]
disallowed_tools = [
  "Edit", "Bash", "WebFetch", "WebSearch",
  "CronCreate", "CronDelete", "CronList",
  "RemoteTrigger", "ScheduleWakeup", "TaskCreate"
]
```

Alternative for operator choosing lower cost/latency: `enabled_plugins = ["oh-my-claudecode@omc", "caveman@caveman"]` (the "both" variant) — 5/5 schema-compliant, 18% faster, 7% more expensive than bare. Marginal difference; bare is safer baseline.

#### M.11.7 Reviewer Spike Regression Harness

Spike directory `harness-ts/spikes/reviewer-spike/` preserved as Wave A regression harness alongside architect-spike.

Wave A `ReviewGate` implementation MUST pass:
- All 5 scenarios produce schema-valid `review.json`
- 0 parse errors
- Verdict matches expected for s2 (reject/request_changes), s3 (reject/request_changes), s4 (request_changes), s5 (request_changes)
- s1 verdict `request_changes` accepted as correct (captures real Executor assumption mismatch)
- Avg cost ≤ $0.30 per review (bare variant observed $0.176)
- Avg latency ≤ 90s (bare variant observed 87.6s)
- Retry-only-to-approve-equivalent authority: 0 invalid verdict types (no `conditional_approve`, no `approve_with_notes`, etc.)

#### M.11.8 Prompt Refinement for Wave A

Observation: prompt rule "Below 0.25 → approve unless critical" + "Each finding must be specific/actionable" created an ambiguity. Reviewer on s1 found a real issue (nonexistent command), classified it as `warning` severity (not critical), computed low risk, yet blocked merge via `request_changes`.

Wave A architect-prompt drafting should clarify:
- If a finding is genuinely blocking, upgrade its severity to `critical` (which forces threshold override).
- If a finding is not blocking, do not let it alone force `request_changes` — issue `approve` with the finding as a `note`-severity observation for post-merge follow-up.
- `request_changes` requires at least one `critical` or `warning` severity finding whose fix is a prerequisite for safe merge.

Not blocking for Wave A implementation; surfaced as an open refinement.

#### M.11.9 Plan Amendments from Reviewer Spike

| Section | Change |
|---------|--------|
| Wave A (line 787) | Lock Reviewer config per M.11.6. Document Read/Grep sandbox-escape behavior per M.11.3. |
| Wave A acceptance criteria | Add regression-harness gate per M.11.7 |
| Section C (routing) | Reviewer configured for ephemeral-per-review (unchanged from plan) |
| Architect prompt drafting | Apply severity-upgrade refinement per M.11.8 to both Architect AND Reviewer prompts |

No wave restructuring. No new dependencies.

#### M.11.10 Executor Spike Still Deferred

Reviewer spike completes the pre-Wave-A validation scope. Executor spike intentionally deferred because:
- Executor already ships in Phase 2A with Wave 1's DEFAULT_PLUGINS (OMC + caveman) wired by default
- Executor task shape (read, edit, bash, git, test) is too open-ended for representative synthetic scenarios
- Better signal comes from real tasks through Wave 2-enabled Executor (Discord visibility) — observational, not synthetic

Revisit Executor spike only if production observations show Wave-1-enabled Executor underperforming (e.g., OMC or caveman causing measurable quality or cost issues in real tasks).

---

### M.12 Decomposer Spike + M.13 Tracker Bug Acknowledgment

**Added 2026-04-24 after empirical validation of Architect decomposition mode AND discovery of measurement methodology bug affecting all prior spike claims about subagent invocations.**

#### M.12.1 Decomposer Spike Matrix

Spike at `harness-ts/spikes/decomposer-spike/` — 4 variants × 2 projects (medium: redis-job-queue; large: postgres→cockroach) + 2 supplementary runs (forced-delegation large, verification probe, re-baseline).

| Variant | Med phases | Lrg phases | Total cost | Avg latency | Subagents (fixed tracker) |
|---------|-----------|-----------|-----------|-------------|---------------------------|
| bare | 7 | 18 | $0.67 | 223s | N/A |
| +caveman | 7 | 18 | $0.92 (+38%) | 353s (+58%) | N/A |
| +OMC | 6 | 19 | $0.75 (+12%) | 209s | 2 generic Agent calls (no subagent_type) |
| +both | 6 | 18 | $0.74 (+11%) | 207s | 0 (not re-run with fixed tracker) |
| +OMC+forced-delegation (large only) | — | 21 | $1.36 | 764s | **2 OMC specialists** (oh-my-claudecode:architect + critic) |

Forced-delegation variant's plan showed genuine quality uplift: caught CockroachDB serializable-semantics retry requirement (bare missed), dark-read validation phase (bare missed), observability as first-class phase concern.

Additional decomposer cost: ~$6 (original 4 variants $3.09 + forced-delegation $1.36 + fixed-tracker re-run $1.35 + verification probe $0.27).

#### M.12.2 Graduated Decomposer Configuration

Match rigor to complexity:

| Project scale | Strategy | Prompt | Plugins | Est cost |
|--------------|----------|--------|---------|----------|
| Small (≤5 phases) | Single-pass | standard | bare | $0.20-0.40 |
| Medium (6-15 phases) | Reflection prompt | "reflect on structural gaps" | bare | $0.60-1.30 |
| Large (16+ phases) | Forced delegation | MANDATORY directive naming `oh-my-claudecode:architect` + `oh-my-claudecode:critic` | OMC enabled | $1.30-3.00 |
| Ambiguous declaration | Decline + escalate | refuse decomposition, recommend operator run `/deep-interview` first | none | $0.10 |

**Key rule:** Architect decomposer does NOT invoke deep-interview itself. Operator clarification lives upstream (Wave 6-split Dialogue channel). Architect consumes crystallized spec.

#### M.12.3 Ralplan Integration for Large Projects

For large-project decomposition, Architect invokes `Skill({skill: "oh-my-claudecode:ralplan"})` for formal consensus cycle — matches the pattern used to build THIS plan. Architect owns final plan.json format; ralplan provides thinking, Architect provides authorship.

Guard against recursion: ralplan invoked from Architect must NOT invoke ralplan recursively (explicit prompt directive in ralplan invocation).

Not yet tested — Skill tool invocation of ralplan from Architect session is Wave B.5 validation item.

---

### M.13 Measurement Methodology Bug + Re-Baseline

#### M.13.1 The Bug

Three spike suites ran with inadequate or incorrect subagent-invocation tracking:

- **architect-spike/run.ts:** no tool_use tracking code at all. "0 invocations" claims in M.2 were asserted without measurement.
- **reviewer-spike/run.ts:** no tool_use tracking code at all. "0 invocations" claims in M.11 were asserted without measurement.
- **decomposer-spike/run.ts:** tracker code present but looked for tool name `"Task"` — actual runtime name is `"Agent"`. Missed every invocation.

Claims of "0 subagent invocations across N runs" in Sections M.2, M.11, M.12 were either fabricated (no measurement) or bugged (wrong tool name).

#### M.13.2 Fix + Re-Baseline

Tracker patched across all three spikes (`name === "Task" || name === "Agent"`). Re-ran critical OMC-enabled variants with corrected tracker:

| Re-run | Result | Prior claim | Verdict |
|--------|--------|-------------|---------|
| Arbiter ephemeral+OMC (5 escalations) | **0 invocations** | 0 (fabricated) | Claim validated — arbiter doesn't use OMC specialists |
| Reviewer OMC (5 scenarios) | **0 invocations** | 0 (fabricated) | Claim validated — reviewer doesn't use OMC specialists |
| Decomposer bare+OMC large (1 run) | 2 generic Agent calls (no subagent_type) | 0 (buggy tracker) | Corrected — sonnet uses generic Agent for self-review, NOT OMC specialists |
| Decomposer forced+OMC large (1 run) | 2 OMC-specialist calls (oh-my-claudecode:architect + critic) | 0 (buggy tracker) | Corrected — explicit OMC-named directive DOES drive specialist invocation |

Re-baseline cost: ~$3.80.

#### M.13.3 Refined OMC Value Thesis

**Single-mode structured-verdict agents (arbiter, reviewer):**
- Don't invoke OMC subagents unprompted
- Don't invoke OMC subagents even when prompt hedges "invoke if material improvement" (prompt interpretation too strict)
- Generic Agent tool not invoked either
- **OMC plugin = pure overhead for these roles**. Lock bare config.

**Divergent-reasoning agents (decomposer):**
- May invoke generic Agent tool for self-directed review (observed on bare+OMC large)
- Don't invoke OMC specialists unprompted
- DO invoke OMC specialists when prompt explicitly names them (`subagent_type: "oh-my-claudecode:architect"`)
- Forced-delegation prompt drives specialist invocation + measurable quality uplift
- **OMC plugin earns keep WHEN paired with explicit OMC-subagent-named directive**

**Executor (not yet spiked):**
- Behavior unknown. Likely divergent-reasoning pattern given task shape (implement + test + iterate)
- Defer to real-task observation via Wave 2-enabled Discord visibility

#### M.13.4 Locked Configurations (Validated)

| Agent tier | Plugins | Prompt strategy | Basis |
|-----------|---------|-----------------|-------|
| Arbiter | bare | systemPrompt only | M.2 + M.13 validation |
| Reviewer | bare | systemPrompt only | M.11.6 + M.13 validation |
| Decomposer — small | bare | systemPrompt only | M.12.2 |
| Decomposer — medium | bare | systemPrompt + reflection directive | M.12.2 |
| Decomposer — large | OMC | systemPrompt + forced-delegation directive naming `oh-my-claudecode:architect` + `critic` | M.12.2 + spike v5 |
| Executor | OMC + caveman (Wave 1 default) | systemPrompt from config | Unvalidated, observation-based |

#### M.13.5 Honest Accounting

Prior "OMC dead weight" conclusion for arbiter + reviewer **happens to be correct** despite fabricated measurement. Lucky. Validated on re-baseline.

Prior decomposer finding of "0 invocations" was **wrong in detail** (invocations did happen) but **directionally correct** — OMC specialists don't invoke without explicit prompt directive.

Wave A + Wave B config locks (bare for arbiter + reviewer) STAND. Wave B decomposer config (OMC+forced for large) is new + now documented.

#### M.13.6 Prompt Refinement Still Outstanding (from M.11.8)

Reviewer's threshold rule ("below 0.25 → approve unless critical") continues to produce edge-case violations: re-baseline s1 issued `request_changes` with risk 0.13 + 0 critical. Severity-upgrade refinement from M.11.8 still applies to Wave A prompt drafting. Not blocking, not measured as blocker.

#### M.13.7 Cumulative Spike Cost (Corrected)

| Spike | Cost |
|-------|------|
| Architect (v1-v5) | $4.96 |
| Reviewer (4 variants) | $3.78 |
| Decomposer (4 variants + forced + fixed-tracker + verification) | $6.14 |
| Re-baseline (arbiter OMC + reviewer OMC with fixed tracker) | $1.81 |
| **Grand total** | **~$16.69** |

Against potential production savings from correctly-locked plan: cheap insurance.

---

*End of Revision 2 + Section M.1-M.13 spike validation and methodology-correction amendments. Architect + Reviewer tier configurations empirically validated via corrected methodology. Architect decomposer configuration defined per M.12.2 with graduated rigor. Executor configuration deferred to observation via Wave 2-enabled real tasks.*

---

### M.14 Future Spike: Parallel code-reviewer Subagents (Reviewer Tier)

**Status:** OPEN — not yet spiked.
**Origin:** Operator observation during Wave 1 live-run debrief (2026-04-24).
**Hypothesis:** Reviewer tier quality lifts meaningfully when Reviewer is allowed to fan out `code-reviewer` subagents (via OMC `Agent` tool) in parallel across review dimensions (correctness, security, performance, integration, regression). Locked M.11/M.13 Reviewer config disabled OMC because specialists never fired *unprompted* — but an explicit forced-delegation directive (per M.12 decomposer finding) could change that and surface defects the single-pass Reviewer misses.

**Why worth spiking:**
- M.11 Reviewer baseline: 80% verdict accuracy, 5-dim scoring, 0 specialist invocations. False-negative risk on edge dimensions (e.g., subtle concurrency bugs) unmeasured.
- M.12 Decomposer result: forced-delegation lifts quality (21 vs 18 phases, caught 3 defects bare missed) at ~3x cost. Applying the same pattern to Reviewer may surface the same lift.
- OMC catalog includes `code-reviewer`, `security-reviewer`, `test-engineer`, `verifier`. Each has distinct training focus — parallel fan-out could produce diverse findings bare sonnet misses.

**Design sketch:**
- Variant baseline: `claude-sonnet-4-6`, ephemeral, no OMC (current M.11 lock).
- Variant parallel: `claude-sonnet-4-6` + OMC + explicit directive: "Fan out one `code-reviewer`, one `security-reviewer`, one `test-engineer` in parallel. Aggregate findings before writing verdict."
- Reuse spike harness at `harness-ts/spikes/reviewer-spike/` — add a `--variant=parallel-specialists` mode.
- Measurement: instrument tracker (tool_use name = `Agent`, input.subagent_type) to confirm specialists fire. Compare verdict accuracy, finding coverage, false-negative rate against baseline.

**Acceptance criteria:**
- At least 3 of 5 scenarios show specialist invocation count ≥ 2 when directive is explicit (validates forced-delegation pattern applies).
- Verdict accuracy ≥ baseline (no regression) AND finding coverage strictly greater on ≥ 2 scenarios (net lift).
- Cost ≤ $0.70/review (3x baseline ceiling; if higher, gate to high-risk tasks only).
- Latency ≤ 180s (parallelism should keep latency close to baseline despite more agents).

**Blockers:**
- Reviewer tier itself not yet built (Wave A).
- Build order: Wave A (Reviewer ephemeral baseline) → this spike as Wave A+ validation → lock parallel config (or discard).

**Expected outcome:** Either
- (a) Parallel specialists fire, lift finding quality → promote `parallel-specialists` to locked Reviewer config for high-risk tasks (mode: "reviewed" in task file), keep single-pass as default; OR
- (b) Specialists still don't fire with explicit directive, or findings overlap with bare sonnet → keep M.13.4 locked config, close spike with null result.

Track in plan Wave A completion criteria.

