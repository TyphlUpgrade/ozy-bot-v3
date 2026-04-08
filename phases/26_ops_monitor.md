# Phase 26: Ops Monitor Agent

Read `plans/2026-04-07-agentic-workflow-v4-omc-only.md` § Phase C (lines ~1193-1240).

**Implementation dependency:** Phase 23 (Discord Companion — for alerting).

**Context:** The Ops Monitor is a persistent Haiku Claude Code instance that watches the bot's
signal output, detects anomalies, manages the process, and documents bugs. It runs during
market hours in a dedicated tmux session.

---

## What to Build

### 1. Ops Monitor role definition (`config/agent_roles/ops_monitor.md`)

Includes:
- Role definition (persistent monitor, not a developer)
- Anomaly detection rules (stale timestamps, WARNING clusters, ERROR patterns, equity drawdown)
- Bug documentation format (structured reports to `state/agent_tasks/`)
- Process management permissions (restart with cooldown, pause entries)
- Escalation protocol (3 tiers: auto-handle, notify+act, escalate+wait)
- Daily summary maintenance (`state/ops_daily_summary.json`)
- Bug report rate limit (3/hour)
- Permission tiers (ReadOnly, ProcessControl, DangerFullAccess)
- Context scope (logs, signals, journal, config — never source code)

### 2. Daily summary schema (`state/ops_daily_summary.json`)

Rolling anomaly counts, pattern timestamps, trend flags. Updated every cycle, read back
after compaction. Decouples pattern memory from conversation memory.

---

## Tests to Write

Create `ozymandias/tests/test_ops_monitor.py`:

- `test_ops_role_file_exists` — verify file exists
- `test_ops_role_has_frontmatter` — verify YAML frontmatter
- `test_ops_role_has_anomaly_rules` — verify anomaly detection rules present
- `test_ops_role_has_escalation_tiers` — verify 3 escalation tiers
- `test_ops_role_has_rate_limit` — verify bug report rate limit mentioned
- `test_daily_summary_schema` — verify expected fields

---

## Done When

1. `config/agent_roles/ops_monitor.md` exists with complete role definition
2. Anomaly detection rules defined (stale timestamps, WARNING clusters, ERROR patterns)
3. Escalation protocol documented (3 tiers)
4. Bug report rate limit (3/hour) specified
5. Daily summary schema defined
6. Tests pass
