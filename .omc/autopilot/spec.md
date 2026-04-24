# Autopilot Spec — Harness-TS Validator Follow-up Refactors

## Goal
Close the 11 validator-flagged follow-ups accumulated from P1 + P2 multi-perspective reviews. All items are mechanical refactors / small hardening; no API spend; tests grow modestly.

## Scope — 11 items

### Medium severity (5)

1. **Cascade symmetry extraction** — `src/orchestrator.ts`: `applyArchitectVerdict::escalate_operator` and `handleMergeResult::merged` both follow `phase.markX → maybe-project.X → emit-project-event`. Extract a private helper `finalizePhaseOutcome(task, "done"|"failed", rationale?)` that returns the project event to emit (or null). Both call sites collapse.

2. **Scratch-repo helper** — `scripts/live-project.ts`, `scripts/live-project-3phase.ts`, `scripts/live-project-arbitration.ts` duplicate `initScratchRepo` + `buildConfig` (~120 lines × 3). Extract `scripts/lib/scratch-repo.ts` with `initScratchRepo({ prefix })` and `buildBaseConfig({ root, name, overrides })`. Retrofit all 3 scripts.

3. **InjectedReviewGate shared fixture** — move from inline in `scripts/live-project-arbitration.ts` to `scripts/lib/stub-review-gate.ts`. Constructor accepts `verdicts: ReviewVerdict[]` queue; falls back to `approve` when exhausted. Callers can program any sequence.

4. **Cascade test gaps** — `tests/orchestrator.test.ts`: add tests for (a) escalate_operator with no `projectStore` injected (orchestrator legacy mode), (b) multiple pending phases where sibling is in `active` state, (c) standalone task (no projectId/phaseId) hits the guard and no-ops.

5. **CR M1 `buildSessionConfig` helper** — `SessionManager.spawnTask`, `ReviewGate.runReview`, `ArchitectManager.spawnSessionWithPrompt` all hand-assemble `SessionConfig`. Extract `buildSessionConfig(opts)` in `src/session/sdk.ts` or new `src/session/config.ts`. Four+ callers after stub-gate extraction.

### Low severity (6)

6. **Scratch dir mkdtempSync** — 3 live scripts use `join(tmpdir(), \`harness-*-${Date.now()}\`)` + `mkdirSync({recursive:true})` → symlink-race-able. Swap for `mkdtempSync(join(tmpdir(), "harness-*-"))`. Addressed alongside item 2.

7. **Rationale length-cap** — `orchestrator.ts` applyArchitectVerdict escalate_operator embeds `verdict.rationale` unbounded in `lastError` + `task_failed.reason` + `project_failed.reason`. Cap at 1KB + strip ANSI/control chars before embedding. New helper `truncateRationale(s)` in `src/lib/text.ts`.

8. **Fence-escape parity** — `src/session/architect.ts::buildReviewArbitrationPrompt` + `buildEscalationPrompt` embed operator text inside `<untrusted:*>` XML + triple-backtick text blocks but don't neutralize triple-backticks in the data. `relayOperatorMessage` already does this with `.replace(/\`\`\`/g, "​\`\`\`")`. Apply same escape to both builders.

9. **Script SIGINT cleanup** — live scripts rely on `main().catch` for top-level only. Add `process.on("SIGINT", () => orch.shutdown())` + try/finally around wait loop in the shared scratch-repo helper. Addressed alongside item 2.

10. **Magic numbers in live scripts** — `25 * 60 * 1000`, `2000` poll cadence, `30 * 60 * 1000`. Extract to named consts at top of each script (or, if item 2 ships, in the shared helper).

11. **Unified terminal predicate** — 3 scripts have 3 slightly different project-done checks. Unify in shared helper (addressed alongside item 2).

## Out of scope
- P3 mass stress (separate autopilot)
- Live crash-recovery + OMC re-measure (separate autopilots)
- Wave B.5 Architect smoke (separate autopilot)
- Wave 6-split + Wave D (need ralplan first)
- Discord live (blocked on bot token)

## PASS criteria
- All 518 existing tests still pass.
- +≥6 new tests (cascade edge-cases + shared-helper smoke + stub-gate queue).
- `npm run lint` clean.
- `npm run build` clean.
- No net API cost (mock-only).
- Multi-perspective Phase 4 validation: architect + security + code-reviewer all APPROVE.
- 3 live scripts still typecheck.

## Non-goals
- Behavior changes (pure refactor + small hardening).
- New features.
- Re-running any existing live script.
