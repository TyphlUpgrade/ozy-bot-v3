---
title: Harness-TS Architecture
tags: [harness-ts, architecture, claude-agent-sdk, typescript, pipeline]
category: architecture
created: 2026-04-11
updated: 2026-04-11
---

# Harness-TS Architecture

TypeScript rewrite of the development harness, built on `@anthropic-ai/claude-agent-sdk`. Replaces the Python asyncio/FIFO/clawhip/signal-file stack with SDK-native agent lifecycle management.

## Vision

Same as Python harness: an **automated dev team** where an operator describes intent and agents do the work. The difference is how agent sessions are managed — SDK `query()` replaces FIFO pipes, tmux, and signal files.

**What changed:**
- FIFO sessions + clawhip tmux → SDK `query()` with `AsyncGenerator<SDKMessage>`
- 7-stage pipeline (classify→architect→executor→reviewer→merge→wiki) → supervised single-session model (agent works → completion signal → merge gate)
- Signal files for escalation/completion → `completion.json` in worktree + future escalation.json
- Python asyncio → Node.js event loop + vitest

**What stayed:**
- Git worktree isolation per task
- Merge queue with rebase-before-merge, test-and-revert
- 9-state task machine with atomic persistence
- Operator communicates via Discord (Phase 2)
- OMC hooks loaded via `settingSources: ["project"]`

## Two-Layer Stack

```text
TypeScript orchestrator (daemon) — task routing, state machine, merge queue
    |
Claude Agent SDK sessions (work) — full CC+OMC sessions doing actual dev work
```

No clawhip layer. No tmux. The SDK handles session lifecycle directly.

## Six Modules

| Module | File | Responsibility |
|--------|------|---------------|
| Config | `src/lib/config.ts` | TOML config loader (smol-toml), typed `HarnessConfig` |
| State | `src/lib/state.ts` | 9-state task machine, atomic JSON persistence, event log |
| SDK | `src/session/sdk.ts` | Thin wrapper around `query()`, stream consumption, abort |
| Session Manager | `src/session/manager.ts` | Worktree lifecycle, session spawn/abort, completion signal |
| Merge Gate | `src/gates/merge.ts` | FIFO queue, rebase, merge --no-ff, test-and-revert |
| Orchestrator | `src/orchestrator.ts` | Daemon main loop, task scan, lifecycle routing |

## Supervised Session Model

The orchestrator is a **daemon managing long-running CC+OMC sessions**, not a pipeline scheduler. Each task gets one agent session in an isolated worktree. The agent's internal workflow (planning, coding, testing) is opaque to the orchestrator.

```text
Task file dropped → Orchestrator picks up
    → Creates worktree + branch (harness/task-{id})
    → Spawns SDK session with prompt + systemPrompt
    → Agent works (opaque — full CC+OMC capabilities)
    → Agent writes .harness/completion.json
    → Orchestrator reads completion signal
    → Routes to merge gate
    → Merge gate: rebase → merge --no-ff → test → done or revert
```

## 9-State Task Machine

```
pending → active → merging → done
                 ↘ failed ↙
         active → reviewing → merging
         active → shelved → pending (retry)
         active → escalation_wait → active (resume)
         active → paused → active (resume)
```

States: `pending`, `active`, `reviewing`, `merging`, `done`, `failed`, `shelved`, `escalation_wait`, `paused`

Transitions enforced by `VALID_TRANSITIONS` record. Atomic persistence via temp-file + rename (O3).

## Merge Queue

Exclusive FIFO. One merge at a time. Pipeline per merge:

1. **Auto-commit** (O7): if worktree has uncommitted changes, `git add --all -- ':!.omc' ':!.harness'` + commit
2. **Rebase**: `git rebase {trunk}` in worktree. Conflict → abort + shelve + schedule retry
3. **Merge**: `git merge --no-ff {branch}` on trunk
4. **Test**: run `test_command` with `test_timeout` (O8). Failure → revert merge
5. **Result**: merged | rebase_conflict | test_failed | test_timeout | error

## Completion Signal

Agent writes `{worktree}/.harness/completion.json`:

```json
{
  "status": "success",
  "commitSha": "abc123",
  "summary": "Fixed the auth bug",
  "filesChanged": ["src/auth.ts", "tests/auth.test.ts"]
}
```

Strict validation: status must be "success" or "failure", commitSha non-empty, all fields required.

## Business Logic Lessons Preserved

| ID | Rule | Implementation |
|----|------|---------------|
| B1 | Sync mutations only | `writeFileSync` + `renameSync` atomic writes |
| B3 | Shelve clock reset | `shelvedAt` set on shelve, cleared on unshelve |
| B5 | Resume at executor | `pre_escalation_stage` captured before escalation |
| B6 | Escalation tier reset | `fromTier` captured before mutation, retryCount reset |
| B7 | Unknown key drop | `KNOWN_KEYS` set, unknown keys silently dropped on deserialize |
| O3 | Atomic writes | UUID temp file + rename |
| O4 | Path traversal | `sanitizeTaskId()` regex `/^[a-zA-Z0-9_-]+$/`, max 128 chars |
| O7 | Auto-commit | Pathspec excludes `':!.omc' ':!.harness'` |
| O8 | Test timeout | Configurable `test_timeout` passed to `runTests()` |
| O9 | Write-only log | JSONL append-only event log |

## Injectable Interfaces

All external dependencies are injectable for testing:

| Interface | Real implementation | Test mock |
|-----------|-------------------|-----------|
| `QueryFn` | SDK `query()` | Returns mock `AsyncGenerator` |
| `GitOps` | `execSync` git commands | `vi.fn()` mocks |
| `MergeGitOps` | `execSync` git commands | `vi.fn()` mocks |

## Configuration

`config/harness/project.toml` — shared between Python archive and TS harness.

Key pipeline settings: `poll_interval` (5s), `test_command`, `max_retries` (3), `test_timeout` (180s), `escalation_timeout` (4h), `retry_delay_ms` (5min).

## Test Coverage

202 tests across 11 files:
- `config.test.ts` (9) — TOML parsing, defaults, errors
- `state.test.ts` (29) — all transitions, persistence, business logic
- `sdk.test.ts` (21) — classify, parse, spawn, stream, abort
- `manager.test.ts` (15) — worktree, spawn, completion validation, abort
- `merge.test.ts` (13) — FIFO, auto-commit, rebase, test timeout, revert
- `orchestrator.test.ts` (18) — scan, lifecycle, merge outcomes, shutdown, crash recovery
- `pipeline.test.ts` (7) — full integration lifecycle
- `validation/git-worktree.test.ts` (14) — real git worktree create, remove, branch lifecycle
- `validation/merge-git.test.ts` (16) — real git merge ops, conflict, timeout, revert
- `validation/merge-pipeline.test.ts` (5) — full MergeGate with real git repos
- `validation/sdk-types.test.ts` (55) — SDK type conformance, compile-time + runtime

## Ambiguity Protection (Current State)

**What exists:** task ID validation, JSON schema check, budget/turn caps, completion signal requirement, test-and-revert merge gate.

**What's missing (Phase 2+):** prompt classification, ambiguity detection, escalation channel, reformulation on rejection, confidence gating, operator feedback channel.

See [[harness-ts-roadmap]] for phased plan.

## Phase Roadmap

### Phase 0+1: Core Pipeline — COMPLETE (2026-04-11)

6 modules, 112 tests (all mocked SDK + git). Committed `2298ad1`.

**Deliverables:** Config loader, 9-state machine, SDK wrapper, session manager, merge gate, orchestrator daemon. Business logic B1/B3/B5/B6/B7/O3/O4/O7/O8/O9 preserved.

**Limitation:** All tests mock SDK and git. Real git worktree operations verified in Phase 1.5. `settingSources` and `resumeSession()` verified against SDK v0.2.101 types (2026-04-12) — see Phase 1.5 resolution notes.

---

### Phase 1.5: Validation — COMPLETE (2026-04-11)

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

### Phase 2A: Pipeline Hardening — COMPLETE (2026-04-11)

Depends: Phase 1.5 validation complete. **273 tests passing (71 new).**

**Goal:** The agent can communicate structured information back to the orchestrator, and the orchestrator routes based on signals. Internal pipeline — no Discord dependency.

| Item | Location | Effort | Description |
|------|----------|--------|------------|
| **systemPrompt content** | `config/harness/system-prompt.md` (loaded by config module) | Prompt-only | Intent classification gate, decision boundaries, simplifier pressure test, completion contract. Ports institutional knowledge from Python `config/harness/agents/*.md` into single prompt. |
| **Completion signal enrichment** | `src/session/manager.ts` (schema), system prompt | ~20 lines | Add `understanding`, `assumptions`, `nonGoals`, `confidence` (structured 5-dimension assessment) to `CompletionSignal`. Validation optional fields. |
| **Escalation protocol** | `src/session/manager.ts` + `src/orchestrator.ts` | ~50 lines | Agent writes `.harness/escalation.json`. Orchestrator detects → transitions to `escalation_wait` → emits `escalation_needed` event. Works without Discord (just pauses task with log entry). |
| **Failure retry + circuit breaker** | `src/orchestrator.ts` | ~40 lines | Completion `status: "failure"` → orchestrator retries with new session (up to `max_retries`). After N failures, auto-escalate instead of silently dropping. Circuit breaker: cap total retries per task before pausing for operator. Replaces Python's reviewer↔executor loop pattern — simpler because single-session model has no inter-session loops. |
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

### Post-2B Testing Options

Items that can validate prompt strength and guardrail effectiveness once Discord integration is complete. Not blockers — informational testing to tune the agent protocol before adding hard gates in Phase 3.

| # | Test Type | What | How | When |
|---|-----------|------|-----|------|
| 1 | **Live agent completion compliance** | Does a real agent produce `completion.json` with all enrichment fields (confidence, understanding, assumptions, nonGoals)? | Spawn real SDK session against `system-prompt.md` in a worktree with a simple task. Inspect output files. Measure compliance score (0-4). | Post-2B (can run without Discord via task file drop) |
| 2 | **Adversarial ambiguity testing** | Does the agent escalate on deliberately ambiguous/impossible tasks instead of guessing? | Drop tasks with vague prompts ("fix the thing", "make it better"). Verify agent writes `escalation.json` with `scope_unclear` or `clarification_needed`. Check confidence dimensions aren't clustered. | Post-2B |
| 3 | **Graduated response calibration** | Are the level 0-4 thresholds correctly tuned for real agent output? | Run N tasks of varying complexity. Collect `response_level` events. Histogram the distribution. If everything clusters at level 1, thresholds need adjustment. | Post-2B |
| 4 | **Compliance regression tracking** | What % of sessions hit compliance score 4/4 over time? | Aggregate `completion_compliance` events. Track trend. Low scores = prompt engineering needs work. This is what Item 8 (compliance event) was designed for. | Ongoing post-2B |
| 5 | **Checkpoint adoption testing** | Do agents actually write checkpoints at decision points? | Run complex multi-file tasks. Check for `checkpoint.json` in worktrees. If agents never write checkpoints, the prompt section needs strengthening before Phase 3 gating. | Post-2B |
| 6 | **Circuit breaker stress test** | Does retry + auto-escalation + circuit breaker work under realistic failure conditions? | Drop tasks designed to fail (bad prompts, impossible constraints). Verify retry → escalation → circuit breaker sequence fires correctly. Check `tier1EscalationCount` increments and caps. | Post-2B |

---

### Phase 2B Pre-Requisites (from Critic/Architect Review, 2026-04-12)

Items that must be resolved before or alongside Phase 2B Discord integration:

| # | Severity | Finding | Status | Resolution |
|---|----------|---------|--------|------------|
| 1 | CRITICAL | `settingSources: ["project"]` doesn't load OMC plugins (user-level `enabledPlugins`) | **FIXED** (three-tier Wave 1, 2026-04-24) | Option C shipped: `Options.settings.enabledPlugins = { "oh-my-claudecode@omc": true, "caveman@caveman": true }` applied at SessionManager layer (default) with per-config override. Empirically validated by 4 live SDK runs. |
| 2 | CRITICAL | No hard budget kill | **FIXED** (Phase 2A Wave 5) | `max_budget_usd` wired to SDK `maxBudgetUsd`. Budget exhaustion (`error_max_budget_usd`) short-circuits to permanent failure, never retries. |
| 3 | HIGH | Persistent-mode hook fights abortController | **FIXED** (three-tier Wave 1, 2026-04-24) | `hooks: {}` now passed explicitly on every SDK Options to block filesystem-discovered hook registration. |
| 4 | HIGH | Crash path doesn't clean up worktrees | **FIXED** (Phase 2A Wave 5) | `cleanupWorktree()` added to `merge_result: "error"` case and catch block. `recoverFromCrash()` now cleans up `failed`-state worktrees. |
| 5 | MEDIUM | Cron/remote triggers escape lifecycle | **FIXED** (three-tier Wave 1, 2026-04-24) | `DEFAULT_DISALLOWED_TOOLS` blocks `CronCreate`, `CronDelete`, `CronList`, `RemoteTrigger`, `ScheduleWakeup` at the SessionManager layer; config-specified additions merge on top. |
| 6 | MEDIUM | `/team` spawns tmux panes outside SDK lifecycle | **FIXED** (three-tier Wave 1, 2026-04-24) | `TmuxOps.killSessionsByPattern('task-{id}*')` invoked on `cleanupWorktree` and in `abortAll` sweep. Failures swallowed so git cleanup always runs. |
| 7 | LOW | No concurrent agent race conditions | **CONFIRMED OK** | Worktree isolation solid. Merge gate FIFO handles contention. `.omc/` exclusion prevents state leaking to trunk. |

---

### Phase 2B: Discord Integration — PARTIAL (Wave 2 outbound complete)

Depends: Phase 2A escalation protocol. Phase 2B pre-requisites #1, #3, #5, #6 resolved in three-tier Wave 1 (2026-04-24). See `.omc/plans/ralplan-harness-ts-three-tier-architect.md` Wave 1 for details.

**Status (2026-04-24):** Plan supersedes original Phase 2B layout. The three-tier Architect/Executor/Reviewer plan integrates Phase 2B into a revised wave sequence. Completed:
- **Wave 1** (pre-reqs: OMC plugin loading, hook defense, cron/remote block, tmux cleanup) — committed `d96444f` + `b78a0f7` (+ `getTrunkBranch` fix).
- **Wave 1.5** (state schema extensions + ProjectStore + TaskFile mode/projectId/phaseId + processTask decomposition) — commits `e274036` / `32b459d` / `be323ac` / `34e434c`. +31 tests.
- **Wave 1.75 item 9** (concurrent-session smoke) — `920e02f`. Live test PASS.
- **Wave 2** (Discord outbound: 13 new OrchestratorEvent variants + DiscordNotifier + WebhookSender + sanitize/redactSecrets defense) — commit `0fe90f4`. +45 tests. Multi-perspective review: architect/security/code-reviewer all APPROVE.

**Pending:** Wave 3 (Discord inbound: `!task`, `!project`, NL routing, accumulator), Wave A (Reviewer gate), Wave B/B.5 (Architect lifecycle + smoke), Wave 4 (escalation routing), Wave C (arbitration), Wave 6-split (dialogue), Wave D (compaction handoff + e2e).

**Test count progression:** 280 (Phase 2A) → 328 (Waves 1 + 1.5) → 373 (+ Wave 2).

**Live validation to date:** 4 real-SDK runs against scratch repos (minimal, enriched, vague, concurrent) — all PASS. Wave 1 plugins + Phase 2A enrichment + Phase 2A graduated response routing + concurrent isolation all confirmed end-to-end. Total live cost ~$0.50.

**Goal:** Operator can submit tasks, see pipeline events, and respond to escalation — all via Discord.

| Item | Location | Effort | Description |
|------|----------|--------|------------|
| **Event → Discord notifications** | `src/discord/notifier.ts` (new) | ~100 lines | Listen to orchestrator events, post to Discord channels. Stage transitions, completions, failures, escalations. |
| **Operator task submission (commands + NL)** | `src/discord/commands.ts` (new) | ~100 lines | `!task` commands AND natural language messages create task files. NL path: single LLM classify call to disambiguate intent (new task vs feedback vs status query). Deterministic routing for structured commands, LLM only for NL ambiguity. |
| **Escalation response** | `src/discord/escalation.ts` (new) | ~60 lines | Operator responds to escalation notification → response written to task context → session resumed via `resumeSession()` or new session spawned with operator input (fallback if resume unavailable). |
| **Escalation dialogue (multi-turn)** | `src/discord/escalation.ts` | ~40 lines | Structured multi-turn conversation during escalation. Operator and agent exchange messages until resolution. Resolution detection: LLM classify (resolution vs continuation), defaults to continuation (safe — keeps dialogue open). |
| **Webhook per-agent identity** | `src/discord/notifier.ts` | ~20 lines | Messages sent via Discord webhook with agent-specific username/avatar. Configurable via `[discord.agents.*]` in project.toml with hardcoded fallback defaults. |
| **Message accumulator** | `src/discord/accumulator.ts` (new) | ~40 lines | 2s debounce window for rapid NL messages. `!` commands bypass immediately. Accumulated text processed as single coherent message. Prevents split-message misrouting. |
| **Reaction acknowledgments** | `src/discord/notifier.ts` | ~10 lines | Cosmetic receipt confirmation: eyes on receive, checkmark on success, X on error. Never blocks processing. |

**Tests:** Unit tests with mocked Discord client. Integration test: submit task via Discord, verify pipeline runs, verify events posted. Accumulator: verify debounce, verify `!` bypass, verify multi-message concatenation.

---

### Phase 3: Review Gate + Dialogue Agent — NOT STARTED

Depends: Phase 2A (escalation protocol), Phase 1.5 (`resumeSession` verification).

**Goal:** Independent review before merge for high-stakes tasks. Dialogue agent for greenfield/ambiguous tasks.

| Item | Location | Effort | Description |
|------|----------|--------|------------|
| **External review gate** | `src/gates/review.ts` (new) | ~100 lines | Spawns separate read-only sonnet session with contrarian prompt (ported from Python `agents/reviewer.md`). Produces structured verdict. Gates merge. |
| **Review trigger logic** | `src/orchestrator.ts` | ~30 lines | Fires review gate when: `totalCostUsd > threshold`, `filesChanged.length > threshold`, `confidence` assessment has partial/degraded dimensions, or task flag `mode: "reviewed"`. |
| **Dialogue agent** | `src/session/dialogue.ts` (new) | ~80 lines | For build-from-scratch tasks. Agent writes `.harness/proposal.json` → orchestrator pauses → operator reviews → implementation proceeds. Single-session-with-pause if `resumeSession` works, two-session fallback otherwise. |
| **Dialogue routing** | `src/orchestrator.ts` | ~20 lines | Auto-triggered when initial assessment has `unclear`/`guessing` dimensions, or operator sets `mode: "dialogue"` in task file. |
| **Dialogue Discord channel** | `src/discord/dialogue-channel.ts` (new) | ~100 lines | Dedicated Discord channel linked to a persistent OmC instance for pre-pipeline design discussion. Operator hashes out broad implementation details (architecture, scope, constraints, trade-offs) conversationally before the task enters the pipeline. The channel is essentially a ralplan/deep-interview surface — once the operator and agent reach consensus on the approach, the refined task spec is submitted to the pipeline as a fully-scoped task. Prevents vague tasks from burning session retries. Separates "what should we build" (dialogue channel) from "build it" (pipeline). |
| **Review verdict schema** | `src/gates/review.ts` | Part of gate | `{ verdict: "approve"|"reject"|"request_changes", risk_score: {...}, findings: [...] }`. Ported from Python reviewer verdict format. |

**Tests:** Mocked review sessions (same pattern as existing SDK mocks). Integration test: task triggers review, review rejects, task fails. Task triggers review, review approves, merge proceeds.

---

### Phase 4: Observability + Hardening — NOT STARTED

Depends: Phase 2A-3 functional.

**Goal:** Production-grade monitoring, cost tracking, and reliability.

| Item | Location | Effort | Description |
|------|----------|--------|------------|
| **Structured event log** | `src/lib/events.ts` (new) | ~60 lines | Replace JSONL append with structured event system. Queryable event history for debugging and analytics. |
| **Cost tracking dashboard** | `src/lib/cost.ts` (new) | ~40 lines | Per-task and aggregate cost tracking. Budget burn rate. Alert when approaching daily/weekly limits. |
| **Session metrics** | `src/session/sdk.ts` | ~30 lines | Track session duration, turn count, token usage per task. Expose via events. |
| **Health monitoring** | `src/lib/health.ts` (new) | ~50 lines | Daemon health check endpoint. Active session count, queue depth, last poll time, error rate. |
| **Stuck detection** | `src/session/sdk.ts` + `src/orchestrator.ts` | ~30 lines | SDK message stream is the heartbeat — stream silence = stuck. Configurable timeout (default 5min no messages). Distinguishes active (messages flowing) from stuck (silence). On stuck: abort session, retry or escalate depending on circuit breaker count. |
| **Crash recovery hardening** | `src/orchestrator.ts` | ~40 lines | Improve crash recovery: detect stale worktrees, handle partial state, recover from mid-merge crashes. |
| **E2E test suite** | `tests/e2e/` (new directory) | ~200 lines | Tests against real SDK (costs money), real git repos. Run manually or in CI with budget cap. Validates Phase 1.5 items permanently. |

---

### Unscheduled: Future Considerations

| Item | Phase dependency | Description |
|------|-----------------|------------|
| **Event-driven task creation** | Phase 2B+ | Ambient operation — the harness's killer differentiator. Git push → auto-test task, CI failure → auto-fix task, cron → maintenance (dep audit, coverage trending, dead code). The harness reacts to events, not just prompts. |
| **Earned autonomy** | Phase 4+ | Trust score from N clean merges → reduced oversight. Graduated response thresholds modulate based on track record. Trust decays on failures. Operator configures gradient via Discord. |
| **Multi-task concurrency** | Phase 4+ | Multiple agent sessions running simultaneously. Requires concurrent worktrees, merge queue contention handling. Thread-per-task Discord UX solves message routing when multiple sessions active. |
| **Post-merge monitoring** | Phase 4+ | Watch deploy health after merge. Error rate spike → auto-create rollback or fix task. Closes the feedback loop without operator. |
| **Proactive maintenance** | Phase 4+ | Autonomous dep audits, test coverage trending, dead code detection, documentation staleness checks. Steady stream of small tasks without operator prompting. |
| **Self-improvement loop** | Phase 4+ | Harness can develop itself through the same pipeline. `!update` equivalent. |
| **Trading bot integration** | Phase 2B+ | Project-specific Discord commands, Ozy-aware task routing. |
| **OMC agent tier integration** | Phase 3+ | Fold OMC agent capabilities (analyst, debugger, tracer) into review/dialogue roles. |

## Cross-References

- [[harness-ts-ambiguity-protections]] — Prior art analysis and TS translation
- [[harness-ts-graduated-response]] — Signal-driven escalation levels, structured confidence
- [[v5-harness-efficiency-proposal]] — Design rationale for the TS rewrite
- [[v5-harness-supervised-session-architecture]] — Supervised session model design doc
- [[v5-harness-lessons-learned]] — Institutional knowledge extracted before rewrite
- [[v5-harness-architecture]] — HISTORICAL: Python harness architecture
