---
title: v5 Harness Design Decisions
tags: [harness, design, caveman, fifo, concurrency]
category: decision
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Design Decisions

Key design choices made during v5 harness Phase 1 implementation, with rationale.

## O_NONBLOCK on FIFO write end

**Decision**: Open FIFO write end with `O_WRONLY | O_NONBLOCK` instead of blocking `O_WRONLY`.

**Why**: A blocking open would stall the entire asyncio event loop when the kernel pipe buffer fills. With `O_NONBLOCK`, a full buffer raises `BlockingIOError` which triggers session restart — a recoverable failure instead of a deadlock. The plan originally specified blocking, but the implementation deliberately diverges.

## Caveman directives on CavemanConfig, not module globals

**Decision**: Store loaded SKILL.md directives on `CavemanConfig.directives` instead of module-level dicts.

**Why**: Module-level `CAVEMAN_DIRECTIVES` in `sessions.py` was copied by reference to `claude.py` via the orchestrator. If `SessionManager` was re-instantiated, the `sessions.py` global pointed to a new dict but `claude.py` still held the old reference. Storing on `CavemanConfig` (which is shared by reference through `ProjectConfig`) means both modules always see the same data.

## Per-level directive construction (append, not template)

**Decision**: Build per-level directives by appending `**Active level: {level}.**` to the full SKILL.md, rather than using `template.replace("CAVEMAN_LEVEL", level)`.

**Why**: SKILL.md contains all levels inline — there's no single `CAVEMAN_LEVEL` placeholder. Appending an activation line is simpler and works regardless of SKILL.md internal structure. The plan's template approach assumed a placeholder that doesn't exist in the actual file.

## pending_mutations for Discord concurrency

**Decision**: Discord commands don't mutate `PipelineState` directly. They append lambdas to a `pending_mutations` list, applied at the top of each poll cycle.

**Why**: The orchestrator is a single asyncio event loop. Discord `on_message` handlers run as separate coroutines. If they mutated state between `await` points in the main loop, state could be inconsistent. The mutation queue serializes all state changes through the main loop's poll cycle.

**Pattern**: Lambdas use default-argument binding (`a=agent, l=level`) to avoid Python's late-binding closure bug where loop variables capture the final iteration's value.

## task_id validation at system boundary

**Decision**: Validate `task_id` format (`[a-zA-Z0-9_\-]+`) in `SignalReader` methods, not at file creation time.

**Why**: Signal files can come from Discord (untrusted) or internal agents (trusted). The `SignalReader` is the system boundary where untrusted data enters path construction. Validating here catches path traversal (`../../etc/passwd`) regardless of signal origin. `write_signal` doesn't validate because it receives data from the orchestrator's own logic.

## FIFO permissions 0o600

**Decision**: `os.mkfifo(path, mode=0o600)` — owner-only read/write.

**Why**: FIFOs live in `/tmp/harness-sessions`, a shared directory. Default umask (0o022) would create 0o644 FIFOs, allowing any local user to write to them — injecting arbitrary messages into Claude sessions. 0o600 restricts to the owning user.

## Executor is per-task, architect/reviewer are persistent

**Decision**: Executor gets a fresh session per task in a git worktree. Architect and reviewer persist across tasks.

**Why**: The executor needs a clean worktree to avoid cross-contamination between tasks. Its session is launched with `with_cwd(worktree)` and terminated when the task completes. Architect and reviewer don't write files — they only read and reason — so they benefit from accumulated context across tasks.

## Phase 2/3 types pulled forward

**Decision**: Implemented `EscalationRequest`, `ArchitectResolution`, `EscalationReply` dataclasses and `reformulate()`/`document_task()` functions even though they're Phase 2/3 scope.

**Why**: The dataclass schemas cost nothing to define early and establishing them now means Phase 2 doesn't need to modify `signals.py`. Similarly, `claude.py` functions are self-contained — implementing them early exercises the subprocess wrapper pattern without adding risk.

## Cross-References

- [[v5-harness-architecture]] — module overview and pipeline flow
- [[v5-harness-reviewer-findings]] — security and quality review results
