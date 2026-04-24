# Architect Spike Results

**Run date:** (fill after run)
**Model:** claude-opus-4-7
**Total cost:** $_____
**Run command:** `npx tsx spikes/architect-spike/run.ts`

## Auto-Computed Metrics (from `results/summary.json`)

| Metric | Value | Threshold | Hit? |
|--------|-------|-----------|------|
| Resolution rate | __% | ≥ 60% proceed / < 40% kill | |
| Avg cost per arbitration | $___ | ≤ $0.30 proceed / > $0.50 kill | |
| Avg latency | ___s | ≤ 60s proceed / > 120s kill | |
| Error rate (NO_OUTPUT / PARSE_ERROR) | __ / 5 | 0 expected | |

## Per-Escalation Grades

### Manual Grade (operator)

| ID | Shape | Type | Verdict | Directive/Amendment | Manual Grade | Notes |
|----|-------|------|---------|---------------------|--------------|-------|
| e1 | executor_escalation | scope_unclear | | | useful / garbage / ambiguous | |
| e2 | executor_escalation | design_decision | | | useful / garbage / ambiguous | |
| e3 | executor_escalation | blocked | | | useful / garbage / ambiguous | |
| e4 | review_arbitration | — | | | useful / garbage / ambiguous | |
| e5 | review_arbitration | — | | | useful / garbage / ambiguous | |

**Manual useful count:** __ / 5 = __%

### Second-Pass Grade (independent Claude session)

Protocol: paste `prompt.md` + each `raw-{id}.json` + original escalation into a fresh Claude Opus 4.7 session. Ask: "Given this escalation and this arbitration verdict, is the verdict appropriate? Grade: useful / garbage / ambiguous. Explain in one sentence."

| ID | Second-Pass Grade | Second-Pass Rationale |
|----|-------------------|----------------------|
| e1 | | |
| e2 | | |
| e3 | | |
| e4 | | |
| e5 | | |

**Second-pass useful count:** __ / 5 = __%

### Convergence

| ID | Manual | Second-Pass | Converged? |
|----|--------|-------------|-----------|
| e1 | | | |
| e2 | | | |
| e3 | | | |
| e4 | | | |
| e5 | | | |

**Convergence rate:** __ / 5 = __%

## Decision

Apply pre-committed rule:

- [ ] **PROCEED** — both grades ≥ 60% useful AND resolution rate ≥ 60% AND cost+latency within budget. Execute the 12-wave plan.
- [ ] **KILL** — either grade < 40% useful OR resolution rate < 40% OR cost/latency blown. Archive plan, ship Phase 2B-3 as originally approved.
- [ ] **ITERATE** — ambiguous zone. Run `iterations/v2-prompt.md` revision + re-run spike. Cap at 3 iterations.

**Decision rationale:**

(one paragraph)

## Observations

### Non-Goal Preservation

Did the Architect respect the `nonGoalsPreserved: true` flag correctly?

- e1: __
- e2: __
- e3: __
- e4: __
- e5: __

### Category Classification

Did the Architect's `category` field match the expected category for each input?

| ID | Expected | Architect assigned | Match? |
|----|----------|--------------------|--------|
| e1 | scope_unclear | | |
| e2 | design_decision | | |
| e3 | blocked | | |
| e4 | review_arbitration | | |
| e5 | review_arbitration | | |

### Behaviors to Note

- Did Architect escalate e3 (blocked on missing credential) correctly?
- Did Architect respect retry-only authority on e4 / e5 (never issue `executor_correct`)?
- On ambiguous cases (e.g., e2 design choice where both options arguably respect non-goals), did Architect commit to one or escalate?
- Did any verdict exceed 4 turns (systemPrompt anti-pattern)?

## Iteration Log (if applicable)

### Iteration 1 (initial)

- Prompt: `prompt.md` (v1)
- Result: see above

### Iteration 2

- Prompt: `iterations/v2-prompt.md`
- Changes from v1:
- Result:

### Iteration 3

- Prompt: `iterations/v3-prompt.md`
- Changes from v2:
- Result:

## Next Step

(PROCEED / KILL / ITERATE + concrete action)
