---
title: "TODO: Harness Launcher Script"
tags: [harness, todo, launcher, devex]
category: decision
created: 2026-04-11
---

# TODO: Harness Launcher Script

## Problem

Starting/stopping the harness requires manually sourcing `.env`, killing stale tmux sessions, starting clawhip, resetting pipeline state, and launching the orchestrator. Too many steps, easy to forget one.

## Requirements

A `harness.sh` (or similar) script that handles:

### Start (`harness.sh start`)
1. Source `.env` (bot token, GitHub PAT, any other secrets)
2. Kill stale tmux sessions (`agent-architect`, `agent-executor`, `agent-reviewer`)
3. Kill any leftover orchestrator processes (`pkill -f harness.orchestrator`)
4. Start clawhip daemon (`clawhip start --config clawhip.toml &`)
5. Wait for clawhip port 25294 to be listening
6. Reset pipeline state to idle (clear `active_task`, `stage`, `shutdown_ts`)
7. Clean stale worktree branches if any
8. Launch orchestrator (`PYTHONPATH=harness:. python3 -m harness.orchestrator --config config/harness/project.toml`)

### Stop (`harness.sh stop`)
1. Send SIGTERM to orchestrator (graceful shutdown)
2. Wait for shutdown_ts to appear in pipeline state (or timeout 10s)
3. Kill tmux agent sessions
4. Stop clawhip daemon
5. Report final pipeline state

### Status (`harness.sh status`)
1. Check if orchestrator PID alive
2. Check clawhip daemon health (`clawhip status`)
3. Show current pipeline state (task, stage)
4. List active tmux sessions
5. Show Discord bot connection status

### Cleanup (`harness.sh clean`)
Stale artifacts accumulate across runs and block future tasks. Must clean:
1. Remove stale git worktrees (`git worktree list` → remove any under `/tmp/harness-worktrees/`)
2. Delete orphaned task branches (`git branch --list 'task/*'` → `git branch -D`)
3. Remove completed/stale task files from `agent_tasks/` dir
4. Reset pipeline state to idle
5. Kill orphaned tmux agent sessions
6. Report what was cleaned

**Why this matters:** Worktree creation fails fatally if branch already exists (`fatal: a branch named 'task/X' already exists`). One failed run leaves artifacts that block all subsequent runs until manually cleaned. This is the single biggest pain point in harness operations right now.

### Auto-cleanup on start
`harness.sh start` should run cleanup automatically before launching. Stale state from a previous crashed run should never block a fresh start. Specifically:
- Detect and remove orphaned worktrees (worktree dir exists but orchestrator not running)
- Delete task branches whose worktree no longer exists
- Clear pipeline state if `shutdown_ts` is set (previous run exited)

### Webhook Reply Routing (Discord UX)
When operator replies to an agent's webhook message, the bot should detect the reply, map the parent message's webhook username back to the agent, and route the reply directly — no need for "tell the executor..." preamble. ~20 lines in `on_message`. Depends: webhook per-agent identity (DONE), NL inbound routing (DONE).

### Self-Iteration Test
Drop a real task for the harness to fix itself. First candidate: `clawhip agent task_started` → `clawhip agent started` typo in orchestrator.py. Proves the pipeline can modify its own codebase.

## Notes
- Script should be idempotent — running `start` twice doesn't double-launch
- PID file at `/tmp/harness-orchestrator.pid` for tracking
- All env vars from `.env` exported with `set -a`
- Stale branch cleanup: `git branch --list 'task/*'` from previous failed runs
- Consider `harness.sh start --fresh` flag for full nuke-and-restart
