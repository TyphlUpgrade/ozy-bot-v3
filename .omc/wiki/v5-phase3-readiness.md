---
title: v5 Phase 3 Readiness Assessment
tags: [harness, phase3, audit, readiness]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# v5 Phase 3 Readiness Assessment

Pre-Phase 3 due diligence findings. All critical blockers identified. Phase 3 entry approved pending stall-triad resolution.

---

## Phase 3 Scope

Intelligence + Disputes resolution. Delivers Claude-driven dispute mitigation and context preservation across stage boundaries.

| Feature | Module(s) | Status |
|---------|-----------|--------|
| `claude.reformulate()` | `claude.py` | Planned — reviewer→executor dispute resolution |
| `claude.summarize()` | `claude.py` | Planned — context transfer between stages |
| Session rotation | `sessions.py`, `orchestrator.py` | Planned — token usage tracking, restart on threshold |
| Pipeline-frozen mitigation | `orchestrator.py` | Planned — shelve blocked task, process next (queue) |

---

## Blockers: The Stall Triad

**Three bugs that compound into permanent pipeline hangs. MUST fix before Phase 3.**

### BUG-015: Deleted Escalation Signal → Silent Forever

| Attribute | Value |
|-----------|-------|
| **Severity** | High |
| **File** | `orchestrator.py:258-260` |
| **Phase** | 2 (deferred) |
| **Risk** | CRITICAL — pipeline permanently stalled on external file deletion |

`handle_escalation_wait` returns silently when `read_escalation` returns `None`. If the signal file is corrupted or deleted externally (operator accident, disk error), no timeout fires, no re-notify fires. Only `_apply_reply` can unblock it. Pipeline stalled forever.

**Fix**: When `esc is None`, check `escalation_started_ts` age. If exceeds `2 * escalation_timeout`, force-resume with warning.

**Effort**: ~10 lines. **Priority**: Fix before Phase 3.

---

### BUG-016: Crash During Tier1 → No Recovery Case

| Attribute | Value |
|-----------|-------|
| **Severity** | Medium |
| **File** | `lifecycle.py:64-86` |
| **Phase** | 2 (deferred) |
| **Risk** | HIGH — crash recovery incomplete for escalation_tier1 stage |

`lifecycle.reconcile()` only checks for `escalation_wait` during crash recovery. If a crash happens while in `escalation_tier1` stage, the recovery handler falls through to generic case — architect session restarts with no escalation context, pipeline hangs forever.

**Fix**: Add `escalation_tier1` case to reconcile — re-send escalation question to architect, or fall back to promote to Tier 2.

**Effort**: ~15 lines. **Priority**: Fix before Phase 3.

---

### BUG-017: No Timeout on Tier 1 Architect Wait

| Attribute | Value |
|-----------|-------|
| **Severity** | Medium |
| **File** | `orchestrator.py:208-247` |
| **Phase** | 2 (deferred) |
| **Risk** | HIGH — combined with BUG-016, creates permanent stall |

`handle_escalation_tier1` polls for `ArchitectResolution` indefinitely. No auto-promote to Tier 2 after architect fails to respond. Combined with BUG-016 (crash during tier1), creates permanent stall condition.

**Fix**: Check `escalation_started_ts` against configurable `tier1_timeout` (suggest 30min). If exceeded, auto-promote to Tier 2.

**Effort**: ~20 lines. **Priority**: Fix before Phase 3.

---

## Code Review Findings

All 5 fixes from Phase 2 fix-now batch validated correct. 3 residual quality issues identified:

### Cache Leak: `_escalation_cache` on Task Abandonment

| Attribute | Value |
|-----------|-------|
| **Severity** | Medium |
| **File** | `orchestrator.py` |
| **Finding** | `_escalation_cache` leaks if task abandoned via `clear_active()` without escalation resolution |

`clear_active()` clears `active_task` but does not purge stale `_escalation_cache` entries. If a task is deleted externally or operator cancels mid-escalation, the cache retains the old entry. Next task with same ID (unlikely but possible after task numbering wraps) gets the stale context.

**Mitigation**: Low risk (cache key is `task_id`, high uniqueness), but should add `_escalation_cache.pop(task_id, None)` to `clear_active()`.

**Effort**: 1 line. **Priority**: Phase 3 improvement.

---

### Signal Reader Accepts `None` in Signature

| Attribute | Value |
|-----------|-------|
| **Severity** | Low |
| **File** | `discord_companion.py` |
| **Finding** | BUG-020 (resolved) but `_apply_reply` signature still has default `signal_reader=None` in type hints |

BUG-020 was fixed by making `signal_reader` required in the constructor. However, `_apply_reply(signal_reader=None)` method signature still accepts `None`, which is inconsistent — implementation crashes if called without it.

**Mitigation**: Update type hint to `signal_reader: SignalReader` (remove `None` and default value).

**Effort**: 1 line. **Priority**: Phase 3 cleanup.

---

### Missing Test: Cache Empty After Resolution

| Attribute | Value |
|-----------|-------|
| **Severity** | Low |
| **File** | `tests/test_orchestrator.py` |
| **Finding** | No test asserts `_escalation_cache` is cleaned after escalation resolution |

Existing 4 tests for escalation (handle_escalation_wait, _apply_reply) verify the happy path but do not assert the cache was properly cleaned. If a future PR accidentally breaks cache cleanup, tests won't catch it.

**Mitigation**: Add test: escalation → resolve → assert cache empty.

**Effort**: ~15 lines. **Priority**: Phase 3 test coverage.

---

## Test Coverage Gaps

### P0: DiscordCompanion Zero Coverage

| Gap | Status | Impact |
|-----|--------|--------|
| `DiscordCompanion.handle_message()` | Zero tests | All Discord command entry points untested |
| `_handle_caveman()` dispatch | Zero tests | Caveman parsing + command router untested |

These are the only paths that ingest Discord user input. No tests means message parsing bugs, command dispatch errors, and permission escapes are undetected.

**Recommended Tests**: 
- `handle_message` with valid `!task`, `!tell`, `!reply`, `!status` commands
- Parse errors (malformed JSON, missing fields)
- Permission checks (operator-only commands)
- `_handle_caveman` mode dispatch

**Effort**: ~80 lines of test code. **Priority**: Fix before Phase 3.

---

### P1: Integration and Edge Cases

| Gap | Status | Impact |
|-----|--------|--------|
| Main loop integration | No test | Full orchestrator → session → Claude flow untested |
| Malformed JSON escalation readers | No tests | BUG-012 residual: escalation JSON parse failures uncovered |
| `should_renotify` boundary | No tests | Edge case: 4h boundary, interval wraps |

These are secondary integration and boundary cases. Lower risk than P0 but important for Phase 3 stability.

**Recommended Tests**: 
- `main_loop()` with mocked sessions + signals (verify stage transitions)
- Escalation JSON parser with invalid JSON (verify graceful skip)
- `should_renotify()` with 4h elapsed + 10s poll window

**Effort**: ~120 lines of test code. **Priority**: Phase 3.

---

## Architecture Gaps for Phase 3+

### No Parallel Reviewer Dispatch

**Problem**: The pipeline processes one task at a time. When `reviewer` stage finishes, it can only advance one task (`current_active_task`). If a new task arrives while reviewer is busy, it must wait in FIFO.

**Phase 3 need**: With `test-engineer` role (parallelizable), we need fan-out/fan-in: send multiple tasks to reviewer simultaneously, collect results asynchronously.

**Current state**: `PipelineState.active_task` is a single field (not a list). Escalation logic assumes one-at-a-time.

**Recommendation**: Defer to Phase 4+. Phase 3 keeps single-task pipeline; add queuing and parallel dispatch in Phase 4.

---

### No Pre-Classify Hook

**Problem**: The analyst role (planned for Phase 3) needs to run a pre-pass *before* classifier — gather market data, filter low-signal tasks, and summarize context. Currently there's no stage for that.

**Current state**: Pipeline starts at `classify` stage. No entry point before it.

**Recommendation**: Add optional `analyst` stage that runs before `classify` if enabled. Output feeds into classifier context.

**Effort**: Phase 3 feature (small).

---

### Single-Task Pipeline (No Queuing)

**Problem**: `PipelineState` tracks one `active_task`. When pipeline is frozen (awaiting escalation resolution), new tasks cannot enter until the frozen task resolves. Blocks progress on unrelated work.

**Current state**: `next_task()` returns one task. Orchestrator activates it. If escalation happens, no new task can be pulled.

**Recommendation**: Phase 3 scope includes "pipeline-frozen mitigation" — shelve escalated task, process next from queue. Requires modest refactor to support task queue, not just active task.

**Effort**: Phase 3 feature (medium).

---

## Recommended Phase 3 Ordering

1. **Fix stall triad** (BUG-015, BUG-016, BUG-017) — ~50 lines total, unblocks Phase 3
2. **Add P0 Discord tests** (`DiscordCompanion.handle_message`, `_handle_caveman` dispatch) — ~80 lines
3. **Implement Phase 3 features**:
   - `claude.reformulate()` for dispute resolution
   - `claude.summarize()` for context transfer
   - Session rotation (token tracking, restart on threshold)
   - Pipeline-frozen mitigation (task shelving + queue)
4. **Stage wall-clock timeout** (BUG-011) — add `max_stage_minutes` per stage, track `stage_started_ts`, auto-kill on exceed

---

## Loose Ends

| Item | Count | Status |
|------|-------|--------|
| TODOs in source | 1 | Priority sort in `next_task()` (Phase 3) |
| Unimplemented plans | 2 | Discord operator mode, agent integration |
| Uncommitted files | 47 | Debug logs, test data, untracked signal files |

**Recommendation**: Clean up debug files before Phase 3 code review. Keep 1 TODO (it's already tracked as Phase 3). Plans are documented in `plans/`.

---

## Sign-Off Checklist

| Item | Status | Owner |
|------|--------|-------|
| Stall triad (BUG-015, 016, 017) fixed | ❌ REQUIRED | Executor |
| P0 Discord tests added | ❌ REQUIRED | Code-Reviewer |
| All existing tests passing | ✅ YES | — |
| Code review findings documented | ✅ YES | Reviewer |
| Architecture gaps identified | ✅ YES | Architect |
| Phase 3 feature scope clear | ✅ YES | Architect |

**Phase 3 Entry**: READY pending stall-triad fixes.

---

## Cross-References

- [[v5-harness-known-bugs]] — full bug tracking (21 total, 9 open, 12 resolved)
- [[v5-harness-architecture]] — module overview, stage pipeline, escalation protocol
- [[v5-harness-reviewer-findings]] — review history (3 rounds, 35 issues all fixed)
- [[v5-omc-agent-integration]] — planned agent roles and integration points
