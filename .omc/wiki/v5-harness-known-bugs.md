---
title: v5 Harness Known Bugs
tags: [harness, bugs, tracking]
category: debugging
created: 2026-04-09
updated: 2026-04-09
---

> **HISTORICAL (2026-04-11)** — Python harness archived. Bugs here are Python-specific.

# v5 Harness Known Bugs (Python — Archived)

Bugs found during review that are deferred or represent latent risks. Tracked here for future phases.

**8 open bugs tracked** — 22 resolved (see [[v5-harness-known-bugs-archive-2026]])

## Open (Deferred)

### BUG-001: _processed set grows without bound
**Severity**: Low | **File**: `signals.py` | **Phase**: 2
`SignalReader._processed` accumulates every task filename forever. In a weeks-long run, this is unbounded memory growth. Also brittle: if a file is archived and re-created with the same name, it's silently skipped.
**Mitigation**: Remove entries from `_processed` after `archive()`, or switch to mtime-based high-water tracking.

### BUG-002: PipelineState.load drops state on version mismatch
**Severity**: Low | **File**: `pipeline.py` | **Phase**: 2
If a newer harness writes extra fields to `state.json`, an older harness catches `TypeError` and discards the entire state ("starting fresh"). A version downgrade silently loses the active task.
**Mitigation**: Pop unknown keys before `cls(**data)` construction. Keep `TypeError` catch only for genuine type mismatches.
**Partial fix (Phase 3)**: `load()` now pops unknown keys before `cls(**data)` construction. Version downgrades no longer crash — they silently drop unknown fields. Full fix (version negotiation) remains deferred.

### BUG-003: Frontmatter stripping is fragile
**Severity**: Low | **File**: `sessions.py` | **Phase**: 2
`split("---", 2)` works for well-formed YAML frontmatter but breaks if the closing `---` is missing (uses entire file including the `---` line as template). Body `---` markers (markdown horizontal rules) work only by coincidence of the maxsplit limit.
**Mitigation**: Use regex: `re.sub(r'\A---\n.*?\n---\n', '', template, count=1, flags=re.DOTALL)`.

### BUG-005: write_signal filename not path-traversal-validated
**Severity**: Low | **File**: `signals.py` | **Phase**: 2
`_safe_task_id` guards all `SignalReader` methods, but `write_signal` accepts a raw `filename`. A caller passing `"../../evil.json"` could write outside the signal directory. Currently all callers are internal and safe.
**Mitigation**: Validate that `filename` contains no path separators.

### BUG-006: FIFO open race window
**Severity**: Low | **File**: `sessions.py` | **Phase**: 2
0.5s sleep between tmux launch and `os.open(O_WRONLY | O_NONBLOCK)` is a best-effort guess. If tmux is slow, `os.open` raises `OSError [ENXIO]` (no reader). No retry logic.
**Mitigation**: Retry `os.open` in a loop with exponential backoff (0.25s, 0.5s, 1s, 2s), catching ENXIO.

### BUG-007: restart() bypasses clawhip for teardown
**Severity**: Info | **File**: `sessions.py` | **Phase**: 2
`restart()` calls raw `tmux kill-session` but `launch()` uses `clawhip tmux new`. If clawhip tracks session state internally, bypassing it for teardown may leave stale metadata.
**Mitigation**: Use `clawhip tmux kill` if available.

### BUG-008: Hardcoded pipeline stages (downgraded)
**Severity**: Low | **File**: `orchestrator.py`, `pipeline.py` | **Phase**: 5
The stage pipeline (`classify → architect → executor → reviewer → merge → wiki`) is hardcoded. Per architect/critic consensus: the three-stage dev pipeline is the stable code review loop, not an arbitrary choice. Future agents (ops monitor, analyst) are task *sources* feeding into this pipeline, not alternative stages. Genericizing is a Phase 5 nicety, not a structural flaw.
**Mitigation**: Extract stage transition graph to a module-level constant in Phase 5 if configurable pipelines are needed.


### BUG-012: AsyncMock/MagicMock GC warning — unawaited coroutines in tests
**Severity**: Info | **File**: `tests/test_orchestrator.py` | **Phase**: 2
Test helpers create `AsyncMock` attributes on `MagicMock` bases; GC emits `RuntimeWarning: coroutine ... was never awaited`. Non-deterministic and cosmetic — mutation testing (6 mutations, all caught) confirms tests are sound. Partial fix removed two unused `AsyncMock()` attrs; 1 residual warning remains.
**Full fix**: Convert helpers to `spec`-based mocks (`MagicMock(spec=SessionManager)`). Disproportionate refactor for current test count.

## Phase 2 Post-Implementation Review (2026-04-09)

Found by architect + critic + code-reviewer agents after Phase 2 escalation implementation.

### ~~BUG-022: restart() re-launches session on stage timeout — wasteful~~ RESOLVED
**Severity**: Medium | **File**: `orchestrator.py`, `sessions.py` | **Phase**: 3
**Fix**: Extracted `kill()` method on `SessionManager` — teardown without relaunch. `restart()` now calls `kill()` + `launch()`. Stage timeout path uses `kill()` instead of `restart()`.

## Phase 3 Pre-Implementation Bug Fixes (2026-04-09)

Found by code-reviewer, debugger, critic, and architect agents during Phase 3 preparation. All 6 fixed and tested (256 tests passing).

### ~~BUG-023: FD leak on session overwrite~~ RESOLVED
**Severity**: Medium | **File**: `sessions.py:134-143` | **Phase**: 3
`launch()` overwrote `sessions[name]` dict entry without closing old fd, orphaning the file descriptor. On repeated `restart()` calls, leaked fds accumulate.
**Fix**: Auto-close existing session (fd + FIFO) before creating new one in `launch()`.

### ~~BUG-024: Stale stage signal causes spurious advancement~~ RESOLVED
**Severity**: High | **File**: `signals.py:137-152`, `orchestrator.py` | **Phase**: 3
When a task enters escalation, the stage completion signal (e.g. `completion-{task_id}.json`) persists. After shelve→unshelve→resume, the orchestrator re-reads the stale signal and spuriously advances the pipeline.
**Fix**: Added `clear_stage_signal()` to `SignalReader`. Orchestrator calls it on escalation entry to remove stale signals.

### ~~BUG-025: reconcile() ignores shelved tasks~~ RESOLVED
**Severity**: Medium | **File**: `lifecycle.py:104-117` | **Phase**: 3
Crash recovery (`reconcile()`) only checked active task state. Shelved tasks stuck in `escalation_wait` or `escalation_tier1` were silently ignored — no re-notification to Discord.
**Fix**: Added shelved task iteration in `reconcile()`. Both `escalation_wait` and `escalation_tier1` are handled. Tier1 with missing signal promotes to `escalation_wait`.

### ~~BUG-026: Lost operator reply for shelved tasks~~ RESOLVED
**Severity**: High | **File**: `discord_companion.py`, `orchestrator.py` | **Phase**: 3
`_apply_reply` only checked active task. If an operator replied to a shelved task's escalation, the reply was silently dropped.
**Fix**: Rewrote `_apply_reply` to search shelved tasks. Resolves escalation in-place, stores `pending_operator_reply` on the shelved dict. Reply injected when task is unshelved in `do_wiki()`.

### ~~BUG-027: do_wiki reply injection timing concern~~ RESOLVED (documented)
**Severity**: Low | **File**: `orchestrator.py` | **Phase**: 3
After unshelving a task with `pending_operator_reply`, the executor session may belong to the just-completed task, not the unshelved one. Reply goes to correct agent name but stale context.
**Fix**: Added warning log when session unavailable. Documented timing concern with inline comment. Not a crash path — edge case with graceful degradation.

### ~~PERF-1: parse_token_usage O(n) re-read~~ MOVED
**Severity**: Low (Performance) | **File**: `sessions.py:266-296` | **Phase**: 3
Moved to [[v5-harness-open-concerns]] — this is a performance concern, not a bug.

## Cross-References

- [[v5-harness-known-bugs-archive-2026]] — resolved bugs archive
- [[v5-harness-reviewer-findings]] — full review history
- [[v5-harness-architecture]] — module overview
