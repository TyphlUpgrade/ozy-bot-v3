---
title: v5 Harness Reviewer Findings
tags: [harness, security, review, quality]
category: decision
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Reviewer Findings

Three rounds of review. Rounds 1-2: 23 issues found and fixed — see [[v5-harness-reviewer-findings-archive-2026]] for full details. Round 3: Phase 2 post-implementation review (architect, critic, code-reviewer) found 9 new bugs + 3 test coverage gaps.

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

## Cross-References

- [[v5-harness-architecture]] — module overview and pipeline flow
- [[v5-harness-design-decisions]] — rationale behind key choices
- [[v5-harness-known-bugs]] — bugs found during review, tracked for future phases
