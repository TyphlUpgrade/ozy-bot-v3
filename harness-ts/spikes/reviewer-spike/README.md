# Reviewer Spike

Pre-Wave-A empirical validation of Reviewer tier configuration. Mirrors `architect-spike/` methodology but for Reviewer (which per plan is ephemeral per-review, not persistent).

## Files

| File | Purpose |
|------|---------|
| `prompt.md` | Candidate Reviewer systemPrompt (contrarian posture, 5-dim risk scoring, acceptance criteria enforcement, read-only authority, structured JSON output) |
| `scenarios.json` | 5 planted-defect review scenarios + 1 clean trivial PR. Each has expected verdict + planted defect description (hidden from the Reviewer). |
| `run.ts` | Runner — fresh session per scenario, 4 variants via `ENABLE_OMC` / `ENABLE_CAVEMAN` constants |
| `tsconfig.json` | Isolated typecheck |
| `RESULTS.md` | Post-run analysis template |

## Scenarios

| ID | Label | Expected Verdict | Planted Defect |
|----|-------|------------------|----------------|
| s1 | `clean_trivial_pr` | `approve` | None — tests false-positive rate |
| s2 | `correctness_bug_off_by_one` | reject/request_changes | `skip = page * pageSize` when spec says 1-indexed |
| s3 | `security_sql_injection` | reject/request_changes | `$queryRawUnsafe` with string concat from user input |
| s4 | `performance_quadratic` | request_changes | O(n²) loop + `.includes()` for 100k-row criterion |
| s5 | `test_coverage_gap` | request_changes | 1 test for 6 criteria, + real memory leak in impl |

Scenarios designed to exercise all 5 risk dimensions + likely OMC subagent pulls:
- s3 invites `security-reviewer` subagent
- s4 invites `code-reviewer` or `analyst` subagent
- s5 invites `test-engineer` subagent
- s1 invites nothing — clean pass

## Variants

| # | Name | ENABLE_OMC | ENABLE_CAVEMAN | Hypothesis |
|---|------|-----------|----------------|-----------|
| 1 | bare | false | false | Baseline — sonnet alone |
| 2 | omc | true | false | Does Reviewer invoke specialist subagents? Different from Architect (which invoked 0) |
| 3 | caveman | false | true | Does caveman work on Reviewer structured output? (Architect: no — bypassed JSON) |
| 4 | both | true | true | Production target |

## Budget

`maxBudgetUsd: $1.00` per review, 5 scenarios per variant = ~$5 hard cap per variant.

Expected total: ~$2-3 per variant (bare ≈ $0.50, OMC ≈ $1-2). 4 variants = $10-15 total.

## Run

```bash
cd harness-ts
npx tsx spikes/reviewer-spike/run.ts
```

Change `ENABLE_OMC` / `ENABLE_CAVEMAN` constants in run.ts for each variant. Archive results per variant:

```bash
mv spikes/reviewer-spike/results spikes/reviewer-spike/results-{variant}
mv spikes/reviewer-spike/run.log spikes/reviewer-spike/run-{variant}.log
```

## Typecheck

```bash
npx tsc -p spikes/reviewer-spike/tsconfig.json
```

## Kill / Proceed Criteria

| Metric | KILL | PROCEED |
|--------|------|---------|
| Verdict accuracy (expected-vs-actual) | < 3/5 (60%) | ≥ 4/5 (80%) |
| Planted-defect detection | s3 or s4 missed | s1-s5 all caught (s2-s5 flagged correctly) |
| Subagent invocation (OMC variant) | 0 across 5 scenarios | ≥ 1 relevant invocation (s3 security, s4 perf, s5 tests) |
| Avg cost per review | > $0.50 | ≤ $0.30 |
| Avg latency | > 90s | ≤ 60s |
| Parse errors | any | 0 |
| Retry-only authority | verdict outside {approve, reject, request_changes} | all verdicts in the set |

## Post-Run

1. Inspect `results/summary.json` — accuracy, cost, latency per scenario.
2. Read each `results/raw-{id}.json` for full verdict text + findings.
3. Manual grade each verdict against planted defect:
   - Did Reviewer catch the exact planted defect?
   - Did Reviewer invent false-positive findings?
   - Were risk scores justified per dimension or lazy-uniform?
4. For OMC variant: check for evidence of Task tool invocation in turn counts (>4 turns likely means subagent spawn).
5. Pick production config: model + plugins + prompt refinements.

## Findings Feed Into

Wave A (`ReviewGate` implementation). Findings will be appended to plan Section M (spike validation) alongside Architect spike results.

## Not Tested by This Spike

- Real codebase Read access (scenarios embed diff + code in prompt; production Reviewer could Read files to investigate)
- Multi-round review loop (Executor↔Reviewer rejection iterations)
- Integration with merge gate
- Performance under concurrent reviews

Wave A's own tests cover those.
