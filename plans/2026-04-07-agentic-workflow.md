# Agentic Workflow — Phase Decomposition Plan

**Date:** 2026-04-07  
**Motivated by:** Operator spends entire trading days babysitting the bot, manually approving minor
decisions during phase work, and performing post-session analyses that could be automated. The
orchestrator extraction (Phase 1+2) created 11 parallel work zones — the infrastructure for
multi-agent development exists but has no coordination layer.

**Reference material:**
- `claw-code-analysis.md` — engineering lessons from the claw-code multi-agent stack
- `NOTES.md` § "Agentic Development Workflow Design" — signal file architecture, agent roles, bus design
- `Multilayer agentic workflow spec.pdf` — rough feature spec (problem statement + channel layout)
- claw-code orchestration concept (OmX / clawhip / oh-my-openagent separation)

---

## Problem Statement

Three pain points, in order of severity:

1. **All-day babysitting.** The bot runs autonomously but the operator watches logs all day to catch
   misbehavior, manually restarts on crashes, and reads post-session logs to identify strategy issues.
   This should be automated.

2. **Approval bottleneck.** During long phase work, the operator must be at the keyboard to approve
   minor decisions. Development agents should be able to operate with structured autonomy — working
   bug backlogs, running verification loops — without blocking on human input for routine decisions.

3. **No closed-loop learning.** Post-session analysis (trade journal audits, strategy hindsight,
   calibration error detection) is done manually in ad-hoc conversations. An automated analysis
   pipeline would feed findings directly into the development backlog.

---

## Design Principles

Taken from claw-code analysis + our own architecture:

1. **Signal files are the universal bus.** The trading bot, ops agents, dev agents, and the signal
   daemon all communicate through structured JSON files. No direct coupling between systems.
   Discord is one client of this bus, not the bus itself.

2. **Monitoring stays outside agent context windows.** An agent implementing a feature should never
   have notification logic or Discord formatting in its working memory. A separate daemon (clawhip
   equivalent) owns all monitoring and delivery.

3. **Three separated concerns.** Workflow decomposition (turning directives into tasks), event
   routing (signal daemon), and agent coordination (role cycle + conflict resolution) are distinct
   layers, not one monolith.

4. **Recovery before escalation.** Known failure modes auto-recover once before asking for help.
   Ops agents classify failures by typed `FailureKind`, apply per-class recovery recipes, and
   escalate only when recovery fails.

5. **State machine first, not log first.** Agent lifecycle, bot process state, and task status are
   typed state machines with explicit transitions — not inferred from log parsing.

6. **Token cost discipline.** Executor and reviewer agents use terse output mode (caveman skill).
   Ops monitor uses Haiku. Strategy analyst and architect use Sonnet/Opus. Per-agent token budgets
   enforced.

---

## Three Systems, Shared Bus

```
System 1: Bot Operations (always running during market hours)
├── Trading bot process (existing)
├── Signal daemon (watches signals + git, routes to/from Discord)
├── Ops monitor agent (watches logs/signals, manages process, documents bugs)
└── Strategy analyst agent (post-market, reads journal, writes hindsight analysis)

System 2: Strategy Dialogue (on-demand, operator-initiated)
└── Dialogue agent (Claude Code on Max plan, bridged to Discord #strategy channel)
    - Thinking partner for the operator, not a gatekeeper
    - Reads everything: analyses, docs, journal, code, signals
    - Writes plan files + task directives when operator says "go"
    - Zero API cost (Max plan)

System 3: Development Pipeline (on-demand or bug-triggered)
├── Architect agent (expands plan into zone-scoped task decomposition)
├── Executor agent(s) (parallel, zone-scoped, writes code + tests)
└── Reviewer agent (runs tests, checks conventions, sends feedback)

Shared: state/signals/ (signal file bus), Discord (human interface)
```

**Operator retains direct access to all layers.** The Dialogue agent does not gate or mediate
access to the development pipeline. The operator can talk directly to dev agents in their channels,
give commands, interrupt work, and course-correct mid-implementation — exactly like the claw-code
workflow. Dialogue is for collaborative strategic reasoning; direct commands are for everything else.

**Flow for larger-than-bug work:**
```
Strategy analyst writes findings → Operator reads → Operator opens #strategy
  → Operator ↔ Dialogue: discuss findings, explore ideas, debate approaches
  → Operator says "write the plan" → Dialogue saves plan file + task directive
  → Architect picks up task, decomposes into zone-scoped work
  → Executors implement (Operator can still talk to them directly)
  → Reviewer checks (Operator can still intervene)
```

**Flow for bugs and quick fixes (unchanged — no Dialogue involved):**
```
Ops monitor writes bug report → Architect decomposes → Executor fixes
  OR: Operator posts direct command in #dev → Executor picks up immediately
```

---

## Phase Breakdown

Each phase is a self-contained deliverable. Phases are ordered by dependency — each one builds on
the previous. No phase should require more than one session to implement.

---

### Phase A — Signal File API + Bot Event Emitter

**Goal:** Extend the existing `EMERGENCY_*` signal file pattern into a structured event bus. The
trading bot writes machine-readable events; external systems consume them.

**What gets built:**
- `state/signals/` directory convention
- Bot writes structured JSON signal files at key events:
  - `status.json` — equity, positions, open orders, loop health (written every fast tick, overwrite)
  - `last_trade.json` — most recent entry/exit with full context (overwrite on each trade)
  - `last_review.json` — most recent Claude position review (overwrite)
  - `alerts/` — append-only alert files for emergency/degradation events (one file per event)
- Inbound signal files the bot reads on fast-loop tick:
  - `state/PAUSE_ENTRIES` — suppress new entries without exiting positions
  - `state/FORCE_REASONING` — trigger immediate slow loop cycle
  - `state/FORCE_BUILD` — trigger immediate watchlist build
- Signal file writer utility in `core/signals.py` — atomic write (temp + rename), schema validation
- All signal writes are fire-and-forget from the bot's perspective — no new dependencies, no imports

**What does NOT get built:** Discord integration, daemon process, agent coordination.

**Constraints:**
- Zero new dependencies. Signal files are JSON written with the existing atomic write pattern.
- The bot's fast loop already checks `EMERGENCY_EXIT` and `EMERGENCY_SHUTDOWN` — extend the same
  polling mechanism for new inbound signals.
- Signal file schema must be machine-readable by ops agents that will consume them in later phases.

**Verification:** Bot runs normally with signal files being written. Manual inspection confirms
files are well-formed. Existing tests unaffected. `touch state/PAUSE_ENTRIES` pauses entries;
removing it resumes.

---

### Phase B — Signal Daemon (clawhip equivalent)

**Goal:** Separate Python process that watches signal files and git activity, routes events to
Discord webhooks (outbound), and writes signal files from Discord commands (inbound).

**What gets built:**
- `tools/signal_daemon.py` — standalone process, no imports from `ozymandias/`
- Outbound: watches `state/signals/` for changes, formats and POSTs to Discord webhooks
- Outbound: watches git for new commits, branch creation, formats and POSTs to Discord
- Inbound: listens on Discord for operator commands, writes corresponding signal files
- Discord channel routing:
  - `#trades` — entry/exit fills from `last_trade.json`
  - `#reviews` — position reviews from `last_review.json`
  - `#alerts` — emergency/degradation from `alerts/`
  - `#status` — periodic equity/position summary from `status.json`
- Inbound command mapping:
  - `!pause` → writes `state/PAUSE_ENTRIES`
  - `!resume` → removes `state/PAUSE_ENTRIES`
  - `!status` → reads `status.json`, posts formatted summary to channel
  - `!exit [symbol]` → writes `state/EMERGENCY_EXIT`
  - `!force-reasoning` → writes `state/FORCE_REASONING`

**What does NOT get built:** Ops monitor agent, dev agent coordination, strategy analyst.

**Constraints:**
- The daemon is stateless. It can crash and restart without losing anything — signal files are the
  source of truth, not daemon memory.
- The daemon never imports from `ozymandias/`. It reads JSON files and calls Discord webhooks.
  This keeps monitoring completely outside the bot's dependency tree.
- Webhook-only for outbound (no discord.py gateway needed initially). discord.py for inbound
  listener (lightweight, no intents beyond message content).
- The daemon process is managed independently of the bot (separate systemd unit or tmux pane).

**Verification:** Bot writes signal files (Phase A). Daemon picks them up and posts to Discord.
Operator types `!pause` in Discord, bot pauses entries. Round-trip confirmed.

---

### Phase B.5 — Strategy Dialogue Agent

**Goal:** A Claude Code instance bridged to Discord's `#strategy` channel, giving the operator a
collaborative thinking partner with full codebase and analysis access — at zero API cost via the
Max plan.

**What gets built:**
- Discord ↔ Claude Code bridge in the signal daemon:
  - Messages in `#strategy` channel are piped to a Claude Code process's stdin
  - Claude Code responses are captured and posted back to `#strategy`
  - Implementation: tmux `send-keys` + `capture-pane`, or pty wrapper around the Claude Code
    process. The daemon manages the bridge, not the agent itself.
- Claude Code instance configuration:
  - Runs in a dedicated tmux session on the Max plan (no API cost)
  - Working directory is the project root — full filesystem access to docs, signal files,
    trade journal, code, analyses, plans
  - CLAUDE.md provides the role context (strategy dialogue partner, not autonomous executor)
- Output actions the Dialogue agent can take when directed by the operator:
  - Write plan files to `plans/`
  - Write task directives to `state/agent_tasks/` (triggers Architect pickup)
  - Update NOTES.md with new analyses or concerns
  - Post summaries to other Discord channels via signal files

**What this is NOT:**
- Not a gatekeeper. The operator's direct access to dev agents, ops agents, and the bot itself
  is completely unchanged. Dialogue is an additional channel, not a replacement for any existing one.
- Not autonomous. Dialogue never initiates action on its own — it only acts when the operator
  directs it. It reasons, discusses, and writes when asked.
- Not the Architect. Dialogue produces high-level plans and directives. The Architect takes those
  and decomposes them into zone-scoped, acceptance-tested task specifications. The division is:
  Dialogue decides *what* and *why*. Architect decides *how* and *where*.

**Constraints:**
- The Claude Code process must be on the Max plan, not API. This is the core cost constraint.
  The daemon bridges Discord to the CLI process; it does not make API calls on behalf of Dialogue.
- Session management: the daemon should handle Claude Code session restarts gracefully. If the
  session compacts or the process crashes, the daemon restarts it with the same working directory.
  Conversation continuity is best-effort, not guaranteed — the filesystem is the persistent memory.
- The bridge adds latency (tmux capture + Discord round-trip). This is acceptable for a
  conversational interface — seconds, not milliseconds.

**Verification:** Operator types a message in `#strategy` Discord channel. Daemon relays to Claude
Code. Claude Code responds (reads a file, discusses a finding). Response appears in `#strategy`.
Operator asks Dialogue to write a plan file — file appears in `plans/`. End-to-end round-trip
confirmed.

---

### Phase C — Ops Monitor Agent

**Goal:** A Claude Code instance (in tmux) that watches the trading bot's signal output, detects
anomalies, manages the process, and documents bugs for the development pipeline.

**What gets built:**
- Ops monitor agent definition: role prompt, context scope, permitted actions
- Process health monitoring via `status.json` (detects stale timestamps, missing heartbeats)
- Log anomaly detection (structured patterns: ERROR/CRITICAL grep, repeated WARNING clusters)
- Bug documentation: writes structured bug reports to `state/agent_tasks/` when issues detected
- Process management: restart bot via `systemctl` or process signal when crash detected

**Context scope:** Logs, signal files, trade journal, config.json. Never sees source code,
CLAUDE.md, or plans.

**Permission tiers for ops agent actions:**
- **ReadOnly** (always allowed): read logs, read signals, read journal, post to Discord
- **ProcessControl** (allowed with notification): restart bot, pause entries, force reasoning
- **DangerFullAccess** (requires human approval): exit positions, modify config

**Escalation protocol:**
- Auto-handle: restart after crash (with cooldown — max 3 restarts per hour), notify Discord
- Notify + act: pause entries during anomalous behavior, notify human, resume after 10 min if
  no human response
- Escalate and wait: never autonomously exit positions, never modify config, never touch code

**What does NOT get built:** Strategy analyst, dev agents, agent coordination.

**Verification:** Ops monitor detects a simulated crash (kill bot process), restarts it, posts
notification to Discord. Ops monitor detects a repeated WARNING pattern, writes a bug report
to `state/agent_tasks/`.

---

### Phase D — Strategy Analyst Agent

**Goal:** Post-market agent that reads the trade journal, compares bot decisions to what happened,
and writes structured analysis that feeds into the development backlog.

**What gets built:**
- Strategy analyst agent definition: role prompt, context scope, output format
- Reads trade journal entries for the completed session
- For each completed trade, categorizes the outcome:
  - **Signal present, bot ignored** — the TA signals or news at decision time indicated the
    correct action, but the bot's gates filtered it out. Actionable: fix the gate or prompt.
  - **Signal present, bot saw but filtered** — the bot detected the signal but a filter
    (RVOL floor, RSI ceiling, dead zone, etc.) blocked it. Check if the filter is too aggressive.
  - **Signal ambiguous, reasonable to miss** — no clear signal at decision time. No action,
    document for pattern recognition across sessions.
  - **Truly unforeseeable** — external event with no precursor signals. No system change warranted.
- For missed opportunities (symbols on watchlist that moved significantly but weren't entered):
  same four-category analysis, with explicit requirement to show what signals existed at decision
  time and why the bot should or shouldn't have caught them.
- Output: structured analysis file in `state/agent_tasks/` tagged as `type: strategy_analysis`.
  Architect picks these up in Phase F.

**Context scope:** Trade journal, session logs, NOTES.md (for known concerns), watchlist state.
Never sees source code or orchestrator internals.

**Constraint — hindsight bias prevention:** The analyst must always show its work. Each finding
must cite the specific signal or indicator value that existed at decision time. "NKE rallied 3%"
is not a finding. "NKE rallied 3% — BB squeeze was firing at entry time with RSI 22, oversold
bounce was predictable from existing signals" is a finding. The distinction between "signal was
there" and "outcome was there" must be explicit in every entry.

**What does NOT get built:** Dev agents, architect, agent coordination.

**Verification:** Run against the existing trade journal (68 trades). Produces structured analysis
with categorized findings. Manual review confirms hindsight bias discipline is maintained.

---

### Phase E — Dev Agent Task Format + Zone Claiming

**Goal:** Define the structured task format that dev agents consume, and the zone-locking protocol
that prevents parallel agents from colliding.

**What gets built:**
- Task schema (adapted from claw-code's `TaskPacket`):
  ```
  task_id, objective, scope (zone files), source (human | ops_monitor | strategy_analyst),
  priority (human > bug > backlog), acceptance_tests, escalation_policy,
  branch_policy, zone_lock_required
  ```
- Zone claim protocol:
  - Agent writes `state/agent_claims/<zone>.lock` with agent ID and timestamp before working
  - Agent deletes lock on completion or timeout (configurable, default 30 min)
  - Before claiming, agent checks for existing lock — if held by another agent, skip this task
  - Idle agents working bug backlog only claim zones that don't overlap with active human-directed tasks
- Task lifecycle states: `pending → claimed → in_progress → review → completed | failed`
- Task result schema: `task_id, status, branch_name, test_results, files_changed`
- Human-directed tasks always take priority: if a human task arrives for a zone held by a
  backlog agent, the backlog agent yields (completes current atomic step, releases lock)

**What does NOT get built:** The agents themselves — just the protocol they follow.

**Verification:** Manual walkthrough: create a task file, simulate claim/release cycle, verify
lock contention is handled correctly.

---

### Phase F — Architect / Executor / Reviewer Agent Cycle

**Goal:** Define and implement the three dev agent roles with their context boundaries and the
verification loop that connects them.

**What gets built:**
- **Architect agent** definition:
  - Context: CLAUDE.md, COMPLETED_PHASES.md, DRIFT_LOG.md, NOTES.md, plans/, codebase structure
  - Input: task from `state/agent_tasks/` (human directive, bug report, or strategy analysis finding)
  - Output: plan file in `plans/` with zone boundaries, file constraints, acceptance criteria
  - Does not write code. Does not see full source implementation.

- **Executor agent** definition:
  - Context: CLAUDE.md conventions + plan + assigned zone files only
  - Input: plan file from architect
  - Output: code changes + tests on a feature branch, completion signal to `state/agent_results/`
  - Uses caveman skill for terse output (token cost control)
  - Multiple executors can run in parallel on different zones
  - Does not see other zones, full doc set, or trade journal

- **Reviewer agent** definition:
  - Context: git diff + test output + CLAUDE.md conventions
  - Input: executor's branch
  - Output: approval (merge-ready) or feedback (written to signal file, executor picks up)
  - Uses caveman skill
  - Does not see architect's reasoning or trade journal

- Verification loop: Architect → Executor(s) → Reviewer → (back to Architect if re-planning
  needed) → merge. Loop terminates when Reviewer approves.

- Integration with signal daemon: daemon posts agent lifecycle events to `#agent-tasks` Discord
  channel. Human can observe progress without being in the loop.

**Constraints:**
- No custom agent runtime framework. Claude Code instances in tmux sessions. Coordination is
  through the signal file protocol defined in Phase E.
- Context boundaries are enforced by what files are included in each agent's working directory
  or CLAUDE.md instructions, not by code-level access control.

**Verification:** End-to-end test: human posts a directive to Discord → signal daemon writes task →
architect produces plan → executor implements → reviewer approves → branch merged. Full cycle
without human intervention beyond the initial directive.

---

## Model Tier + Token Cost Strategy

| Agent | Model | Output mode | Estimated cost/cycle |
|-------|-------|-------------|---------------------|
| Ops monitor | Haiku | Terse | Low — mostly reads, occasional short reports |
| Strategy analyst | Sonnet | Normal | Medium — one post-market analysis per day |
| **Dialogue** | **Claude Code (Max plan)** | **Normal** | **Zero — Max subscription, no API** |
| Architect | Opus | Normal | Medium — full reasoning on task decomposition |
| Executor | Opus | Caveman | Medium-High — code generation, scoped context |
| Reviewer | Sonnet | Caveman | Low — diff review, test verification |
| Signal daemon | None | N/A | Zero — pure Python, no LLM calls |

Caveman skill reduces executor/reviewer token output significantly. Combined with scoped context
(executor only sees its zone), the per-agent cost stays manageable.

---

## Dependencies and Ordering

```
Phase A   (signal files)       ← no dependencies, can start immediately
Phase B   (signal daemon)      ← depends on A
Phase B.5 (strategy dialogue)  ← depends on B (needs Discord bridge)
Phase C   (ops monitor)        ← depends on B (needs Discord for notifications)
Phase D   (strategy analyst)   ← depends on A (needs signal files), independent of B/C
Phase E   (task format)        ← depends on A (task files are signal files), independent of B/C/D
Phase F   (dev agent cycle)    ← depends on B + E

Parallelizable: B.5, C, and D can run in parallel after B
Parallelizable: D and E can run in parallel after A
```

---

## What This Plan Does NOT Cover

- **Bot autonomy escalation** (supervised → guided → autonomous → silent). This is a config
  change + approval gate mechanism that layers on top of Phase A's signal files. Worth doing
  but not phase-level work — it's a feature within Phase C's ops monitor.
- **Prompt versioning fix** (CONCERN-5). Should be done before Phase F to avoid multi-agent
  prompt conflicts, but it's an independent fix, not part of this plan.
- **Custom agent runtime or framework.** Explicitly rejected. Claude Code in tmux + signal files.
- **Complex message broker.** Explicitly rejected. No Redis, no RabbitMQ. File polling.

---

## Success Criteria

The agentic workflow is complete when:

1. The operator can leave the bot running during market hours and receive Discord notifications
   for all significant events without watching logs.
2. The operator can issue development directives from Discord and have them decomposed, implemented,
   reviewed, and merged without being at the keyboard for the implementation.
3. Post-market analysis is automated — trade journal findings are categorized, documented, and
   fed into the development backlog without manual session log reading.
4. Bug reports from the ops monitor flow into the dev pipeline and get fixed without the operator
   triaging them manually.
5. Multiple dev agents can work in parallel on different zones without file conflicts or
   integration regressions.
