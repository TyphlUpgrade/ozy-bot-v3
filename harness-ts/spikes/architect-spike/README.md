# Architect Spike / Regression Harness

Originally a minimum-viable empirical test of the three-tier Architect hypothesis. **Spike phase complete** (2026-04-23). This directory is now preserved as the **regression harness for Wave B** of the approved plan at `.omc/plans/ralplan-harness-ts-three-tier-architect.md` (see Section M for full spike validation results).

## Files

| File | Purpose |
|------|---------|
| `prompt.md` | Candidate production Architect systemPrompt (retry-only authority, 3 verdict types, safe-by-default, non-goals preservation, anti-patterns). Wave B may refine. |
| `escalations.json` | 5 mixed ephemeral scenarios: 3 executor_escalation (scope_unclear, design_decision, blocked) + 2 review_arbitration. Distinct projects per escalation. |
| `escalations-persistent.json` | 10-phase coherent project (`alerts-migration-v2`) with cross-call memory tests (p5→p2+p4, p7→p4, p9→p6, p10→p1). |
| `run.ts` | Runner supporting both ephemeral and persistent modes + `ENABLE_OMC` / `ENABLE_CAVEMAN` flags. |
| `tsconfig.json` | Isolated typecheck config (does not pollute main `src/` build). |
| `results-{variant}/` | Archived per-variant results from spike evaluation (5 variants). |
| `run-{variant}.log` | Raw stdout per variant. |
| `RESULTS.md` | Original spike RESULTS template (retained for reference). |

## Archived Spike Results

| Dir | Variant | Resolution | Avg cost | Notes |
|-----|---------|-----------|----------|-------|
| `results-opus/` | Bare opus-4-7 ephemeral, 5 escalations | 60% | $0.526 | Opus over-spec'd |
| `results-sonnet-bare/` | Bare sonnet-4-6 ephemeral, 5 escalations | 80% | $0.096 | Clean baseline |
| `results-sonnet-omc/` | Sonnet + OMC plugin ephemeral, 5 escalations | 80% | $0.149 | OMC subagents never invoked — dead weight |
| `results-sonnet-caveman/` | Sonnet + caveman plugin ephemeral, 5 escalations | 80% | $0.106 | Caveman bypasses structured output |
| `results-sonnet-persistent/` | **Bare sonnet PERSISTENT, 10 escalations, 1 session** | **90%** | **$0.058** | **Winning configuration** — cross-call memory validated |

Total spike cost: $4.96.

Full findings in plan Section M.

## Wave B Regression Gate (per plan M.7)

Wave B Architect implementation MUST pass the following before ship:

### Gate 1: Ephemeral mode (5 escalations, `escalations.json`)
- Resolution rate ≥ 60%
- 0 parse errors
- Retry-only authority: 0 `executor_correct` verdicts (enforced by schema)

### Gate 2: Persistent mode (10 escalations, `escalations-persistent.json`)
- Resolution rate ≥ 80% (spike v5 achieved 90%)
- Total cost ≤ $1.00 (spike v5 was $0.58; 40% headroom)
- Cross-call memory demonstrated on ≥ 3 of 4 memory-test phases (p5, p7, p9, p10). "Demonstrated" means Architect rationale explicitly references the prior phase ID or its decision content.
- 0 parse errors

Both gates run against the Wave B production `ArchitectManager` (not via this spike's `run.ts`). The `run.ts` here stays as the reference implementation only.

## Production Configuration (locked via spike)

```toml
[architect.arbitration]         # hot path
model = "claude-sonnet-4-6"
max_budget_usd = 1.0
max_turns = 10
enabled_plugins = []             # bare — OMC + caveman both counterproductive
persistSession = true
allowed_tools = ["Read", "Write"]
disallowed_tools = [
  "Bash", "Edit", "WebFetch", "WebSearch",
  "CronCreate", "CronDelete", "CronList",
  "RemoteTrigger", "ScheduleWakeup", "TaskCreate"
]

[architect.decomposition]        # cold path — UNVALIDATED
model = "claude-sonnet-4-6"
max_budget_usd = 5.0
enabled_plugins = ["oh-my-claudecode@omc"]
```

## Prerequisites

- Node 22+
- Anthropic API auth (env or Claude Code session auth)
- From `harness-ts/` root: `npm install` already done

## Re-Running a Variant

```bash
cd harness-ts

# Edit constants in run.ts:
#   MODEL, ENABLE_OMC, ENABLE_CAVEMAN, PERSISTENT_MODE

# Optionally archive current results:
#   mv spikes/architect-spike/results spikes/architect-spike/results-<name>

# Re-run:
npx tsx spikes/architect-spike/run.ts
```

First invocation installs `tsx` on demand. To pre-install: `npm install -D tsx`.

## Typecheck

```bash
npx tsc -p spikes/architect-spike/tsconfig.json
```

Zero output expected (noEmit=true).

## Kill/Proceed Criteria (historical — spike phase)

| Metric | KILL | PROCEED |
|--------|------|---------|
| Resolution rate | < 40% | ≥ 60% |
| Directive usefulness | < 40% useful | ≥ 60% useful |
| Avg cost per call | > $0.50 | ≤ $0.30 |
| Avg latency per call | > 120s | ≤ 60s |

Spike v5 (winning variant) hit all PROCEED thresholds. Plan executed to Section M amendment. Gates above now retained as Wave B regression thresholds.

## Gaps (not tested by this harness)

- **Decomposition mode** — only arbitration tested. Wave B.5 smoke gate is the first decomposition validation.
- **Reviewer/Executor integration** — spike simulates only Architect's input (synthetic escalations + reviewer verdicts). Real Reviewer session not spawned.
- **Merge gate integration** — no real merges happen.
- **State machine + orchestrator coordination** — spike bypasses state machine entirely.
- **Long-lived session beyond 10 calls** — spike persistent maxed at 10. Wave D compaction fidelity test covers longer runs.

## Cleanup

After Wave B lands and regression passes:

```bash
# Archive entire spike dir
mv spikes/architect-spike .omc/archive/architect-spike-2026-04-23

# Or keep in-repo as ongoing regression suite
```

Don't delete `escalations.json` or `escalations-persistent.json` — they're the regression scenarios. The runner (`run.ts`) can be superseded by a Wave B test file that calls the real `ArchitectManager`.
