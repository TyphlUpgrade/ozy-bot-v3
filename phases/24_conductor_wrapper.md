# Phase 24: Conductor Wrapper + Task Format

Read `plans/2026-04-07-agentic-workflow-v4-omc-only.md` § Phase E (lines ~1284-1547),
§ Debugging & Observability (lines ~457-603), § Self-Modification Safety (lines ~605-733),
and § Structured Task Packet Format (lines ~735-768).

**Implementation dependency:** Phase 22 (Signal File API), Phase 23 (clawhip + Discord Companion).

**Context:** The conductor is a deterministic bash wrapper (~50-80 lines) that owns all
mechanical operations: polling, state I/O with `jq`, git operations, tmux lifecycle, timeout
enforcement, and logging. Claude is invoked on-demand via `claude -p` for judgment calls only
(task classification, context assembly, failure diagnosis). The wrapper is sequential-first:
one task at a time through the full pipeline.

---

## What to Build

### 1. Conductor wrapper (`tools/conductor.sh`, ~50-80 lines)

New file: `tools/conductor.sh`

A bash script that implements the development pipeline's mechanical loop:

```bash
#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Conductor wrapper — deterministic pipeline coordination
#
# Owns: polling, state I/O (jq), git operations, tmux lifecycle, timeout,
#       heartbeat, and all logging infrastructure.
# Does NOT own: judgment calls — those go to `claude -p` on-demand.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="$PROJECT_ROOT/ozymandias/state"
SIGNALS_DIR="$STATE_DIR/signals"
TASKS_DIR="$STATE_DIR/agent_tasks"
LOG_DIR="$STATE_DIR/logs"
CONDUCTOR_LOG="$LOG_DIR/conductor.log"
ORCH_STATE="$SIGNALS_DIR/orchestrator/orchestrator_state.json"
INTENT_FILE="$SIGNALS_DIR/conductor/exit_intent.json"
CONDUCTOR_ROLE="$PROJECT_ROOT/config/agent_roles/conductor.md"
POLL_INTERVAL=10
HEARTBEAT_INTERVAL=60  # heartbeat every 60s (not every poll — avoids log bloat)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log_event() {
  local event="$1"; shift
  local extra="${1:-}"
  local ts; ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  local line="{\"ts\":\"$ts\",\"event\":\"$event\""
  [ -n "$extra" ] && line="$line,$extra"
  line="$line}"
  echo "$line" >> "$CONDUCTOR_LOG"
}

write_exit_intent() {
  local action="$1" reason="$2"
  echo "{\"action\":\"$action\",\"reason\":\"$reason\",\"ts\":\"$(date -Iseconds)\"}" \
    > "$INTENT_FILE"
}

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

mkdir -p "$LOG_DIR/agents" "$LOG_DIR/signals" "$LOG_DIR/judgments" "$LOG_DIR/summaries"
mkdir -p "$TASKS_DIR" "$SIGNALS_DIR/conductor" "$SIGNALS_DIR/orchestrator"
mkdir -p "$STATE_DIR/staging"

# Initialize orchestrator state if absent
if [ ! -f "$ORCH_STATE" ]; then
  echo '{"active_task":null,"completed_tasks":[],"last_merge":null}' > "$ORCH_STATE"
fi

# Log rotation: archive conductor.log if >1MB
if [ -f "$CONDUCTOR_LOG" ] && [ "$(stat -c%s "$CONDUCTOR_LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  mv "$CONDUCTOR_LOG" "$CONDUCTOR_LOG.$(date +%Y%m%d).bak"
fi

# Agent log compression: gzip logs older than 7 days
find "$LOG_DIR/agents" -name "*.log" -mtime +7 -exec gzip -q {} \; 2>/dev/null || true

log_event "startup" "\"version\":\"1.0\",\"pid\":$$"

# Reconcile: if orch state shows an active task, check if its worktree/branch still exist
active_task=$(jq -r '.active_task // empty' "$ORCH_STATE" 2>/dev/null)
if [ -n "$active_task" ]; then
  wt_path=".worktrees/$active_task"
  if [ ! -d "$wt_path" ]; then
    log_event "reconcile" "\"task_id\":\"$active_task\",\"detail\":\"worktree missing, marking failed\""
    jq '.active_task = null' "$ORCH_STATE" > "$ORCH_STATE.tmp" && mv "$ORCH_STATE.tmp" "$ORCH_STATE"
  fi
fi

# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

last_heartbeat=$(date +%s)

while true; do
  # -- Check conductor control signals --------------------------------------
  if [ -f "$SIGNALS_DIR/conductor/restart.json" ]; then
    rm -f "$SIGNALS_DIR/conductor/restart.json"
    log_event "restart_signal" "\"source\":\"discord\""
    write_exit_intent "restart" "discord_command"
    exit 0
  fi

  if [ -f "$SIGNALS_DIR/conductor/shutdown.json" ]; then
    rm -f "$SIGNALS_DIR/conductor/shutdown.json"
    log_event "shutdown_signal" "\"source\":\"discord\""
    write_exit_intent "shutdown" "discord_command"
    exit 0
  fi

  # -- Heartbeat (every HEARTBEAT_INTERVAL, not every poll) -----------------
  now=$(date +%s)
  if (( now - last_heartbeat >= HEARTBEAT_INTERVAL )); then
    log_event "heartbeat" "\"uptime_s\":$((now - ${start_time:-$now}))"
    last_heartbeat=$now
  fi

  # -- Check for new tasks -------------------------------------------------
  active_task=$(jq -r '.active_task // empty' "$ORCH_STATE" 2>/dev/null)

  if [ -z "$active_task" ]; then
    # No active task — scan for new tasks
    new_task=$(find "$TASKS_DIR" -name "*.json" -type f 2>/dev/null | sort | head -1)

    if [ -n "$new_task" ]; then
      task_content=$(cat "$new_task")
      task_id=$(basename "$new_task" .json)
      log_event "signal_detected" "\"type\":\"new_task\",\"task_id\":\"$task_id\",\"source\":\"$new_task\""

      # -- Judgment call: classify task ------------------------------------
      classify_input=$(jq -n \
        --arg judgment "classify_task" \
        --argjson task_file "$task_content" \
        --argjson active_tasks '[]' \
        --argjson state_summary "$(jq '{active_count: (if .active_task then 1 else 0 end), last_merge}' "$ORCH_STATE")" \
        '{judgment: $judgment, task_file: $task_file, active_tasks: $active_tasks, orchestrator_state_summary: $state_summary}')

      # Save judgment input
      mkdir -p "$LOG_DIR/judgments/$task_id"
      echo "$classify_input" > "$LOG_DIR/judgments/$task_id/001-classify-input.json"
      log_event "judgment_call" "\"reason\":\"task_classify\",\"task_id\":\"$task_id\""

      classify_output=$(echo "$classify_input" | claude -p "$(cat "$CONDUCTOR_ROLE")" --output-format json 2>/dev/null) || classify_output='{"action":"defer","reason":"claude invocation failed"}'
      echo "$classify_output" > "$LOG_DIR/judgments/$task_id/001-classify-output.json"
      log_event "judgment_result" "\"task_id\":\"$task_id\",\"decision\":$classify_output"

      action=$(echo "$classify_output" | jq -r '.action // "defer"')

      case $action in
        accept)
          # Preserve task signal, update state, begin pipeline
          mkdir -p "$LOG_DIR/signals/$task_id"
          cp "$new_task" "$LOG_DIR/signals/$task_id/001-task.json"
          rm -f "$new_task"

          jq --arg tid "$task_id" '.active_task = $tid' "$ORCH_STATE" > "$ORCH_STATE.tmp" \
            && mv "$ORCH_STATE.tmp" "$ORCH_STATE"

          log_event "task_accepted" "\"task_id\":\"$task_id\""

          # -- Phase 1: Spawn Architect -----------------------------------
          # (Placeholder — full pipeline implementation in Executor/Reviewer phases)
          log_event "pipeline_start" "\"task_id\":\"$task_id\",\"stage\":\"architect\""
          ;;
        reject)
          reject_reason=$(echo "$classify_output" | jq -r '.reject_reason // "rejected"')
          mkdir -p "$LOG_DIR/signals/$task_id"
          cp "$new_task" "$LOG_DIR/signals/$task_id/001-task-rejected.json"
          rm -f "$new_task"
          log_event "task_rejected" "\"task_id\":\"$task_id\",\"reason\":\"$reject_reason\""
          ;;
        *)
          log_event "task_deferred" "\"task_id\":\"$task_id\""
          ;;
      esac
    fi
  fi

  # -- Poll cycle marker (only if verbose debug is needed) ------------------
  # log_event "poll_cycle"  # Uncomment for debugging; noisy in production

  sleep "$POLL_INTERVAL"
done
```

**Key design decisions:**
- The wrapper is the only process that reads/writes `orchestrator_state.json`
- All judgment calls go through `claude -p` with the conductor role prompt
- Every signal file is preserved to `state/logs/signals/` before deletion
- Every judgment call input/output is saved to `state/logs/judgments/`
- Heartbeat every 60s (not every 10s poll) to avoid log bloat
- `jq` for all JSON manipulation (atomic via temp file + mv)

### 2. Outer restart loop (`tools/start_conductor.sh`, ~20 lines)

New file: `tools/start_conductor.sh`

```bash
#!/bin/bash
# Outer restart loop with exit intent file dispatch.
# Run in tmux pane 5: tmux send-keys -t ozymandias:0.5 "bash tools/start_conductor.sh" Enter

INTENT_FILE="ozymandias/state/signals/conductor/exit_intent.json"

while true; do
  # Clean stale intent file BEFORE starting wrapper.
  rm -f "$INTENT_FILE"

  bash tools/conductor.sh
  ts=$(date -Iseconds)

  # Read intent file. Wrapper writes this before every planned exit.
  if [ -f "$INTENT_FILE" ]; then
    action=$(jq -r .action "$INTENT_FILE" 2>/dev/null) || action="crash"
    reason=$(jq -r .reason "$INTENT_FILE" 2>/dev/null) || reason="corrupt intent file"
    rm -f "$INTENT_FILE"
  else
    action="crash"
    reason="no exit intent file — unclean death"
  fi

  case $action in
    restart)
      echo "[$ts] Conductor restart ($reason), restarting in 5s..."
      sleep 5
      ;;
    shutdown)
      echo "[$ts] Conductor shutdown ($reason). Exiting."
      break
      ;;
    *)
      echo "[$ts] Conductor crashed ($reason). NOT restarting."
      echo "{\"type\":\"conductor_crash\",\"reason\":\"$reason\",\"ts\":\"$ts\"}" \
        > "ozymandias/state/signals/alerts/conductor_crash.json"
      break
      ;;
  esac
done
```

### 3. Conductor judgment prompt (`config/agent_roles/conductor.md`)

New file: `config/agent_roles/conductor.md`

```markdown
---
name: conductor
description: Development pipeline judgment prompt (invoked by wrapper)
model: sonnet
tier: MEDIUM
mode: claude-p
output: json
---

# Conductor — Development Pipeline Coordinator

## Role

You are the Conductor. You are invoked by the conductor wrapper (`tools/conductor.sh`)
via `claude -p` when a signal requires judgment. You are NOT a persistent session — each
invocation is fresh. You respond with a structured JSON decision, then exit.

## What You Do

- **Classify tasks:** Determine priority and whether to accept, defer, or reject.
- **Assemble context:** Select relevant files and domain context for Architect sessions.
- **Diagnose failures:** After 3+ consecutive failures, analyze what's going wrong.

## What You Do NOT Do

- Write code (that's the Executor's job)
- Poll for signals (the wrapper does that)
- Spawn agents or manage tmux (the wrapper does that)
- Write state files (the wrapper does that)
- Loop or wait (you run once and exit)

## Judgment Types

### classify_task
Input: task file contents, active tasks, orchestrator state summary.
Output: `{"action": "accept|defer|reject", "priority": "human|bug|strategy_analysis|backlog", "reason": "...", "reject_reason": "..."}`

Priority ordering: human > bug > strategy_analysis > backlog.
Reject if: duplicate of active task, stale TTL (bugs: 2h, strategy: 8h, human: no TTL).
Defer if: another task is already active (sequential-first).

### assemble_context
Input: task packet, zone files, recent drift log.
Output: `{"relevant_files": [...], "domain_context": "...", "known_concerns": [...]}`

Select files the Architect needs to see. Include trading domain rules relevant to the task.
Flag any open NOTES.md concerns affecting the task's area.

### diagnose_failure
Input: task_id, zone file, failure history, last agent log tail.
Output: `{"decision": "replan|escalate|retry_simpler", "notes": "...", "architect_hint": "..."}`

Replan if the approach seems wrong. Escalate if the problem is beyond the pipeline's scope.
Retry simpler only if the failure was a transient issue (timeout, flaky test).

## Trading Domain Context

This pipeline develops an automated trading bot (Ozymandias v3). Key constraints:
- Python 3.12+, asyncio, atomic JSON state files
- No third-party TA libraries
- Claude JSON parsing has 4-step defensive pipeline
- Risk manager has override authority over everything
- Modules communicate via interfaces and JSON, never direct coupling
- Only the orchestrator knows about all other modules
```

### 4. Orchestrator state schema (`state/signals/orchestrator/orchestrator_state.json`)

Written by the wrapper on startup if absent. Updated atomically (write temp + mv).

```json
{
  "active_task": null,
  "completed_tasks": [],
  "last_merge": null
}
```

When a task is active:
```json
{
  "active_task": "2026-04-08-fix-rvol",
  "completed_tasks": ["2026-04-07-add-sector-dispersion"],
  "last_merge": "2026-04-07T18:30:00Z"
}
```

### 5. Task packet schema

Task files live in `state/agent_tasks/`. Schema:
```json
{
  "task_id": "<string>",
  "sections": {
    "TASK": "<one-line description>",
    "EXPECTED_OUTCOME": "<what success looks like>",
    "MUST_DO": ["<required actions>"],
    "MUST_NOT_DO": ["<forbidden actions>"],
    "CONTEXT": "<relevant background>",
    "ACCEPTANCE_TESTS": ["<test names>"]
  },
  "source": "human|strategy_analyst|ops_monitor",
  "priority": "human|bug|strategy_analysis|backlog",
  "model_override": null,
  "zone": "<primary file/directory>",
  "checkpoint_units": [2]
}
```

### 6. Zone file schema

Written by the Executor into the worktree, read by the wrapper for progress tracking:
```json
{
  "task_id": "<string>",
  "units_completed": [1, 2],
  "unit_in_progress": 3,
  "units_remaining": [4, 5],
  "test_status": "passing",
  "branch": "feature/<task-id>",
  "worktree_path": ".worktrees/<task-id>",
  "wall_clock_seconds": 1830,
  "last_updated": "<ISO timestamp>",
  "history": [
    {"ts": "<ISO>", "transition": "started", "unit": 1},
    {"ts": "<ISO>", "transition": "completed", "unit": 1},
    {"ts": "<ISO>", "transition": "checkpoint", "unit": 2},
    {"ts": "<ISO>", "transition": "resumed", "unit": 2, "fresh_session": true}
  ]
}
```

### 7. Logging infrastructure

All built into the wrapper, not separate components:

| Layer | Location | Format | Purpose |
|-------|----------|--------|---------|
| Event log | `state/logs/conductor.log` | JSONL | Primary debug artifact |
| Agent capture | `state/logs/agents/<role>-<task-id>.log` | Raw terminal | Flight recorder |
| Signal archive | `state/logs/signals/<task-id>/` | Numbered JSON | Audit trail |
| Judgment recording | `state/logs/judgments/<task-id>/` | Input/output JSON | Judgment debugging |
| Post-task summary | `state/logs/summaries/<task-id>.json` | Structured JSON | High-level overview |

Event types: `startup`, `shutdown`, `heartbeat`, `poll_cycle`, `signal_detected`,
`agent_spawn`, `agent_exit`, `judgment_call`, `judgment_result`, `merge`, `revert`,
`timeout`, `error`, `escalation`, `worktree_create`, `worktree_cleanup`,
`task_accepted`, `task_rejected`, `task_deferred`, `pipeline_start`, `reconcile`.

Retention: `conductor.log` rotated when >1MB (keep 14 daily backups). Agent logs compressed
after 7 days. Signals, judgments, summaries kept indefinitely (small files).

### 8. Staging directory convention

```
state/staging/
├── architect-<task-id>/
│   └── CLAUDE.md          # Architect role + task context
├── architect-review-<task-id>/
│   ├── CLAUDE.md          # Architect role + diff + test output
│   └── review_context.json
├── reviewer-<task-id>/
│   ├── CLAUDE.md          # Reviewer role + diff + test output
│   └── review_context.json
└── analyst-<date>/
    └── CLAUDE.md          # Analyst role + journal pointers
```

Each staging CLAUDE.md includes:
- Agent's role definition (from `config/agent_roles/`)
- Task-specific context (task packet, diff, test results)
- Trading domain rules from main CLAUDE.md
- `OMC_TEAM_WORKER=true` convention (environment variable + prompt mention)

### 9. Self-modification detection

After every merge in the pipeline, the wrapper checks:
```bash
if git diff --name-only HEAD~1 HEAD | grep -qE '^tools/conductor\.sh|^config/agent_roles/conductor\.md'; then
  log_event "self_mod_detected" "Pipeline infrastructure changed, restarting"
  write_exit_intent "restart" "self_mod_detected"
  exit 0
fi
```

### 10. Discord companion extensions

Add two new commands to `tools/discord_companion.py`:
- `!restart-conductor` → writes `state/signals/conductor/restart.json`
- `!shutdown-conductor` → writes `state/signals/conductor/shutdown.json`

---

## Tests to Write

### Wrapper tests (`tests/test_conductor.sh` — shell-based)

Since the conductor is a bash script, tests use a mock environment:

- `test_startup_creates_dirs` — verify all log directories created
- `test_startup_creates_orch_state` — verify orchestrator_state.json initialized
- `test_restart_signal_exits` — write restart.json, verify wrapper exits with intent
- `test_shutdown_signal_exits` — write shutdown.json, verify wrapper exits with intent
- `test_log_rotation` — create >1MB log, verify rotation on startup
- `test_agent_log_compression` — create old agent logs, verify gzip on startup
- `test_stale_worktree_reconciliation` — set active task with missing worktree, verify state cleared

### Companion extension tests (`ozymandias/tests/test_discord_companion.py` — extend existing)

- `test_restart_conductor_creates_signal` — verify `!restart-conductor` writes correct file
- `test_shutdown_conductor_creates_signal` — verify `!shutdown-conductor` writes correct file

### Judgment schema tests (`ozymandias/tests/test_conductor_schemas.py`)

- `test_classify_task_input_schema` — validate input JSON matches expected schema
- `test_classify_task_output_schema` — validate output JSON has required fields
- `test_assemble_context_input_schema` — validate input JSON
- `test_assemble_context_output_schema` — validate output JSON
- `test_diagnose_failure_input_schema` — validate input JSON
- `test_diagnose_failure_output_schema` — validate output JSON
- `test_task_packet_schema` — validate task packet has all 6 sections
- `test_zone_file_schema` — validate zone file has history array

---

## Done When

1. `tools/conductor.sh` exists and runs without syntax errors (`bash -n tools/conductor.sh`)
2. `tools/start_conductor.sh` exists — outer restart loop with intent file dispatch
3. `config/agent_roles/conductor.md` exists with full role prompt
4. Exit intent protocol works: wrapper writes intent file, outer loop reads it correctly
5. Conductor control signals work: `restart.json` and `shutdown.json` trigger clean exits
6. Logging infrastructure: all 5 layers create files in correct locations
7. Self-modification detection: merged diff touching pipeline files triggers restart
8. All judgment call schemas have documented input/output contracts
9. Task packet schema matches the plan's 6-section format
10. Zone file schema includes `history` array for debugging
11. Staging directory convention documented and mkdir'd on startup
12. Shell tests pass: `bash tests/test_conductor.sh`
13. Python tests pass: `pytest ozymandias/tests/test_conductor_schemas.py`
14. Companion extension tests pass for `!restart-conductor` and `!shutdown-conductor`
