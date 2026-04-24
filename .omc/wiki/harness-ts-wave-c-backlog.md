---
title: Harness-TS Wave C Backlog (deferred from 2026-04-24 session)
category: architecture
tags: ["harness-ts", "wave-c", "backlog", "three-tier"]
updated: 2026-04-24
---

# Wave C Backlog — Deferred Action List

Picked up at end of the 2026-04-24 session. Ordered by leverage. Run through top-to-bottom when Wave C starts.

## Context

Live end-to-end project run (commit `8e11a3b`, `scripts/live-project.ts`) passed 5/5 checks with `$0.69` total cost in 66.8s. BUT surfaced two production bugs mocks never caught. Plan M.15 has full detail:
`.omc/plans/ralplan-harness-ts-three-tier-architect.md` section M.15.

## Blocking bugs (fix first — one-liners, no design risk)

### 1. `TaskFile.projectId` not propagated to `TaskRecord.projectId`

- **Where:** `harness-ts/src/orchestrator.ts` `scanForTasks` ~line 240
- **Symptom:** `scanForTasks` calls `state.createTask(prompt, id)` — drops `projectId` and `phaseId` from the ingested TaskFile.
- **Consequence:** Wave A project-mandatory review gate silently bypassed for every project phase (`task.projectId === undefined` at routing time).
- **Fix scope:** 2-line edit, plus 1 regression test asserting round-trip via `scanForTasks`.
- **Patch:**
  ```typescript
  const task = this.state.createTask(taskFile.prompt, taskId);
  if (taskFile.projectId) {
    this.state.updateTask(task.id, {
      projectId: taskFile.projectId,
      phaseId: taskFile.phaseId ?? task.id,
    });
  }
  ```

### 2. Project auto-completion not wired

- **Where:** `harness-ts/src/orchestrator.ts` `handleMergeResult` "merged" case ~line 630
- **Symptom:** After all phases reach `done`, project record stays in `state: "decomposing"` forever. `projectStore.markPhaseDone` + `completeProject` never called.
- **Consequence:** `project_completed` event never fires; projects never close.
- **Fix scope:** ~10 lines + 2 regression tests.
- **Patch sketch:**
  ```typescript
  // inside handleMergeResult "merged" case, after existing transition to "done":
  if (task.projectId && task.phaseId) {
    this.projectStore.markPhaseDone(task.projectId, task.phaseId);
    if (!this.projectStore.hasActivePhases(task.projectId)) {
      const p = this.projectStore.getProject(task.projectId)!;
      this.projectStore.completeProject(task.projectId);
      this.emit({
        type: "project_completed",
        projectId: task.projectId,
        phaseCount: p.phases.length,
        totalCostUsd: p.totalCostUsd,
      });
    }
  }
  ```

## Validation run (after fixes 1 + 2 land)

### 3. 3-phase real-SDK project run

Write a test script similar to `live-project.ts` but with a project the Architect decomposes into ≥ 3 phases where later phases depend on earlier. Stresses state machine + concurrency + project completion. Budget cap: `$5` total.

Assert: all phases land on trunk in order, project state → `completed`, `project_completed` event fires.

## Spikes (before Wave C design decisions lock)

### 4. Caveman × structured JSON on Executor

Risk: Reviewer spike M.11.5 showed caveman corrupting structured verdict JSON. Executor writes `completion.json` — same risk surface. First live run was clean on a trivial task; one data point.

Design: 5 Executor runs against strict-JSON completion contract including confidence block. Measure field preservation rate. Acceptance: ≥ 95% round-trip; below that → drop caveman from Executor defaults.

Cost: ~`$1` total.

### 5. OMC plugin dead-weight on Executor

Per M.13.3, single-mode Executors don't invoke OMC specialists unprompted. Loading OMC adds init overhead for no benefit.

Recommendation pending spike 4: drop OMC from Executor defaults. Keep for Architect (decomposer, M.12 validated) and future parallel-Reviewer (M.14).

## Unknowns — Executor tier (M.15.2)

- **U3 — Enrichment adoption:** real Executor produced compliance 0/4 (base fields only). Phase 2A graduated response routing never fires in production. Options: (a) enrich Executor systemPrompt globally per `live-run.ts` enriched mode, or (b) Architect injects self-assess directive per-phase. Prefer (a) for Wave C default.

- **U4 — persistSession accumulation:** every Executor phase keeps its session via `persistSession: true`. 100 phases = 100 persistent records. No cleanup path. Not urgent (single-project runs don't accumulate) but flag for Phase 4 observability.

- **U5 — Budget tuning:** $0.11 actual vs $1 cap = 9× over. Graduated caps by Architect-declared phase complexity would fit better. Defer to Wave D.

## Deferred from Wave B Phase 4 review (low priority)

- **SEC M1 (compaction summary schema validation):** fence every field shape in `readArchitectSummaryFile`, not just nonGoals.
- **SEC M2 (Architect disallowlist narrow):** currently Architect has full OMC tool surface. Block network (`WebFetch`, `WebSearch`) + `Cron*` + `TeamCreate`/`TeamDelete` while keeping `Task` for OMC delegation.
- **Code-reviewer M1 (`buildSessionConfig` helper):** SessionManager + ReviewGate + ArchitectManager all hand-assemble SessionConfig. Extract shared helper when a fourth caller arrives.
- **Code-reviewer M2 (synthesized summary finalCostUsd = 0):** fallback summary in `requestSummary` sets finalCostUsd = 0 per phase. Aggregate from projectStore.

## Plan references

- Section M.15 (live run): `.omc/plans/ralplan-harness-ts-three-tier-architect.md`
- Wave C scope: plan Section F Wave C
- Spike configs: plan Section M.13.4

## Session summary

- 31 commits landed this session
- Waves 1 / 1.5 / 1.75 item 9 / 2 / 3 / A / B all shipped + reviewed
- Test count: 280 → 484 (+204)
- Live runs: 5 total (4 Wave 1-2 validations + 1 end-to-end project) — all PASS
- Real bugs found: 2 (both noted above)
- Cumulative live-run API cost this session: ~$1.20
