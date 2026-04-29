---
title: Harness-TS Phase Roadmap
description: Phase 0 → Phase 4+ delivery history and pending work. Split from harness-ts-architecture.md (2026-04-27) for size policy compliance.
category: reference
tags: ["harness-ts", "roadmap", "phases", "history", "pipeline"]
created: 2026-04-27
updated: 2026-04-27
---

# Harness-TS Phase Roadmap

Phased delivery history for harness-ts pipeline. For core architecture concepts (modules, state machine, merge queue, completion signal, business-logic invariants), see [[harness-ts-architecture]].

For verbatim type signatures, see [[harness-ts-types-reference-source-of-truth]]. For Wave E-α/β/γ/δ split details, see [[phase-e-agent-perspective-discord-rendering-intended-features]].

---

## Phase 0+1: Core Pipeline — COMPLETE (2026-04-11)

6 modules, 112 tests (all mocked SDK + git). Committed `2298ad1`.

**Deliverables:** Config loader, 9-state machine, SDK wrapper, session manager, merge gate, orchestrator daemon. Business logic B1/B3/B5/B6/B7/O3/O4/O7/O8/O9 preserved.

**Limitation:** All tests mock SDK and git. Real git worktree operations verified in Phase 1.5. `settingSources` and `resumeSession()` verified against SDK v0.2.101 types (2026-04-12) — see Phase 1.5 resolution notes.

---

## Phase 1.5: Validation — COMPLETE (2026-04-11)

Verified Phase 0+1 foundation against reality. 90 new validation tests. Fixed 2 bugs: missing `cwd` on `GitOps.removeWorktree/branchExists/deleteBranch`, `runTests` timeout detection (`e.signal` not `e.killed`). **Unblocks Phase 2A.**

| Item | What | How | Risk if skipped |
|------|------|-----|----------------|
| **SDK smoke test** | `query()` works, messages match expected shapes | Manual: spawn one session, log all SDKMessages | Build on wrong assumptions |
| **settingSources verification** | `settingSources: ["project"]` loads CLAUDE.md + OMC hooks | Manual: session with settingSources, check agent behavior | Entire hook/prompt loading model fails |
| **resumeSession test** | SDK `resumeSession()` works for dialogue pattern | Manual: spawn, abort, resume with new prompt | Dialogue agent pattern (Phase 3) blocked |
| **Real git integration** | Worktree create/merge/rebase with actual git repos | Script: create repo, worktree, commit, merge, verify | Merge gate failures in production |
| **Agent completion protocol** | Real agent writes `.harness/completion.json` when systemPrompt instructs it | Manual: spawn session with completion instructions | Core pipeline protocol doesn't work |
| **End-to-end manual test** | Full lifecycle: task file → agent → completion → merge → trunk | Drop real task, observe full pipeline | Everything |

**`settingSources` — RESOLVED (2026-04-12):** Verified in SDK v0.2.101. `settingSources: ["project"]` loads CLAUDE.md + `.claude/settings.json` + OMC hooks. Type: `SettingSource = 'user' | 'project' | 'local'`. Omitting = SDK isolation mode. No fallback needed.

**`resumeSession()` — RESOLVED (2026-04-12):** Verified in SDK v0.2.101. Stable path: `query()` with `resume: sessionId` (full `Options` — keeps `settingSources`, `systemPrompt`, budget controls). Unstable V2 path (`unstable_v2_resumeSession`) exists but lacks `settingSources`/`systemPrompt` — not viable for OMC sessions. **Caveat:** `persistSession` must be `true` on original session or it can't be resumed. Fixed in `sdk.ts` (default changed from `false` to `true`).

---

## Phase 2A: Pipeline Hardening — COMPLETE (2026-04-11)

Depends: Phase 1.5 validation complete. **273 tests passing (71 new).**

**Goal:** The agent can communicate structured information back to the orchestrator, and the orchestrator routes based on signals. Internal pipeline — no Discord dependency.

| Item | Location | Effort | Description |
|------|----------|--------|------------|
| **systemPrompt content** | `config/harness/system-prompt.md` (loaded by config module) | Prompt-only | Intent classification gate, decision boundaries, simplifier pressure test, completion contract. Ports institutional knowledge from Python `config/harness/agents/*.md` into single prompt. |
| **Completion signal enrichment** | `src/session/manager.ts` (schema), system prompt | ~20 lines | Add `understanding`, `assumptions`, `nonGoals`, `confidence` (structured 5-dimension assessment) to `CompletionSignal`. Validation optional fields. |
| **Escalation protocol** | `src/session/manager.ts` + `src/orchestrator.ts` | ~50 lines | Agent writes `.harness/escalation.json`. Orchestrator detects → transitions to `escalation_wait` → emits `escalation_needed` event. Works without Discord (just pauses task with log entry). |
| **Failure retry + circuit breaker** | `src/orchestrator.ts` | ~40 lines | Completion `status: "failure"` → orchestrator retries with new session (up to `max_retries`). After N failures, auto-escalate instead of silently dropping. Circuit breaker: cap total retries per task before pausing for operator. |
| **Budget alarm events** | `src/session/sdk.ts` (`consumeStream`) | ~10 lines | Emit `budget_warning` event at 50% and 80% of `maxBudgetUsd` by tracking cumulative cost from SDKMessages. Informational only (O6) — does not pause pipeline. |
| **Mid-task checkpoints** | system prompt + `src/orchestrator.ts` | ~30 lines | Agent writes `.harness/checkpoint.json` at decision points and budget thresholds. Orchestrator logs but doesn't pause (informational in Phase 2, gating in Phase 3). |
| **Graduated response routing** | `src/orchestrator.ts` | ~40 lines | Evaluate completion signal assessment dimensions → select escalation level (0-4). Routes to merge directly (level 0-1), external review (level 2), or pause (level 3-4). |

**Tests:** 78 new tests across 4 new + 3 modified test files. All 280 passing.

**Delivered (5 waves, 12 items):**
- Wave 1: `config/harness/system-prompt.md` (agent protocol), `src/lib/budget.ts` (threshold tracker), config loader extensions
- Wave 2: `src/lib/types.ts` (shared assessment types), enriched `CompletionSignal` with optional confidence/understanding/assumptions/nonGoals, B7-pattern validation
- Wave 3: `src/lib/escalation.ts`, `src/lib/checkpoint.ts`, `src/lib/response.ts` (graduated routing levels 0-4), completion compliance event. 5 new `OrchestratorEvent` types wired into `processTask()`
- Wave 4: Failure retry with `max_session_retries`, auto-escalation with `persistent_failure`, circuit breaker with `tier1EscalationCount` + `max_tier1_escalations`
- Wave 5 (2026-04-12): SDK verification + critic/architect findings fixes:
  - `settingSources: ["project"]` and `resumeSession()` verified against SDK v0.2.101 types
  - `persistSession` default fixed `false` → `true` (was silently blocking session resumption)
  - Hard budget kill: `config.pipeline.max_budget_usd` wired to SDK `maxBudgetUsd` in `spawnTask()`
  - Budget exhaustion no-retry: `error_max_budget_usd` terminal reason short-circuits to permanent failure
  - Crash cleanup: `cleanupWorktree()` added to `merge_result: "error"` and catch block paths
  - Recovery gap: `recoverFromCrash()` now cleans up worktrees for tasks stuck in `failed` state
  - New `budget_exhausted` orchestrator event type

**Consensus plan:** `.omc/plans/ralplan-harness-ts-phase2a.md` (APPROVED, 2026-04-11)

---

## Phase 2B: Discord Integration — PARTIAL (Wave 2+3 outbound+inbound complete)

Depends: Phase 2A escalation protocol. Phase 2B pre-requisites #1, #3, #5, #6 resolved in three-tier Wave 1 (2026-04-24). See `.omc/plans/ralplan-harness-ts-three-tier-architect.md` Wave 1 for details.

### Phase 2B Pre-Requisites (Critic/Architect Review, 2026-04-12)

| # | Severity | Finding | Status | Resolution |
|---|----------|---------|--------|------------|
| 1 | CRITICAL | `settingSources: ["project"]` doesn't load OMC plugins (user-level `enabledPlugins`) | **FIXED** (three-tier Wave 1, 2026-04-24) | Option C shipped: `Options.settings.enabledPlugins = { "oh-my-claudecode@omc": true, "caveman@caveman": true }` applied at SessionManager layer (default) with per-config override. Empirically validated by 4 live SDK runs. |
| 2 | CRITICAL | No hard budget kill | **FIXED** (Phase 2A Wave 5) | `max_budget_usd` wired to SDK `maxBudgetUsd`. Budget exhaustion (`error_max_budget_usd`) short-circuits to permanent failure, never retries. |
| 3 | HIGH | Persistent-mode hook fights abortController | **FIXED** (three-tier Wave 1, 2026-04-24) | `hooks: {}` now passed explicitly on every SDK Options to block filesystem-discovered hook registration. |
| 4 | HIGH | Crash path doesn't clean up worktrees | **FIXED** (Phase 2A Wave 5) | `cleanupWorktree()` added to `merge_result: "error"` case and catch block. `recoverFromCrash()` now cleans up `failed`-state worktrees. |
| 5 | MEDIUM | Cron/remote triggers escape lifecycle | **FIXED** (three-tier Wave 1, 2026-04-24) | `DEFAULT_DISALLOWED_TOOLS` blocks `CronCreate`, `CronDelete`, `CronList`, `RemoteTrigger`, `ScheduleWakeup` at the SessionManager layer; config-specified additions merge on top. |
| 6 | MEDIUM | `/team` spawns tmux panes outside SDK lifecycle | **FIXED** (three-tier Wave 1, 2026-04-24) | `TmuxOps.killSessionsByPattern('task-{id}*')` invoked on `cleanupWorktree` and in `abortAll` sweep. Failures swallowed so git cleanup always runs. |
| 7 | LOW | No concurrent agent race conditions | **CONFIRMED OK** | Worktree isolation solid. Merge gate FIFO handles contention. `.omc/` exclusion prevents state leaking to trunk. |

### Phase 2B Delivery (2026-04-24 to 2026-04-27)

**Status (2026-04-24):** Plan supersedes original Phase 2B layout. The three-tier Architect/Executor/Reviewer plan integrates Phase 2B into a revised wave sequence.

- **Wave 1** (pre-reqs: OMC plugin loading, hook defense, cron/remote block, tmux cleanup) — committed `d96444f` + `b78a0f7` (+ `getTrunkBranch` fix).
- **Wave 1.5** (state schema extensions + ProjectStore + TaskFile mode/projectId/phaseId + processTask decomposition) — commits `e274036` / `32b459d` / `be323ac` / `34e434c`. +31 tests.
- **Wave 1.75 item 9** (concurrent-session smoke) — `920e02f`. Live test PASS.
- **Wave 2** (Discord outbound: 13 new OrchestratorEvent variants + DiscordNotifier + WebhookSender + sanitize/redactSecrets defense) — commit `0fe90f4`. +45 tests. Multi-perspective review: architect/security/code-reviewer all APPROVE.
- **Wave 3** (Discord inbound: `!task`, `!project`, `!status`, `!abort`, `!retry`, `!reply`; NL classification with deterministic + LLM-fallback stages; MessageAccumulator with 2s debounce + `!` bypass; ReactionClient stub) — commit `07ed8c4`. +36 tests. Security defenses: @everyone/@here sanitize, secret redaction, project name length cap, `!reply` state-gating. Wave 2+3 roundtrip integration test committed as `066bde9`.
- **Wave A** (Reviewer gate: ephemeral Reviewer session, mandatory-for-project review, `review_arbitration` state wiring with interim Wave A→C warning) — commit `414cd45`. +33 tests. Security defenses: untrusted-prompt fencing, stale-review.json defense, expanded disallowlist (`WebFetch`/`WebSearch`/`Task`/`Agent`/`Cron*`/`RemoteTrigger`/`ScheduleWakeup`), spawn+consumeStream fail-safe wrapping.
- **Wave B** (Project lifecycle + Architect session: ArchitectManager with spawn/respawn/decompose/compact/arbitration stubs, orchestrator.declareProject + checkArchitectHealth crash recovery, retry-only guardrail with 3 verdict types — no `executor_correct`) — commit `fddef2b`. +32 tests. Security: fenced operator name/description/nonGoals with untrusted label, phase-file bounds (32KB prompt, phaseId shape), worktree cleanup on spawn failure, description/name/nonGoals drift enforcement in compaction summary.
- **Discord Rich Rendering** (2026-04-26) — multi-line markdown bodies for `session_complete` failure (errors[]+terminalReason), `merge_result` (sha7/file count/error excerpt branches), `task_done` (response level trailer), `task_failed` (attempt prefix), `escalation_needed` (Options/Context expansion), `project_failed` (failedPhase + truncateRationale on reason), `arbitration_verdict` (truncateRationale on rationale). New `truncateBody(1900)` helper for Discord 2000-char cap. Three commits: `e585c3c` (Phase A payload extensions traced to existing type:field — see [[session-log-discord-rich-rendering-2026-04-26]] for field-source matrix), `3fd81a8` (Phase B Commit 1 test scaffolds), `32ce0ea` (Phase B Commit 2 renderer + 16-fixture live-discord-smoke matrix). Plan: `.omc/plans/2026-04-26-discord-conversational-output.md`. Closes Wave C P2 LOW "rationale length-cap + ANSI/control-char strip" (`truncateRationale` from `src/lib/text.ts:66` reused).
- **Wave E-α Discord Identity & Templates** (2026-04-27) — first wave of Phase E split per [[phase-e-agent-perspective-discord-rendering-intended-features]]. Two commits: `66801b0` (mechanical extraction; zero behavior change) + `5bec3dc` (markPhaseSuccess collapse + executor identity + un-skip). NEW: `src/discord/identity.ts` (pure `resolveIdentity` over verbatim 27 OrchestratorEvent variants); `src/discord/epistle-templates.ts` (`renderEpistle(event, identity, ctx)` + `EpistleContext` + `defaultCtx()`; 6 narrative-event templates); `src/lib/review-format.ts` (`formatFindingForOps`); `src/lib/state.ts.markPhaseSuccess` (single re-read pass-by-reference); `tests/lib/no-discord-leak.test.ts` (Architecture Invariant guard). NOTIFIER_MAP wraps 6 entries with `format: (e, ctx?) => renderEpistle(...)`. 755 tests pass. Plan: `.omc/plans/2026-04-26-discord-wave-e-alpha.md`. Wave E-β/γ/δ deferred as separate consensus passes — see [[reference-screenshot-analysis-conversational-discord-operator-st]] for design target gap analysis.

**Pending:** Wave B.5 (Architect smoke test — 5 mock escalations, 3+ resolve), Wave 4 (escalation routing), Wave C (arbitration routing + real verdict parsing + Architect listener consumes `review_arbitration`), Wave 6-split (dialogue: standalone proposal.json vs project Architect channel), Wave D (compaction handoff + e2e validation), Wave E-β (reply-API threading), Wave E-γ (LLM voice per role), Wave E-δ (nudge_check + per-role mention routing).

**Test count progression:** 280 (Phase 2A) → 328 (Waves 1 + 1.5) → 373 (+ Wave 2) → 755 (Wave E-α).

**Live validation to date:** 4 real-SDK runs against scratch repos (minimal, enriched, vague, concurrent) — all PASS. Wave 1 plugins + Phase 2A enrichment + Phase 2A graduated response routing + concurrent isolation all confirmed end-to-end. Live operator dialogue end-to-end validated via Tier 1 A smoke test (2026-04-27). Total live cost ~$0.50.

### Post-2B Testing Options

Items that can validate prompt strength and guardrail effectiveness once Discord integration is complete. Not blockers — informational testing to tune the agent protocol before adding hard gates in Phase 3.

| # | Test Type | What | How | When |
|---|-----------|------|-----|------|
| 1 | **Live agent completion compliance** | Does a real agent produce `completion.json` with all enrichment fields? | Spawn real SDK session against `system-prompt.md` in a worktree with a simple task. Inspect output files. Measure compliance score (0-4). | Post-2B (can run without Discord via task file drop) |
| 2 | **Adversarial ambiguity testing** | Does the agent escalate on deliberately ambiguous/impossible tasks? | Drop tasks with vague prompts. Verify agent writes `escalation.json` with `scope_unclear` or `clarification_needed`. | Post-2B |
| 3 | **Graduated response calibration** | Are the level 0-4 thresholds correctly tuned? | Run N tasks of varying complexity. Histogram `response_level` events. | Post-2B |
| 4 | **Compliance regression tracking** | What % of sessions hit compliance score 4/4 over time? | Aggregate `completion_compliance` events. Track trend. | Ongoing post-2B |
| 5 | **Checkpoint adoption testing** | Do agents actually write checkpoints at decision points? | Run complex multi-file tasks. Check for `checkpoint.json` in worktrees. | Post-2B |
| 6 | **Circuit breaker stress test** | Does retry + auto-escalation + circuit breaker work under realistic failure conditions? | Drop tasks designed to fail. Verify retry → escalation → circuit breaker sequence. | Post-2B |

---

## Phase 3: Review Gate + Dialogue Agent — NOT STARTED

Depends: Phase 2A (escalation protocol), Phase 1.5 (`resumeSession` verification).

**Goal:** Independent review before merge for high-stakes tasks. Dialogue agent for greenfield/ambiguous tasks.

| Item | Location | Effort | Description |
|------|----------|--------|------------|
| **External review gate** | `src/gates/review.ts` (new) | ~100 lines | Spawns separate read-only sonnet session with contrarian prompt. Produces structured verdict. Gates merge. |
| **Review trigger logic** | `src/orchestrator.ts` | ~30 lines | Fires review gate when: `totalCostUsd > threshold`, `filesChanged.length > threshold`, `confidence` assessment has partial/degraded dimensions, or task flag `mode: "reviewed"`. |
| **Dialogue agent** | `src/session/dialogue.ts` (new) | ~80 lines | For build-from-scratch tasks. Agent writes `.harness/proposal.json` → orchestrator pauses → operator reviews → implementation proceeds. |
| **Dialogue routing** | `src/orchestrator.ts` | ~20 lines | Auto-triggered when initial assessment has `unclear`/`guessing` dimensions, or operator sets `mode: "dialogue"` in task file. |
| **Dialogue Discord channel** | `src/discord/dialogue-channel.ts` (new) | ~100 lines | Dedicated Discord channel linked to a persistent OmC instance for pre-pipeline design discussion. Once consensus reached, refined task spec submitted to pipeline. |
| **Review verdict schema** | `src/gates/review.ts` | Part of gate | `{ verdict: "approve"|"reject"|"request_changes", risk_score: {...}, findings: [...] }`. |

**Tests:** Mocked review sessions (same pattern as existing SDK mocks). Integration test: task triggers review, review rejects, task fails. Task triggers review, review approves, merge proceeds.

**Note:** Wave A delivered partial Phase 3 review gate (mandatory-for-project Reviewer session). Standalone dialogue agent + dialogue Discord channel still pending.

---

## Phase 4: Observability + Hardening — NOT STARTED

Depends: Phase 2A-3 functional.

**Goal:** Production-grade monitoring, cost tracking, and reliability.

| Item | Location | Effort | Description |
|------|----------|--------|------------|
| **Structured event log** | `src/lib/events.ts` (new) | ~60 lines | Replace JSONL append with structured event system. Queryable event history. |
| **Cost tracking dashboard** | `src/lib/cost.ts` (new) | ~40 lines | Per-task and aggregate cost tracking. Budget burn rate. Alerts. |
| **Session metrics** | `src/session/sdk.ts` | ~30 lines | Track session duration, turn count, token usage per task. Expose via events. |
| **Health monitoring** | `src/lib/health.ts` (new) | ~50 lines | Daemon health check endpoint. Active session count, queue depth, last poll time, error rate. |
| **Stuck detection** | `src/session/sdk.ts` + `src/orchestrator.ts` | ~30 lines | SDK message stream is the heartbeat — stream silence = stuck. **See "Stall detection" below for expanded requirements.** |
| **Crash recovery hardening** | `src/orchestrator.ts` | ~40 lines | Improve crash recovery: detect stale worktrees, handle partial state, recover from mid-merge crashes. |
| **E2E test suite** | `tests/e2e/` (new directory) | ~200 lines | Tests against real SDK, real git repos. Run manually or in CI with budget cap. |

### Stall detection — observation from autopilot Wave 2/3 runs (2026-04-24)

**Observation:** during Wave 2 and Wave 3 autopilot cycles, the driving agent occasionally paused silently mid-phase without producing terminal output. No error. No "stop" signal. No escalation. Just silence until the operator poked it back to life. This is the *meta-orchestration* analogue of the single-session "SDK stream silence" case — and when it happens in production Executor sessions, the harness currently has no recovery path.

**Why the existing stuck-detection row isn't enough:**
- It's scoped to the SDK message stream within a single session. A silent agent *can still emit tool calls* that look like heartbeats while making no real progress.
- A "stalled-on-thought" session that emits occasional tool_use messages (re-reading files, re-grepping) passes the heartbeat test but is semantically stuck.
- Multi-phase workflows have cross-phase stalls: Phase 3 QA completes, Phase 4 reviewers return, and the orchestrator silently fails to advance to Phase 5. No single session is stuck — the *meta-loop* is.

**Production requirements (Phase 4+ expansion):**
1. **Semantic progress watchdog.** Beyond raw SDK stream silence, track whether the agent has advanced state (new file writes, new commits, new .harness/* signal files). Stream activity without state advance for ≥ N minutes = stuck. Dimension: `last_state_advance_at` per session.
2. **Meta-phase watchdog.** For multi-phase runs, track phase transition timestamps. If the current phase has a documented "next action" but no transition occurs within a budget, fire a **nudge**.
3. **Nudge protocol.** On stall detection, inject a single targeted message into the session: "You appear to have stopped mid-phase. Current phase: {phase}. Next action: {next}. Continue, or report the blocker." Give the agent one chance to recover before aborting.
4. **Telemetry.** Every stall/nudge/abort decision emits a structured event (`stall_detected`, `nudge_sent`, `stall_abort`) with the reason and which watchdog triggered. Critical for tuning thresholds.
5. **Escalation tie-in.** After N failed nudges on the same session/phase, escalate to operator with the stall context.

**Track:** operator-flagged during autopilot Wave 3 debrief (2026-04-24). Add to Phase 4 acceptance criteria when that wave kicks off.

---

## Unscheduled: Future Considerations

| Item | Phase dependency | Description |
|------|-----------------|------------|
| **Event-driven task creation** | Phase 2B+ | Ambient operation. Git push → auto-test task, CI failure → auto-fix task, cron → maintenance. The harness reacts to events, not just prompts. |
| **Earned autonomy** | Phase 4+ | Trust score from N clean merges → reduced oversight. Graduated response thresholds modulate based on track record. |
| **Multi-task concurrency** | Phase 4+ | Multiple agent sessions running simultaneously. Requires concurrent worktrees, merge queue contention handling. |
| **Post-merge monitoring** | Phase 4+ | Watch deploy health after merge. Error rate spike → auto-create rollback or fix task. |
| **Proactive maintenance** | Phase 4+ | Autonomous dep audits, test coverage trending, dead code detection, documentation staleness checks. |
| **Self-improvement loop** | Phase 4+ | Harness can develop itself through the same pipeline. `!update` equivalent. |
| **Trading bot integration** | Phase 2B+ | Project-specific Discord commands, Ozy-aware task routing. |
| **OMC agent tier integration** | Phase 3+ | Fold OMC agent capabilities (analyst, debugger, tracer) into review/dialogue roles. |

---

## Cross-refs

- [[harness-ts-architecture]] — core architecture concepts (modules, state machine, merge, completion)
- [[harness-ts-types-reference-source-of-truth]] — verbatim type signatures
- [[harness-ts-core-invariants]] — load-bearing rules
- [[harness-ts-plan-index]] — index of plans/ files
- [[phase-e-agent-perspective-discord-rendering-intended-features]] — Wave E-α/β/γ/δ split
- [[harness-ts-wave-c-backlog]] — deferred items + P1/P2 follow-ups
