# Reviewer Spike Results

**Run date:** (fill per variant)

## Cross-Variant Summary (fill after all 4 runs)

| Variant | Accuracy | Avg cost | Avg latency | Subagents invoked | Notes |
|---------|----------|----------|-------------|-------------------|-------|
| bare | __/5 | $___ | __s | N/A | |
| omc | __/5 | $___ | __s | __ | |
| caveman | __/5 | $___ | __s | N/A | |
| both | __/5 | $___ | __s | __ | |

## Per-Scenario Comparison

| Scenario | Expected | bare | omc | caveman | both | Notes |
|----------|----------|------|-----|---------|------|-------|
| s1 clean | approve | | | | | |
| s2 off-by-one | reject/RC | | | | | |
| s3 SQL inj | reject/RC | | | | | |
| s4 O(n²) | request_changes | | | | | |
| s5 test gap | request_changes | | | | | |

## Planted Defect Detection Matrix

| Scenario | Defect | bare caught? | omc caught? | caveman caught? | both caught? |
|----------|--------|--------------|-------------|-----------------|--------------|
| s2 | `skip = page * pageSize` (1-indexed off-by-one) | | | | |
| s3 | `$queryRawUnsafe` SQL injection | | | | |
| s4 | O(n²) + .includes() on 100k criterion | | | | |
| s5 | Memory leak in impl + 1/6 test coverage | | | | |

## OMC Subagent Invocations (OMC + both variants)

| Scenario | Expected subagent | Actually invoked? | Subagent output helped? |
|----------|-------------------|-------------------|-------------------------|
| s3 | security-reviewer | | |
| s4 | code-reviewer / analyst | | |
| s5 | test-engineer | | |

## Risk Score Rigor

Did Reviewer avoid lazy-uniform scores (all dimensions ≈ 0.5)?

| Variant | Scenarios with distinct dimension scores | Scenarios with uniform scores |
|---------|------------------------------------------|--------------------------------|
| bare | | |
| omc | | |
| caveman | | |
| both | | |

## Decisions

Apply pre-committed rule:
- [ ] **PROCEED** with Wave A implementation using winning variant config
- [ ] **ITERATE** — prompt revision + re-run
- [ ] **KILL** — Reviewer unreliable; revisit plan

**Winning variant:**

**Winning model:**

**Winning plugin config:**

**Prompt refinements:**

## Observations

- Reject rate (verdict ≠ approve):
- False-positive findings (bare vs OMC):
- Did caveman break JSON output:
- Budget: all under cap?
- Latency: production-acceptable?

## Next Step

(feed findings into Wave A `ReviewGate` config + plan Section M append)
