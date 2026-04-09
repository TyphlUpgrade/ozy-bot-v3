---
title: v5 Harness Known Bugs
tags: [harness, bugs, tracking]
category: debugging
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Known Bugs

Bugs found during review that are deferred or represent latent risks. Tracked here for future phases.

**8 open bugs tracked** — 15 resolved (see [[v5-harness-known-bugs-archive-2026]])

## Open (Deferred)

### BUG-001: _processed set grows without bound
**Severity**: Low | **File**: `signals.py` | **Phase**: 2
`SignalReader._processed` accumulates every task filename forever. In a weeks-long run, this is unbounded memory growth. Also brittle: if a file is archived and re-created with the same name, it's silently skipped.
**Mitigation**: Remove entries from `_processed` after `archive()`, or switch to mtime-based high-water tracking.

### BUG-002: PipelineState.load drops state on version mismatch
**Severity**: Low | **File**: `pipeline.py` | **Phase**: 2
If a newer harness writes extra fields to `state.json`, an older harness catches `TypeError` and discards the entire state ("starting fresh"). A version downgrade silently loses the active task.
**Mitigation**: Pop unknown keys before `cls(**data)` construction. Keep `TypeError` catch only for genuine type mismatches.

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


## Cross-References

- [[v5-harness-known-bugs-archive-2026]] — resolved bugs archive
- [[v5-harness-reviewer-findings]] — full review history
- [[v5-harness-architecture]] — module overview
