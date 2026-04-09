---
title: v5 Harness Roadmap
tags: [harness, roadmap, timeline, phases]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Roadmap

Central timeline mapping v5 harness features to phases with dependencies and status. This is the "where are we, what's next" document.

## Phase Overview Table

| Phase | Name | Status | Key Deliverables | Test Coverage |
|-------|------|--------|-----------------|---|
| 1 | Foundation | COMPLETE | Async orchestrator, FIFO sessions, stage pipeline | 86 tests |
| 2 | Escalation | COMPLETE | Tiered escalation (Tier 1 architect, Tier 2 operator), confidence gating, timeouts | 150 tests |
| 2.5 | Stall Triad | COMPLETE | BUG-015/016/017 fixes, DiscordCompanion tests | Prerequisite for Phase 3 |
| 3 | Intelligence + Disputes | NOT STARTED | `claude.reformulate()`, `claude.summarize()`, session rotation, frozen-pipeline mitigation | Phase 2.5 prerequisite |
| 4 | Wiki + Documentation | NOT STARTED | Wiki stage integration, document-task improvements | Phase 3 prerequisite |
| 5 | Bot Pipeline + Extensibility | NOT STARTED | Configurable pipelines, trading bot integration, sessions.toml | Phase 4 prerequisite |

---

## Phase 2.5: Stall Triad — COMPLETE

All stall triad bugs fixed + P0 Discord tests added. See [[v5-harness-roadmap-archive-2026]] for full details.

---

## Phase 3: Intelligence + Disputes

Deliver Claude-driven dispute mitigation and context preservation across stage boundaries. **Depends:** Phase 2.5 complete.

### Features

| Feature | Module | Purpose | Effort |
|---------|--------|---------|--------|
| `claude.reformulate()` | `claude.py` | Reviewer rejects → `reformulate()` reframes for executor. Prevents circular replanning. | ~40 lines |
| `claude.summarize()` | `claude.py` | Context transfer between stages. Compresses 500-line diff to "changed 3 files, added retry logic". | ~40 lines |
| Session rotation | `sessions.py`, `orchestrator.py` | Track token usage from stream-json output. Restart session on threshold, re-inject context. | ~60 lines |
| Pipeline-frozen mitigation | `orchestrator.py` | Escalated task blocks progress. Shelve it, process next from queue. Requires task queue refactor. | ~80 lines |
| Stage wall-clock timeout (BUG-011) | `orchestrator.py` | Add `max_stage_minutes` per stage in config. Track `stage_started_ts`. Auto-kill session if exceeded. | ~50 lines |

### Additional Improvements

- **Priority sort TODO** (Phase 3 gap): `next_task()` line 75. Current: FIFO by mtime. Future: configurable sort (priority label, age, etc.)
- **Cache cleanup on resolution**: Add `_escalation_cache.pop(task_id, None)` to `clear_active()` — prevents stale cache on task abandonment
- **Signal reader type hint**: Fix `_apply_reply(signal_reader: SignalReader)` — remove `None` default

### Test Coverage

| Scenario | Status | Lines |
|----------|--------|-------|
| `reformulate()` with low-confidence rejection | New | ~20 |
| `summarize()` with large diff | New | ~25 |
| Session restart on token threshold | New | ~30 |
| Shelve + dequeue on escalation | New | ~40 |
| Stage timeout boundary (4h, 10s window) | New | ~20 |

**Entry Criteria:** Phase 2.5 fixes + P0 Discord tests complete. All Phase 2 tests passing.

---

## Phase 4: Wiki + Documentation

Post-merge documentation integration. **Depends:** Phase 3 complete.

| Feature | Scope | Purpose |
|---------|-------|---------|
| Wiki stage integration | `orchestrator.py`, `do_wiki()` | After successful merge, call `claude.document_task()` with task context |
| Document-task improvements | `claude.py` | Accept: task description, plan summary, diff stat, review verdict. Output: formatted wiki entry via `/wiki` skill. |
| OMC hook dependency | `claude.py` | `/wiki` skill requires OMC hooks. Fail gracefully if hooks don't fire (warn, don't block pipeline). |

---

## Phase 5: Bot Pipeline + Extensibility

Graduation from hardcoded to configurable. **Depends:** Phase 4 complete.

| Feature | Scope | Purpose | Current State |
|---------|-------|---------|---|
| sessions.toml config | `config/harness/sessions.toml` | Read agent defs from TOML instead of Python hardcodes. Same `AgentDef` dataclass, different source. | Phase 1: hardcoded `DEFAULT_AGENTS` |
| Configurable pipelines | `config/harness/clawhip.toml.template` | Support alternate stage orders (e.g., no wiki, or analyst pre-pass). | Phase 1: hardcoded `architect → executor → reviewer → merge → wiki` |
| Trading bot integration | `config/harness/commands.py` | Load project-specific Discord commands via plugin contract. | Defined in architecture plan |
| Analytics stage (ops monitor) | `config/harness/agents/ops_monitor.md` | Always-on persistent session monitoring pipeline health, performance, cost. | Defined in architecture plan |

---

## Unscheduled Proposals

Approved designs not yet assigned to a phase. Most likely timeline noted.

### OMC Agent Integration (Tier 1-3)

**Status:** Approved design, unscheduled  
**Phases:** Most likely 3-4

| Tier | Feature | Integration Point |
|------|---------|-------------------|
| Tier 1 | Fold analyst into architect | `classify` stage: if task complex+vague, run analyst pre-pass (Opus). Output enriches architect context. |
| Tier 1 | Fold test-engineer into reviewer | `reviewer` stage: add test-engineer (Sonnet) parallel with code-reviewer + security-reviewer. |
| Tier 1 | Fold document-specialist | Both executor + reviewer: fetch current docs before implementation/verification. Catch stale patterns. |
| Tier 2 | Writer (Haiku) | Wiki stage, post-phase completion, doc tasks. Cheap documentation (60x cheaper than Opus). |
| Tier 2 | Verifier (Sonnet) | Pre-commit + pre-merge gate. Fresh test suite, acceptance criteria validation. Optional vs. mandatory per task. |
| Tier 2 | Debugger + Tracer (Sonnet) | `persistent_failure` escalation. Debugger attempts fix; tracer tracks competing hypotheses if first fails. |
| Tier 3 | Structured feedback loop | Reviewer rejection now includes `what_failed`, `why`, `suggested_fix`. Executor receives structured feedback, not text blob. |

**Reference:** [[v5-omc-agent-integration]]

### Conversational Discord Operator

**Status:** Approved design, unscheduled  
**Phases:** Most likely 4-5

**Three-Piece Architecture:**

1. **Agent Outbound (Status Posts)** — Agents post to Discord via `clawhip send --channel "$AGENT_CHANNEL"` (no code changes, prompt-only)
2. **Natural Language Inbound Routing** — Operator sends messages without `!` prefix. Companion interprets → routes to correct agent. (~50 lines in `discord_companion.py`)
3. **Escalation Dialogue** — Multi-turn conversation during Tier 2 escalation. Operator message → FIFO route → agent responds via `clawhip send`. Stateless, max 30s timeout.

**Reference:** [[v5-conversational-discord-operator]]

---

## Dependencies Graph

```
Phase 1 (DONE)
    ↓
Phase 2 (DONE)
    ↓
Phase 2.5 (Fix stall triad + Discord tests) ← BLOCKS Phase 3
    ↓
Phase 3 (reformulate, summarize, session rotation, frozen-pipeline)
    ↓
    ├─→ OMC agent integration Tier 1-2 (parallel, feeds into Phase 4)
    ├─→ Discord conversational operator Piece 1-2 (parallel, independent)
    ↓
Phase 4 (Wiki integration, document-task)
    ↓
    └─→ Discord operator Piece 3 (uses Pieces 1-2)
    ↓
Phase 5 (Configurable pipelines, bot integration, extensibility)
```

---

## Open Questions

1. **BUG-019 fix timing?** `should_renotify` window coupling to poll_interval is low severity — defer to Phase 3 or Phase 4?
2. **Analyst trigger criteria?** "Complex + vague tasks" is heuristic. Define concrete thresholds in Phase 3 design review.
3. **Verifier mandatory or opt-in?** Phase 3 spec needed to decide if pre-merge gate is standard or per-task.

---

## Cross-References

- [[v5-harness-architecture]] — Architecture design, module structure, orchestrator logic
- [[v5-phase3-readiness]] — Readiness assessment, full blocker analysis, test coverage gaps
- [[v5-harness-known-bugs]] — Bug tracking (24 total, 9 open, 15 resolved)
- [[v5-harness-roadmap-archive-2026]] — Archived Phase 2.5 (Stall Triad) details
- [[v5-omc-agent-integration]] — Tier 1-3 agent folding and ad-hoc delegation
- [[v5-conversational-discord-operator]] — Three-piece Discord operator design
