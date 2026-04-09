---
title: v5 Harness Known Bugs — Resolved (2026)
tags: [harness, bugs, archive]
category: reference
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Known Bugs — Resolved (2026)

Archive of resolved bugs from v5-harness-known-bugs.md. All entries below were confirmed fixed.

## Resolved Bugs

### ~~BUG-004: advance() accepts arbitrary stage strings~~ RESOLVED
**Severity**: Low | **File**: `pipeline.py` | **Phase**: 2
**Fix**: Added `VALID_STAGES` frozenset. `advance()` now raises `ValueError` on invalid stage strings. 7 tests added covering validation, all valid stages, and escalation timing.

### ~~BUG-010: Worktree cwd never propagated to session~~ RESOLVED
**Severity**: High | **File**: `sessions.py` | **Phase**: 2
**Fix**: `launch()` now prepends `cd {shlex.quote(cwd)} &&` when `agent_def.cwd` is set. `AgentDef` gained `cwd: Path | None` field, stored via `with_cwd()`. All shell interpolations use `shlex.quote()` to prevent injection.

### ~~BUG-013: Fire-and-forget git subprocesses in do_merge~~ RESOLVED
**Severity**: High | **File**: `orchestrator.py:105,120,127` | **Phase**: 2
**Fix**: All `git merge --abort` and `git revert` subprocesses now capture proc and `await proc.communicate()` before proceeding. Applied in fix-now batch.

### ~~BUG-014: TOCTOU on escalation file re-read during tier promotion~~ RESOLVED
**Severity**: High | **File**: `orchestrator.py:218` | **Phase**: 2
**Fix**: Added module-level `_escalation_cache` dict in orchestrator.py. `check_for_escalation` stashes the `EscalationRequest` when routing to tier1; `handle_escalation_tier1` pops from cache (with disk fallback). Cache cleaned on both promote and resolve paths. Applied in fix-soon batch.

### ~~BUG-018: Resume-state boilerplate triplicated~~ RESOLVED
**Severity**: Medium | **File**: `orchestrator.py:238-241,266-271`, `discord_companion.py:181-191` | **Phase**: 2
**Fix**: Extracted `resume_from_escalation()` method on `PipelineState`. All 3 call sites (orchestrator handle_escalation_tier1, handle_escalation_wait, discord_companion _apply_reply) now use the single method. Applied in fix-now batch.

### ~~BUG-020: signal_reader defaults to None on DiscordCompanion~~ RESOLVED
**Severity**: Low | **File**: `discord_companion.py:67-69` | **Phase**: 2
**Fix**: Made `signal_reader` a required parameter (removed `None` default). Applied in fix-soon batch.

### ~~BUG-021: Dead guard clause and lambda naming in discord_companion~~ RESOLVED
**Severity**: Low | **File**: `discord_companion.py:149,154` | **Phase**: 2
**Fix**: Removed dead guard clause, renamed `l=level` to `lvl=level`. Applied in fix-now batch.

### ~~BUG-015: Pipeline stuck on deleted escalation signal during escalation_wait~~ RESOLVED
**Severity**: High | **File**: `orchestrator.py` | **Phase**: 2
**Fix**: `handle_escalation_wait` now checks `escalation_started_ts` age when `esc is None`. If exceeds `2 * escalation_timeout`, force-resumes with warning log and `escalation_force_resumed` event. Also logs warning when `started_ts` is None (prevents silent stall).

### ~~BUG-016: Crash recovery doesn't handle escalation_tier1 stage~~ RESOLVED
**Severity**: Medium | **File**: `lifecycle.py` | **Phase**: 2
**Fix**: `reconcile()` now handles `escalation_tier1` stage. Re-sends escalation to architect if signal exists; promotes to `escalation_wait` (Tier 2) if no signal found.

### ~~BUG-017: No timeout on Tier 1 architect wait~~ RESOLVED
**Severity**: Medium | **File**: `orchestrator.py`, `pipeline.py` | **Phase**: 2
**Fix**: `handle_escalation_tier1` checks `escalation_started_ts` against `config.tier1_timeout` (default 1800s). If exceeded, auto-promotes to Tier 2 with `escalation_promoted` event. `tier1_timeout` added as a first-class field on `ProjectConfig` (loaded from `pipeline.tier1_timeout` in TOML). Also logs warning when `started_ts` is None.

### ~~BUG-009: Hardcoded test runner in do_merge~~ RESOLVED
**Severity**: Medium | **File**: `orchestrator.py`, `pipeline.py` | **Phase**: 2
**Fix**: Added `test_command` field to `ProjectConfig` (default `"python3 -m pytest tests/ -x"`), loaded from `pipeline.test_command` in TOML. `do_merge()` uses `shlex.split(config.test_command)` instead of hardcoded args. Empty test_command guard skips tests with warning.

### ~~BUG-011: No wall-clock stage timeout~~ RESOLVED
**Severity**: Medium | **File**: `orchestrator.py`, `pipeline.py` | **Phase**: 2
**Fix**: Added `stage_started_ts` to `PipelineState` (set on `activate()` and `advance()`, cleared in `clear_active()`). Added `max_stage_minutes` config dict with per-stage defaults (classify=10, architect=60, executor=120, reviewer=60, merge=15, wiki=15). `_check_stage_timeout()` in main loop kills session and clears task on exceed. Escalation stages excluded.

### ~~BUG-019: should_renotify window coupled to poll_interval~~ RESOLVED
**Severity**: Low | **File**: `escalation.py`, `pipeline.py` | **Phase**: 2
**Fix**: Replaced `int(elapsed) % interval_seconds < 10` modulo trick with explicit `last_renotify_ts` field on `PipelineState`. `should_renotify()` now takes `last_renotify_ts` parameter — returns True when None (first renotify after one interval) or when elapsed since last renotify >= interval.

## Phase 2 Prereq Batch (2026-04-09)

Fixes applied before Phase 3 entry. Found via Phase 3 readiness assessment.

| Bug | Severity | Fix |
|-----|----------|-----|
| BUG-009 | Medium | `test_command` config field, `shlex.split()` in `do_merge()` |
| BUG-011 | Medium | `stage_started_ts` + `max_stage_minutes` + `_check_stage_timeout()` in main loop |
| BUG-019 | Low | `last_renotify_ts` replaces modulo-based renotify window |

**Tests added**: 8 tests (5 stage timeout orchestrator + 3 pipeline), 3 renotify tests, 9 P1 edge case tests (signals, escalation, discord).

## Phase 2 Stall Triad Batch (2026-04-09)

Three interconnected bugs (BUG-015/016/017) that compound into permanent pipeline hangs when escalation state is lost or architect is unresponsive.

| Bug | Severity | Fix |
|-----|----------|-----|
| BUG-015 | High | Timeout-based force-resume when escalation signal missing in `handle_escalation_wait` |
| BUG-016 | Medium | `escalation_tier1` crash recovery case in `lifecycle.reconcile()` |
| BUG-017 | Medium | `tier1_timeout` config field + auto-promote to Tier 2 in `handle_escalation_tier1` |

**Additional fixes in batch**: `_escalation_cache` leak (5 `clear_active` sites now pop from cache), `_apply_reply` signature hardened (signal_reader required), warning logs for missing `started_ts` in both tier1 and wait handlers.

**Tests added**: 8 new tests (2 for BUG-015 force-resume, 2 for BUG-017 tier1 timeout, 2 for cache cleanup, 2 for started_ts=None edge case). 3 lifecycle tests for BUG-016 reconcile. 5 existing `_apply_reply` tests updated.

## Phase 2 Fix-Now + Fix-Soon Batch (2026-04-09)

Fixes applied after Phase 2 post-implementation review.

| Bug | Severity | Fix |
|-----|----------|-----|
| BUG-013 | High | `await proc.communicate()` on all git subprocesses in `do_merge()` |
| BUG-014 | High | Module-level `_escalation_cache` dict avoids TOCTOU re-read in `handle_escalation_tier1` |
| BUG-018 | Medium | `resume_from_escalation()` method on `PipelineState`, replaced 3 copy-paste sites |
| BUG-020 | Low | `signal_reader` now required on `DiscordCompanion` constructor |
| BUG-021 | Low | Dead guard removed, lambda `l=level` renamed to `lvl=level` |

**Tests added**: 3 tests for `handle_escalation_wait` (no-op, advisory auto-proceed, blocking re-notify), 1 test for `_apply_reply` with `signal_reader` (asserts `clear_escalation` called).

## Phase 2 Prerequisite Batch (2026-04-08)

Fixes applied before Phase 2 implementation. Found via autopilot validation (code-reviewer + security-reviewer).

| Bug | Severity | Fix |
|-----|----------|-----|
| BUG-004 | Low | `VALID_STAGES` frozenset + `ValueError` in `advance()` |
| BUG-010 | High | `cd shlex.quote(cwd)` prefix in `launch()`, `cwd` field on `AgentDef` |
| Shell injection in `launch()` | Critical | `shlex.quote()` on all interpolated shell values (binary, cwd, fifo, log, model) |
| Missing `_safe_task_id` in `next_task()` | High | Added validation after `TaskSignal` construction, `ValueError` in except clause |
| `activate()` not clearing `escalation_started_ts` | High | Added `self.escalation_started_ts = None` to `activate()` |
| Missing event emissions | Medium | Added `EventLog` parameter + emissions to `classify_task` and `check_reviewer` |

## Additional Improvements in Phase 2 Prerequisite Batch

- **EventLog** (`harness/lib/events.py`): JSONL append-only audit log with `record(event_type, data)`. 5 tests.
- **`escalation_started_ts`**: Set-once on escalation entry, preserved across tiers, cleared on exit. 5 tests + roundtrip.
- **Configurable CLI binary**: `claude_binary` in `ProjectConfig`, loaded from `[pipeline]` in toml.
- **`Session.role`**: Tracks agent archetype for health check routing.
- **Message prefix conventions**: Documented `[TASK]`, `[RETRY]`, `[OPERATOR]`, `[SYSTEM]`, `[REINIT]` prefixes.
- **Priority sort TODO**: Comment in `next_task()` documenting Phase 3 gap (FIFO by mtime is current behavior).

## Cross-References

- [[v5-harness-known-bugs]] — open bugs tracker
- [[v5-harness-reviewer-findings]] — full review history (10 from Round 1, 13 from Round 2)
- [[v5-harness-architecture]] — module overview
