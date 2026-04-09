---
title: v5 Harness Known Bugs
tags: [harness, bugs, tracking]
category: debugging
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Known Bugs

Bugs found during review that are deferred or represent latent risks. Tracked here for future phases.

**9 bugs tracked** — 7 from code review, 2 from genericity audit.

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

### BUG-004: advance() accepts arbitrary stage strings
**Severity**: Low | **File**: `pipeline.py` | **Phase**: 2
No validation on `next_stage` parameter. A typo like `state.advance("reviwer")` puts the pipeline in a stage the match block silently ignores — task hangs forever with no error.
**Mitigation**: Define `VALID_STAGES` frozenset and validate in `advance()`, or use a `StrEnum`.

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

### BUG-008: Hardcoded pipeline stages break genericity
**Severity**: High | **File**: `orchestrator.py`, `pipeline.py` | **Phase**: 2
The stage pipeline (`classify → architect → executor → reviewer → merge → wiki`) is hardcoded in three places: the `match/case` dispatch in `main_loop`, the `next_stages` dict in `check_stage()`, and `_default_agents()`. A project with a different role structure (e.g., `planner → coder → tester → integrator`) cannot use the harness without editing orchestrator code. This violates the design intent of a generic, config-driven pipeline.
**Mitigation**: Define stages as an ordered list in `project.toml` with per-stage transition rules. Replace the match/case with dynamic dispatch from config. Make `_default_agents()` return empty (force explicit config).

### BUG-009: Hardcoded test runner in do_merge
**Severity**: Medium | **File**: `orchestrator.py` | **Phase**: 2
`do_merge()` runs `python3 -m pytest tests/ -x --timeout=120` — hardcoded to pytest, hardcoded directory, hardcoded timeout. Non-Python projects or projects using other test frameworks can't use the merge stage.
**Mitigation**: Add `test_command` to `[pipeline]` in `project.toml`. Use `shlex.split()` to execute the user-defined command. Keep pytest as the default value in config, not in code.

## Resolved

All bugs from Round 1 and Round 2 reviews have been fixed. See [[v5-harness-reviewer-findings]] for the full fix log (10 from Round 1, 13 from Round 2).

## Cross-References

- [[v5-harness-reviewer-findings]] — full review history
- [[v5-harness-architecture]] — module overview
