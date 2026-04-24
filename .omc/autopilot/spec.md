# Autopilot Spec — P2 Live Arbitration Flow Test

## Goal
Validate the P1 Architect verdict wiring end-to-end with real Architect SDK output. Confirm that when a reviewer rejects a project phase, the orchestrator consults the Architect, the Architect writes a well-formed `.harness/architect-verdict.json`, the orchestrator parses it, applies the verdict, and the Executor retries successfully.

## Scope anchor
Code surface exercised: `src/orchestrator.ts::routeArbitration + applyArchitectVerdict` (P1-B), `src/session/architect.ts::runArbitration + buildReviewArbitrationPrompt + readArchitectVerdict` (P1-A), `src/session/manager.ts` directive-surfacing (P1 gap fix), `config/harness/architect-prompt.md §5` verdict contract.

## Constraints
- Budget cap: Architect $6, Executor $1 per run × 2 runs = $2. Target total ~$3.
- Real SDK for Architect + Executor. Mocked Reviewer (injected verdict sequence: reject → approve) so the test is deterministic.
- Reviewer `arbitration_threshold = 1` so the first rejection triggers arbitration.
- Single-phase project to keep the test tight.

## PASS criteria (binary)
1. `architect_spawned` event fired.
2. `project_decomposed` event fired (phaseCount ≥ 1).
3. Exactly ONE `review_arbitration_entered` event (first Reviewer rejection trips threshold=1).
4. `architect_arbitration_fired` event with `cause = "review_disagreement"`.
5. `arbitration_verdict` event with verdict in `{retry_with_directive, plan_amendment}` (not `escalate_operator`).
6. At least TWO `session_complete` events (original + retry after verdict).
7. Exactly ONE `task_done` event (retry passes Reviewer).
8. `project_completed` event (project state → completed).
9. Expected output file present on trunk.

## FAIL modes we want to surface
- Architect writes malformed/missing verdict.json → `architect_no_verdict_written` rationale → escalate_operator. Indicates §5 prompt contract is ambiguous.
- Architect emits `executor_correct` or other disallowed type → schema rejects → escalate_operator. Indicates prompt text not sticky enough.
- Directive stored but not surfaced in retry prompt (gap 2 from architect review).
- Verdict file from prior run leaks through (stale-file defense failed).

## Deliverable
- `harness-ts/scripts/live-project-arbitration.ts` — one-shot script that spins a scratch repo, declares a project, runs the loop, asserts the PASS checks, exits 0/1.
- Update `.omc/wiki/harness-ts-wave-c-backlog.md` with the result.

## Non-goals
- Not validating mass-phase (that's P3).
- Not validating crash recovery (separate test).
- Not validating Discord integration live (separate work).
- Not chasing `plan_amendment` vs `retry_with_directive` preference — either is a pass as long as it's not `escalate_operator`.
