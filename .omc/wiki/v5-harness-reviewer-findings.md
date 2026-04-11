---
title: v5 Harness Reviewer Findings
tags: [harness, security, review, quality]
category: decision
created: 2026-04-09
updated: 2026-04-09
---

> **HISTORICAL (2026-04-11)** — Python harness archived. Findings here are Python-specific.

# v5 Harness Reviewer Findings (Python — Archived)

Four rounds of review. Rounds 1-2: 23 issues found and fixed — see [[v5-harness-reviewer-findings-archive-2026]] for full details. Round 3: Phase 2 post-implementation review (architect, critic, code-reviewer) found 9 new bugs + 3 test coverage gaps. Round 4: Phase 3 pre-implementation review found 6 bugs, all fixed.

## Round 3: Phase 2 Post-Implementation Review (2026-04-09)

Three parallel reviews (architect, critic, code-reviewer) of the Phase 2 escalation system. Found 9 new bugs (BUG-013 through BUG-021), plus 3 test coverage gaps.

### Architect Review (6 findings)

| Finding | Severity | Status |
|---------|----------|--------|
| BUG-015: Pipeline stuck on deleted escalation signal | High | Track |
| BUG-016: Crash recovery ignores `escalation_tier1` | Medium | Track |
| BUG-019: `should_renotify` window coupled to poll_interval | Low | Track |
| BUG-017: No Tier 1 architect timeout | Medium | Track |
| BUG-020: `signal_reader` defaults to None | Low | Fix soon |
| Dead `EscalationReply` dataclass | Low | Fix now |

### Critic Review (3 major test gaps + 6 minor)

| Finding | Severity | Status |
|---------|----------|--------|
| `handle_escalation_wait` has ZERO orchestrator tests | Major | Fix soon |
| `_apply_reply` tests never pass `signal_reader` | Major | Fix soon |
| `cannot_resolve` promotion untested at orchestrator level | Major | Fix soon |
| No end-to-end lifecycle test | Minor | Track |
| No double-escalation test | Minor | Track |
| `_elapsed_seconds` malformed timestamp untested | Minor | Track |

Critic verdict: **ACCEPT-WITH-RESERVATIONS**. Core routing logic solid (mutation-tested). Gaps are in secondary paths, not primary routing.

### Code Reviewer Review (9 findings)

| Finding | Severity | Status |
|---------|----------|--------|
| BUG-013: Fire-and-forget git subprocesses | High | Fix now |
| BUG-014: TOCTOU re-read on tier promotion | High | Fix soon |
| BUG-018: Resume-state boilerplate triplicated | Medium | Fix now |
| Sync `clear_escalation` in async context | Medium | Track |
| `do_merge` doesn't re-validate task_id | Medium | Track |
| BUG-021: Dead guard + lambda naming | Low | Fix now |
| `verdict == "approve" or "approved"` style | Low | Track |

Code reviewer verdict: **REQUEST CHANGES** on the 2 HIGH findings before production.

### Positive observations (code reviewer)

- Mutation queue pattern well-designed (default-argument binding, concurrency-safe)
- Task ID validation thorough (`_safe_task_id` at all ingestion points)
- Escalation tier routing clean and extensible (frozenset lookup tables)
- Guard clauses in `_apply_reply` are solid (wrong task, wrong stage, dead session all handled)
- Informational bypass correctly ordered (severity check before any state mutation)

## Round 4: Phase 3 Pre-Implementation Review (2026-04-09)

Code-reviewer, debugger, critic, and architect agents reviewed Phase 2 code for Phase 3 readiness. Found 6 bugs (BUG-023 through BUG-027 + PERF-1), all fixed. 256 tests passing after fixes.

### Findings

| Finding | Severity | File(s) | Status |
|---------|----------|---------|--------|
| BUG-023: FD leak on session overwrite | Medium | `sessions.py` | FIXED |
| BUG-024: Stale stage signal spurious advancement | High | `signals.py`, `orchestrator.py` | FIXED |
| BUG-025: reconcile() ignores shelved tasks | Medium | `lifecycle.py` | FIXED |
| BUG-026: Lost operator reply for shelved tasks | High | `discord_companion.py`, `orchestrator.py` | FIXED |
| BUG-027: do_wiki reply injection timing concern | Low | `orchestrator.py` | FIXED (documented) |
| PERF-1: parse_token_usage O(n) re-read | Low | `sessions.py` | Filed in NOTES.md |

### Tests Added (12 new tests)

| Test | File | Covers |
|------|------|--------|
| `test_shelve_during_escalation_activates_new_task` | `test_orchestrator.py` | Shelve flow |
| `test_unshelve_after_wiki_restores_task` | `test_orchestrator.py` | Unshelve flow |
| `test_unshelve_injects_pending_operator_reply` | `test_orchestrator.py` | BUG-026 fix |
| `test_escalation_entry_clears_stale_stage_signal` | `test_orchestrator.py` | BUG-024 fix |
| `test_rotation_restarts_and_reinjects_context` | `test_orchestrator.py` | Session rotation |
| `test_reconcile_shelved_tasks_renotifies_escalation_wait` | `test_lifecycle.py` | BUG-025 fix |
| `test_reconcile_shelved_tasks_no_signal_logs_warning` | `test_lifecycle.py` | BUG-025 edge |
| `test_reconcile_shelved_tasks_non_escalation_ignored` | `test_lifecycle.py` | BUG-025 negative |
| `test_reconcile_shelved_tier1_renotifies_when_signal_exists` | `test_lifecycle.py` | Tier1 shelved |
| `test_reconcile_shelved_tier1_promotes_when_no_signal` | `test_lifecycle.py` | Tier1 promotion |
| `test_apply_reply_shelved_task_stores_reply` | `test_discord_companion.py` | BUG-026 fix |
| `test_apply_reply_unknown_task_logs_warning` | `test_discord_companion.py` | BUG-026 edge |

Plus 5 unit tests for `clear_stage_signal` in `test_signals.py` (architect, executor, reviewer, missing, unknown stage).

### Architect Verification

**Verdict: APPROVED** with reservations (all addressed):
- Reply injection timing concern → documented with warning log
- Missing `clear_stage_signal` tests → 5 tests added
- Shelved `escalation_tier1` gap in lifecycle → fixed with promotion logic
- Lifecycle test assertion → added `read_escalation.assert_awaited_with` check

## Cross-References

- [[v5-harness-architecture]] — module overview and pipeline flow
- [[v5-harness-design-decisions]] — rationale behind key choices
- [[v5-harness-known-bugs]] — bugs found during review, tracked for future phases
