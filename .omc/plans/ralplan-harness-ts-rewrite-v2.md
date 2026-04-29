# Harness TypeScript Rewrite — Consensus Plan v2

**Status:** CONSENSUS APPROVED — Architect APPROVE + Critic APPROVE (iteration 2, 2026-04-11)
**Created:** 2026-04-11
**Supersedes:** `ralplan-harness-ts-rewrite.md` (v1, CONSENSUS APPROVED 2026-04-11)
**Source:** v1 plan + lessons learned + clawhip retention + merge conflict handling

---

## What Changed From v1

1. **Clawhip role clarified** — v1 said "Discord.js replaces clawhip's Discord relay." Architect review found clawhip never handled inbound Discord — the Python harness uses `discord.py` directly for inbound, clawhip for monitoring/outbound/tmux. Corrected: Discord.js handles inbound gateway (replacing `discord.py`), clawhip retained for file/git watching and outbound notifications. Matches the proven Python architecture pattern in a new language.
2. **Merge conflict handling added** — v1 had no merge conflict resolution mechanics. Added shelve-and-notify for V1 (from lessons learned analysis). Agent-driven resolution deferred.
3. **17 business logic lessons integrated** — v1 said "extract ~130 behavioral specs." Now explicitly calls out which lessons survive the architecture change and which are eliminated by the SDK. Required pre-implementation reading, not optional.
4. **Line estimates unchanged from v1** — ~1,800-2,200 lines. Discord.js inbound code replaces `discord.py`, clawhip bridge for monitoring/events is additional.

---

## RALPLAN-DR Summary

### Principles

1. **Prototype before committing** — SDK+OMC hook loading is the blocking unknown. No implementation work begins until the prototype validates or invalidates the approach.
2. **Scope is the enemy** — V1 delivers: Discord message → agent works → merge-tested result reported. No event-driven tasks, no multi-instance, no earned autonomy.
3. **Rewrite the code, port the knowledge** — Design around the SDK. Don't carry transport-layer complexity. But the 17 surviving business logic lessons are mandatory pre-reading — they encode edge cases the rewrite will rediscover the hard way.
4. **Protocol boundaries are load-bearing** — Harness and trading bot communicate via filesystem/git/Discord, never via shared code. Clawhip sits at this boundary and stays.
5. **The harness earns its existence by providing structural guarantees** — Independent review, merge-test-revert, crash recovery, audit trail, escalation circuit breakers, merge conflict handling. If the harness can't enforce these, it's overhead.

### Decision Drivers

1. **SDK capability verification** — Does `settingSources: ["project"]` load OMC hooks? This determines whether agents get full OMC or bare CC with tools. The entire value proposition pivots on this.
2. **Time to first working task** — How quickly can the new harness process a Discord message through to a completed, merge-tested agent task? Weeks of infrastructure before a single task works = scope creep.
3. **Reversibility** — The Python harness works today. If the TypeScript rewrite stalls at 60%, can we fall back? Migration plan must preserve the ability to retreat.

### Viable Options

#### Option A: Full TypeScript Rewrite (Recommended)

Rewrite from scratch in TypeScript using Claude Agent SDK. **~1,800-2,200 lines estimated.** Discord.js for inbound gateway (replacing `discord.py`), clawhip retained for file/git monitoring and outbound notifications. Python harness untouched as fallback.

**Pros:**
- SDK is first-class: no bridge, no serialization, native types
- Pipeline simplifies (though merge, escalation, review remain as structural gates)
- Single language for harness + SDK + OMC ecosystem
- Clean break avoids carrying transport-layer complexity
- Discord.js gives native thread support for thread-per-task UX
- Clawhip retained for proven file/git monitoring — no reimplementation of event watching

**Cons:**
- Discards working system with 418 tests (17 business logic lessons ported as knowledge, ~130 behavioral specs extracted)
- TypeScript skill gap if operator is primarily Python-fluent
- SDK is a hard dependency on Anthropic's continued maintenance
- Merge, escalation, shelve logic must be reimplemented from behavioral specs
- Two Discord-aware components (Discord.js inbound + clawhip outbound) — but this matches the proven Python pattern (discord.py + clawhip)

**Invalidation of alternatives:**
- Python+bridge: bridge grows fat as orchestrator needs mid-session control (hooks, permissions, session forking). Permanent serialization overhead.
- Porting: 2,400 lines shaped by FIFO/tmux. Porting preserves complexity the SDK eliminates. Rewrite is smaller than faithful port.

#### Option B: Incremental Hybrid (Alternative)

Keep Python orchestrator. Add thin Node.js bridge for SDK calls. Migrate module-by-module.

**Pros:** Preserves 418 tests, lower risk per step, no big-bang cutover
**Cons:** Two languages permanently, bridge is permanent serialization boundary, "incremental migration" often means "permanent halfway state"

#### Option C: Parallel Runner (Architect's Synthesis)

TS handles simple tasks, Python stays primary for complex. Migrate per-gate.

**Pros:** Maximum reversibility, per-gate testability
**Cons:** Operational confusion (which harness?), routing logic is non-trivial, slowest path, historically stalls at "TS does easy, Python does hard" forever

**Why Option A over C:** Not production (paper trading) — operational risk tolerance is high. Option A's phase gates provide sufficient reversibility. The key question is whether parallel-runner would actually complete. History says no.

### Pre-Mortem (Deliberate Mode)

**Scenario 1: SDK doesn't load OMC hooks.**
`settingSources: ["project"]` loads CLAUDE.md but not OMC plugin system. Agents get tools but no ralph/ultrawork/skills.

*Impact:* "Intelligence lives in OMC" thesis weakens. Orchestrator must retain more pipeline stages. +400-600 lines.
*Mitigation:* Phase 0 is the prototype gate. If hooks fail, reconvene with revised architecture.
*Decision gate:* (a) rewrite anyway with thicker orchestrator, (b) investigate SDK internals, (c) fall back to Python.

**Scenario 2: Rewrite takes 3x longer than estimated.**
Real implementation hits Discord.js quirks, TOML edge cases, clawhip integration friction, SDK undocumented behaviors, merge/revert edge cases. 4,000+ lines, weeks of work.

*Impact:* Two codebases in limbo. Python rots, TypeScript isn't ready.
*Mitigation:* Per-phase time gates. Any phase >2x estimate → pause and present options. Python never deleted.
*Decision gate:* Phase 1 >1 week = stop. Total >3 weeks = consider Option C.

**Scenario 3: Anthropic changes/deprecates SDK API.**
SDK at v0.2.x with 154 versions. Breaking change to `query()` requires harness updates.

*Impact:* Maintenance burden.
*Mitigation:* Pin SDK version. Wrap `query()` in adapter (`agents/pool.ts`). Adapter is ~50-80 lines.
*Decision gate:* Migration cost >1 day → stay pinned.

**Scenario 4: Agent produces plausible but broken code that reaches trunk.**
Agent self-approves, merge succeeds, but code is subtly wrong. Tests pass because agent wrote the tests too.

*Impact:* Highest-probability real-world failure.
*Mitigation:* Three layers: (1) merge-test-revert runs FULL existing test suite, not agent-written tests, (2) independent reviewer with only diff + criteria, (3) post-merge monitoring (future). Layer 1 is minimum viable structural guarantee.

**Scenario 5: Merge conflict on concurrent worktrees (NEW).**
Two agents edit overlapping files. Git merge fails. Current Python harness drops the task entirely — no retry, no notification.

*Impact:* Agent work is silently wasted. With parallel workers (Phase 4), this becomes common.
*Mitigation:* Shelve-and-notify for V1 (see Phase 1 scope). Task shelved with conflict details, operator notified via Discord. Agent-driven resolution is a future earned-autonomy feature.
*Decision gate:* If conflict rate >20% of parallel tasks, add automatic rebase-and-retry before shelving.
*Operational note (Architect finding):* With 2 concurrent workers on a single-project repo, overlapping edits will be frequent. Shelve-and-notify will generate a steady stream of operator interrupts. Acceptable for V1, but automatic rebase-and-retry should be Phase 4 scope rather than deferred indefinitely.

---

## Lessons That Survive the Rewrite

Of the 27 lessons extracted from the Python harness (`.omc/wiki/v5-harness-lessons-learned.md`), **17 survive** the architecture change. These are business logic and operational wisdom, not transport-layer fixes. **Required pre-implementation reading for each phase.**

### Business Logic (must be reimplemented)

| ID | Lesson | Applies to Phase | Implementation Note |
|----|--------|-----------------|-------------------|
| B1 | Mutation queue — never mutate state between `await` points | 1 | Node.js has same async interleave risk. State writes must be synchronous within Promise resolution. |
| B2 | Clear stale completion markers on state transitions | 1 | SDK eliminates signal files, but any event system has TOCTOU. Clear old markers when entering new state. |
| B3 | Shelved task escalation clock reset | 1 | Time-spent-shelved must not count toward escalation timeout. Reset to `now()` on unshelve. |
| B4 | New message clears pending confirmation | 2 | Stale "yes" detection from old message must not fire on unrelated input. |
| B5 | Resume at executor, not reviewer, after auto-escalation (BUG-024) | 1 | Reviewer-resume loop: rejected → resume at reviewer → re-reject → infinite loop. Always resume at executor. |
| B6 | Auto-escalation resets retry count to 0 (BUG-023) | 1 | Escalated tasks must start at tier 1, not skip to operator. |
| B7 | Drop unknown state keys on load, don't crash | 1 | State schema evolution is inevitable. `JSON.parse` + pick known fields, ignore rest. |
| B9 | Crash during dialogue reverts to safe stage | 1 | Ephemeral conversation context doesn't survive crash. Revert to `escalation_wait`, not `escalation_dialogue`. |

### Operational (must be designed in)

| ID | Lesson | Applies to Phase | Implementation Note |
|----|--------|-----------------|-------------------|
| O1 | Every LLM judgment has a safe default | 1 | classify → "complex", confidence → "low", intent → "feedback". Err toward more review. |
| O3 | Atomic writes (write-temp-rename) for state and audit files | 1 | Still writing state to disk. `fs.writeFile` to temp → `fs.rename`. |
| O4 | Path traversal validation on task IDs from Discord | 1, 2 | Task IDs are untrusted input. Validate `[a-zA-Z0-9_-]+` before any path construction. |
| O5 | Confidence gating promotes on unknown values | 1 | Unknown confidence → promote to operator, don't silently resolve. |
| O6 | Informational escalations notify but don't block pipeline | 1 | FYI messages go to Discord, pipeline continues. |
| O7 | Auto-commit worktree changes before merge attempt | 1 | Agents forget instructions. Verify preconditions mechanically. Exclude `.omc/` from commits. |
| O8 | Test timeout with automatic revert | 1 | 180s hard timeout. Kill process, revert merge on timeout. Prevents hanging suites. |
| O9 | Event/audit log is write-only, never read by orchestrator | 1 | Audit data must never affect control flow. |
| O10 | Multi-message accumulation debounce (2s buffer) | 2 | Discord users send bursts. Buffer per-channel, flush as single string. |
| O11 | Gateway reconnect dedup (bounded seen-message set) | 2 | Clawhip or Discord.js reconnect replays messages. `Set` with max size for dedup. |

### Eliminated by SDK (do NOT reimplement)

| ID | Why Gone |
|----|----------|
| T1-T5 | FIFO/tmux lifecycle — SDK `query()` replaces all pipe management |
| B8 | Shelved reply injection timing — SDK `resume: sessionId` handles conversation continuity |
| B10 | Late-binding closure bug — Python-specific, TypeScript `let` doesn't have this |
| O2 | Zombie reaping — SDK manages subprocess lifecycle, `AbortController` handles cleanup |
| O12 | Wiki skill tool-blocking — SDK `allowedTools` is explicit, no accidental collision |

---

## Implementation Plan

### Phase 0: SDK Prototype (1 day) — BLOCKING GATE

**Goal:** Verify SDK capabilities. Everything else waits.

**Tests (all must pass):**
1. **Hook loading:** `query()` with `settingSources: ["project"]`, check for OMC session-start hook, CLAUDE.md loading, skill availability.
2. **AbortController:** `query()` with long task, abort after 5s. Verify clean termination, no orphans.
3. **Session resume:** Capture `session_id`, kill process, `resume: sessionId`. Verify context survives.
4. **Cost tracking:** Verify result message contains `usage` (tokens) and cost data.

**Acceptance criteria:**
- [ ] `query()` completes without error
- [ ] Message stream contains typed objects (system, assistant, result)
- [ ] AbortController cleanly terminates
- [ ] Session resume restores context
- [ ] Result has `usage` and cost data
- [ ] OMC hooks fire — OR document failure and revise per Scenario 1

### Phase 1: Agent Pool + State Machine + Merge Gate (4-5 days)

**Goal:** TypeScript process that spawns SDK agent, collects result, runs tests, merges — with full state persistence, crash recovery, and merge conflict handling.

**Pre-implementation requirement:** Read lessons B1-B9, O1, O3-O9 before writing any code. These are the edge cases this phase will rediscover if ignored.

**Locked scope:**

*Agent pool:*
- `src/agents/pool.ts` — `query()` wrapper with AbortController, maxBudgetUsd, tool-aware liveness (active/tool-running/stuck)
- `src/agents/definitions.ts` — Agent configs: executor (sonnet, full tools), reviewer (sonnet, read-only tools)

*State machine (lessons B1-B7, B9 are design inputs):*
- `src/orchestrator/state.ts` — TaskState: `pending | active | review | merge | done | failed | shelved | escalation_wait | paused`
- Mutation safety: state writes are synchronous within Promise resolution callbacks (B1)
- Stale marker clearing on every state transition (B2)
- Shelve/unshelve: preserve full context, reset escalation clock on unshelve (B3)
- Escalation circuit breaker: tier routing, retry count reset on auto-escalation (B5, B6)
- Crash recovery: load last valid state, drop unknown keys (B7), revert ephemeral stages (B9)
- Defensive deserialization: pick known fields, ignore unknown (B7)
- Persist via atomic writes after every transition (O3)

*Merge gate (structural guarantee — principle #5):*
- `src/orchestrator/merge.ts`:
  1. Auto-commit worktree changes, excluding `.omc/` (O7)
  2. `git merge --no-ff` to task branch
  3. **On merge conflict: shelve task with conflict details, log to stdout (NEW).** In Phase 1 (pre-Discord), notification is console log only. Phase 2 adds Discord notification via clawhip outbound. **Unshelve trigger:** conflict-shelved tasks auto-retry on next orchestrator poll cycle after a configurable cooldown (default 5min). The retry gets a fresh worktree with updated trunk, so textual conflicts from stale base are resolved by the fresh merge. Semantic conflicts will re-shelve — operator intervention via `!resume` (Phase 2) or manual task-file edit (Phase 1).
  4. On clean merge: run full test suite with 180s timeout (O8)
  5. Test timeout → kill process, revert merge
  6. Test failure → revert merge, state = "failed" with test output
  7. Test pass → advance to done
- Worktree creation/cleanup lifecycle

*Orchestrator:*
- `src/orchestrator/index.ts` — Event loop: poll task file → create worktree → spawn agent → collect result → merge gate → update state
- `src/lib/config.ts` — TOML loader
- Max-stage timeout: configurable per-stage, abort agent on timeout
- Safe defaults on all LLM judgment calls (O1)
- Path traversal validation on task IDs (O4)
- Confidence gating promotes on unknown values (O5)
- Informational escalations don't block pipeline (O6)
- Event log is write-only (O9)

**Explicitly deferred:**
- Discord companion (Phase 2)
- Independent review gate (Phase 3)
- Event-driven task creation (Phase 4)
- Thread-per-task UX (Phase 4)
- Multi-instance concurrency (Phase 4)
- Wiki/documentation stage (Phase 4)
- Agent-driven merge conflict resolution (Future)
- Earned autonomy (Future)

**Acceptance criteria:**
- [ ] Drop task JSON → agent works → tests run → merge succeeds → result written
- [ ] Merge conflict → task shelved with conflict details, notification sent
- [ ] Test failure → automatic revert, state = "failed" with test output
- [ ] Test timeout (180s) → kill, revert, state = "failed"
- [ ] Agent timeout (AbortController) → state "failed", worktree cleaned
- [ ] State survives process restart (crash recovery from last valid state)
- [ ] Unknown state keys dropped on load (forward compatibility)
- [ ] Crash during ephemeral stage → reverts to safe stage
- [ ] Shelve/unshelve preserves full context, resets escalation clock
- [ ] Escalation circuit breaker fires after N retries, resets at tier 1
- [ ] Auto-commit catches uncommitted changes before merge attempt
- [ ] State writes are synchronous (no async interleave corruption)
- [ ] Tests pass with mocked SDK (no real API calls in unit tests)

### Phase 2: Discord Companion (2-3 days)

**Goal:** Operator interacts with harness via Discord.

**Pre-implementation requirement:** Read lessons B4, O10, O11 before writing Discord handling code.

**Discord architecture (CORRECTED after Architect review):** The Python harness uses `discord.py` for inbound Discord and clawhip for monitoring/outbound/tmux. The v2 plan preserves this proven split:
- **Discord.js** — Inbound gateway connection, message handling, thread creation (replaces `discord.py`)
- **Clawhip** — File/git watching (Phase 4 events), outbound notifications, tmux management

This matches the current architecture pattern in a new language. Discord.js gives native thread support for thread-per-task UX (Phase 4) without extending clawhip's Rust code.

**Locked scope:**
- `src/discord/bot.ts` — Discord.js client, gateway connection, message handling
- `src/discord/commands.ts` — `!task`, `!status`, `!tell`, `!pause`, `!resume`, `!caveman`
- `src/discord/routing.ts` — Single active task → messages go to it. Multiple → ask which.
- `src/discord/accumulator.ts` — 2s debounce buffer for multi-message input (O10)
- `src/events/clawhip.ts` — Clawhip event bridge for monitoring/outbound notifications
- Message dedup via bounded Set for reconnect replays (O11)
- New message clears pending confirmation state (B4)
- Tests: port command parsing intent from Python `test_discord_companion.py` (~40 behavioral specs)

**Acceptance criteria:**
- [ ] `!task fix the auth bug` → agent spawns, works, merge-tests, result reported to Discord
- [ ] `!status` returns active task, stage, elapsed time, cost
- [ ] `!pause` / `!resume` freezes/unfreezes task processing
- [ ] `!tell` routes messages to active agent
- [ ] Multi-message burst → debounced into single input (2s window)
- [ ] Gateway reconnect → duplicate messages filtered
- [ ] New dialogue message clears stale confirmation state
- [ ] Command parsing tests pass (ported from Python)

### Phase 3: Independent Review Gate (1-2 days)

**Goal:** Complex tasks get independent review before merge.

**Locked scope:**
- `src/orchestrator/gates.ts` — After agent completes, spawn second `query()` with:
  - Reviewer role (from `config/agents/reviewer.md`)
  - Read-only tools (`allowedTools: ["Read", "Glob", "Grep"]`)
  - `cwd` pointing to **committed branch** (not working directory)
  - Prompt: diff, acceptance criteria, codebase access. NOT executor's conversation.
- Heuristic: configurable file-count / diff-size threshold, path sensitivity tags (security/, auth/ → always review)
- State machine: "review" stage between "active" and "merge"

**Acceptance criteria:**
- [ ] Task touching >3 files → independent reviewer spawns
- [ ] Reviewer cwd = committed branch, not executor working directory
- [ ] APPROVE → merge gate. REJECT → back to "active" with feedback.
- [ ] Simple tasks (1 file, <50 lines) skip review, go to merge.

### Phase 4: Event Sources + Concurrency (3-5 days)

**Goal:** Harness reacts to events without human prompting. Multiple tasks in flight.

**Locked scope:**
- `src/events/watcher.ts` — Clawhip event bridge for git push, file changes (clawhip already monitors these)
- `src/events/cron.ts` — Scheduled tasks (dependency audit, test suite)
- `src/orchestrator/index.ts` — Task queue with concurrency cap (default 2). Shelve/unshelve for blocked tasks.
- `src/orchestrator/wiki.ts` — Post-task documentation
- `src/discord/threads.ts` — Thread-per-task UX via Discord.js thread API
- Concurrent worktrees: each task in its own worktree
- Merge conflict handling for concurrent tasks: shelve conflicting task, notify operator (B1 mutation safety critical here — state writes from concurrent task completions must not interleave)

**Acceptance criteria:**
- [ ] Git push → harness auto-creates "run tests" task
- [ ] Two independent tasks run concurrently in separate worktrees
- [ ] Merge conflict between concurrent tasks → later task shelved with details
- [ ] Operator messages in task thread → reach that task's agent
- [ ] `!status` shows all active tasks
- [ ] Completed tasks produce wiki entries

---

## Cutover Plan

**Python harness is never deleted.** Migration checkpoints:

1. **Phase 0 complete:** SDK verified. Python still primary.
2. **Phase 1 complete:** TS processes file-dropped tasks with merge-test-revert + conflict handling. Python handles Discord.
3. **Phase 2 complete:** TS handles Discord via Discord.js + clawhip outbound. **Parallel-run begins.** TS primary, Python monitors.
4. **Phase 3 complete:** Independent review. Python enters maintenance-only.
5. **Phase 4 complete + 10 successful e2e tasks:** Python retired. `harness/` preserved in git history.

**Rollback:** Stop TS process, restart Python harness. State schemas differ — active TS tasks are lost, but Python picks up new tasks cleanly.

**Phase time gates:** Any phase >2x estimated → pause, present options: (a) continue, (b) Option C parallel runner, (c) abandon rewrite. **Sunk-cost guard:** The decision to continue must compare remaining work to Option C's cost from scratch — not factor in work already done. If Phase 2 is 2x over and the honest answer is "Option C would be cheaper from here," take Option C regardless of Phase 0-1 investment.

---

## Test Plan

### Unit Tests
- **State machine:** All 9 TaskState transitions, invalid transitions rejected, persistence, crash recovery (partial write → last valid), shelve/unshelve with escalation clock reset, escalation counter increment/reset, mutation safety (no async interleave), defensive deserialization (unknown keys dropped)
- **Agent pool:** Mock SDK `query()`, message streaming, AbortController timeout, maxBudgetUsd, liveness classification
- **Merge gate:** Mock git: worktree creation, auto-commit (O7), merge --no-ff, **merge conflict → shelve (NEW)**, test execution with timeout (O8), revert on failure, worktree cleanup
- **Escalation:** Circuit breaker fires after N retries, retry reset on auto-escalation (B6), resume at executor not reviewer (B5), confidence gating (O5), informational escalations don't block (O6)
- **Config:** TOML parsing, agent definitions, defaults, timeouts
- **Discord commands:** Parse all commands. Edge cases: empty args, unknown commands, path traversal (O4)
- **Accumulator:** Multi-message debounce (O10), reconnect dedup (O11), confirmation clearing (B4)

### Integration Tests
- **Full lifecycle:** Mock SDK → spawn → stream → result → merge gate (mock git) → state transitions → completion
- **Merge conflict flow (NEW):** Agent completes → merge → conflict → shelve → notification → operator reply → unshelve → retry
- **Merge failure recovery:** Agent completes → merge → test failure → revert → state "failed" → Discord report
- **Escalation flow:** Fail → retry → fail → circuit breaker → escalation_wait → reply → resume at executor
- **Crash recovery:** Write state mid-task → kill → restart → state restored, task resumable, ephemeral stages reverted
- **Discord flow:** Discord.js message → parse → create task → agent → merge → Discord.js reply + clawhip outbound notification

### E2E Tests (gated behind `--e2e`, real API calls)
- **Haiku smoke:** Real `query()`, trivial task, verify result structure
- **Full flow:** Discord → task → agent → merge-test → result → Discord report
- **Session resume:** Start → kill → restart → resume → complete
- **Merge conflict (real git):** Two worktrees, overlapping edits, verify shelve behavior

### Observability
- Structured logging: every state transition, spawn, merge, test result, escalation, **merge conflict** — with task ID, timestamp, duration
- Cost tracking: per-task USD from SDK results
- Error classification: SDK / agent / merge / conflict / orchestrator — distinct categories

---

## Value Analysis

### What you gain
1. Structured agent control — typed messages, not FIFO scraping
2. Concurrent agents without tmux — `Promise.all()` over `query()` calls
3. Independent review as structural guarantee — fresh context reviewer
4. Merge-test-revert as structural guarantee — untested code cannot reach trunk
5. **Merge conflict handling (NEW)** — conflicts shelved and reported, not silently dropped
6. Crash recovery — SDK sessions persist to JSONL, orchestrator state to JSON
7. Cost visibility — per-task USD with token budgets
8. Foundation for ambient operation — event-driven tasks
9. Proven architecture pattern — Discord.js for inbound (like discord.py), clawhip for monitoring/events (unchanged)

### What you lose
1. 418 battle-tested Python tests — mitigated by 17 lessons + ~130 behavioral specs extracted before rewrite
2. Operational stability — mitigated by parallel-run period and rollback
3. Simplicity — SDK is black box vs. debuggable FIFO+tmux. Mitigated by Anthropic maintenance.

### The self-iteration question
Expanded from "action dicts" → "efficiency analysis" → "SDK discovery" → "TypeScript rewrite." Each step justified by new information. **Is it justified?** Yes, IF prototype succeeds. Phase 0 is the honest gate. If it fails, total investment is 1 day + design docs.

**The discipline:** 1-day prototype. Per-phase time gates. Python never deleted until 10 successful e2e tasks. Lessons document prevents rediscovering known edge cases.

---

## Required Reading Before Implementation

Before writing any TypeScript code, implementer MUST read:
1. `.omc/wiki/v5-harness-lessons-learned.md` — Full lessons document (focus on the 17 surviving lessons)
2. `.omc/wiki/v5-harness-efficiency-proposal.md` — Architecture thesis and open concerns
3. `harness/tests/` — Extract behavioral intent from Python tests before writing TS equivalents

---

## Related

- [v1 plan](ralplan-harness-ts-rewrite.md) — Original consensus-approved plan (superseded)
- [Lessons learned](../wiki/v5-harness-lessons-learned.md) — 27 lessons, 17 survive rewrite
- [Efficiency proposal](../wiki/v5-harness-efficiency-proposal.md) — Architecture thesis
- [Known bugs](../wiki/v5-harness-known-bugs.md) — BUG-002, BUG-023, BUG-024 referenced in lessons
