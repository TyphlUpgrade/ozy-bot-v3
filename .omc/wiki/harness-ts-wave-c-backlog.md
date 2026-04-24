---
title: Harness-TS Wave C Backlog (deferred from 2026-04-24 session)
category: architecture
tags: ["harness-ts", "wave-c", "backlog", "three-tier"]
updated: 2026-04-24
---

# Wave C Backlog — Status + Remaining Action List

**Second-session status (2026-04-24 cont'd):** bugs 1+2, SEC M1/M2, CR M2, U3, U4, and the 3-phase live stress have all landed. Remaining items are live-API spikes (4, 5) + deferred Wave D (U5) + on-arrival CR M1.

## Context

Live end-to-end project run (commit `8e11a3b`, `scripts/live-project.ts`) passed 5/5 checks with `$0.69` total cost in 66.8s. BUT surfaced two production bugs mocks never caught. Plan M.15 has full detail:
`.omc/plans/ralplan-harness-ts-three-tier-architect.md` section M.15.

## ✅ Completed items (commits + evidence)

| # | Item | Commits | Evidence |
|---|------|---------|----------|
| 1 | Bug 1 — `TaskFile.projectId` propagation | `d2388b3` | +2 tests; live-run `review_mandatory` now fires on project phases |
| 2 | Bug 2 — Project auto-completion | `d2388b3` | +2 tests; 3-phase stress `project state=completed` + `project_completed` event |
| 3 | 3-phase real-SDK stress (`scripts/live-project-3phase.ts`) | `7d4e7ed` | PASS 7/7 checks, `$0.75`, 177s elapsed |
| SEC M1 | `validateArchitectCompactionSummary` full schema check | `7d4e7ed` | +6 tests; malformed → fallback to projectStore summary |
| SEC M2 | Architect disallowlist (network + cron + team) | `7d4e7ed` | +1 test asserting Task NOT blocked |
| CR M2 | `finalCostUsd` aggregated from StateManager | `7d4e7ed` | +1 test asserting cost round-trip |
| U3 | Executor enrichment default (`DEFAULT_EXECUTOR_SYSTEM_PROMPT`) | *this ralph session* | +2 tests; SessionManager falls through to enriched prompt when `config.systemPrompt` unset |
| U4 | persistSession accumulation observability | *this ralph session* | `persistentSessionCount` accessor + `persistent_session_warn_threshold` config + 3 tests |

Cumulative test count: 484 → **500** (+16). Live-run cost this turn: `$0.75` (3-phase stress).

## 🔓 Remaining items

### 4. Caveman × structured JSON on Executor (live spike, ~`$1`)

Risk: Reviewer spike M.11.5 showed caveman corrupting structured verdict JSON. Executor writes `completion.json` with the same shape — same risk surface. First two live runs (minimal + enriched + 3-phase) were clean, but sample size is small.

**Observable acceptance threshold:** ≥ 95% field-preservation across 5 independent Executor runs against a strict-JSON completion contract. Count every top-level schema field (`status`, `commitSha`, `summary`, `filesChanged`, `understanding`, `assumptions`, `nonGoals`, `confidence`) + every `confidence.*` sub-field. Below 95% → drop caveman from Executor defaults and fall back to the validated enriched prompt alone.

**Spike execution plan:**
- Proposed path: `scripts/spike-caveman-json.ts`
- Shape: 5 parallel invocations of `live-run.ts --mode enriched` equivalents, each using a slightly different trivial task prompt to prevent prompt caching from skewing results. Record each completion.json and diff against the schema.
- Budget ceiling: `$1` total. Per-run orchestrator cap: `$0.25`.
- One-command invocation: `npx tsx scripts/spike-caveman-json.ts` (script will stream pass/fail per run and print the 8-field preservation ratio).
- Decision output: committed as a new section in `ralplan-harness-ts-three-tier-architect.md` M.15.x.

### 5. OMC plugin dead-weight on Executor (live spike, informed by #4)

Per M.13.3, single-mode Executors don't invoke OMC specialists unprompted. Loading OMC adds init overhead for no benefit. The 3-phase live run corroborated: Executor never delegated.

**Observable acceptance threshold:** ≥ 20% init-overhead reduction on Executor cold-start wall-time when OMC plugin disabled, with zero regression on completion compliance. Keep OMC for Architect (decomposer, M.12 validated) and future parallel-Reviewer (M.14).

**Spike execution plan:**
- Proposed path: `scripts/spike-omc-overhead.ts`
- Shape: 3 Executor runs with `plugins: { "oh-my-claudecode@omc": false }` against the same trivial task; 3 runs with OMC enabled; measure wall-clock from spawn → first completion.json write. Compare medians.
- Budget ceiling: `$0.50` total (runs are cheap without OMC init + Haiku-equivalent Sonnet).
- One-command invocation: `npx tsx scripts/spike-omc-overhead.ts`.
- Decision output: if threshold met, flip `DEFAULT_PLUGINS["oh-my-claudecode@omc"]` to `false` in `src/session/manager.ts` for Executor; operators override via `config.pipeline.plugins`.

### U5 — Budget tuning (deferred to Wave D)

Live runs used `~9×` under the per-phase cap ($0.11 actual vs $1 cap). Graduated caps by Architect-declared phase complexity would fit better (e.g. phase.complexity = "trivial" → $0.25; "standard" → $1.00; "complex" → $3.00). Defer to Wave D; no urgency while project-cost aggregation is accurate (CR M2 fixed that).

### CR M1 — `buildSessionConfig` helper (on-arrival)

SessionManager + ReviewGate + ArchitectManager all hand-assemble `SessionConfig`. Extract a shared helper when a fourth caller arrives. Not blocking Wave C; deliberately held until extraction delivers de-duplication leverage (currently 3 callers, marginal).

## Session summary (rolling)

- First-session commits: 32 (Waves 1 / 1.5 / 1.75 item 9 / 2 / 3 / A / B).
- This ralph session: Wave C hardening + U3 + U4.
- Test count: 280 → 484 (session 1) → **500** (session 2, +16 this turn).
- Live runs: 6 total, all PASS.
- Real bugs found: 2 (both closed this turn).
- Cumulative live-run API cost across both sessions: ~`$1.95`.

## Plan references

- Section M.15 (live run): `.omc/plans/ralplan-harness-ts-three-tier-architect.md`
- Wave C scope: plan Section F Wave C
- Spike configs: plan Section M.13.4
