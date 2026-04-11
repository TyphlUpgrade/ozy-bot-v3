---
title: "Proposal: Harness as Infrastructure Layer, Not Intelligence Layer"
tags: [harness, architecture, proposal, efficiency, discord, omc]
category: decision
created: 2026-04-11
updated: 2026-04-11
---

# Proposal: Harness as Infrastructure Layer, Not Intelligence Layer

## Core Thesis

The harness should not replicate what OMC already does. It should provide the things a single CC + OMC instance **structurally cannot give itself**: event loops, process supervision, structural guarantees, and persistence across sessions.

**The harness is infrastructure. The intelligence lives in OMC.**

## Benchmark: What OMC Already Does

Before building anything, acknowledge what a single CC + OMC instance handles:

- **Architect/critic review** — ralplan, autopilot Phase 4
- **Parallel subtask execution** — ultrawork, teams
- **Persistent task loops** — ralph
- **Subagent delegation with model routing** — haiku/sonnet/opus tiers
- **Code review, security review, verification** — specialized agents

Reimplementing any of these in the harness is engineering for its own sake.

## What CC + OMC Structurally Cannot Do

### 1. Ambient Operation (Event-Driven, Not Prompt-Driven)

CC only works when someone talks to it. The harness runs continuously and **reacts to events**: git push, CI failure, Discord message, stale branch, dependency update, trading bot anomaly.

Nobody types a prompt. CI fails on main → harness creates fix task → agent executes → PRs → pings operator for approval. You wake up to a green build.

This is the killer differentiator. Not "I tell it what to do from Discord" but "it notices things need doing and does them."

### 2. Structural Guarantees vs. Prompt Compliance

When you tell OMC "review before merging," it's a suggestion in a prompt. The model can skip it, rush it, or self-approve. In the harness, the pipeline **physically cannot advance** from executor to merge without a reviewer stage.

This matters for trust calibration. You can prove "every merged change went through independent review" because the pipeline enforces it structurally, not because you trust the model followed instructions.

### 3. Crash-Resilient State Machine

A CC instance that crashes loses its conversation context. OMC's ralph has iteration persistence, but it's within a single CC session. If Claude Code itself crashes (OOM, API timeout, terminal closed), ralph state is orphaned.

The harness writes pipeline state to disk after every transition. Process dies → restart → resumes at exact stage. The orchestrator is the supervisor — it restarts agents, not the other way around.

### 4. Context Management Across Tasks

A single CC conversation accumulates context until it compresses and loses nuance. The harness can give each agent **exactly the context it needs**: scoped files, relevant history, specific acceptance criteria. Fresh context per task, no pollution from previous work.

Beyond per-task scoping, the harness **accumulates patterns across tasks**: "executor always struggles with async test fixtures," "reviewer catches more issues in auth/ than utils/," "tasks touching the broker module take 3x longer than estimated." This metadata improves task routing, time estimates, and context scoping over time. No single instance can build this picture because each starts fresh.

### 5. External System Integration as First-Class

Clawhip monitors git, workspace files, tmux sessions. Discord provides async human-in-the-loop. A CC instance has tool access but no **event loop**. It can read GitHub but can't **watch** GitHub.

### 6. Deterministic Audit Trail

The harness produces a machine-readable log of every decision: who approved what, which tests passed, what was the diff at merge time, what agent handled the task, how long each stage took. A single CC instance produces conversation logs. The harness produces an **auditable pipeline trace**. This matters for any team that needs to answer "why did this change ship?" — provable compliance, not trust-based compliance.

## Extended Value Stack (Futures)

> **Note:** Items 7-12 below are genuine differentiators but are not part of Phases 1-5. They represent the long-term vision for what the infrastructure layer enables *once the core is solid*. The architectural case stands on items 1-6 alone.

### 7. Time Awareness and Scheduling

CC has no sense of time passing. It runs when prompted, not when something is *due*. The harness knows: "this PR has been open 3 days with no review," "tests haven't run since Tuesday," "the dependency audit is overdue." It can defer non-urgent work to off-peak hours, schedule recurring maintenance, and create urgency signals based on elapsed time — none of which a prompt-driven instance can do.

### 7. Post-Merge Monitoring

CC's involvement ends when the PR merges. The harness can **watch what happens after**: did the deploy succeed? Did error rates spike? Did a downstream test suite break? If so, it creates a rollback or fix task automatically. The feedback loop closes without human intervention.

### 8. Speculative Execution *(Experimental — operationally risky)*

While waiting for operator approval on a risky merge, the harness can **speculatively start the next task** in a separate worktree. If approved, work is already in progress. If rejected, the speculative work is discarded cheaply. A single instance blocks on every approval; the harness pipelines around them.

> **Risk:** Speculative work in a shared codebase creates phantom branches and wasted API spend. This should be validated with real pipeline data before committing. Consider starting with speculative *planning* only (cheaper to discard than speculative implementation).

### 9. Operator Augmentation

The harness doesn't just relay messages — it **enriches the operator's context**. When the operator asks "what's going on?", a single instance reports its own state. The harness reports: all active tasks, their stages, blockers, recent completions, test health, git state, and estimated time to next milestone. It's the difference between asking one worker vs. asking the floor manager.

### 11. Cost Optimization

A single CC instance uses whatever model it's configured with for everything. The harness routes by actual complexity: haiku for classification and simple responses, sonnet for standard implementation, opus only for architecture and complex debugging. Token budgets per task prevent runaway costs. The harness can also batch similar tasks to amortize context assembly.

### 12. Proactive Maintenance

Nobody prompts "check if dependencies are outdated" or "run the full test suite on a schedule." The harness does this autonomously: dependency audits, test coverage trending, dead code detection, documentation staleness checks. These create a steady stream of small maintenance tasks that keep the codebase healthy without operator attention.

## Revised Architecture: Infrastructure + OMC

The harness is the **supervisor and policy layer**. LLM work happens inside OMC.

```
Event Sources (Discord / Git / CI / Cron / File watch)
        ↓
  Orchestrator (lightweight state machine — NOT an LLM)
        ↓
  Spawn CC + OMC instance with:
    - Scoped context (only relevant files/history)
    - Structural constraints (must pass tests, must get review)
    - Resource budget (token limit, time limit)
    - Event subscriptions ("notify me when CI finishes")
        ↓
  CC + OMC does the actual work (ralph, autopilot, whatever fits)
        ↓
  Orchestrator collects result, enforces gates, advances pipeline
```

### What the Orchestrator Does

- **Event detection** — monitors external systems for actionable events
- **Task routing** — decides what to work on, in what order, with what urgency
- **Context scoping** — assembles the right context for each agent instance
- **Structural gates** — enforces policy (review required, tests must pass, etc.)
- **Crash recovery** — restarts agents, resumes pipeline from last checkpoint
- **Resource management** — token budgets, rate limits, concurrent instance limits
- **Audit trail** — wiki, signal files, stage transitions
- **Discord companion** — human-in-the-loop async interface

### What the Orchestrator Does NOT Do

- **Planning** — that's OMC's architect/ralplan
- **Code review** — that's OMC's code-reviewer/critic
- **Task persistence** — that's ralph
- **Parallel execution** — that's ultrawork/teams
- **Pipeline-internal classification** — deterministic routing by event source for pipeline stages (LLM classification preserved for Discord NL disambiguation where human intent is genuinely ambiguous)

### What Changes from v5

| v5 Current | Proposed |
|------------|----------|
| `classify` stage (haiku LLM call) | Deterministic routing by event source for pipeline stages; haiku NL classification preserved for Discord disambiguation |
| `architect` stage (separate tmux session) | OMC handles planning inside agent |
| `executor` stage (separate tmux session) | CC + OMC instance handles full task |
| `reviewer` stage (separate tmux session) | OMC's code-reviewer inside agent, OR independent instance for structural guarantee |
| Signal file coordination | Async queues + signal files as audit only |
| `claude -p` subprocess per call | Direct SDK for orchestrator utilities; agents use native CC tools |

## Implementation Phases

### Phase 1: Agent Instances Are CC + OMC (Medium-High effort, High impact) ⚠️ HIGHEST RISK

Replace bare tmux claude sessions with full CC + OMC instances. The agent gets ralph, ultrawork, architect, critic — the full toolkit. The orchestrator just provides the task, context, and constraints.

Concrete change: agent prompt includes OMC instructions, CC launched with `--permission-mode dontAsk`, CLAUDE.md scopes the work.

**Risk (architect + critic convergence):** Current agent launch uses `claude -p --input-format stream-json` with FIFO piping. OMC expects an interactive terminal, not piped JSON. The FIFO/stream-json protocol may not survive this transition. **Prototype this first** — verify a CC+OMC instance can initialize OMC hooks when launched via the current session mechanism before committing to the rest of the roadmap.

**Result collection contract:** The orchestrator currently reads structured results via signal files (`signals/{agent}/completion-{task_id}.json`). With CC+OMC instances running ralph/autopilot internally, the agent must still write the completion signal as its terminal action. The harness owns task lifecycle (start, gate, complete); OMC tools operate within the agent's session but do not control pipeline advancement. This contract must be documented and enforced.

### Phase 2: Event-Driven Task Creation (Medium effort, High impact)

Orchestrator watches for events and creates tasks without human prompting:
- Git push to PR branch → run tests, report results
- CI failure on main → create fix task
- Stale branch detected → cleanup task
- Scheduled cron → dependency audit, test coverage report
- Trading bot anomaly → investigation task

### Phase 3: Structural Review Gate (Small effort, High impact)

For complex/risky tasks, spawn an **independent** CC instance for review. Key word: independent. Same codebase access, but no shared conversation context with the executor. Can't be influenced by the executor's reasoning.

This is genuinely stronger than OMC's internal code-reviewer (which shares the conversation).

### Phase 4: Non-Blocking Task Queue (Medium effort, High impact)

Multiple tasks can be in-flight. Escalated tasks park; next task starts. Operator responds asynchronously. True work queue, not single-task pipeline.

### Phase 5: Earned Autonomy (Medium effort, Medium impact)

Track record → reduced oversight:
- N clean merges → auto-merge simple fixes
- M clean merges → auto-merge anything with passing tests
- Trust decays on failures
- Operator configures gradient via Discord

### Future: Ambient Codebase Intelligence

- Regression watchdog: continuously monitors for test failures, style drift
- Dependency tracker: flags outdated/vulnerable deps, creates update tasks
- Knowledge distillation: after each task, extracts reusable patterns into wiki
- Cross-task optimization: groups related tasks, identifies shared refactoring

## Instance Model: Single by Default, Multiple for Structural Guarantees

### Default: One CC + OMC Instance Per Task

A single instance with ralph can plan, code, test, review, and iterate — all in one context. No coordination overhead, no context loss. For ~90% of tasks, this is strictly better than multiple instances.

### When to Spawn Multiple

| Scenario | Why multiple helps | Why single fails |
|----------|-------------------|-----------------|
| **Independent review** | Reviewer has fresh context, can't be primed by executor's reasoning | Self-review is theater — same model rationalizes the same choices |
| **Truly parallel tasks** | Two unrelated tasks run simultaneously | Single instance is sequential; one waits |
| **Preemption** | Urgent fix while big task runs — spawn second instance | Must shelve, context-switch, lose momentum |
| **Long task + monitoring** | One works, another watches CI/tests/health | Can't background-monitor while coding |

### Architecture

```
Default (90%):  Orchestrator → 1 CC+OMC instance → work → result

Complex:        Orchestrator → CC+OMC executor → work
                             → CC+OMC reviewer → independent review
                (reviewer gets only: diff, acceptance criteria, codebase access)
                (NOT the executor's conversation or reasoning)

Parallel:       Orchestrator → CC+OMC instance A → task 1
                             → CC+OMC instance B → task 2
                (only for genuinely independent tasks)
```

The independent reviewer is the strongest argument for multiple. A fresh instance with only the diff and acceptance criteria catches things the executor was blind to — different perspective, not just different hat.

### Open Concerns

**1. When to escalate from single to multiple?**

The orchestrator must decide when a task warrants independent review vs. self-review. Wrong threshold in either direction is costly: too aggressive spawns reviewers for one-line fixes (waste), too conservative lets complex changes self-approve (risk).

Candidate heuristics (all configurable, none proven):
- File count: >N files touched → independent review
- Diff size: >M lines changed → independent review
- Sensitivity tags: security/, auth/, payments/ paths → always independent
- Explicit policy: operator sets per-task or per-category rules via Discord
- Earned autonomy: trust score modulates the threshold over time

Need real data from pipeline runs to calibrate. Initial approach: default to independent review for everything complex (per existing classifier), self-review for simple. Tune down as trust builds.

**2. Operator routing: who am I talking to?**

With multiple instances, the operator's Discord message must reach the right agent. Current NL routing (classify_target) assumes one set of named agents. With per-task instances, the routing problem changes:

- **During single-instance work**: operator messages go to that instance. Simple.
- **During executor + reviewer**: operator needs to specify which, or the orchestrator routes by context:
  - Messages about the implementation → executor
  - Messages about review concerns → reviewer
  - Ambiguous → ask (current "who do you mean?" pattern, but per-instance not per-role)
- **During parallel tasks**: operator references task ID, not agent role. "tell task-42 to use async generators" not "tell the executor."
- **Thread-per-task**: Discord threads solve this naturally. Each task gets a thread. Messages in that thread go to that task's instance. Cross-thread messages go to orchestrator.

Thread-per-task is the cleanest UX. Requires Discord thread API integration but eliminates routing ambiguity.

**3. Managing multiple complex agents**

Each CC + OMC instance is a heavyweight process: tmux session, conversation context, tool access, file system state. Concerns:

- **Resource limits**: How many concurrent instances can the machine handle? Token rate limits, CPU, memory, disk I/O. The orchestrator needs a concurrency cap (configurable, default probably 2-3).
- **Git conflicts**: Two instances editing the same file → merge conflicts. Mitigations:
  - Each instance works in its own git worktree (already implemented)
  - Orchestrator sequences tasks that touch overlapping files
  - Parallel only for tasks with disjoint file sets (detectable from task description + codebase knowledge)
- **Shared state corruption**: Pipeline state, signal files, wiki — multiple writers risk corruption.
  - Orchestrator owns all shared state writes (agents report results, orchestrator applies them)
  - Agents are read-only on shared state, write-only to their own worktree
- **Lifecycle management**: Instances must start cleanly, report completion, and die. Orphaned instances waste resources.
  - Heartbeat: instance pings orchestrator periodically
  - Timeout: no heartbeat for N minutes → orchestrator kills and restarts or shelves
  - Clean exit: instance signals completion → orchestrator collects result, tears down
- **Observability**: Operator needs to know what's running, where, and how it's going.
  - `!status` shows all active instances, their tasks, and current stage
  - Discord embeds per instance with progress
  - Clawhip monitors all tmux sessions

**4. Stuck detection vs. working detection**

A full CC+OMC instance running ralph might be idle for minutes between iterations — that looks identical to "stuck" from outside. Current health checks use tmux pane PID, which only tells you the process is alive, not productive. The heartbeat model (concern #3) needs to distinguish:

- **Active:** agent wrote output or a signal file in the last N minutes
- **Idle-but-working:** ralph is between iterations, waiting for test suite, etc.
- **Stuck:** no progress indicators for >M minutes, conversation context may be exhausted

Candidate approach: agents write a lightweight progress heartbeat (timestamp + current stage) to a known path. Orchestrator reads it. No heartbeat for N minutes + no signal file → escalate or restart. This must be designed before Phase 1 ships.

**5. Task lifecycle ownership: harness vs. OMC**

If ralph runs inside a CC+OMC agent, who decides when the task is "done" — ralph or the harness? Two supervisors is a classic coordination failure. Ralph might declare success while the harness is still waiting for a signal file, or the harness might time out while ralph is mid-iteration.

**Resolution:** The harness owns the pipeline lifecycle. OMC tools (ralph, ultrawork, autopilot) are capabilities available to the agent, but the agent's **terminal action** must be writing the completion signal file. Ralph iterates internally; when ralph is satisfied, the agent writes the signal. The harness never reaches into the agent's conversation to check OMC state — it only reads signal files and heartbeats. This is the boundary: harness = supervisor, agent = worker with tools.

## What NOT to Build

- Don't reimplement OMC's architect/critic pipeline — use it
- Don't reimplement ralph's task persistence — use it
- Don't reimplement ultrawork's parallelism — use it
- Don't build LLM-based classification for pipeline-internal routing — use deterministic routing (keep LLM for Discord NL disambiguation where human intent is ambiguous)
- Don't build custom review agents — use OMC's code-reviewer with independent context

## Reviewer Findings (2026-04-11)

Proposal reviewed independently by architect and critic agents. Key convergences:

### Verdict: ACCEPT-WITH-RESERVATIONS

**Top 3 Strengths:**
1. Core thesis is correct — harness should not reimplement OMC's planning, review, and iteration
2. Independent review gate (Phase 3) is a genuinely novel structural guarantee OMC cannot provide
3. Crash-resilient state machine and ambient operation are legitimate differentiators

**Top 3 Risks:**
1. **Dual supervision** — ralph inside agent + harness pipeline could fight over task lifecycle (resolved: harness owns lifecycle, agent writes terminal signal)
2. **Deterministic routing gap** — original proposal removed all LLM classification without specifying concrete replacement rules (resolved: keep haiku for Discord NL, deterministic for pipeline-internal)
3. **Phase 1 compatibility** — FIFO/stream-json agent launch may not survive OMC hook initialization (unresolved: needs prototype)

**Actionable changes applied:**
- Merged overlapping value props 4 (context) + 8 (cross-task learning)
- Added Deterministic Audit Trail as core differentiator (#6)
- Separated core architectural case (items 1-6) from futures roadmap (items 7-11)
- Marked Speculative Execution as experimental with risk note
- Nuanced routing claim: deterministic for pipeline, LLM for Discord NL
- Added Phase 1 risk warning and result-collection contract requirement
- Added open concerns #4 (stuck detection) and #5 (task lifecycle ownership)

**Open questions for future work:**
- How does `--permission-mode dontAsk` interact with OMC sub-agent delegation?
- Discord thread API rate limits and bot permissions for thread-per-task UX
- Cost analysis: per-task cost of CC+OMC instances vs. current bare sessions
- Testing strategy: 388 tests mock `claude -p` subprocess calls — CC+OMC instances are harder to mock
- Signal file migration: "audit only" claim requires scoping the async queue replacement

### Round 2: Feasibility Review (2026-04-11)

Architect and critic reviewed the SDK addendum and TypeScript migration. Verdict: **REVISE** — architectural direction correct, scope estimates were wrong.

**Corrected scope (critic finding):**
- Python harness is 2,400+ non-trivial lines, not ~500. Discord companion alone (685 lines) exceeds original claim.
- ~390 of 418 tests are business logic, not infrastructure. Only ~28 test FIFO/signal/tmux patterns.
- However: rewrite analysis shows SDK eliminates ~800 lines of transport code entirely, and pipeline collapse reduces orchestrator from 7 stages to 3. Rewrite estimate: ~1,100 lines with ~130 behavioral specs to preserve.

**Risks identified:**
| Risk | Rating | Status |
|------|--------|--------|
| OMC hook loading via `settingSources` unverified | MEDIUM-HIGH | **BLOCKING — prototype required** |
| "Stream silence = stuck" too simplistic | MEDIUM | Resolved: use tool-aware liveness (active / tool-running / stuck) |
| No cutover plan during migration | HIGH | Needs migration section: parallel-run, cutover criteria, rollback |
| clawhip post-migration role unclear | MEDIUM | Evaluate: keep for Discord relay + file watching, or absorb into TS |
| Rewrite loses edge-case knowledge from 390 tests | MEDIUM | Mitigated: extract test intent as checklist before rewriting |
| Scope creep during rewrite | HIGH | Mitigated: V1 scope locked to route → spawn → collect → gate → report |

**Architect recommendations applied:**
- Port 89 state-machine + escalation tests as behavioral specification (TDD)
- Add `AbortController` + `maxBudgetUsd` as standard `query()` wrappers
- Tool-aware liveness replaces naive stream-silence detection
- Phase 1 risk corrected to MEDIUM-HIGH pending prototype

## Addendum: Claude Agent SDK as Implementation Path (2026-04-11)

The discovery of the Claude Agent SDK (`@anthropic-ai/claude-agent-sdk`) fundamentally changes the implementation approach. The architectural thesis is unchanged — the SDK is *how* we build the infrastructure layer.

### What the SDK Provides

The SDK is a **native TypeScript library** (not a subprocess wrapper) that implements the same agent loop CC uses:

```typescript
import { query } from "@anthropic-ai/claude-agent-sdk";

for await (const message of query({
  prompt: "Fix the auth bug",
  options: {
    cwd: "/path/to/worktree",
    model: "claude-sonnet-4-6",
    permissionMode: "dontAsk",
    allowedTools: ["Read", "Edit", "Bash", "Grep", "Glob"],
    systemPrompt: "You are an executor agent...",
    settingSources: ["project"],  // loads CLAUDE.md, hooks, MCP
    maxTurns: 50,
  }
})) {
  // Typed message stream: assistant, result, system, task_progress
}
```

### How It Resolves Prior Concerns

| Concern | FIFO/tmux approach | SDK approach |
|---------|-------------------|-------------|
| Structured I/O | Parse stream-json | Typed message objects natively |
| Multi-turn | FIFO pipe, manual JSON framing | `resume: sessionId` built-in |
| Concurrent agents | Separate tmux sessions | `Promise.all()` over `query()` calls |
| OMC/hooks | Only in interactive mode | `settingSources: ["project"]` loads hooks |
| Result collection | Signal files + polling | `message.type === "result"` in stream |
| Permissions | `--dangerously-skip-permissions` | Granular `allowedTools` + `disallowedTools` |
| Health/progress | PID checks, tmux scraping | Message stream is the heartbeat |
| Cost tracking | Manual token parsing from logs | Every result has `usage` and USD cost |
| Tool interception | Not possible | PreToolUse/PostToolUse hooks in code |
| Session persistence | Lost on crash | Auto-persisted JSONL, resumable by ID |
| Stuck detection (concern #4) | Needs custom heartbeat | Stream silence = stuck; message flow = alive |
| Lifecycle ownership (concern #5) | Signal file contract | `query()` returns when agent is done — no ambiguity |
| Phase 1 risk (FIFO/OMC compat) | Needs prototype, may fail | SDK is the native CC runtime — no compatibility question |

### Phase 1 Revised: SDK Agent Instances

**Risk level drops from HIGHEST to MEDIUM-HIGH.** The FIFO/stream-json compatibility concern is eliminated — the SDK *is* the CC runtime. However, OMC hook initialization via `settingSources: ["project"]` is **unverified** and is the blocking unknown. If OMC skills don't load, agents are bare CC with tools — useful, but the "intelligence lives in OMC" thesis weakens significantly. **Prototype this before committing to anything else.**

### Implementation Language Decision: TypeScript

The SDK is TypeScript. The orchestrator moves to TypeScript. Clean break from the Python harness.

**Rationale:** The harness and trading bot share *protocols* (filesystem, git, Discord), not code. No import-level dependency exists. Keeping the harness in Python "because the trading bot is Python" would couple by language affinity where no actual code dependency exists, and require a permanent serialization bridge for every SDK interaction.

```
Trading Bot (Python)              Harness (TypeScript)
  ├── ozymandias/                   ├── src/orchestrator/
  ├── intelligence/                 ├── src/discord/
  ├── execution/                    ├── src/agents/
  └── tests/                        └── tests/
         ↕                                ↕
    [files on disk]                  [SDK query() calls]
    [broker APIs, numpy, pandas]     [Discord.js, clawhip events]
```

### Rewrite vs. Port: Rewrite From Scratch

**Decision: Rewrite, not port.** The SDK changes the problem shape enough that porting line-by-line would preserve complexity that no longer has a reason to exist.

**Why not port?** Critic review (round 2) found the Python harness is 2,400+ non-trivial lines across 9 files with ~390 business logic tests. But many of those lines and tests exist because of the FIFO/tmux/signal architecture:
- Signal file polling race conditions → don't exist with SDK streaming
- FIFO write failures → don't exist with SDK `query()`
- tmux PID checks → don't exist with SDK
- Token rotation thresholds → SDK handles context internally
- Multi-stage pipeline orchestration → SDK agent handles plan+execute+test internally

**The pipeline collapses.** Current 7-stage pipeline (classify → architect → executor → reviewer → merge → wiki → done) exists because the orchestrator micromanages each agent role. With CC+OMC agents, the pipeline simplifies to: **route → agent works → gate check → done.** The agent handles planning, execution, testing, and internal review. The orchestrator only enforces structural gates the agent can't self-serve.

**Estimated rewrite size: ~1,100 lines** (vs 2,400 to port):

```
src/orchestrator/  (~390 lines)
  state.ts         — TaskState, persist to disk
  router.ts        — event source → task config
  gates.ts         — review gate, test gate, merge approval
  index.ts         — event loop, spawn agent, collect result, advance

src/agents/        (~170 lines)
  pool.ts          — query() wrapper with AbortController, budget, liveness
  definitions.ts   — agent configs (role prompts + permissions)

src/discord/       (~380 lines)
  bot.ts           — Discord.js client, thread management
  commands.ts      — !status, !tell, !pause, !resume, !caveman, !task
  routing.ts       — NL routing (which agent/task did you mean?)

src/events/        (~80 lines)
  watcher.ts       — clawhip event bridge OR native git/file watching

src/lib/           (~100 lines)
  config.ts        — TOML loader
  audit.ts         — wiki/log integration
```

**What genuinely transfers as test intent (~130 behavioral specs):**
- Escalation tier routing (~38 tests)
- Shelve/unshelve semantics (~15 tests)
- Discord command parsing (~40 tests, reimplemented for Discord.js)
- NL routing intent/target (~20 tests)
- Caveman level management (~10 tests)
- State persistence save/load (~10 tests)

**Caveats of rewriting:**
1. Rediscovering edge cases — some of those 390 tests encode lessons from real failures. Read Python tests before writing TS, extract intent into a checklist. Port the knowledge, not the code.
2. Initially buggier — Python harness is battle-tested. TS harness starts at zero. Acceptable because harness isn't production.
3. Scope discipline critical — "while we're at it" is the death of rewrites. V1: route → spawn → collect → gate → report. Nothing else.
4. Lose ability to fix bugs in old harness — once committed, Python rots. Mitigated by keeping rewrite small enough to ship fast.

### Proposed Directory Structure

```
harness-ts/
  ├── package.json
  ├── tsconfig.json
  ├── src/
  │   ├── orchestrator/
  │   │   ├── index.ts          # Main event loop
  │   │   ├── state-machine.ts  # Pipeline state, stage transitions
  │   │   ├── router.ts         # Event source → task routing
  │   │   └── gates.ts          # Structural gates (review required, tests pass)
  │   ├── agents/
  │   │   ├── pool.ts           # Agent instance lifecycle (SDK query() wrapper)
  │   │   ├── context.ts        # Context scoping per task
  │   │   └── definitions.ts    # Agent configs (model, permissions, tools)
  │   ├── discord/
  │   │   ├── companion.ts      # Discord bot (Discord.js)
  │   │   ├── commands.ts       # Command handling (route, status, caveman, etc.)
  │   │   └── threads.ts        # Thread-per-task UX
  │   ├── events/
  │   │   ├── git-watcher.ts    # Git push/PR/CI events
  │   │   ├── cron.ts           # Scheduled maintenance tasks
  │   │   └── clawhip.ts        # clawhip event bridge
  │   └── lib/
  │       ├── config.ts         # Project config (from TOML)
  │       ├── audit.ts          # Audit trail / wiki integration
  │       └── cost.ts           # Token budget / cost tracking
  ├── tests/
  │   ├── orchestrator/
  │   ├── agents/
  │   ├── discord/
  │   └── fixtures/
  └── config/
      ├── agents/               # Agent role files (.md)
      └── project.toml          # Harness configuration
```

### Key Unknown: OMC Skill Initialization

Does `settingSources: ["project"]` + CLAUDE.md load OMC's full skill system (ralph, ultrawork, architect agents)? Needs prototype verification:
1. Install SDK: `npm install @anthropic-ai/claude-agent-sdk`
2. Spawn one `query()` call with `settingSources: ["project"]`
3. Check if OMC hooks fire (session-start hook, stop hook)
4. Check if skills are available to the agent

If yes → agents get full OMC for free. If no → agents get CC tools without OMC orchestration, which is acceptable since the harness is now the orchestrator.

### What This Obsoletes

- **`harness/` Python codebase** — orchestrator, discord companion, pipeline, sessions, signals, lifecycle, claude wrapper. All replaced by TypeScript `harness-ts/`.
- FIFO pipe protocol (`os.mkfifo`, `O_NONBLOCK`, stream-json framing)
- tmux session management for agents (clawhip `tmux new` wrapper)
- PID-based health checks (`os.kill(pid, 0)`)
- Token rotation via log parsing (`parse_token_usage`)
- Signal file completion contract (replaced by SDK result messages)
- `claude -p` subprocess calls for classify/summarize/reformulate (replaced by SDK `query()` with `maxTurns: 1` or direct Anthropic SDK for simple calls)
- 388 Python tests (test patterns being replaced — new tests written for new architecture)

**What survives:**
- clawhip (Rust daemon) — Discord relay, git/file watching, event routing. Language-agnostic interface.
- Agent role files (`config/agents/*.md`) — content reusable as SDK `systemPrompt`
- `config/harness/project.toml` — configuration structure, adapted for TypeScript loader
- Wiki content (`.omc/wiki/`) — architecture docs, proposals, all still valid
- The trading bot (`ozymandias/`) — completely untouched, protocol boundary

## Related

- [[v5-harness-architecture]] — current pipeline design
- [[v5-harness-roadmap]] — active development roadmap
- [[v5-conversational-discord-operator]] — Discord UX improvements
