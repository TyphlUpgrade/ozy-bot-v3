#!/usr/bin/env bash
# v5 Harness — Thin Bash Launcher
# Launches clawhip, then runs the Python orchestrator in a restart loop.
# No logic, no state, no polling — just process management.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$PROJECT_ROOT/config/harness/project.toml"

# Verify config exists
if [[ ! -f "$CONFIG" ]]; then
  echo "[harness] ERROR: Config not found: $CONFIG" >&2
  exit 1
fi

# 1. Generate clawhip.toml from template (substitute $PROJECT_ROOT)
TEMPLATE="$PROJECT_ROOT/config/harness/clawhip.toml.template"
if [[ -f "$TEMPLATE" ]]; then
  export PROJECT_ROOT
  envsubst < "$TEMPLATE" > "$PROJECT_ROOT/clawhip.toml"
  echo "[harness] Generated clawhip.toml from template"
fi

# 2. Launch clawhip (if not already running)
if command -v clawhip >/dev/null 2>&1; then
  if ! pgrep -f "clawhip.*start" >/dev/null 2>&1; then
    clawhip start --config "$PROJECT_ROOT/clawhip.toml" &
    echo "[harness] Started clawhip"
    sleep 1
  else
    echo "[harness] clawhip already running"
  fi
else
  echo "[harness] WARNING: clawhip not found — running without session monitoring"
fi

# 3. Restart loop — if orchestrator dies, restart it
while true; do
  echo "[harness] Starting orchestrator..."
  python3 "$SCRIPT_DIR/orchestrator.py" --config "$CONFIG" || true

  echo "[harness] Orchestrator exited. Restarting in 5s..."
  sleep 5
done
