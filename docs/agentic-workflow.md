# Agentic Development Workflow

The v4 agentic workflow automates the Ozymandias development pipeline using Claude Code
instances coordinated by a deterministic bash wrapper. It is development infrastructure —
it builds and maintains the trading bot, but is not part of the trading bot itself.

**Design document:** `plans/2026-04-07-agentic-workflow-v4-omc-only.md`
**Phase files:** `phases/22_signal_file_api.md` through `phases/28_omc_hooks_roles.md`

---

## Architecture

Three complementary layers, each doing what it does best:

```
Operator (Discord)
    ↕
clawhip (event routing) + Discord companion (inbound commands)
    ↕
Conductor wrapper (tools/conductor.sh — deterministic bash)
    ↕ spawns via tmux, judges via claude -p
Agent sessions (Architect → Executor → Reviewer)
    ↕
Signal file bus (state/signals/ — JSON files, atomic writes)
    ↕
Trading bot (ozymandias/ — reads inbound signals, writes status/alerts)
```

- **Conductor wrapper** (`tools/conductor.sh`, ~140 lines bash): Deterministic coordination.
  Owns polling, signal scanning, state I/O, git operations, tmux lifecycle. Invokes `claude -p`
  on-demand for judgment calls (task classification, context assembly, failure diagnosis).
  No persistent LLM session.
- **Outer restart loop** (`tools/start_conductor.sh`, ~45 lines bash): Reads the conductor's
  exit intent file to decide restart vs. shutdown vs. crash alert.
- **OMC agent sessions:** Every agent (Architect, Executor, Reviewer, etc.) is a Claude Code
  instance with OMC hooks, running in its own tmux pane.
- **clawhip** (`clawhip.toml`): Event routing daemon. Monitors `state/signals/` and git,
  routes notifications to Discord channels.
- **Discord companion** (`tools/discord_companion.py`): Standalone Python script handling
  inbound Discord commands. Does NOT import from `ozymandias/` — pure signal file I/O.

## Roles

| Role | File | Model | Tier | Mode | Permissions |
|------|------|-------|------|------|-------------|
| Conductor | `config/agent_roles/conductor.md` | Sonnet | MEDIUM | claude-p (on-demand) | Read, Write, Bash, Glob, Grep |
| Executor | `config/agent_roles/executor.md` | Sonnet | HIGH | ephemeral (worktree) | Read, Write, Edit, Bash, Glob, Grep |
| Architect | `config/agent_roles/architect.md` | Opus | HIGH | ephemeral | Read, Bash (read-only), Glob, Grep. **No Write/Edit** |
| Reviewer | `config/agent_roles/reviewer.md` | Sonnet | MEDIUM | ephemeral | Read, Bash (read-only), Glob, Grep. **No Write/Edit** |
| Ops Monitor | `config/agent_roles/ops_monitor.md` | Haiku | LOW | persistent | Read, Bash (limited), Glob, Grep. **No Write/Edit (source)** |
| Dialogue | `config/agent_roles/dialogue.md` | Sonnet | HIGH | persistent | Read, Write (plans/state), Bash, Glob, Grep |
| Strategy Analyst | `config/agent_roles/strategy_analyst.md` | Sonnet | MEDIUM | ephemeral | Read, Bash (read-only), Glob, Grep. **No Write/Edit** |

## Permission Model

Permission prompts block agents in their tmux pane until resolved. Three layers handle this
without requiring manual intervention for known-safe tools.

| Layer | Mechanism | Behavior |
|-------|-----------|----------|
| Pre-approved | `--allowedTools` per role in conductor.sh | Auto-approved, no prompt |
| Hard deny | `.claude/settings.json` deny rules | Blocked silently |
| Discord proxy | Conductor detects prompt → routes to Discord | Operator approves/denies via `!approve`/`!deny` |

### Per-role `--allowedTools`

| Role | `--allowedTools` | Notes |
|------|-----------------|-------|
| Judgment | `Read,Glob,Grep` | Piped call, uses `--permission-mode dontAsk` |
| Architect | `Read,Bash,Glob,Grep` | No Write/Edit — writes signal files via Bash |
| Executor | `Read,Write,Edit,Bash,Glob,Grep` | Full access in worktree |
| Reviewer | `Read,Bash,Glob,Grep` | No Write/Edit — writes verdict via Bash |

### Proxy flow

1. Agent hits unapproved tool → Claude Code shows permission prompt in tmux pane
2. Conductor's poll loop runs `tmux capture-pane`, detects prompt pattern
3. Conductor writes `state/signals/conductor/permission_request.json`
4. clawhip routes to Discord with @mention
5. Operator sends `!approve <task-id>` or `!deny <task-id>`
6. Companion writes `state/signals/conductor/permission_response.json`
7. Conductor reads response, sends `y` or `n` via `tmux send-keys`
8. Agent resumes (or tool is denied and agent adapts)

If no response arrives within 120 seconds, the conductor auto-denies and logs
`permission_timeout`.

To pre-approve a new tool for a role, add it to the `--allowedTools` string in the role's
launch function in `tools/conductor.sh`.

## Signal File Conventions

All signals use atomic JSON writes (temp file + `os.replace()`). The signal bus is the
universal communication layer — no module imports another module's code.

### Directory structure (`state/signals/`)

```
state/signals/
├── status.json              # Bot status (overwrite each fast tick)
├── last_trade.json          # Most recent entry/exit (overwrite)
├── last_review.json         # Most recent Claude position review (overwrite)
├── alerts/                  # Append-only alert files (one per event)
│   └── 20260408T143022123456_equity_drawdown.json
├── orchestrator/            # Reserved for bot-level signals
├── conductor/               # Conductor control signals
│   ├── restart.json         # Written by companion (!restart-conductor)
│   └── shutdown.json        # Written by companion (!shutdown-conductor)
├── architect/<task-id>/     # Architect output (plan.json)
├── reviewer/<task-id>/      # Reviewer output (verdict.json)
├── analyst/<date>/          # Strategy Analyst output (findings.json)
├── dialogue/                # Dialogue agent output (response.json)
└── executor/                # Executor signals (checkpoint.json)
```

### Inbound signals (bot reads)

These use the pre-existing touch-file pattern in `ozymandias/state/` (not under `signals/`):
- `state/PAUSE_ENTRIES` — Persistent. Suppresses new entries while file exists.
- `state/FORCE_REASONING` — One-shot. Consumed on read. Triggers immediate slow loop.
- `state/FORCE_BUILD` — One-shot. Consumed on read. Triggers immediate watchlist build.

### Alert types

Bot emits three alert types via `write_alert()`:
- `equity_drawdown` — Session equity drops >2%
- `broker_error` — First broker API failure in a failure sequence
- `loop_stall` — Fast loop tick exceeds 60 seconds

### Signal utilities (`core/signals.py`)

Key functions:
- `_atomic_write_json(path, data)` — Temp file + `os.replace()`, never partial writes
- `write_status(data)`, `write_last_trade(data)`, `write_last_review(data)` — Overwrite signals
- `write_alert(alert_type, data)` — Append-only with microsecond timestamps
- `read_signal(path)` — Returns dict or None (malformed = None, not crash)
- `check_inbound_signal(name)` / `consume_inbound_signal(name)` — Touch-file pattern
- `ensure_signal_dirs()` — Creates all directories on startup. Idempotent.

## Discord Commands

Handled by `tools/discord_companion.py`:

| Command | Action | Signal |
|---------|--------|--------|
| `!pause` | Suppress new entries | Touch `state/PAUSE_ENTRIES` |
| `!resume` | Resume entries | Remove `state/PAUSE_ENTRIES` |
| `!status` | Show bot status | Read `state/signals/status.json` |
| `!exit` | Emergency exit all positions | Touch `state/EMERGENCY_EXIT` |
| `!force-reasoning` | Trigger Claude reasoning cycle | Touch `state/FORCE_REASONING` |
| `!fix <desc>` | Submit bug fix task | Write to `state/agent_tasks/` |
| `!restart-conductor` | Restart conductor wrapper | Write `conductor/restart.json` |
| `!shutdown-conductor` | Shut down conductor | Write `conductor/shutdown.json` |

An intent filter blocks informational questions (messages containing "?", "what", "how",
"why", "when", "does", "is it", "can you", "should") from being dispatched as commands.

## Conductor Judgment Calls

The wrapper invokes `claude -p` for three judgment types:

1. **classify_task** — Accept/defer/reject incoming tasks. Priority: human > bug > strategy_analysis > backlog.
2. **assemble_context** — Select relevant files and domain context for Architect sessions.
3. **diagnose_failure** — After 3+ consecutive failures: replan, escalate, or retry simpler.

Each is a fresh session with focused JSON I/O — no context accumulation.

## Task Flow

```
Operator posts !fix or task file appears in state/agent_tasks/
  → Conductor detects → classify_task judgment → accept/defer/reject
  → assemble_context judgment → relevant files + domain context
  → Spawn Architect (Opus, tmux pane) → reads codebase, writes plan with checkpoints
  → Conductor detects plan signal → creates worktree → spawns Executor (Sonnet, tmux pane)
  → Executor implements units, pauses at checkpoints
  → Conductor detects checkpoint → spawns Architect review
  → Executor completes → Conductor spawns Reviewer (Sonnet, tmux pane)
  → Reviewer reads diff, runs tests, writes verdict
  → Conductor detects approval → merges branch
  → clawhip routes lifecycle events to Discord
```

## Shutdown Protocol

### Market close (automatic)
Bot writes `session_close` to `status.json`. Conductor stops accepting new tasks. In-progress
Executors continue. Ops Monitor writes daily summary. Strategy Analyst spawns for post-market
analysis. clawhip, Dialogue, and companion continue running.

### Full shutdown (operator-initiated)
Triggered by `state/SHUTDOWN` file or `!shutdown` Discord command.
1. Conductor stops accepting tasks, signals Executors to commit and exit (5min timeout)
2. Kills remaining agent panes, writes final state
3. Outer loop reads shutdown intent, stops cleanly
4. Ops Monitor, Dialogue, companion, clawhip wind down in order

### Crash recovery
| Component | Recovery |
|-----------|----------|
| Trading bot | Ops Monitor detects stale status.json, restarts (max 3/hour) |
| Conductor | Outer loop reads exit intent: restart → 5s delay + reloop; crash → alert + stop |
| Executor | Wrapper detects missing tmux pane, respawns in same worktree from zone file |
| Architect/Reviewer | Wrapper detects timeout, respawns |

## Pressure-Testing Protocols

Three adversarial personas used across roles:

- **Contrarian** — "What breaks if this interacts with X?" Used by Reviewer (threshold 0.25),
  Dialogue.
- **Simplifier** — "Can we get 80% of this with less code?" Used by Executor (threshold 0.15),
  Dialogue.
- **Ontologist** — "Is this actually new, or an instance of something we already have?" Used by
  Strategy Analyst (dedup gate), Dialogue.

## Zone File Protocol

Executors track progress via zone files (JSON) with append-only history:

```json
{
  "task_id": "<from plan>",
  "units_completed": [1, 2],
  "unit_in_progress": 3,
  "units_remaining": [4, 5],
  "test_status": "passing",
  "branch": "feature/<task-id>",
  "worktree_path": ".worktrees/<task-id>",
  "wall_clock_seconds": 0,
  "last_updated": "<ISO timestamp>",
  "history": [
    {"ts": "<ISO>", "transition": "started", "unit": 1},
    {"ts": "<ISO>", "transition": "completed", "unit": 1}
  ]
}
```

---

## Implementation History

### Phase 22 — Signal File API + Bot Event Emitter (Plan Phase A)

Created `core/signals.py` with atomic JSON write utility and 6 signal writer functions.
Wired into orchestrator: `write_status()` in fast loop, `write_last_review()` after position
reviews, inbound signal checks (`PAUSE_ENTRIES`, `FORCE_REASONING`, `FORCE_BUILD`). Wired
`write_last_trade()` into `fill_handler.py`. Added three alert emitters to orchestrator:
equity drawdown (>2%), broker error, loop stall (>60s).

**Deviation from plan:** Alert filenames use microsecond resolution (`%Y%m%dT%H%M%S%f`)
instead of second resolution to prevent collisions during rapid-fire alerts.

### Phase 23 — clawhip + Discord Companion (Plan Phase B)

Created `clawhip.toml` with workspace and git monitors, 6 routing rules (trades, alerts,
reviews, dev commits, agent tasks, executor checkpoints). Created
`tools/discord_companion.py` (~180 lines) with 8 commands and an intent filter. The
companion is completely standalone — no imports from `ozymandias/`.

### Phase 24 — Conductor Wrapper + Task Format (Plan Phase E)

Created `tools/conductor.sh` (~140 lines) with task polling, `claude -p` judgment calls,
heartbeat, log rotation, and stale task reconciliation. Created `tools/start_conductor.sh`
(~45 lines) outer restart loop with exit intent dispatch. Created
`config/agent_roles/conductor.md` with three judgment type schemas.

**Deviation from plan:** Conductor is ~140 lines vs. planned ~50-80. Extra lines are
operational robustness: log rotation, old log compression, stale task reconciliation on
startup, heartbeat writes.

### Phase 25 — Strategy Dialogue Agent (Plan Phase B.5)

Created `config/agent_roles/dialogue.md` with all three adversarial personas (Contrarian,
Simplifier, Ontologist), 6-dimension ambiguity scoring (0.20 threshold), readiness gates
(non-goals, decision boundaries), and signal file output convention.

### Phase 26 — Ops Monitor Agent (Plan Phase C)

Created `config/agent_roles/ops_monitor.md` with anomaly detection rules, 3 escalation
tiers (auto-handle, notify+act, escalate+wait), 3 permission tiers (ReadOnly,
ProcessControl, DangerFullAccess), bug report rate limiting (3/hour), and daily summary
schema.

### Phase 27 — Strategy Analyst Agent (Plan Phase D)

Created `config/agent_roles/strategy_analyst.md` with 4-category outcome classification,
hindsight bias prevention gate (must cite signal values at decision time), Ontologist
pressure-test (cross-reference NOTES.md + findings log for dedup), and structured findings
output schema.

### Phase 28 — OMC Hooks + Custom Agent Roles (Plan Phase F)

Created `config/agent_roles/executor.md` (trading domain rules, Simplifier gate at 0.15,
zone file update protocol, checkpoint protocol, worktree scope guidance),
`config/agent_roles/architect.md` (intent classification gate, checkpoint placement
strategy, readiness gates, `disallowedTools: Write, Edit`), and
`config/agent_roles/reviewer.md` (Contrarian pressure-test at 0.25, three verification
tiers, trading convention checks, structured verdict format, `disallowedTools: Write, Edit`).

**Integration fix during verification:** Executor checkpoint path changed from
`.executor/checkpoint.json` to `state/signals/executor/checkpoint.json` to match clawhip's
monitor path. Added `executor` subdirectory to `ensure_signal_dirs()`.

### Test Coverage

| Phase | Test file | Tests |
|-------|-----------|-------|
| 22 | `test_signals.py` | 16 |
| 23 | `test_discord_companion.py` | 17 |
| 24 | `test_conductor_schemas.py` | 13 |
| 25 | `test_dialogue_agent.py` | 9 |
| 26 | `test_ops_monitor.py` | 8 |
| 27 | `test_strategy_analyst.py` | 7 |
| 28 | `test_agent_roles.py` | 21 |
| **Total** | | **91** |

All 91 tests pass. 159 orchestrator regression tests also pass with signal wiring changes.
