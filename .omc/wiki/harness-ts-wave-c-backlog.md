---
title: Harness-TS Wave C Backlog + P1/P2 Follow-ups
category: architecture
tags: ["harness-ts", "wave-c", "backlog", "three-tier", "p1", "p2"]
updated: 2026-04-24
---

# Wave C Backlog — Status + Remaining Action List

**Second-session status (2026-04-24 cont'd):** bugs 1+2, SEC M1/M2, CR M2, U3, U4, and the 3-phase live stress have all landed. Remaining items are live-API spikes (4, 5) + deferred Wave D (U5) + on-arrival CR M1.

## Context

Live end-to-end project run (commit `8e11a3b`, `scripts/live-project.ts`) passed 5/5 checks with `$0.69` total cost in 66.8s. BUT surfaced two production bugs mocks never caught. Plan M.15 has full detail:
`.omc/plans/ralplan-harness-ts-three-tier-architect.md` section M.15.

## ✅ Completed items (commits + evidence)

| # | Item | Commits | Evidence |
|---|------|---------|----------|
| 1 | Bug 1 — `TaskFile.projectId` propagation | `d2388b3` | +2 tests; live-run `review_mandatory` now fires on project phases |
| 2 | Bug 2 — Project auto-completion | `d2388b3` | +2 tests; 3-phase stress `project state=completed` + `project_completed` event |
| 3 | 3-phase real-SDK stress (`scripts/live-project-3phase.ts`) | `7d4e7ed` | PASS 7/7 checks, `$0.75`, 177s elapsed |
| SEC M1 | `validateArchitectCompactionSummary` full schema check | `7d4e7ed` | +6 tests; malformed → fallback to projectStore summary |
| SEC M2 | Architect disallowlist (network + cron + team) | `7d4e7ed` | +1 test asserting Task NOT blocked |
| CR M2 | `finalCostUsd` aggregated from StateManager | `7d4e7ed` | +1 test asserting cost round-trip |
| U3 | Executor enrichment default (`DEFAULT_EXECUTOR_SYSTEM_PROMPT`) | *this ralph session* | +2 tests; SessionManager falls through to enriched prompt when `config.systemPrompt` unset |
| U4 | persistSession accumulation observability | *this ralph session* | `persistentSessionCount` accessor + `persistent_session_warn_threshold` config + 3 tests |

Cumulative test count: 484 → **500** (+16). Live-run cost this turn: `$0.75` (3-phase stress) + `$1.221` (spikes 4 + 5).

### 4. Caveman × structured JSON on Executor — ✅ PASS (100%)

**Result:** 5 Executor runs through `scripts/spike-caveman-json.ts`, each with a distinct trivial file-creation task, caveman + OMC both enabled per production defaults. Every run wrote a complete completion.json.

| runs | top fields (8) | confidence subfields (5) | overall |
|------|----------------|--------------------------|---------|
| 5    | 40/40 (100%)   | 25/25 (100%)             | 65/65 (**100%**) |

Total cost: `$0.607`. Wall range: 23–36s per run.

**Decision:** Caveman safe on Executor structured-JSON contract. No change to `DEFAULT_PLUGINS`. The M.11.5 Reviewer finding does not reproduce at the Executor tier — likely because the enriched prompt fences the schema in a code block that caveman preserves verbatim.

Spike script: `scripts/spike-caveman-json.ts`.

### 5. OMC plugin dead-weight on Executor — ✅ threshold FAIL (3.8% < 20%) — keep OMC

**Result:** 6 Executor runs via `scripts/spike-omc-overhead.ts` (3 with `oh-my-claudecode@omc: true`, 3 with `false`, interleaved to spread cache warming). Caveman kept on for both arms.

| arm     | wall median | cost median | preservation |
|---------|-------------|-------------|--------------|
| OMC ON  | 25891 ms    | $0.119      | 100% (39/39) |
| OMC OFF | 24899 ms    | $0.070      | 100% (39/39) |

Wall reduction (OFF vs ON): **3.8%** — far below the 20% threshold. Total spend: `$0.614`.

**Decision:** KEEP OMC in Executor defaults. Init-overhead savings are negligible; the hypothesis that "OMC loading dominates Executor cold-start" does not hold at the wall-clock level.

**Secondary observation (not a formal threshold):** OMC-OFF token cost ran ~42% lower ($0.070 vs $0.119 median). Sample size is small (3 per arm), and the OFF runs' $0.070 may reflect a cache hit on the smaller plugin surface. Worth re-measuring if per-phase spend becomes a constraint; not urgent while per-phase budget is 9× over-provisioned (U5 territory).

Spike script: `scripts/spike-omc-overhead.ts`.

## 🔓 Remaining items

### U5 — Budget tuning (deferred to Wave D)

Live runs used `~9×` under the per-phase cap ($0.11 actual vs $1 cap). Graduated caps by Architect-declared phase complexity would fit better (e.g. phase.complexity = "trivial" → $0.25; "standard" → $1.00; "complex" → $3.00). Defer to Wave D; no urgency while project-cost aggregation is accurate (CR M2 fixed that).

### CR M1 — `buildSessionConfig` helper (on-arrival)

SessionManager + ReviewGate + ArchitectManager all hand-assemble `SessionConfig`. Extract a shared helper when a fourth caller arrives. Not blocking Wave C; deliberately held until extraction delivers de-duplication leverage (currently 3 callers, marginal).

## Session summary (rolling)

- First-session commits: 32 (Waves 1 / 1.5 / 1.75 item 9 / 2 / 3 / A / B).
- This session: Wave C hardening + U3 + U4 + spikes 4 + 5 + **P1** (verdict wiring) + **P2** (live arbitration + cascade fix).
- Test count: 280 → 484 (session 1) → 500 (Wave C core) → **518** (P1+P2).
- Live runs: 19 total (6 prior + 5 spike-4 + 6 spike-5 + 2 arbitration). All PASS on compliance path.
- Real bugs found: 4 (2 closed in Wave C core, 1 in P1 gap-fix retry path, 1 in P2 cascade).
- Cumulative live-run API cost across both sessions: ~`$4.64` ($1.95 prior + $1.22 spikes + $0.75 3-phase + $0.72 arbitration).

## Spike decisions (Wave C design lock)

- Caveman stays in Executor defaults (100% field preservation).
- OMC stays in Executor defaults (wall reduction 3.8% — below 20% threshold). Token-cost side signal (~42% cheaper without OMC) logged for possible re-measurement under Wave D budget tuning.

## ✅ P1 — Architect verdict wiring (commits d1aec73 + 21c87a5 + e75fa92)

Wave C core. Wired real Architect verdict parsing end-to-end.

- ArchitectManager: `runArbitration` + context-fenced `buildReviewArbitrationPrompt`/`buildEscalationPrompt` + stale-file unlink + fresh verdict read.
- Orchestrator: `routeArbitration` + `applyArchitectVerdict` apply the 3 verdict types. retry_with_directive / plan_amendment transition task → `shelved` so `scheduleRetry` unshelves and re-spawns the Executor. escalate_operator → `failed`.
- SessionManager: prepends `task.lastDirective` to the SDK prompt on retry with an "Architect directive (from prior arbitration)" header.
- ProjectStore: `updatePhaseSpec` for plan_amendment verdicts.
- State machine: `review_arbitration` + `escalation_wait` gain `shelved` as valid destination.
- architect-prompt.md §5: verdict file contract (`.harness/architect-verdict.json`) with exact JSON shape for each type.
- Tests: 500 → 516 (+16).

Architect review found + fixed: `scheduleRetry` only accepted shelved, not active — first pass dead-ended retry path. Second fix surfaced `lastDirective` to Executor prompt.

## ✅ P2 — Live arbitration E2E (commits a23ef64 + dd6b2b8)

Script `scripts/live-project-arbitration.ts`. Real SDK Architect + Executor, stub Reviewer with reject-then-approve queue.

- First run: stub reviewer claimed a concern not in the task prompt; real Architect **correctly** escalated. Informed test design (stub reviewer concern must be grounded).
- Second run: trailer requirement added to prompt. Architect issued `retry_with_directive`; Executor retried with directive prepended; reviewer approved; merged. PASS 7/9 tightened checks. Cost $0.725.
- Cascade bug found by live run: `escalate_operator` transitioned task to failed but never marked phase failed or failed the project. Single-phase projects hung in `decomposing`. Fix: `markPhaseFailed` always; `failProject` + emit `project_failed` when no active phases remain.
- Tests: 516 → 518 (+2 cascade coverage).

## 🔓 Validator follow-ups — P1/P2 (deferred, non-blocking)

### Medium

- **Cascade symmetry**: `applyArchitectVerdict::escalate_operator` and `handleMergeResult::merged` have ~12 lines of parallel structure (phase.X → maybe-project.X → emit). Extract `finalizePhaseOutcome(task, outcome)` when the 3rd caller lands or the shapes diverge.
- **Scratch-repo helper duplication**: `scripts/live-project.ts`, `live-project-3phase.ts`, `live-project-arbitration.ts` all duplicate `initScratchRepo` + `buildConfig` (~120 lines × 3). Extract `scripts/lib/scratch-repo.ts` before the 4th script lands.
- **InjectedReviewGate as shared fixture**: move from inline in `live-project-arbitration.ts` to `scripts/lib/stub-review-gate.ts` with a configurable verdict queue (approve-twice, threshold boundary, multi-rejection). Useful for P3.
- **Test gaps** on cascade: no-projectStore no-op; multiple phases with sibling in `active` (not just `pending`). Pin `hasActivePhases` contract.
- **CR M1 from before**: `buildSessionConfig` extraction — still waiting for 4th caller.

### Low

- **Scratch dir TOCTOU**: `/tmp/harness-*-${Date.now()}` + `mkdirSync({recursive:true})` is symlink-raceable. Swap for `mkdtempSync(join(tmpdir(), "harness-*-"))`. Dev-only risk; not urgent.
- ~~**Rationale length-cap**: `project_failed.reason` embeds `verdict.rationale` unbounded. Add a 1KB cap + ANSI/control-char strip when rendering to Discord/logs.~~ **CLOSED** 2026-04-26 commit `32ce0ea` — `truncateRationale(s, 1024)` from `src/lib/text.ts:66` (existed already; now applied to `project_failed.reason` and `arbitration_verdict.rationale` formatters in `src/discord/notifier.ts`).
- **Fence-escape parity**: `buildReviewArbitrationPrompt` does not neutralize triple-backticks inside fenced data (unlike `relayOperatorMessage`). Pre-existing; worth aligning.
- **Script SIGINT cleanup**: `main().catch` catches top-level only. Add `process.on("SIGINT", () => orch.shutdown())` + try/finally around the wait loop in the shared scratch-repo helper.
- **Magic numbers in live scripts**: `25 * 60 * 1000` timeouts and `2000` poll cadence should move to named consts in the shared helper.
- **Inconsistent terminal predicate**: 3 scripts have 3 slightly different terminal checks for "project done". Unify in helper.

### Still open from the original list

- **U5 Budget tuning (Wave D)** — graduated caps by Architect-declared phase complexity.
- **P3 — 7-10 phase mass-phase stress** — state.json contention, poll-loop throughput, worktree branch pileup.
- **Discord integration live** — RICH RENDERING SHIPPED 2026-04-26 per `.omc/plans/2026-04-26-discord-conversational-output.md`. Three commits: Phase A `e585c3c` (payload extensions: session_complete +errors[]/terminalReason?, task_failed +attempt, project_failed +failedPhase?, task_done +responseLevelName? sourced from new TaskRecord.lastResponseLevelName); Phase B Commit 1 `3fd81a8` (8 it.skip test scaffolds, additions-only); Phase B Commit 2 `32ce0ea` (NOTIFIER_MAP rich rewrite for rows 3/5/7/8/9/10/11/12/14 + truncateBody(1900) helper + 16-fixture live-discord-smoke matrix). 714/714 tests PASS. **Still open**: operator dialogue (`relayOperatorInput`) end-to-end exercise; live-smoke visual operator confirmation pending real Discord run.
- **Architect crash recovery live** — `respawn("crash_recovery")` unit-tested only; no real abort-mid-decomposition + resume-with-summary round-trip.
- **OMC-OFF cost re-measurement** — 42% token-cost side signal with N=3. Bigger-N validation before Wave D budget tuning.

## Plan references

- Section M.15 (live run): `.omc/plans/ralplan-harness-ts-three-tier-architect.md`
- Wave C scope: plan Section F Wave C
- Spike configs: plan Section M.13.4
