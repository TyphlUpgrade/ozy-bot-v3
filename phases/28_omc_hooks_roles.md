# Phase 28: OMC Hook Configuration + Custom Agent Roles

Read `plans/2026-04-07-agentic-workflow-v4-omc-only.md` § Phase F (lines ~1550-1620).

**Implementation dependency:** All prior phases (A through D).

**Context:** This phase configures OMC hooks for unattended agent operation and writes
the remaining agent role definitions (Executor, Architect, Reviewer) adapted for the
trading domain.

---

## What to Build

### 1. Executor role definition (`config/agent_roles/executor.md`)

Adapted from standard OMC executor for trading domain:
- Trading domain rules (async, no third-party TA, atomic writes)
- Worktree-aware scope guidance
- Commit-before-completion rule
- Simplifier pressure-test gate (threshold 0.15)
- Zone file update protocol (write history transitions)
- Signal file write convention (checkpoint signals)

### 2. Architect role definition (`config/agent_roles/architect.md`)

Adapted for trading domain:
- Intent classification gate (bug/calibration/feature/refactor/analysis)
- Checkpoint placement strategy
- Readiness gates (non-goals, decision boundaries)
- Trading domain rules
- Signal file write convention (`state/signals/architect/<task-id>/`)
- `disallowedTools: Write, Edit` (reads code, writes signals via Bash)

### 3. Reviewer role definition (`config/agent_roles/reviewer.md`)

Adapted from OMC verifier for trading domain:
- Contrarian pressure-test (threshold 0.25)
- Verification tiers (light/standard/thorough)
- Trading convention checks
- Structured approval format with file:line citations
- Signal file write convention (`state/signals/reviewer/<task-id>/`)
- `disallowedTools: Write, Edit`

### 4. Permission summary

Document the permission model for each role (for future `.claude/settings.json` config):

| Role | Allowed Tools | Disallowed |
|------|--------------|------------|
| Conductor | Read, Write, Bash, Glob, Grep | — |
| Executor | Read, Write, Edit, Bash, Glob, Grep | — |
| Architect | Read, Bash (read-only), Glob, Grep | Write, Edit |
| Reviewer | Read, Bash (read-only), Glob, Grep | Write, Edit |
| Ops Monitor | Read, Bash (limited), Glob, Grep | Write, Edit (source) |
| Dialogue | Read, Write (plans/state only), Bash, Glob, Grep | — |
| Strategy Analyst | Read, Bash (read-only), Glob, Grep | Write, Edit |

---

## Tests to Write

Create `ozymandias/tests/test_agent_roles.py`:

- `test_executor_role_exists` — verify file exists with frontmatter
- `test_executor_has_trading_rules` — verify trading domain rules present
- `test_executor_has_simplifier` — verify Simplifier pressure-test
- `test_executor_has_zone_protocol` — verify zone file update protocol
- `test_architect_role_exists` — verify file exists with frontmatter
- `test_architect_has_intent_classification` — verify intent classification gate
- `test_architect_has_readiness_gates` — verify non-goals and decision boundaries
- `test_architect_has_checkpoint_strategy` — verify checkpoint placement
- `test_reviewer_role_exists` — verify file exists with frontmatter
- `test_reviewer_has_contrarian` — verify Contrarian pressure-test
- `test_reviewer_has_verification_tiers` — verify 3 tiers
- `test_all_roles_exist` — verify all 7 role files present in config/agent_roles/

---

## Done When

1. All 7 role files exist in `config/agent_roles/`:
   conductor.md, ops_monitor.md, strategy_analyst.md, dialogue.md,
   executor.md, architect.md, reviewer.md
2. Each role has YAML frontmatter with name, model, tier
3. Trading domain rules present in Executor, Architect, Reviewer roles
4. Pressure-test protocols present in applicable roles
5. Permission model documented
6. Tests pass
