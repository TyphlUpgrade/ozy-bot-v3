# Deep Interview Spec: Three-Tier Architect-Executor-Reviewer Harness

## Metadata
- Interview ID: harness-three-tier-2026-04-23
- Rounds: 6
- Final Ambiguity Score: 19%
- Type: brownfield (extends `harness-ts/`, Phase 2A complete)
- Generated: 2026-04-23
- Threshold: 20%
- Status: PASSED

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.92 | 0.35 | 0.322 |
| Constraint Clarity | 0.75 | 0.25 | 0.188 |
| Success Criteria | 0.70 | 0.25 | 0.175 |
| Context Clarity | 0.85 | 0.15 | 0.128 |
| **Total Clarity** | | | **0.812** |
| **Ambiguity** | | | **0.188 (19%)** |

## Goal

Add a persistent Architect tier on top of the current harness-ts supervised-session model, activated per operator-declared project. Architect serves three roles for the duration of a project:

1. **Project decomposer** — splits a multi-phase project into individual phase task files that flow through the existing harness-ts pipeline.
2. **Tier-1 escalation resolver** — receives `escalation_needed` events from Executor sessions within its project, resolves by directing retry or amending plan, escalates to operator only if it cannot resolve.
3. **Tier-1 review arbiter** — breaks Executor↔Reviewer deadlocks after N rejections on the same phase, with retry-only authority (cannot override Reviewer's verdict; operator is sole override path).

Reviewer is promoted from threshold-gated (current Phase 3 design) to mandatory-per-phase-merge for project phases only. Standalone (non-project) tasks continue using the current harness-ts flow unchanged.

The current `harness-ts/` orchestrator becomes Architect's execution substrate rather than the top-level coordinator.

## Constraints

- Architect spawns only when operator explicitly declares a project. No auto-detection in v1.
- Architect is a persistent OMC SDK session scoped to one project, with full CC+OMC capability (can use OMC `architect`/`planner` agents, `/team`, `/ralplan` internally).
- Executor sessions run with `persistSession: true` to enable Architect-directed resume.
- Architect arbitration verdicts reduce to three values: `retry_with_directive`, `plan_amendment`, `escalate_operator`. No `executor_correct` override.
- Reviewer verdict is terminal unless operator overrides. Architect cannot force-merge over Reviewer rejection.
- Disagreement threshold: 2 Reviewer rejections on same phase before Architect arbiter fires.
- Tier-1 circuit breaker: reuse existing `max_tier1_escalations` (default 2). After that, escalate to operator (tier 2).
- Architect's response to Executor:
  - `retry_with_directive` → resume Executor via SDK `resume: sessionId` + directive as new prompt (zero context loss).
  - `plan_amendment` → terminate old Executor, spawn fresh Executor against amended phase spec.
- Standalone (non-project) tasks bypass Architect entirely. Escalations route direct to operator (current `escalation_wait` behavior).
- Budget bounds: reuse existing SDK `maxBudgetUsd` per session. Project-level budget ceiling enforced by orchestrator tracking cumulative cost across all sessions in a project.
- State machine adds one new state: `review_arbitration` (active → review_arbitration → active | escalation_wait | merging).
- Persistent-Architect context management: compaction trigger at ~60% budget → summary handoff preserving decision boundaries, non-goals, and prior-phase outcomes.
- Additive only: existing 273 harness-ts Phase 2A tests must remain green. New tests added; none removed.

## Non-Goals

- NOT replacing the supervised-single-session model for standalone tasks. Low-conflict framing only — additive above current harness-ts.
- NOT mandating Architect for every task. Undeclared tasks skip Architect. Trivial-task regression avoided.
- NOT giving Architect merge-override authority. Operator retains sole override path.
- NOT implementing plan-citation recusal in v1. Architect's potential bias toward its own plan is measured empirically before building a mitigation.
- NOT implementing ephemeral Architect-for-standalone-tasks in v1. Standalone escalations go direct to operator.
- NOT implementing configurable thresholds in v1. Disagreement threshold fixed at 2, tier-1 cap at 2 (existing default).
- NOT implementing LLM-based escalation classification. Category routing is deterministic based on `escalation.type` field written by Executor.
- NOT removing or changing Phase 2B Discord integration plan. Architect adds a listener for `escalation_needed` and `review_arbitration` events upstream of Discord notification.
- NOT auto-promoting standalone tasks to projects on escalation. Operator is the sole project-declaration authority.
- NOT adding a new Wiki/Classify stage. Wiki updates remain post-merge concern of existing harness-ts pipeline.

## Acceptance Criteria

- [ ] **Tier-1 resolution rate ≥ X%** (X operator-configured at first deploy, candidate 60%). Architect resolves ≥X% of escalations (failure-retry + review-disagreement combined) without paging operator. Measurable from `tier1EscalationCount` + orchestrator event log over a 20-task sample.
- [ ] **Multi-phase project runs end-to-end.** One operator-declared project of 5+ phases: Architect decomposes, each phase executes in its own Executor session, each merge passes through Reviewer, all phases merge to trunk. Operator paged only for scope changes not captured in original plan.
- [ ] **Cost per project under ceiling.** Total cumulative cost across Architect + N × Executor + N × Reviewer + arbitration sessions for the end-to-end project stays under the operator-configured ceiling. Tracked via SDK `usage` accumulation per session.
- [ ] **No infinite loops, clean failures.** Every escalation path terminates: resolved by Architect, escalated to operator, or circuit-broken to `failed`. No task observed stuck indefinitely in `escalation_wait`, `review_arbitration`, or any new state. Verified via state-machine audit over 50-task mixed workload.
- [ ] **All existing harness-ts Phase 2A tests pass.** 273 tests remain green. New tests added for: `review_arbitration` state transitions, Architect arbitration verdict routing, project declaration lifecycle, Executor resume-with-directive, Architect session compaction handoff.
- [ ] **Architect session survives full project lifespan** without unrecoverable context exhaustion. If compaction fires, summary handoff preserves plan, decision boundaries, and non-goals such that subsequent phases proceed correctly.

## Assumptions Exposed & Resolved

| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| "Replace single-session model" meant every task goes 3-tier | Wiki (v5-harness-efficiency-proposal, harness-ts-ambiguity-protections) documents single-session as the deliberate direction with prompt-engineering replacing most pipeline stages | Scope restricted to declared projects only (low-conflict framing) |
| Architect is persistent per project | Considered one-shot decomposer (dies after emitting phases) and global-permanent alternatives | Persistent per project chosen — matches Tier-1 escalation resolver role, keeps project context hot |
| Architect can force-merge via `executor_correct` verdict | Contrarian round questioned trust calibration — Architect wrote the plan it's arbitrating | Retry-only authority chosen — Reviewer verdict terminal unless operator overrides |
| Every escalation routes through Architect | Simplifier round questioned whether standalone tasks need tier-1 buffer | Standalone tasks skip Architect; direct-to-operator preserved |
| Resume vs fresh Executor on arbitration | Tradeoff: resume preserves context but risks stale; fresh clean but costly | Split by verdict: `retry_with_directive` resumes, `plan_amendment` spawns fresh |
| Plan-citation bias needs explicit recusal rule | Deferred — measurable empirically from arbitration logs post-deploy | v1 treats all rejections uniformly; revisit with data |
| Architect requires LLM-classified escalation routing | Python harness used LLM classify; modern consensus is deterministic for pipeline-internal | Deterministic routing on `escalation.type` field; no new LLM classify stage |

## Technical Context

### Current Codebase State (harness-ts/)

- **Phase 2A complete** (commit `627cbf9`): enriched completion signal, escalation protocol, failure retry, circuit breaker, graduated response evaluation, budget kill.
- **273 tests passing** across 11 test files covering config, state machine, SDK wrapper, session manager, merge gate, orchestrator, full pipeline, validation (real git + SDK types).
- **Escalation pipeline** (`src/lib/escalation.ts`, `src/orchestrator.ts:234-305`): agent writes `.harness/escalation.json` → orchestrator reads → state transitions to `escalation_wait` → emits `escalation_needed` event → no listener consumes it.
- **Graduated response** (`src/lib/response.ts`): evaluates every completion to level 0–4 based on structured confidence. Emits event. Currently informational — no gate enforcement.
- **Circuit breaker fields** (`src/lib/state.ts`): `tier1EscalationCount`, `max_tier1_escalations`, `pre_escalation_stage` already exist. `pre_escalation_stage` unused (Python B5 legacy).
- **Reviewer gate** (Phase 3 roadmap): external read-only session with contrarian prompt, triggered by cost/diff-size/confidence thresholds. Not implemented.

### What This Proposal Slots Into

1. **Empty `escalation_needed` listener** becomes Architect tier-1 resolver for project tasks.
2. **Unimplemented Phase 3 reviewer gate** becomes mandatory-per-phase-merge for project phases, threshold-gated for standalone tasks.
3. **Unused `max_tier1_escalations` circuit breaker** wires up to count Architect-arbitration failures, promoting to operator at cap.
4. **New `review_arbitration` state** added to state machine, orthogonal to existing `escalation_wait`.

### New Modules / Extensions

| File | Role | Estimated lines |
|------|------|-----------------|
| `src/session/architect.ts` (new) | Architect session spawn, resume, compaction, per-project lifecycle | ~200 |
| `src/lib/project.ts` (new) | Project declaration, phase queue, cumulative cost tracking | ~150 |
| `src/gates/review.ts` (new, also fulfills Phase 3) | Reviewer spawn (read-only contrarian), verdict schema, disagreement counter | ~180 |
| `src/lib/arbitration.ts` (new) | Architect arbitration verdict schema, routing to resume/amend/escalate | ~80 |
| `src/lib/state.ts` (extended) | `review_arbitration` state + transitions | ~30 |
| `src/orchestrator.ts` (extended) | Listener for `escalation_needed` routing to Architect, project-aware routing | ~100 |

Rough total: ~740 new lines. Reuses all existing infrastructure.

### OMC / SDK Dependencies

- Architect session runs with `settingSources: ["project"]` + inline `enabledPlugins: { "oh-my-claudecode@omc": true, "caveman@caveman": true }` (per Phase 2B pre-requisite #1).
- Architect's systemPrompt instructs it to use OMC `architect`/`planner` agents and `/team` for internal decomposition work.
- Executor sessions use `persistSession: true` (already default after Phase 2A wave-5 fix).
- SDK `resume: sessionId` path used for `retry_with_directive` arbitration response.

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| Architect | tier, persistent OMC session | projectId, sessionId, systemPrompt, plan, budgetUsed | owns Project, arbitrates for Executor+Reviewer, escalates to Operator |
| Executor | tier, per-phase OMC session | taskId, worktreePath, sessionId (persisted) | executes Phase, reports to Reviewer, receives directives from Architect |
| Reviewer | tier, per-phase OMC session, read-only | taskId, verdict, feedback, sessionId (ephemeral) | gates Phase merge, rejects trigger Architect arbitration |
| Task | unit of work | id, prompt, projectId?, phaseId?, state | belongs to Project (if declared) |
| Project | operator-declared container | projectId, plan, phases[], architectSessionId, totalCostUsd | contains Phases, owned by Architect |
| Phase | task within project | phaseId, spec, dependencies, taskFileId | belongs to Project, executed by Executor |
| Operator | human in loop | discord user | declares Project, resolves tier-2 escalations, sole merge-override authority |
| OMC session | SDK substrate | sessionId, persisted, usage, cost | substrate for Architect, Executor, Reviewer |
| Harness orchestrator | daemon | poll loop, state manager, merge queue | executes substrate, becomes subordinate to Architect for project tasks |
| EscalationClassifier | deterministic router | category enum, routing table | routes escalation.json to Architect or Operator |
| PlanAmendment | Architect verdict payload | updatedPhaseSpec, rationale | flows from Architect to fresh Executor session |
| ReviewArbitration | state + signal | verdict (retry_with_directive/plan_amendment/escalate_operator), rationale | Architect writes in response to N Reviewer rejections |
| OverridePolicy | config concept | retry-only (locked v1) | defines Architect authority ceiling |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|-------|-------------|-----|---------|--------|----------------|
| 1 | 9 | 9 | - | - | N/A |
| 2 | 11 | 2 | 0 | 9 | 91% |
| 3 | 13 | 2 | 0 | 11 | 92% |
| 4 | 13 | 0 | 0 | 13 | 100% |
| 5 | 13 | 0 | 0 | 13 | 100% |
| 6 | 13 | 0 | 0 | 13 | 100% |

Domain model stabilized at round 4. Three rounds of no new entities confirms convergence — the remaining design work is constraint specification, not entity discovery.

## Interview Transcript

<details>
<summary>Full Q&A (6 rounds)</summary>

### Round 1 — Architect scope & lifetime
**Q:** When does the Architect tier run, and what is its lifetime?
**A (initial):** Every task, persistent per project.
**A (revised after prior-art briefing, Round 2 rejection):** Project-scope only, persistent (low-conflict framing with Architect also handling tier-1 escalation).
**Ambiguity:** 100% → 67%.

### Round 2 — Prior-art briefing (user-requested)
User paused interview for analysis of prior discussions. Full briefing covered: current harness-ts structure, multi-session vs single-session pros/cons, why current single-session was chosen, four decisions the original proposal contradicted, three points that supported it, three framings ranked by conflict level. User accepted low-conflict framing.
**Ambiguity delta:** 67% → 53% (after round 2 answer).

### Round 3 — Architect as tier-1 escalation resolver
**Q (implicit, raised by user):** Could Architect also be one layer of escalation management, with operator as tier 2?
**A:** Yes. Architect already persistent per project → natural tier-1 resolver for Executor escalations within its project.
**Ambiguity:** 53% unchanged structurally; constraint sub-decisions now enumerable.

### Round 3 (continued) — Architect as review arbiter
**Q (raised by user):** Could new Architect also slot into Reviewer process? Executor↔Reviewer disagreements escalate to Architect, then operator.
**A:** Yes. Solves real gap (current harness has no resolver for reviewer↔executor deadlock). Pros: completes tier-1 buffer symmetrically, reuses project context, uses existing circuit breaker fields. Cons: Architect bias on own plan, 3-session cost, circular blame risk.
**Ambiguity:** 53% → 47%.

### Round 4 — Architect authority (contrarian mode)
**Q:** What authority does Architect have when arbitrating?
**A:** Retry-only, no merge override. Reviewer verdict terminal unless operator overrides.
**Ambiguity:** 47% → 43%. Goal 0.82 → 0.88.

### Round 5 — Ship criteria
**Q:** What proves the Architect tier justifies its 2–3× session cost?
**A:** Tier-1 resolution rate ≥ X%, multi-phase project end-to-end, cost under ceiling, no infinite loops (added after user follow-up).
**Ambiguity:** 43% → 29%.

### Round 6 — v1 scope (simplifier mode)
**Q:** Which v1 scope is acceptable to ship?
**A:** Minimal + resume. Architect for declared projects only. Fresh Executor with plan amendment OR resume with directive (split by verdict type). Retry-only authority. Standalone tasks → direct to operator. Plan-citation recusal deferred. Disagreement threshold fixed at 2.
**Ambiguity:** 29% → 19% (threshold met).

</details>

## Recommended Next Steps (for execution bridge)

1. **ralplan → autopilot (3-stage pipeline)** — this spec is opinionated but has concrete implementation decisions ahead (schema of `review-arbitration.json`, Architect systemPrompt content, project declaration UX, compaction handoff mechanism). Consensus refinement via Planner/Architect/Critic will expose remaining ambiguity before execution.
2. **Prerequisite:** Phase 2B Discord integration completes first (or lands in parallel). Architect's `escalate_operator` verdict depends on Discord operator-notification channel existing. v1 can stub Discord as log-only if Phase 2B slips, but production value requires both.
3. **Split into waves for execution:**
   - Wave A: `review_arbitration` state + Reviewer gate (mandatory for project phases, threshold-gated for standalone) — completes Phase 3 work.
   - Wave B: Project declaration + Architect session lifecycle + plan decomposition.
   - Wave C: Architect arbitration verdict routing + Executor resume-with-directive.
   - Wave D: Compaction handoff + end-to-end project validation test.
