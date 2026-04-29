---
title: Harness-TS Architecture
description: Core architecture concepts (modules, state machine, merge queue, completion signal, business logic, injectable interfaces). Phase delivery history split to harness-ts-phase-roadmap.
tags: [harness-ts, architecture, claude-agent-sdk, typescript, pipeline]
category: architecture
created: 2026-04-11
updated: 2026-04-27
---

# Harness-TS Architecture

TypeScript rewrite of the development harness, built on `@anthropic-ai/claude-agent-sdk`. Replaces the Python asyncio/FIFO/clawhip/signal-file stack with SDK-native agent lifecycle management.

## Vision

Same as Python harness: an **automated dev team** where an operator describes intent and agents do the work. The difference is how agent sessions are managed — SDK `query()` replaces FIFO pipes, tmux, and signal files.

**What changed:**
- FIFO sessions + clawhip tmux → SDK `query()` with `AsyncGenerator<SDKMessage>`
- 7-stage pipeline (classify→architect→executor→reviewer→merge→wiki) → supervised single-session model (agent works → completion signal → merge gate)
- Signal files for escalation/completion → `completion.json` in worktree + future escalation.json
- Python asyncio → Node.js event loop + vitest

**What stayed:**
- Git worktree isolation per task
- Merge queue with rebase-before-merge, test-and-revert
- 9-state task machine with atomic persistence
- Operator communicates via Discord (Phase 2)
- OMC hooks loaded via `settingSources: ["project"]`

## Two-Layer Stack

```text
TypeScript orchestrator (daemon) — task routing, state machine, merge queue
    |
Claude Agent SDK sessions (work) — full CC+OMC sessions doing actual dev work
```

No clawhip layer. No tmux. The SDK handles session lifecycle directly.

## Module map

> **For the comprehensive per-file breakdown** (8770 LOC, 5 layers, recent commits, cross-cutting themes): see [[harness-ts-architecture-snapshot-2026-04-27-as-built]].

Layer summary:
- `src/lib/` — cross-cutting primitives (config, state, project, escalation, budget, checkpoint, response, text, types)
- `src/session/` — agent lifecycle (sdk, manager Executor tier, architect Architect tier)
- `src/gates/` — merge + review gates
- `src/discord/` — Discord I/O (notifier, sender, gateway, dispatcher, intent-classifier, response-generator, identity, epistle-templates, message-context, channel-context, accumulator, commands, sender-factory, types, client, identity-map)
- `src/orchestrator.ts` — daemon, sole event bus (1084 LOC, 27-event `OrchestratorEvent` union)

## Supervised Session Model

The orchestrator is a **daemon managing long-running CC+OMC sessions**, not a pipeline scheduler. Each task gets one agent session in an isolated worktree. The agent's internal workflow (planning, coding, testing) is opaque to the orchestrator.

```text
Task file dropped → Orchestrator picks up
    → Creates worktree + branch (harness/task-{id})
    → Spawns SDK session with prompt + systemPrompt
    → Agent works (opaque — full CC+OMC capabilities)
    → Agent writes .harness/completion.json
    → Orchestrator reads completion signal
    → Routes to merge gate
    → Merge gate: rebase → merge --no-ff → test → done or revert
```

## 9-State Task Machine

```
pending → active → merging → done
                 ↘ failed ↙
         active → reviewing → merging
         active → shelved → pending (retry)
         active → escalation_wait → active (resume)
         active → paused → active (resume)
```

States: `pending`, `active`, `reviewing`, `merging`, `done`, `failed`, `shelved`, `escalation_wait`, `paused`

Transitions enforced by `VALID_TRANSITIONS` record. Atomic persistence via temp-file + rename (O3).

## Merge Queue

Exclusive FIFO. One merge at a time. Pipeline per merge:

1. **Auto-commit** (O7): if worktree has uncommitted changes, `git add --all -- ':!.omc' ':!.harness'` + commit
2. **Rebase**: `git rebase {trunk}` in worktree. Conflict → abort + shelve + schedule retry
3. **Merge**: `git merge --no-ff {branch}` on trunk
4. **Test**: run `test_command` with `test_timeout` (O8). Failure → revert merge
5. **Result**: merged | rebase_conflict | test_failed | test_timeout | error

## Completion Signal

Agent writes `{worktree}/.harness/completion.json`:

```json
{
  "status": "success",
  "commitSha": "abc123",
  "summary": "Fixed the auth bug",
  "filesChanged": ["src/auth.ts", "tests/auth.test.ts"]
}
```

Strict validation: status must be "success" or "failure", commitSha non-empty, all fields required.

## Business Logic Lessons Preserved

| ID | Rule | Implementation |
|----|------|---------------|
| B1 | Sync mutations only | `writeFileSync` + `renameSync` atomic writes |
| B3 | Shelve clock reset | `shelvedAt` set on shelve, cleared on unshelve |
| B5 | Resume at executor | `pre_escalation_stage` captured before escalation |
| B6 | Escalation tier reset | `fromTier` captured before mutation, retryCount reset |
| B7 | Unknown key drop | `KNOWN_KEYS` set, unknown keys silently dropped on deserialize |
| O3 | Atomic writes | UUID temp file + rename |
| O4 | Path traversal | `sanitizeTaskId()` regex `/^[a-zA-Z0-9_-]+$/`, max 128 chars |
| O7 | Auto-commit | Pathspec excludes `':!.omc' ':!.harness'` |
| O8 | Test timeout | Configurable `test_timeout` passed to `runTests()` |
| O9 | Write-only log | JSONL append-only event log |

## Injectable Interfaces

All external dependencies are injectable for testing:

| Interface | Real implementation | Test mock |
|-----------|-------------------|-----------|
| `QueryFn` | SDK `query()` | Returns mock `AsyncGenerator` |
| `GitOps` | `execSync` git commands | `vi.fn()` mocks |
| `MergeGitOps` | `execSync` git commands | `vi.fn()` mocks |

## Configuration

`config/harness/project.toml` — shared between Python archive and TS harness.

Key pipeline settings: `poll_interval` (5s), `test_command`, `max_retries` (3), `test_timeout` (180s), `escalation_timeout` (4h), `retry_delay_ms` (5min).

## Test Coverage

**763 tests across 40 files** (post-Wave-E-α + spike fixes 2026-04-27). For the per-suite breakdown see [[harness-ts-architecture-snapshot-2026-04-27-as-built]] — that page tracks current totals; this page no longer lists individual suites.

## Ambiguity Protection (Current State)

**What exists:** task ID validation, JSON schema check, budget/turn caps, completion signal requirement, test-and-revert merge gate, Reviewer gate (mandatory-for-project), Architect session crash recovery, no-discord-leak architectural guard.

**What's missing (Phase 3+):** standalone dialogue agent (proposal.json pattern), pre-pipeline dialogue Discord channel, semantic progress watchdog, meta-phase nudge protocol.

For full phased delivery history (Phase 0+1 → Phase 4) and pending waves (B.5 / 4 / C / 6 / D / E-β / E-γ / E-δ), see [[harness-ts-phase-roadmap]].

## Phase Roadmap

Moved to [[harness-ts-phase-roadmap]] (split 2026-04-27 for size policy compliance).

Quick status snapshot:
- **Phase 0+1 + 1.5** — LANDED (`2298ad1`)
- **Phase 2A** — LANDED (273 tests, `.omc/plans/ralplan-harness-ts-phase2a.md`)
- **Phase 2B** — PARTIAL via three-tier-architect (Waves 1/1.5/1.75/2/3/A/B + Discord rich rendering + Wave E-α landed; B.5/4/C/6/D + E-β/γ/δ pending)
- **Phase 3** — NOT STARTED (Wave A delivered partial review gate; standalone dialogue agent pending)
- **Phase 4** — NOT STARTED (semantic stall detection design surfaced from autopilot Wave 2/3 debrief 2026-04-24)


## Cross-References

- [[harness-ts-architecture-snapshot-2026-04-27-as-built]] — comprehensive per-file as-built map (8770 LOC, 5 layers, recent commits, cross-cutting themes)
- [[harness-ts-phase-roadmap]] — phase-by-phase delivery history (split from this page 2026-04-27)
- [[harness-ts-types-reference-source-of-truth]] — verbatim type signatures
- [[harness-ts-core-invariants]] — load-bearing rules (read FIRST)
- [[harness-ts-common-mistakes]] — repeated mistakes catalog
- [[harness-ts-plan-index]] — index of `.omc/plans/` files
- [[harness-ts-ambiguity-protections]] — Prior art analysis and TS translation
- [[harness-ts-graduated-response]] — Signal-driven escalation levels, structured confidence
- [[v5-harness-efficiency-proposal]] — Design rationale for the TS rewrite
- [[v5-harness-supervised-session-architecture]] — Supervised session model design doc
- [[v5-harness-lessons-learned]] — Institutional knowledge extracted before rewrite
- [[v5-harness-architecture]] — HISTORICAL: Python harness architecture
