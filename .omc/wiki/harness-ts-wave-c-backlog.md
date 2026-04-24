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

Cumulative test count: 484 → **500** (+16). Live-run cost this turn: `$0.75` (3-phase stress) + `$1.221` (spikes 4 + 5).

### 4. Caveman × structured JSON on Executor — ✅ PASS (100%)

**Result:** 5 Executor runs through `scripts/spike-caveman-json.ts`, each with a distinct trivial file-creation task, caveman + OMC both enabled per production defaults. Every run wrote a complete completion.json.

| runs | top fields (8) | confidence subfields (5) | overall |
|------|----------------|--------------------------|---------|
| 5    | 40/40 (100%)   | 25/25 (100%)             | 65/65 (**100%**) |

Total cost: `$0.607`. Wall range: 23–36s per run.

**Decision:** Caveman safe on Executor structured-JSON contract. No change to `DEFAULT_PLUGINS`. The M.11.5 Reviewer finding does not reproduce at the Executor tier — likely because the enriched prompt fences the schema in a code block that caveman preserves verbatim.

Spike script: `scripts/spike-caveman-json.ts`.

### 5. OMC plugin dead-weight on Executor — ✅ threshold FAIL (3.8% < 20%) — keep OMC

**Result:** 6 Executor runs via `scripts/spike-omc-overhead.ts` (3 with `oh-my-claudecode@omc: true`, 3 with `false`, interleaved to spread cache warming). Caveman kept on for both arms.

| arm     | wall median | cost median | preservation |
|---------|-------------|-------------|--------------|
| OMC ON  | 25891 ms    | $0.119      | 100% (39/39) |
| OMC OFF | 24899 ms    | $0.070      | 100% (39/39) |

Wall reduction (OFF vs ON): **3.8%** — far below the 20% threshold. Total spend: `$0.614`.

**Decision:** KEEP OMC in Executor defaults. Init-overhead savings are negligible; the hypothesis that "OMC loading dominates Executor cold-start" does not hold at the wall-clock level.

**Secondary observation (not a formal threshold):** OMC-OFF token cost ran ~42% lower ($0.070 vs $0.119 median). Sample size is small (3 per arm), and the OFF runs' $0.070 may reflect a cache hit on the smaller plugin surface. Worth re-measuring if per-phase spend becomes a constraint; not urgent while per-phase budget is 9× over-provisioned (U5 territory).

Spike script: `scripts/spike-omc-overhead.ts`.

## 🔓 Remaining items

### U5 — Budget tuning (deferred to Wave D)

Live runs used `~9×` under the per-phase cap ($0.11 actual vs $1 cap). Graduated caps by Architect-declared phase complexity would fit better (e.g. phase.complexity = "trivial" → $0.25; "standard" → $1.00; "complex" → $3.00). Defer to Wave D; no urgency while project-cost aggregation is accurate (CR M2 fixed that).

### CR M1 — `buildSessionConfig` helper (on-arrival)

SessionManager + ReviewGate + ArchitectManager all hand-assemble `SessionConfig`. Extract a shared helper when a fourth caller arrives. Not blocking Wave C; deliberately held until extraction delivers de-duplication leverage (currently 3 callers, marginal).

## Session summary (rolling)

- First-session commits: 32 (Waves 1 / 1.5 / 1.75 item 9 / 2 / 3 / A / B).
- This session: Wave C hardening + U3 + U4 + spikes 4 + 5.
- Test count: 280 → 484 (session 1) → **500** (session 2).
- Live runs: 17 total (6 prior + 5 spike-4 + 6 spike-5), all PASS on compliance.
- Real bugs found: 2 (both closed).
- Cumulative live-run API cost across both sessions: ~`$3.17` ($1.95 prior + $1.22 spikes).

## Spike decisions (Wave C design lock)

- Caveman stays in Executor defaults (100% field preservation).
- OMC stays in Executor defaults (wall reduction 3.8% — below 20% threshold). Token-cost side signal (~42% cheaper without OMC) logged for possible re-measurement under Wave D budget tuning.

## Plan references

- Section M.15 (live run): `.omc/plans/ralplan-harness-ts-three-tier-architect.md`
- Wave C scope: plan Section F Wave C
- Spike configs: plan Section M.13.4
