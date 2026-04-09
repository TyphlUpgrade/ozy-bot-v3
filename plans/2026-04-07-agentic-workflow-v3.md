# Agentic Workflow — Phase Decomposition Plan (v3)

**Date:** 2026-04-07  
**Motivated by:** Operator spends entire trading days babysitting the bot, manually approving minor
decisions during phase work, and performing post-session analyses that could be automated. The
orchestrator extraction (Phase 1+2) created 11 parallel work zones — the infrastructure for
multi-agent development exists but has no coordination layer.

**Reference material:**
- `claw-code-analysis.md` — deep engineering analysis of OmX, clawhip, OmO, OMC (revised 2026-04-07)
- `NOTES.md` § "Agentic Development Workflow Design" — signal file architecture, agent roles, bus design
- `Multilayer agentic workflow spec.pdf` — rough feature spec (problem statement + channel layout)
- Cost-benefit analysis of 4 architecture models (conversation record, 2026-04-07)

**Key architectural decision:** Three complementary layers, each with a different scope:
- **OMC (oh-my-claudecode):** Inside each Claude Code session — hooks, agent role behavior,
  persistent mode, subagent delegation, failure tracking, context monitoring
- **Orchestrator (`tools/agent_runner.py`):** Between sessions — task routing, worktree management,
  API calls for Architect/Reviewer, checkpoint protocol, cost tracking
- **clawhip:** Between the system and the outside world — Discord routing, git monitoring,
  filesystem watching, notification delivery

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

1. **Signal files are the universal bus.** The trading bot, ops agents, dev agents, and the signal
   daemon all communicate through structured JSON files in `state/signals/`. No direct coupling
   between systems. Discord is one client of this bus, not the bus itself. One state system — no
   competing state stores.

2. **Monitoring stays outside agent context windows.** An agent implementing a feature should never
   have notification logic or Discord formatting in its working memory. clawhip owns all monitoring
   and delivery. (claw-code analysis §2.3)

3. **Three separated concerns.** Workflow decomposition (prompt templates + orchestrator script),
   event routing (clawhip), and agent coordination (sequential pipeline in orchestrator). These are
   distinct layers, not one monolith. (claw-code analysis §2.3)

4. **Recovery before escalation.** Known failure modes auto-recover once before asking for help.
   Ops agents classify failures by typed `FailureKind`, apply per-class recovery recipes, and
   escalate only when recovery fails.

5. **State machine first, not log first.** Agent lifecycle, bot process state, and task status are
   typed state machines with explicit transitions — not inferred from log parsing.

6. **Token cost discipline.** Executor and Reviewer use terse output mode (caveman skill). Ops
   Monitor uses Haiku. Strategy Analyst uses Sonnet. Per-task cost tracking with budget limits.
   Model override per-task when complexity doesn't justify the default tier.

7. **Mandatory pressure-testing at every role.** Every agent role has at least one built-in
   adversarial check before proceeding, matched to that role's primary failure mode. Checks use
   quantified ambiguity scoring with per-role threshold gates — not optional "use your judgment"
   guidance. (§ Pressure-Testing Protocol)

8. **Architect-in-the-loop for Executor.** The Executor does not operate autonomously for the
   full implementation. The Architect defines mandatory checkpoints in the plan; the Executor
   pauses at each checkpoint for Architect review before continuing. (§ Architect Approval Gates)

9. **Authoring and review are always separate passes.** Agents that analyze (Architect, Reviewer,
   Analyst) cannot write code — enforced by tool restrictions, not just prompts. Agents that write
   code (Executor) cannot self-approve. (claw-code analysis §2.4)

10. **Skills are prompt injection, not code execution.** Agent roles are markdown files with YAML
    frontmatter that get loaded as context. The "agent" is Claude following different instructions
    depending on the role. No custom agent framework. (claw-code analysis §2.1)

11. **OMC for session internals, orchestrator for session externals.** OMC handles what happens
    inside a Claude Code session (hooks, persistent mode, delegation, failure tracking). The
    orchestrator handles what happens between sessions (task routing, worktree lifecycle, API
    calls, checkpoint protocol). These are complementary layers, not competing ones.

---

## Session Architecture: Persistent Core, Ephemeral Workers (Model C)

Evaluated four architecture models (Full Presence, API-Orchestrated, Persistent Core +
Ephemeral Workers, Full Claw-Code Stack). Selected Model C based on:

- Persistent agents where persistence matters (Ops Monitor accumulates context over the trading
  day, Dialogue maintains strategic conversation)
- Ephemeral agents where statelessness is an advantage (Executor does work and exits — no stale
  context, no compaction risk during idle time, no session management)
- Skip the oh-my-openagent coordination layer entirely — our dev pipeline is sequential
  (Architect → Executor → Reviewer), not a committee requiring conflict resolution

| Agent | Implementation | Why |
|-------|---------------|-----|
| **Dialogue** | Claude Code on Max plan, persistent tmux session | Conversational, full filesystem access, zero cost |
| **Ops Monitor** | Claude Code on API (Haiku), persistent tmux session | Accumulates context over trading day, needs pattern awareness |
| **Strategy Analyst** | API call (Sonnet) | Post-market, one-shot analysis, no filesystem interaction needed |
| **Architect** | API call (Opus) | Reads docs + task → outputs plan. One call. No filesystem interaction. |
| **Executor** | Claude Code on API (Opus), ephemeral tmux session in git worktree | Full filesystem access in isolated worktree. Spawned per-task, worktree cleaned up on merge. |
| **Reviewer** | API call (Sonnet) | Reads diff + test output → outputs verdict. One call. |
| **Orchestrator** | Python script (`tools/agent_runner.py`) | Assembles context, makes API calls, spawns Executor sessions, manages task lifecycle |
| **clawhip** | Rust daemon, persistent | Event routing, no LLM |

**What the orchestrator does:**
1. Watches `state/agent_tasks/` for new tasks
2. For API agents: assembles context packet → API call → writes result to signal file
3. For Executor: creates git worktree on feature branch → writes plan + zone file into
   worktree → writes worktree-specific CLAUDE.md with role definition → spawns Claude Code
   in tmux with worktree as working directory → monitors for completion/checkpoint/blocked signals
4. Manages the Architect → Executor → Reviewer pipeline sequentially
5. Handles checkpoint protocol (Executor pauses → orchestrator calls Architect API → routes response)
6. Handles failure escalation (3 failures → different approach, 5 → escalate to operator)
7. Tracks per-task cost (token usage per API call, alerts at 80% budget)

**tmux layout:**
```
Session: ozymandias
├── Pane 0: Trading bot
├── Pane 1: clawhip daemon
├── Pane 2: Discord companion
├── Pane 3: Ops Monitor (persistent, Haiku)
├── Pane 4: Dialogue (persistent, Max plan, bridged to Discord #strategy)
├── Pane 5: Orchestrator (tools/agent_runner.py)
└── Pane 6+: Executor (ephemeral, spawned per-task in git worktree by orchestrator)
```

**Orchestrator lifecycle:**
- Starts in its own tmux pane (Pane 5), runs continuously
- State persisted to `state/orchestrator_state.json` (active tasks, spawned sessions, worktree
  paths, cost accumulators)
- On crash/restart: reads state file, resumes monitoring in-progress worktrees, re-polls for
  pending tasks. In-progress Executors are unaffected (they work independently in worktrees).
- Heartbeat: writes `state/signals/orchestrator/heartbeat.json` every 30 seconds. clawhip
  watches this and alerts Discord if stale (>2 minutes).

---

## Agent Role Definition Format

All agent roles are defined as markdown files in `config/agent_roles/` with YAML frontmatter
and XML-structured prompt body. Format adapted from OMC (claw-code analysis §2.2, §4.1):

```yaml
---
name: executor
description: Zone-scoped code implementation
model: claude-opus-4-6
tier: HIGH
mode: claude-code          # "api" or "claude-code"
isolation: worktree        # runs in isolated git worktree on feature branch
output: caveman            # "normal" or "caveman"
max_iterations: 10         # hard cap on retry loops
cost_budget_usd: 5.00      # per-task cost limit
---
<Agent_Prompt>
  <Role>
    You are the Executor. You implement exactly what the plan specifies.
    You are working in an isolated git worktree on a feature branch.
    Your changes will be reviewed before merging to main.
  </Role>
  <Trading_Domain_Rules>
    Ozymandias conventions: async everywhere, no third-party TA libs,
    atomic JSON writes, get_logger(), StateManager...
  </Trading_Domain_Rules>
  <Constraints>
    - Focus on the files listed in your zone — do not modify unrelated modules
    - Do not add features beyond the plan
    - Do not spawn subagents or invoke orchestration commands
    - Do not self-review — the Reviewer handles quality
    - Commit your work before reporting completion
  </Constraints>
  <Execution_Policy>
    On startup: read `.executor/architect_response.json` if it exists (you may be
    resuming after a checkpoint). Read zone file for progress state.

    For each implementation unit in the plan:
    1. Read the unit description and acceptance criteria
    2. Simplifier check: "Can I do this with less code than planned?"
    3. Implement the unit
    4. Run tests — do not proceed to the next unit with failing tests
    5. Commit to feature branch with structured message
    6. Update zone file with completion status + wall_clock_seconds
    7. If this unit is a CHECKPOINT: write `.executor/checkpoint.json`,
       then EXIT. The orchestrator will review your work, get Architect
       feedback, and spawn a fresh session to continue from the next unit.
    8. Next unit
    On final unit: write `.executor/completion.json`
    If blocked: write `.executor/blocked.json` and EXIT. The orchestrator
    will route to Architect and spawn a fresh session with the response.
    All signal files are written within your worktree — never write outside it.
  </Execution_Policy>
  <Failure_Modes_To_Avoid>
    - Retrying the same approach after 3 failures (try something different)
    - Modifying unrelated modules to make your zone work (escalate instead)
    - Adding error handling, comments, or type annotations to unchanged code
    - Scope creep: "while I'm here, I should also..."
  </Failure_Modes_To_Avoid>
  <Output_Format>
    Zone file updated with: units completed, unit in progress, units remaining,
    test status, branch name. Written after each unit.
  </Output_Format>
</Agent_Prompt>
```

Every agent that touches trading code includes a `<Trading_Domain_Rules>` section referencing
CLAUDE.md conventions. This ensures domain constraints survive context scoping.

---

## Agent Safety Infrastructure

Two layers of safety: OMC provides session-internal safety (hooks that run inside each Claude
Code session), and the orchestrator provides session-external safety (cost tracking, worktree
lifecycle, task-level escalation).

### OMC-Provided (session-internal)

These come from OMC's existing hook system, installed into each Claude Code session:

- **Persistent mode** (`persistent-mode.cjs`, Stop hook) — prevents premature termination during
  active work. Safety guards already built in: never blocks context limits, user abort, auth
  errors, or stale state (>2 hours). Cancel signal coordination via TTL-based state file.
- **Context window monitoring** (`context-window-monitor`, 70% warning + 78% preemptive
  compaction with 60-second cooldown). PreCompact hooks preserve critical context through
  compaction.
- **Tool failure retry tracking** (`post-tool-use-failure.mjs`) — counts consecutive failures
  within 60-second windows, injects "try a different approach" after 5 failures.
- **Anti-spawn-loop** (`pre-tool-enforcer.mjs`) — when `OMC_TEAM_WORKER` env is set, blocks
  Task tool, Skill tool, and orchestration commands. Workers cannot spawn more workers.
- **Subagent tracking** (`subagent-tracker.mjs`) — tracks all spawned agents with model, tokens,
  duration. $1 cost limit per subagent. Stale agent detection (>5 min without progress).
- **Anti-duplication** (injected into orchestrator prompts) — "DO NOT perform the same search
  yourself after delegating."

These hooks are battle-tested and handle edge cases (session isolation via session_id +
project_path, stale state expiry, cancel coordination) that we would otherwise need to
discover and debug ourselves.

### Orchestrator-Provided (session-external)

These are managed by `tools/agent_runner.py`, operating across sessions:

- **Git worktree isolation** — Executor runs in an isolated worktree on a feature branch (same
  pattern as OmX `$team --worktree`). Full read/write access; the merge is the quality gate.
  Worktree lifecycle managed by orchestrator:
  1. `git worktree add .worktrees/<task-id> -b feature/<task-id>`
  2. Write plan + zone file + worktree-specific CLAUDE.md into worktree
  3. Create `<worktree>/.executor/` directory for worktree-local signals
  4. Spawn Claude Code with `--cwd .worktrees/<task-id>`
  5. On completion: Executor commits to feature branch
  6. Reviewer checks `git diff main...feature/<task-id>`
  7. On approval: run full test suite on main after merge, cleanup (`git worktree remove`)
  8. On failure/timeout: preserve worktree for debugging
  9. On post-merge test failure: revert merge commit, escalate to operator

- **Worktree signal polling** — orchestrator polls `<worktree>/.executor/` for all active
  worktrees (paths tracked in `state/orchestrator_state.json`). Poll interval: 5 seconds.
  Detected signals are routed: checkpoints → Architect API call, blocked → Architect API call,
  completion → Reviewer API call. Orchestrator writes to main repo `state/signals/` for any
  event that needs Discord notification (clawhip's domain, not the Executor's).

- **Per-task cost tracking** — orchestrator tracks token usage per API call and per task.
  For Claude Code Executor sessions: Executor role prompt includes instruction to write
  cumulative token usage to zone file after each unit. Orchestrator reads this.
  Alert at 80% budget via signal file. Kill session at 100% if no human override.
  Default budgets: quick ($0.50), standard ($3.00), deep ($8.00), complex ($15.00).

- **Task-level failure escalation** — 3 failures within a task: orchestrator writes "try a
  different approach" to `<worktree>/.executor/architect_response.json` (complements OMC's
  tool-level tracking). 5 failures: escalate to Architect for re-planning. Architect also
  fails: escalate to operator via Discord.

- **Executor wall-clock timeout** — 60-minute default per task. Orchestrator kills the tmux
  pane, preserves worktree for debugging, escalates to operator.

- **Task deduplication** — before accepting a new task, orchestrator hashes `TASK` + `zone`
  fields and checks against all known tasks (active + historical in
  `state/orchestrator_state.json`). Behavior depends on the existing task's status:
  - `pending` or `in_progress`: drop — work already scheduled. Log entry.
  - `completed`: allow through — this is a regression, the fix didn't hold.
  - `failed` or `failed_post_merge`: allow through, flag as retry in the task packet.
  - `dismissed`: drop — operator already decided this isn't worth fixing.

- **Stale state detection** — orchestrator ignores state files older than 2 hours. clawhip emits
  warning on stale state detection.

### Discord Companion (user-facing)

- **Informational intent filtering** — before dispatching a command, check for patterns (`what is`,
  `how does`, `explain`). "What does !status do?" should not trigger the status command.

---

## Structured Task Packet Format

When the orchestrator delegates work to any agent, it uses a structured 6-section format
(adapted from OmO's delegation protocol, claw-code analysis §4.3):

```json
{
  "task_id": "2026-04-07-fix-rvol-oscillation",
  "sections": {
    "TASK": "Fix RVOL filter oscillation between Claude reasoning calls",
    "EXPECTED_OUTCOME": "min_rvol value persists across reasoning calls within a session",
    "MUST_DO": [
      "Persist filter_adjustments.min_rvol to session state",
      "Load persisted value as floor for subsequent calls",
      "Add test verifying persistence across simulated reasoning cycles"
    ],
    "MUST_NOT_DO": [
      "Modify the risk manager",
      "Change how filter_adjustments are applied to the ranker",
      "Add new config parameters"
    ],
    "CONTEXT": "See DRIFT_LOG § filter_adjustments.min_rvol. Current behavior: each reasoning call re-evaluates from scratch, causing oscillation from 1.2 to 0.7 within a session.",
    "ACCEPTANCE_TESTS": [
      "test_rvol_persistence_across_calls",
      "test_rvol_floor_not_below_strategy_minimum"
    ]
  },
  "source": "strategy_analyst",
  "priority": "backlog",
  "model_override": null,
  "zone": "core/orchestrator.py",
  "checkpoint_units": [2]
}
```

The `MUST_NOT_DO` section is the most important — it prevents the most common delegation failure:
the agent solving a different problem than intended. The `checkpoint_units` array tells the
Executor which implementation units require Architect review before continuing.

---

## Intent Classification Gate

Before planning, the Architect classifies each incoming task (adapted from OmO's Sisyphus intent
gate, claw-code analysis §4.2):

| Classification | Description | Decomposition strategy |
|---------------|-------------|----------------------|
| Bug fix | Known broken behavior, reproducer exists | Minimal change, focused scope, no new tests beyond regression |
| Calibration | Parameter tuning, prompt edit | Config or prompt file change only, no structural changes |
| Feature | New capability | Full plan with zones, checkpoints, and acceptance tests |
| Refactor | Structural improvement, no behavior change | Plan with migration strategy, before/after tests must be identical |
| Analysis | Investigation, no code change | Read-only, output is a document, not code |

The classification determines: plan depth, number of implementation units, checkpoint placement,
and Reviewer verification tier.

---

## Pressure-Testing Protocol

Each agent role has a mandatory adversarial check matched to its primary failure mode. Checks use
the ambiguity scoring framework with per-role thresholds (Option A — per-role calibration, not
cross-role normalization).

**Ambiguity scoring framework (brownfield weights):**

| Dimension | Weight | What it checks |
|-----------|--------|----------------|
| Intent | 0.25 | "Are we optimizing for fewer losses or more wins?" |
| Outcome | 0.20 | "What does success look like? Config change? New module?" |
| Scope | 0.20 | "Does this touch just the ranker, or ripple into risk management?" |
| Constraints | 0.15 | "Must this work within existing loop timing?" |
| Success criteria | 0.10 | "How do we know this worked? Backtest? Paper trading period?" |
| Context | 0.10 | "Market-condition-specific fix or structural improvement?" |

Weighted ambiguity = Σ(weight × score). If ambiguity exceeds the role's threshold, the agent
**must** ask clarifying questions before proceeding.

| Role | Persona | Failure mode it catches | Threshold |
|------|---------|------------------------|-----------|
| Dialogue | All three (Contrarian + Simplifier + Ontologist) | Ambiguous plans | 0.20 |
| Architect | Contrarian + Simplifier | Over-scoped decomposition | 0.20 |
| Executor | Simplifier | Over-engineering, building beyond plan | 0.15 |
| Reviewer | Contrarian | Rubber-stamp reviews, missing interactions | 0.25 |
| Strategy Analyst | Ontologist | False discoveries, hindsight-as-insight | 0.20 |
| Ops Monitor | — (Haiku, too lightweight) | N/A | N/A |

**Persona definitions:**
- **Contrarian:** "What breaks if this interacts with X? What's the failure mode?"
- **Simplifier:** "Can we get 80% of this with less code than planned? Is this over-built?"
- **Ontologist:** "Is this actually new, or an instance of something we already have?"

**Mandatory readiness gates** (independent of ambiguity score). Before any plan is handed from
Dialogue to Architect, or from Architect to Executor:
1. **Non-goals** — What this change explicitly does NOT do
2. **Decision boundaries** — What the Executor can decide autonomously vs. what requires escalation

---

## Architect Approval Gates

The Executor does not run autonomously through the entire implementation. The Architect defines
**checkpoints** in the plan where the Executor must pause for review.

### Plan file structure for checkpoints

```
## Implementation Units
1. [description]
2. [description] ← CHECKPOINT: Architect reviews approach before continuing
3. [description]
4. [description] ← CHECKPOINT: Reviewer before merge
```

The Architect decides where checkpoints go based on where the risk is. The Architect controls
*when* oversight happens; the Executor controls *how* implementation happens.

### Checkpoint protocol (exit-and-respawn)

Zone files carry all state needed for crash recovery — session continuity adds no value and
burns Opus context on idle polling. Checkpoints use exit-and-respawn instead of polling.

1. Executor completes the checkpoint unit, commits to feature branch in its worktree
2. Executor updates zone file with checkpoint status (including `resumed_from_checkpoint: null`
   or the unit number if this is a resumed session) and writes
   `<worktree>/.executor/checkpoint.json` with: unit completed, approach taken, ambiguities
   encountered, test status
3. **Executor exits.** The session terminates cleanly. Zone file + git branch carry all state.
4. Orchestrator detects checkpoint signal (polls `<worktree>/.executor/` for all active
   worktrees), assembles Architect review context (plan + `git diff main...feature/<task-id>` +
   test output), makes Architect API call
5. Architect responds with either:
   - `proceed` — Orchestrator pre-seeds `<worktree>/.executor/architect_response.json` with
     `{"verdict": "proceed", "notes": "..."}`
   - `course-correct` — Orchestrator pre-seeds `<worktree>/.executor/architect_response.json`
     with `{"verdict": "course-correct", "instructions": "..."}`
6. **Orchestrator spawns a fresh Executor** in the same worktree with initial prompt:
   "Continue from unit N. Read architect_response.json for review feedback, then read your
   zone file for progress state." Fresh context window per segment.
7. If Architect API call fails → escalate to Operator via Discord
8. Orchestrator writes to `state/signals/executor/<task-id>/checkpoint.json` in the main repo
   for clawhip → Discord notification (orchestrator's responsibility, not Executor's)

**Tradeoff:** Loses in-session mental model (files read, approaches tried). Mitigated by zone
file + git commits + Architect response. Over a 5-checkpoint plan, fresh context per segment
dominates vs. 4 rounds of idle-polling degradation at Opus cost.

**Signal file convention — strict role separation:**
- The Executor writes signals **within its own worktree** (`<worktree>/.executor/`). It never
  writes outside its working directory. This is the Executor's boundary.
- The orchestrator polls all active worktree signal dirs (it tracks all worktree paths in
  `state/orchestrator_state.json`). When it detects an executor signal that needs external
  routing, the orchestrator writes to `state/signals/` in the main repo.
- clawhip watches `state/signals/` in the main repo only. It never touches worktrees.

### Mid-unit escalation

Between checkpoints, the Executor can escalate if it hits something the plan didn't anticipate:
- Executor writes `<worktree>/.executor/blocked.json` with the ambiguity description
- Orchestrator detects blocked signal, routes to Architect API call
- Architect response written to `<worktree>/.executor/architect_response.json` (same file,
  same poll mechanism)
- If Architect can't resolve → Orchestrator writes to main repo signal bus → clawhip pings
  Operator on Discord

---

## Verification Tiers

Reviewer effort is sized by change scope (adapted from OMC, claw-code analysis §8.1):

| Change scope | Verification tier | Reviewer behavior |
|-------------|-------------------|-------------------|
| ≤2 files changed | Light | Quick convention check, test pass confirmation |
| 3-10 files | Standard | Full diff review, interaction analysis, convention check |
| >10 files | Thorough | Comprehensive audit, cross-zone impact analysis, explicit approval per changed module |

The Architect specifies the expected verification tier in the task packet based on the plan's
scope. The Reviewer can upgrade but not downgrade the tier.

### Structured completion evidence

Each agent's completion signal must include structured evidence:

| Agent | Required evidence |
|-------|------------------|
| Architect | Plan file exists with non-goals, decision boundaries, and checkpoint markers |
| Executor | All tests pass, branch pushed, zone file shows all units complete |
| Reviewer | Approval with file:line citations for every finding, or structured feedback |
| Strategy Analyst | Categorized findings with signal citations at decision time |
| Ops Monitor | Bug report with reproduction steps and severity classification |

---

## Three Systems, Shared Bus

```
System 1: Bot Operations (always running during market hours)
├── Trading bot process (existing)
├── clawhip daemon (watches signals + git, routes to/from Discord)
├── Ops Monitor (persistent Haiku Claude Code session, accumulates day context)
├── Orchestrator script (watches task queue, manages agent lifecycle)
└── Strategy Analyst (post-market Sonnet API call, triggered by orchestrator)

System 2: Strategy Dialogue (on-demand, operator-initiated)
└── Dialogue agent (Claude Code on Max plan, bridged to Discord #strategy)
    - Thinking partner for the operator, not a gatekeeper
    - Reads everything: analyses, docs, journal, code, signals
    - Uses all three pressure-testing personas before crystallizing plans
    - Enforces readiness gates (non-goals + decision boundaries)
    - Zero API cost (Max plan)

System 3: Development Pipeline (on-demand or bug-triggered)
├── Architect (Opus API call — reads docs + task, outputs plan with checkpoints)
├── Executor(s) (Opus Claude Code — parallel, zone-scoped, ephemeral, in git worktrees)
└── Reviewer (Sonnet API call — reads diff + tests, outputs verdict)
    Managed by: orchestrator script (tools/agent_runner.py)

Shared: state/signals/ (signal file bus), Discord (human interface)
```

**Operator retains direct access to all layers.** Dialogue does not gate or mediate access to the
development pipeline. The operator can talk directly to dev agents, give commands, interrupt work,
and course-correct mid-implementation.

**Flow for larger-than-bug work:**
```
Strategy Analyst writes findings → Operator reads in Discord
  → Operator opens #strategy → Operator ↔ Dialogue discuss
  → Dialogue pressure-tests (Contrarian/Simplifier/Ontologist)
  → Dialogue enforces readiness gates (non-goals, decision boundaries)
  → Operator says "write the plan" → Dialogue saves plan file + task directive
  → Orchestrator detects task → Architect API call → plan with checkpoints
  → Orchestrator creates worktree(s) + spawns Executor(s) (zone-scoped, feature branches)
  → Executor pauses at checkpoints → Orchestrator calls Architect API for review
  → Executor completes → Orchestrator calls Reviewer API
  → Reviewer approves → Orchestrator merges branch
  → clawhip posts lifecycle events to Discord throughout
```

**Flow for bugs and quick fixes:**
```
Ops Monitor writes bug report to state/agent_tasks/
  → Orchestrator detects task → Architect API call → minimal plan
  → Orchestrator spawns Executor → Executor fixes → Reviewer approves → merge
  OR: Operator posts !fix <description> in Discord → companion script writes task → same pipeline
```

**Idle work:** When no human-directed or bug tasks are pending, the orchestrator checks for
`type: strategy_analysis` tasks from the Strategy Analyst. These are lower priority and yield
immediately if a higher-priority task arrives (orchestrator deprioritizes remaining backlog
units in favor of the incoming task).

---

## Phase Breakdown

Each phase is a self-contained deliverable. Phases are ordered by dependency.

---

### Phase A — Signal File API + Bot Event Emitter

**Goal:** Extend the existing `EMERGENCY_*` signal file pattern into a structured event bus.

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
- All signal writes are fire-and-forget — no new dependencies, no imports from external systems

**Constraints:**
- Zero new dependencies. JSON files with the existing atomic write pattern.
- Extend the same polling mechanism the fast loop already uses for `EMERGENCY_EXIT`.
- Signal file schemas must be machine-readable by ops agents and the orchestrator in later phases.

**Verification:** Bot runs normally with signal files being written. Manual inspection confirms
files are well-formed. `touch state/PAUSE_ENTRIES` pauses entries; removing it resumes.

---

### Phase B — clawhip + Discord Companion

**Goal:** Install and configure clawhip as the event routing daemon. Build a thin Python companion
for inbound Discord commands (clawhip is outbound-only via REST API — no gateway connection).

**What gets built:**

**clawhip configuration** (`clawhip.toml`):
```toml
[providers.discord]
token = "${DISCORD_BOT_TOKEN}"
default_channel = "${ALERTS_CHANNEL}"

[daemon]
bind = "127.0.0.1:25294"

[[monitors]]
kind = "workspace"
path = "state/signals/"
poll_interval_secs = 5
debounce_ms = 2000

[[monitors]]
kind = "git"
path = "."
poll_interval_secs = 30

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/last_trade.json" }
sink = "discord"
channel = "${TRADES_CHANNEL}"
format = "compact"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/alerts/*" }
sink = "discord"
channel = "${ALERTS_CHANNEL}"
format = "alert"
mention = "${OPERATOR_MENTION}"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/last_review.json" }
sink = "discord"
channel = "${REVIEWS_CHANNEL}"
format = "compact"

[[routes]]
event = "git.commit"
sink = "discord"
channel = "${DEV_CHANNEL}"
format = "compact"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/agent_tasks/*" }
sink = "discord"
channel = "${AGENT_CHANNEL}"
format = "compact"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/executor/checkpoint.json" }
sink = "discord"
channel = "${AGENT_CHANNEL}"
format = "alert"
```

clawhip uses `inotifywait` on Linux (external binary dependency — `sudo pacman -S inotify-tools`).
Discord integration is pure REST API (bot token + channel ID), no gateway. Rate limiting (5/sec
token bucket), circuit breaker (3 failures → 5s cooldown), retry (3 attempts with jitter), dead
letter queue for failed deliveries.

**Thin Python companion** (`tools/discord_companion.py`, ~150 lines):
- discord.py gateway connection (lightweight, message content intent only)
- Listens for commands in configured channels
- Informational intent filter: regex check for `what is|how does|explain` before dispatching
- Command mapping:
  - `!pause` → writes `state/PAUSE_ENTRIES`
  - `!resume` → removes `state/PAUSE_ENTRIES`
  - `!status` → reads `status.json`, posts formatted summary
  - `!exit [symbol]` → writes `state/EMERGENCY_EXIT`
  - `!force-reasoning` → writes `state/FORCE_REASONING`
  - `!fix <description>` → writes task to `state/agent_tasks/` with source `human`
- The companion never imports from `ozymandias/`. It reads/writes JSON files only.

**tmux session management:** clawhip can monitor tmux sessions (`clawhip tmux watch`) with keyword
detection (error, FAILED, panic) and stale timeout (30 min default). Configure for bot and agent
panes.

**Constraints:**
- clawhip and the companion are independent processes. Both can crash and restart without losing
  state — signal files are the source of truth.
- Neither process imports from `ozymandias/`.
- Discord server setup (channels, webhook URLs, bot token) is an operator task, not code.

**Verification:** Bot writes signal files (Phase A). clawhip posts them to Discord. Operator
types `!pause` in Discord, companion writes signal file, bot pauses entries. Round-trip confirmed.

---

### Phase B.5 — Strategy Dialogue Agent

**Goal:** A Claude Code instance bridged to Discord `#strategy`, giving the operator a
collaborative thinking partner at zero API cost via the Max plan.

**What gets built:**
- Agent role definition file: `config/agent_roles/dialogue.md`
- Discord ↔ Claude Code bridge in the companion script:
  - Messages in `#strategy` are piped to Claude Code process via tmux `send-keys`
  - Dialogue role prompt includes instruction: after responding, write response to
    `state/signals/dialogue/response.json` (atomic write, includes message text + timestamp)
  - Companion polls `state/signals/dialogue/response.json`, posts content to `#strategy`,
    deletes the file. Avoids `tmux capture-pane` entirely — no ANSI codes, no buffer
    truncation, no timing ambiguity.
  - Message chunking for Discord's 2000-char limit
  - Fallback: if response file not written within 120 seconds, capture-pane with ANSI strip
    as degraded mode
- Claude Code instance configuration:
  - Dedicated tmux session, Max plan, project root as working directory
  - Role-specific CLAUDE.md additions (or `.claude/` project config) defining the Dialogue role
  - Permission handling: project `.claude/settings.json` pre-allows tools Dialogue needs
    (Read, Write for plans/state only, Glob, Grep, Bash for git). No interactive permission
    prompts during bridged operation.
- Pressure-testing protocol in the role prompt:
  - Ambiguity scoring (6 dimensions, brownfield weights, threshold 0.20)
  - All three personas (Contrarian, Simplifier, Ontologist)
  - Mandatory readiness gates on all plan output (non-goals, decision boundaries)
- Output actions (operator-directed only):
  - Write plan files to `plans/` (with readiness gates)
  - Write task directives to `state/agent_tasks/`
  - Update NOTES.md with analyses or concerns
  - Post summaries to other Discord channels via signal files
- Session recovery: companion detects dead Claude Code process, restarts with same working
  directory. Conversation continuity is best-effort — filesystem is the persistent memory.

**What this is NOT:**
- Not a gatekeeper — operator retains direct access to everything
- Not autonomous — acts only when operator directs
- Not the Architect — Dialogue decides *what* and *why*, Architect decides *how* and *where*

**Verification:** Operator types in `#strategy`. Response appears. Operator asks for a plan file —
file appears in `plans/` with non-goals and decision boundaries sections.

---

### Phase C — Ops Monitor Agent

**Goal:** A persistent Claude Code instance (Haiku) that watches the bot's signal output, detects
anomalies over the trading day, manages the process, and documents bugs.

**What gets built:**
- Agent role definition file: `config/agent_roles/ops_monitor.md`
- Persistent Haiku session in tmux, runs during market hours
- Reads `state/signals/status.json` on a timer (every 60 seconds)
- Anomaly detection:
  - Stale timestamps (status.json not updated for >30 seconds)
  - Repeated WARNING clusters (same warning 5+ times in 10 minutes)
  - ERROR/CRITICAL patterns in session log
  - Equity drawdown beyond threshold
  - Pattern accumulation: "third time RVOL drifted today" requires day-long context
- Bug documentation: structured reports to `state/agent_tasks/` with reproduction steps,
  severity classification, and affected zone
- Process management: restart bot via process signal, with cooldown (max 3/hour)
- Stop hook: persistent mode prevents Haiku from terminating during market hours.
  Safety guards: never block context limits, user abort, or auth errors.

**Context scope:** Logs, signal files, trade journal, config.json. Never sees source code,
CLAUDE.md, or plans. `disallowedTools: Write, Edit` for source files (can write bug reports
to `state/agent_tasks/`).

**Permission tiers:**
- **ReadOnly** (always): read logs, signals, journal; post to Discord
- **ProcessControl** (with notification): restart bot, pause entries, force reasoning
- **DangerFullAccess** (requires human approval): exit positions, modify config

**Escalation protocol:**
- Auto-handle: restart after crash (with cooldown), notify Discord
- Notify + act: pause entries during anomalous behavior, resume after 10 min if no human response
- Escalate and wait: never autonomously exit positions, modify config, or touch code

**Verification:** Kill the bot process. Ops Monitor detects, restarts, posts to Discord. Inject
repeated WARNINGs. Ops Monitor detects cluster, writes bug report.

---

### Phase D — Strategy Analyst Agent

**Goal:** Post-market Sonnet API call that reads the trade journal, categorizes outcomes, and
writes structured analysis feeding into the development backlog.

**What gets built:**
- Agent role definition file: `config/agent_roles/strategy_analyst.md`
- Orchestrator triggers post-market (after session close signal)
- Context assembled by orchestrator: trade journal entries for the session, NOTES.md (known
  concerns), watchlist state at session close, recent entries from
  `state/analyst_findings_log.json` (prevents re-discovering known issues)
- Ontologist pressure-test in role prompt: before reporting a finding, check "Is this actually
  new, or a known behavior I'm re-discovering?" by cross-referencing provided NOTES.md content
  AND the findings log
- Four-category outcome classification for each trade:
  - **Signal present, bot ignored** — TA signals indicated correct action, bot's gates filtered
  - **Signal present, bot saw but filtered** — bot detected signal, filter blocked it
  - **Signal ambiguous, reasonable to miss** — no clear signal at decision time
  - **Truly unforeseeable** — external event with no precursor signals
- Same analysis for missed opportunities (watchlist symbols that moved but weren't entered)
- Output: structured analysis JSON in `state/agent_tasks/` tagged `type: strategy_analysis`
- **Findings log** (`state/analyst_findings_log.json`): orchestrator appends each processed
  finding with status (`queued`, `completed`, `dismissed`) and date. The Analyst sees this log
  in context so it doesn't re-discover issues that have already been acted on or dismissed.

**Hindsight bias prevention:** Each finding must cite the specific signal or indicator value that
existed at decision time. "NKE rallied 3%" is not a finding. "NKE rallied 3% — BB squeeze was
firing at entry time with RSI 22, oversold bounce was predictable from existing signals" is.

**Verification:** Run against existing trade journal (68 trades). Categorized findings with
signal citations. Manual review confirms hindsight discipline.

---

### Phase E — Orchestrator Script + Task Format

**Goal:** Build the orchestrator script that manages the agent lifecycle and define the structured
task format.

**What gets built:**

**Orchestrator script** (`tools/agent_runner.py`):
- Watches `state/agent_tasks/` for new task files
- Task prioritization: human > bug > strategy_analysis > backlog
- Task deduplication: hash of `TASK` + `zone` fields, status-aware (see § Agent Safety Infrastructure)
- For each task:
  1. Read task packet, classify intent (bug/calibration/feature/refactor/analysis)
  2. Assemble Architect context → Opus API call → plan file with checkpoints
  3. Create git worktree: `git worktree add .worktrees/<task-id> -b feature/<task-id>`
  4. Write into worktree: plan file, zone file, worktree-specific CLAUDE.md (Executor role
     definition + task packet + trading domain rules — no orchestration instructions)
  5. Create `<worktree>/.executor/` directory for worktree-local signals
  6. Spawn Claude Code: `claude --api-key $KEY -p "Read CLAUDE.md for your task" --cwd .worktrees/<task-id>`
     in a new tmux pane
  7. Poll `<worktree>/.executor/` (5-second interval) for checkpoint/blocked/completion signals
  8. At checkpoints: run `git diff main...feature/<task-id>`, assemble Architect review
     context → API call → write `<worktree>/.executor/architect_response.json`
  9. On completion: run `git diff main...feature/<task-id>` + `pytest`, assemble Reviewer
     context → Sonnet API call → approval or feedback
  10. On approval: merge branch, run full test suite on main post-merge. If post-merge tests
      fail → revert merge commit, escalate to operator. If pass → `git worktree remove`,
      update task status.
  11. On feedback: write feedback to `<worktree>/.executor/architect_response.json`, Executor
      picks up via poll, re-run Reviewer after fixes
- Per-task cost tracking: token usage per API call; Executor writes cumulative usage to zone
  file. Alert at 80% budget, kill session at 100%.
- Wall-clock timeout: 60 minutes default per task. Kill tmux pane, preserve worktree, escalate.
- Failure escalation: 3 failures → write "try different approach" to architect_response.json,
  5 → operator Discord ping
- Idle work: when no tasks pending, check for strategy_analysis tasks
- Worktree cleanup: on task failure or timeout, preserve worktree for debugging, log path
- State persistence: `state/orchestrator_state.json` tracks active tasks, worktree paths,
  spawned sessions, cost accumulators. On restart, resume monitoring in-progress worktrees.
- Heartbeat: writes `state/signals/orchestrator/heartbeat.json` every 30 seconds

**Task packet schema** (structured delegation format):
```json
{
  "task_id": "<string>",
  "sections": {
    "TASK": "<what to do>",
    "EXPECTED_OUTCOME": "<what success looks like>",
    "MUST_DO": ["<mandatory requirements>"],
    "MUST_NOT_DO": ["<explicit prohibitions>"],
    "CONTEXT": "<relevant background>",
    "ACCEPTANCE_TESTS": ["<test names or descriptions>"]
  },
  "source": "human | ops_monitor | strategy_analyst",
  "priority": "human | bug | backlog",
  "model_override": null,
  "zone": "<primary zone file>",
  "checkpoint_units": [2, 4],
  "session_id": "<uuid>",
  "project_path": "<absolute path>",
  "created_at": "<ISO timestamp>"
}
```

**Task lifecycle:** `pending → in_progress → checkpoint → in_progress → review →
completed | failed`

**Zone file schema** (Executor state persistence for crash recovery):
```json
{
  "task_id": "<string>",
  "units_completed": [1, 2],
  "unit_in_progress": 3,
  "units_remaining": [4, 5],
  "test_status": "passing",
  "branch": "feature/fix-rvol-oscillation",
  "worktree_path": ".worktrees/fix-rvol-oscillation",
  "cumulative_tokens": {"input": 45000, "output": 12000},
  "last_updated": "<ISO timestamp>"
}
```

**Verification:** Create a task file manually. Orchestrator detects it, runs Architect API call,
spawns Executor, manages checkpoint cycle, runs Reviewer. End-to-end pipeline without human
intervention.

---

### Phase F — OMC Integration + Custom Agent Roles

**Goal:** Install OMC as the session-internal agent layer. Configure its hooks for our use case.
Write custom agent role definitions for Ozymandias-specific roles. Integrate OMC's hook system
with the orchestrator's worktree and checkpoint protocol.

**What gets built:**

**OMC installation and configuration:**
- Install `oh-my-claudecode` via npm
- Configure OMC's hooks.json for our project (selective — not all 10 lifecycle events):
  - `Stop` → `persistent-mode.cjs` (keep Executor working until zone file complete)
  - `PreCompact` → `pre-compact.mjs` (preserve zone file state + trading domain rules)
  - `PreToolUse` → `pre-tool-enforcer.mjs` (block spawn loops for worker sessions)
  - `PostToolUseFailure` → `post-tool-use-failure.mjs` (failure retry tracking)
  - `SubagentStop` → `subagent-tracker.mjs` (cost tracking)
- Configure OMC's model routing for our tiers:
  - `OMC_MODEL_HIGH=claude-opus-4-6` (Architect, Executor)
  - `OMC_MODEL_MEDIUM=claude-sonnet-4-6` (Reviewer, Analyst)
  - `OMC_MODEL_LOW=claude-haiku-4-5` (Ops Monitor, Explore)
- Disable features we don't use: keyword detector (our orchestrator handles routing),
  team pipeline (our orchestrator handles the Architect → Executor → Reviewer cycle)

**Custom agent role definitions** (in OMC's `agents/` format):
- `ops_monitor.md` — anomaly detection protocol, escalation tiers, permission boundaries.
  Model: Haiku. `disallowedTools: Write, Edit` (for source files; can write bug reports).
- `strategy_analyst.md` — four-category classification, hindsight bias prevention, Ontologist
  gate. Model: Sonnet. `disallowedTools: Write, Edit`.
- `dialogue.md` — full pressure-testing protocol (Contrarian/Simplifier/Ontologist), readiness
  gates (non-goals, decision boundaries). Model: uses Max plan (not OMC model routing).

**Adapted OMC agent definitions** (modify existing OMC agents for trading domain):
- `executor.md` — add `<Trading_Domain_Rules>` section (async, no third-party TA, atomic writes),
  add worktree-aware scope guidance, add commit-before-completion rule, add Simplifier
  pressure-test gate, add zone file update protocol
- `architect.md` — add intent classification gate (bug/calibration/feature/refactor/analysis),
  add checkpoint placement strategy, add readiness gates, add `<Trading_Domain_Rules>`
- `reviewer.md` (adapt from OMC's `verifier.md`) — add Contrarian pressure-test, add
  verification tiers (light/standard/thorough), add trading convention checks, add structured
  approval format with file:line citations

**Worktree-specific CLAUDE.md:**
The orchestrator writes a custom CLAUDE.md into each worktree that:
- Loads the Executor role definition (from OMC agents/ format)
- Includes task packet and zone scope
- Includes trading domain rules from main CLAUDE.md
- Omits orchestration instructions (anti-spawn by omission)
- Configures OMC hooks for worker mode (`OMC_TEAM_WORKER=true` env var)

**What we skip from OMC:**
- Keyword detector (our orchestrator + Discord companion handle routing)
- Team pipeline (our orchestrator manages the Architect → Executor → Reviewer cycle)
- Most of the 19 stock agents (we define 6 custom roles)
- Skill system (we don't use OmX skills)
- Session start/end hooks (our orchestrator manages session lifecycle externally)

**Verification:** Spawn an Executor session in a worktree with OMC hooks active. Verify:
persistent-mode prevents premature exit. pre-tool-enforcer blocks spawn attempts. Kill the
session mid-task — verify worktree + zone file enable recovery by spawning a new session in
the same worktree. Verify Executor commits to feature branch, not main. Verify subagent-tracker
logs cost. Verify post-tool-use-failure triggers approach change after 5 failures.

---

## Graceful Shutdown Protocol

Two distinct shutdown events with different scopes:

### Market close (daily, automatic)

Triggered by: bot writes `session_close` signal to `state/signals/status.json`.

| Component | Behavior | Order |
|-----------|----------|-------|
| Trading bot | Winds down normally (existing behavior) | 1 |
| Ops Monitor | Writes daily summary to `state/agent_tasks/` (type: `daily_summary`), enters idle. Stop hook releases — Haiku session can terminate. | 2 |
| Strategy Analyst | Orchestrator triggers post-market analysis API call | 3 |
| Orchestrator | Stops accepting new tasks. In-progress Executors continue to completion (they're writing code, not trading). New tasks queued but not started until next session. | 2 |
| Executor(s) | Continue working. Unaffected by market close. | — |
| clawhip | Continues running. Routes dev notifications normally. | — |
| Dialogue | Continues running. Operator may still want to discuss strategy post-market. | — |
| Discord companion | Continues running. `!status` still works, `!pause`/`!exit` become no-ops. | — |

### Full system shutdown (operator-initiated)

Triggered by: operator writes `state/SHUTDOWN` or sends `!shutdown` via Discord.

Shutdown sequence (order matters — dependencies flow downward):

1. **Orchestrator** reads shutdown signal:
   a. Stops accepting new tasks immediately
   b. For each active Executor: writes `{"verdict": "shutdown", "instructions": "Commit current
      work, push branch, stop."}` to `<worktree>/.executor/architect_response.json`
   c. Waits up to 5 minutes for Executors to commit and exit
   d. Any Executor that hasn't exited after 5 min: log worktree path for manual recovery
   e. Writes final state to `state/orchestrator_state.json`, exits
2. **Ops Monitor** detects shutdown signal, writes final status snapshot, exits
3. **Dialogue** receives shutdown via companion bridge, exits
4. **Discord companion** posts "System shutting down" to all channels, exits
5. **clawhip** drains delivery queue (dead letter queue persists failed deliveries), exits
6. **Trading bot** — if still running, already has its own shutdown via `EMERGENCY_EXIT`

### Per-component crash recovery

Each component must handle the others being dead:

| Component | If it crashes... | Recovery |
|-----------|-----------------|----------|
| Trading bot | Ops Monitor detects stale `status.json`, restarts (max 3/hour) | Automatic |
| clawhip | Discord goes silent. No data loss — signal files persist. Orchestrator unaffected. | Manual restart or systemd |
| Ops Monitor | No anomaly detection. clawhip detects stale Ops Monitor heartbeat, alerts Discord. | Orchestrator or operator restarts |
| Orchestrator | In-progress Executors continue working in worktrees. On restart, orchestrator reads `state/orchestrator_state.json`, resumes polling active worktrees. | Automatic on restart |
| Executor | Worktree preserved with zone file showing last completed unit. Orchestrator detects no progress for >60 min (wall-clock timeout), escalates. Operator or orchestrator can spawn new Executor in same worktree to resume. | Semi-automatic |
| Dialogue | Companion detects dead process, restarts. Conversation context lost, filesystem state preserved. | Automatic |
| Discord companion | Bot commands stop working. clawhip still posts outbound notifications. | Manual restart or systemd |

---

## Rollback Protocol

**Post-merge test failure:**
1. Orchestrator runs full test suite (`pytest`) on main after every merge
2. If tests fail: `git revert --no-edit HEAD` (reverts the merge commit)
3. Orchestrator writes alert to `state/signals/alerts/` (clawhip → Discord)
4. Failed task moved to status `failed_post_merge`, worktree preserved for debugging
5. Operator decides: retry with different approach, or manual fix

**Sequential merge conflict:**
1. If two Executors finish in sequence and the second merge has conflicts:
   `git merge --no-commit feature/<task-id>` detects conflicts before committing
2. Orchestrator aborts the merge, escalates to operator with conflict file list
3. First merge is not reverted — it passed tests. Second task goes back to `review` status.

---

## Model Tier + Token Cost Strategy

| Agent | Model | Implementation | Output mode | Cost/cycle |
|-------|-------|----------------|-------------|------------|
| Ops Monitor | Haiku | Claude Code (persistent tmux) | Terse | ~$0.50-1.00/day |
| Strategy Analyst | Sonnet | API call | Normal | ~$0.30-0.50/run |
| **Dialogue** | **Max plan** | **Claude Code (persistent tmux)** | **Normal** | **Zero** |
| Architect | Opus | API call | Normal | ~$0.50-2.00/plan |
| Executor | Opus | Claude Code (ephemeral tmux, git worktree) | Caveman | ~$3-10/feature |
| Reviewer | Sonnet | API call | Caveman | ~$0.30-0.50/review |
| Orchestrator | — | Python script | — | Zero |
| clawhip | — | Rust daemon | — | Zero |

**Daily floor (bot ops, no dev work):** ~$0.80-1.50/day
**Per feature (full dev cycle):** ~$4-13
**Model override:** Any role can use a lighter model per-task. Simple config rename → Sonnet Executor.

---

## Dependencies and Ordering

```
Phase A   (signal files)              ← no dependencies, start immediately
Phase B   (clawhip + companion)       ← depends on A
Phase B.5 (dialogue)                  ← depends on B (needs Discord bridge)
Phase C   (ops monitor)               ← depends on B (needs Discord for notifications)
Phase D   (strategy analyst)          ← depends on A (needs signal files for journal access)
Phase E   (orchestrator + task format) ← depends on A + B (orchestrator uses signals + clawhip)
Phase F   (OMC + custom roles)        ← depends on E (OMC hooks integrate with orchestrator protocol)

Parallelizable after B: B.5, C
Parallelizable after A: D, early E work (task schema design)
```

---

## What This Plan Does NOT Cover

- **Bot autonomy escalation** (supervised → guided → autonomous → silent). Feature within
  Phase C's Ops Monitor, not phase-level work.
- **Prompt versioning fix** (CONCERN-5). Independent fix, should be done before Phase F.
- **Hashline editing.** Deferred — git worktrees eliminate most edit conflicts. Revisit only if
  post-merge integration conflicts become a pattern.
- **OmO adoption.** Rejected — too complex, multi-provider, OpenCode dependency. OMC is the
  Claude-native equivalent and is adopted instead. See `claw-code-analysis.md` § 7.
- **OmX adoption.** Rejected as a direct dependency — Codex-first. Patterns adopted (worktree
  isolation, structured delegation, ambiguity scoring). clawhip adopted directly.
- **oh-my-openagent coordination layer.** Rejected. Our pipeline is sequential, managed by the
  orchestrator script. No conflict resolution needed beyond escalation to operator.
- **Complex message broker.** Explicitly rejected. No Redis, no RabbitMQ. File watching via
  clawhip (inotifywait) and polling by the orchestrator.

---

## Success Criteria

The agentic workflow is complete when:

1. The operator can leave the bot running during market hours and receive Discord notifications
   for all significant events without watching logs.
2. The operator can issue development directives from Discord and have them decomposed, implemented,
   reviewed, and merged without being at the keyboard for the implementation.
3. Post-market analysis is automated — trade journal findings are categorized, documented, and
   fed into the development backlog without manual session log reading.
4. Bug reports from the Ops Monitor flow into the dev pipeline and get fixed without the operator
   triaging them manually.
5. Multiple Executors can work in parallel in separate git worktrees without file conflicts or
   integration regressions.
6. Every agent role has a quantified pressure-testing gate — no agent operates on vibes.
7. Executor implementations are checkpoint-reviewed by Architect before proceeding past high-risk
   units — no unsupervised architectural drift.
8. Per-task cost tracking is active — no runaway API spend without operator awareness.
9. Agent safety infrastructure prevents: premature termination (Stop hook), stale state (2-hour
   expiry), and context degradation (PreCompact preservation). Git worktree isolation prevents
   cross-Executor file conflicts and protects main from incomplete work.
10. Graceful shutdown: market close winds down ops agents while dev pipeline continues;
    full shutdown commits in-progress work before cleanup; each component recovers independently
    from crashes.
11. Post-merge regression safety: full test suite runs after every merge, automatic revert on
    failure, operator escalation.
12. Strategy Analyst findings are tracked and deduplicated — no re-discovering known issues.
13. Orchestrator health is monitored via heartbeat — silent failure is detected and alerted.
