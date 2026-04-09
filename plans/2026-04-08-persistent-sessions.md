# Plan: Persistent Agent Sessions via stream-json FIFO

**Date:** 2026-04-08
**Status:** Draft
**Scope:** `tools/conductor.sh` (primary)

## Problem Statement

Each agent invocation (architect, executor, reviewer) starts a fresh `claude -p` process.
Every invocation pays ~25K tokens of system context (CLAUDE.md, hooks, MCP tools, role
prompt). For a 3-stage pipeline, that's ~75K tokens of overhead per task. Architect and
reviewer gain nothing from isolation — they don't write files, and their accumulated
codebase understanding would improve over multiple tasks.

## Requirements Summary

1. Architect and reviewer run as persistent multi-turn sessions (start once, reuse across tasks)
2. Executor remains single-shot per task (needs fresh worktree and isolated context)
3. Signal file bus remains the completion mechanism (no change to `check_pipeline()` detection)
4. Caveman compression activates in the first turn of each persistent session
5. Session death detected and auto-restarted
6. Conductor restart/shutdown cleanly terminates persistent sessions
7. Audit logging maintained (one log per persistent session, task IDs in messages)
8. No changes to judgment calls, task intake, merge logic, or signal file conventions

## Acceptance Criteria

- [ ] Architect session starts at conductor startup, persists across 2+ tasks
- [ ] Reviewer session starts at conductor startup, persists across 2+ tasks
- [ ] Executor still uses single-shot `spawn_agent()` with per-task worktree
- [ ] Token usage measurably lower on 2nd+ task (visible in `result` event's token fields)
- [ ] `check_pipeline()` still transitions stages via signal file detection (no behavior change)
- [ ] Persistent agent death triggers auto-restart with fresh session
- [ ] Conductor shutdown closes all FIFOs and waits for Claude processes to exit
- [ ] Conductor restart starts fresh persistent sessions (old sessions exit on FIFO EOF)
- [ ] Caveman directive delivered in first message of each persistent session
- [ ] Agent logs contain task ID per turn for audit traceability

## Design

### Core Insight

The signal file bus decouples completion detection from agent lifecycle. `check_pipeline()`
polls for `plan.json`, `completion-*.json`, and `verdict.json` — it never inspects agent
logs or process state for results. This means we can change how agents are spawned and
receive prompts without touching the completion detection loop.

The only `check_pipeline()` change: don't `tmux kill-pane` for persistent agents after
stage completion. The pane stays alive for the next task.

### New Functions

#### `spawn_persistent_session()` — `tools/conductor.sh` (new, after `spawn_agent` ~line 184)

```bash
# Spawn a persistent claude session with FIFO-based multi-turn input.
# Returns: writes pane_id, pid, and fd number to caller-provided vars.
# Persistent sessions stay alive across tasks. Send messages via send_to_session().
# End session by closing the fd (sends EOF to FIFO).
# Usage: spawn_persistent_session <role> <workdir> <log_file> [disallowed_tools] [model]
spawn_persistent_session() {
  local role="$1" workdir="$2" log_file="$3"
  local disallowed_tools="${4:-}" model="${5:-}"
  local deny_flag="" model_flag=""
  [ -n "$disallowed_tools" ] && deny_flag="--disallowedTools $disallowed_tools"
  [ -n "$model" ] && model_flag="--model $model"

  local fifo="$SESSION_DIR/${role}.fifo"
  rm -f "$fifo" && mkfifo "$fifo"

  local pane_id
  pane_id=$(tmux split-window -t "$TMUX_SESSION" -d -P -F '#{pane_id}' \
    "cd '$workdir' && claude -p --verbose --permission-mode dontAsk \
     $deny_flag $model_flag \
     --input-format stream-json --output-format stream-json --include-hook-events \
     < '$fifo' > '$log_file' 2>&1; echo '{\"event\":\"session_ended\",\"role\":\"$role\",\"ts\":\"'$(date -Iseconds)'\"}' >> '$log_file'")

  # Use fixed fd numbers: architect=10, reviewer=11
  local fd
  case "$role" in
    architect) fd=10 ;;
    reviewer)  fd=11 ;;
    *)         log_event "error" "\"detail\":\"unknown persistent role: $role\""; return 1 ;;
  esac
  eval "exec ${fd}>\"$fifo\""

  # Store session metadata in orchestrator state
  update_state "$(printf '.sessions.%s = {"pane_id":"%s","fd":%d,"fifo":"%s","log":"%s","started":%d,"turn_count":0}' \
    "$role" "$pane_id" "$fd" "$fifo" "$log_file" "$(date +%s)")"

  log_event "session_started" "\"role\":\"$role\",\"pane_id\":\"$pane_id\",\"fd\":$fd"
}
```

#### `send_to_session()` — (new, after `spawn_persistent_session`)

```bash
# Send a prompt to a persistent session via its FIFO fd.
# The prompt is sent as a stream-json user message.
# Usage: send_to_session <role> <prompt_text>
send_to_session() {
  local role="$1" prompt_text="$2"
  local fd
  case "$role" in
    architect) fd=10 ;;
    reviewer)  fd=11 ;;
    *)         return 1 ;;
  esac

  # Escape the prompt for JSON embedding
  local escaped
  escaped=$(printf '%s' "$prompt_text" | jq -Rs '.')

  echo "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":${escaped}}}" >&${fd}

  # Increment turn count
  update_state "$(printf '.sessions.%s.turn_count += 1 | .sessions.%s.last_sent = %d' \
    "$role" "$role" "$(date +%s)")"

  log_event "message_sent" "\"role\":\"$role\",\"prompt_length\":${#prompt_text}"
}
```

#### `check_session_alive()` — (new)

```bash
# Check if a persistent session's pane is still alive.
# If dead, log and clear session state (caller should restart).
# Usage: check_session_alive <role>  — returns 0 if alive, 1 if dead
check_session_alive() {
  local role="$1"
  local pane_id
  pane_id=$(jq -r ".sessions.${role}.pane_id // empty" "$ORCH_STATE" 2>/dev/null)
  [ -z "$pane_id" ] && return 1
  check_pane_alive "$pane_id"
}
```

#### `ensure_session()` — (new)

```bash
# Ensure a persistent session is running for the given role.
# Starts one if missing or dead. Sends caveman init on first start.
# Usage: ensure_session <role> <workdir> <disallowed_tools> <model>
ensure_session() {
  local role="$1" workdir="$2" disallowed_tools="$3" model="$4"
  local session_log="$SESSION_DIR/${role}.log"

  if ! check_session_alive "$role"; then
    log_event "session_restart" "\"role\":\"$role\",\"reason\":\"dead or missing\""

    # Clean up old FIFO/fd if they exist
    local old_fd
    case "$role" in
      architect) old_fd=10 ;;
      reviewer)  old_fd=11 ;;
    esac
    eval "exec ${old_fd}>&- 2>/dev/null" || true
    rm -f "$SESSION_DIR/${role}.fifo"

    spawn_persistent_session "$role" "$workdir" "$session_log" "$disallowed_tools" "$model"

    # Wait for session to initialize (hooks fire)
    sleep 5

    # First message: establish role context + activate caveman
    local role_file="$CONFIG_DIR/agent_roles/${role}.md"
    local role_prompt=""
    [ -f "$role_file" ] && role_prompt=$(cat "$role_file")
    local caveman_block
    caveman_block=$(get_caveman_block)

    local init_prompt="You are the ${role} agent in the Ozymandias development pipeline.

${role_prompt}

${caveman_block}

You will receive tasks as follow-up messages. For each task, do your analysis and write your output to the signal file as instructed. Acknowledge this setup with: READY"

    send_to_session "$role" "$init_prompt"
    sleep 10  # let init turn complete
    log_event "session_initialized" "\"role\":\"$role\""
  fi
}
```

#### `close_session()` — (new)

```bash
# Close a persistent session by closing its FIFO write-end (sends EOF).
# Usage: close_session <role>
close_session() {
  local role="$1"
  local fd
  case "$role" in
    architect) fd=10 ;;
    reviewer)  fd=11 ;;
    *)         return 1 ;;
  esac

  eval "exec ${fd}>&- 2>/dev/null" || true
  rm -f "$SESSION_DIR/${role}.fifo"

  local pane_id
  pane_id=$(jq -r ".sessions.${role}.pane_id // empty" "$ORCH_STATE" 2>/dev/null)
  if [ -n "$pane_id" ]; then
    # Give Claude 5s to exit gracefully on EOF, then kill
    sleep 5
    if check_pane_alive "$pane_id"; then
      tmux kill-pane -t "$pane_id" 2>/dev/null || true
    fi
  fi

  update_state "$(printf 'del(.sessions.%s)' "$role")"
  log_event "session_closed" "\"role\":\"$role\""
}
```

### Changes to Existing Functions

#### `launch_architect()` — lines 286–322

**Current:** Builds prompt file, calls `spawn_agent()`, stores pane_id.

**New:** Builds prompt text (same content), calls `ensure_session()` + `send_to_session()`.
Stores the persistent session's pane_id in pipeline state (for `check_pipeline` alive checks).

```bash
launch_architect() {
  local task_id="$1"
  local task_file="$LOG_DIR/signals/$task_id/001-task.json"
  # ... existing prompt building (lines 289-309) but to variable, not file ...

  local deny_tools="Write,Edit,NotebookEdit,mcp__filesystem__write_file,mcp__filesystem__edit_file,mcp__filesystem__move_file,mcp__filesystem__create_directory"
  ensure_session "architect" "$PROJECT_ROOT" "$deny_tools" "opus"

  # Send task-specific prompt (not role instructions — those were sent in init)
  local task_prompt="## New Task: $task_id

$(cat "$task_file" | jq -r '.description // .prompt // "No description"')

Write your architectural plan to: $SIGNALS_DIR/architect/$task_id/plan.json

$(cat "$task_file")"

  send_to_session "architect" "$task_prompt"

  local pane_id
  pane_id=$(jq -r '.sessions.architect.pane_id // empty' "$ORCH_STATE" 2>/dev/null)
  update_state "$(printf '.stage = "architect" | .pane_id = "%s" | .stage_started = %d' "$pane_id" "$(date +%s)")"
  log_event "agent_spawned" "\"stage\":\"architect\",\"task_id\":\"$task_id\",\"persistent\":true"
}
```

#### `launch_reviewer()` — lines 380–438

**Same pattern as architect:** `ensure_session()` + `send_to_session()` with task-specific
prompt (diff content, plan content, verdict output path).

#### `launch_executor()` — lines 324–378

**No change.** Executor stays single-shot via `spawn_agent()`. Fresh worktree per task.

#### `check_pipeline()` — lines 509–632

**Minimal changes:**

1. **Architect completion (lines 552–553):** Don't kill pane. Replace:
   ```bash
   if [ -n "$pane_id" ] && check_pane_alive "$pane_id"; then
     tmux kill-pane -t "$pane_id" 2>/dev/null || true
   fi
   ```
   With:
   ```bash
   # Persistent session — don't kill pane, just log completion
   log_event "stage_complete_persistent" "\"stage\":\"architect\",\"task_id\":\"$task_id\""
   ```

2. **Reviewer completion (lines 600–601):** Same — don't kill pane.

3. **Executor completion (lines 573–574):** Keep killing pane (single-shot).

4. **Death detection:** For persistent agents, death = session died. Trigger `ensure_session()`
   on next `launch_*()` call (it already handles this). For the current task, treat as failure
   (same as current behavior: "agent died without plan/verdict").

#### Startup (lines 664–675)

Add persistent session initialization after reconciliation:

```bash
# Start persistent sessions
SESSION_DIR="$LOG_DIR/sessions"
mkdir -p "$SESSION_DIR"

DENY_TOOLS_READONLY="Write,Edit,NotebookEdit,mcp__filesystem__write_file,mcp__filesystem__edit_file,mcp__filesystem__move_file,mcp__filesystem__create_directory"
ensure_session "architect" "$PROJECT_ROOT" "$DENY_TOOLS_READONLY" "opus"
ensure_session "reviewer" "$PROJECT_ROOT" "$DENY_TOOLS_READONLY" "sonnet"
log_event "persistent_sessions_ready" "{}"
```

#### Shutdown (before `exit 0` on restart/shutdown signals, lines 686–698)

```bash
close_session "architect"
close_session "reviewer"
```

### State Schema Extension

Current orchestrator state (per-task):
```json
{
  "active_task": "task-id",
  "stage": "architect",
  "pane_id": "%123",
  "stage_started": 1712600000
}
```

Extended (persistent sessions):
```json
{
  "active_task": "task-id",
  "stage": "architect",
  "pane_id": "%123",
  "stage_started": 1712600000,
  "sessions": {
    "architect": {
      "pane_id": "%123",
      "fd": 10,
      "fifo": "/path/to/architect.fifo",
      "log": "/path/to/architect.log",
      "started": 1712600000,
      "turn_count": 3
    },
    "reviewer": {
      "pane_id": "%456",
      "fd": 11,
      "fifo": "/path/to/reviewer.fifo",
      "log": "/path/to/reviewer.log",
      "started": 1712600000,
      "turn_count": 1
    }
  }
}
```

### Caveman Integration

First message to each persistent session includes the caveman directive block
(from `get_caveman_block()`). This establishes the compression mode for all
subsequent turns. No per-task caveman injection needed.

For executor (single-shot), caveman is still injected in the prompt file as today.

### Context Window Management

Persistent sessions accumulate context. Mitigation:

1. **Monitor token counts:** Each `"type":"result"` event in the JSONL log includes
   token usage. Track cumulative tokens per session.
2. **Session rotation:** When cumulative tokens exceed a threshold (e.g., 150K input tokens),
   close and restart the session. This is a fresh start with caveman re-init.
3. **Implementation:** Add a `check_session_health()` call after each turn completion.
   Parse the last `result` event from the log for token data.

This is a Phase 2 optimization — the initial implementation can skip rotation and
rely on Claude's built-in context management (compaction). If sessions hit limits,
the conductor will see a session death and auto-restart.

## Implementation Steps

### Unit 1: New session management functions (~4 functions)
- File: `tools/conductor.sh`
- Add after `spawn_agent()` (line 184):
  - `spawn_persistent_session()` — FIFO creation, tmux + claude startup, fd allocation
  - `send_to_session()` — JSON message encoding + write to fd
  - `check_session_alive()` — wrapper around `check_pane_alive` using session state
  - `ensure_session()` — start-if-dead + caveman init
  - `close_session()` — fd close + FIFO cleanup + pane kill

### Unit 2: Modify `launch_architect()` to use persistent session
- File: `tools/conductor.sh`, lines 286–322
- Build prompt text instead of prompt file
- Call `ensure_session()` + `send_to_session()` instead of `spawn_agent()`
- Store persistent session pane_id in pipeline state

### Unit 3: Modify `launch_reviewer()` to use persistent session
- File: `tools/conductor.sh`, lines 380–438
- Same pattern as architect

### Unit 4: Modify `check_pipeline()` — don't kill persistent panes
- File: `tools/conductor.sh`, lines 509–632
- Architect completion (line 552): skip pane kill
- Reviewer completion (line 600): skip pane kill
- Executor completion (line 573): keep killing pane (single-shot)

### Unit 5: Startup + shutdown lifecycle
- File: `tools/conductor.sh`
- Startup (after line 675): `mkdir -p $SESSION_DIR`, call `ensure_session` for both roles
- Shutdown (lines 686–698): call `close_session` before `exit 0`
- Restart signal handler: same

### Unit 6: Smoke test
- Start conductor: `bash tools/ozy up`
- Verify two persistent panes appear (architect + reviewer)
- Submit task: `bash tools/ozy task "add a docstring to core/trigger_engine.py"`
- Verify architect receives task via FIFO (check session log for message)
- Verify pipeline completes through all 3 stages
- Submit second task: verify architect reuses same session (no new pane, turn_count increments)

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| FIFO write blocks if Claude isn't reading | Low | High | `send_to_session` could hang. Add timeout via `timeout 5 bash -c 'echo ... >&N'` or background write. |
| fd leak on conductor crash (no graceful shutdown) | Medium | Low | Startup reconciliation: kill old panes, clean old FIFOs in `$SESSION_DIR`. |
| Context window overflow after many tasks | Medium | Medium | Phase 2: token monitoring + session rotation. Phase 1: rely on auto-compaction + session death restart. |
| Persistent session accumulates stale context from failed tasks | Low | Low | Each task prompt is self-contained. Stale context is noise but not harmful. Session rotation (Phase 2) clears it. |
| Race: message sent before previous turn completes | Low | Medium | Pipeline is sequential (one active task at a time). Architect finishes before executor starts. No concurrent sends to same session. |
| JSON escaping bugs in prompt text | Medium | High | Use `jq -Rs` for all escaping. Test with prompts containing quotes, newlines, backticks. |

## Non-Goals

- Persistent executor sessions (worktree isolation requires per-task process)
- Session resume across conductor restarts (fresh sessions on restart is simpler and safer)
- Multi-task concurrency (pipeline is sequential by design)
- Log splitting per task from persistent session log (Phase 2 — audit works via task ID in messages)
- Token-based session rotation (Phase 2)
