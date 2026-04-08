# Agentic Workflow — Phase Decomposition Plan (v4, OMC-Only)

**Date:** 2026-04-07  
**Based on:** v3 plan (same date). This variant replaces the API + OMC hybrid architecture with
a strictly OMC-only architecture — no custom Python orchestrator script, no raw Anthropic API
calls. Every agent is a Claude Code instance in its own tmux pane, coordinated by a
deterministic bash wrapper (`tools/conductor.sh`) that spawns and monitors them via tmux
commands and signal file polling, with on-demand `claude -p` invocations for judgment calls.

**Motivated by:** Same as v3. Operator spends entire trading days babysitting the bot, manually
approving minor decisions during phase work, and performing post-session analyses that could be
automated. The orchestrator extraction (Phase 1+2) created 11 parallel work zones — the
infrastructure for multi-agent development exists but has no coordination layer.

**Reference material:**
- `claw-code-analysis.md` — deep engineering analysis of OmX, clawhip, OmO, OMC (revised 2026-04-07)
- `NOTES.md` § "Agentic Development Workflow Design" — signal file architecture, agent roles, bus design
- `Multilayer agentic workflow spec.pdf` — rough feature spec (problem statement + channel layout)
- `plans/2026-04-07-agentic-workflow-v3.md` — full hybrid (API + OMC) architecture for comparison

**Key architectural decision:** Three complementary layers, each doing what it does best:
- **Conductor wrapper** (`tools/conductor.sh`, ~50-80 lines bash): Deterministic coordination.
  Owns the polling loop, signal scanning, state I/O, git operations, tmux lifecycle, timeout
  enforcement. Invokes `claude -p` on-demand for judgment calls (task classification, context
  assembly, failure diagnosis). No persistent LLM session — restores the claw-code pattern of
  deterministic code in the coordination seat.
- **OMC agent sessions:** Every agent (Architect, Executor, Reviewer) is a Claude Code instance
  with OMC hooks active, running in its own tmux pane. Spawned by the wrapper, not by an LLM.
- **clawhip:** Between the system and the outside world — Discord routing, git monitoring,
  filesystem watching, notification delivery. Unchanged from v3.

**What was removed (vs. v3):**
- `tools/agent_runner.py` — the custom Python orchestrator script (~500-800 lines). Replaced by
  `tools/conductor.sh` (~50-80 lines bash) that owns coordination deterministically, with
  on-demand `claude -p` invocations for judgment calls.
- Raw Anthropic API calls for Architect, Reviewer, Strategy Analyst. These are now Claude Code
  instances in their own tmux panes, each with a role-specific CLAUDE.md and OMC hooks.

**Tradeoffs vs. v3:**
- **Much simpler coordination code** — ~50-80 lines bash vs. ~500-800 lines Python. No API key
  management for raw calls. No custom context assembly or response parsing in the wrapper. The
  wrapper is trivially debuggable — it's a `while true` loop with `ls`, `jq`, and `tmux`.
- **Higher per-interaction cost** — Claude Code sessions have overhead (system prompt, tool
  definitions) that raw API calls don't. Architect and Reviewer sessions cost ~2-3x more than
  v3's lean API packets. Offset by Max plan for zero-cost persistent roles (Dialogue, Ops Monitor).
- **Less precise context control** — v3's API calls gave the orchestrator exact control over what
  each agent saw. Claude Code sessions have full filesystem access (constrained by
  `disallowedTools` and working directory, but not hermetically sealed). Mitigated by
  worktree isolation for Executors and role prompts for read-only agents.
- **Better agent capability** — Architect can browse the codebase, run grep, check tests.
  Reviewer can run the test suite directly, not just read test output assembled by the
  orchestrator. These agents are more capable than their API-call equivalents.
- **No Conductor context window risk** — the wrapper is stateless bash. `claude -p` invocations
  are fresh sessions with focused context. No accumulation, no compaction, no degradation.
  This resolves v4's originally identified "single genuinely novel risk."
- **Independent observability** — every agent runs in its own tmux pane. The operator can
  `tmux select-pane` to watch any agent work in real time. (Same as v3, unlike an in-process
  subagent approach.)

---

## Problem Statement

*Unchanged from v3.* Three pain points, in order of severity:

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
   competing state stores. Agents write to scoped **outboxes** (e.g., `<worktree>/.executor/`);
   the Conductor acts as a **signal gateway**, polling outboxes and routing events to the shared
   bus. This is one bus with scoped write points, not two competing buses. (CONCERN-8 resolution:
   rename, don't unify — the dual namespace is architecturally correct.)

2. **Monitoring stays outside agent context windows.** An agent implementing a feature should never
   have notification logic or Discord formatting in its working memory. clawhip owns all monitoring
   and delivery. (claw-code analysis §2.3)

3. **Three separated concerns, correctly split.** Deterministic coordination (bash wrapper —
   polling, state I/O, git, tmux lifecycle), agent execution (OMC — Claude Code sessions with
   hooks), and event routing (clawhip — Discord, monitoring, notifications). The wrapper replaces
   both v3's Python script and v4's original persistent LLM Conductor. LLM judgment is invoked
   on-demand via `claude -p`, not embedded in the coordination loop.

4. **Recovery before escalation.** Known failure modes auto-recover once before asking for help.
   Ops agents classify failures by typed `FailureKind`, apply per-class recovery recipes, and
   escalate only when recovery fails.

5. **State machine first, not log first.** Agent lifecycle, bot process state, and task status are
   typed state machines with explicit transitions — not inferred from log parsing.

6. **Wall-clock cost discipline.** Claude Code sessions don't expose token counts
   programmatically (`--max-budget-usd` only works with `--print` mode, not interactive tmux
   sessions — CONCERN-9a). Budget enforcement uses **wall-clock timeout** (60 min default),
   not token tracking. Executor and Reviewer use terse output mode (caveman skill). Ops Monitor
   uses Haiku. Strategy Analyst uses Sonnet. Model override per-task when complexity doesn't
   justify the default tier. Max plan covers Dialogue (zero cost). Conductor standing cost is
   near-zero (bash wrapper + on-demand Sonnet `claude -p` per judgment call).
   Per-cycle cost estimates in the cost table are projections, not tracked actuals.

7. **Mandatory pressure-testing at every role.** Every agent role has at least one built-in
   adversarial check before proceeding, matched to that role's primary failure mode. Checks use
   quantified ambiguity scoring with per-role threshold gates — not optional "use your judgment"
   guidance. (§ Pressure-Testing Protocol)

8. **Architect-in-the-loop for Executor.** The Executor does not operate autonomously for the
   full implementation. The Architect defines mandatory checkpoints in the plan; the Executor
   pauses at each checkpoint for Architect review before continuing. (§ Architect Approval Gates)

9. **Authoring and review are always separate passes.** Agents that analyze (Architect, Reviewer,
   Analyst) cannot write code — enforced by tool restrictions (`disallowedTools`), not just
   prompts. Agents that write code (Executor) cannot self-approve. (claw-code analysis §2.4)

10. **Skills are prompt injection, not code execution.** Agent roles are markdown files with YAML
    frontmatter that get loaded as context. The "agent" is Claude following different instructions
    depending on the role. No custom agent framework. (claw-code analysis §2.1)

11. **Deterministic coordination, LLM execution.** The bash wrapper (`tools/conductor.sh`) owns
    all mechanical coordination (polling, state I/O, git, tmux lifecycle). OMC handles what
    happens inside each agent session (hooks, persistent mode, failure tracking, context
    monitoring). `claude -p` is invoked on-demand for judgment calls only. This restores the
    claw-code pattern: deterministic code in the coordination seat, LLM in the execution seat.

---

## Session Architecture: Conductor + Tmux Sessions

v3 evaluated four architecture models and selected Model C (Persistent Core + Ephemeral Workers).
This variant preserves Model C's session topology but replaces the Python orchestrator with a
deterministic bash wrapper + on-demand `claude -p` for judgment, and replaces API calls with
separate Claude Code instances in their own tmux panes.

**Key change:** The orchestrator is no longer a Python script making API calls, nor a persistent
LLM session. It is a bash script (`tools/conductor.sh`, ~50-80 lines) that owns all mechanical
coordination (polling, state I/O, git, tmux lifecycle) and invokes `claude -p` on-demand for
judgment calls (task classification, context assembly, failure diagnosis). Each agent runs in
its own tmux pane with its own context window, OMC hooks, and role-specific CLAUDE.md.

| Agent | Implementation | Why |
|-------|---------------|-----|
| **Dialogue** | Claude Code on Max plan, persistent tmux pane | Conversational, full filesystem access, zero cost |
| **Ops Monitor** | Claude Code (Haiku), persistent tmux pane | Accumulates context over trading day, needs pattern awareness |
| **Conductor** | Bash wrapper (`tools/conductor.sh`) + on-demand `claude -p` (Sonnet) | Deterministic polling loop, signal scanning, git/tmux lifecycle. Invokes LLM only for judgment. No persistent LLM session — no context degradation. |
| **Strategy Analyst** | Claude Code (Sonnet), ephemeral tmux pane, spawned by wrapper | Post-market, one-shot analysis. Spawned via `tmux split-window` with `claude -p` command. |
| **Architect** | Claude Code (Opus), ephemeral tmux pane, spawned by wrapper | Reads docs + task → outputs plan. Full codebase access. Spawned per-task. |
| **Executor** | Claude Code (Opus), ephemeral tmux pane in git worktree, spawned by wrapper | Full filesystem access in isolated worktree. Spawned per-task, worktree cleaned up on merge. |
| **Reviewer** | Claude Code (Sonnet), ephemeral tmux pane, spawned by wrapper | Reads diff + runs tests → outputs verdict. Full codebase access. Spawned per-task. |
| **clawhip** | Rust daemon, persistent | Event routing, no LLM. Unchanged from v3. |

**What the Conductor does:**
1. Watches `state/agent_tasks/` for new tasks (polls via Bash on a timer, or prompted by
   operator/Ops Monitor writing a task file and nudging the Conductor via tmux `send-keys`)
2. For Architect: creates a tmux pane, writes role-specific CLAUDE.md + task context to a
   staging directory, spawns Claude Code with `claude -p "Read CLAUDE.md..." --cwd <staging-dir>`
3. For Executor: creates git worktree via Bash (`git worktree add`), writes plan + zone file +
   worktree-specific CLAUDE.md into worktree, spawns Claude Code in a tmux pane with
   `--cwd .worktrees/<task-id>`
4. For Reviewer: creates a tmux pane, spawns Claude Code with diff + test context
5. Polls signal directories (`<worktree>/.executor/`, `state/signals/architect/`,
   `state/signals/reviewer/`) for completion, checkpoint, and blocked signals
6. Manages the Architect → Executor → Reviewer pipeline sequentially per task
7. Handles checkpoint protocol (Executor writes checkpoint signal → Conductor detects via poll →
   spawns Architect review session → writes response → spawns fresh Executor)
8. Handles failure escalation (3 failures → different approach, 5 → escalate to operator via
   signal file → clawhip → Discord)
9. Tracks per-task progress in zone files and `state/orchestrator_state.json`
10. Can forcibly kill agent panes via `tmux kill-pane` for timeout enforcement

**How the Conductor wrapper spawns agents (via tmux commands):**
```bash
# Spawn Architect in a new tmux pane
tmux split-window -t ozymandias -d \
  "claude -p 'Read CLAUDE.md for your task.' --cwd /path/to/staging/architect-<task-id>"

# Spawn Executor in a new tmux pane
tmux split-window -t ozymandias -d \
  "claude -p 'Read CLAUDE.md for your task.' --cwd /path/to/.worktrees/<task-id>"

# Spawn Reviewer in a new tmux pane
tmux split-window -t ozymandias -d \
  "claude -p 'Read CLAUDE.md for your task.' --cwd /path/to/staging/reviewer-<task-id>"
```

Each spawned session has its own:
- tmux pane (independently observable by operator)
- Working directory (worktree for Executor, staging dir for Architect/Reviewer)
- Role-specific CLAUDE.md (loaded by Claude Code on startup)
- OMC hooks (inherited from project `.claude/settings.json`)
- Context window (no cross-contamination between agents)

**Conductor wrapper approach:**
The Conductor is a deterministic bash script (`tools/conductor.sh`), not a persistent Claude Code
session. It owns the polling loop and all mechanical operations. Two input channels:
1. **Signal file polling.** The wrapper checks (via `ls`/`stat`/`jq`) for new files in
   `state/agent_tasks/`, `state/signals/architect/`, `state/signals/reviewer/`, and
   `<worktree>/.executor/` for all active worktrees. Poll interval: 10 seconds.
2. **External nudge.** The Ops Monitor, Discord companion, or operator can write a task file
   directly to `state/agent_tasks/`. The wrapper detects it on the next poll cycle (≤10s).

The wrapper invokes `claude -p` only when a signal requires judgment (task classification,
context assembly, failure diagnosis). Each invocation is a fresh session — no accumulated state,
no context degradation. No Stop hook needed — the wrapper is a bash `while true` loop.

**tmux layout:**
```
Session: ozymandias
├── Pane 0: Trading bot
├── Pane 1: clawhip daemon
├── Pane 2: Discord companion
├── Pane 3: Ops Monitor (persistent, Haiku)
├── Pane 4: Dialogue (persistent, Max plan, bridged to Discord #strategy)
├── Pane 5: Conductor wrapper (bash, tools/conductor.sh, manages dev pipeline)
├── Pane 6+: Ephemeral agent panes (Architect, Executor, Reviewer — spawned/killed by Conductor)
```

Ephemeral agent panes are created by the Conductor via `tmux split-window` and destroyed when
the agent exits or is killed. The operator can observe any agent in real time by selecting its
pane. This preserves v3's independent observability.

**Conductor state persistence:**
- State persisted to `state/orchestrator_state.json` (same schema as v3: active tasks, worktree
  paths, spawned pane IDs, cost accumulators)
- (Compaction is N/A for the Conductor wrapper — it has no context window. PreCompact hooks
  apply to persistent agent sessions: Ops Monitor, Dialogue, and long-running Executors.)
- On session restart: **git-state reconciliation before trusting state file.** Conductor runs
  `git branch --merged main` and `git worktree list` (via Bash) to determine actual repo state,
  then compares against `state/orchestrator_state.json`. Git is source of truth for committed
  changes; zone files are source of truth for task progress. If zone file says "unit 3 in
  progress" but git shows unit 5 committed, trust git (zone file is stale). Conductor also
  scans all active worktree `.executor/` dirs for unprocessed signals (CONCERN-8 startup
  reconciliation). Then checks tmux pane list for surviving agent sessions, resumes pipeline.
- Heartbeat: Conductor writes `state/signals/orchestrator/heartbeat.json` every 30 seconds.
  clawhip watches this and alerts Discord if stale (>2 minutes).

---

## Agent Role Definition Format

*Format unchanged from v3.* All agent roles are defined as markdown files in `config/agent_roles/`
with YAML frontmatter and XML-structured prompt body. The `mode` field is `claude-code` for all
roles (no `api` mode in v4):

```yaml
---
name: executor
description: Zone-scoped code implementation
model: claude-opus-4-6
tier: HIGH
mode: claude-code          # Agent session roles are "claude-code" (Conductor uses "claude-p")
isolation: worktree        # Conductor creates worktree, Executor works within it
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
       then EXIT. The Conductor will review your work, get Architect
       feedback, and spawn a fresh session to continue from the next unit.
    8. Next unit
    On final unit: write `.executor/completion.json`
    If blocked: write `.executor/blocked.json` and EXIT. The Conductor
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

**Architect and Reviewer signal conventions:**
Unlike v3 where the orchestrator parsed API responses directly, v4's Architect and Reviewer are
Claude Code sessions that write signal files on completion:
- **Architect** writes `state/signals/architect/<task-id>/plan.json` (plan file path, checkpoint
  units) or `state/signals/architect/<task-id>/review.json` (verdict, notes/instructions)
- **Reviewer** writes `state/signals/reviewer/<task-id>/verdict.json` (approved/feedback,
  file:line citations)
- **Executor** writes within its worktree: `.executor/checkpoint.json`,
  `.executor/completion.json`, or `.executor/blocked.json` (unchanged from v3)

**Signal file write mechanism for read-only roles:** Architect and Reviewer have
`disallowedTools: Write, Edit` to prevent them from modifying source code. They write signal
files via the Bash tool: `echo '{"verdict":"approved",...}' > state/signals/reviewer/<task-id>/verdict.json`.
This is explicitly documented in each role prompt. The Executor uses the Write tool normally
(it has full write access in its worktree).

The Conductor polls all these directories. Each agent exits after writing its signal file.

---

## Agent Safety Infrastructure

Two layers of safety: OMC provides session-internal safety (hooks inside each Claude Code
session), and the Conductor provides task-level safety (tmux lifecycle, signal file polling,
escalation). Both layers are OMC-native — no custom Python script.

### OMC-Provided (session-internal)

*Unchanged from v3.* These come from OMC's existing hook system, active in every agent session:

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
  Active for Executor, Architect, Reviewer, and Strategy Analyst sessions.
- **Subagent tracking** (`subagent-tracker.mjs`) — tracks all spawned agents with model, tokens,
  duration. $1 cost limit per subagent. Stale agent detection (>5 min without progress).
- **Anti-duplication** (injected into Conductor prompts) — "DO NOT perform the same search
  yourself after delegating."

### Conductor-Provided (task-level, replaces v3's orchestrator-provided)

These are managed by the Conductor wrapper (`tools/conductor.sh`), operating across agent
sessions via tmux commands and signal file polling:

- **Git worktree isolation** — Conductor creates worktrees via Bash commands (same `git worktree`
  pattern as v3). Full lifecycle:
  1. `git worktree add .worktrees/<task-id> -b feature/<task-id>` (via Bash)
  2. Write plan + zone file + worktree-specific CLAUDE.md into worktree (via bash heredoc/cat)
  3. Create `<worktree>/.executor/` directory (via Bash)
  4. Spawn Executor Claude Code session in a tmux pane:
     `tmux split-window -t ozymandias -d "claude -p '...' --cwd .worktrees/<task-id>"`
  5. On completion: Executor commits to feature branch (within its session)
  6. Conductor detects completion signal, runs `git diff main...feature/<task-id>` (via Bash)
  7. Conductor spawns Reviewer session with diff + test context
  8. On approval: Conductor merges branch, runs full test suite, cleans up worktree (via Bash)
  9. On failure/timeout: preserve worktree for debugging
  10. On post-merge test failure: revert merge commit, escalate to operator

- **Worktree signal polling** — Conductor polls `<worktree>/.executor/` for all active
  worktrees (paths tracked in `state/orchestrator_state.json`). Also polls
  `state/signals/architect/` and `state/signals/reviewer/`. Poll interval: 5-10 seconds.
  Detected signals are routed: Executor checkpoints → spawn Architect review session,
  Executor blocked → spawn Architect session, Executor completion → spawn Reviewer session,
  Architect completion → spawn next pipeline step.

- **Per-task wall-clock tracking** (CONCERN-9a) — Claude Code sessions don't expose token
  counts programmatically. Budget enforcement uses **wall-clock timeout**, not token tracking.
  Executor writes `wall_clock_seconds` (not `cumulative_tokens`) to zone file after each unit.
  Conductor reads zone files to track elapsed time. Default timeouts: quick (15 min), standard
  (30 min), deep (60 min), complex (90 min). Alert at 80% timeout via signal file → clawhip →
  Discord. Kill session at 100% via `tmux kill-pane` if no human override. Per-cycle cost
  estimates in the cost table are unverifiable projections, not tracked actuals.

- **Task-level failure escalation** — Conductor tracks failure count per task in
  `state/orchestrator_state.json`. 3 failures: Conductor includes "try a different approach" in
  the next Executor's CLAUDE.md context. 5 failures: Conductor writes escalation signal for
  Discord. (Complements OMC's tool-level tracking within each agent session.)

- **Executor wall-clock timeout** — 60-minute default per task. Conductor tracks spawn time in
  `state/orchestrator_state.json`. On timeout: `tmux kill-pane -t <pane-id>`, preserve worktree
  for debugging, escalate to operator. (Same mechanism as v3 — tmux kill is available.)

- **Task deduplication** — same logic as v3. Before accepting a new task, Conductor hashes
  `TASK` + `zone` fields and checks against `state/orchestrator_state.json`.

- **Stale state detection** — Conductor ignores state files older than 2 hours. clawhip emits
  warning on stale state detection. Same as v3.

- **Task backpressure** (CONCERN-9d) — three layered defenses against stale task accumulation:
  1. *Reproduction gate:* Before calling Architect, Conductor re-runs the bug report's
     reproduction test (if provided). If it passes, auto-close as `resolved_before_processing`.
     Catches the "9AM bug fixed by 2PM" scenario. Skip for `source: human` tasks.
  2. *TTL per task type:* Bug reports 2h, strategy findings 8h, human tasks no TTL. Conductor
     checks file age at dequeue.
  3. *Ops Monitor rate limit:* Cap at 3 bug reports per rolling hour (prevents cascade from a
     single root cause generating many reports).

- **Worktree leak prevention** (CONCERN-9e) — TTL + startup sweep + max count cap:
  1. 48-hour TTL on failed/timed-out worktrees (timestamp in `orchestrator_state.json`).
  2. On Conductor startup: sweep removes worktrees past TTL.
  3. Max-5 cap on preserved worktrees — oldest removed if new failure exceeds cap.
  4. Git branch survives cleanup — `git log`/`git diff`/`git show` recover all committed state.
     Uncommitted changes are the only loss (acceptable: Executors commit before signaling).

- **Staging directory cleanup** — Conductor deletes staging directories
  (`state/staging/architect-<task-id>/`, etc.) after the agent session exits and its signal is
  processed. On retry: Conductor overwrites the staging directory (idempotent). Stale staging
  dirs older than 24h are swept on startup.

- **Parallel Executor protocol** — when the Architect decomposes a task into multiple zones,
  the Conductor can spawn parallel Executors:
  1. *Zone claim locks:* Each Executor writes `state/agent_claims/<zone>.lock` with task-id and
     timestamp before working. Another Executor checking the same zone skips it.
  2. *Disjoint file ownership:* Architect assigns disjoint file sets per zone at planning time.
     Executors receive their zone file listing which files they can touch — anything outside is
     a violation. This prevents merge conflicts by design, not resolution.
  3. *Merge serialization:* Conductor merges completed branches one at a time (single-threaded
     polling loop is the implicit queue). No two merges in flight.
  4. *Rebase-and-retest before second merge:* After task A merges, before merging task B:
     Conductor rebases B on new main (`git rebase main` in worktree via Bash), re-runs full
     test suite. If tests fail post-rebase, route to Architect for re-planning — do not
     auto-merge. This catches semantic conflicts that git merge cannot detect.
  5. *Concurrency limit:* Start with max 2 parallel Executors. Increase after sequential
     pipeline is proven reliable.
  6. *Claim lock cleanup:* Locks deleted on completion or after 30-minute soft timeout.
     Orphaned locks (agent crashed) moved to `<zone>.lock.orphaned` for inspection.

### Debugging & Observability

The agentic workflow is itself an experimental system. During development and early operation,
the operator must be able to reconstruct what happened, why, and where things went wrong —
both for live debugging and post-session review. This section defines what gets logged, where
it goes, and how to use it.

**Design constraint:** Debugging output must not pollute agent context windows. All logging is
to disk. Agents never read their own debug logs — they read zone files, signal files, and
task packets. Debug logs are for the human operator and the Ops Monitor only.

**1. Wrapper event log** (`state/logs/conductor.log`):
The wrapper appends one structured line per event. Machine-parseable (JSON lines), human-
readable with `jq`. This is the primary debugging artifact for the entire pipeline.

```jsonl
{"ts":"2026-04-08T14:32:01Z","event":"signal_detected","type":"checkpoint","task_id":"fix-rvol","source":".worktrees/fix-rvol/.executor/checkpoint.json"}
{"ts":"2026-04-08T14:32:02Z","event":"agent_spawn","role":"architect","task_id":"fix-rvol","pane":"ozymandias:0.7","staging_dir":"state/staging/architect-review-fix-rvol/"}
{"ts":"2026-04-08T14:35:18Z","event":"agent_exit","role":"architect","task_id":"fix-rvol","pane":"ozymandias:0.7","exit_code":0,"wall_clock_s":196}
{"ts":"2026-04-08T14:35:19Z","event":"judgment_call","reason":"task_classify","task_id":"fix-rvol","model":"sonnet","prompt_bytes":2340}
{"ts":"2026-04-08T14:35:22Z","event":"judgment_result","task_id":"fix-rvol","decision":{"action":"spawn_executor","priority":"bug"}}
{"ts":"2026-04-08T14:50:01Z","event":"merge","task_id":"fix-rvol","branch":"feature/fix-rvol","merge_sha":"a1b2c3d","test_result":"pass"}
{"ts":"2026-04-08T14:50:02Z","event":"error","task_id":"fix-rvol","detail":"post-merge test failure in test_orchestrator.py","action":"revert","revert_sha":"a1b2c3d"}
```

Event types: `signal_detected`, `agent_spawn`, `agent_exit`, `judgment_call`, `judgment_result`,
`merge`, `revert`, `timeout`, `error`, `escalation`, `heartbeat`, `startup`, `shutdown`,
`worktree_create`, `worktree_cleanup`, `poll_cycle` (emitted every 60s, not every 10s — avoids
log bloat while proving the wrapper is alive).

**2. Agent session capture** (`state/logs/agents/<role>-<task-id>.log`):
Each agent tmux pane's output is captured via `tmux pipe-pane`. This gives full scrollback of
what the agent did — tool calls, edits, test runs, errors — without relying on the agent to
self-report. The wrapper starts capture on spawn, stops on exit.

```bash
# In the wrapper, after spawning an agent pane:
tmux pipe-pane -t "$pane_id" -o "cat >> state/logs/agents/${role}-${task_id}.log"
```

These logs are append-only, unstructured (raw terminal output), and can be large. They are the
"flight recorder" — you don't read them unless something went wrong. Retention: 7 days, then
compressed to `.gz`. The wrapper does not parse these — they exist for human review only.

**3. Signal file preservation** (`state/logs/signals/<task-id>/`):
Before the wrapper deletes a processed signal file, it copies it to the per-task log directory.
This creates a complete audit trail of every signal in the order processed. Each file is
timestamped on copy (e.g., `001-checkpoint.json`, `002-architect-review.json`,
`003-complete.json`, `004-approved.json`). Cost: negligible (small JSON files). Value: lets you
replay the exact sequence of events for any task without reconstructing from the event log.

**4. Judgment call recording** (`state/logs/judgments/<task-id>/`):
Each `claude -p` invocation saves its input and output:
- `<sequence>-input.json` — the prompt and context passed to `claude -p`
- `<sequence>-output.json` — the JSON decision returned

This is critical during development — when the LLM makes a bad classification or routing
decision, you can see exactly what it saw and what it decided. Without this, debugging
judgment errors requires reproducing the exact state that triggered the call, which is
fragile and time-consuming.

**5. Zone file snapshots** (in zone file itself):
Zone files already track `units_completed`, `unit_in_progress`, `units_remaining`,
`wall_clock_seconds`. Add one field: `history` — an append-only array of state transitions:

```json
{
  "history": [
    {"ts": "2026-04-08T14:00:00Z", "transition": "started", "unit": 1},
    {"ts": "2026-04-08T14:12:30Z", "transition": "completed", "unit": 1},
    {"ts": "2026-04-08T14:12:31Z", "transition": "started", "unit": 2},
    {"ts": "2026-04-08T14:18:45Z", "transition": "checkpoint", "unit": 2},
    {"ts": "2026-04-08T14:22:00Z", "transition": "resumed", "unit": 2, "fresh_session": true},
    {"ts": "2026-04-08T14:35:00Z", "transition": "completed", "unit": 2}
  ]
}
```

The Executor writes these transitions (it already writes zone file updates). The wrapper
reads them for timeout tracking and post-task review. This answers: "How long did each unit
take? Where did the Executor stall? How many checkpoint cycles did it need?"

**6. Post-task summary** (`state/logs/summaries/<task-id>.json`):
After a task completes (merged or failed), the wrapper writes a summary:

```json
{
  "task_id": "fix-rvol",
  "status": "completed",
  "total_wall_clock_s": 1830,
  "agent_sessions": [
    {"role": "architect", "wall_clock_s": 120, "exit_code": 0},
    {"role": "executor", "wall_clock_s": 900, "exit_code": 0, "checkpoints": 1},
    {"role": "architect-review", "wall_clock_s": 196, "exit_code": 0},
    {"role": "executor", "wall_clock_s": 480, "exit_code": 0, "checkpoints": 0},
    {"role": "reviewer", "wall_clock_s": 134, "exit_code": 0}
  ],
  "judgment_calls": 3,
  "merge_sha": "a1b2c3d",
  "branch": "feature/fix-rvol",
  "files_changed": 4,
  "test_result": "pass"
}
```

This is the high-level "what happened" for each task — readable at a glance, without digging
into event logs. The Ops Monitor can read these for daily summaries. The operator can scan them
to spot patterns (e.g., tasks consistently needing 3+ checkpoint cycles = Architect plans are
too coarse).

**How to use these during workflow development:**

| Question | Where to look |
|----------|--------------|
| "Is the wrapper polling correctly?" | `conductor.log` — check `poll_cycle` events every ~60s |
| "Why did this task fail?" | `summaries/<task-id>.json` for overview, then `conductor.log` filtered by task_id |
| "What did the Executor actually do?" | `agents/executor-<task-id>.log` (raw tmux scrollback) |
| "Why did the wrapper make that routing decision?" | `judgments/<task-id>/` — see exact input/output of the `claude -p` call |
| "What signals were exchanged?" | `signals/<task-id>/` — preserved copies in order |
| "Where did the Executor stall?" | Zone file `history` array — look for long gaps between transitions |
| "Is the workflow getting slower over time?" | `summaries/` — compare `total_wall_clock_s` across tasks |
| "Did the agent session crash or exit cleanly?" | `conductor.log` `agent_exit` events — check `exit_code` |

**Log directory structure:**
```
state/logs/
├── conductor.log              # Wrapper event log (JSONL, primary debug artifact)
├── agents/                    # Raw tmux scrollback per agent session
│   ├── architect-fix-rvol.log
│   ├── executor-fix-rvol.log
│   └── reviewer-fix-rvol.log
├── signals/                   # Preserved signal files per task
│   └── fix-rvol/
│       ├── 001-checkpoint.json
│       ├── 002-architect-review.json
│       └── 003-approved.json
├── judgments/                  # claude -p input/output per task
│   └── fix-rvol/
│       ├── 001-input.json
│       └── 001-output.json
└── summaries/                 # Post-task summaries
    └── fix-rvol.json
```

**Retention:** `conductor.log` rotated daily (keep 14 days). `agents/` compressed after 7 days
(keep 30 days). `signals/`, `judgments/`, `summaries/` kept indefinitely (small files).
Wrapper handles rotation on startup — no external cron needed.

### Self-Modification Safety

The dev pipeline can modify its own infrastructure — the wrapper, agent role definitions,
OMC hooks, signal schemas. This is a self-referential hazard: modifying a running system's
code while it runs. The core rule: **pipeline infrastructure changes require a clean exit
and restart, never hot-patching.**

**File classification:**

| Category | Examples | Safe to modify while wrapper runs? |
|----------|---------|-----------------------------------|
| Bot code | `ozymandias/**`, `config/prompts/**` | Yes — wrapper doesn't touch these |
| Agent role definitions | `config/agent_roles/*.md` | Yes — loaded fresh per agent spawn |
| OMC hooks | `.claude/hooks/**` | Partially — new sessions pick up changes, running sessions don't |
| Pipeline infrastructure | `tools/conductor.sh`, signal dir structure, `orchestrator_state.json` schema | **No — requires wrapper restart** |

**Exit intent protocol (state on disk, not exit codes):**

Exit codes are unreliable — SIGKILL, OOM, `kill -9`, tmux pane death produce no code. Bash
syntax errors exit with code 2, which could be misinterpreted. The wrapper writes its intent
to a file *before* exiting. The outer loop reads the file, not the exit code.

```bash
# In conductor.sh, before any planned exit:
write_exit_intent() {
  echo "{\"action\":\"$1\",\"reason\":\"$2\",\"ts\":\"$(date -Iseconds)\"}" \
    > state/signals/conductor/exit_intent.json
}

# Example: self-modification detected
write_exit_intent "restart" "self_mod_detected"
exit 0

# Example: operator requested shutdown
write_exit_intent "shutdown" "discord_command"
exit 0
```

**Outer restart loop** (runs in tmux pane 5, wraps `tools/conductor.sh`):

```bash
#!/bin/bash
# tools/start_conductor.sh — outer loop with intent file dispatch
INTENT_FILE="state/signals/conductor/exit_intent.json"

while true; do
  # Clean stale intent file BEFORE starting wrapper.
  # Disk persists — a leftover from a previous crash would be misread as fresh.
  rm -f "$INTENT_FILE"

  bash tools/conductor.sh
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
      echo "{\"type\":\"conductor_crash\",\"reason\":\"$reason\",\"ts\":\"$ts\"}" \
        > state/signals/alerts/conductor_crash.json
      break
      ;;
  esac
done
```

**Key invariant:** No intent file = the wrapper didn't get a chance to write one = unclean
death = alert the operator. Every planned exit (restart, shutdown) writes the intent file
first. The outer loop never guesses.

**Automatic restart on self-modification:**
After every merge, the wrapper checks whether the merged diff touches pipeline infrastructure:

```bash
# In the wrapper, after successful merge:
if git diff --name-only HEAD~1 HEAD | grep -qE '^tools/conductor\.sh|^config/agent_roles/conductor\.md'; then
  log_event "self_mod_detected" "Pipeline infrastructure changed, restarting"
  exit 0  # EXIT_RESTART — outer loop restarts with new code
fi
```

This is safe because the merge is already committed. The outer loop restarts the wrapper,
which reads the new `conductor.sh` from disk. No in-flight state is lost — all task state is
in `orchestrator_state.json` and zone files. The restarted wrapper reconciles state on startup
(git-state reconciliation, same as crash recovery).

**Manual restart from Discord:**
Operator types `!restart-conductor` → Discord companion writes
`state/signals/conductor/restart.json` → wrapper detects on next poll (≤10s) → wrapper
finishes any in-progress mechanical operation (never exits mid-merge) → `exit 0` →
outer loop restarts.

**Manual shutdown from Discord:**
Operator types `!shutdown-conductor` → same signal path → wrapper finishes in-progress
work → `exit 2` → outer loop exits.

**What the Architect must know:** When decomposing a task that touches pipeline infrastructure,
the Architect's plan must note: "This task modifies pipeline infrastructure. The wrapper will
automatically restart after merge. Verify the restart was clean (check `conductor.log` for
`startup` event after `self_mod_detected`)." This goes in the Architect role prompt.

### Discord Companion (user-facing)

*Mostly unchanged from v3.* New commands for pipeline control:
- `!restart-conductor` — writes `state/signals/conductor/restart.json`, wrapper restarts
  cleanly on next poll cycle. See § Self-Modification Safety.
- `!shutdown-conductor` — writes `state/signals/conductor/shutdown.json`, wrapper writes
  `shutdown` intent file and exits, outer loop stops. Executor sessions continue independently.
- **Informational intent filtering** — before dispatching a command, check for patterns (`what is`,
  `how does`, `explain`). "What does !status do?" should not trigger the status command.

---

## Structured Task Packet Format

*Unchanged from v3.* When the Conductor delegates work to any agent, it writes a structured
6-section task packet:

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
the agent solving a different problem than intended.

---

## Intent Classification Gate

*Unchanged from v3.* Before planning, the Architect classifies each incoming task:

| Classification | Description | Decomposition strategy |
|---------------|-------------|----------------------|
| Bug fix | Known broken behavior, reproducer exists | Minimal change, focused scope, no new tests beyond regression |
| Calibration | Parameter tuning, prompt edit | Config or prompt file change only, no structural changes |
| Feature | New capability | Full plan with zones, checkpoints, and acceptance tests |
| Refactor | Structural improvement, no behavior change | Plan with migration strategy, before/after tests must be identical |
| Analysis | Investigation, no code change | Read-only, output is a document, not code |

---

## Pressure-Testing Protocol

*Unchanged from v3.* Each agent role has a mandatory adversarial check matched to its primary
failure mode.

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
| Conductor (`claude -p`) | Simplifier | Over-complex task routing, unnecessary agent spawns | 0.15 (embedded in judgment prompt) |

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

*Protocol unchanged from v3, mechanism adapted for Conductor + tmux sessions.*

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

### Checkpoint protocol (exit-and-respawn via tmux)

Same exit-and-respawn pattern as v3. Zone files carry all state. Checkpoints use signal file
polling instead of API calls.

1. Executor completes the checkpoint unit, commits to feature branch in its worktree
2. Executor updates zone file with checkpoint status and writes
   `<worktree>/.executor/checkpoint.json` with: unit completed, approach taken, ambiguities
   encountered, test status
3. **Executor exits.** The tmux pane closes. Zone file + git branch carry all state.
4. Conductor detects checkpoint signal (polls `<worktree>/.executor/` every 5-10 seconds),
   assembles Architect review context: reads zone file, runs `git diff main...feature/<task-id>`
   and `pytest` (via Bash)
5. Conductor writes review context to a staging directory for the Architect:
   `state/staging/architect-review-<task-id>/CLAUDE.md` containing the Architect role prompt +
   plan + diff + test output + task packet
6. Conductor spawns Architect review session in a tmux pane:
   `tmux split-window -t ozymandias -d "claude -p '...' --cwd <staging-dir>"`
7. Architect reads context, evaluates, writes verdict to
   `state/signals/architect/<task-id>/review.json`:
   - `{"verdict": "proceed", "notes": "..."}`
   - `{"verdict": "course-correct", "instructions": "..."}`
8. Architect session exits. Conductor detects verdict signal (polls).
9. Conductor writes Architect response to `<worktree>/.executor/architect_response.json`
10. **Conductor spawns a fresh Executor** in the same worktree:
    `tmux split-window -t ozymandias -d "claude -p 'Continue from unit N. Read
    architect_response.json for review feedback, then read your zone file for state.'
    --cwd .worktrees/<task-id>"`
11. Conductor writes checkpoint event to `state/signals/executor/<task-id>/checkpoint.json`
    in the main repo for clawhip → Discord notification

**Tradeoff:** Same as v3 — loses in-session mental model (files read, approaches tried).
Mitigated by zone file + git commits + Architect response. Over a 5-checkpoint plan, fresh
context per segment dominates vs. idle-polling degradation at Opus cost.

**v4 advantage over v3:** The Architect review session has full codebase access. It can browse
related files, run grep, check test coverage — not just evaluate an assembled context packet.
This produces higher-quality review at the cost of higher per-review token usage.

**Signal file convention — outbox + gateway pattern (CONCERN-8 resolution):**
- The Executor writes signals **within its own worktree** (`<worktree>/.executor/`). This is
  a sandboxed **outbox**. It never writes outside its working directory.
- The Architect and Reviewer write signals to `state/signals/architect/<task-id>/` and
  `state/signals/reviewer/<task-id>/` in the main repo. Task-id scoping prevents cross-wiring.
- The Conductor is the **signal gateway**: it polls all four outbox namespaces
  (`state/agent_tasks/`, `<worktree>/.executor/`, `state/signals/architect/`,
  `state/signals/reviewer/`) and routes events to the shared bus or to the next pipeline step.
- clawhip watches `state/signals/` in the main repo only. It never touches worktrees.
- **Phantom completion defense (claw-code §7.4):** Every signal file includes a `task_id` field
  inside its JSON, not just in its directory path. The Conductor validates that the `task_id`
  field matches the expected task before acting. This prevents cross-wiring if an LLM parsing
  `ls` output confuses task IDs. Same pattern as claw-code's session_id + project_path scoping.

### Mid-unit escalation

Between checkpoints, the Executor can escalate if it hits something the plan didn't anticipate:
- Executor writes `<worktree>/.executor/blocked.json` with the ambiguity description, then exits
- Conductor detects blocked signal, spawns Architect session with the ambiguity context
- Architect response written to `<worktree>/.executor/architect_response.json`
- Conductor spawns fresh Executor in the same worktree
- If Architect can't resolve → Conductor writes to main repo signal bus → clawhip pings
  Operator on Discord

---

## Verification Tiers

*Unchanged from v3.* Reviewer effort is sized by change scope:

| Change scope | Verification tier | Reviewer behavior |
|-------------|-------------------|-------------------|
| ≤2 files changed | Light | Quick convention check, test pass confirmation |
| 3-10 files | Standard | Full diff review, interaction analysis, convention check |
| >10 files | Thorough | Comprehensive audit, cross-zone impact analysis, explicit approval per changed module |

**v4 advantage:** The Reviewer is a Claude Code session with full codebase access. It can run
`pytest` directly, grep for cross-references, and verify behavioral claims — not just review
a pre-assembled diff packet.

### Structured completion evidence

*Unchanged from v3.*

| Agent | Required evidence |
|-------|------------------|
| Architect | Plan file exists with non-goals, decision boundaries, and checkpoint markers |
| Executor | All tests pass, branch pushed, zone file shows all units complete |
| Reviewer | Approval with file:line citations for every finding, or structured feedback |
| Strategy Analyst | Categorized findings with signal citations at decision time |
| Ops Monitor | Bug report with reproduction steps and severity classification |

---

## Two Systems, Shared Bus

```
System 1: Bot Operations (always running during market hours)
├── Trading bot process (existing)
├── clawhip daemon (watches signals + git, routes to/from Discord)
├── Ops Monitor (persistent Haiku Claude Code session in tmux pane 3)
├── Conductor wrapper (bash, tools/conductor.sh, tmux pane 5)
└── Strategy Analyst (post-market Sonnet Claude Code session, spawned by Conductor)

System 2: Strategy Dialogue (on-demand, operator-initiated)
└── Dialogue agent (Claude Code on Max plan in tmux pane 4, bridged to Discord #strategy)
    - Thinking partner for the operator, not a gatekeeper
    - Reads everything: analyses, docs, journal, code, signals
    - Uses all three pressure-testing personas before crystallizing plans
    - Enforces readiness gates (non-goals + decision boundaries)
    - Zero API cost (Max plan)

Development Pipeline (on-demand or bug-triggered, managed by Conductor)
├── Architect (Opus Claude Code session — reads docs + task + code, outputs plan)
├── Executor(s) (Opus Claude Code session — zone-scoped, ephemeral, in git worktrees)
└── Reviewer (Sonnet Claude Code session — reads diff, runs tests, outputs verdict)
    All spawned by Conductor via tmux commands. Each in its own pane.
    Communication via signal files. Conductor polls for signals.

Shared: state/signals/ (signal file bus), Discord (human interface)
```

**Operator retains direct access to all layers.** Dialogue does not gate or mediate access to the
development pipeline. The operator can talk directly to dev agents (by selecting their tmux pane),
give commands, interrupt work, and course-correct mid-implementation.

**Flow for larger-than-bug work:**
```
Strategy Analyst writes findings → Operator reads in Discord
  → Operator opens #strategy → Operator ↔ Dialogue discuss
  → Dialogue pressure-tests (Contrarian/Simplifier/Ontologist)
  → Dialogue enforces readiness gates (non-goals, decision boundaries)
  → Operator says "write the plan" → Dialogue saves plan file + task directive
  → Conductor detects task → spawns Architect session (Opus, tmux pane)
  → Architect reads codebase, writes plan with checkpoints, writes signal, exits
  → Conductor detects Architect signal → creates worktree(s) → spawns Executor(s)
  → Executor pauses at checkpoint, writes signal, exits
  → Conductor detects checkpoint → spawns Architect review session
  → Executor completes → Conductor spawns Reviewer session (Sonnet, tmux pane)
  → Reviewer reads diff, runs tests, writes verdict signal, exits
  → Conductor detects approval → merges branch (via Bash)
  → Conductor writes lifecycle events to signal files → clawhip posts to Discord
```

**Flow for bugs and quick fixes:**
```
Ops Monitor writes bug report to state/agent_tasks/
  → Conductor detects task → spawns Architect session → minimal plan
  → Conductor spawns Executor session → fix → spawns Reviewer → approves → merge
  OR: Operator posts !fix <description> in Discord → companion writes task → same pipeline
```

**Idle work:** When no human-directed or bug tasks are pending, the Conductor checks for
`type: strategy_analysis` tasks from the Strategy Analyst. These are lower priority and yield
immediately if a higher-priority task arrives.

---

## Phase Breakdown

Each phase is a self-contained deliverable. Phases are ordered by dependency.
v4 replaces Phase E (orchestrator script) with a Conductor role definition and modifies Phase F.

---

### Phase A — Signal File API + Bot Event Emitter

*Unchanged from v3.*

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
- Signal file schemas must be machine-readable by ops agents and the Conductor in later phases.

**Verification:** Bot runs normally with signal files being written. Manual inspection confirms
files are well-formed. `touch state/PAUSE_ENTRIES` pauses entries; removing it resumes.

---

### Phase B — clawhip + Discord Companion

*Unchanged from v3.*

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

*Unchanged from v3.*

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

*Unchanged from v3.*

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
- **Structured daily summary** (CONCERN-9c): Ops Monitor maintains
  `state/ops_daily_summary.json` with rolling anomaly counts, pattern timestamps, and trend
  flags — updated every cycle, read back after compaction. Decouples pattern memory from
  conversation memory entirely. PreCompact hook injects one-line reminder: "Read
  ops_daily_summary.json for pattern history." Poll frequency reduced from 60s to 2-3 minutes
  (same detection quality for multi-minute anomalies, longer between compactions).
- **Bug report rate limit** (CONCERN-9d): cap at 3 bug reports per rolling hour. Prevents
  cascade from a single root cause generating many reports.

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

*Minor change from v3 — implementation is a Claude Code session instead of API call.*

**Goal:** Post-market analysis that reads the trade journal, categorizes outcomes, and writes
structured analysis feeding into the development backlog.

**What gets built:**
- Agent role definition file: `config/agent_roles/strategy_analyst.md`
- **Conductor triggers post-market** (after session close signal). Spawns a Claude Code Analyst
  session in an ephemeral tmux pane with a staging directory containing the Analyst role prompt +
  relevant context (trade journal, NOTES.md, watchlist state, findings log).
- **v4 advantage over v3:** The Analyst has full filesystem access. It can read the trade journal
  directly, grep for related patterns, check indicator histories — not just evaluate an assembled
  context packet. The staging CLAUDE.md points the Analyst to relevant files rather than including
  all content inline.
- Ontologist pressure-test in role prompt: before reporting a finding, check "Is this actually
  new, or a known behavior I'm re-discovering?" by cross-referencing NOTES.md content
  AND the findings log
- Four-category outcome classification for each trade:
  - **Signal present, bot ignored** — TA signals indicated correct action, bot's gates filtered
  - **Signal present, bot saw but filtered** — bot detected signal, filter blocked it
  - **Signal ambiguous, reasonable to miss** — no clear signal at decision time
  - **Truly unforeseeable** — external event with no precursor signals
- Same analysis for missed opportunities (watchlist symbols that moved but weren't entered)
- Output: Analyst writes structured analysis JSON to `state/signals/analyst/<date>/findings.json`,
  then exits. Conductor detects signal, writes tasks to `state/agent_tasks/` tagged
  `type: strategy_analysis`.
- **Findings log** (`state/analyst_findings_log.json`): Conductor appends each processed
  finding with status (`queued`, `completed`, `dismissed`) and date. The Analyst sees this log
  in context so it doesn't re-discover issues that have already been acted on or dismissed.

**Hindsight bias prevention:** Each finding must cite the specific signal or indicator value that
existed at decision time. "NKE rallied 3%" is not a finding. "NKE rallied 3% — BB squeeze was
firing at entry time with RSI 22, oversold bounce was predictable from existing signals" is.

**Verification:** Run against existing trade journal (68 trades). Categorized findings with
signal citations. Manual review confirms hindsight discipline.

---

### Phase E — Conductor Wrapper + Task Format (replaces v3's "Orchestrator Script")

**Goal:** Build the deterministic conductor wrapper (`tools/conductor.sh`, ~50-80 lines bash),
define the Conductor judgment prompt, task format, staging directory conventions, and lifecycle
management behavior. The wrapper owns all mechanical operations; `claude -p` is invoked
on-demand for judgment calls only.

**What gets built:**

1. **Conductor wrapper** (`tools/conductor.sh`, ~50-80 lines bash): Polling loop, signal
   scanning, state I/O with `jq`, git operations, tmux lifecycle, timeout enforcement,
   heartbeat, and all logging infrastructure (see § Debugging & Observability). Sequential-
   first — one task at a time through the full pipeline. Line count grows from the original
   ~30-50 estimate to ~50-80 due to logging, but remains trivially debuggable.

2. **Conductor judgment prompt** (`config/agent_roles/conductor.md`): System prompt for
   on-demand `claude -p` invocations. Used when the wrapper detects a signal requiring
   judgment (task classification, context assembly, failure diagnosis).

3. **Logging infrastructure** (built into the wrapper, not a separate component):
   - Event log (`state/logs/conductor.log`) — JSONL, one line per event
   - Agent session capture (`state/logs/agents/`) — `tmux pipe-pane` per spawn
   - Signal file preservation (`state/logs/signals/<task-id>/`) — copy before delete
   - Judgment call recording (`state/logs/judgments/<task-id>/`) — input/output per `claude -p`
   - Post-task summaries (`state/logs/summaries/`) — written on task completion
   - Log rotation on startup (daily conductor.log, 7-day agent compression)
   See § Debugging & Observability for full specification.

```yaml
---
name: conductor
description: Development pipeline judgment prompt (invoked by wrapper)
model: sonnet                 # On-demand, not persistent — cost per invocation
tier: MEDIUM
mode: claude-p
output: json
---
<Agent_Prompt>
  <Role>
    You are the Conductor. You coordinate the development pipeline by
    spawning Architect, Executor, and Reviewer Claude Code sessions in
    tmux panes. You manage git worktrees, task lifecycle, checkpoint
    protocol, and failure escalation. You do not write code yourself.
  </Role>
  <Task_Operations>
    (CONCERN-6 cluster 1: pure data operations on state/agent_tasks/)
    - Detect new tasks in state/agent_tasks/ (poll or external nudge)
    - Read task packet, classify priority (human > bug > strategy_analysis > backlog)
    - Check deduplication: hash TASK + zone, compare against orchestrator_state.json
    - Backpressure checks (CONCERN-9d):
      * Reproduction gate: re-run bug repro test before Architect. Auto-close if passes.
      * TTL check: bug reports 2h, strategy findings 8h, human tasks no TTL.
    - Update orchestrator_state.json after each state change (atomic write)
  </Task_Operations>
  <Worktree_Operations>
    (CONCERN-6 cluster 2: git/tmux operations)
    - Create git worktree: git worktree add .worktrees/<task-id> -b feature/<task-id>
    - Write plan + zone file + Executor CLAUDE.md into worktree
    - Write staging dirs for Architect/Reviewer with role CLAUDE.md + context
    - Spawn agent sessions: tmux split-window + claude -p --cwd <dir>
    - Kill agent panes on timeout: tmux kill-pane -t <pane-id>
    - Cleanup: delete staging dirs after signal processed, worktree after merge
    - Worktree leak prevention (CONCERN-9e): 48h TTL, startup sweep, max-5 cap
  </Worktree_Operations>
  <Pipeline_Sequencing>
    (Owned by the wrapper — this section documents the pipeline the wrapper implements.
     The LLM is invoked at steps marked [judgment] for context assembly and classification.)
    Per task:
    1. [judgment] New task detected → invoke claude -p to classify priority, check backpressure
    2. [wrapper] Spawn Architect session → poll state/signals/architect/<task-id>/ for plan
    3. [wrapper] Create worktree → spawn Executor → poll <worktree>/.executor/ for signals
    4. [wrapper] On checkpoint: spawn Architect review session → detect response → fresh Executor
    5. [wrapper] On complete: spawn Reviewer → poll state/signals/reviewer/<task-id>/ for verdict
    6. [wrapper] On approval: rebase on current main, re-run tests, merge (capture SHA),
       post-merge tests, cleanup worktree. Revert on failure: git revert --no-edit <merge-sha>
    7. [wrapper] Write lifecycle events to state/signals/ for clawhip
    8. [judgment] On 3+ failures → invoke claude -p to diagnose, decide re-plan vs escalate
    Sequential-first: one task at a time. Parallel support added after sequential is proven.
  </Pipeline_Sequencing>
  <Invocation_Context>
    You are invoked by the conductor wrapper (tools/conductor.sh) via `claude -p` when a
    signal requires judgment. You are NOT a persistent session — each invocation is fresh.
    The wrapper passes you: the signal file contents, current orchestrator_state.json, and
    any relevant task/zone files. You respond with a structured JSON decision, then exit.
    The wrapper handles all mechanical operations (polling, git, tmux, state writes).
    Do not attempt to poll, loop, spawn agents, or write state files — the wrapper does that.
  </Invocation_Context>
  <Failure_Handling>
    - 3 failures in a task: spawn Architect session for re-planning (OmO Oracle pattern —
      read-only strategic advisor analyzes what went wrong). Do not just retry with "try
      different approach" text.
    - 5 failures: write escalation signal to state/signals/alerts/
    - Post-merge test failure: git revert --no-edit <merge-sha> (CONCERN-9b: explicit SHA)
    - Merge conflict: rebase + re-test, escalate to Architect if rebase fails
    - Agent timeout: tmux kill-pane, preserve worktree, escalate
  </Failure_Handling>
  <Wall_Clock_Discipline>
    (CONCERN-9a: token counts unavailable in Claude Code sessions)
    Budget enforcement is wall-clock only. The wrapper tracks start_time per agent session.
    On each poll cycle: elapsed = $(date +%s) - start_time. Alert at 80% via signal file.
    Kill session (tmux kill-pane) at 100% if no human override. Per-cycle cost estimates
    are projections, not tracked actuals. All timestamp math is in the wrapper — never in
    an LLM.
  </Wall_Clock_Discipline>
  <Constraints>
    - Never write code. You classify, diagnose, and assemble context.
    - Never self-review or self-approve work.
    - Your output is a JSON decision object. The wrapper acts on it.
    - You are stateless — all context comes from files the wrapper passes you.
    - Do not attempt to poll, loop, spawn agents, or write state files.
  </Constraints>
</Agent_Prompt>
```

**Staging directory convention:**
Each ephemeral agent (Architect, Reviewer, Strategy Analyst) gets a staging directory:
```
state/staging/
├── architect-<task-id>/
│   ├── CLAUDE.md          # Architect role prompt + task context
│   └── (reads main repo for code — staging dir is just for the role definition)
├── architect-review-<task-id>/
│   ├── CLAUDE.md          # Architect role prompt + diff + test output + checkpoint context
│   └── review_context.json
├── reviewer-<task-id>/
│   ├── CLAUDE.md          # Reviewer role prompt + diff + test output
│   └── review_context.json
└── analyst-<date>/
    └── CLAUDE.md          # Analyst role prompt + pointers to journal, NOTES, findings log
```

The Conductor creates these directories and writes the CLAUDE.md files before spawning each
agent. The staging CLAUDE.md includes:
- The agent's role definition (from `config/agent_roles/`)
- Task-specific context (task packet, diff output, test results)
- Trading domain rules from main CLAUDE.md
- `OMC_TEAM_WORKER=true` convention (for anti-spawn-loop hook)
- Pointers to relevant files in the main repo (the agent can read them directly)

**Conductor launch script** (`tools/start_conductor.sh`, ~20 lines):
The outer restart loop with exit code dispatch. See § Self-Modification Safety for the full
script. Launched in tmux pane 5:
```bash
tmux send-keys -t ozymandias:0.5 \
  "bash tools/start_conductor.sh" Enter
```

The wrapper reads `state/orchestrator_state.json` on startup for any in-progress tasks,
reconciles against actual git/tmux state, then enters its polling loop. On self-modification
or `!restart-conductor`, it exits cleanly and the outer loop restarts with new code. On crash,
the outer loop alerts via clawhip and stops. No Claude Code session runs persistently —
`claude -p` is invoked on-demand for judgment calls.

**Task packet schema:** *Identical to v3.* See § Structured Task Packet Format.

**Task lifecycle:** `pending → in_progress → checkpoint → in_progress → review →
completed | failed`

**Zone file schema:** *Extended from v3 with `history` field for debugging.*
```json
{
  "task_id": "<string>",
  "units_completed": [1, 2],
  "unit_in_progress": 3,
  "units_remaining": [4, 5],
  "test_status": "passing",
  "branch": "feature/fix-rvol-oscillation",
  "worktree_path": ".worktrees/fix-rvol-oscillation",
  "wall_clock_seconds": 1830,
  "last_updated": "<ISO timestamp>",
  "history": [
    {"ts": "<ISO>", "transition": "started", "unit": 1},
    {"ts": "<ISO>", "transition": "completed", "unit": 1},
    {"ts": "<ISO>", "transition": "checkpoint", "unit": 2},
    {"ts": "<ISO>", "transition": "resumed", "unit": 2, "fresh_session": true}
  ]
}
```
The `history` array is append-only, written by the Executor on each state transition.
The wrapper reads it for post-task review and debugging summaries (the wrapper's own clock
handles timeouts). See § Debugging & Observability.

**Worktree-specific CLAUDE.md:** The Conductor writes a custom CLAUDE.md into each worktree
that:
- Loads the Executor role definition
- Includes task packet and zone scope
- Includes trading domain rules from main CLAUDE.md
- Omits orchestration instructions (anti-spawn by omission)
- Sets `OMC_TEAM_WORKER=true` as an environment variable on the spawned process AND mentions
  it in the CLAUDE.md as belt-and-suspenders. The anti-spawn-loop hook checks `process.env`,
  so the env var is the mechanism; the prompt mention is defense-in-depth.

**Judgment call schemas** (input/output contracts for `claude -p` invocations):

Each `claude -p` call receives the Conductor role prompt as system context, plus a structured
JSON input on stdin. It returns a JSON decision object. The wrapper parses the output with `jq`.

*Task classification* (triggered when a new task appears in `state/agent_tasks/`):
```json
// Input
{
  "judgment": "classify_task",
  "task_file": "<contents of the task JSON>",
  "active_tasks": ["<task-ids currently in-progress>"],
  "orchestrator_state_summary": {"active_count": 1, "last_merge": "<ISO>"}
}
// Output
{
  "action": "accept" | "defer" | "reject",
  "priority": "human" | "bug" | "strategy_analysis" | "backlog",
  "reason": "<one-line explanation>",
  "reject_reason": "<only if action=reject — e.g., duplicate, stale TTL>"
}
```

*Context assembly* (triggered before spawning an Architect session):
```json
// Input
{
  "judgment": "assemble_context",
  "task": "<task packet JSON>",
  "zone_files": ["<list of files in the task's target zone>"],
  "recent_drift_log": "<last 20 lines of DRIFT_LOG.md>"
}
// Output
{
  "relevant_files": ["<file paths the Architect should see>"],
  "domain_context": "<paragraph of trading domain rules relevant to this task>",
  "known_concerns": ["<any open NOTES.md concerns affecting this area>"]
}
```

*Failure diagnosis* (triggered after 3+ consecutive failures on a task):
```json
// Input
{
  "judgment": "diagnose_failure",
  "task_id": "<string>",
  "zone_file": "<current zone file contents>",
  "failure_history": [
    {"attempt": 1, "error": "<summary>", "wall_clock_s": 420},
    {"attempt": 2, "error": "<summary>", "wall_clock_s": 380}
  ],
  "last_agent_log_tail": "<last 50 lines of executor log>"
}
// Output
{
  "decision": "replan" | "escalate" | "retry_simpler",
  "notes": "<diagnosis of what's going wrong>",
  "architect_hint": "<if replan: suggested approach change for the Architect>"
}
```

The wrapper templates these inputs using `jq` and passes them via: 
`echo "$input_json" | claude -p "$(cat config/agent_roles/conductor.md)" --output-format json`

**Verification:** Write a task file to `state/agent_tasks/`. The wrapper detects it on the next
poll cycle, invokes `claude -p` for classification, spawns Architect session in a tmux pane,
detects plan signal, creates worktree, spawns Executor, manages checkpoint cycle, spawns
Reviewer. End-to-end pipeline without human intervention. Verify: (a) each agent ran in its own
tmux pane and communicated via signal files, (b) the wrapper never invoked `claude -p` for
mechanical operations (git, tmux, state writes), (c) `claude -p` was only invoked for judgment
calls (classification, context assembly).

---

### Phase F — OMC Hook Configuration + Custom Agent Roles

**Goal:** Configure OMC's hooks for our use case. Write custom agent role definitions for
Ozymandias-specific roles. This phase is simpler in v4 than v3 because there's no orchestrator
script to integrate hooks with — every agent is a Claude Code session with the same hook system.

**What gets built:**

**OMC hook configuration:**
- Configure OMC's hooks.json for our project (selective — not all lifecycle events):
  - `Stop` → `persistent-mode.cjs` (keep Ops Monitor/Dialogue running; keep Executor
    working until zone file complete. Conductor is a bash wrapper — no Stop hook needed.)
  - `PreCompact` → `pre-compact.mjs` (preserve zone file state + task state + trading domain
    rules through compaction)
  - `PreToolUse` → `pre-tool-enforcer.mjs` (block spawn loops for worker sessions — Executor,
    Architect, Reviewer, Analyst all have `OMC_TEAM_WORKER=true`)
  - `PostToolUseFailure` → `post-tool-use-failure.mjs` (failure retry tracking)
  - `SubagentStop` → `subagent-tracker.mjs` (cost tracking if any agent uses internal subagents)
- Configure OMC's model routing:
  - `OMC_MODEL_HIGH=claude-opus-4-6` (Architect, Executor)
  - `OMC_MODEL_MEDIUM=claude-sonnet-4-6` (Reviewer, Analyst)
  - `OMC_MODEL_LOW=claude-haiku-4-5` (Ops Monitor)
- Disable features we don't use: keyword detector (wrapper handles routing),
  team pipeline (wrapper manages the pipeline directly via tmux)

**Custom agent role definitions** (in `config/agent_roles/`):
- `conductor.md` — judgment prompt for on-demand `claude -p` invocations (task classification,
  context assembly, failure diagnosis). Model: Sonnet. See § Phase E for full definition.
- `ops_monitor.md` — anomaly detection protocol, escalation tiers, permission boundaries.
  Model: Haiku. `disallowedTools: Write, Edit` (for source files; can write bug reports).
- `strategy_analyst.md` — four-category classification, hindsight bias prevention, Ontologist
  gate. Model: Sonnet. `disallowedTools: Write, Edit` (writes findings to signal files only).
- `dialogue.md` — full pressure-testing protocol (Contrarian/Simplifier/Ontologist), readiness
  gates (non-goals, decision boundaries). Model: Max plan.

**Adapted OMC agent definitions** (modify existing OMC agents for trading domain):
- `executor.md` — add `<Trading_Domain_Rules>` section (async, no third-party TA, atomic writes),
  add worktree-aware scope guidance, add commit-before-completion rule, add Simplifier
  pressure-test gate, add zone file update protocol, add signal file write convention
- `architect.md` — add intent classification gate (bug/calibration/feature/refactor/analysis),
  add checkpoint placement strategy, add readiness gates, add `<Trading_Domain_Rules>`,
  add signal file write convention (`state/signals/architect/<task-id>/`)
- `reviewer.md` (adapt from OMC's `verifier.md`) — add Contrarian pressure-test, add
  verification tiers (light/standard/thorough), add trading convention checks, add structured
  approval format with file:line citations, add signal file write convention

**Permission configuration** (`.claude/settings.json`):
Pre-allow tools each role needs to avoid interactive permission prompts during unattended
operation:
- Conductor: Read, Write, Bash (git, tmux, file operations), Glob, Grep
- Executor: Read, Write, Edit, Bash (git, pytest), Glob, Grep
- Architect: Read, Bash (git diff, pytest — read-only operations), Glob, Grep.
  `disallowedTools: Write, Edit`
- Reviewer: Read, Bash (git diff, pytest), Glob, Grep. `disallowedTools: Write, Edit`
- Ops Monitor: Read, Bash (process management, limited), Glob, Grep.
  `disallowedTools: Write, Edit` for source files
- Dialogue: Read, Write (plans/, state/ only), Bash (git), Glob, Grep

**What we skip from OMC:**
- Keyword detector (Conductor handles routing)
- Team pipeline (wrapper manages the Architect → Executor → Reviewer cycle via tmux)
- Most of the 19 stock agents (we define 7 custom roles)
- Session start/end hooks (persistent mode + tmux management handle session lifecycle)

**Verification:** Conductor spawns an Executor session in a worktree with OMC hooks active.
Verify: persistent-mode prevents premature exit. pre-tool-enforcer blocks spawn attempts within
the Executor session. Kill the Conductor session mid-task — verify worktree + zone file enable
recovery by restarting the Conductor (it reads orchestrator_state.json and resumes). Verify each
agent ran in its own tmux pane. Verify cost tracking in zone files. Verify post-tool-use-failure
triggers approach change after 5 failures within an Executor session.

---

## Graceful Shutdown Protocol

*Adapted from v3 for Conductor replacing orchestrator script.*

### Market close (daily, automatic)

Triggered by: bot writes `session_close` signal to `state/signals/status.json`.

| Component | Behavior | Order |
|-----------|----------|-------|
| Trading bot | Winds down normally (existing behavior) | 1 |
| Ops Monitor | Writes daily summary to `state/agent_tasks/` (type: `daily_summary`), enters idle. Stop hook releases — Haiku session can terminate. | 2 |
| Strategy Analyst | Wrapper spawns Analyst session in ephemeral tmux pane | 3 |
| Conductor wrapper | Stops accepting new tasks (reads shutdown flag from signal file). In-progress Executor sessions continue to completion (they're writing code, not trading). New tasks queued but not started until next session. | 2 |
| Executor(s) | Continue working in their tmux panes. Unaffected by market close. | — |
| clawhip | Continues running. Routes dev notifications normally. | — |
| Dialogue | Continues running. Operator may still want to discuss strategy post-market. | — |
| Discord companion | Continues running. `!status` still works, `!pause`/`!exit` become no-ops. | — |

### Full system shutdown (operator-initiated)

Triggered by: operator writes `state/SHUTDOWN` or sends `!shutdown` via Discord.

Shutdown sequence (order matters — dependencies flow downward):

1. **Conductor** reads shutdown signal:
   a. Stops accepting new tasks immediately
   b. For each active Executor tmux pane: writes `{"verdict": "shutdown", "instructions":
      "Commit current work, push branch, stop."}` to
      `<worktree>/.executor/architect_response.json`
   c. Waits up to 5 minutes for Executor sessions to exit (polls tmux pane list)
   d. Any Executor that hasn't exited after 5 min: `tmux kill-pane`, log worktree path
      for manual recovery
   e. Kills any remaining ephemeral agent panes (Architect, Reviewer)
   f. Writes final state to `state/orchestrator_state.json`
   g. Conductor wrapper writes `shutdown` intent file, exits — outer loop stops cleanly
2. **Ops Monitor** detects shutdown signal, writes final status snapshot, exits
3. **Dialogue** receives shutdown via companion bridge, exits
4. **Discord companion** posts "System shutting down" to all channels, exits
5. **clawhip** drains delivery queue (dead letter queue persists failed deliveries), exits
6. **Trading bot** — if still running, already has its own shutdown via `EMERGENCY_EXIT`

### Per-component crash recovery

| Component | If it crashes... | Recovery |
|-----------|-----------------|----------|
| Trading bot | Ops Monitor detects stale `status.json`, restarts (max 3/hour) | Automatic |
| clawhip | Discord goes silent. No data loss — signal files persist. Conductor unaffected. | Manual restart or systemd |
| Ops Monitor | No anomaly detection. clawhip detects stale Ops Monitor heartbeat, alerts Discord. | Conductor or operator restarts |
| Conductor wrapper | In-progress Executor sessions continue working in their tmux panes (independent processes). Outer loop reads `exit_intent.json`: if `restart` intent → restarts in 5s. If no intent file (unclean death) → writes alert signal, stops — operator restarts `tools/start_conductor.sh`. On restart, wrapper reads `state/orchestrator_state.json`, checks tmux pane list for surviving sessions, reads zone files, resumes pipeline. | Automatic on `restart` intent; manual on crash (no intent file) |
| Executor (tmux pane) | Pane exits unexpectedly. Worktree preserved with zone file showing last completed unit. Wrapper detects missing pane (via `tmux list-panes`), spawns new Executor in same worktree. | Automatic via wrapper |
| Architect/Reviewer (tmux pane) | Pane exits without writing signal. Wrapper detects timeout, re-spawns session. | Automatic via wrapper |
| Dialogue | Companion detects dead process, restarts. Conversation context lost, filesystem state preserved. | Automatic |
| Discord companion | Bot commands stop working. clawhip still posts outbound notifications. | Manual restart or systemd |

---

## Rollback Protocol

*Unchanged from v3.*

**Post-merge test failure** (CONCERN-9b: use explicit SHA, not HEAD):
1. Conductor runs full test suite (`pytest`) on main after every merge (via Bash)
2. Conductor captures the merge commit SHA from the `git merge` output
3. If tests fail: `git revert --no-edit <merge-sha>` (via Bash). **Never use `HEAD`** — an
   operator hotfix or a second merge between the merge and the revert would target the wrong
   commit. The SHA is captured at step 2 and passed directly.
4. Conductor writes alert to `state/signals/alerts/` (clawhip → Discord)
5. Failed task moved to status `failed_post_merge`, worktree preserved for debugging
6. Operator decides: retry with different approach, or manual fix

**Sequential merge conflict:**
1. If two Executors finish in sequence, the Conductor merges one at a time (merge serialization
   — see § Parallel Executor protocol).
2. Before merging the second branch: Conductor rebases it on current main
   (`git rebase main` in worktree via Bash) and re-runs the full test suite. This catches
   semantic conflicts (no git conflict, but broken interaction) that `git merge` cannot detect.
3. If rebase conflicts or post-rebase tests fail: Conductor aborts, escalates to Architect for
   re-planning. First merge is not reverted — it passed tests. Second task goes back to
   `review` status.
4. If rebase succeeds and tests pass: `git merge --no-commit feature/<task-id>` for final
   conflict check, then commit.

---

## Model Tier + Token Cost Strategy

| Agent | Model | Implementation | Output mode | Cost/cycle |
|-------|-------|----------------|-------------|------------|
| Ops Monitor | Haiku | Claude Code (persistent tmux pane) | Terse | ~$0.50-1.00/day |
| Strategy Analyst | Sonnet | Claude Code (ephemeral tmux pane) | Normal | ~$0.50-1.00/run |
| **Dialogue** | **Max plan** | **Claude Code (persistent tmux pane)** | **Normal** | **Zero** |
| **Conductor** | **Sonnet (on-demand)** | **Bash wrapper + `claude -p` per-event** | **JSON** | **~$0.30-1.00/day** |
| Architect | Opus | Claude Code (ephemeral tmux pane) | Normal | ~$1.00-3.00/plan |
| Executor | Opus | Claude Code (ephemeral tmux pane, git worktree) | Caveman | ~$5-15/feature |
| Reviewer | Sonnet | Claude Code (ephemeral tmux pane) | Caveman | ~$0.50-1.00/review |
| clawhip | — | Rust daemon | — | Zero |

**Daily floor (bot ops, no dev work):** ~$0.50-1.00/day (Ops Monitor + Conductor judgment calls;
Dialogue is on Max plan)
**Per feature (full dev cycle):** ~$7-20 (higher than v3's $4-13 due to Claude Code overhead
per session vs. raw API calls)
**Model override:** Any role can use a lighter model per-task. The wrapper writes the model
into the staging CLAUDE.md for that agent session.

**Cost tradeoff vs. v3:** Per-interaction costs are ~2-3x higher because each agent session
carries Claude Code system prompt + tool definitions overhead that raw API calls don't. This
is offset by:
- Zero standing cost for Dialogue (Max plan); near-zero for Conductor (bash wrapper + on-demand Sonnet)
- No infrastructure cost for building/maintaining a Python orchestrator script
- Simpler debugging (bash wrapper + JSONL event log + tmux scrollback capture — see § Debugging & Observability)
- Higher agent capability (Architect/Reviewer can browse codebase directly)

---

## Dependencies and Ordering

```
Phase A   (signal files)              ← no dependencies, start immediately
Phase B   (clawhip + companion)       ← depends on A
Phase B.5 (dialogue)                  ← depends on B (needs Discord bridge)
Phase C   (ops monitor)               ← depends on B (needs Discord for notifications)
Phase D   (strategy analyst)          ← depends on A + E (wrapper spawns Analyst, routes findings)
Phase E   (conductor wrapper + format)← depends on A + B (wrapper uses signals + clawhip)
Phase F   (OMC hooks + custom roles)  ← depends on E (hooks integrate with Conductor protocol)

Parallelizable after B: B.5, C
Parallelizable after A: D, early E work (task schema design)
```

**v4 change:** Phase E is lighter than v3's — ~50-80 lines of bash instead of ~500-800 lines
of Python. But the wrapper is real code (polling loop, state I/O, git ops, tmux lifecycle,
logging infrastructure) that needs testing. Phase D now depends on E (wrapper spawns and
manages the Analyst). Phase F carries significant weight (role definitions, permission
configuration, OMC hook setup).

---

## Resolved: Conductor Architecture (B+C)

**Decision:** Option B (thin deterministic wrapper) + Option C (sequential-first staging).

**Root cause of the original risk:** Category error — conflating judgment work (what LLMs excel
at) with mechanical work (what deterministic code excels at). The claw-code ecosystem
universally puts deterministic code in the coordination seat. OmX, clawhip, and OmO are all
deterministic code. None put an LLM in the coordination seat. v4's original design departed
from this pattern by making the Conductor a persistent LLM session running a polling loop.

**The six failure modes this resolves:**
- *Polling drift:* Impossible — `while true; sleep 10; ls` never drifts
- *Non-deterministic routing:* Impossible — priority order is a bash case statement
- *State file corruption:* Impossible — wrapper reads JSON with `jq`, not an LLM
- *Arithmetic errors:* Impossible — `$(( $(date +%s) - start_time ))` never hallucinates
- *Hallucinated state:* Impossible — wrapper reads disk every cycle, has no memory
- *Context degradation:* N/A — wrapper has no context window

**Architecture change:** The Conductor is no longer a persistent Claude Code session running a
polling loop. Instead:
- **`tools/conductor.sh`** (~50-80 lines bash, sequential-first): Owns the polling loop, signal
  directory scanning, timestamp math, state file I/O (`jq`), git operations (worktree
  create/cleanup, merge, revert), tmux lifecycle (spawn/kill panes), heartbeat, and timeout
  enforcement. Runs as a plain bash process — no LLM, no context window, no degradation.
- **`claude -p` invocations** (on-demand, per-event): The wrapper invokes Claude Code with
  focused context when a signal requires judgment: task classification/prioritization, Architect
  plan assembly, failure diagnosis after 3 retries, context assembly for agent staging dirs.
  Each invocation is a fresh session with minimal context — no accumulated state, no compaction
  risk. The Conductor role prompt (`config/agent_roles/conductor.md`) becomes the `-p` system
  prompt for these invocations, not a persistent session prompt.
- **Agent spawning** (unchanged): `tmux split-window` for Architect, Executor, Reviewer sessions.
  The wrapper spawns them, not a persistent LLM.

**What the wrapper does (mechanical — deterministic):**
```bash
while true; do
  # 1. Poll signal directories
  scan state/agent_tasks/          → detect new tasks
  scan state/signals/architect/    → detect plan completions, review verdicts
  scan state/signals/reviewer/     → detect review verdicts
  scan .worktrees/*/. executor/    → detect executor checkpoints, completions

  # 2. For each new signal, classify by filename convention
  #    checkpoint.json    → spawn Architect review session
  #    complete.json      → spawn Reviewer session
  #    plan.json          → create worktree, spawn Executor
  #    approved.json      → rebase, test, merge (capture SHA), post-merge test
  #    rejected.json      → route feedback to new Executor session
  #    blocked.json       → spawn Architect re-plan session
  #    escalation.json    → alert operator via signal file for clawhip

  # 3. Mechanical operations (no LLM needed)
  #    - git worktree add/remove
  #    - git merge, git revert --no-edit <sha>
  #    - tmux split-window (spawn), tmux kill-pane (timeout)
  #    - jq read/write on orchestrator_state.json
  #    - wall-clock timeout checks
  #    - heartbeat file write
  #    - staging dir create/cleanup

  # 4. Judgment operations (invoke claude -p with focused context)
  #    - New task arrived → classify priority, check backpressure
  #    - Architect plan needed → assemble context for staging CLAUDE.md
  #    - 3+ failures on a task → diagnose, decide re-plan vs escalate
  #    - Ambiguous signal → interpret and route

  sleep 10
done
```

**What the LLM does (judgment — on-demand `claude -p`):**
- Task classification: Is this a bug fix, strategy analysis, or backlog item? Priority?
- Context assembly: What files, indicators, and history should the Architect see?
- Failure diagnosis: After 3 retries, what went wrong? Re-plan or escalate?
- Ambiguity resolution: Signal doesn't match a known pattern — what should happen?

Each `claude -p` call gets: the Conductor role prompt as system prompt, the specific signal
file contents, relevant state from `orchestrator_state.json`, and pointers to task/zone files.
Fresh context every time — no accumulation, no compaction, no degradation.

**Sequential-first staging (Option C):**
Phase E implements the wrapper with sequential task execution only — one task at a time through
the full pipeline (Architect → Executor → Reviewer → merge). No zone claim locks, no merge
serialization, no rebase-and-retest. The wrapper starts at ~30 lines. Parallel Executor support
(~20 more lines for concurrency tracking) is added after the sequential pipeline is proven
reliable over 5+ trading days.

**Options considered and rejected:**

| Option | Verdict | Reason |
|--------|---------|--------|
| A. Pure LLM Conductor | Rejected | 6 known failure modes on critical path over ~2,880 daily poll cycles. 0.1% error rate = ~3 corruptions/day. |
| D. clawhip as polling | Deferred | Architecturally elegant but puts clawhip on the dev pipeline critical path. 10s bash polling is adequate. Revisit as a performance optimization after B+C is proven. |
| E. OmO coordination | Rejected | Solves agent *disagreement* — a problem that does not exist in a hierarchical pipeline. See § What This Plan Does NOT Cover. |

---

## What This Plan Does NOT Cover

- **Bot autonomy escalation** (supervised → guided → autonomous → silent). Feature within
  Phase C's Ops Monitor, not phase-level work.
- **Prompt versioning fix** (CONCERN-5). Independent fix, should be done before Phase F.
- **Hashline editing.** Deferred — git worktrees eliminate most edit conflicts. Revisit only if
  post-merge integration conflicts become a pattern.
- **OmO adoption.** Rejected — 214K LOC TypeScript, OpenCode plugin dependency, multi-provider
  abstraction tax for Claude-only use. OMC is the Claude-native equivalent and is adopted
  instead. See `claw-code-analysis.md` § 7.
- **OmX adoption.** Rejected as a direct dependency — Codex-first. Patterns adopted (worktree
  isolation, structured delegation, ambiguity scoring). clawhip adopted directly.
- **oh-my-openagent coordination layer (Option E).** Rejected after lead engineer analysis.
  OmO's core value proposition is managing *disagreement* between peer agents from different AI
  providers. Our pipeline is hierarchical (Architect has planning authority, Executor implements
  or escalates, Reviewer has veto authority) — there is no peer negotiation to manage. A
  Reviewer rejection is a state machine transition routed by the wrapper, not a conflict
  requiring mediation. OmO solves the wrong problem for this topology.
- **Complex message broker.** Explicitly rejected. No Redis, no RabbitMQ. File watching via
  clawhip (inotifywait) and polling by the Conductor.
- **Custom Python orchestrator script.** v3's `tools/agent_runner.py` (~500-800 lines Python)
  is replaced by `tools/conductor.sh` (~50-80 lines bash) + on-demand `claude -p` for judgment.
- **Raw Anthropic API calls.** All LLM interactions go through Claude Code sessions. No API key
  management for agent calls (only for the trading bot's existing Claude reasoning).
- **OMC Agent tool for inter-agent coordination.** Agents are separate tmux sessions, not
  subagents. The wrapper spawns and monitors them via tmux commands and signal file polling.

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
5. Sequential pipeline works end-to-end: one task flows through Architect → Executor → Reviewer
   → merge without human intervention. (Post-stabilization: parallel Executors in separate git
   worktrees without file conflicts or integration regressions — deferred per B+C decision.)
6. Every agent role has a quantified pressure-testing gate — no agent operates on vibes.
7. Executor implementations are checkpoint-reviewed by Architect before proceeding past high-risk
   units — no unsupervised architectural drift.
8. Per-task wall-clock tracking is active — no runaway sessions without operator awareness.
   (Token-based cost tracking is unavailable for Claude Code sessions — CONCERN-9a.)
9. Agent safety infrastructure prevents: premature termination (Stop hook), stale state (2-hour
   expiry), context degradation (PreCompact preservation), and spawn loops (anti-spawn-loop hook).
   Git worktree isolation prevents cross-Executor file conflicts and protects main from
   incomplete work.
10. Graceful shutdown: market close winds down ops agents while dev pipeline continues;
    full shutdown commits in-progress work before cleanup; each component recovers independently
    from crashes. Conductor can forcibly kill agent panes via `tmux kill-pane`.
11. Post-merge regression safety: Conductor runs full test suite after every merge, automatic
    revert on failure, operator escalation.
12. Strategy Analyst findings are tracked and deduplicated — no re-discovering known issues.
13. Conductor health is monitored via heartbeat — silent failure is detected and alerted.
14. **Deterministic coordination.** The dev pipeline is managed by a bash wrapper
    (`tools/conductor.sh`, ~50-80 lines) that owns polling, state I/O, git ops, and tmux
    lifecycle. LLM judgment is invoked on-demand via `claude -p` — no persistent LLM session
    in the coordination seat. Infrastructure to build: wrapper script, role definitions, OMC
    hook config, and permission config.
15. **Every agent is independently observable.** Each runs in its own tmux pane — the operator
    can select any pane to watch an agent work in real time.

---

## v3 → v4 Change Summary

| Aspect | v3 (API + OMC) | v4 (OMC-only) |
|--------|---------------|---------------|
| Orchestrator | `tools/agent_runner.py` (Python script) | `tools/conductor.sh` (bash wrapper) + on-demand `claude -p` for judgment |
| Architect | Raw Opus API call | Claude Code session in ephemeral tmux pane |
| Reviewer | Raw Sonnet API call | Claude Code session in ephemeral tmux pane |
| Strategy Analyst | Raw Sonnet API call | Claude Code session in ephemeral tmux pane |
| Executor | Claude Code spawned by Python script in tmux pane | Claude Code spawned by Conductor in tmux pane |
| Agent spawning | Python `subprocess` / `tmux send-keys` | Bash wrapper uses `tmux split-window` |
| Agent monitoring | Python polls signal dirs | Bash wrapper polls signal dirs (deterministic) |
| Agent communication | Signal files (Executor) + API response parsing (Architect/Reviewer) | Signal files for all agents (uniform) |
| Checkpoint protocol | File polling (5s) + API calls for Architect review | Wrapper polling (10s) + spawn Architect tmux session |
| Context control | API calls: exact context. CC sessions: filesystem access. | All filesystem access (less precise, more capable) |
| Cost tracking | Custom token counting in Python | Wall-clock timeout only (CONCERN-9a: CC sessions don't expose tokens) |
| Executor timeout | Python `tmux kill-pane` after 60min | Wrapper `tmux kill-pane` after 60min (same mechanism) |
| Daily standing cost | ~$0.80-1.50/day | ~$0.80-2.00/day (Ops Monitor + Conductor judgment calls; wrapper itself is free) |
| Per-feature cost | ~$4-13 | ~$7-20 (Claude Code overhead per session) |
| Infrastructure to build | ~500-800 lines Python | ~50-80 lines bash (wrapper) + role definitions + hook config |
| Layers | 3 (OMC + orchestrator script + clawhip) | 3 (bash wrapper + OMC agent sessions + clawhip) |
| Agent observability | Each in own tmux pane | Each in own tmux pane (same) |
| Forced kill | Python `tmux kill-pane` | Wrapper `tmux kill-pane` (same) |
| Agent capability | API agents: only see assembled context. CC agents: full access. | All agents: full codebase access (higher capability, less isolation) |
