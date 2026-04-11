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
| 2.5 | Stall Triad | COMPLETE | BUG-015/016/017 fixes, DiscordCompanion tests (43 tests) | Prerequisite for Phase 3 |
| 3 | Intelligence + Disputes | COMPLETE | `claude.reformulate()`, `claude.summarize()`, session rotation, frozen-pipeline mitigation | 256 tests (full suite) |
| 4 | Wiki + Documentation | COMPLETE | Wiki stage integration, document-task improvements | 272 tests (full suite) |
| 5 | Bot Pipeline + Extensibility | NOT STARTED | Configurable pipelines, trading bot integration, sessions.toml | Phase 4 prerequisite |

---

## Phase 2.5: Stall Triad — COMPLETE (2026-04-09)

All three stall triad bugs fixed and P0 Discord tests added. See [[v5-harness-known-bugs-archive-2026]] for fix details.

| Bug | Fix Summary |
|-----|-------------|
| BUG-015 | Timeout-based force-resume on missing escalation signal |
| BUG-016 | `escalation_tier1` crash recovery in `lifecycle.reconcile()` |
| BUG-017 | `tier1_timeout` config + auto-promote to Tier 2 |

**Tests added:** 43 (DiscordCompanion dispatch + stall triad scenarios). All passing.

---

## Phase 3: Intelligence + Disputes — COMPLETE (2026-04-09)

All features implemented. 256 tests passing (full suite).

| Feature | Module | Status |
|---------|--------|--------|
| `claude.reformulate()` | `claude.py` | Done — reviewer→executor dispute resolution |
| `claude.summarize()` | `claude.py` | Done — context transfer between stages |
| Session rotation | `sessions.py`, `orchestrator.py` | Done — `token_rotation_threshold` config, `needs_rotation()` check |
| Pipeline-frozen mitigation | `orchestrator.py`, `pipeline.py` | Done — `shelve()`/`unshelve()` LIFO queue, operator reply injection, lifecycle reconciliation |
| Stage wall-clock timeout (BUG-011) | `orchestrator.py` | Done — fixed in Phase 2 fix batch |

### Residual Items (deferred)

- **Priority sort TODO**: `next_task()` line 75. Current: FIFO by mtime. Future: configurable sort.
- **BUG-019**: `should_renotify` window coupling to poll_interval — low severity, defer to Phase 4+.

---

## Phase 4: Wiki + Documentation — COMPLETE (2026-04-09)

Real data now flows to wiki stage. 272 tests passing (full suite).

| Feature | Module | Status |
|---------|--------|--------|
| Wiki stage data collection | `pipeline.py`, `orchestrator.py` | Done — plan_summary, diff_stat, review_verdict accumulated on PipelineState |
| Diff stat capture | `orchestrator.py:do_merge` | Done — `git diff --stat HEAD~1` after successful merge |
| Wiki fallbacks | `orchestrator.py:do_wiki` | Done — sensible defaults when data is None |
| wiki_failed event | `orchestrator.py:do_wiki` | Done — event_log records failures for telemetry |
| OMC hook graceful degradation | `claude.py:document_task` | Pre-existing — warn and continue on failure |

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

### Conversational Discord Operator — COMPLETE

**Status:** All pieces implemented (2026-04-09). Discord Integration Revisions also executed.

**Three-Piece Architecture:**

1. **Agent Outbound (Status Posts)** — DONE. Agents post to Discord via `clawhip send` (prompt-only).
2. **Natural Language Inbound Routing** — DONE. `classify_target()` (haiku), `handle_raw_message()`, `start()` coroutine.
3. **Escalation Dialogue** — DONE. `classify_resolution()`, `escalation_dialogue` stage, dialogue state fields, circuit breaker, auto-escalation.

**Discord Integration Revisions** — DONE. Three-way NL classify (`classify_intent`), deterministic control pre-filter (`_CONTROL_PATTERN`), pipeline pause/resume, NL-initiated task creation via `TaskSignal`.

**Post-completion enhancements (2026-04-10):**
- Reaction acknowledgments: 👀 on receive, ✅/❌ on complete — operator knows message was seen
- Webhook per-agent identity: responses sent via Discord webhook with agent-specific username (Orchestrator/Architect/Executor/Reviewer)
- Message accumulator design: NL messages buffer for 2s debounce window, then process as one — replaces message queue approach. `!` commands and control words bypass buffer for instant processing.
- Mention-only filter: bot only responds when @mentioned

**Resolved design gaps:**
- NL pipeline commands: control pre-filter catches "stop", "pause", "resume", "status" deterministically before any LLM call.
- NL-initiated tasks: `classify_intent` distinguishes feedback from new_task. New tasks create `TaskSignal` → full pipeline.
- Subtask vs task completion: `notify("task_completed", ...)` in `do_wiki()` sends distinct notification. Subtask commits still route via clawhip `git.commit`.

**Reference:** [[v5-conversational-discord-operator]]

### Discord Presence Polish (Visual Identity + Proactive Reporting)

**Status:** Approved design, unscheduled  
**Depends:** Webhook per-agent (DONE), agent outbound via clawhip (DONE)  
**Reference:** [[v5-conversational-discord-operator]]

Gap analysis against production-grade bot Discord presence (Devin-style). Five items, priority ordered:

| # | Feature | Effort | Impact | What |
|---|---------|--------|--------|------|
| 1 | **Avatar URLs per agent** | Trivial | High visual | Expand `AGENT_DISPLAY_NAMES` → `AGENT_IDENTITIES` with `avatar_url`. Host 4 colored circle PNGs. Pass through webhook POST. |
| 2 | **Stage transition announcements** | ~15 lines | High operational | Orchestrator posts to Discord on every stage change via `clawhip send` or webhook. Format: role + task ID + description. |
| 3 | **Agent progress prompts** | Prompt-only | Medium | Update `config/harness/agents/*.md` to instruct agents to post work-in-progress summaries via `clawhip send`. Zero code changes. |
| 4 | **Structured message formatting** | Low | Polish | Message templates for stage transitions, escalations, completions. Markdown formatting with sections and bullets. |
| 5 | **Commit notifications** | Config-only | Medium | Unblock `git.commit` route in clawhip.toml. Either add GitHub PAT to `.env` (5000 req/hr) or increase poll to 60s (within unauthenticated limit). |

**What NOT to do:**
- Don't create separate Discord bots per agent — webhooks achieve same visual result with one token
- Don't add Discord embeds — harder to read on mobile, more API complexity
- Don't make agents read Discord — star topology stays intact; mediated presence is correct

Items 1+2 get 80% of the polished feel. Items 3-5 are incremental polish.

### Message Accumulator (Multi-Message Handling)

**Status:** Approved design, unscheduled  
**Depends:** Reactions (DONE), webhook per-agent (DONE)

Three-lane design replacing FIFO queue approach:
- **Immediate lane**: `!` commands, control words → process instantly
- **Accumulate lane**: NL messages → 2s debounce window, concatenate, process once
- **Bypass lane**: own messages, non-mentioned, dedup → drop

~40 lines in `on_message` handler. No new dependencies, no new module. Stays in single event loop.

**Key insight**: rapid-fire Discord messages are a message boundary problem, not a queueing problem. Operator types 3 messages in 4 seconds = one instruction, not three tasks.

**Reference:** [[v5-conversational-discord-operator]] (Message Accumulator section)

---

## Dependencies Graph

```
Phase 1 (DONE)
    ↓
Phase 2 (DONE)
    ↓
Phase 2.5 (DONE — stall triad + Discord tests)
    ↓
Phase 3 (DONE — reformulate, summarize, session rotation, frozen-pipeline)
    ↓
    ├─→ OMC agent integration Tier 1-2 (parallel, feeds into Phase 4)
    ├─→ Discord conversational operator Pieces 1-3 (DONE)
    ├─→ Discord Integration Revisions (DONE — three-way classify, pause, NL tasks)
    ├─→ Reactions + Webhook per-agent + Mention filter (DONE)
    ├─→ Message accumulator (approved, unscheduled)
    ├─→ Discord presence polish (approved, unscheduled — avatars, stage announcements, progress prompts)
    ↓
Phase 4 (DONE — Wiki integration, document-task)
    ↓
Phase 5 (Configurable pipelines, bot integration, extensibility)
```

---

## Open Questions

1. ~~**Phase 2.5 ownership?**~~ Resolved — stall triad fixed, 43 Discord tests added (2026-04-09).
2. **BUG-019 fix timing?** `should_renotify` window coupling to poll_interval is low severity — defer to Phase 4+.
3. **Analyst trigger criteria?** "Complex + vague tasks" is heuristic. Define concrete thresholds in Phase 4 design review.
4. **Verifier mandatory or opt-in?** Decide if pre-merge gate is standard or per-task in Phase 4.

---

## Cross-References

- [[v5-harness-architecture]] — Architecture design, module structure, orchestrator logic
- [[v5-phase3-readiness]] — Readiness assessment, full blocker analysis, test coverage gaps
- [[v5-harness-known-bugs]] — Bug tracking (21 total, 9 open, 12 resolved)
- [[v5-omc-agent-integration]] — Tier 1-3 agent folding and ad-hoc delegation
- [[v5-conversational-discord-operator]] — Three-piece Discord operator design
