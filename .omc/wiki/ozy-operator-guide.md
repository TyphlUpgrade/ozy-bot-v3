---
title: Ozymandias Operator Guide
tags: [ozymandias, operations, operator, trading-bot]
category: pattern
created: 2026-04-09
updated: 2026-04-09
---

# Operator's Guide — Agentic Development Workflow

Quick reference for running the v4 development pipeline. For architecture details
see `docs/agentic-workflow.md`.

---

## Prerequisites

```bash
sudo pacman -S jq tmux inotify-tools   # Arch
# clawhip (prebuilt binary):
git clone https://github.com/Yeachan-Heo/clawhip.git /tmp/clawhip-install
cd /tmp/clawhip-install && CLAWHIP_SKIP_STAR_PROMPT=1 bash install.sh --skip-star-prompt
# Ensure ~/.cargo/bin is on PATH
```

Verify:
```bash
jq --version && tmux -V && claude --version && clawhip --version
```

---

## Starting the Pipeline

```bash
# 1. Create signal directories (first time only, or after clean checkout)
python -c "from ozymandias.core.signals import ensure_signal_dirs; ensure_signal_dirs()"

# 2. Start tmux session
tmux new-session -d -s ozy-dev -c /path/to/ozy-bot-v3

# 3. Start the conductor (inside tmux)
tmux send-keys -t ozy-dev 'export PATH="$HOME/.cargo/bin:$PATH" && ./tools/start_conductor.sh' Enter

# 4. (Optional) Start clawhip for Discord routing
tmux split-window -t ozy-dev
tmux send-keys -t ozy-dev 'clawhip daemon --config clawhip.toml' Enter

# 5. (Optional) Start Discord companion for inbound commands
tmux split-window -t ozy-dev
tmux send-keys -t ozy-dev 'python tools/discord_companion.py' Enter
```

---

## Submitting Tasks

### From the filesystem

```bash
# Human task (highest priority)
echo '{"type":"human","description":"Add trailing stop support","source":"operator"}' \
  > ozymandias/state/agent_tasks/trailing-stop.json

# Bug fix
echo '{"type":"bug","description":"Stop loss placed on wrong side of price","source":"ops_monitor"}' \
  > ozymandias/state/agent_tasks/stop-loss-bug.json
```

The conductor polls every 10 seconds, classifies the task, and starts the pipeline.

### From Discord (requires companion)

```
!fix Stop loss placed on wrong side of price
```

---

## Controlling the Bot

### Inbound signals (trading bot control)

```bash
# Pause new entries (persistent — stays until removed)
touch ozymandias/state/PAUSE_ENTRIES

# Resume entries
rm ozymandias/state/PAUSE_ENTRIES

# Force immediate Claude reasoning cycle (one-shot, consumed on read)
touch ozymandias/state/FORCE_REASONING

# Force immediate watchlist build (one-shot)
touch ozymandias/state/FORCE_BUILD

# Emergency exit all positions
touch ozymandias/state/EMERGENCY_EXIT
```

### Discord commands (requires companion)

| Command | Effect |
|---------|--------|
| `!pause` | Suppress new entries |
| `!resume` | Resume entries |
| `!status` | Show bot status (equity, positions, health) |
| `!exit` | Emergency exit all positions |
| `!force-reasoning` | Trigger Claude reasoning cycle |
| `!fix <description>` | Submit bug fix task to pipeline |
| `!restart-conductor` | Restart conductor wrapper |
| `!shutdown-conductor` | Shut down conductor |
| `!approve <task-id>` | Approve a pending agent permission request |
| `!deny <task-id>` | Deny a pending agent permission request |

---

## Controlling the Conductor

```bash
# Restart (preserves in-progress work)
echo '{}' > ozymandias/state/signals/conductor/restart.json

# Shut down cleanly
echo '{}' > ozymandias/state/signals/conductor/shutdown.json
```

---

## Monitoring

### Conductor log

```bash
# Live tail
tail -f ozymandias/state/logs/conductor.log | jq .

# Recent events
tail -20 ozymandias/state/logs/conductor.log | jq -r '[.ts, .event, .task_id // ""] | join(" ")'
```

### Task classification history

```bash
# See how a task was classified
cat ozymandias/state/logs/judgments/<task-id>/001-classify-output.json | jq .
```

### Bot status

```bash
cat ozymandias/state/signals/status.json | jq .
```

### Active alerts

```bash
ls ozymandias/state/signals/alerts/
cat ozymandias/state/signals/alerts/*.json | jq .
```

### tmux session

```bash
# List all panes (each agent gets its own)
tmux list-panes -t ozy-dev

# Watch a specific pane
tmux select-pane -t ozy-dev:0.1

# Attach to the session
tmux attach -t ozy-dev
```

---

## Shutdown

### Market close (automatic)

The bot writes `session_close` to `status.json`. The conductor stops accepting new tasks.
In-progress executors continue. Everything else keeps running.

### Full shutdown

```bash
# Option A: signal file
echo '{}' > ozymandias/state/signals/conductor/shutdown.json

# Option B: Discord
!shutdown-conductor

# Option C: nuclear (kills everything)
tmux kill-session -t ozy-dev
```

---

## Crash Recovery

| What crashed | What happens | You do |
|-------------|-------------|--------|
| Conductor | Outer loop reads exit intent. Restart intent = auto-restart in 5s. No intent = crash alert written, loop stops. | Re-run `tools/start_conductor.sh` |
| Executor | Worktree + zone file preserved. Conductor detects missing tmux pane, respawns. | Nothing (automatic) |
| Architect/Reviewer | Conductor detects timeout, respawns. | Nothing (automatic) |
| Trading bot | Ops Monitor detects stale `status.json`, restarts (max 3/hour). | Nothing (automatic) |
| clawhip | Discord goes silent. Signal files still work. | Restart clawhip |
| Discord companion | Inbound commands stop. Outbound (clawhip) still works. | Restart companion |
| Permission timeout | Conductor auto-denies after 120s | Add frequently-needed tools to `--allowedTools` in conductor.sh |

---

## Troubleshooting

### Agent stuck (no progress, pane alive)

The agent is likely waiting on a permission prompt. Check:

```bash
# View the agent's tmux pane directly
tmux capture-pane -t <pane-id> -p | tail -20

# Check conductor log for permission requests
grep permission_request ozymandias/state/logs/conductor.log | tail -5
```

If a permission request was sent to Discord, respond with `!approve <task-id>` or `!deny <task-id>`.

### Agent died without signal file

Check the agent log for permission errors:

```bash
cat ozymandias/state/logs/agents/<task-id>/<role>.log | grep -iE "permission|denied|not allowed|abort"
```

**Common causes:**
- Tool not in `--allowedTools` AND conductor didn't detect the prompt before timeout
- `.claude/settings.json` deny rule blocked a command the agent needed

**Fix:** Add the tool to `--allowedTools` in the role's launch function in `tools/conductor.sh`.

### Common failure patterns

| Symptom | Cause | Fix |
|---------|-------|-----|
| Agent pane shows "Allow...?" with no response | Conductor hasn't detected it yet | Wait for next poll cycle (10s), or respond manually in pane |
| `permission_timeout` in conductor log | Operator didn't respond within 120s | Respond faster, or add tool to `--allowedTools` to pre-approve |
| Agent aborts immediately | Tool in `.claude/settings.json` deny list | Remove from deny list if safe, or use different approach |
| Agent loops requesting same tool | Operator denied but agent retries | The agent adapts — if it can't, it will fail and conductor detects |

---

## File Quick Reference

| Path | Purpose |
|------|---------|
| `tools/start_conductor.sh` | Outer restart loop |
| `tools/conductor.sh` | Main conductor logic |
| `tools/discord_companion.py` | Discord inbound commands |
| `clawhip.toml` | Event routing config |
| `config/agent_roles/*.md` | Agent role definitions (7 files) |
| `ozymandias/state/signals/` | Signal file bus |
| `ozymandias/state/agent_tasks/` | Task queue |
| `ozymandias/state/logs/conductor.log` | Conductor event log |
| `ozymandias/state/logs/judgments/` | Classification history |
| `ozymandias/state/logs/agents/` | Per-agent session logs |


---

## Cross-References

- [[ozy-doc-index]] — Full routing table
- [[ozy-completed-phases]] — Phase narratives
- [[ozy-drift-log]] — Active drift log
