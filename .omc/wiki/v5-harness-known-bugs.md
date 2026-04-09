---
title: v5 Harness Known Bugs
tags: [harness, bugs, tracking]
category: debugging
created: 2026-04-09
updated: 2026-04-08
---

# v5 Harness Known Bugs

Bugs found during review that are deferred or represent latent risks. Tracked here for future phases.

**21 bugs tracked** — 7 from code review, 2 from genericity audit, 2 from nested execution review, 1 from AsyncMock investigation, 9 from Phase 2 post-implementation review (architect + critic + code-reviewer). BUG-008 downgraded after architect/critic consensus. **4 resolved** in Phase 2 prerequisite batch (BUG-004, BUG-010, plus 2 validation-discovered issues). **5 resolved** in Phase 2 fix-now + fix-soon batch (BUG-013, BUG-014, BUG-018, BUG-020, BUG-021). **4 new tests** added (3 for handle_escalation_wait, 1 for _apply_reply with signal_reader).

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

### ~~BUG-004: advance() accepts arbitrary stage strings~~ RESOLVED
**Severity**: Low | **File**: `pipeline.py` | **Phase**: 2
**Fix**: Added `VALID_STAGES` frozenset. `advance()` now raises `ValueError` on invalid stage strings. 7 tests added covering validation, all valid stages, and escalation timing.

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

### BUG-009: Hardcoded test runner in do_merge
**Severity**: Medium | **File**: `orchestrator.py` | **Phase**: 2
`do_merge()` runs `python3 -m pytest tests/ -x --timeout=120` — hardcoded to pytest, hardcoded directory, hardcoded timeout. Non-Python projects or projects using other test frameworks can't use the merge stage.
**Mitigation**: Add `test_command` to `[pipeline]` in `project.toml`. Use `shlex.split()` to execute the user-defined command. Keep pytest as the default value in config, not in code.

### ~~BUG-010: Worktree cwd never propagated to session~~ RESOLVED
**Severity**: High | **File**: `sessions.py` | **Phase**: 2
**Fix**: `launch()` now prepends `cd {shlex.quote(cwd)} &&` when `agent_def.cwd` is set. `AgentDef` gained `cwd: Path | None` field, stored via `with_cwd()`. All shell interpolations use `shlex.quote()` to prevent injection.

### BUG-012: AsyncMock/MagicMock GC warning — unawaited coroutines in tests
**Severity**: Info | **File**: `tests/test_orchestrator.py` | **Phase**: 2
Test helpers (`_make_proc`, `_make_signal_reader`, `_make_session_mgr`) create `AsyncMock` attributes on `MagicMock` bases. When tests don't call every async attribute, GC finalization emits `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited`. Warning is non-deterministic (jumps between tests each run) and cosmetic — **mutation testing (6 mutations, all caught) confirms tests are sound**.
**Partial fix applied**: Removed unused `wait=AsyncMock()` from `_make_proc()` and `check_stage_complete=AsyncMock()` from `_make_signal_reader()`. 1 residual warning remains from Python stdlib `MagicMock`/`AsyncMock` auto-attribute interaction.
**When to worry**: If you see a test pass that SHOULD fail (e.g., after deliberately breaking `should_promote` logic), that's real sleepwalking. The GC warning alone is not it — re-run mutation tests to verify.
**Full fix**: Convert helpers to `spec`-based mocks (`MagicMock(spec=SessionManager)`) to prevent auto-attribute generation. Disproportionate refactor for current test count.

## Phase 2 Post-Implementation Review (2026-04-09)

Found by architect + critic + code-reviewer agents after Phase 2 escalation implementation.

### ~~BUG-013: Fire-and-forget git subprocesses in do_merge~~ RESOLVED
**Severity**: High | **File**: `orchestrator.py:105,120,127` | **Phase**: 2
**Fix**: All `git merge --abort` and `git revert` subprocesses now capture proc and `await proc.communicate()` before proceeding. Applied in fix-now batch.

### ~~BUG-014: TOCTOU on escalation file re-read during tier promotion~~ RESOLVED
**Severity**: High | **File**: `orchestrator.py:218` | **Phase**: 2
**Fix**: Added module-level `_escalation_cache` dict in orchestrator.py. `check_for_escalation` stashes the `EscalationRequest` when routing to tier1; `handle_escalation_tier1` pops from cache (with disk fallback). Cache cleaned on both promote and resolve paths. Applied in fix-soon batch.

### BUG-015: Pipeline stuck on deleted escalation signal during escalation_wait
**Severity**: High | **File**: `orchestrator.py:258-260` | **Phase**: 2
`handle_escalation_wait` returns silently when `read_escalation` returns None. If the signal file is corrupted or deleted externally, no timeout fires, no re-notify fires, `_apply_reply` is the only exit. Pipeline permanently stalled.
**Fix**: When `esc is None`, check `escalation_started_ts` age. If exceeds `2 * escalation_timeout`, force-resume with warning. **Track for future phase.**

### BUG-016: Crash recovery doesn't handle escalation_tier1 stage
**Severity**: Medium | **File**: `lifecycle.py:64-86` | **Phase**: 2
`lifecycle.reconcile()` only checks for `escalation_wait` during crash recovery. Crash during `escalation_tier1` falls through to generic handler — architect session restarts with no escalation context, pipeline hangs forever.
**Fix**: Add `escalation_tier1` case to reconcile — re-send escalation question to architect, or fall back to promote to Tier 2. **Track for future phase.**

### BUG-017: No timeout on Tier 1 architect wait
**Severity**: Medium | **File**: `orchestrator.py:208-247` | **Phase**: 2
`handle_escalation_tier1` polls for `ArchitectResolution` indefinitely. No auto-promote to Tier 2 after the architect fails to respond. Combined with BUG-016 (crash during tier1), creates permanent stall.
**Fix**: Check `escalation_started_ts` against configurable `tier1_timeout` (suggest 30min). If exceeded, auto-promote to Tier 2. **Track for future phase.**

### ~~BUG-018: Resume-state boilerplate triplicated~~ RESOLVED
**Severity**: Medium | **File**: `orchestrator.py:238-241,266-271`, `discord_companion.py:181-191` | **Phase**: 2
**Fix**: Extracted `resume_from_escalation()` method on `PipelineState`. All 3 call sites (orchestrator handle_escalation_tier1, handle_escalation_wait, discord_companion _apply_reply) now use the single method. Applied in fix-now batch.

### BUG-019: should_renotify window coupled to poll_interval
**Severity**: Low | **File**: `escalation.py:114-116` | **Phase**: 2
`int(elapsed) % interval_seconds < 10` assumes poll loop hits the 10-second window at least once. Breaks at `poll_interval > 10s`. Default config is safe but non-obvious footgun.
**Fix**: Replace with `last_renotify_ts` field on PipelineState. **Track for future phase.**

### ~~BUG-020: signal_reader defaults to None on DiscordCompanion~~ RESOLVED
**Severity**: Low | **File**: `discord_companion.py:67-69` | **Phase**: 2
**Fix**: Made `signal_reader` a required parameter (removed `None` default). Applied in fix-soon batch.

### ~~BUG-021: Dead guard clause and lambda naming in discord_companion~~ RESOLVED
**Severity**: Low | **File**: `discord_companion.py:149,154` | **Phase**: 2
**Fix**: Removed dead guard clause, renamed `l=level` to `lvl=level`. Applied in fix-now batch.

### BUG-011: No wall-clock stage timeout — orchestrator waits forever
**Severity**: Medium | **File**: `orchestrator.py` | **Phase**: 2
The orchestrator polls `check_stage_complete()` every `poll_interval` but has no maximum wait for any stage. The only timeout is clawhip's `stale_minutes` (output-based). A session that produces steady output but never completes will run indefinitely. At scale (many sessions, internal teams), this becomes an unbounded cost risk.
**Mitigation**: Add `max_stage_minutes` per stage in `project.toml`. Track `stage_started_ts` on PipelineState. If exceeded, kill the session and fail the task.

## Resolved

All bugs from Round 1 and Round 2 reviews have been fixed. See [[v5-harness-reviewer-findings]] for the full fix log (10 from Round 1, 13 from Round 2).

### Phase 2 Fix-Now + Fix-Soon Batch (2026-04-09)

Fixes applied after Phase 2 post-implementation review.

| Bug | Severity | Fix |
|-----|----------|-----|
| BUG-013 | High | `await proc.communicate()` on all git subprocesses in `do_merge()` |
| BUG-014 | High | Module-level `_escalation_cache` dict avoids TOCTOU re-read in `handle_escalation_tier1` |
| BUG-018 | Medium | `resume_from_escalation()` method on `PipelineState`, replaced 3 copy-paste sites |
| BUG-020 | Low | `signal_reader` now required on `DiscordCompanion` constructor |
| BUG-021 | Low | Dead guard removed, lambda `l=level` renamed to `lvl=level` |

**Tests added**: 3 tests for `handle_escalation_wait` (no-op, advisory auto-proceed, blocking re-notify), 1 test for `_apply_reply` with `signal_reader` (asserts `clear_escalation` called).

### Phase 2 Prerequisite Batch (2026-04-08)

Fixes applied before Phase 2 implementation. Found via autopilot validation (code-reviewer + security-reviewer).

| Bug | Severity | Fix |
|-----|----------|-----|
| BUG-004 | Low | `VALID_STAGES` frozenset + `ValueError` in `advance()` |
| BUG-010 | High | `cd shlex.quote(cwd)` prefix in `launch()`, `cwd` field on `AgentDef` |
| Shell injection in `launch()` | Critical | `shlex.quote()` on all interpolated shell values (binary, cwd, fifo, log, model) |
| Missing `_safe_task_id` in `next_task()` | High | Added validation after `TaskSignal` construction, `ValueError` in except clause |
| `activate()` not clearing `escalation_started_ts` | High | Added `self.escalation_started_ts = None` to `activate()` |
| Missing event emissions | Medium | Added `EventLog` parameter + emissions to `classify_task` and `check_reviewer` |

### Additional improvements in this batch

- **EventLog** (`harness/lib/events.py`): JSONL append-only audit log with `record(event_type, data)`. 5 tests.
- **`escalation_started_ts`**: Set-once on escalation entry, preserved across tiers, cleared on exit. 5 tests + roundtrip.
- **Configurable CLI binary**: `claude_binary` in `ProjectConfig`, loaded from `[pipeline]` in toml.
- **`Session.role`**: Tracks agent archetype for health check routing.
- **Message prefix conventions**: Documented `[TASK]`, `[RETRY]`, `[OPERATOR]`, `[SYSTEM]`, `[REINIT]` prefixes.
- **Priority sort TODO**: Comment in `next_task()` documenting Phase 3 gap (FIFO by mtime is current behavior).

## Cross-References

- [[v5-harness-reviewer-findings]] — full review history
- [[v5-harness-architecture]] — module overview
