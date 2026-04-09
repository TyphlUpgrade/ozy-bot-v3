# Agentic Workflow — Phase Decomposition Plan (v2)

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
- OmX `$deep-interview`, `$ralph`, `$team` skill definitions — adopted patterns documented below
- OmO Hashline and task categorization concepts — evaluated, selective adoption

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

Taken from claw-code analysis + our own architecture + OmX/OmO pattern evaluation:

1. **Signal files are the universal bus.** The trading bot, ops agents, dev agents, and the signal
   daemon all communicate through structured JSON files. No direct coupling between systems.
   Discord is one client of this bus, not the bus itself.

2. **Monitoring stays outside agent context windows.** An agent implementing a feature should never
   have notification logic or Discord formatting in its working memory. A separate daemon (clawhip)
   owns all monitoring and delivery.

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

7. **Mandatory pressure-testing at every role.** Every agent role has at least one built-in
   adversarial check before proceeding, matched to that role's primary failure mode (see
   § Pressure-Testing Protocol below). Checks use quantified ambiguity scoring with per-role
   threshold gates — not optional "use your judgment" guidance.

8. **Architect-in-the-loop for Executor.** The Executor does not operate autonomously for the
   full implementation. The Architect defines mandatory checkpoints in the plan; the Executor
   pauses at each checkpoint for Architect review before continuing (see § Architect Approval
   Gates below).

---

## Adopted Patterns from OmX and OmO

### From OmX `$deep-interview` → Dialogue + Architect + All Roles

**Ambiguity scoring framework.** Before crystallizing any decision into a plan or implementation,
agents score ambiguity across 6 dimensions (0.0–1.0 each):

| Dimension | Weight | What it checks |
|-----------|--------|----------------|
| Intent | 0.25 | "Are we optimizing for fewer losses or more wins?" |
| Outcome | 0.20 | "What does success look like? Config change? New module?" |
| Scope | 0.20 | "Does this touch just the ranker, or ripple into risk management?" |
| Constraints | 0.15 | "Must this work within existing loop timing?" |
| Success criteria | 0.10 | "How do we know this worked? Backtest? Paper trading period?" |
| Context | 0.10 | "Market-condition-specific fix or structural improvement?" |

Weighted ambiguity = Σ(weight × score). If ambiguity exceeds the role's threshold, the agent
**must** ask clarifying questions before proceeding. The threshold is a forcing function — without
it, "probe for ambiguity" becomes a suggestion the agent ignores when it feels confident.

**Mandatory readiness gates.** Before any plan is handed from Dialogue to Architect, or from
Architect to Executor, two sections must be present:
1. **Non-goals** — What this change explicitly does NOT do. Prevents scope creep during execution.
2. **Decision boundaries** — What decisions the Executor can make autonomously vs. what requires
   escalation back to Architect/Operator.

### From OmX `$ralph` → Executor Protocol

**Verification loop pattern.** The Executor's core loop is: plan → implement one unit → run tests →
verify → commit → next unit. The verification step is mandatory — no moving to the next unit with
failing tests. This matches and extends the existing CLAUDE.md bug handling rules.

**State persistence in zone files.** If an Executor crashes or runs out of context, a new instance
picks up the zone file and continues. Zone files contain: what's completed, what's in progress,
what's remaining, current test status.

**Quality gate (deslop).** Every Executor output gets a mandatory Reviewer pass. The Reviewer
checks for unnecessary comments, dead code, over-engineering, and convention violations. This is
not optional feedback — it's a gate before merge.

### From OmX `$team` → Phase F (Agent Coordination)

**Pre-launch context snapshots.** Before spawning worker agents, the coordinator creates a
compressed context packet scoped to each role:
- Executor packet: zone files + CLAUDE.md + relevant module code paths
- Reviewer packet: diff + test results + relevant CLAUDE.md rules

**Worker commit protocol.** Executors commit to feature branches with structured messages. The
coordinator (or Reviewer) merges only after approval. No direct commits to main branch by agents.

**Mailbox/ACK validation.** Workers communicate through signal files — write a message, wait for
acknowledgment. This validates our signal file bus design from Phase A.

### From OmO → Selective Adoption

**Task categorization for model routing.** OmO categorizes tasks as visual-engineering / deep /
quick / ultrabrain and routes to different models. Our model tier table (below) implements this
principle. Added refinement: model selection is a function of task complexity, not role prestige.
If an Executor task is genuinely simple (rename a config key), it could run on Sonnet. The plan
allows model override per-task, defaulting to the role's standard tier.

**Hashline (deferred).** Content-hash-anchored edits solve a real problem — agent edits corrupting
code when the file changed since the agent last read it. However, Claude Code's Edit tool already
requires exact string matching, which catches most drift. Revisit when Phase F parallel agents
reveal actual edit conflict frequency. Don't pre-build.

---

## Pressure-Testing Protocol

Each agent role has a mandatory adversarial check matched to its primary failure mode. Checks use
the ambiguity scoring framework with per-role thresholds (Option A — per-role calibration, not
cross-role normalization).

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

Thresholds are tuned empirically per role based on observed false-positive / false-negative rates
during paper trading. The value is the gate existing — not the exact numbers. Start with these
defaults, adjust based on experience.

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

The Architect decides where checkpoints go based on where the risk is. Maybe unit 1 is trivial
config and unit 2 is a dangerous refactor — checkpoint goes on unit 2, not unit 1. The Architect
controls *when* oversight happens; the Executor controls *how* implementation happens.

### Checkpoint protocol

1. Executor completes the checkpoint unit, commits to feature branch
2. Executor writes `state/signals/executor/checkpoint.json` with: unit completed, approach taken,
   any ambiguities encountered, test status
3. Daemon routes checkpoint signal to Architect (spawns Architect if no session is active)
4. Architect reviews the implementation (not just the plan, but the *code written so far*)
5. Architect responds with either:
   - `proceed` — Executor continues autonomously to next checkpoint
   - `course-correct` — Architect writes revised instructions, Executor adjusts
6. If Architect doesn't respond within timeout → escalate to Operator via Discord

### Mid-unit escalation

Between checkpoints, the Executor can optionally escalate if it hits something the plan didn't
anticipate. This uses the same signal file:
- Executor writes `state/signals/executor/blocked.json` with the ambiguity it encountered
- Daemon routes to Architect. If Architect session is alive, it responds. If not, daemon spawns one.
- If neither resolves within timeout, Operator gets pinged on Discord.

---

## Three Systems, Shared Bus

```
System 1: Bot Operations (always running during market hours)
├── Trading bot process (existing)
├── clawhip daemon (watches signals + git, routes to/from Discord)
├── Ops monitor agent (watches logs/signals, manages process, documents bugs)
└── Strategy analyst agent (post-market, reads journal, writes hindsight analysis)

System 2: Strategy Dialogue (on-demand, operator-initiated)
└── Dialogue agent (Claude Code on Max plan, bridged to Discord #strategy channel)
    - Thinking partner for the operator, not a gatekeeper
    - Reads everything: analyses, docs, journal, code, signals
    - Writes plan files + task directives when operator says "go"
    - Uses pressure-testing protocol (all three personas) before crystallizing plans
    - Enforces readiness gates (non-goals + decision boundaries) on all plan output
    - Zero API cost (Max plan)

System 3: Development Pipeline (on-demand or bug-triggered)
├── Architect agent (expands plan into zone-scoped task decomposition with checkpoints)
├── Executor agent(s) (parallel, zone-scoped, pauses at Architect-defined checkpoints)
└── Reviewer agent (runs tests, checks conventions, Contrarian pressure-test, sends feedback)

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
  → Dialogue pressure-tests the direction (Contrarian/Simplifier/Ontologist)
  → Dialogue enforces readiness gates (non-goals, decision boundaries)
  → Operator says "write the plan" → Dialogue saves plan file + task directive
  → Architect picks up task, decomposes into zone-scoped work with checkpoints
  → Executors implement (pause at checkpoints for Architect review)
  → Reviewer checks (Contrarian pressure-test on every review)
  → Operator can still talk directly to any agent at any point
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

### Phase B — Signal Daemon (clawhip)

**Goal:** Adopt [clawhip](https://github.com/anthropics/clawhip) as the signal daemon. clawhip is
a Rust-based daemon that watches filesystem events and git activity, routing notifications to
Discord. It supports Claude Code natively and has a plugin architecture for custom event handlers.

**What gets built:**
- clawhip installation and configuration for this project
- Plugin or config for outbound routing:
  - `state/signals/last_trade.json` changes → `#trades` Discord channel
  - `state/signals/last_review.json` changes → `#reviews` Discord channel
  - `state/signals/alerts/*` new files → `#alerts` Discord channel
  - `state/signals/status.json` changes → `#status` Discord channel (throttled)
  - Git commits/branches → `#dev` Discord channel
- Custom inbound handler (if clawhip plugin API supports it, otherwise thin wrapper):
  - `!pause` → writes `state/PAUSE_ENTRIES`
  - `!resume` → removes `state/PAUSE_ENTRIES`
  - `!status` → reads `status.json`, posts formatted summary
  - `!exit [symbol]` → writes `state/EMERGENCY_EXIT`
  - `!force-reasoning` → writes `state/FORCE_REASONING`
- If clawhip's plugin architecture doesn't cover inbound commands, a thin Python companion
  script handles the Discord→signal-file direction. The principle holds: daemon never imports
  from `ozymandias/`.

**Why clawhip over custom daemon:**
- Rust-based: low resource footprint, no Python GIL contention with the bot
- Filesystem watching is its core competency (inotify/kqueue, not polling)
- Plugin architecture means we extend rather than fork
- Supports Claude Code worker awareness out of the box
- Maintained by the claw-code ecosystem — battle-tested on similar agent workflows

**What does NOT get built:** Ops monitor agent, dev agent coordination, strategy analyst.

**Constraints:**
- clawhip is stateless from our perspective. It can crash and restart without losing anything —
  signal files are the source of truth, not daemon memory.
- clawhip never imports from `ozymandias/`. It reads JSON files and calls Discord webhooks.
- The daemon process is managed independently of the bot (separate tmux pane or systemd unit).

**Verification:** Bot writes signal files (Phase A). clawhip picks them up and posts to Discord.
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
    process. clawhip manages the bridge, not the agent itself.
- Claude Code instance configuration:
  - Runs in a dedicated tmux session on the Max plan (no API cost)
  - Working directory is the project root — full filesystem access to docs, signal files,
    trade journal, code, analyses, plans
  - CLAUDE.md provides the role context (strategy dialogue partner, not autonomous executor)
- Pressure-testing protocol built into Dialogue's role prompt:
  - Before crystallizing any plan, score ambiguity across 6 dimensions
  - If weighted ambiguity > 0.20, must ask clarifying questions before proceeding
  - Apply Contrarian, Simplifier, and Ontologist checks to every proposed direction
- Readiness gates enforced on all output:
  - Every plan file must include a **Non-goals** section
  - Every plan file must include a **Decision boundaries** section
  - Dialogue refuses to hand off a plan that lacks either gate
- Output actions the Dialogue agent can take when directed by the operator:
  - Write plan files to `plans/` (with readiness gates)
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
  clawhip bridges Discord to the CLI process; it does not make API calls on behalf of Dialogue.
- Session management: clawhip should handle Claude Code session restarts gracefully. If the
  session compacts or the process crashes, clawhip restarts it with the same working directory.
  Conversation continuity is best-effort, not guaranteed — the filesystem is the persistent memory.
- The bridge adds latency (tmux capture + Discord round-trip). This is acceptable for a
  conversational interface — seconds, not milliseconds.

**Verification:** Operator types a message in `#strategy` Discord channel. clawhip relays to Claude
Code. Claude Code responds (reads a file, discusses a finding). Response appears in `#strategy`.
Operator asks Dialogue to write a plan file — file appears in `plans/` with non-goals and decision
boundaries sections. End-to-end round-trip confirmed.

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
- Ontologist pressure-test built into role prompt: before reporting a finding, the analyst must
  check "Is this actually a new pattern, or a known behavior I'm re-discovering?" by cross-
  referencing NOTES.md and previous analysis files
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
- Task schema (adapted from claw-code's `TaskPacket` + OmX `$ralph` state persistence):
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
- Zone file state persistence (from OmX `$ralph`): zone files track what's completed, in progress,
  remaining, and current test status — enabling session recovery if an agent crashes
- Human-directed tasks always take priority: if a human task arrives for a zone held by a
  backlog agent, the backlog agent yields (completes current atomic step, releases lock)

**What does NOT get built:** The agents themselves — just the protocol they follow.

**Verification:** Manual walkthrough: create a task file, simulate claim/release cycle, verify
lock contention is handled correctly. Verify zone file state enables recovery after simulated crash.

---

### Phase F — Architect / Executor / Reviewer Agent Cycle

**Goal:** Define and implement the three dev agent roles with their context boundaries, checkpoint
protocol, pressure-testing gates, and the verification loop that connects them.

**What gets built:**
- **Architect agent** definition:
  - Context: CLAUDE.md, COMPLETED_PHASES.md, DRIFT_LOG.md, NOTES.md, plans/, codebase structure
  - Input: task from `state/agent_tasks/` (human directive, bug report, or strategy analysis finding)
  - Output: plan file in `plans/` with zone boundaries, file constraints, acceptance criteria,
    and **checkpoint markers** on implementation units (see § Architect Approval Gates)
  - Pressure-test: Contrarian ("what breaks?") + Simplifier ("can this be smaller?") before
    finalizing decomposition. Ambiguity threshold: 0.20.
  - Readiness gates: every plan must include non-goals and decision boundaries sections
  - Does not write code. Does not see full source implementation.

- **Executor agent** definition:
  - Context: CLAUDE.md conventions + plan + assigned zone files only
  - Pre-launch context snapshot: Architect produces a scoped context packet (zone files +
    CLAUDE.md + relevant module code paths) — Executor receives only this, not the full doc set
  - Input: plan file from Architect (with checkpoint markers)
  - Core loop (from OmX `$ralph`): implement unit → run tests → verify → commit → next unit
  - **Pauses at Architect-defined checkpoints** for approach review before continuing
  - Pressure-test: Simplifier check before each unit — "Can I do this with less code than the
    plan suggests?" Ambiguity threshold: 0.15.
  - Mid-unit escalation: writes `state/signals/executor/blocked.json` if plan is ambiguous
  - Uses caveman skill for terse output (token cost control)
  - Commits to feature branches with structured messages (from OmX `$team` protocol)
  - Multiple executors can run in parallel on different zones
  - Does not see other zones, full doc set, or trade journal

- **Reviewer agent** definition:
  - Context: git diff + test output + CLAUDE.md conventions
  - Pre-launch context snapshot: diff + test results + relevant CLAUDE.md rules only
  - Input: Executor's branch
  - Output: approval (merge-ready) or feedback (written to signal file, Executor picks up)
  - Pressure-test: Contrarian check — "What breaks if this change interacts with X? What edge
    case was missed?" Ambiguity threshold: 0.25.
  - Quality gate (deslop, from OmX `$ralph`): checks for unnecessary comments, dead code,
    over-engineering, convention violations. Mandatory, not optional feedback.
  - Uses caveman skill
  - Does not see Architect's reasoning or trade journal

- Verification loop: Architect → Executor(s) [with checkpoint pauses] → Reviewer → (back to
  Architect if re-planning needed) → merge. Loop terminates when Reviewer approves.

- Integration with clawhip: daemon posts agent lifecycle events to `#agent-tasks` Discord
  channel. Human can observe progress without being in the loop.

**Constraints:**
- Context boundaries are enforced by what files are included in each agent's context packet,
  not by code-level access control.
- Model override per-task: if an Executor task is genuinely simple (rename a config key), it can
  run on Sonnet instead of Opus. Default to the role's standard tier, override when justified.

**Verification:** End-to-end test: human posts a directive to Discord → clawhip writes task →
Architect produces plan with checkpoints → Executor implements (pauses at checkpoint, Architect
approves) → Reviewer approves → branch merged. Full cycle without human intervention beyond the
initial directive.

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
| clawhip | None | N/A | Zero — Rust daemon, no LLM calls |

Caveman skill reduces executor/reviewer token output significantly. Combined with scoped context
(executor only sees its zone), the per-agent cost stays manageable.

Model override: any role can be overridden per-task when task complexity doesn't justify the
default tier. A simple config rename doesn't need Opus.

---

## Dependencies and Ordering

```
Phase A   (signal files)       ← no dependencies, can start immediately
Phase B   (clawhip)            ← depends on A
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

- **Session architecture (tmux layout, persistent vs ephemeral agents).** Under active discussion.
  Deferred to a separate decision before Phase B implementation.
- **Bot autonomy escalation** (supervised → guided → autonomous → silent). This is a config
  change + approval gate mechanism that layers on top of Phase A's signal files. Worth doing
  but not phase-level work — it's a feature within Phase C's ops monitor.
- **Prompt versioning fix** (CONCERN-5). Should be done before Phase F to avoid multi-agent
  prompt conflicts, but it's an independent fix, not part of this plan.
- **Custom agent runtime or framework.** Explicitly rejected. Claude Code + signal files.
- **Complex message broker.** Explicitly rejected. No Redis, no RabbitMQ. File polling (or
  inotify via clawhip).

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
6. Every agent role has a quantified pressure-testing gate that prevents it from proceeding on
   ambiguous inputs — no agent operates on "vibes."
7. Executor implementations are checkpoint-reviewed by Architect before proceeding past high-risk
   implementation units — no unsupervised architectural drift.
