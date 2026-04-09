---
title: v5 Phase 3 Readiness Assessment
tags: [harness, phase3, audit, readiness]
category: architecture
created: 2026-04-09
updated: 2026-04-09
status: all-blockers-resolved
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

## Blockers: The Stall Triad — RESOLVED

All three stall triad bugs fixed (2026-04-09). See [[v5-harness-known-bugs-archive-2026]] for full details.

| Bug | Fix Summary |
|-----|-------------|
| BUG-015 | Timeout-based force-resume on missing escalation signal |
| BUG-016 | `escalation_tier1` crash recovery in `lifecycle.reconcile()` |
| BUG-017 | `tier1_timeout` config + auto-promote to Tier 2 |

---

## Code Review Findings

All 5 fixes from Phase 2 fix-now batch validated correct. 3 residual quality issues identified:

- **Cache leak** (`orchestrator.py`): `_escalation_cache` not purged in `clear_active()` on task abandonment. Fix: add `_escalation_cache.pop(task_id, None)` to `clear_active()`. 1 line, Phase 3 improvement.
- **Signal reader None signature** (`discord_companion.py`): `_apply_reply(signal_reader=None)` still accepts `None` inconsistently with constructor enforcement. Fix: update type hint to `signal_reader: SignalReader`. 1 line, Phase 3 cleanup.
- **Missing cache-cleanup test** (`tests/test_orchestrator.py`): No test asserts `_escalation_cache` is empty after escalation resolution. Fix: add test for escalation → resolve → cache empty. ~15 lines, Phase 3 test coverage.

---

## Test Coverage Gaps

### P0: DiscordCompanion Zero Coverage — RESOLVED (40 tests, 2026-04-09)

`DiscordCompanion.handle_message()` and `_handle_caveman()` dispatch now covered. 40 tests added covering valid commands (`!task`, `!tell`, `!reply`, `!status`), parse errors, permission checks, and caveman mode dispatch.

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

1. ~~**Fix stall triad** (BUG-015, BUG-016, BUG-017)~~ — DONE (stall triad batch, 2026-04-09)
2. ~~**Add P0 Discord tests** (`DiscordCompanion.handle_message`, `_handle_caveman` dispatch)~~ — DONE (40 tests, 2026-04-09)
3. **Implement Phase 3 features**:
   - `claude.reformulate()` for dispute resolution
   - `claude.summarize()` for context transfer
   - Session rotation (token tracking, restart on threshold)
   - Pipeline-frozen mitigation (task shelving + queue)
4. ~~**Stage wall-clock timeout** (BUG-011)~~ — DONE (Phase 2 prereq batch, 2026-04-09)
5. **BUG-022 mitigation**: Extract `kill()` from `restart()` on SessionManager — prevents wasteful session relaunch on stage timeout

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
| Stall triad (BUG-015, 016, 017) fixed | ✅ DONE | Executor |
| P0 Discord tests added | ✅ DONE (40 tests) | Test-Engineer |
| All existing tests passing | ✅ YES | — |
| Code review findings documented | ✅ YES | Reviewer |
| Architecture gaps identified | ✅ YES | Architect |
| Phase 3 feature scope clear | ✅ YES | Architect |

**Phase 3 Entry**: ✅ ALL BLOCKERS RESOLVED. Ready to proceed.

---

## Cross-References

- [[v5-harness-known-bugs]] — full bug tracking (24 total, 9 open, 15 resolved)
- [[v5-harness-known-bugs-archive-2026]] — resolved bug details including stall triad (BUG-015, BUG-016, BUG-017)
- [[v5-harness-architecture]] — module overview, stage pipeline, escalation protocol
- [[v5-harness-reviewer-findings]] — review history (3 rounds, 35 issues all fixed)
- [[v5-omc-agent-integration]] — planned agent roles and integration points
