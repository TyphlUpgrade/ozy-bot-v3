# Decomposer Spike

Pre-Wave-B.5 empirical validation of Architect decomposition mode. Tests plan M.8's unvalidated claim that decomposer benefits from OMC subagents + whether caveman helps/hurts prose-heavy JSON output.

## Files

| File | Purpose |
|------|---------|
| `prompt.md` | Architect Decomposer systemPrompt — distinct from arbiter prompt. Divergent-reasoning mode. Includes prose-conciseness directive within JSON fields. |
| `projects.json` | 2 projects: `medium` (redis-job-queue migration, expected 6-12 phases) + `large` (postgres→cockroach migration, expected 18-28 phases). |
| `run.ts` | Runner — fresh session per project. 4 variants via `ENABLE_OMC` / `ENABLE_CAVEMAN`. Tracks subagent (Task/Skill) invocations by scanning assistant messages for tool_use blocks. |
| `tsconfig.json` | Isolated typecheck |
| `RESULTS.md` | Post-run analysis template |

## Key Measurements (per run)

- Phase count (actual vs expected range)
- Non-goals preserved (operator-declared) vs added (Architect-surfaced)
- Critical path length
- Dependency graph validity (no orphan deps, no missing phases)
- Decomposition rationale presence
- **Subagent invocation count** (primary signal — plan M.8 hypothesis)
- Cost, latency, turns, input/output tokens

## Variants (toggle `ENABLE_OMC` + `ENABLE_CAVEMAN` in run.ts, re-run, archive)

| # | Config | Hypothesis |
|---|--------|------------|
| 1 | bare | Baseline decomposition quality |
| 2 | +caveman | Prose compression + JSON bypass — does it help or drop nuance in phase specs? |
| 3 | +OMC | Does decomposer invoke planner/architect/team subagents? (0 = same pattern as arbiter+reviewer; ≥1 = OMC earns cost here) |
| 4 | +both | Production target (currently Wave 1 default) |

## Budget

`maxBudgetUsd: $5.00` per project (decomposition is more expensive than arbitration — larger output, more reasoning). 2 projects × 4 variants = 8 runs. Expected total: $10-20.

Soft cap on variant total $5-8. If bare medium is $1.50, OMC medium could be $3-5. Watch closely.

## Run

```bash
cd harness-ts
npx tsx spikes/decomposer-spike/run.ts
```

Between variants:
```bash
mv spikes/decomposer-spike/results spikes/decomposer-spike/results-{variant}
mv spikes/decomposer-spike/run.log spikes/decomposer-spike/run-{variant}.log
# edit run.ts constants
```

## Typecheck

```bash
npx tsc -p spikes/decomposer-spike/tsconfig.json
```

## Kill / Proceed Criteria

| Metric | KILL | PROCEED |
|--------|------|---------|
| Plan parse errors | any | 0 |
| Phase count (medium) | <4 or >20 | 6-12 expected |
| Phase count (large) | <12 or >40 | 18-28 expected |
| Non-goals preservation | <80% of operator list | 100% operator preserved |
| Dependency graph validity | any invalid | all valid |
| Subagent invocations (OMC variant) | 0 on medium AND large | ≥1 on medium OR ≥3 on large |
| Avg cost | >$4 per project | ≤$3 per project |
| Avg latency | >15 min | ≤5 min |

**Primary decision the spike answers:**
- If OMC variant shows ≥1 subagent invocation on medium AND ≥3 on large → **OMC earns cost for decomposer**. Plan Wave B keeps OMC enabled for decomposition mode.
- If OMC variant shows 0 invocations across both projects → **OMC is dead weight across all Architect modes**. Plan updates to lock bare sonnet for decomposer too. Section M amended.

## After Run

1. Inspect `results/summary.json` for aggregate per variant.
2. Read `results/raw-{medium,large}.json` for full plans + subagent invocation details.
3. Manual review of produced plan.json:
   - Does the phase breakdown look reasonable to an experienced engineer?
   - Are acceptance criteria testable?
   - Are decision boundaries well-scoped?
   - Does the critical path match the project's natural bottlenecks?
4. Compare across variants: where does quality diverge? Where does caveman compress too far?
5. Pick production config for Architect decomposer (plan Section M amendment).

## What This Spike Does NOT Test

- Arbitration mode (already tested in architect-spike)
- Real Executor consumption of produced phase task files
- Multi-project concurrency (decomposer spawns per project; spike runs sequential)
- Plan amendment (Architect handles via arbitration mode, not decomposition)
- Long-running session persistence (decomposition is one-shot by design)
