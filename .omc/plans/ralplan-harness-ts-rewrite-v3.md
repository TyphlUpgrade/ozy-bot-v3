# Harness TypeScript Rewrite — Consensus Plan v3

**Status:** CONSENSUS APPROVED — Architect APPROVE + Critic APPROVE (iteration 2, 2026-04-11)
**Created:** 2026-04-11
**Supersedes:** `ralplan-harness-ts-rewrite-v2.md` (v2, CONSENSUS APPROVED 2026-04-11)
**Source:** v2 plan + supervised session architecture discussion + concurrency design

---

## What Changed From v2

1. **Pipeline replaced with supervised session model.** The orchestrator is a daemon that manages long-running CC+OMC agent sessions, not a stage-based pipeline. Agent internal workflow (planning, coding, testing, iteration) is opaque to the orchestrator. Structural gates fire only when the agent signals completion.
2. **Merge queue with rebase-before-merge.** v2 had shelve-and-notify on conflict. v3 adds rebase-first (handles stale-base silently), exclusive merge queue (serialized integration), and conflict prediction (worktree diff scanning). Shelve is the fallback after rebase fails.
3. **Operator interjection model.** Messages flow into active sessions at any time via Discord. No stage-based routing. Thread-per-task solves multi-session routing.
4. **Context management by orchestrator.** Token tracking, compression triggers, stale context refresh, resume vs. fresh session decisions — all managed mechanically.
5. **Classification corrected.** LLM classification for Discord input (human messages are ambiguous). Deterministic routing only for machine-generated events.
6. **Line estimate reduced.** ~1,650 lines (down from 1,800-2,200) because the orchestrator doesn't manage internal pipeline stages. Merge queue + conflict prediction budgeted at ~250 + ~60 (split into separate files after Critic review).

---

## RALPLAN-DR Summary

### Principles

1. **Prototype before committing** — SDK+OMC hook loading is the blocking unknown. No implementation work until the prototype validates or invalidates.
2. **The harness is a supervisor, not a scheduler** — It manages sessions, enforces gates, handles crashes. It does not micromanage the agent's internal workflow. OMC handles planning, execution, internal review.
3. **Rewrite the code, port the knowledge** — Design around the SDK. The 17 surviving business logic lessons are mandatory pre-reading. They encode edge cases the rewrite will rediscover.
4. **Protocol boundaries are load-bearing** — Harness and trading bot communicate via filesystem/git/Discord, never shared code. Clawhip sits at this boundary.
5. **Structural guarantees earn the harness's existence** — Independent review, merge-test-revert, crash recovery, merge queue, conflict handling. If the harness can't enforce these, it's overhead.

### Decision Drivers

1. **SDK capability verification** — Does `settingSources: ["project"]` load OMC hooks? Determines whether agents get full OMC or bare CC. Entire value proposition pivots on this.
2. **Operator experience** — Must feel like conversational development with CC+OMC via Discord. Not dispatching work through rigid stages. Interjection at any time.
3. **Reversibility** — Python harness works today. Migration plan preserves ability to retreat at every phase.

### Viable Options

#### Option A: Supervised Session Rewrite (Recommended)

TypeScript daemon managing CC+OMC agent sessions via Claude Agent SDK. **~1,650 lines estimated.** Orchestrator is a process supervisor with structural gates, not a pipeline scheduler. Discord.js for inbound, clawhip for monitoring/outbound.

**Pros:**
- Operator experience matches conversational CC+OMC development
- Agent gets full OMC toolkit (ralplan, ultrawork, code-reviewer) — no reimplementation
- Pipeline complexity eliminated — orchestrator doesn't track internal agent stages
- Merge queue + rebase handles concurrency systematically
- Fewer lines = less to maintain, less to break

**Cons:**
- More autonomy to agent — if OMC hooks don't load, agent is bare CC with less structure
- Harder to debug — agent's internal workflow is opaque to orchestrator
- Merge queue serializes integration — throughput limited by test suite speed
- SDK is a hard dependency on Anthropic's continued maintenance

**Invalidation of alternatives:**
- Pipeline model (v2): Creates friction with conversational operator experience. Orchestrator manages stages the agent handles internally via OMC. Reimplements OMC's planning/review pipeline.
- Python+bridge: Permanent serialization overhead. Bridge grows fat for mid-session control.
- Incremental hybrid: Two languages permanently. "Incremental migration" = "permanent halfway state."

#### Option B: Pipeline Rewrite (v2 approach)

TypeScript with multi-stage pipeline: classify → agent → review → merge → wiki. Orchestrator manages stage transitions.

**Pros:** More visible control flow, easier debugging, familiar pattern from Python harness.
**Cons:** Reimplements what OMC does (planning stages, internal review). Rigid operator experience. More code (~1,800-2,200 lines).

#### Option C: Parallel Runner

TS handles simple tasks, Python stays primary for complex. Migrate per-gate.

**Pros:** Maximum reversibility, per-gate testability.
**Cons:** Operational confusion, historically stalls at "TS does easy, Python does hard" forever.

### Pre-Mortem (Deliberate Mode)

**Scenario 1: SDK doesn't load OMC hooks.**
Agents are bare CC with tools but no ralph/ultrawork/skills. The "intelligence lives in OMC" thesis weakens.

*Impact:* Agent can't plan via ralplan or self-review via code-reviewer internally. Orchestrator must add more structure — potentially reintroducing pipeline stages.
*Mitigation:* Phase 0 prototype gate. If hooks fail, evaluate options below.
*Decision gate:* Prototype result determines architecture shape.

*Scenario 1 Contingency (if OMC hooks don't load):*
The supervised session model survives but gets thicker. Without OMC, agents can't self-plan (no ralplan) or self-review (no code-reviewer). The orchestrator reintroduces two managed stages:
- **Plan stage:** Orchestrator spawns a planning `query()` (system prompt: "plan this task"), collects plan output, presents to operator for approval (or auto-approves simple tasks).
- **Review stage:** Already exists as independent review gate (Phase 3). Becomes mandatory for all tasks, not just complex ones.
The executor agent still runs as a supervised session (no pipeline for execution), but is bookended by orchestrator-managed plan and review. Revised estimate: ~1,980-2,180 lines (+400-600). Phase timeline extends ~3-4 days. This is essentially Option A with Option B's plan/review stages grafted on — worse than full OMC, but still better than the Python harness because SDK eliminates transport code and merge queue handles concurrency.

**Scenario 2: Rewrite takes 3x longer than estimated.**
Real implementation hits SDK quirks, Discord.js edge cases, clawhip integration friction. 3,000+ lines, weeks of work.

*Impact:* Two codebases in limbo.
*Mitigation:* Per-phase time gates. Any phase >2x → pause and present options. Python never deleted.
*Decision gate:* Phase 1 >1 week = stop. Total >3 weeks = consider Option C.
*Sunk-cost guard:* Decision to continue must compare remaining work to Option C's cost from scratch — not factor in work already done.

**Scenario 3: Anthropic changes/deprecates SDK API.**
*Mitigation:* Pin SDK version. Wrap `query()` in adapter. Adapter is ~50-80 lines.

**Scenario 4: Agent produces plausible but broken code that reaches trunk.**
*Mitigation:* Three layers: (1) merge-test-revert runs FULL existing test suite, (2) independent reviewer, (3) post-merge monitoring (future). Layer 1 is minimum viable guarantee.

**Scenario 5: Merge conflicts under concurrent workload.**
Two agents edit overlapping files. Rebase fails.

*Impact:* Agent work wasted if shelved without rebase attempt.
*Mitigation:* Rebase-before-merge handles stale base silently. Exclusive merge queue prevents simultaneous merges. Conflict prediction warns agents of overlap. Shelve + auto-retry (max 3) for persistent conflicts. Operator escalation as backstop.
*Operational note:* With 2 concurrent workers on single repo, overlapping edits are common. Rebase-first makes most conflicts invisible. Persistent conflicts (high-contention files) escalate to operator.

---

## Lessons That Survive the Rewrite

17 of 27 lessons survive the architecture change. Full detail in `.omc/wiki/v5-harness-lessons-learned.md`. **Required pre-implementation reading.**

### Business Logic (must be reimplemented)

| ID | Lesson | Phase | Note |
|----|--------|-------|------|
| B1 | Mutation queue — sync state writes within callbacks | 1 | Node.js event loop makes this natural IF writes are synchronous |
| B2 | Clear stale markers on state transitions | 1 | Any event system has TOCTOU |
| B3 | Shelved task escalation clock reset | 1 | Time shelved != time toward timeout |
| B4 | New message clears pending confirmation | 2 | Stale confirmation must not fire on unrelated input |
| B5 | Resume at executor not reviewer after auto-escalation (BUG-024) | 1 | Prevents infinite reviewer-resume loop |
| B6 | Auto-escalation resets retry count (BUG-023) | 1 | Escalated tasks start at tier 1 |
| B7 | Drop unknown state keys on load | 1 | Defensive deserialization for schema evolution |
| B9 | Crash during dialogue reverts to safe stage | 1 | Ephemeral context doesn't survive crash |

### Operational (must be designed in)

| ID | Lesson | Phase | Note |
|----|--------|-------|------|
| O1 | Safe defaults on LLM failure | 1 | classify -> "complex", confidence -> "low" |
| O3 | Atomic writes (temp + rename) | 1 | writeFileSync + renameSync, never async between read/write |
| O4 | Path traversal validation on task IDs | 1,2 | Untrusted Discord input |
| O5 | Confidence gating promotes on unknown | 1 | Unknown -> operator, don't silently resolve |
| O6 | Informational escalations don't block | 1 | FYI -> Discord, pipeline continues |
| O7 | Auto-commit before merge attempt | 1 | Agents forget; verify mechanically |
| O8 | Test timeout with revert | 1 | 180s hard kill, revert merge |
| O9 | Event log is write-only | 1 | Audit never affects control flow |
| O10 | Multi-message debounce (2s) | 2 | Discord burst buffering |
| O11 | Gateway reconnect dedup | 2 | Bounded set for seen message IDs |

### Eliminated by SDK

T1-T5 (FIFO/tmux), B8 (reply injection — SDK resume handles), B10 (Python closure bug), O2 (zombie reaping — AbortController), O12 (tool-blocking — explicit allowedTools).

---

## Implementation Plan

### Phase 0: SDK Prototype (1 day) — BLOCKING GATE

**Goal:** Verify SDK capabilities. Everything waits.

**Tests:**
1. **Hook loading:** `query()` with `settingSources: ["project"]`, check OMC hooks fire.
2. **AbortController:** Abort after 5s, verify clean termination.
3. **Session resume:** Capture session_id, kill, resume. Verify context survives.
4. **Cost tracking:** Verify `usage` in result messages.

**Acceptance criteria:**
- [ ] `query()` completes, typed message stream works
- [ ] AbortController cleanly terminates
- [ ] Session resume restores context
- [ ] Usage/cost data present
- [ ] OMC hooks fire — OR document failure, revise per Scenario 1

### Phase 1: Daemon + Session Manager + Merge Gate (5-6 days)

**Goal:** TypeScript daemon that spawns an SDK agent session, monitors it, enforces merge-test-revert with merge queue, and handles crashes.

**Pre-implementation:** Read lessons B1-B9, O1, O3-O9.

**Locked scope:**

*Daemon:*
- `src/daemon.ts` — Process entry, graceful shutdown, signal handling
- `src/orchestrator.ts` — Main loop: session monitoring, liveness (stream activity + tool-use detection), timeout, completion detection (agent writes to `{worktree}/.harness/completion.json` with required schema: `{ status, commitSha, summary, filesChanged }` — orchestrator verifies SHA exists on worktree branch before entering merge queue)
- `src/lib/config.ts` — TOML loader from `config/harness/project.toml`

*Session manager:*
- `src/session/manager.ts` — Spawn `query()` with worktree cwd, system prompt, tools, budget. Resume sessions. Kill on timeout. Track active sessions. Worktree creation/cleanup.
- `src/session/context.ts` — Token tracking (accumulate SDK `usage`). Compression trigger at threshold (~80k tokens). **Invariant: compression only triggers between turns (after result message, before next prompt), never mid-stream.** Fresh session reconstruction from: task description, git diff, last summary, operator messages. Resume vs. fresh decision logic.

*State:*
- `src/lib/state.ts` — TaskState: `pending | active | reviewing | merging | done | failed | shelved | escalation_wait | paused`. Synchronous mutations only (B1). Atomic writes (O3). Defensive deserialization (B7). Crash recovery: load last valid, revert ephemeral stages (B9). Escalation circuit breaker with tier reset (B5, B6). Shelve/unshelve with clock reset (B3).
- Path traversal validation on task IDs (O4). Safe LLM defaults (O1). Confidence gating (O5). Informational escalations don't block (O6). Event log write-only (O9).

*Merge gate with queue:*
- `src/gates/merge.ts`:
  1. Exclusive merge queue — one merge at a time, FIFO ordering
  2. Auto-commit worktree changes excluding `.omc/` (O7)
  3. Rebase onto current trunk
  4. If rebase conflict: auto-resolve attempt, else shelve + auto-retry after 5min cooldown (max 3, then operator escalation)
  5. If rebase clean: `git merge --no-ff` to trunk
  6. Run full test suite with 180s timeout (O8)
  7. Test fail/timeout -> revert, state = "failed"
  8. Test pass -> advance to done

*Conflict prediction (advisory, non-blocking):*
- `src/gates/conflicts.ts` — Periodic `git diff --name-only` scan of active worktrees. Flag overlapping file sets between sessions. Inject advisory to agent if overlap detected early.

**Explicitly deferred:**
- Discord (Phase 2)
- Independent review gate (Phase 3)
- Event-driven task creation (Phase 4)
- Thread-per-task UX (Phase 4)
- Wiki stage (Phase 4)
- Agent-driven conflict resolution (Future)
- Earned autonomy (Future)

**Acceptance criteria:**
- [ ] Drop task JSON -> agent session spawns in worktree, works, signals completion
- [ ] Completion -> merge queue -> rebase -> test -> merge succeeds -> result written
- [ ] Stale base -> rebase succeeds silently, merge proceeds
- [ ] Textual conflict -> rebase fails -> shelve + auto-retry after cooldown
- [ ] 3 failed rebases -> escalate to operator
- [ ] Two tasks completing near-simultaneously -> merge queue serializes (second waits, then rebases)
- [ ] Test failure -> revert, state = "failed"
- [ ] Test timeout (180s) -> kill, revert
- [ ] Agent timeout (AbortController) -> state "failed", worktree cleaned
- [ ] State survives process restart (crash recovery)
- [ ] Context compression triggers at token threshold, fresh session reconstructed
- [ ] Synchronous state writes (no async interleave — verified by test)
- [ ] Shelve/unshelve preserves full task context, resets escalation clock on unshelve (B3)
- [ ] Escalation circuit breaker fires after N retries, resets at tier 1 on auto-escalation (B5, B6)
- [ ] Unknown state keys dropped on load without crash (B7 — forward compatibility)
- [ ] Auto-commit catches uncommitted worktree changes before merge attempt (O7)
- [ ] Tests pass with mocked SDK

*Note on B9 (crash during ephemeral stage):* v3's state machine has no ephemeral sub-states (no `escalation_dialogue`). Operator messages flow into sessions via Discord, not through an ephemeral dialogue state. Crash during `reviewing` → restart review (fresh instance). Crash during `active` → resume session. No special revert logic needed — architecturally eliminated.

### Phase 2: Discord Companion (2-3 days)

**Goal:** Operator converses with agents via Discord. Bidirectional, anytime.

**Pre-implementation:** Read lessons B4, O10, O11.

**Locked scope:**
- `src/discord/relay.ts` — Discord.js inbound gateway, thread routing (thread = session, #general = orchestrator), clawhip outbound for notifications
- `src/discord/commands.ts` — `!task`, `!status`, `!pause`, `!resume`, `!tell`, `!caveman`
- `src/discord/accumulator.ts` — 2s debounce for multi-message bursts (O10)
- Message dedup via bounded Set for reconnect replays (O11)
- New message clears pending confirmation (B4)
- Operator messages -> active session via `resume(sessionId)` or new turn
- Agent milestone updates via clawhip (prompted in system prompt, not polled)
- Thread-per-task: `!task` creates Discord thread, all task communication in thread

**Classification:**
- Discord messages: LLM classification (keeps `classify()` pattern, fails safe to "complex")
- Machine events (Phase 4): deterministic routing

**Complexity assessment (three-source union):**
- Operator flag -> always triggers review (can never be downgraded by agent)
- Agent self-escalation -> agent reports complexity during work, can only escalate up
- Mechanical triggers -> orchestrator checks diff post-completion (file count, paths, diff size)

**Acceptance criteria:**
- [ ] `!task fix auth bug` -> thread created, agent spawns, works, result reported in thread
- [ ] Operator message mid-task -> reaches agent session, agent adapts
- [ ] Natural language status question -> agent responds conversationally
- [ ] `!status` -> all active sessions with task, elapsed time, cost
- [ ] `!pause` / `!resume` -> freeze/unfreeze
- [ ] Multi-message burst -> debounced (2s)
- [ ] Reconnect -> duplicates filtered
- [ ] New message clears stale pending confirmation (B4 — prevents old "yes" firing on unrelated input)
- [ ] Command parsing tests pass (ported from Python ~40 specs)

### Phase 3: Independent Review Gate (1-2 days)

**Goal:** Complex tasks get independent review before merge.

**Locked scope:**
- `src/gates/reviewer.ts` — After agent signals completion, IF complexity triggers fire:
  - Spawn fresh `query()` with reviewer role
  - Read-only tools (`allowedTools: ["Read", "Glob", "Grep"]`)
  - `cwd` = committed branch (not working directory)
  - Prompt: diff + acceptance criteria + codebase access. NOT executor's conversation.
  - Full isolation by default — reviewer doesn't see operator messages or task context beyond diff
- Feedback loop: rejection -> feedback to agent session, agent fixes, re-review. Max 3 rounds -> escalate to operator.
- Simple tasks (no triggers) skip review, go straight to merge queue.

**Acceptance criteria:**
- [ ] Task with >3 files or sensitive paths -> reviewer spawns
- [ ] Reviewer cwd = committed branch
- [ ] APPROVE -> merge queue. REJECT -> feedback to agent, agent continues.
- [ ] 3 rejections -> operator escalation
- [ ] Simple task -> skips review, merges directly

### Phase 4: Events + Wiki + Polish (3-4 days)

**Goal:** Ambient operation. Harness reacts to events. Multiple tasks in parallel.

**Locked scope:**
- `src/events/clawhip.ts` — Git push, file changes via clawhip bridge -> auto-task creation
- `src/events/cron.ts` — Scheduled tasks (dependency audit, test suite)
- Concurrency cap (default 2) with merge queue handling serialized integration
- `src/lib/wiki.ts` — Agent writes post-merge summary (has full context at that point)
- `src/lib/audit.ts` — Structured logging, cost aggregation, error classification

**Acceptance criteria:**
- [ ] Git push -> harness auto-creates "run tests" task
- [ ] Two independent tasks run concurrently in separate worktrees
- [ ] Merge queue serializes their integration correctly
- [ ] Conflict prediction flags overlapping file sets
- [ ] Completed tasks produce wiki entries
- [ ] Audit log shows per-task cost, duration, gate decisions

---

## Cutover Plan

**Python harness is never deleted.** Migration checkpoints:

1. **Phase 0 complete:** SDK verified. Python still primary.
2. **Phase 1 complete:** TS processes file-dropped tasks with merge queue. Python handles Discord.
3. **Phase 2 complete:** TS handles Discord. **Parallel-run begins.** TS primary, Python monitors.
4. **Phase 3 complete:** Independent review. Python enters maintenance-only.
5. **Phase 4 complete + 10 successful e2e tasks:** Python retired. `harness/` preserved in git history.

**Rollback:** Stop TS, restart Python. Active TS tasks lost, Python picks up new tasks.

**Phase time gates:** Any phase >2x estimated -> pause, present options: (a) continue, (b) Option C, (c) abandon. **Sunk-cost guard:** Compare remaining work to Option C from scratch, not factor in work done.

---

## Test Plan

### Unit Tests
- **State machine:** All transitions, invalid rejected, persistence, crash recovery, shelve/unshelve with clock reset, escalation counter, sync mutations (verify no async interleave), defensive deserialization
- **Session manager:** Mock SDK query(), spawn/resume/kill, worktree lifecycle, liveness classification
- **Context manager:** Token accumulation, compression trigger threshold, fresh session reconstruction, resume-vs-fresh logic
- **Merge gate:** Mock git: rebase (clean + conflict), merge queue exclusivity (two concurrent -> serialized), auto-commit (O7), test timeout (O8), revert on fail, conflict -> shelve, retry cap (3) -> escalation
- **Conflict prediction:** Overlapping file detection from mock worktree diffs
- **Escalation:** Circuit breaker, tier reset (B6), resume at executor (B5), confidence gating (O5)
- **Discord commands:** Parse all commands, edge cases, path traversal (O4)
- **Accumulator:** Debounce (O10), reconnect dedup (O11), confirmation clearing (B4)
- **Config:** TOML parsing, agent definitions from config, default values, timeout config, invalid config rejection
- **Classification:** LLM for Discord input, deterministic for events, safe default (O1)

### Integration Tests
- **Full lifecycle:** Mock SDK -> session spawns -> agent works -> completion signal -> merge queue -> rebase -> test -> merge -> done
- **Concurrent merge:** Two sessions complete near-simultaneously -> queue serializes, second rebases onto first's merge
- **Rebase conflict flow:** Overlapping edits -> rebase fails -> shelve -> auto-retry -> fresh rebase succeeds
- **Operator interjection:** Mid-task Discord message -> reaches agent via resume -> agent adapts
- **Review loop:** Complex task -> reviewer rejects -> feedback to agent -> agent fixes -> re-review -> approve -> merge
- **Escalation flow:** Agent fails -> retry -> circuit breaker fires -> state = escalation_wait -> operator responds in Discord thread -> session resumes as active with operator guidance
- **Crash recovery:** Kill process mid-task -> restart -> session resumes from state
- **Context compression:** Long session hits token threshold -> orchestrator triggers compression -> fresh session with reconstructed context
- **Discord flow:** Discord.js message -> thread created -> agent works -> clawhip updates -> result in thread

### E2E Tests (gated behind `--e2e`, real API calls)
- **Haiku smoke:** Real `query()`, trivial task, verify result structure
- **Full flow:** Discord message -> session -> agent -> review -> merge-test -> wiki -> Discord report
- **Concurrent:** Two real tasks, verify merge queue and rebase
- **Session resume:** Start -> kill -> restart -> resume -> complete

### Observability
- Structured logging: session spawn, completion, gate decisions, merge results, rebase outcomes, conflicts, escalations — all with task ID, timestamp, duration
- Cost tracking: per-session USD from SDK results, aggregated in audit log
- Error classification: SDK / agent / merge / conflict / rebase / orchestrator — distinct categories

---

## Value Analysis

### What you gain
1. **Conversational operator experience** — feels like CC+OMC via Discord, not a dispatch system
2. **Full OMC inside agents** — no reimplementation of planning, review, iteration
3. **Merge queue + rebase** — concurrent work merges cleanly, conflicts handled systematically
4. **Independent code review** — fresh instance, full isolation, structural guarantee
5. **Merge-test-revert** — untested code cannot reach trunk
6. **Crash recovery + context management** — orchestrator manages what agents can't
7. **Cost visibility** — per-task USD with budgets
8. **Ambient operation foundation** — event-driven tasks (Phase 4)
9. **Simpler codebase** — ~1,650 lines vs 2,400 Python (and shrinking vs v2's 1,800-2,200)

### What you lose
1. 418 battle-tested Python tests — mitigated by 17 lessons + ~130 behavioral specs
2. Visible pipeline stages — agent workflow is opaque. Mitigated by agent progress updates + audit trail.
3. Debuggability — SDK is black box. Mitigated by structured logging at gate boundaries.

### Self-iteration check
v1 was a pipeline. v2 corrected clawhip + added lessons. v3 pivots to supervised sessions after recognizing the pipeline reimplements what OMC does. Each revision was driven by a concrete finding, not scope expansion. v3 is actually smaller than v2 (~1,650 vs ~1,800-2,200 lines).

---

## Required Reading Before Implementation

1. `.omc/wiki/v5-harness-lessons-learned.md` — 17 surviving lessons
2. `.omc/wiki/v5-harness-supervised-session-architecture.md` — Architecture rationale
3. `.omc/wiki/v5-harness-efficiency-proposal.md` — Core thesis
4. `harness/tests/` — Extract behavioral intent before writing TS equivalents

---

## Related

- [v2 plan](ralplan-harness-ts-rewrite-v2.md) — Pipeline-based plan (superseded)
- [v1 plan](ralplan-harness-ts-rewrite.md) — Original plan (superseded)
- [Supervised session architecture](../wiki/v5-harness-supervised-session-architecture.md) — Design discussion wiki
- [Lessons learned](../wiki/v5-harness-lessons-learned.md) — 17 surviving lessons
- [Efficiency proposal](../wiki/v5-harness-efficiency-proposal.md) — Core thesis
