#!/usr/bin/env bash
# v5 Harness — Process Manager
# Usage: harness.sh {start|stop|restart|status}
# No -e: wait + $? pattern needs non-zero exits to be captured, not fatal
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$PROJECT_ROOT/config/harness/project.toml"
RUN_DIR="$PROJECT_ROOT/.run"

# --- Helpers ---

_write_pid() { echo "$2" > "$RUN_DIR/$1.pid"; }

# --- cmd_clean ---

cmd_clean() {
  echo "[harness] Cleaning stale artifacts..."

  # Remove orphaned worktrees
  local wt_base="/tmp/harness-worktrees"
  if [[ -d "$wt_base" ]]; then
    local wt
    for wt in "$wt_base"/*/; do
      [[ -d "$wt" ]] || continue
      local task_id
      task_id=$(basename "$wt")
      git -C "$PROJECT_ROOT" worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
      echo "  removed worktree: $task_id"
    done
  fi
  git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true

  # Delete orphaned task branches
  local branch
  for branch in $(git -C "$PROJECT_ROOT" branch --list 'task/*' 2>/dev/null); do
    branch="${branch#  }"  # trim leading whitespace
    git -C "$PROJECT_ROOT" branch -D "$branch" 2>/dev/null && echo "  deleted branch: $branch"
  done

  # Remove stale task files (completed tasks still in queue)
  local state_file="$PROJECT_ROOT/ozymandias/state/signals/conductor/pipeline_state.json"
  if [[ -f "$state_file" ]]; then
    local active
    active=$(python3 -c "import json; print(json.load(open('$state_file')).get('active_task') or '')" 2>/dev/null)
    local task_dir="$PROJECT_ROOT/ozymandias/state/signals/agent_tasks"
    if [[ -d "$task_dir" ]]; then
      local tf
      for tf in "$task_dir"/*.json; do
        [[ -f "$tf" ]] || continue
        local tid
        tid=$(basename "$tf" .json)
        # Don't remove the currently active task
        if [[ "$tid" != "$active" ]]; then
          rm -f "$tf"
          echo "  removed stale task: $tid"
        fi
      done
    fi
  fi

  # Reset pipeline state to idle
  if [[ -f "$state_file" ]]; then
    python3 -c "
import json
s = json.load(open('$state_file'))
s['active_task'] = None
s['stage'] = None
s['stage_agent'] = None
s['worktree'] = None
s['shutdown_ts'] = None
s['plan_summary'] = None
s['diff_stat'] = None
s['review_verdict'] = None
json.dump(s, open('$state_file', 'w'), indent=2)
" 2>/dev/null && echo "  pipeline state reset"
  fi

  echo "[harness] Clean complete."
}

# --- cmd_start ---

cmd_start() {
  if [[ ! -f "$CONFIG" ]]; then
    echo "[harness] ERROR: Config not found: $CONFIG" >&2
    exit 1
  fi

  # Auto-cleanup stale artifacts from previous runs
  cmd_clean

  mkdir -p "$RUN_DIR"
  _write_pid "start" $$

  SHUTDOWN=0
  CLAWHIP_PID=""
  ORCH_PID=""

  cleanup() {
    SHUTDOWN=1
    [[ -n "$CLAWHIP_PID" ]] && kill "$CLAWHIP_PID" 2>/dev/null || true
    [[ -n "$ORCH_PID" ]]    && kill "$ORCH_PID"    2>/dev/null || true
    rm -f "$RUN_DIR/start.pid" "$RUN_DIR/clawhip.pid" "$RUN_DIR/orchestrator.pid"
    exit 0
  }
  trap cleanup INT TERM

  # Load .env and generate clawhip.toml
  if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
  fi
  if [[ -z "${DISCORD_BOT_TOKEN:-}" ]]; then
    echo "[harness] WARNING: DISCORD_BOT_TOKEN is unset — Discord integration will not work" >&2
  fi

  TEMPLATE="$PROJECT_ROOT/config/harness/clawhip.toml.template"
  if [[ -f "$TEMPLATE" ]]; then
    export PROJECT_ROOT
    envsubst '$PROJECT_ROOT $DISCORD_BOT_TOKEN $ALERTS_CHANNEL $AGENT_CHANNEL $OPERATOR_MENTION $DEV_CHANNEL' \
      < "$TEMPLATE" > "$PROJECT_ROOT/clawhip.toml"
    echo "[harness] Generated clawhip.toml from template"
  fi

  # Launch clawhip (if not already running)
  if command -v clawhip >/dev/null 2>&1; then
    if ! pgrep -f "clawhip.*start" >/dev/null 2>&1; then
      clawhip start --config "$PROJECT_ROOT/clawhip.toml" &
      CLAWHIP_PID=$!
      _write_pid "clawhip" "$CLAWHIP_PID"
      echo "[harness] Started clawhip (PID $CLAWHIP_PID)"
      sleep 1
    else
      echo "[harness] clawhip already running"
    fi
  else
    echo "[harness] WARNING: clawhip not found — running without session monitoring"
  fi

  # Restart loop — crash restarts, graceful exit stops, !update restarts immediately
  while true; do
    echo "[harness] Starting orchestrator..."
    python3 "$SCRIPT_DIR/orchestrator.py" --config "$CONFIG" &
    ORCH_PID=$!
    _write_pid "orchestrator" "$ORCH_PID"

    wait "$ORCH_PID"
    EXIT_CODE=$?

    rm -f "$RUN_DIR/orchestrator.pid"
    ORCH_PID=""

    if [[ "$SHUTDOWN" -eq 1 ]]; then
      echo "[harness] Shutdown requested — exiting"
      break
    fi

    # !update sets this flag before triggering graceful shutdown
    if [[ -f "$RUN_DIR/restart_requested" ]]; then
      rm -f "$RUN_DIR/restart_requested"
      echo "[harness] Restart requested (update) — restarting immediately"
      continue
    fi

    if [[ "$EXIT_CODE" -eq 0 ]]; then
      echo "[harness] Orchestrator exited cleanly — not restarting"
      break
    fi

    echo "[harness] Orchestrator crashed (exit $EXIT_CODE). Restarting in 5s..."
    sleep 5
  done

  cleanup
}

# --- cmd_stop ---

cmd_stop() {
  echo "[harness] Stopping..."

  # Kill by PID files first (precise)
  for pid_file in "$RUN_DIR"/*.pid; do
    [ -f "$pid_file" ] || continue
    local pid
    pid=$(cat "$pid_file" 2>/dev/null) || continue
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done

  # Fallback: project-scoped pkill patterns (pkill skips own PID on Linux)
  pkill -f "$PROJECT_ROOT/harness/harness.sh" 2>/dev/null || true
  pkill -f "orchestrator.py.*$PROJECT_ROOT" 2>/dev/null || true
  pkill -f "clawhip.*start" 2>/dev/null || true

  # Kill agent tmux sessions (dynamic discovery)
  local session
  for session in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^agent-'); do
    tmux kill-session -t "$session" 2>/dev/null || true
  done

  sleep 5

  # Force-kill anything still alive
  if pgrep -f "orchestrator.py|$PROJECT_ROOT/harness/harness.sh|clawhip.*start" >/dev/null 2>&1; then
    echo "[harness] Force-killing remaining processes..."
    pkill -9 -f "$PROJECT_ROOT/harness/harness.sh" 2>/dev/null || true
    pkill -9 -f "orchestrator.py" 2>/dev/null || true
    pkill -9 -f "clawhip.*start" 2>/dev/null || true
    sleep 1
  fi

  # Clean up FIFOs and PID dir
  rm -rf /tmp/harness-sessions/ 2>/dev/null || true
  rm -rf "$RUN_DIR" 2>/dev/null || true

  echo "[harness] Stopped."
}

# --- cmd_status ---

cmd_status() {
  local running=0
  local name pid_file pid
  for name in start orchestrator clawhip; do
    pid_file="$RUN_DIR/$name.pid"
    if [[ -f "$pid_file" ]]; then
      pid=$(cat "$pid_file" 2>/dev/null)
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "  $name: running (PID $pid)"
        running=1
      else
        echo "  $name: dead (stale PID file)"
      fi
    else
      echo "  $name: not running"
    fi
  done

  local sessions
  sessions=$(tmux ls -F '#{session_name}' 2>/dev/null | grep '^agent-' || true)
  if [[ -n "$sessions" ]]; then
    echo "  tmux sessions:"
    local s
    for s in $sessions; do echo "    $s"; done
  else
    echo "  tmux sessions: none"
  fi

  [[ "$running" -eq 1 ]] && echo "[harness] Running." || echo "[harness] Not running."
}

# --- Main ---

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; sleep 2; cmd_start ;;
  status)  cmd_status ;;
  clean)   cmd_clean ;;
  *)       echo "Usage: $0 {start|stop|restart|status|clean}" >&2; exit 1 ;;
esac
