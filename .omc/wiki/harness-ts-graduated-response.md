---
title: Harness-TS Graduated Response Model
tags: [harness-ts, architecture, confidence, escalation, dialogue-agent, design-decision]
category: decision
created: 2026-04-11
updated: 2026-04-11
---

# Graduated Response Model

Design decision from 2026-04-11 session. Resolves the core tension between autonomy, control, budget, and reasoning quality in the harness pipeline.

## The Core Tension

Every harness design decision sits on one axis:
```
FAST/CHEAP/AUTONOMOUS ←————————————→ SAFE/EXPENSIVE/CONTROLLED
```

Fixed positions fail. Far left (current TS): great for "fix the typo", burns budget on ambiguous greenfield. Far right (Python pipeline): great for "redesign the cache", absurdly wasteful for trivial fixes. OMC autopilot: fixed phase ladder, every task gets same treatment within a phase.

## Decision: Signal-Driven Escalation Levels

The system defaults to the cheapest effective posture and escalates based on **signals**, not fixed rules.

| Level | Name | What happens | Default trigger |
|-------|------|-------------|----------------|
| 0 | Direct | Agent works → merge gate → done | Task has concrete anchors |
| 1 | Enriched | Agent declares understanding + assumptions in completion | All tasks (free — prompt engineering) |
| 2 | Reviewed | External review session before merge | High cost, large diff, agent partial confidence |
| 3 | Dialogue | Agent pauses, asks operator, resumes | Agent can't resolve, open questions |
| 4 | Planned | Plan → approve → implement → review → merge | Operator declares, or multiple unclear dimensions |

The agent doesn't choose the level. Assessment dimensions determine it automatically.

## Structured Confidence Assessment (Not Raw Scores)

Raw "confidence: 0.7" is useless — LLMs cluster around the same values. Instead, decomposed criteria with specific questions, following the pattern from the Python reviewer's contrarian pressure test.

### Five Assessment Dimensions

1. **SCOPE CLARITY** — Do you know exactly what to change?
   - `clear`: specific files, functions, or behaviors identified
   - `partial`: general area known, specifics require investigation
   - `unclear`: task could mean multiple different things

2. **DESIGN CERTAINTY** — Is there one obvious approach?
   - `obvious`: one approach matches existing patterns
   - `alternatives_exist`: multiple valid approaches, choosing one
   - `guessing`: no clear basis for the choice being made

3. **ASSUMPTIONS** — List every decision not explicitly requested by operator
   - Each rated: `impact` (high/low), `reversible` (yes/no)

4. **OPEN QUESTIONS** — Anything unresolvable from codebase or task alone

5. **TEST COVERAGE** — Can changes be verified?
   - `verifiable`: tests exist or can be written
   - `partial`: some behavior testable, some not
   - `untestable`: core behavior can't be automatically verified

### Escalation Rules

- Any dimension at worst rating → write `.harness/escalation.json`
- Any high-impact irreversible assumption → write `.harness/escalation.json`
- Any open question → write `.harness/escalation.json`
- Multiple "partial" ratings → declare in completion, operator reviews

### Anti-Clustering Directive

> "Do NOT default to middle ratings. 'partial' means you have specific evidence of partial clarity. If you can name the unclear parts, it's partial. If you can't, it's unclear."

Forces the agent to commit to "clear" (with evidence) or admit "unclear" (with what's missing).

## Mid-Task Confidence Changes

Confidence can degrade during implementation. Two mechanisms:

**Event-driven (Mechanism A):** Agent reassesses whenever it discovers a decision point not covered by the task. Writes `.harness/checkpoint.json` or `.harness/escalation.json` depending on severity.

**Budget-driven (Mechanism B):** Agent reassesses at 25% and 50% budget consumption. Catches slow drift where assumptions accumulate without a single clear decision point.

Both mechanisms active simultaneously. Either can trigger escalation.

## Dialogue Agent Pattern (Build-From-Scratch)

For greenfield tasks where operator input before implementation is valuable:

1. Agent analyzes codebase, identifies decision points
2. Writes `.harness/proposal.json` with design choices + open questions
3. Orchestrator pauses for operator review
4. Operator confirms/adjusts
5. Agent implements with confirmed design (full context preserved)

Two implementation options:
- **Two-session**: Dialogue session (cheap) → proposal → operator → implementation session. Clean separation, context loss at handoff.
- **Single-session with pause**: Agent writes proposal → orchestrator pauses → operator responds → session resumes via SDK `resumeSession()`. Zero information loss, depends on resume reliability.

Routing: operator declares `mode: "dialogue"` in task, or auto-triggered when assessment dimensions are degraded at task start.

## Assessment → Level Mapping

| Assessment state | Level | Action |
|-----------------|-------|--------|
| All clear/obvious, no high-impact assumptions | 0 (direct) | Execute → merge |
| All clear/obvious, has assumptions | 1 (enriched) | Execute → declare in completion → merge |
| Any "partial" or "alternatives_exist" | 2 (reviewed) | Execute → external review → merge |
| Any "unclear"/"guessing" or open questions | 3 (dialogue) | Pause → operator → execute |
| Multiple unclear + high-impact assumptions | 4 (planned) | Plan → approve → execute → review |

## Prior Art Preserved

From Python harness agent prompts (`config/harness/agents/*.md`):
- Architect intent classification gate → assessment dimension 1 (scope clarity)
- Decision boundaries in plan → assessment dimension 3 (assumptions inventory)
- Non-goals → completion signal `nonGoals` field
- Executor simplifier pressure test → systemPrompt directive
- Reviewer contrarian pressure test → external review gate prompt (5 weighted risk dimensions)
- Escalation confidence gating → structured assessment replaces raw confidence

## Cross-References

- [[harness-ts-ambiguity-protections]] — Full prior art analysis and translation mapping
- [[harness-ts-architecture]] — Current TS harness architecture (Phase 0+1)
- [[v5-harness-efficiency-proposal]] — Original TS rewrite rationale
