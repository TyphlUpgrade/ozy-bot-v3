# Agentic Development Workflow — Operator & Agent Reference

Development infrastructure that builds and maintains the Ozymandias trading bot. Not part of the bot itself.

**Design doc:** `plans/2026-04-07-agentic-workflow-v4-omc-only.md`

## Quick Start

```bash
bash tools/ozy up       # start all services
bash tools/ozy status   # check what's running
bash tools/ozy task "fix the fill handler race condition"  # submit task
bash tools/ozy logs     # tail conductor log
bash tools/ozy restart  # restart conductor only
bash tools/ozy down     # graceful shutdown
```

From Discord: `!fix <description>` submits a task. `!help` lists all commands.

## Architecture

```
Operator (Discord or terminal)
    ↕
clawhip (event routing daemon) + Discord companion (inbound commands)
    ↕
ozy launcher (tools/ozy — tmux lifecycle, start/stop/status)
    ↕
Conductor wrapper (tools/conductor.sh — deterministic bash)
    ↕ spawns via tmux (full OMC ecosystem), judges via claude -p
Agent sessions (Architect → Executor → Reviewer)
    ↕
Signal file bus (state/signals/ — JSON files, atomic writes)
    ↕
Trading bot (ozymandias/ — reads inbound signals, writes status/alerts)
```

Three separated concerns:
- **Deterministic coordination** — conductor.sh: polling, state I/O, git, tmux lifecycle. Stateless `claude -p` for judgment calls. Persistent multi-turn sessions available via `--input-format stream-json` + FIFO for agents that benefit from context accumulation (architect, reviewer).
- **Agent execution** — Claude Code instances in `-p` mode with full OMC tool ecosystem (LSP, AST grep, python REPL, notepad, project memory). OMC hooks fire in `-p` mode (SessionStart, PreToolUse, PostToolUse, Stop). Each agent runs in a tmux pane with JSONL audit trail.
- **Event routing** — clawhip: watches signal files + git, routes to Discord channels. Discord companion: translates inbound commands to signal files.

## Services

| Service | Binary | Start command | Notes |
|---------|--------|---------------|-------|
| Conductor | `claude` CLI + `bash` | `bash tools/start_conductor.sh` | Outer restart loop wraps conductor.sh. Reads exit intent to decide restart/shutdown/crash. |
| Discord companion | `python` + `discord.py` | `python tools/discord_companion.py` | Requires `DISCORD_BOT_TOKEN` env var. Standalone — no ozymandias imports. |
| clawhip | `~/.cargo/bin/clawhip` | `clawhip start --config clawhip.toml` | Daemon on port 25294. May already be running system-wide. |

All three require `.env` sourced for Discord tokens and channel IDs.

`tools/ozy up` manages all three: creates tmux panes, sources `.env`, checks prerequisites, handles idempotency. `tools/ozy down` sends conductor shutdown signal, waits for graceful exit, then stops companion and clawhip.

## CLI Reference (`tools/ozy`)

| Command | Action |
|---------|--------|
| `ozy up` | Pre-flight checks, ensure dirs, create tmux panes, start all services. Idempotent. |
| `ozy down` | Write shutdown signal to conductor, wait up to 10s, Ctrl-C companion/clawhip. |
| `ozy restart` | Write restart signal — conductor exits and outer loop relaunches it. Companion/clawhip stay running. |
| `ozy status` | Show per-service running/stopped, active task, pending task count. |
| `ozy logs` | `tail -f` conductor log with jq pretty-printing. |
| `ozy task "DESC"` | Write task JSON to `state/agent_tasks/`. Conductor picks up within ~10s. |

Environment: `TMUX_SESSION` (default: `ozy-dev`).

Pre-flight checks: `claude` CLI, `jq`, `tmux` (fatal). `.env`, `clawhip`, `discord.py` (warn).

## Discord Commands

Handled by `tools/discord_companion.py`. Intent filter blocks informational questions (`what is`, `how does`, `explain`, `tell me about`, `describe`).

| Command | Action | Mechanism |
|---------|--------|-----------|
| `!fix <desc>` | Submit task to agent pipeline | Write to `state/agent_tasks/` |
| `!pause` | Suppress new entries | Touch `state/PAUSE_ENTRIES` |
| `!resume` | Resume entries | Remove `state/PAUSE_ENTRIES` |
| `!status` | Show bot equity, positions, health | Read `state/signals/status.json` |
| `!exit` | Emergency exit all positions | Touch `state/EMERGENCY_EXIT` |
| `!force-reasoning` | Trigger Claude reasoning cycle | Touch `state/FORCE_REASONING` |
| `!restart-conductor` | Restart conductor | Write `conductor/restart.json` |
| `!shutdown-conductor` | Shut down conductor | Write `conductor/shutdown.json` |
| `!approve <task-id>` | Approve pending permission | Write `conductor/permission_response.json` |
| `!deny <task-id>` | Deny pending permission | Write `conductor/permission_response.json` |
| `!help` | List commands | — |

## Agent Roles

| Role | File | Model | Mode | Tools | Key behavior |
|------|------|-------|------|-------|--------------|
| Conductor | `config/agent_roles/conductor.md` | Sonnet | `claude -p` (stateless) | Read, Glob, Grep | Classify tasks, assemble context, diagnose failures. JSON in, JSON out. |
| Architect | `config/agent_roles/architect.md` | Opus | ephemeral | All + OMC MCP. **Write/Edit/filesystem-write denied** | Read codebase, produce plan.json. LSP navigation, AST grep. Intent classification gate. |
| Executor | `config/agent_roles/executor.md` | Sonnet | ephemeral (worktree) | All + OMC MCP. No restrictions. | Implement plan units. LSP diagnostics, AST grep/replace, python REPL. Commit per unit. |
| Reviewer | `config/agent_roles/reviewer.md` | Sonnet | ephemeral | All + OMC MCP. **Write/Edit/filesystem-write denied** | Contrarian pressure-test. LSP diagnostics. Three verification tiers. 8-point convention check. |
| Ops Monitor | `config/agent_roles/ops_monitor.md` | Haiku | persistent | Read, Bash (limited), Glob, Grep | Anomaly detection, auto-restart (3/hr), bug report generation (3/hr), daily summary. |
| Dialogue | `config/agent_roles/dialogue.md` | Sonnet | persistent | Read, Write (plans/state), Bash, Glob, Grep | Strategy partner. All three pressure-test personas. Ambiguity scoring (0.20 threshold). |
| Strategy Analyst | `config/agent_roles/strategy_analyst.md` | Sonnet | ephemeral (post-market) | Read, Bash, Glob, Grep. **No Write/Edit** | 4-category outcome classification. Hindsight bias gate. Ontologist dedup. |

## Pipeline Flow

```
1. Task appears (Discord !fix or ozy task or Ops Monitor bug report)
   ↓
2. Conductor detects task file in state/agent_tasks/
   ↓
3. classify_task judgment (claude -p) → accept / defer / reject
   → Discord notification: "Task accepted" or "Task rejected"
   ↓
4. Architect spawns (Opus, tmux pane, full OMC tools, Write/Edit denied)
   → Reads codebase via LSP/AST grep, writes plan to state/signals/architect/<task-id>/plan.json
   → Discord notification: "Architect done — plan delivered"
   ↓
5. Conductor creates git worktree (.worktrees/<task-id>), spawns Executor (Sonnet, full access)
   → Implements units sequentially using LSP diagnostics, AST grep/replace, commits per unit, runs tests
   → Writes completion signal to state/signals/executor/completion-<task-id>.json
   → Discord notification: "Executor done — tests passing"
   ↓
6. Reviewer spawns (Sonnet, full OMC tools, Write/Edit denied)
   → Contrarian pressure-test, LSP diagnostics, convention checks, runs tests
   → Writes verdict to state/signals/reviewer/<task-id>/verdict.json
   → Discord notification: "Reviewer approved" or "Reviewer: reject/request_changes"
   ↓
7. If approved: Conductor merges branch, runs post-merge tests
   → If tests fail: auto-revert merge
   → Clean up worktree and branch
   → Discord notification: "Task complete — merged and tests passing"
```

Checkpoints: Architect marks certain units as checkpoints. Executor pauses at checkpoints, writes checkpoint signal, waits for Architect review before continuing.

Timeout: 10 minutes per agent stage. Exceeded → kill pane, fail task, Discord notification.

## Conductor Judgment Calls

Three `claude -p` invocation types. Each is stateless — fresh session, focused JSON I/O.

### classify_task
Input: task file, active tasks, orchestrator state. Output: `{action, priority, reason}`.
- Priority: human > bug > strategy_analysis > backlog
- Reject if: duplicate of active task, stale TTL exceeded (bugs 2h, strategy 8h, human: no TTL)
- Defer if: another task active (sequential-first)
- Accept if: no active task and valid

### assemble_context
Input: task packet, zone files, drift log. Output: `{relevant_files, domain_context, known_concerns}`.

### diagnose_failure
Input: task_id, zone file, failure history, agent log tail. Output: `{decision, notes, architect_hint}`.
- Replan: wrong approach. Escalate: needs human. Retry simpler: transient (timeout, flaky test).

## Pressure-Testing Protocols

Three adversarial personas enforced across roles:

| Persona | Question | Used by | Threshold |
|---------|----------|---------|-----------|
| Contrarian | "What breaks if this interacts with X?" | Reviewer, Dialogue | 0.25 (weighted risk score) |
| Simplifier | "Can we get 80% with less code?" | Executor, Dialogue | 0.15 |
| Ontologist | "Is this new, or an instance of something we have?" | Strategy Analyst, Dialogue | Dedup gate |

Reviewer Contrarian dimensions: correctness risk (0.30), integration risk (0.25), state corruption (0.20), performance risk (0.15), regression risk (0.10). Score >= 0.25 → mandatory reject.

Dialogue ambiguity scoring: intent (0.25), outcome (0.20), scope (0.20), constraints (0.15), success criteria (0.10), context (0.10). Score >= 0.20 → must ask clarifying questions before proceeding.

## Permission Model

Two layers enforce agent boundaries:

| Layer | Mechanism | Behavior |
|-------|-----------|----------|
| Auto-approve | `--permission-mode dontAsk` on all agents | All tools available without prompting |
| Deny list | `--disallowedTools` per role | Tools removed from agent's context entirely |
| Hard deny | `.claude/settings.json` deny rules | Bash patterns blocked (rm -rf, sudo, force push, etc.) |

Per-role `--disallowedTools` (blacklist model — everything else is allowed):

| Role | Denied tools | Model | Why |
|------|-------------|-------|-----|
| Judgment | N/A (uses `--allowedTools Read,Glob,Grep`) | Sonnet | Stateless classification, no MCP needed |
| Architect | Write, Edit, NotebookEdit + mcp filesystem writes | Opus | Reads codebase, must not modify source. Bash writes allowed for signal files. |
| Executor | *(none)* | Sonnet | Full access in isolated worktree |
| Reviewer | Write, Edit, NotebookEdit + mcp filesystem writes | Sonnet | Reviews code, must not modify source. Bash writes allowed for verdict signal. |

OMC MCP tools available to agents: LSP (goto-definition, find-references, hover, diagnostics, code-actions, rename, workspace-symbols), AST grep (search/replace), python REPL, notepad, project memory, state management, session search, trace tools. OMC hooks fire on every tool call (PreToolUse, PostToolUse).

Discord permission proxy (`check_permission_prompt`, `handle_permission_prompt`) is retained as a safety net but currently inactive — `--permission-mode dontAsk` auto-approves all tools before prompts can appear.

## Persistent Sessions via `--input-format stream-json`

**Status:** Verified working (2026-04-08). Officially undocumented (GitHub #24594, closed "not planned").

Agents can run as persistent multi-turn sessions instead of single-shot `-p` calls. This enables context accumulation, token savings (system prompt loaded once), and caveman compression across tasks.

### Protocol

Required flags: `claude -p --verbose --input-format stream-json --output-format stream-json`

Input message format (one JSONL line per message):
```json
{"type":"user","message":{"role":"user","content":"your prompt here"}}
```

**Critical:** The type field is `"user"`, not `"user_message"`. The latter silently fails (hooks fire, no response, clean exit 0).

### FIFO Pattern for Multi-Turn

```bash
FIFO="/tmp/agent-session-$$"
mkfifo "$FIFO"

# Start persistent session
claude -p --verbose --input-format stream-json --output-format stream-json \
  --permission-mode dontAsk $DENY_FLAG $MODEL_FLAG \
  < "$FIFO" > "$LOG_FILE" 2>&1 &
AGENT_PID=$!

# Open write end — keeps session alive (no EOF until we close)
exec 3>"$FIFO"

# Send messages over time
echo '{"type":"user","message":{"role":"user","content":"..."}}' >&3
# ... wait for result in log ...
echo '{"type":"user","message":{"role":"user","content":"..."}}' >&3

# End session (sends EOF)
exec 3>&-
wait $AGENT_PID
rm -f "$FIFO"
```

### Output Format

JSONL with event types:
- `system:init` — session initialization (tool list, model info)
- `system:hook_started` / `system:hook_response` — hook lifecycle (with `--include-hook-events`)
- `assistant` — model response (content blocks in `.message.content[]`)
- `result:success` — turn completion (`.result` has final text, `.num_turns`, `.duration_ms`)
- `rate_limit_event` — rate limit status

### Verified Behaviors

| Behavior | Status |
|----------|--------|
| Multi-turn context retention | Works — remembers previous turns |
| Tool usage across turns | Works — Read, Glob, MCP tools all fire |
| OMC hooks | SessionStart fires once; PreToolUse/PostToolUse fire per tool call |
| `--permission-mode dontAsk` | Works with stream-json |
| `--disallowedTools` | Works — denied tools removed from context |
| Session ends on EOF | Yes — close FIFO write-end to terminate |
| Caveman compression | Invoke `/caveman` in first turn, persists for session (not yet tested in pipeline) |

### Conductor Integration (Future)

Persistent sessions enable:
- **Architect**: Start once, send multiple analysis requests. Builds codebase understanding over tasks.
- **Reviewer**: Start once, review multiple changes. Consistent review context.
- **Executor**: Remains per-task (isolated worktree per task requires fresh session).
- **Token savings**: ~25K system context tokens loaded once per session instead of per task.

## Signal File Bus

Universal communication layer. All signals use atomic JSON writes. No module imports another's code.

### Directory structure

```
ozymandias/state/
├── agent_tasks/               # Task queue (conductor polls this)
├── signals/
│   ├── status.json            # Bot status (fast loop overwrites)
│   ├── last_trade.json        # Most recent fill
│   ├── last_review.json       # Most recent Claude review
│   ├── alerts/                # Append-only (microsecond timestamps)
│   ├── orchestrator/          # orchestrator_state.json (pipeline state)
│   ├── conductor/             # Control: restart.json, shutdown.json, permission_*.json
│   ├── architect/<task-id>/   # plan.json
│   ├── executor/              # completion-<task-id>.json, checkpoint.json
│   ├── reviewer/<task-id>/    # verdict.json
│   ├── analyst/<date>/        # findings.json
│   └── dialogue/              # response.json
├── logs/
│   ├── conductor.log          # JSONL event log (auto-rotated at 1MB)
│   ├── agents/<task-id>/      # Per-task JSONL agent logs (--output-format stream-json, gzipped after 7d)
│   ├── judgments/<task-id>/    # Judgment I/O for audit
│   └── summaries/             # Daily summaries
├── PAUSE_ENTRIES              # Touch file — suppresses entries
├── FORCE_REASONING            # Touch file — one-shot, consumed on read
├── FORCE_BUILD                # Touch file — one-shot, consumed on read
└── EMERGENCY_EXIT             # Touch file — liquidate all positions
```

### Signal utilities (`core/signals.py`)

- `_atomic_write_json(path, data)` — temp file + `os.replace()`
- `write_status()`, `write_last_trade()`, `write_last_review()` — overwrite signals
- `write_alert(alert_type, data)` — append-only, microsecond filenames
- `read_signal(path)` — returns dict or None (malformed = None, not crash)
- `check_inbound_signal(name)` / `consume_inbound_signal(name)` — touch-file pattern

## Discord Notifications

clawhip routes outbound events to Discord channels:

| Event | Channel | Trigger |
|-------|---------|---------|
| Trade fills | `TRADES_CHANNEL` | `last_trade.json` changed |
| Alerts | `ALERTS_CHANNEL` (with @mention) | `alerts/*` changed |
| Claude reviews | `REVIEWS_CHANNEL` | `last_review.json` changed |
| Git commits | `DEV_CHANNEL` | Any commit |
| Task queue changes | `AGENT_CHANNEL` | `agent_tasks/*` changed |
| Executor checkpoints | `AGENT_CHANNEL` | `executor/checkpoint.json` changed |
| Permission requests | `ALERTS_CHANNEL` (with @mention) | `conductor/permission_request.json` changed |

Conductor sends stage transition notifications directly via `clawhip send --channel`:

| Event | Emoji | Message |
|-------|-------|---------|
| Task accepted | `📋` | Task accepted: `<id>` — starting Architect |
| Task rejected | `🚫` | Task rejected: `<id>` — `<reason>` |
| Architect done | `📐` | Architect done — plan delivered, launching Executor |
| Architect died | `💀` | Architect died — no plan delivered |
| Executor done | `🔨` | Executor done — tests passing, launching Reviewer |
| Executor failed | `❌` | Executor failed — status: `<status>` |
| Executor died | `💀` | Executor died — no completion signal |
| Reviewer approved | `✅` | Reviewer approved — merging |
| Reviewer rejected | `🔄` | Reviewer: `<verdict>` |
| Merge failed | `💥` | Merge failed |
| Task complete | `🎉` | Task complete — merged and tests passing |
| Agent timeout | `⏰` | Agent timeout — `<stage>` exceeded 600s |

## Shutdown & Recovery

### Graceful shutdown
`ozy down` or `!shutdown-conductor`: writes `shutdown.json` → conductor exits → outer loop reads intent, breaks → companion/clawhip get Ctrl-C.

### Conductor restart
`ozy restart` or `!restart-conductor`: writes `restart.json` → conductor exits → outer loop reads intent, waits 5s, relaunches.

### Crash recovery

| Component | Detection | Recovery |
|-----------|-----------|----------|
| Trading bot | Ops Monitor: stale `status.json` >30s | Auto-restart (max 3/hr), Discord alert |
| Conductor | Outer loop: no exit intent file | Alert file written, loop stops. Manual intervention. |
| Executor pane | Conductor poll: pane not alive, no completion signal | Task failed, cleared from state |
| Architect/Reviewer | Conductor poll: pane not alive, no output signal | Task failed, cleared from state |
| Agent timeout | Conductor poll: stage_started + 600s elapsed | Kill pane, fail task, Discord alert |

### Merge safety
Post-merge: conductor runs `pytest -q --tb=line` and checks exit code. If tests fail → auto-revert merge commit via `git revert -m 1` (safe for `--no-ff` merges). Worktree cleanup only after successful merge.

## Environment Variables (`.env`)

| Variable | Purpose |
|----------|---------|
| `DISCORD_BOT_TOKEN` | Bot token for companion + clawhip |
| `ALERTS_CHANNEL` | Channel ID for alerts + permission requests |
| `TRADES_CHANNEL` | Channel ID for trade fill notifications |
| `REVIEWS_CHANNEL` | Channel ID for Claude review summaries |
| `DEV_CHANNEL` | Channel ID for git commit notifications |
| `AGENT_CHANNEL` | Channel ID for task + pipeline stage notifications |
| `OPERATOR_MENTION` | Discord mention string for alert pings |

## Key Files

| File | Purpose |
|------|---------|
| `tools/ozy` | Unified launcher — start/stop/status/logs/task |
| `tools/conductor.sh` | Pipeline coordinator (~650 lines) |
| `tools/start_conductor.sh` | Outer restart loop (~50 lines) |
| `tools/discord_companion.py` | Inbound Discord commands (~250 lines) |
| `clawhip.toml` | Event routing configuration |
| `config/agent_roles/*.md` | 7 agent role definitions (frontmatter + prompt) |
| `ozymandias/core/signals.py` | Signal file utilities (atomic writes, read/write helpers) |

## Implementation History

### Phase 22 — Signal File API (Plan Phase A)
`core/signals.py`: atomic writes, 6 signal writers. Wired into orchestrator fast/slow loops. Alert emitters: equity drawdown, broker error, loop stall. Microsecond alert filenames.

### Phase 23 — clawhip + Discord Companion (Plan Phase B)
`clawhip.toml`: workspace + git monitors, 6 routes. `discord_companion.py`: 8 commands, intent filter. Standalone — no ozymandias imports.

### Phase 24 — Conductor + Task Format (Plan Phase E)
`conductor.sh`: task polling, `claude -p` judgments, heartbeat, log rotation, startup reconciliation. `start_conductor.sh`: exit intent dispatch. `conductor.md`: 3 judgment schemas. ~140 lines (planned 50-80; extra = operational robustness).

### Phase 25 — Strategy Dialogue (Plan Phase B.5)
`dialogue.md`: 3 adversarial personas, 6-dimension ambiguity scoring (0.20 threshold), readiness gates, signal file output.

### Phase 26 — Ops Monitor (Plan Phase C)
`ops_monitor.md`: anomaly detection, 3 escalation tiers, 3 permission tiers, bug report rate limiting (3/hr), daily summary schema.

### Phase 27 — Strategy Analyst (Plan Phase D)
`strategy_analyst.md`: 4-category outcome classification, hindsight bias gate, Ontologist dedup, structured findings.

### Phase 28 — Agent Roles + Pipeline Stages (Plan Phase F)
`executor.md` (Simplifier 0.15, zone files, checkpoints), `architect.md` (intent classification, readiness gates), `reviewer.md` (Contrarian 0.25, 3 verification tiers, 8 convention checks). Pipeline stage launchers + permission proxy in conductor.sh. `notify_discord` for stage transitions.

### Phase 29 — OMC Tool Unlock + Model Routing + Persistent Sessions
Migrated agent permission model from whitelist (`--allowedTools` with 4-6 basic tools) to blacklist (`--permission-mode dontAsk` + `--disallowedTools`). Agents now have access to 78 tools including 33 OMC MCP tools (LSP, AST grep, python REPL, notepad, project memory). Added `--model` per role (architect=opus, executor/reviewer=sonnet). Fixed broken audit logging (`--verbose` required for `--output-format stream-json`). Added `--include-hook-events` for full OMC hook visibility in agent logs. Changed `[AGENT_DONE]` sentinel to valid JSONL. MCP filesystem write tools denied for architect/reviewer alongside built-in Write/Edit. Key findings: OMC hooks fire in `-p` mode (verified empirically); `-p --resume` does NOT carry conversation context; `--input-format stream-json` enables persistent multi-turn sessions via FIFO (message format: `{"type":"user","message":{"role":"user","content":"..."}}` — type is `"user"`, not `"user_message"`). Multi-turn context retention, tool usage across turns, and session lifecycle via EOF all verified.

### Test Coverage: 91 tests across 7 test files + 159 orchestrator regression tests.
