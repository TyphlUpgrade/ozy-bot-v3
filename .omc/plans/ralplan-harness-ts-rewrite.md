# Harness TypeScript Rewrite — Consensus Plan

**Status:** CONSENSUS APPROVED — Architect APPROVE + Critic APPROVE (iteration 2)
**Created:** 2026-04-11
**Source:** `.omc/wiki/v5-harness-efficiency-proposal.md`

---

## RALPLAN-DR Summary

### Principles

1. **Prototype before committing** — The SDK+OMC hook loading is the blocking unknown. No implementation work begins until the prototype validates or invalidates the approach.
2. **Scope is the enemy** — V1 delivers one thing: Discord message → agent works → merge-tested result reported. No event-driven tasks, no multi-instance, no earned autonomy. Those are Phase 2+.
3. **Rewrite the code, port the knowledge** — Design around what the SDK provides. Don't carry transport-layer complexity. But extract business logic edge cases from Python tests as behavioral specs before rewriting.
4. **Protocol boundaries are load-bearing** — The harness and trading bot communicate via filesystem/git/Discord, never via shared code. This boundary must remain crisp.
5. **The harness earns its existence by providing structural guarantees** — Independent review, merge-test-revert, crash recovery, audit trail, escalation circuit breakers. If the harness can't enforce these, it's overhead.

### Decision Drivers

1. **SDK capability verification** — Does `settingSources: ["project"]` load OMC hooks? This determines whether agents get full OMC (ralph, ultrawork, skills) or bare CC with tools. The entire value proposition pivots on this.
2. **Time to first working task** — How quickly can the new harness process a Discord message through to a completed, merge-tested agent task? Weeks of infrastructure before a single task works = scope creep.
3. **Reversibility** — The Python harness works today. If the TypeScript rewrite stalls at 60%, can we fall back without lost work? The migration plan must preserve the ability to retreat.

### Viable Options

#### Option A: Full TypeScript Rewrite (Recommended)

Rewrite the harness from scratch in TypeScript using the Claude Agent SDK. **~1,800-2,200 lines estimated** (revised upward from 1,100 after Architect review identified missing merge stage, escalation logic, and shelve/unshelve). Python harness remains untouched as fallback.

**Pros:**
- SDK is first-class: no bridge, no serialization, native types
- Pipeline simplifies significantly (though not to 3 stages — merge and escalation remain)
- Discord.js has native thread support for thread-per-task UX
- Single language for harness + SDK + OMC ecosystem
- Clean break avoids carrying transport-layer complexity

**Cons:**
- Discards working system with 418 tests (though ~130 behavioral specs are ported as knowledge)
- Edge cases encoded in Python tests may be rediscovered the hard way
- TypeScript skill gap if operator is primarily Python-fluent
- clawhip's role becomes unclear (may be over-engineered for what remains)
- SDK is a hard dependency on Anthropic's continued maintenance
- Larger scope than initially estimated — merge, escalation, shelve logic must be reimplemented

**Invalidation of alternatives:**
- Python+bridge was evaluated and rejected: the bridge grows fat as soon as the orchestrator needs mid-session control (hooks, permission changes, session forking). Permanent serialization overhead for every SDK interaction.
- Porting was evaluated and rejected: 2,400 lines of Python shaped by FIFO/tmux constraints. Porting preserves complexity that the SDK eliminates. Even with the revised estimate, rewrite (~2,000 lines) is smaller than a faithful port (~2,400) because transport code is eliminated.

#### Option B: Incremental Hybrid (Alternative)

Keep Python orchestrator. Add a thin Node.js bridge process for SDK `query()` calls. Migrate Discord companion to Discord.js separately. Convert module-by-module over time.

**Pros:**
- Preserves 418 tests during transition
- Lower risk — each step is small and reversible
- Python orchestrator continues working throughout
- No big-bang cutover

**Cons:**
- Two languages in the harness permanently (or for a long time)
- Bridge is a permanent serialization boundary
- Mid-session SDK features (hooks, permissions, session fork) are awkward through IPC
- Discord ends up in JS while orchestrator stays in Python — split brain
- "Incremental migration" often means "permanent halfway state"

#### Option C: Parallel Runner (Architect's Synthesis)

Build TS harness as a parallel runner alongside the Python harness. TS handles only simple tasks (no escalation, no retry, no review gate). Python stays primary for complex tasks. Migrate policy logic one gate at a time, porting corresponding Python tests as behavioral specs before writing TS code.

**Pros:**
- Maximizes reversibility — Python stays authoritative until each gate is proven in TS
- No big-bang cutover — migration is per-gate, not per-codebase
- Each gate migration is independently testable and reversible
- Operational risk is lowest of all options

**Cons:**
- Two running harnesses creates operational confusion (which one handled this task?)
- Routing logic needed to decide "simple" vs "complex" — itself a non-trivial judgment
- Slowest path to full migration — could take months of part-time work
- The "one gate at a time" discipline often collapses into "implement the easy gates, defer the hard ones indefinitely"

**Why Option A over C:** Option C is safer but slower. The Python harness is not production (paper trading), so operational risk tolerance is high. Option A's phase gates provide sufficient reversibility — each phase is independently validated, and the Python harness stays as fallback until 10 successful end-to-end tasks. The key question is: would the parallel-runner approach actually complete, or would it stall at "TS does simple tasks, Python does everything else" forever? History suggests the latter. Option A forces commitment after prototype validation.

### Pre-Mortem (Deliberate Mode)

**Scenario 1: SDK doesn't load OMC hooks.**
`settingSources: ["project"]` loads CLAUDE.md and `.claude/settings.json` hooks, but OMC's plugin system requires the full CC interactive runtime — which the SDK doesn't provide. Agents get Read/Edit/Bash/Grep but no ralph, no ultrawork, no OMC skills.

*Impact:* The "intelligence lives in OMC" thesis collapses. Agents become bare CC instances. The orchestrator must retain pipeline stages (architect planning, reviewer verification) rather than delegating everything to the agent. Estimated scope increase: +400-600 lines for orchestrator-managed planning and review stages.

*Mitigation:* This is why the prototype is Phase 0, not Phase 1. If hooks don't load, reconvene with revised architecture.

*Decision gate:* If prototype fails: (a) rewrite anyway without OMC, accepting a thicker orchestrator, (b) investigate SDK internals to enable hook loading, (c) fall back to Python harness. Operator chooses.

**Scenario 2: Rewrite takes 3x longer than estimated.**
The 1,800-2,200 line estimate covers the happy path. Real implementation hits: Discord.js API quirks, TOML edge cases, clawhip integration friction, SDK undocumented behaviors, test infrastructure, merge/revert edge cases. Actual effort is 4,000+ lines and weeks of work.

*Impact:* Two codebases in limbo. Python harness rots. TypeScript harness isn't ready.

*Mitigation:* Each phase is independently gated. If any phase takes >2x estimated time, pause and present options to operator. Python harness is never deleted — stays as fallback until 10 successful end-to-end tasks.

*Decision gate:* Phase 1 taking >1 week = stop and evaluate. Total effort exceeding 3 weeks = consider Option C instead.

**Scenario 3: Anthropic changes/deprecates the SDK API.**
The SDK is at v0.2.x with 154 versions. A breaking change to `query()` or message types would require harness updates.

*Impact:* Maintenance burden. Harness breaks on SDK update.

*Mitigation:* Pin SDK version. Wrap `query()` in adapter (`agents/pool.ts`) — orchestrator never calls SDK directly. Adapter is ~50-80 lines; updating for new SDK version is a small task.

*Decision gate:* If SDK migration cost ever exceeds 1 day, evaluate staying on pinned version.

**Scenario 4: Agent produces plausible but broken code that reaches trunk.**
The agent completes work, self-approves (or review gate doesn't catch the issue), and the merge succeeds — but the code is subtly wrong. Tests pass because the agent also wrote the tests (or existing tests don't cover the regression).

*Impact:* This is the highest-probability real-world failure. The harness's structural guarantees are supposed to prevent exactly this.

*Mitigation:* Three layers of defense:
1. **Merge-test-revert gate** (Phase 1): After agent work merges to worktree branch, run the FULL existing test suite (not just agent-written tests) with timeout. Revert on failure. This catches regressions against existing code.
2. **Independent review** (Phase 3): Fresh reviewer with only diff + acceptance criteria catches logical errors the executor was blind to.
3. **Post-merge monitoring** (Future): Watch for failures after trunk merge. Auto-create fix tasks.

Layer 1 is the minimum viable structural guarantee. Without it, the harness provides less safety than manual code review.

---

## Implementation Plan

### Phase 0: SDK Prototype (1 day) — BLOCKING GATE

**Goal:** Verify SDK capabilities. Everything else waits on this.

**Tests (all must pass):**

1. **Hook loading:** Spawn `query()` with `settingSources: ["project"]`, check for OMC session-start hook output, CLAUDE.md loading, skill availability.
2. **AbortController:** Spawn `query()` with a long task, abort after 5 seconds via `AbortController.abort()`. Verify clean termination and no orphaned processes.
3. **Session resume:** Spawn `query()`, capture `session_id` from system message. Kill process. Spawn new `query()` with `resume: sessionId`. Verify conversation context survives.
4. **Cost tracking:** Verify result message contains `usage` (input/output tokens) and cost data.

**Acceptance criteria:**
- [ ] SDK `query()` completes without error
- [ ] Message stream contains typed objects (system, assistant, result)
- [ ] AbortController cleanly terminates a running query
- [ ] Session resume restores conversation context after process restart
- [ ] Result message contains `usage` and cost data
- [ ] OMC hooks fire — OR we document failure and revise plan per Scenario 1

**Decision gate:** If OMC hooks DON'T load, reconvene. If AbortController or session resume fails, evaluate SDK maturity before proceeding.

### Phase 1: Agent Pool + State Machine + Merge Gate (4-5 days)

**Goal:** TypeScript process that spawns an SDK agent, collects the result, runs tests, and merges — with full state persistence and crash recovery.

**Locked scope:**

*Agent pool:*
- `src/agents/pool.ts` — `query()` wrapper with AbortController, maxBudgetUsd, tool-aware liveness (active/tool-running/stuck)
- `src/agents/definitions.ts` — Agent configs: executor (sonnet, full tools), reviewer (sonnet, read-only tools)

*State machine (ported from Python behavioral specs):*
- `src/orchestrator/state.ts` — TaskState: `pending | active | review | merge | done | failed | shelved | escalation_wait | paused`
- Shelve/unshelve: preserve full task context (description, stage, escalation count, operator replies)
- Escalation circuit breaker: `tier1_escalation_count`, max retries before auto-escalation to operator
- Persist to JSON after every transition. Crash recovery: load last valid state on restart.

*Merge gate (structural guarantee — principle #5):*
- `src/orchestrator/merge.ts` — After agent completes: auto-commit worktree changes → `git merge --no-ff` to task branch → run full test suite with timeout → revert on failure → report result
- Worktree creation/cleanup lifecycle: `git worktree add -b task/{id}`, cleanup on completion/failure

*Orchestrator:*
- `src/orchestrator/index.ts` — Event loop: poll for task file → create worktree → spawn agent → collect result → merge gate → update state
- `src/lib/config.ts` — TOML loader for `config/harness/project.toml`
- Max-stage timeout logic: configurable per-stage time limits, abort agent on timeout

**Explicitly deferred:**
- Discord (Phase 2)
- Independent review gate (Phase 3)
- Event-driven task creation (Phase 4)
- Thread-per-task UX (Phase 4)
- Multi-instance concurrency (Phase 4)
- Wiki/documentation stage (Phase 4)
- Earned autonomy (not V1)

**Acceptance criteria:**
- [ ] Drop task JSON in `harness-ts/tasks/` → agent works → tests run → merge succeeds → result in `harness-ts/results/`
- [ ] Test failure after merge → automatic revert, task state = "failed" with test output
- [ ] Agent timeout (AbortController) → state persists as "failed", worktree cleaned up
- [ ] State survives process restart (read from disk, resume at last stage)
- [ ] Shelve/unshelve preserves full task context across cycles
- [ ] Escalation circuit breaker fires after N failed retries
- [ ] Tests pass with mocked SDK (no real API calls in unit tests)

### Phase 2: Discord Companion (2-3 days)

**Goal:** Operator interacts with the harness via Discord.

**Locked scope:**
- `src/discord/bot.ts` — Discord.js client, gateway connection, message handling
- `src/discord/commands.ts` — `!task`, `!status`, `!tell`, `!pause`, `!resume`, `!caveman`
- `src/discord/routing.ts` — Single active task → messages go to it. Multiple → ask which.
- `src/events/clawhip.ts` — clawhip event bridge for file/git watching (Discord relay replaced by Discord.js gateway)
- Tests: port command parsing intent from Python `test_discord_companion.py` (~40 behavioral specs)

**clawhip decision:** Discord.js owns the gateway connection (replaces clawhip's Discord relay). clawhip retained for file watching and git event detection only. If its remaining role proves too narrow, evaluate replacing with `chokidar` in Phase 4.

**Acceptance criteria:**
- [ ] `!task fix the auth bug` → agent spawns, works, merge-tests, reports result to Discord
- [ ] `!status` returns active task, stage, elapsed time, cost so far
- [ ] `!pause` / `!resume` freezes/unfreezes task processing
- [ ] `!tell` routes messages to active agent (via new `query()` turn or `resume`)
- [ ] Command parsing tests pass (ported intent from Python tests)

### Phase 3: Independent Review Gate (1-2 days)

**Goal:** Complex tasks get reviewed by an independent agent before merge.

**Locked scope:**
- `src/orchestrator/gates.ts` — After agent completes work, spawn a second `query()` with:
  - Reviewer role (system prompt from `config/agents/reviewer.md`)
  - Read-only tools (`allowedTools: ["Read", "Glob", "Grep"]`)
  - `cwd` pointing to the **committed branch** (not the working directory — prevents reviewer seeing uncommitted executor artifacts)
  - Prompt containing: diff, acceptance criteria, codebase access. NOT the executor's conversation.
- Heuristic: configurable file-count / diff-size threshold, path sensitivity tags (security/, auth/ → always review)
- State machine: "review" stage between "active" and "merge"

**Acceptance criteria:**
- [ ] Task touching >3 files automatically spawns independent reviewer
- [ ] Reviewer's `cwd` is the committed branch, not executor's working directory
- [ ] Reviewer APPROVE → proceeds to merge gate. REJECT → returns to "active" with feedback.
- [ ] Simple tasks (1 file, <50 lines) skip independent review, go straight to merge.

### Phase 4: Event Sources + Concurrency (3-5 days)

**Goal:** Harness reacts to events without human prompting. Multiple tasks in flight.

**Locked scope:**
- `src/events/watcher.ts` — Git push events, file change events (via clawhip or chokidar)
- `src/events/cron.ts` — Scheduled tasks (dependency audit, test suite run)
- `src/orchestrator/index.ts` — Task queue with concurrency cap (default 2). Shelve/unshelve for blocked tasks.
- `src/orchestrator/wiki.ts` — Post-task documentation: extract task summary to wiki
- `src/discord/threads.ts` — Thread-per-task UX. Each task gets a Discord thread.
- Git worktree management — each concurrent task in its own worktree. State writes are synchronous within Promise resolution callbacks (Node.js single-threaded — safe if no async state writes interleave).

**Acceptance criteria:**
- [ ] Git push to branch → harness auto-creates "run tests" task
- [ ] Two independent tasks run concurrently in separate worktrees
- [ ] Operator messages in a task's thread reach that task's agent
- [ ] `!status` shows all active tasks with their threads
- [ ] Completed tasks produce wiki summary entries

### Cutover Plan

**The Python harness is never deleted.** Migration checkpoints:

1. **Phase 0 complete:** SDK verified. Python harness still primary.
2. **Phase 1 complete:** TS harness can process file-dropped tasks with merge-test-revert. Python handles Discord.
3. **Phase 2 complete:** TS harness handles Discord. **Parallel-run period begins.** TS is primary, Python monitors.
4. **Phase 3 complete:** TS harness has independent review. Python enters maintenance-only.
5. **Phase 4 complete + 10 successful end-to-end tasks:** Python harness retired. `harness/` preserved in git history.

**Rollback at any point:** Stop TS process, restart Python harness. Note: state schemas differ (TS has different field set than Python's `PipelineState`). Rollback means active TS tasks are lost — but the Python harness can pick up new tasks cleanly.

**Phase time gates:** If any phase exceeds 2x estimated duration, pause and present operator with options: (a) continue, (b) switch to Option C (parallel runner), (c) abandon rewrite, improve Python harness incrementally.

---

## Expanded Test Plan (Deliberate Mode)

### Unit Tests
- **State machine:** All 9 TaskState transitions, invalid transitions rejected, persistence to/from JSON, crash recovery (partial write → load last valid), shelve/unshelve preserves full context, escalation counter increment/reset, paused flag
- **Agent pool:** Mock SDK `query()`, verify message streaming, AbortController timeout, maxBudgetUsd enforcement, tool-aware liveness (active/tool-running/stuck classification)
- **Merge gate:** Mock git commands, verify: worktree creation, auto-commit, merge --no-ff, test execution with timeout, revert on test failure, worktree cleanup
- **Escalation:** Circuit breaker fires after N retries, escalation state transitions, operator reply routing
- **Config:** TOML parsing, agent definition loading, default values, timeout config
- **Discord commands:** Parse `!task`, `!status`, `!tell`, `!pause`, `!resume`, `!caveman`. Edge cases: empty args, unknown commands, path traversal

### Integration Tests
- **Full task lifecycle:** Mock SDK → spawn agent → stream messages → collect result → merge gate (mock git) → state transitions → completion. Verify entire orchestrator loop.
- **Merge failure recovery:** Agent completes → merge → test failure → revert → state = "failed" → reported to Discord
- **Escalation flow:** Agent fails → retry → fail again → circuit breaker → escalation_wait → operator reply → resume
- **Crash recovery:** Write state mid-task → kill process → restart → verify state restored and task resumable
- **Discord flow:** Mock Discord.js → receive message → create task → agent completes → merge → Discord reply

### E2E Tests (gated behind `--e2e` flag, real API calls)
- **Haiku smoke test:** Real `query()` with haiku, trivial task, verify result structure
- **Full flow:** Discord message → task → agent execution → merge-test → result → Discord report
- **Session resume:** Start task → kill process → restart → resume session → complete task

### Observability
- **Structured logging:** Every state transition, agent spawn, merge attempt, test result, escalation event — logged with task ID, timestamp, duration
- **Cost tracking:** Per-task API cost from SDK result messages, aggregated in audit log
- **Error classification:** SDK errors vs. agent errors vs. merge errors vs. orchestrator errors — distinct log categories

---

## Value Analysis: Is This Worth It?

### What you gain
1. **Structured agent control** — typed messages, not FIFO scraping
2. **Concurrent agents without tmux** — `Promise.all()` over `query()` calls
3. **Independent review as structural guarantee** — fresh context reviewer
4. **Merge-test-revert as structural guarantee** — untested code cannot reach trunk
5. **Crash recovery for free** — SDK sessions persist to JSONL, orchestrator state to JSON
6. **Cost visibility** — every agent task has a USD cost with token budgets
7. **Foundation for ambient operation** — event-driven tasks make the harness proactive

### What you lose
1. **418 battle-tested Python tests** — mitigated by extracting ~130 behavioral specs before rewriting
2. **Operational stability** — mitigated by parallel-run period and rollback plan
3. **Simplicity** — SDK is a black box vs. debuggable FIFO+tmux. Mitigated by SDK being Anthropic-maintained.
4. **Escalation circuit breaker edge cases** — Python's `_route_with_circuit_breaker` and `_escalation_cache` encode real operational lessons. Must be studied, not just reimplemented.

### The self-iteration question
This proposal expanded from "action dicts + haiku responses" → "efficiency analysis" → "architecture proposal" → "SDK discovery" → "TypeScript rewrite." Each step was justified by new information (SDK existence wasn't known at the start), but the scope has grown significantly.

**Is the scope justified?** Yes, IF the prototype succeeds. The SDK genuinely changes the calculus — it's not scope creep from ambition, it's scope adjustment from discovery. But the prototype is the honest gate. If it fails, the scope contracts back to "improve the Python harness incrementally."

**The discipline:** Phase 0 is 1 day. If it fails, total investment is 1 day + design documents (which have value regardless). Each subsequent phase is independently gated with 2x time gates. The rewrite never gets ahead of validation.

**The honest risk:** The 1,800-2,200 line estimate could still be low. Merge logic, escalation, shelve/unshelve, and Discord command handling are real complexity. The revised estimate accounts for known business logic; unknown edge cases will add more. Budget 30% contingency.

---

## Acceptance Criteria (Full Plan)

- [ ] Phase 0: SDK prototype validates hook loading, AbortController, session resume (or documents failure)
- [ ] Phase 1: Task file → agent → merge-test-revert → result, with full state machine and crash recovery
- [ ] Phase 2: Discord message → task → agent → merge-test → result → Discord reply
- [ ] Phase 3: Complex tasks get independent review before merge
- [ ] Phase 4: Event-driven tasks + concurrent execution + thread-per-task Discord UX
- [ ] 10 successful end-to-end tasks before Python harness retired
- [ ] All phases have passing tests (unit + integration)
- [ ] Merge-test-revert gate: untested code cannot reach trunk
- [ ] Escalation circuit breaker: infinite retry loops impossible
- [ ] Audit trail: every task has log entries for creation, spawn, merge, gate, completion
- [ ] Cost tracking: per-task USD cost visible in audit log
