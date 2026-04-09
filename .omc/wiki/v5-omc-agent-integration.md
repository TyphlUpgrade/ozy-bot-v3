---
title: v5 OMC Agent Integration Plan
tags: [harness, agents, omc, pipeline]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# v5 OMC Agent Integration Plan

Expansion of the v5 harness to use specialized OMC agents for quality, documentation, and debugging. The design principle is "Fold, Don't Expand" — augment existing pipeline roles instead of adding new stages.

## Design Principle: Fold, Don't Expand

The current pipeline is `classify → architect → executor → reviewer → merge → wiki`. Adding new stages increases orchestrator complexity and failure modes. Instead:

- **Fold capabilities into existing roles** — Analyst augments architect; test-engineer augments reviewer
- **Spawn ad-hoc agents outside the pipeline** — Debugger, tracer, writer on demand
- **Close the reviewer → executor feedback loop** — The real pipeline gap

## Tier 1: Fold Into Existing Roles

### Analyst Augments Architect

| Aspect | Details |
|--------|---------|
| **Current** | Architect designs plans. Requirements analysis skipped or done inline. |
| **Change** | For complex or vague tasks, conductor spawns analyst (Opus) as pre-pass. |
| **Output** | Requirements, edge cases, acceptance criteria enrich task description. |
| **Integration** | Conductor judgment during `classify`. If task is complex + description is vague, run analyst first. |
| **Model** | Opus 4.6 |
| **No new stage** | Analyst → architect → executor (unchanged pipeline order). |

### Test-Engineer Augments Reviewer

| Aspect | Details |
|--------|---------|
| **Current** | Reviewer runs code-reviewer + security-reviewer in parallel. Test adequacy not explicitly checked. |
| **Change** | Add test-engineer as third parallel reviewer. |
| **Output** | Test adequacy findings merge into review verdict. |
| **Integration** | Orchestrator's reviewer dispatch (parallel with existing reviewers). |
| **Model** | Sonnet 4.6 |

### Document-Specialist Augments Executor and Reviewer

| Aspect | Details |
|--------|---------|
| **Current** | Executor uses training data for external API/library usage. Reviewer can't verify against current docs. |
| **Change** | Spawn document-specialist (read-only + web fetch) for both executor and reviewer. |
| **Executor role** | Fetch current docs before implementation — method signatures, version constraints, deprecation notices. |
| **Reviewer role** | Verify library/API usage in diff against current docs. Catch stale patterns. |
| **Integration** | Conductor flags tasks as "external-API-touching" during classify. Document-specialist runs parallel with primary agent. |
| **Model** | Sonnet 4.6 |

## Tier 2: Ad-Hoc Agents (Outside Pipeline)

These spawn on demand by conductor, operator, or other agents. No new pipeline stages.

### Writer (Haiku)

- **Purpose**: Documentation — wiki updates, COMPLETED_PHASES entries, plan drafts, README updates
- **Cost**: 1x (Haiku: ~60x cheaper than Opus)
- **Trigger**: Wiki stage, post-phase completion, doc tasks
- **Rationale**: Documentation needs clarity and accuracy, not deep reasoning

### Verifier (Sonnet)

- **Purpose**: Pre-commit and pre-merge gate. Fresh test suite, acceptance criteria validation, evidence-backed PASS/FAIL
- **Cost**: ~10x
- **Trigger**: Pre-commit, pre-phase-completion, pre-merge
- **Status**: Optional vs. mandatory per task complexity (open question)

### Debugger (Sonnet)

- **Purpose**: Structured root-cause analysis for identified bugs. Minimal fix recommendation with exact diff
- **Cost**: ~10x
- **Trigger**: `persistent_failure` escalation or operator command
- **Scope**: Bug-category escalations only, not design or requirements

### Tracer (Sonnet)

- **Purpose**: For `persistent_failure` only — tracks competing hypotheses with evidence when same error recurs
- **Cost**: ~10x
- **Trigger**: `persistent_failure` + `retry_count >= 2`
- **Scope**: Bug-category escalations only. Hypothesis elimination for bugs, not ambiguity
- **Rationale**: Debugger attempts first fix; tracer only if first attempt fails

## Tier 3: Reviewer → Executor Feedback Loop

**The real pipeline gap.** When reviewer rejects, feedback currently reaches executor as unstructured text. After max_retries, task abandons silently.

### Current Flow (Broken)
```
reviewer rejects → retry_count++ → text feedback → executor retries
                                       ↓
                              repeats same mistake
                                       ↓
                            max_retries → task dies
```

### Proposed Flow

1. **Structured feedback**: Reviewer verdict includes `what_failed`, `why`, `suggested_fix`
2. **Feedback routing**: Orchestrator passes structured feedback to executor (not text blob)
3. **Same-category escalation**: If `what_failed` category appears twice, escalate to architect for redesign
4. **No abandonment**: After max_retries, escalate to operator (don't silently abandon)

### Integration

- Update `check_reviewer()` signal schema with structured fields
- Implement feedback routing in orchestrator
- **Only orchestrator logic change in Tier 3** — everything else is delegation

## Implementation Phases

| Phase | Scope | Timeline |
|-------|-------|----------|
| **Phase 1** | Ad-hoc delegation: writer, verifier, debugger, tracer (no code changes) | Immediate |
| **Phase 2** | Test-engineer parallel dispatch + structured feedback fields + feedback routing | Post-Phase 1 |
| **Phase 3** | Analyst pre-pass for complex+vague tasks + verifier as pre-merge gate | Post-Phase 2 |

## Out of Scope

- **git-master**: Conductor already handles git merge. History rewriting adds risk without value on feature branches.
- **Wiki agent**: Wiki stage is simple inline. Defer to when knowledge base needs active maintenance.
- **New pipeline stages**: All agents fold into existing roles or spawn ad-hoc. Zero new transitions.

## Cost Analysis

| Agent | Model | Cost | Frequency | Justification |
|-------|-------|------|-----------|---------------|
| writer | Haiku | 1x | Every wiki stage, doc update | Documentation, not reasoning |
| verifier | Sonnet | ~10x | Pre-commit, pre-merge | Quality gate |
| debugger | Sonnet | ~10x | Per bug (ad-hoc, rare) | Root-cause analysis |
| tracer | Sonnet | ~10x | Per persistent_failure (rare) | Hypothesis tracking |
| test-engineer | Sonnet | ~10x | Every reviewer stage | Parallel with existing reviewers |
| document-specialist | Sonnet | ~10x | Tasks touching external APIs | Current docs lookup |
| analyst | Opus | ~60x | Complex + vague tasks only | Deep requirements analysis |

**Primary savings**: Writer at Haiku cost replacing inline Opus documentation.

## Open Questions

1. Should verifier be mandatory pre-merge or opt-in per task complexity?
2. Should test-engineer run parallel with code-reviewer (faster) or sequential (can see prior findings)?
3. Should repeated same-category failures escalate to architect or directly to operator?

## Related Pages

- [[v5-harness-architecture]] — Current pipeline, stage details, FIFO session model
- [[v5-harness-known-bugs]] — Existing issues and their resolution status
