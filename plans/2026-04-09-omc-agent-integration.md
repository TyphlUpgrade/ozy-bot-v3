# OMC Agent Integration

**Date**: 2026-04-09  
**Status**: Proposal v2 (revised with critic feedback)  
**Depends on**: Phase 2 complete, "fix now" + "fix soon" batches applied

## Problem Statement

We have access to 19 specialized OMC agent types but actively use only 6 (architect, critic, code-reviewer, security-reviewer, explore, executor). Several pipeline stages are currently handled inline by the orchestrating LLM, where a specialized agent would produce better results at lower cost.

## Design Principle: Fold, Don't Expand

The core pipeline is `classify → architect → executor → reviewer → merge → wiki`. Adding new stages increases orchestrator complexity, introduces failure modes, and reduces throughput. Instead, we:

- **Fold capabilities into existing roles** — analyst augments architect, test-engineer augments reviewer
- **Spawn ad-hoc agents outside the pipeline** — debugger, tracer, writer on demand
- **Close the reviewer → executor feedback loop** — the real pipeline gap

## Proposed Integration

### Tier 1: Fold Into Existing Roles

#### Analyst augments architect

**Current**: Architect designs plans. Requirements analysis is skipped or done inline by the orchestrator.

**Change**: For complex or vague tasks, conductor spawns analyst (Opus) as a pre-pass. Analyst output (requirements, edge cases, acceptance criteria) enriches the task description before architect invocation. No new stage.

**Integration**: Conductor judgment during `classify`. If task is complex + description is vague, run analyst first, then advance to architect.

**Model**: Opus 4.6

#### Test-engineer augments reviewer

**Current**: Reviewer stage runs code-reviewer + security-reviewer in parallel. Test adequacy is not explicitly checked.

**Change**: Add test-engineer as a third parallel reviewer. Its findings merge into the review verdict alongside code and security reviews.

**Integration**: Orchestrator's reviewer dispatch (parallel).

**Model**: Sonnet 4.6

#### Document-specialist augments executor and reviewer

**Current**: Executor implements against external libraries using training data knowledge, which may be stale. Reviewer checks code style and logic but can't verify API usage against current docs.

**Change**: When a task touches external libraries or APIs, spawn document-specialist (read-only + web fetch) as a support agent for both executor and reviewer.
- **Executor**: Fetches current docs before implementation — correct method signatures, version constraints, deprecation notices. Prevents "works in v2 but we're on v3" bugs at the source.
- **Reviewer**: Verifies library/API usage in the diff against current docs. Catches stale patterns that code-reviewer and security-reviewer would miss.

**Integration**: Executor and reviewer stages. Conductor flags tasks as "external-API-touching" during classify (based on task description or file paths). Document-specialist runs parallel with the primary agent in each stage.

**Model**: Sonnet 4.6 (read-only + web fetch)

### Tier 2: Ad-Hoc Agents

These agents spawn on demand — by the conductor, operator, or other agents. They sit outside the pipeline.

#### writer (Haiku)

Delegate documentation: wiki updates, COMPLETED_PHASES entries, plan drafts, README updates. Haiku costs ~60x less than Opus. Documentation needs clarity and accuracy, not deep reasoning.

**Trigger**: Wiki stage, post-phase completion, doc tasks.

#### verifier (Sonnet)

Pre-commit and pre-merge gate. Runs fresh test suite, validates acceptance criteria, reports evidence-backed PASS/FAIL.

**Trigger**: Pre-commit, pre-phase-completion, pre-merge.

#### debugger (Sonnet)

Structured root-cause analysis for identified bugs. Produces minimal fix recommendation with exact diff.

**Trigger**: `persistent_failure` escalation or operator command.

#### tracer (Sonnet)

For `persistent_failure` only — when the same error recurs after debugger's first attempt. Tracks competing hypotheses with evidence, suggests discriminating probes.

**Scope**: Bug-category escalations only (not design or requirements). Hypothesis elimination methodology applies to bugs, not ambiguity.

**Trigger**: `persistent_failure` + `retry_count >= 2`.

### Tier 3: Reviewer → Executor Feedback Loop

The real pipeline gap. When reviewer rejects a task, feedback currently reaches executor as unstructured text. After max_retries, the task abandons silently.

**Current flow**:
```
reviewer rejects → retry_count++ → text feedback → executor retries
                                       ↓
                              repeats same mistake
                                       ↓
                            max_retries → task dies
```

**Proposed**:

1. Reviewer verdict includes structured fields: `what_failed`, `why`, `suggested_fix`
2. Orchestrator passes structured feedback to executor (not text blob)
3. If same `what_failed` category appears twice, escalate to architect for redesign before executor retry
4. After max_retries, escalate to operator (don't abandon)

**Integration**: Update `check_reviewer()` signal schema (add structured fields) and implement feedback routing in orchestrator.

**This is the only orchestrator logic change.** Everything else is delegation.

### Tier 4: Future Consideration

| Agent | When | Notes |
|-------|------|-------|
| scientist | Backtest analysis, perf metrics | Trading bot analysis, not dev pipeline |
| designer | Monitoring dashboard/web UI | If we build one |
| qa-tester | Interactive CLI testing | Conductor + Discord companion |
| ~~document-specialist~~ | ~~New library/API research~~ | Promoted to Tier 1 (executor + reviewer) |
| code-simplifier | Post-refactor cleanup | Separate pass after large refactors |

## Out of Scope

- **git-master**: Conductor already handles git in merge. History rewriting adds risk without value on feature branches.
- **Wiki agent**: Wiki stage is simple inline. Defer to when knowledge base needs active maintenance.
- **New pipeline stages**: Analyst folds into conductor, test-engineer into reviewer. Zero new transitions.

## Cost Analysis

| Agent | Model | Cost | Frequency |
|-------|-------|------|-----------|
| writer | Haiku | 1x | Every wiki stage, doc update |
| verifier | Sonnet | ~10x | Pre-commit, pre-merge |
| debugger | Sonnet | ~10x | Per bug (ad-hoc) |
| tracer | Sonnet | ~10x | Per persistent_failure (rare) |
| test-engineer | Sonnet | ~10x | Every reviewer stage |
| document-specialist | Sonnet | ~10x | Tasks touching external APIs |
| analyst | Opus | ~60x | Complex + vague tasks only |

**Primary savings**: Writer at Haiku cost replacing inline Opus documentation.

## Implementation Plan

**Phase 1 (immediate, no code changes)**:
- Start delegating writer, verifier, debugger, tracer ad-hoc
- Build judgment for when to delegate vs. inline

**Phase 2 (reviewer augmentation)**:
- Add test-engineer to reviewer parallel dispatch
- Update reviewer signal schema with structured feedback fields (`what_failed`, `why`, `suggested_fix`)
- Implement reviewer → executor feedback routing

**Phase 3 (conductor enrichment)**:
- Add analyst pre-pass for complex + vague tasks
- Add verifier as pre-merge gate

## Open Questions

1. Should verifier be mandatory pre-merge or opt-in per task complexity?
2. Should test-engineer run parallel with code-reviewer (faster) or sequential (can see prior findings)?
3. Should repeated same-category failures escalate to architect or directly to operator?
