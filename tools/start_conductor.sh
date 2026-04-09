#!/bin/bash
# Outer restart loop with exit intent file dispatch.
# Run in tmux pane 5: tmux send-keys -t ozymandias:0.5 "bash tools/start_conductor.sh" Enter
#
# The conductor wrapper writes an intent file before every planned exit.
# This loop reads that file to decide: restart, shutdown, or alert on crash.
# No intent file = unclean death = alert the operator and stop.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INTENT_FILE="$PROJECT_ROOT/ozymandias/state/signals/conductor/exit_intent.json"
ALERTS_DIR="$PROJECT_ROOT/ozymandias/state/signals/alerts"

while true; do
  # Clean stale intent file BEFORE starting wrapper.
  # Disk persists — a leftover from a previous crash would be misread as fresh.
  rm -f "$INTENT_FILE"

  bash "$SCRIPT_DIR/conductor.sh"
  ts=$(date -Iseconds)

  # Read intent file. Wrapper writes this before every planned exit.
  # No file = wrapper didn't get a chance to write = unclean death.
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
      mkdir -p "$ALERTS_DIR"
      echo "{\"type\":\"conductor_crash\",\"reason\":\"$reason\",\"ts\":\"$ts\"}" \
        > "$ALERTS_DIR/conductor_crash.json"
      break
      ;;
  esac
done
