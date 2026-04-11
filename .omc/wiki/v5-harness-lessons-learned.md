---
title: "v5 Harness Lessons Learned — Institutional Knowledge for TypeScript Rewrite"
tags: [harness, lessons, rewrite, architecture]
category: decision
created: 2026-04-11
updated: 2026-04-11
---

# v5 Harness Lessons Learned

Extracted from the Python harness before the TypeScript rewrite. These are hard-won edge cases, defensive patterns, and operational lessons that must survive the rewrite — not as code, but as knowledge.

## Transport Lessons (FIFO/tmux-specific — mostly eliminated by SDK)

These problems won't recur exactly, but the underlying principles apply to any IPC.

| ID | Lesson | Source | Principle |
|----|--------|--------|-----------|
| T1 | FIFO open deadlock — never assume the other end of a pipe is ready | `sessions.py:158-169` | Always handle connection handshake with retries and backoff |
| T2 | Retry write once after reconnect, then give up | `sessions.py:224-239` | Bounded retry prevents infinite reconnect loops |
| T3 | Kill stale sessions before creating new ones | `sessions.py:148-152` | Always clean up prior handles before reuse |
| T4 | Close old FD before overwriting session | `sessions.py:137-143` | Close before replace prevents resource leaks |
| T5 | Two-phase shutdown: graceful signal, wait, then hard kill | `sessions.py:316-333` | Universal process lifecycle pattern |

## Business Logic Lessons (WILL recur in TypeScript)

These are the edge cases the rewrite will rediscover the hard way if not studied first.

### B1. Mutation Queue for Concurrency Safety
**`discord_companion.py:6-7`, `orchestrator.py:711-714`**

Discord handlers never mutate `PipelineState` directly. They append closures to `pending_mutations`, which the main loop drains synchronously. This prevents corruption between `await` points in a single-threaded event loop.

**Why it matters for TS:** Even with Node.js single-threaded model, any async gap between reading and writing state can interleave. The mutation queue pattern must survive.

### B2. Stale Signal Guard on Escalation Resume
**`orchestrator.py:401-403`, `signals.py:140-155`**

When entering escalation, `clear_stage_signal()` deletes the completion signal for the current stage. Without this, an old "executor done" signal would cause spurious advancement when resuming from escalation.

**Why it matters for TS:** Any event/signal-based system has this TOCTOU problem. The rewrite must clear stale completion markers on state transitions, even if using SDK message streams instead of signal files.

### B3. Shelved Task Escalation Clock Reset
**`pipeline.py:404-406`**

When unshelving, if the task was in escalation, the escalation timestamp resets to `now()`. Without this, time spent shelved counts toward escalation timeout → immediate timeout on unshelve.

### B4. Confirmation Pending Cancellation
**`discord_companion.py:890`**

A new dialogue message clears `dialogue_pending_confirmation`. Without this, an old "yes" detection would still be pending, and a stale confirmation could fire on an unrelated message.

### B5. Reviewer-Resume Loop Prevention (BUG-024)
**`orchestrator.py:180-181`**

When auto-escalating after max retries, `pre_escalation_stage` is set to `"executor"`, not `"reviewer"`. Reviewer rejected → resuming at reviewer → re-reject → infinite loop. Resuming at executor forces a fresh attempt.

### B6. Circuit Breaker Retry Count Reset (BUG-023)
**`orchestrator.py:175`**

Auto-escalation sets `retry_count=0` on the EscalationRequest so the circuit breaker routes to Tier 1 first, not straight to operator. Without this, escalated tasks skip intermediate resolution tiers.

### B7. Unknown State Keys on Version Mismatch (BUG-002)
**`pipeline.py:430-435`**

`PipelineState.load()` drops unknown JSON keys instead of crashing with `TypeError`. This handles state files from newer/older harness versions.

**Why it matters for TS:** State schema evolution is inevitable. Always deserialize defensively.

### B8. Shelved Reply Injection Timing
**`orchestrator.py:336-351`**

When unshelving a task whose escalation was resolved while shelved, the pending operator reply is injected only if the agent session exists. If not, the reply is preserved for future re-injection. The executor session "may belong to the just-completed task, not the unshelved one."

### B9. Dialogue Demoted on Crash
**`lifecycle.py:92-104`**

Crash during `escalation_dialogue` reverts to `escalation_wait` because dialogue context is ephemeral. Same for shelved tasks at `lifecycle.py:127-132`.

### B10. Late-Binding Closure Bug
**`discord_companion.py:234-236, 255-257, 308-310`**

Every lambda capturing a loop variable uses default-argument binding (`a=agent, m=message`). Python-specific footgun, but TypeScript closures over `let` in loops have analogous issues with `var`.

## Operational Lessons (Universal — apply regardless of implementation)

### O1. Default to Safe on LLM Failure
**`claude.py:75-82, 167, 198`**

Every LLM judgment call has a safe default: `classify()` → `"complex"`, `classify_resolution` → `"continuation"`, `classify_intent` → `"feedback"`. Always err toward more review, not less.

### O2. Zombie Reaping After Timeout Kill
**`claude.py:56-58`**

After `proc.kill()` on timeout, call `await proc.wait()` to reap the zombie. Without this, killed subprocesses become zombies. Applies to any subprocess model.

### O3. Atomic Writes Everywhere
**`pipeline.py:419-421`, `signals.py:181-188`**

All state and signal files use write-to-temp-then-rename. Prevents partial reads if the orchestrator crashes mid-write.

### O4. Path Traversal Validation
**`signals.py:17-24`**

`_safe_task_id()` validates task IDs against `[a-zA-Z0-9_-]+` using `fullmatch`. Called before every file path construction. Prevents `../../etc/passwd` attacks via crafted task IDs from Discord.

### O5. Confidence Gating Is Safe-by-Default
**`escalation.py:84-92`**

`should_promote()` promotes on anything other than `confidence == "high"`. Unknown confidence values promote rather than silently resolving.

### O6. Informational Escalations Do Not Pause
**`orchestrator.py:387-395`**

FYI escalations notify Discord and clear the signal without blocking the pipeline. Low-severity issues should not stall work.

### O7. Auto-Commit Safety Net Before Merge
**`orchestrator.py:228-246`**

The merge stage checks for uncommitted worktree changes and auto-commits them. Agents forget instructions; verify preconditions mechanically. Excludes `.omc/` from commits.

### O8. Test Timeout With Revert
**`orchestrator.py:274-287`**

Tests get a 180s hard timeout. On timeout, process is killed and merge is reverted. Prevents hanging test suites from blocking the pipeline forever.

### O9. Event Log Is Write-Only
**`events.py:17-22`**

The event log is a pure telemetry sink. Never read by the orchestrator. Prevents audit data from accidentally affecting control flow.

### O10. Accumulation Debounce for Multi-Message Input
**`discord_companion.py:580-628`**

NL messages buffer per-channel for 2 seconds before flushing as a single concatenated string. Timer is not cancelled mid-flush to avoid dropping messages.

### O11. Gateway Reconnect Dedup
**`discord_companion.py:574`**

A bounded `deque(maxlen=1000)` tracks seen message IDs to prevent duplicate processing on Discord gateway reconnect replays.

### O12. Wiki Skill Tool-Blocking Bug
**`claude.py:136-137`**

Real incident: passing a skill name as `--allowedTools` blocks real tools (Write, Read), leaving the model unable to write files. "Umbra catch, 2026-04-09."

## Merge Conflict Gap

**Current behavior (`orchestrator.py:253-262`):** On merge conflict, the harness calls `git merge --abort` then `state.clear_active()`. The task is **dropped** — no retry, no resolution, no operator notification beyond the log.

**Why this is a problem for parallel workers:** With concurrent worktrees, merge conflicts will be common. Dropping the task silently wastes the agent's work. Options for the TypeScript rewrite:

1. **Retry with rebase** — `git rebase` onto updated trunk, then re-merge. Risky if the agent's changes conflict semantically, not just textually.
2. **Shelve and notify** — Shelve the task, notify operator via Discord with the conflict details. Operator resolves manually or asks agent to retry with updated context.
3. **Agent-driven resolution** — Spawn a new `query()` with the conflict markers, ask the agent to resolve. Risky (agent may resolve incorrectly) but autonomous.
4. **Queue for retry** — Put the task back in the queue with a "needs rebase" flag. Next time it runs, the agent gets fresh trunk context. Simplest but wastes the first attempt's work.

**Recommended for V1:** Option 2 (shelve and notify). Matches the existing escalation pattern. Autonomous resolution (option 3) can be added later as an earned-autonomy feature.

## Related

- [[v5-harness-efficiency-proposal]] — Architecture proposal for TypeScript rewrite
- [[v5-harness-architecture]] — Current Python harness design
- [[v5-harness-known-bugs]] — Bug history (BUG-002, BUG-023, BUG-024 referenced above)
