---
title: "v5 Harness Supervised Session Architecture"
tags: [harness, architecture, typescript, rewrite, concurrency]
category: architecture
created: 2026-04-11
updated: 2026-04-11
---

# v5 Harness Supervised Session Architecture

Architectural decisions from the design discussion that pivoted the TypeScript rewrite from a multi-stage pipeline to a supervised session model. This supersedes the pipeline-centric framing in [[v5-harness-efficiency-proposal]] while preserving its core thesis (harness = infrastructure, intelligence = OMC).

## Core Insight: The Harness Is Not a Pipeline

The original v5 harness and the v2 ralplan both modeled the orchestrator as a stage-based pipeline (classify -> architect -> executor -> reviewer -> merge -> wiki -> done). This created friction with the operator experience, which should resemble conversational development with CC+OMC — not dispatching work through rigid stages.

**The reframe:** The orchestrator is a daemon that manages agent sessions. Each session is a full CC+OMC instance. The orchestrator provides structural guarantees the agent can't self-serve: independent review, merge safety, crash recovery, ambient Discord presence, and parallel task management.

## Architecture

```
+-----------------------------------------------+
|            Orchestrator Daemon                  |
|                                                 |
|  Discord Presence <-> Operator (bidirectional)  |
|       |                                         |
|  Session Manager                                |
|    +-- Session A (task-42, worktree-a)          |
|    |     +-- CC+OMC agent (long-running query())|
|    +-- Session B (task-43, worktree-b)          |
|    |     +-- CC+OMC agent (long-running query())|
|    +-- [reviewer pool -- ephemeral, on-demand]  |
|                                                 |
|  Gate Enforcer                                  |
|    +-- Independent code review (fresh query())  |
|    +-- Merge queue (exclusive, rebase-first)    |
|    +-- Wiki summary                             |
|                                                 |
|  Recovery Manager                               |
|    +-- Crash detection + session resume          |
|    +-- Timeout / cost enforcement               |
|    +-- Context compression management           |
|                                                 |
|  Event Watcher (clawhip bridge)                 |
|    +-- Git push -> auto-task                    |
|    +-- CI failure -> fix-task                   |
|    +-- Cron -> maintenance tasks                |
+-----------------------------------------------+
```

## Session Lifecycle

```
1. Task arrives (Discord message or event)
2. Orchestrator creates worktree, spawns agent session via SDK query()
3. Agent works (full CC+OMC -- plans, codes, tests, iterates)
   |-- Operator messages flow in via Discord at any time
   |-- Agent sends updates via clawhip at natural milestones
   |-- Orchestrator monitors: cost, time, liveness, context health
   |-- Agent can self-escalate complexity
4. Agent signals completion (writes to known path in worktree)
5. Gate sequence (orchestrator-managed):
   a. Complexity check (mechanical + agent assessment + operator flag)
   b. IF review triggered: fresh reviewer -> feedback loop (max 3) -> escalate
   c. Rebase onto trunk (merge queue, exclusive lock)
   d. Merge-test-revert (mechanical, always)
   e. Wiki summary (agent writes, post-merge)
6. Done -> report to Discord
```

## State Machine (Minimal)

```
pending --> active --> reviewing --> merging --> done
              ^           |            |
              +-----------+            v
             (rejection)            failed

Plus orthogonal flags: shelved, escalation_wait, paused
```

`active` covers everything the agent does internally. The orchestrator doesn't subdivide it or track the agent's internal OMC workflow.

## Key Design Decisions

### 1. Operator Interjection Model

Operator messages flow into active agent sessions at any time via Discord. No routing by stage, no "is this a task update or new command?" classification. The agent receives it as conversation context via `resume(sessionId)` or new `query()` turn. Structured commands (`!status`, `!pause`, `!tell`) coexist with natural language.

Thread-per-task in Discord solves multi-session routing: messages in a thread go to that session, messages in #general go to the orchestrator.

### 2. Classification: LLM for Discord, Deterministic for Events

Deterministic routing only for machine-generated events (git push -> run tests, cron -> audit). Discord input keeps LLM classification because human messages are inherently ambiguous. The Python harness's `classify()` works and fails safe (defaults to "complex"). No reason to replace it with a rule engine.

### 3. Complexity Assessment: Three-Source Union

Complexity is assessed from three sources. The union triggers review -- any one is sufficient:

- **Operator flag** -- operator marks task complex. Can never be downgraded by agent.
- **Agent self-escalation** -- agent discovers during work that scope is larger than expected. Can only escalate up, never down.
- **Mechanical triggers** -- orchestrator checks actual diff post-completion: file count, diff size, sensitive paths (auth/, security/, core/).

Assessed twice: once on the plan (estimated), once on the diff (actual). Catches scope creep.

### 4. Clawhip Roles (Corrected)

- **Discord relay** -- clawhip manages Discord gateway (inbound via discord.py pattern, outbound notifications). Agents send updates by writing messages that clawhip routes.
- **File/git watching** -- event detection for auto-task creation.
- **Discord.js** -- handles inbound gateway connection (replacing discord.py in TypeScript). Clawhip retained for monitoring and outbound.

### 5. Agent Progress Updates

Agents are prompted (not polled) to send updates at natural milestones: understood task, have plan, started execution, hit snag, running tests, ready for review. Updates flow through clawhip to Discord. Not on a timer, not after every file edit.

### 6. Context Management by Orchestrator

The orchestrator manages what the agent cannot: its own context window health.

- **Token tracking:** SDK `usage` fields accumulated per session. At threshold (~80k tokens), orchestrator triggers context summary and fresh session.
- **Stale context:** Long-idle sessions get context refresh on resume.
- **Resume vs. fresh:** Short interruptions -> `resume`. Long gaps or crash -> fresh `query()` with reconstructed context (task description, git diff, last summary, operator messages).

### 7. Full Reviewer Isolation

Independent reviewer gets: diff + acceptance criteria + read-only codebase access. Does NOT get: executor's conversation, operator messages, task description beyond what's in the diff context. This is intentional -- reviewer's job is "does this code work and is it sound," not "did the agent follow instructions."

## Concurrency Model

### Merge Queue With Exclusive Lock

Parallel work, serial integration. Agents work simultaneously in separate worktrees. Merges are serialized through an exclusive queue (FIFO for V1).

```
Agent A completes -> enters merge queue -> rebase -> test -> merge -> trunk advances
Agent B completes -> waits in queue -> rebase onto new trunk -> test -> merge
```

### Rebase Before Merge

Every merge attempt starts with `git rebase` onto current trunk. This handles the common case (stale base, no overlapping edits) silently. Only true textual conflicts fail.

```
Agent signals completion
  -> git fetch trunk into worktree
  -> git rebase onto trunk
  -> IF clean: proceed to merge gate
  -> IF textual conflict, auto-resolve attempted
  -> IF auto-resolve fails: shelve + auto-retry after cooldown (5min)
  -> IF persistent (3 retries): escalate to operator
```

### Conflict Prediction

Orchestrator periodically scans active worktrees (`git diff --name-only`) to detect overlapping file sets. When overlap detected:

- Don't kill either agent
- Mark sessions as "merge-conflicting"
- First to complete merges normally
- Second gets automatic rebase
- If overlap detected early, inject advisory message to agent

### State Write Safety (Lesson B1)

Node.js single-threaded event loop guarantees no concurrent state mutation IF writes are synchronous within Promise resolution callbacks. All state mutations follow:

1. Mutate in-memory state object (synchronous)
2. Write to temp file (synchronous: `fs.writeFileSync`)
3. Atomic rename (synchronous: `fs.renameSync`)

Never `await` between reading and writing state. This is the mutation queue pattern from the Python harness, simplified by Node.js's execution model.

### Semantic Conflict Detection

Two agents edit different files but make incompatible changes. The test suite in the merge gate is the only reliable detector. Rebase succeeds (no textual conflict), merge succeeds, tests fail -> revert. The second agent's work is preserved in its worktree for retry after the first agent's changes are on trunk.

## Estimated Component Sizes

| Component | Responsibility | Lines |
|-----------|---------------|-------|
| `src/daemon.ts` | Process entry, shutdown, signals | ~60 |
| `src/session/manager.ts` | Spawn, resume, kill sessions. Worktree lifecycle. | ~250 |
| `src/session/context.ts` | Token tracking, compression triggers, context reconstruction | ~150 |
| `src/gates/reviewer.ts` | Fresh reviewer query(), feedback loop, escalation | ~120 |
| `src/gates/merge.ts` | Merge queue, rebase, test suite, revert, conflict handling | ~250 |
| `src/gates/conflicts.ts` | Conflict prediction: worktree diff scanning, overlap detection, advisory | ~60 |
| `src/discord/relay.ts` | Discord.js inbound, clawhip outbound, thread routing | ~200 |
| `src/discord/commands.ts` | !task, !status, !pause, !resume, !tell, !caveman | ~100 |
| `src/events/clawhip.ts` | Clawhip bridge, auto-task from git/cron | ~80 |
| `src/orchestrator.ts` | Main loop: monitoring, liveness, timeout, completion detection | ~200 |
| `src/lib/config.ts` | TOML loader, agent definitions | ~60 |
| `src/lib/state.ts` | Task state, crash recovery, atomic writes | ~120 |
| `src/lib/audit.ts` | Event log (write-only), cost tracking | ~60 |
| **Total** | | **~1,650** |

## What the Orchestrator Manages vs. Delegates

**Orchestrator manages:**
- Discord presence (always online)
- Session lifecycle (spawn, resume, kill, crash recovery)
- Message routing (thread = session, #general = orchestrator)
- Context health (token tracking, compression, reconstruction)
- Structural gates (review, merge-test-revert)
- Concurrency (merge queue, conflict prediction, worktree isolation)
- Cost/time bounds (maxBudgetUsd, AbortController)
- Event watching (clawhip bridge)
- Audit trail

**Agent (CC+OMC) manages:**
- Planning approach
- Execution strategy
- Internal review/iteration
- Tool selection
- When to ask for help (self-escalation)
- Progress updates to Discord

## Related

- [[v5-harness-efficiency-proposal]] -- Original architecture thesis (infrastructure-not-intelligence)
- [[v5-harness-lessons-learned]] -- 17 surviving lessons that inform this design
- [[v5-harness-design-decisions]] -- Historical design decisions from Python harness
