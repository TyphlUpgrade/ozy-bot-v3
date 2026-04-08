#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Conductor wrapper — deterministic pipeline coordination
#
# Owns: polling, state I/O (jq), git operations, tmux lifecycle, timeout,
#       heartbeat, and all logging infrastructure.
# Does NOT own: judgment calls — those go to `claude -p` on-demand.
# Sequential-first: one task at a time through the full pipeline.
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
ARCHITECT_ROLE="$PROJECT_ROOT/config/agent_roles/architect.md"
EXECUTOR_ROLE="$PROJECT_ROOT/config/agent_roles/executor.md"
REVIEWER_ROLE="$PROJECT_ROOT/config/agent_roles/reviewer.md"
POLL_INTERVAL=10
HEARTBEAT_INTERVAL=60
AGENT_TIMEOUT=600  # 10 min max per agent stage
PERMISSION_TIMEOUT=120  # seconds to wait for Discord approval before auto-deny
TMUX_SESSION="${TMUX_SESSION:-ozy-dev}"

# ---------------------------------------------------------------------------
# Logging helpers
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
# Pipeline helpers
# ---------------------------------------------------------------------------

strip_frontmatter() {
  awk 'BEGIN{fm=0} /^---$/{fm++; next} fm>=2{print}' "$1"
}

# Run a claude -p judgment call. Handles frontmatter stripping and fence parsing.
# Usage: run_judgment <role_file> <input_json> → sets JUDGMENT_RESULT
run_judgment() {
  local role_file="$1" input="$2"
  local role_prompt
  role_prompt=$(strip_frontmatter "$role_file")
  local raw
  if raw=$(echo "$input" | claude -p --permission-mode dontAsk --allowedTools "Read,Glob,Grep" "$role_prompt" 2>/dev/null); then
    JUDGMENT_RESULT=$(echo "$raw" | sed '/^```/d' | jq '.' 2>/dev/null) \
      || JUDGMENT_RESULT='{"error":"response not valid JSON"}'
  else
    JUDGMENT_RESULT='{"error":"claude invocation failed"}'
    return 1
  fi
}

# Spawn a claude -p agent in a tmux pane. Returns pane ID.
# Permission model: --allowedTools pre-approves known tools (no prompt). Tools not
# in the list trigger an interactive permission prompt, which the conductor detects
# via check_permission_prompt() and proxies to Discord for operator approval.
# Per-role tool lists:
#   Architect: Read,Bash,Glob,Grep           (read-only + signal writes via Bash)
#   Executor:  Read,Write,Edit,Bash,Glob,Grep (full access in worktree)
#   Reviewer:  Read,Bash,Glob,Grep           (read-only + signal writes via Bash)
# Usage: pane_id=$(spawn_agent <prompt_file> <workdir> <log_file> [allowed_tools])
spawn_agent() {
  local prompt_file="$1" workdir="$2" log_file="$3" allowed_tools="${4:-}"
  local tools_flag=""
  [ -n "$allowed_tools" ] && tools_flag="--allowedTools $allowed_tools"
  tmux split-window -t "$TMUX_SESSION" -d -P -F '#{pane_id}' \
    "cd '$workdir' && claude -p $tools_flag \"\$(cat '$prompt_file')\" > '$log_file' 2>&1; echo '[AGENT_DONE]' >> '$log_file'"
}

# Check if a tmux pane is still alive.
check_pane_alive() {
  tmux list-panes -t "$TMUX_SESSION" -F '#{pane_id}' 2>/dev/null | grep -qF "$1"
}

# Update orchestrator state fields via jq.
update_state() {
  jq "$1" "$ORCH_STATE" > "$ORCH_STATE.tmp" && mv "$ORCH_STATE.tmp" "$ORCH_STATE"
}

# ---------------------------------------------------------------------------
# Permission proxy — detect prompts in tmux panes, route to Discord
# ---------------------------------------------------------------------------

# Check if a tmux pane is showing a Claude Code permission prompt.
# Captures last 10 lines, greps for known prompt patterns.
# Sets PERM_PROMPT_TEXT on match. Returns 0 if prompt detected.
check_permission_prompt() {
  local pane_id="$1"
  local pane_tail
  pane_tail=$(tmux capture-pane -t "$pane_id" -p -S -10 2>/dev/null) || return 1
  # Claude Code permission prompts contain "Allow" + y/n options
  if echo "$pane_tail" | grep -qiE 'Allow .*(tool|action|command|\?)|\(y\)es.*\(n\)o|yes.*no.*always'; then
    PERM_PROMPT_TEXT=$(echo "$pane_tail" | grep -iE 'Allow|Bash|Read|Write|Edit|tool|command|action' | tail -3 | tr '\n' ' ')
    return 0
  fi
  return 1
}

# Send a permission request to Discord via clawhip CLI (bypasses workspace monitor).
send_permission_request() {
  local task_id="$1" stage="$2" prompt_text="$3"
  local channel="${ALERTS_CHANNEL:-}"
  local mention="${OPERATOR_MENTION:-}"
  if [ -n "$channel" ]; then
    local msg="${mention} **Permission Request** | task: \`${task_id}\` stage: \`${stage}\`
\`\`\`${prompt_text}\`\`\`
Reply: \`!approve ${task_id}\` or \`!deny ${task_id}\`"
    "${HOME}/.cargo/bin/clawhip" send --channel "$channel" --message "$msg" 2>/dev/null || true
  fi
  # Also write signal file as fallback
  local req_file="$SIGNALS_DIR/conductor/permission_request.json"
  mkdir -p "$(dirname "$req_file")"
  local ts; ts=$(date -Iseconds)
  jq -n --arg tid "$task_id" --arg stg "$stage" --arg p "$prompt_text" --arg ts "$ts" \
    '{type:"permission_request",task_id:$tid,stage:$stg,prompt:$p,ts:$ts}' > "$req_file"
  log_event "permission_request_sent" "\"task_id\":\"$task_id\",\"stage\":\"$stage\""
}

# Check for a permission response signal from Discord companion.
# Sets PERM_DECISION on match. Returns 0 if response found for this task.
check_permission_response() {
  local task_id="$1"
  local resp_file="$SIGNALS_DIR/conductor/permission_response.json"
  [ -f "$resp_file" ] || return 1
  local resp_tid
  resp_tid=$(jq -r '.task_id // empty' "$resp_file" 2>/dev/null)
  [ "$resp_tid" = "$task_id" ] || return 1
  PERM_DECISION=$(jq -r '.decision // "deny"' "$resp_file" 2>/dev/null)
  rm -f "$resp_file"
  return 0
}

# Handle a detected permission prompt: proxy to Discord, wait for response,
# send keystroke back to the tmux pane.
handle_permission_prompt() {
  local task_id="$1" stage="$2" pane_id="$3" prompt_text="$4"

  send_permission_request "$task_id" "$stage" "$prompt_text"

  local start_time elapsed
  start_time=$(date +%s)

  while true; do
    if check_permission_response "$task_id"; then
      if [ "$PERM_DECISION" = "approve" ]; then
        tmux send-keys -t "$pane_id" "y" Enter
        log_event "permission_approved" "\"task_id\":\"$task_id\",\"stage\":\"$stage\""
      else
        tmux send-keys -t "$pane_id" "n" Enter
        log_event "permission_denied" "\"task_id\":\"$task_id\",\"stage\":\"$stage\""
      fi
      return 0
    fi

    elapsed=$(( $(date +%s) - start_time ))
    if (( elapsed > PERMISSION_TIMEOUT )); then
      tmux send-keys -t "$pane_id" "n" Enter
      log_event "permission_timeout" "\"task_id\":\"$task_id\",\"stage\":\"$stage\",\"elapsed\":$elapsed"
      return 1
    fi

    sleep 5
  done
}

# ---------------------------------------------------------------------------
# Pipeline stage launchers
# ---------------------------------------------------------------------------

launch_architect() {
  local task_id="$1"
  local task_file="$LOG_DIR/signals/$task_id/001-task.json"
  local prompt_file="$LOG_DIR/agents/$task_id/architect-prompt.md"
  local agent_log="$LOG_DIR/agents/$task_id/architect.log"
  mkdir -p "$LOG_DIR/agents/$task_id" "$SIGNALS_DIR/architect/$task_id"

  local task_desc
  task_desc=$(jq -r '.description // "No description"' "$task_file" 2>/dev/null || echo "No description")

  cat > "$prompt_file" <<PROMPT
$(strip_frontmatter "$ARCHITECT_ROLE")

## Your Task

Task ID: $task_id
Description: $task_desc

## Instructions

1. Read the relevant code in the ozymandias/ directory to understand the current implementation.
2. Create a detailed implementation plan following the plan format in your role definition.
3. Write the plan as valid JSON to this exact path using the Bash tool:
   $SIGNALS_DIR/architect/$task_id/plan.json
4. The plan JSON must include: task_id, category, summary, non_goals, decision_boundaries, units, test_strategy, zone.
5. After writing the plan file, you are done. Do not implement anything.
PROMPT

  local pane_id
  pane_id=$(spawn_agent "$prompt_file" "$PROJECT_ROOT" "$agent_log" "Read,Bash,Glob,Grep")

  update_state "$(printf '.stage = "architect" | .pane_id = "%s" | .stage_started = %d' "$pane_id" "$(date +%s)")"
  log_event "agent_spawned" "\"stage\":\"architect\",\"task_id\":\"$task_id\",\"pane_id\":\"$pane_id\""
}

launch_executor() {
  local task_id="$1"
  local plan_file="$SIGNALS_DIR/architect/$task_id/plan.json"
  local prompt_file="$LOG_DIR/agents/$task_id/executor-prompt.md"
  local agent_log="$LOG_DIR/agents/$task_id/executor.log"
  local branch="feature/$task_id"
  local wt_path="$PROJECT_ROOT/.worktrees/$task_id"

  # Create worktree + branch
  mkdir -p "$PROJECT_ROOT/.worktrees"
  git -C "$PROJECT_ROOT" worktree add "$wt_path" -b "$branch" 2>/dev/null \
    || git -C "$PROJECT_ROOT" worktree add "$wt_path" "$branch" 2>/dev/null \
    || { log_event "error" "\"detail\":\"worktree creation failed\",\"task_id\":\"$task_id\""; return 1; }

  local plan_json
  plan_json=$(cat "$plan_file")

  cat > "$prompt_file" <<PROMPT
$(strip_frontmatter "$EXECUTOR_ROLE")

## Your Task

Task ID: $task_id
Branch: $branch
Worktree: $wt_path

## Architect's Plan

$plan_json

## Instructions

1. Implement each unit in the plan sequentially. Do not skip or reorder.
2. After each unit, commit your changes with message format: "<type>: <description>\n\nZone: $task_id, unit <N>"
3. Run tests after each unit: python -m pytest (from the worktree root).
4. When all units are complete, write a completion signal as valid JSON using the Bash tool:
   echo '{"status":"complete","task_id":"$task_id","test_status":"passing"}' > $SIGNALS_DIR/executor/completion-$task_id.json
5. If tests fail and you cannot fix them, write:
   echo '{"status":"failed","task_id":"$task_id","test_status":"failing","error":"<description>"}' > $SIGNALS_DIR/executor/completion-$task_id.json
PROMPT

  local pane_id
  pane_id=$(spawn_agent "$prompt_file" "$wt_path" "$agent_log" "Read,Write,Edit,Bash,Glob,Grep")

  update_state "$(printf '.stage = "executor" | .pane_id = "%s" | .stage_started = %d | .worktree = "%s" | .branch = "%s"' \
    "$pane_id" "$(date +%s)" "$wt_path" "$branch")"
  log_event "agent_spawned" "\"stage\":\"executor\",\"task_id\":\"$task_id\",\"pane_id\":\"$pane_id\""
}

launch_reviewer() {
  local task_id="$1"
  local plan_file="$SIGNALS_DIR/architect/$task_id/plan.json"
  local prompt_file="$LOG_DIR/agents/$task_id/reviewer-prompt.md"
  local agent_log="$LOG_DIR/agents/$task_id/reviewer.log"
  local branch
  branch=$(jq -r '.branch // empty' "$ORCH_STATE" 2>/dev/null)
  mkdir -p "$SIGNALS_DIR/reviewer/$task_id"

  # Capture the diff for the reviewer
  local diff_content
  diff_content=$(git -C "$PROJECT_ROOT" diff "main...$branch" 2>/dev/null || echo "(diff unavailable)")

  local plan_json
  plan_json=$(cat "$plan_file" 2>/dev/null || echo "{}")

  cat > "$prompt_file" <<PROMPT
$(strip_frontmatter "$REVIEWER_ROLE")

## Your Task

Task ID: $task_id
Branch: $branch

## Architect's Plan

$plan_json

## Changes to Review

\`\`\`diff
$diff_content
\`\`\`

## Instructions

1. Review the changes against the Architect's plan.
2. Apply the Contrarian pressure-test and score risk dimensions.
3. Check all trading convention rules listed in your role definition.
4. Run the test suite: cd $PROJECT_ROOT && python -m pytest
5. Write your verdict as valid JSON using the Bash tool:
   $SIGNALS_DIR/reviewer/$task_id/verdict.json
6. The verdict JSON must include: task_id, verdict (approve/reject/request_changes), tier, contrarian_score, checklist, findings, summary.
PROMPT

  local pane_id
  pane_id=$(spawn_agent "$prompt_file" "$PROJECT_ROOT" "$agent_log" "Read,Bash,Glob,Grep")

  update_state "$(printf '.stage = "reviewer" | .pane_id = "%s" | .stage_started = %d' "$pane_id" "$(date +%s)")"
  log_event "agent_spawned" "\"stage\":\"reviewer\",\"task_id\":\"$task_id\",\"pane_id\":\"$pane_id\""
}

do_merge() {
  local task_id="$1"
  local branch
  branch=$(jq -r '.branch // empty' "$ORCH_STATE" 2>/dev/null)
  local wt_path
  wt_path=$(jq -r '.worktree // empty' "$ORCH_STATE" 2>/dev/null)

  log_event "merge_start" "\"task_id\":\"$task_id\",\"branch\":\"$branch\""

  # Stash any uncommitted changes in the working tree so merge can proceed.
  # This is common during development when new files haven't been committed yet.
  local stashed=false
  if ! git -C "$PROJECT_ROOT" diff --quiet 2>/dev/null || ! git -C "$PROJECT_ROOT" diff --cached --quiet 2>/dev/null; then
    git -C "$PROJECT_ROOT" stash push -u -m "conductor-merge-$task_id" 2>/dev/null && stashed=true
    log_event "stash_push" "\"task_id\":\"$task_id\""
  fi

  # Merge into main
  local merge_sha merge_ok=true
  if merge_sha=$(git -C "$PROJECT_ROOT" merge "$branch" --no-edit 2>&1 | grep -oP '[0-9a-f]{7,}' | head -1); then
    log_event "merge_complete" "\"task_id\":\"$task_id\",\"sha\":\"$merge_sha\""
  else
    log_event "merge_failed" "\"task_id\":\"$task_id\",\"branch\":\"$branch\""
    merge_ok=false
  fi

  # Pop stash regardless of merge outcome
  if $stashed; then
    git -C "$PROJECT_ROOT" stash pop 2>/dev/null || {
      log_event "stash_pop_conflict" "\"task_id\":\"$task_id\""
      # Stash is preserved — operator can resolve with git stash show / git stash pop
    }
    log_event "stash_pop" "\"task_id\":\"$task_id\""
  fi

  $merge_ok || return 1

  # Run post-merge tests
  if (cd "$PROJECT_ROOT" && python -m pytest -q --tb=line 2>&1 | tail -5) | grep -q "passed"; then
    log_event "post_merge_tests" "\"task_id\":\"$task_id\",\"result\":\"passed\""
  else
    log_event "post_merge_tests" "\"task_id\":\"$task_id\",\"result\":\"failed\""
    # Revert the merge using the captured SHA
    git -C "$PROJECT_ROOT" revert --no-edit HEAD 2>/dev/null || true
    log_event "merge_reverted" "\"task_id\":\"$task_id\""
    return 1
  fi

  # Clean up worktree
  if [ -n "$wt_path" ] && [ -d "$wt_path" ]; then
    git -C "$PROJECT_ROOT" worktree remove "$wt_path" --force 2>/dev/null || true
    git -C "$PROJECT_ROOT" branch -d "$branch" 2>/dev/null || true
  fi

  # Mark task complete
  update_state "$(printf '.active_task = null | .stage = null | .pane_id = null | .stage_started = null | .worktree = null | .branch = null | .last_merge = "%s" | .completed_tasks += ["%s"]' \
    "$(date -Iseconds)" "$task_id")"
  log_event "task_completed" "\"task_id\":\"$task_id\""
}

# ---------------------------------------------------------------------------
# Pipeline progress checker — called each poll when a task is active
# ---------------------------------------------------------------------------

check_pipeline() {
  local task_id="$1"
  local stage pane_id stage_started
  stage=$(jq -r '.stage // empty' "$ORCH_STATE" 2>/dev/null)
  pane_id=$(jq -r '.pane_id // empty' "$ORCH_STATE" 2>/dev/null)
  stage_started=$(jq -r '.stage_started // 0' "$ORCH_STATE" 2>/dev/null)

  [ -z "$stage" ] && return

  # Check for permission prompt before anything else — proxy to Discord
  if [ -n "$pane_id" ] && check_pane_alive "$pane_id" && check_permission_prompt "$pane_id"; then
    handle_permission_prompt "$task_id" "$stage" "$pane_id" "$PERM_PROMPT_TEXT"
    return  # Let the agent resume; check signal files next cycle
  fi

  # Check for timeout
  local now elapsed
  now=$(date +%s)
  elapsed=$((now - stage_started))
  if (( elapsed > AGENT_TIMEOUT )); then
    log_event "agent_timeout" "\"stage\":\"$stage\",\"task_id\":\"$task_id\",\"elapsed\":$elapsed"
    # Kill the pane if still alive
    if [ -n "$pane_id" ] && check_pane_alive "$pane_id"; then
      tmux kill-pane -t "$pane_id" 2>/dev/null || true
    fi
    # Diagnose failure
    update_state '.stage = "failed"'
    log_event "task_failed" "\"task_id\":\"$task_id\",\"reason\":\"agent_timeout in $stage\""
    # Clear active task so conductor can pick up the next one
    update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
    return
  fi

  case $stage in
    architect)
      local plan_file="$SIGNALS_DIR/architect/$task_id/plan.json"
      if [ -f "$plan_file" ] && jq empty "$plan_file" 2>/dev/null; then
        # Plan delivered — transition to executor
        log_event "stage_complete" "\"stage\":\"architect\",\"task_id\":\"$task_id\""
        # Kill architect pane if still lingering
        if [ -n "$pane_id" ] && check_pane_alive "$pane_id"; then
          tmux kill-pane -t "$pane_id" 2>/dev/null || true
        fi
        launch_executor "$task_id"
      elif [ -n "$pane_id" ] && ! check_pane_alive "$pane_id"; then
        # Pane died without writing plan
        log_event "agent_died" "\"stage\":\"architect\",\"task_id\":\"$task_id\""
        update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
        log_event "task_failed" "\"task_id\":\"$task_id\",\"reason\":\"architect died without plan\""
      fi
      ;;

    executor)
      local completion_file="$SIGNALS_DIR/executor/completion-$task_id.json"
      if [ -f "$completion_file" ]; then
        local exec_status
        exec_status=$(jq -r '.status // "unknown"' "$completion_file" 2>/dev/null)
        log_event "stage_complete" "\"stage\":\"executor\",\"task_id\":\"$task_id\",\"status\":\"$exec_status\""
        # Kill executor pane if still lingering
        if [ -n "$pane_id" ] && check_pane_alive "$pane_id"; then
          tmux kill-pane -t "$pane_id" 2>/dev/null || true
        fi
        if [ "$exec_status" = "complete" ]; then
          launch_reviewer "$task_id"
        else
          log_event "task_failed" "\"task_id\":\"$task_id\",\"reason\":\"executor reported: $exec_status\""
          update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
        fi
      elif [ -n "$pane_id" ] && ! check_pane_alive "$pane_id"; then
        # Pane died without completion signal
        log_event "agent_died" "\"stage\":\"executor\",\"task_id\":\"$task_id\""
        update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
        log_event "task_failed" "\"task_id\":\"$task_id\",\"reason\":\"executor died without completion signal\""
      fi
      ;;

    reviewer)
      local verdict_file="$SIGNALS_DIR/reviewer/$task_id/verdict.json"
      if [ -f "$verdict_file" ] && jq empty "$verdict_file" 2>/dev/null; then
        local verdict
        verdict=$(jq -r '.verdict // "unknown"' "$verdict_file" 2>/dev/null)
        log_event "stage_complete" "\"stage\":\"reviewer\",\"task_id\":\"$task_id\",\"verdict\":\"$verdict\""
        # Kill reviewer pane if still lingering
        if [ -n "$pane_id" ] && check_pane_alive "$pane_id"; then
          tmux kill-pane -t "$pane_id" 2>/dev/null || true
        fi
        case $verdict in
          approve)
            do_merge "$task_id" || {
              log_event "task_failed" "\"task_id\":\"$task_id\",\"reason\":\"merge failed\""
              update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
            }
            ;;
          reject|request_changes)
            log_event "review_rejected" "\"task_id\":\"$task_id\",\"verdict\":\"$verdict\""
            # For now, fail the task. Future: replan cycle.
            update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
            ;;
          *)
            log_event "review_unknown" "\"task_id\":\"$task_id\",\"verdict\":\"$verdict\""
            update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
            ;;
        esac
      elif [ -n "$pane_id" ] && ! check_pane_alive "$pane_id"; then
        log_event "agent_died" "\"stage\":\"reviewer\",\"task_id\":\"$task_id\""
        update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null'
        log_event "task_failed" "\"task_id\":\"$task_id\",\"reason\":\"reviewer died without verdict\""
      fi
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

mkdir -p "$LOG_DIR/agents" "$LOG_DIR/signals" "$LOG_DIR/judgments" "$LOG_DIR/summaries"
mkdir -p "$TASKS_DIR" "$SIGNALS_DIR/conductor" "$SIGNALS_DIR/orchestrator"
mkdir -p "$STATE_DIR/staging" "$PROJECT_ROOT/.worktrees"

# Initialize orchestrator state if absent
if [ ! -f "$ORCH_STATE" ]; then
  echo '{"active_task":null,"stage":null,"pane_id":null,"stage_started":null,"worktree":null,"branch":null,"completed_tasks":[],"last_merge":null}' > "$ORCH_STATE"
fi

# Log rotation: archive conductor.log if >1MB
if [ -f "$CONDUCTOR_LOG" ] && [ "$(stat -c%s "$CONDUCTOR_LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  mv "$CONDUCTOR_LOG" "$CONDUCTOR_LOG.$(date +%Y%m%d).bak"
fi

# Agent log compression: gzip logs older than 7 days
find "$LOG_DIR/agents" -name "*.log" -mtime +7 -exec gzip -q {} \; 2>/dev/null || true

log_event "startup" "\"version\":\"2.2\",\"pid\":$$"

# Reconcile: if orch state shows an active task, check if its worktree still exists
active_task=$(jq -r '.active_task // empty' "$ORCH_STATE" 2>/dev/null)
if [ -n "$active_task" ]; then
  stage=$(jq -r '.stage // empty' "$ORCH_STATE" 2>/dev/null)
  wt_path=$(jq -r '.worktree // empty' "$ORCH_STATE" 2>/dev/null)
  if [ -n "$wt_path" ] && [ ! -d "$wt_path" ]; then
    log_event "reconcile" "\"task_id\":\"$active_task\",\"detail\":\"worktree missing, clearing active task\""
    update_state '.active_task = null | .stage = null | .pane_id = null | .stage_started = null | .worktree = null | .branch = null'
  elif [ -n "$stage" ]; then
    log_event "reconcile" "\"task_id\":\"$active_task\",\"stage\":\"$stage\",\"detail\":\"resuming — checking pipeline\""
  fi
fi

# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

start_time=$(date +%s)
last_heartbeat=$start_time

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
    active_task=$(jq -r '.active_task // empty' "$ORCH_STATE" 2>/dev/null)
    stage=$(jq -r '.stage // "idle"' "$ORCH_STATE" 2>/dev/null)
    log_event "heartbeat" "\"uptime_s\":$((now - start_time)),\"active_task\":\"${active_task:-none}\",\"stage\":\"$stage\""
    last_heartbeat=$now
  fi

  # -- Active task: check pipeline progress ---------------------------------
  active_task=$(jq -r '.active_task // empty' "$ORCH_STATE" 2>/dev/null)

  if [ -n "$active_task" ]; then
    check_pipeline "$active_task"
  else
    # -- No active task: check for new tasks --------------------------------
    new_task=$(find "$TASKS_DIR" -name "*.json" -type f 2>/dev/null | sort | head -1)

    if [ -n "$new_task" ]; then
      task_content=$(cat "$new_task")
      task_id=$(basename "$new_task" .json)
      log_event "signal_detected" "\"type\":\"new_task\",\"task_id\":\"$task_id\""

      # -- Judgment call: classify task ------------------------------------
      classify_input=$(jq -n \
        --arg judgment "classify_task" \
        --argjson task_file "$task_content" \
        --argjson active_tasks '[]' \
        --argjson state_summary "$(jq '{active_count: (if .active_task then 1 else 0 end), last_merge}' "$ORCH_STATE")" \
        '{judgment: $judgment, task_file: $task_file, active_tasks: $active_tasks, orchestrator_state_summary: $state_summary}')

      mkdir -p "$LOG_DIR/judgments/$task_id"
      echo "$classify_input" > "$LOG_DIR/judgments/$task_id/001-classify-input.json"
      log_event "judgment_call" "\"reason\":\"task_classify\",\"task_id\":\"$task_id\""

      if run_judgment "$CONDUCTOR_ROLE" "$classify_input"; then
        classify_output="$JUDGMENT_RESULT"
      else
        classify_output='{"action":"defer","reason":"claude invocation failed"}'
        log_event "error" "\"detail\":\"claude -p classify failed\",\"task_id\":\"$task_id\""
      fi
      echo "$classify_output" > "$LOG_DIR/judgments/$task_id/001-classify-output.json"
      log_event "judgment_result" "\"task_id\":\"$task_id\""

      action=$(echo "$classify_output" | jq -r '.action // "defer"')

      case $action in
        accept)
          mkdir -p "$LOG_DIR/signals/$task_id"
          cp "$new_task" "$LOG_DIR/signals/$task_id/001-task.json"
          rm -f "$new_task"

          update_state "$(printf '.active_task = "%s"' "$task_id")"
          log_event "task_accepted" "\"task_id\":\"$task_id\""

          # Start the pipeline: launch architect
          launch_architect "$task_id"
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

  sleep "$POLL_INTERVAL"
done
