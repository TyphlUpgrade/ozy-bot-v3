# v5 Harness Operators Guide

Quick-reference for running the automated trading bot development harness. This guide covers terminal commands, Discord commands, and troubleshooting.

## Terminal Commands

### Start the harness

```bash
./harness/harness.sh start
```

Launches the orchestrator in a restart loop. Automatically:
- Generates `clawhip.toml` from template (requires `.env` with `DISCORD_BOT_TOKEN`)
- Starts the clawhip Discord monitoring daemon (if installed)
- Restarts orchestrator on crash (5-second delay between restarts)
- Exits cleanly on graceful shutdown (exit 0 = no restart)
- Restarts immediately on `!update` (via `.run/restart_requested` flag)

### Stop all processes

```bash
./harness/harness.sh stop
```

Kills:
- Orchestrator and harness.sh restart loop (PID-based, with project-scoped fallback)
- clawhip daemon
- All agent tmux sessions (dynamically discovered)

### Restart

```bash
./harness/harness.sh restart
```

Stops all processes, waits 2 seconds, starts fresh.

### Check process health

```bash
./harness/harness.sh status
```

Reports PID status for start loop, orchestrator, and clawhip. Lists active agent tmux sessions.

### Attach to an agent session

```bash
tmux attach -t agent-executor
tmux attach -t agent-architect
tmux attach -t agent-reviewer

# Detach: Ctrl+B then D
```

### View orchestrator logs

The orchestrator logs to stdout. If running under systemd or in background, check:

```bash
# Recent logs (last 20 lines)
journalctl -u harness -n 20 -f

# Or, if running in foreground:
# logs print directly to terminal
```

### View session logs

Agent and task session logs are stored in `/tmp/harness-sessions/`:

```bash
# List all session logs
ls -lh /tmp/harness-sessions/

# Tail the latest session log
tail -f /tmp/harness-sessions/latest.log
```

## Discord Commands

All commands use the `!` prefix. Commands are case-insensitive.

| Command | Usage | Effect |
|---------|-------|--------|
| `!status` | `!status` | Show pipeline status: running/paused, current stage, active task, agent states |
| `!tell` | `!tell <agent> <message>` | Send feedback to specific agent (architect, executor, or reviewer) |
| `!reply` | `!reply <task_id> <response>` | Reply to escalation dialogue (answer architect's question) |
| `!caveman` | `!caveman status` | Show current compression levels for all agents |
| `!caveman` | `!caveman reset` | Reset all agents to default compression (from config) |
| `!caveman` | `!caveman <agent> <level>` | Set compression for one agent (lite, full, ultra, off) |
| `!caveman` | `!caveman <level>` | Set all agents to same compression level |
| `!update` | `!update` | Pull latest code and gracefully restart harness |

### Natural language messages

Messages without `!` prefix are classified and routed automatically:

- **New task**: "Implement feature X" → queued as new task
- **Feedback**: "That approach won't work because..." → sent to blocked agent (during escalation)
- **Control**: "stop the pipeline" or "pause" → pauses execution (no `!` needed)

### Pipeline control (no `!` prefix required)

| Message | Effect |
|---------|--------|
| `stop` / `pause` / `halt` | Pause pipeline (agents continue current work, no new tasks) |
| `resume` / `unpause` | Resume pipeline (process queued tasks) |

## Discord Channels

| Channel | Purpose |
|---------|---------|
| `dev-agents` | Agent activity: work in progress, signal file changes, heartbeats |
| `ops-monitor` | Operational monitoring: stage transitions, task status |
| `escalations` | Blocking issues requiring operator attention and decisions |

## Pipeline Lifecycle

### Stage flow

```
classify → architect → executor → reviewer → merge → wiki → idle
```

**Classify**: Is the task complex? Routes to architect (complex) or executor (simple).

**Architect**: Read-only planning. Creates design, approves approach.

**Executor**: Full implementation. Writes code, runs tests, commits.

**Reviewer**: Read-only quality gate. Approves merge or requests changes.

**Merge**: Merges worktree branch to main, runs full test suite.

**Wiki**: Documents completed task for knowledge base.

### Escalation tiers

Escalations happen when a task is **blocked** (needs operator input or decision):

| Tier | Who | Action |
|------|-----|--------|
| Tier 1 | Architect | Tries to resolve (replan, simplify scope) |
| Tier 2 | Operator | Receives notification in `#escalations`, decides next step |

When Architect is in Tier 1 escalation, you can:

```
!reply <task_id> <your instruction>
```

The task resumes from where it was blocked (pre-escalation stage).

### Task shelving

While the current task is blocked in escalation, new tasks can start. The orchestrator queues them for execution after the blocked task resolves or is canceled.

## Configuration Files

### Main configuration

**`config/harness/project.toml`**

Key settings:

```toml
[project]
signal_dir = "ozymandias/state/signals"  # Where signal files live
session_dir = "/tmp/harness-sessions"     # Session logs

[pipeline]
poll_interval = 5.0                       # Main loop polling rate (seconds)
max_retries = 3                           # Reviewer rejection retry limit
escalation_timeout = 14400                # 4 hours — re-notify for hanging escalations

[caveman]
default_level = "full"                    # Default compression (off/lite/full/ultra)

[caveman.agents]
architect = "off"                         # Design needs full detail
executor = "full"                         # Code untouched, prose compressed
reviewer = "lite"                         # Professional and tight
```

### Discord template

**`config/harness/clawhip.toml.template`**

Templated with environment variables from `.env` at startup. Defines Discord channels and monitoring rules.

### Environment variables

**`.env` (do not commit)**

```bash
DISCORD_BOT_TOKEN=<token>
DISCORD_TOKEN=<token>
ALERTS_CHANNEL=<channel_id>
DEV_CHANNEL=<channel_id>
ESCALATION_CHANNEL=<channel_id>
```

## Troubleshooting

### Orchestrator keeps restarting

Check logs for crash reason:

```bash
tail -f /tmp/harness-sessions/latest.log | grep -i error
```

Common causes:
- Signal file corruption: inspect `ozymandias/state/signals/`
- Missing config: verify `config/harness/project.toml` exists
- Database lock: wait 30 seconds, orchestrator will auto-retry

### Agent not responding

Check agent tmux session:

```bash
tmux attach -t agent-executor
# Look for errors or stuck prompts
# Ctrl+B D to detach
```

If session is hung, kill it:

```bash
tmux kill-session -t agent-executor
```

The orchestrator will auto-restart persistent agents (architect, reviewer) within 15 minutes.

### Task stuck in escalation

Use `!status` to check current state. If operator input was sent via `!reply` but task hasn't resumed after 5 minutes:

1. Check `#escalations` for architect's response
2. Send a follow-up `!reply` with clarification
3. If still stuck after 10 minutes, escalate to Tier 2 (notify team)

### High token usage

Check current compression levels:

```
!caveman status
```

Reduce for verbose agents:

```
!caveman executor ultra
!caveman architect lite
```

Note: `architect = "off"` is intentional (design quality requires detail). Reduce `executor` or `reviewer` instead.

### Merge failures

If merge fails (branch conflicts), the task is cleared and you're notified. Inspect the branch:

```bash
git checkout task/<task_id>
git log --oneline -5
git diff main...
```

Resolve conflicts manually or ask executor to rebase, then `!tell executor rebase and force-push branch`.

## Quick Checks

**Is the harness healthy?**

```bash
./harness/harness.sh restart
```

Full restart clears corrupted state.

**Did the last task pass tests?**

```
!status
```

Shows task status and stage. If stuck in "merge", check `/tmp/harness-sessions/latest.log` for test output.

**How many retries left?**

```bash
grep "retry_count" /tmp/harness-sessions/latest.log | tail -1
```

Max retries configured in `config/harness/project.toml` (`max_retries = 3`).
