---
title: v5 Harness Design Decisions
tags: [harness, design, caveman, fifo, concurrency]
category: decision
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Design Decisions

Key design choices made during v5 harness Phase 1 implementation, with rationale.

Resolved decisions archived to [[v5-harness-design-decisions-archive-2026]].

## O_NONBLOCK on FIFO write end

**Decision**: Open FIFO write end with `O_WRONLY | O_NONBLOCK` instead of blocking `O_WRONLY`.

**Why**: A blocking open would stall the entire asyncio event loop when the kernel pipe buffer fills. With `O_NONBLOCK`, a full buffer raises `BlockingIOError` which triggers session restart — a recoverable failure instead of a deadlock. The plan originally specified blocking, but the implementation deliberately diverges.

## Caveman directives on CavemanConfig, not module globals

**Decision**: Store loaded SKILL.md directives on `CavemanConfig.directives` instead of module-level dicts.

**Why**: Module-level `CAVEMAN_DIRECTIVES` in `sessions.py` was copied by reference to `claude.py` via the orchestrator. If `SessionManager` was re-instantiated, the `sessions.py` global pointed to a new dict but `claude.py` still held the old reference. Storing on `CavemanConfig` (which is shared by reference through `ProjectConfig`) means both modules always see the same data.

## pending_mutations for Discord concurrency

**Decision**: Discord commands don't mutate `PipelineState` directly. They append lambdas to a `pending_mutations` list, applied at the top of each poll cycle.

**Why**: The orchestrator is a single asyncio event loop. Discord `on_message` handlers run as separate coroutines. If they mutated state between `await` points in the main loop, state could be inconsistent. The mutation queue serializes all state changes through the main loop's poll cycle.

**Pattern**: Lambdas use default-argument binding (`a=agent, l=level`) to avoid Python's late-binding closure bug where loop variables capture the final iteration's value.

## task_id validation at system boundary

**Decision**: Validate `task_id` format (`[a-zA-Z0-9_\-]+`) in `SignalReader` methods, not at file creation time.

**Why**: Signal files can come from Discord (untrusted) or internal agents (trusted). The `SignalReader` is the system boundary where untrusted data enters path construction. Validating here catches path traversal (`../../etc/passwd`) regardless of signal origin. `write_signal` doesn't validate because it receives data from the orchestrator's own logic.

## Executor is per-task, architect/reviewer are persistent

**Decision**: Executor gets a fresh session per task in a git worktree. Architect and reviewer persist across tasks.

**Why**: The executor needs a clean worktree to avoid cross-contamination between tasks. Its session is launched with `with_cwd(worktree)` and terminated when the task completes. Architect and reviewer don't write files — they only read and reason — so they benefit from accumulated context across tasks.

## Generic harness, project-specific config

**Decision**: The harness lives in the Ozymandias repo but is designed to be project-agnostic. All project-specific content belongs in `config/harness/` (agent roles, project.toml), never in `harness/` Python code.

**Why**: The harness will eventually drive other projects and support Ozymandias-specific tools (Phase 5). Keeping project knowledge in config files means a new project only needs to supply its own `project.toml` and agent role `.md` files. The Phase 1 code violates this in one place — hardcoded test runner (tracked as BUG-009) — scheduled for Phase 2 fix.

**Implication**: When adding harness features, ask: "Would a non-Python, non-trading project need to change harness code to use this?" If yes, parameterize it in `project.toml`.

## Three-stage dev pipeline is stable (not a genericization target)

**Decision**: The architect → executor → reviewer pipeline is the software code review loop. It stays hardcoded. Future agents (ops monitor, analyst, dialogue) are task *sources* feeding into this pipeline, not alternative stages within it.

**Why**: Every workflow considered (ops monitor detects bug, analyst produces insight, human says "do X") enters the pipeline at `classify` — none replace architect/executor/reviewer. Genericizing the stage dispatch adds speculative complexity for a scenario (non-software pipeline) that isn't on the roadmap. BUG-008 downgraded from High to Low/Deferred (Phase 5 nicety).

**Implication**: When adding new agent types, they should create tasks that enter the existing pipeline, not require new pipeline stages.

## Sessions never talk to each other

**Decision**: All inter-session communication goes through the orchestrator. Sessions communicate inward (signal files → orchestrator) and receive outward (orchestrator → FIFO). No session-to-session channel exists or should be created.

**Why**: With N sessions, direct communication creates N-squared routing complexity — every session needs to know about every other session's FIFO, handle failures, and manage ordering. The orchestrator as single hub keeps the coordination problem linear. Each session is independently testable (mock the FIFO in, check the signal out) and independently replaceable.

**Implication**: If a future feature seems to require agent-to-agent communication, route it through the orchestrator via signal files. The orchestrator reads the signal, decides the routing, and writes to the target FIFO. This adds one poll cycle of latency (~5s) but preserves the star topology. At 20 sessions this is the difference between a manageable system and a distributed systems nightmare.

## Latent Issues

Known concerns from the Phase 1 critic review that remain unresolved. Verify or address during Phase 2+.

- **`_apply_reply` re-advance**: `_apply_reply` in `discord_companion.py` re-advances to the current stage via `state.advance(state.stage, state.stage_agent)` — may be intentional for escalation unblocking or a latent bug. Verify during Phase 2 implementation.
- **Signal file cleanup**: `SignalReader.archive()` exists but is never called from the orchestrator. Files accumulate. Not Phase 2 blocking but should be wired in.
- **PipelineState serialization**: Has no `schema_version` — if TaskState extraction happens in Phase 3, a migration strategy is needed.

## Cross-References

- [[v5-harness-design-decisions-archive-2026]] — resolved decisions from Phase 1
- [[v5-harness-architecture]] — module overview and pipeline flow
- [[v5-harness-reviewer-findings]] — security and quality review results
- [[v5-harness-known-bugs]] — 9 deferred bugs for Phase 2+
