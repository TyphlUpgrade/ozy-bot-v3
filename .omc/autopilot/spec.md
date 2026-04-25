# Autopilot Spec — P3 Mass-Phase Stress

## Goal
Validate the three-tier pipeline under 7-phase real-SDK load. Expose contention in state.json writes, poll-loop throughput, worktree branch pileup, ProjectStore persistence ordering, merge-gate FIFO under bursts, and Architect decomposition when N is larger than any prior run.

## Scope anchor
Exercises every module wired during Waves 1–C + P1/P2 on a larger scale than the 3-phase stress did. Primary targets: `src/orchestrator.ts` poll loop, `src/lib/state.ts` atomic writes under parallel transitions, `src/lib/project.ts` persist ordering, `src/gates/merge.ts` FIFO under 7 phases, `src/session/architect.ts` decompose with `phaseCount ≥ 7`, `src/session/manager.ts` branch-name pileup, `src/gates/review.ts` mandatory-review on every project phase.

## Constraints
- Target cost ≤ $5 total. Worst-case cap (budgets × tiers) $20.
- Architect $6 single session. Executor $1 × 7 = $7 worst. Reviewer $1 × 7 = $7 worst. Expected realistic ~$4 (≈ $0.16/phase executor + $0.05/phase reviewer + ~$2 architect).
- Trivial per-phase work (one file each) so we measure pipeline scaling, not Executor work time.
- Tasks chosen so Architect cleanly decomposes into ≥ 7 independent phases.

## PASS criteria
1. `architect_spawned` fires.
2. `project_decomposed` fires with `phaseCount ≥ 7`.
3. ≥ 7 `task_picked_up` events.
4. ≥ 7 `session_complete` events with `success=true`.
5. 0 `task_failed` events (every phase must land).
6. ≥ 7 `task_done` events (one per phase merged).
7. `project_completed` fires exactly once with `phaseCount ≥ 7`.
8. Project state → `completed`.
9. All 7+ expected files present on trunk.
10. Trunk commit log shows ≥ 7 merge commits — FIFO didn't drop any phase.
11. Total wall < 30 minutes.

## FAIL modes we want to surface
- state.json corruption when 2+ phases transition simultaneously.
- Merge-gate FIFO dropping a phase when burst arrival.
- Branch-name collision (`harness/task-project-{id}-phase-NN`).
- Architect hitting its $6 cap mid-decomposition of a larger project.
- ProjectStore persistence lag where `hasActivePhases` returns stale `false` → premature project_completed.
- Reviewer timeouts piling up during burst.
- Worktree directory leak (fail to cleanup on a subset of phases).

## Deliverable
- `harness-ts/scripts/live-project-mass-phase.ts` using the shared `scratch-repo` helper + real Architect + real Executor + real Reviewer.
- Project: "Build 7 arithmetic utility files", one per operation (add/sub/mul/div/mod/pow/abs). Expect Architect to decompose 1-per-phase.
- Exit 0 on all 11 checks; exit 1 on any failure with full diagnostics dumped.

## Non-goals
- Not validating arbitration (P1 + P2 already cover).
- Not validating crash recovery (autopilot #3).
- Not measuring token cost differences (no spike comparisons here).
- Not hitting 10+ phases — 7 is enough to stress every dimension; the marginal cost of 10 vs 7 doesn't buy much signal.
