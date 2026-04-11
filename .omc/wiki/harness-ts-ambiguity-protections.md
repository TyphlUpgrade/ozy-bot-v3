---
title: Harness Ambiguity Protections — Prior Art and TS Design
tags: [harness-ts, ambiguity, escalation, prompt-engineering, architecture, dialogue-agent]
category: architecture
created: 2026-04-11
updated: 2026-04-11
---

# Harness Ambiguity Protections

Analysis of the Python harness's layered ambiguity defense and how it translates to the TypeScript single-session model. Informs Phase 2+ design decisions.

## Python Harness: 7-Layer Defense (Prior Art)

The Python multi-session pipeline (classify→architect→executor→reviewer→merge) had ambiguity protections at every stage. Most were **prompt engineering**, not code.

### Layer 1 — Intake Classification (`claude.py:classify`)

Haiku call: "complex or simple?" Defaults to "complex" on failure (safe). Routes complex tasks through architect pre-pass. Not an ambiguity detector — a complexity router.

### Layer 2 — Architect Intent Gate (`agents/architect.md`)

Before planning, architect classifies: `bug | calibration | feature | refactor | analysis`. Each category has different plan structure and checkpoint frequency.

Key constraint: **"If you cannot confidently classify the task, STOP and write a clarification signal. Do not guess."** This is the primary ambiguity gate — forced categorization catches unclear tasks before any implementation budget is spent.

### Layer 3 — Decision Boundaries in Plan

Architect's plan format required:
```json
{
  "decision_boundaries": {
    "executor_decides": ["choices the Executor can make autonomously"],
    "escalate_to_operator": ["choices that require human input"]
  },
  "non_goals": ["what this change explicitly does NOT do"]
}
```

Prevents executor scope creep by scoping decisions before implementation starts.

### Layer 4 — Executor Simplifier Pressure-Test (threshold 0.15)

> "Can we get 80% of this with less code than planned? Is this over-built?"

If implementation is significantly more complex than planned → STOP and checkpoint. Overbuilding is a symptom of unclear scope.

### Layer 5 — Reviewer Contrarian Pressure-Test (threshold 0.25)

Weighted risk score across 5 dimensions: correctness (0.30), integration (0.25), state corruption (0.20), performance (0.15), regression (0.10). Above threshold → mandatory reject. Genuine independent assessment — reviewer context is separate from implementer.

### Layer 6 — Escalation Routing (`escalation.py`)

`ambiguous_requirement` category → Tier 1 (architect first). Confidence gating: anything below "high" confidence → auto-promote to Tier 2 (operator). Safe-by-default: unknown confidence promotes.

### Layer 7 — Resolution Classification (`claude.py:classify_resolution`)

During escalation dialogue: is operator giving a *decision* or still *discussing*? Defaults to "continuation" (safe — keeps dialogue open rather than prematurely resolving).

## TS Harness: Current State (Phase 0+1)

| Protection | Status |
|-----------|--------|
| Task ID validation (O4 path traversal) | Built |
| JSON schema check on task files | Built |
| Budget/turn caps on sessions | Built (via SDK options) |
| Completion signal with explicit status | Built |
| Test-and-revert merge gate | Built |
| Prompt classification | Not built |
| Ambiguity detection | Not built |
| Escalation channel | Not built |
| Independent review | Not built |
| Operator feedback channel | Not built |

Key gap: **merge gate catches broken code (tests fail) but not wrong code (tests pass, intent wrong).** Test-and-revert is structural integrity, not semantic correctness.

## Translation to Single-Session Model

| Python layer | TS mechanism | Type |
|-------------|-------------|------|
| Intake classify | Not needed — agent adapts internally | Eliminated |
| Architect intent gate | systemPrompt directive | Prompt engineering |
| Decision boundaries | systemPrompt directive | Prompt engineering |
| Non-goals | completion.json `nonGoals` field | Signal contract |
| Simplifier pressure test | systemPrompt directive | Prompt engineering |
| Contrarian review | External review session (Phase 3) | Separate session |
| Escalation routing | `.harness/escalation.json` protocol | File protocol |
| Resolution classify | Discord integration (Phase 2) | Code |

Core insight: **most ambiguity protection was prompt engineering, not pipeline structure.** The agent prompts in `config/harness/agents/*.md` did the heavy lifting. The pipeline enforced gates (can't code without plan, can't merge without verdict) but the quality came from the prompts.

The one thing that genuinely requires a second session is **independent review** (Layer 5). Self-review has confirmation bias. A fresh context catches things the implementer is blind to.

## Proposed Completion Signal Extension

```typescript
interface CompletionSignal {
  status: "success" | "failure";
  commitSha: string;
  summary: string;
  filesChanged: string[];
  // Ambiguity protection fields
  understanding?: string;    // "I interpreted this as..."
  assumptions?: string[];    // Decisions made without explicit instruction
  nonGoals?: string[];       // What was explicitly NOT done
  confidence?: "high" | "medium" | "low";
}
```

`confidence: "low"` → orchestrator pauses before merge, emits `review_needed` event.

## Proposed Escalation Protocol

Agent writes `.harness/escalation.json` when stuck:
```json
{
  "type": "clarification_needed",
  "question": "Per-IP or per-user rate limiting?",
  "context": "Found both patterns in codebase",
  "options": ["per-IP (simpler)", "per-user (requires auth middleware)"]
}
```

Orchestrator detects → transitions to `escalation_wait` → notifies operator (Phase 2: Discord) → operator responds → session resumes.

## Multi-Session vs Single-Session Trade-off

**Multi-session (Python) advantages:**
- Hard tool boundaries (architect can't code, reviewer can't edit)
- Forced plan articulation before implementation (structural, not advisory)
- Independent review from fresh context (no confirmation bias)
- Checkpoint interrupts with external visibility
- Signal file audit trail

**Single-session (TS) advantages:**
- Zero information loss (continuous reasoning context)
- Adaptive workflow (agent decides approach based on complexity)
- Speed (no handoff latency, no poll cycle waits)
- CC+OMC already orchestrates agents internally
- Infrastructure simplicity (6 modules vs Python's ~2,500 lines of session mgmt)

**Key loss in single-session:** The forced-plan-before-code constraint. In multi-session, it was structurally impossible to start coding without `plan.json`. In single-session, it's a prompt directive (soft constraint).

**Key gain in single-session:** Context preservation. The Python model lost information at every handoff — architect's reasoning didn't survive into executor's context.

## Hybrid Model (Recommended for Phase 2+)

Default path (most tasks): single session → completion → merge. Fast.

Gated triggers for external review:
- `totalCostUsd > threshold` (expensive work warrants second opinion)
- `filesChanged.length > threshold` (large blast radius)
- `confidence: "low"` in completion (agent self-identifies uncertainty)
- `mode: "dialogue"` in task file (operator requests pre-implementation review)
- `mode: "cautious"` in task file (two-phase: plan → approve → implement)

## Dialogue Agent Pattern (Build-From-Scratch)

For greenfield/ambiguous tasks where operator input before implementation is valuable:

1. Agent analyzes codebase, identifies decision points
2. Agent writes `.harness/proposal.json` with design proposal + open questions
3. Orchestrator pauses for operator review
4. Operator confirms/adjusts via Discord
5. Agent implements with confirmed design (full context preserved)

Two implementation options:
- **Two-session**: Dialogue session (cheap, read-only) → proposal → operator → implementation session (full). Clean separation, but context loss at handoff.
- **Single-session with pause**: Agent writes proposal mid-session → orchestrator pauses → operator responds → session resumes. Zero information loss, but depends on SDK `resumeSession()` reliability.

Routing: operator declares `mode: "dialogue"` in task, or orchestrator auto-detects (no file references, no function names → dialogue path). Operator knows when they need to be in the loop.

## Phase Roadmap for Ambiguity Protections

| Phase | Addition | Effort |
|-------|---------|--------|
| 2 | systemPrompt with intent gate + pressure tests + decision boundaries | Prompt-only |
| 2 | Completion signal enrichment (understanding, assumptions, confidence) | ~20 lines |
| 2 | Escalation write-back protocol (.harness/escalation.json) | ~50 lines |
| 2 | Budget alarm events (50%, 80% thresholds) | ~10 lines |
| 2 | Discord integration for escalation notifications | Phase 2 scope |
| 3 | External review gate (separate read-only session, contrarian prompt) | ~100 lines |
| 3 | Dialogue agent pattern for build-from-scratch tasks | ~80 lines |

## Cross-References

- [[harness-ts-architecture]] — Current TS harness architecture
- [[v5-harness-architecture]] — HISTORICAL: Python harness with multi-session pipeline
- [[v5-harness-lessons-learned]] — Institutional knowledge from Python harness
- [[v5-harness-efficiency-proposal]] — Design rationale for TS rewrite
