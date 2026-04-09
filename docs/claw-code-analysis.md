> **Deprecated:** One-time historical analysis. Not migrated to wiki.

# Claw-Code Ecosystem — Engineering Analysis

**Date:** 2026-04-07 (revised)
**Purpose:** Extract adoptable architecture, engineering solutions, and patterns from the
claw-code multi-agent development ecosystem. Evaluated through the lens of Ozymandias v3:
a single-developer, Claude-only, cost-sensitive trading bot project.

**Repositories analyzed:**
- `Yeachan-Heo/oh-my-codex` (OmX) — TypeScript + Rust, workflow orchestration, 25.6K stars
- `Yeachan-Heo/clawhip` — Rust, event routing daemon, 641 stars
- `code-yeongyu/oh-my-openagent` (OmO) — TypeScript, multi-agent coordination, 49.2K stars
- `Yeachan-Heo/oh-my-claudecode` (OMC) — TypeScript, Claude Code agent definitions, 25.6K stars
- `ultraworkers/claw-code` — Rust, Claude Code CLI reimplementation, 176K stars

**Evaluation criteria:**
- **Want:** Architecture patterns, engineering solutions to unsolved problems, tools and prompts
  to base ours on, agent problem handling (context, scope, ambiguity)
- **Don't want:** Multi-ecosystem dependencies, solutions more complex than the problems they solve

---

## 1. The Ecosystem: What It Actually Is

The claw-code stack is four interlocking tools built by three developers (Bellman/Yeachan Heo,
YeonGyu Kim, Sigrid Jin) in the Korean UltraWorkers community. Each tool has a distinct role:

| Tool | Language | Role | Size |
|------|----------|------|------|
| OmX | TypeScript + Rust | Workflow layer: turns directives into structured agent work | ~35 skills, Rust runtime core |
| clawhip | Rust | Event daemon: watches filesystem/git/tmux, routes to Discord | ~8K LOC, axum HTTP server |
| OmO | TypeScript | Multi-agent orchestration: delegation, conflict, verification | ~214K LOC, 1,602 files |
| OMC | TypeScript | Claude Code adapter: agent definitions, hooks, team pipeline | ~4,779 files, 19 agents |

**Critical context:** OmX was built for Codex CLI (Claude support is secondary via
`OMX_TEAM_WORKER_CLI=claude`). OmO is a plugin for OpenCode (a Claude Code fork), not
standalone. OMC is the Claude-native adaptation of the same ideas. clawhip is the only
truly standalone, CLI-agnostic tool.

---

## 2. Architecture Patterns Worth Adopting

### 2.1 Skills as Prompt Injection, Not Code

OmX skills are not executable code. They are SKILL.md files — markdown with YAML frontmatter
that get injected into the LLM's context when a keyword is detected. The LLM reads the
instructions and follows them. The skill system is a prompt routing layer, not an orchestration
framework.

```yaml
---
name: ralph
description: Persistent execution loop
argument-hint: "[--prd] <task>"
---
# Ralph Execution Protocol
1. Load context snapshot...
2. Review TODO list...
[~200 lines of structured instructions the LLM follows]
```

**Why this matters for us:** Our agent roles (Architect, Executor, Reviewer) don't need to be
separate programs. They can be prompt templates that get injected into a Claude session. The
"agent" is just Claude following different instructions depending on the role. This collapses
the implementation complexity dramatically — we don't need an agent framework, we need a
prompt loader.

**Adoption path:** Define each agent role as a markdown file in `config/agent_roles/`. The
orchestrator reads the role file and injects it as context for the appropriate API call or
Claude Code session.

### 2.2 Agent Definitions with YAML Frontmatter

OMC defines 19 agents as markdown files with structured frontmatter:

```yaml
---
name: architect
description: System design, debugging advisor
model: claude-opus-4-6
level: 3
disallowedTools: Write, Edit
---
<Agent_Prompt>
<Role>...</Role>
<Constraints>...</Constraints>
<Output_Format>...</Output_Format>
<Failure_Modes_To_Avoid>...</Failure_Modes_To_Avoid>
</Agent_Prompt>
```

**Key design decisions:**
- `disallowedTools` enforces read-only agents structurally (Architect can't write code)
- `model` ties complexity to cost (Haiku for exploration, Sonnet for implementation, Opus for
  architecture)
- `level` is a complexity tier (2-4) that affects verification depth
- The body uses XML-structured sections, not free-form markdown

**What makes this better than ad-hoc prompts:** The frontmatter is machine-parseable. An
orchestrator can read the model, tool restrictions, and complexity tier without parsing the
prompt itself. The role behavior is in the markdown body; the role metadata is in the YAML.

**Adoption path:** Use this format for our agent role definitions. Each role file specifies the
model, allowed tools, and behavioral prompt. The orchestrator reads frontmatter to determine
which model to call and what tool restrictions to enforce.

### 2.3 The Three-Layer Separation (Refined)

The claw-code architecture separates three concerns. Our version should preserve the
separation but simplify the implementation:

| Concern | Claw-code tool | Our implementation |
|---------|---------------|-------------------|
| Directive parsing + workflow | OmX (TypeScript + Rust, 35 skills) | Prompt templates + orchestrator script |
| Event routing + notification | clawhip (Rust daemon) | clawhip (adopt directly) |
| Agent coordination | OmO (214K LOC) + OMC (4,779 files) | Sequential pipeline in orchestrator script |

**The key insight:** OmX and OmO are massively complex because they support multiple AI
providers, parallel teams of arbitrary size, and persistent execution loops that run for hours.
We need none of this. Our dev pipeline is sequential (Architect → Executor → Reviewer) with
at most 2-3 parallel Executors on different zones. The orchestrator script that manages this
is ~500 lines of Python, not a Rust runtime with a TypeScript bridge.

clawhip, by contrast, does exactly one thing well (event routing) and is genuinely reusable.

### 2.4 Authoring and Review Are Always Separate Passes

OMC enforces this structurally: agents with `disallowedTools: Write, Edit` physically cannot
modify code. The Architect analyzes, the Executor implements, the Reviewer evaluates. No
agent self-approves in the same context.

This is not just role separation — it's a structural guarantee. An Architect that could also
write code might skip planning and jump to implementation. A Reviewer that could edit might
fix issues instead of reporting them, masking the Executor's quality problems.

**Adoption path:** For API-based agents (Architect, Reviewer, Analyst), don't provide write
tools at all. For Claude Code Executor sessions, the role prompt should explicitly forbid
self-review. The Reviewer's API call includes only the diff and test output, never the plan
reasoning — preventing it from rubber-stamping based on good intentions rather than good code.

---

## 3. Engineering Solutions to Problems We Haven't Solved

### 3.1 Persistent Mode (Stop Hook Blocking)

**Problem:** Claude terminates when it thinks the task is done, even mid-workflow.

**OMC's solution:** A Stop event hook (`persistent-mode.cjs`) that intercepts Claude's
termination attempt. When an active workflow state exists (ralph, autopilot, team, etc.),
the hook returns `{ decision: "block", reason: "[RALPH LOOP - ITERATION 5/100] Work is
NOT done. Continue working." }`. Claude receives this as a continuation prompt and resumes.

**Safety guards (critical — these prevent deadlocks):**
- Context limit stops: NEVER blocked (Claude must be able to compact)
- User abort (Ctrl+C): NEVER blocked
- Authentication errors (401/403): NEVER blocked
- Stale state: States older than 2 hours are ignored
- Cancel signal: A cancel state file with 30-second TTL allows clean termination

**Relevance to us:** This solves the "Executor stops mid-feature" problem without requiring
session management or restart logic. The Executor keeps going until all implementation units
are complete. Combined with zone file state tracking, even if the session compacts, the
continuation prompt includes what's been done and what remains.

**Adoption path:** For Claude Code Executor sessions, implement a Stop hook that checks zone
file completion status. If units remain, block termination with a continuation prompt listing
remaining work. Include all safety guards — especially the context limit exception.

### 3.2 Context Window Monitoring and Preemptive Compaction

**Problem:** Agent sessions silently degrade as context fills up. By the time Claude starts
producing lower-quality output, it's too late.

**OmO's solution:** Two-stage monitoring:
1. At 70% context usage: inject a reminder — "You still have context remaining, do NOT rush
   or skip tasks." This counteracts Claude's tendency to cut corners as context grows.
2. At 78% context usage: automatically trigger compaction. 60-second cooldown prevents
   compaction loops.

During compaction, critical context and todo state are preserved via dedicated hooks
(`compaction-context-injector`, `compaction-todo-preserver`) so they survive summarization.

**Relevance to us:** Our Executor sessions on multi-unit features will hit context limits.
Without monitoring, the Executor might produce sloppy code on the last unit because context
is nearly full. The 70% warning is cheap and effective. The preservation hooks ensure zone
file state and CLAUDE.md conventions survive compaction.

**Adoption path:** Implement context monitoring as a hook for Claude Code sessions. The 70%
threshold injects "Check your zone file — what units remain? Do not skip steps." The 78%
threshold triggers compaction with zone file state preserved.

### 3.3 Tool Failure Retry Tracking

**Problem:** Agents retry failed operations identically, burning tokens.

**OmO's solution:** `post-tool-use-failure.mjs` tracks failures in
`.omc/state/last-tool-error.json` with retry counting within a 60-second window. After 5+
consecutive failures of the same tool, it injects guidance: "Try a different approach instead
of retrying."

OMC's agent prompts reinforce this: Executor must escalate to Architect after 3 consecutive
failures. The escalation is structural, not suggested.

**Relevance to us:** An Executor stuck in a test-fail-retry loop burns Opus tokens on the same
broken approach. The failure counter forces a strategy change: try a different implementation,
or escalate to Architect for re-planning.

**Adoption path:** For API-based Executor calls with tool use, count consecutive failures
per tool type. After 3 failures: inject "Stop retrying. Describe what you've tried and why
it failed. Try a fundamentally different approach." After 5: escalate to Architect via signal
file.

### 3.4 Anti-Duplication Enforcement

**Problem:** After delegating research to a subagent, the parent agent redundantly performs
the same searches itself.

**OmO's solution:** An explicit anti-duplication section injected into Sisyphus and other
orchestrator prompts: "DO NOT perform the same search yourself after delegating. DO NOT
manually grep/search for the same information. DO NOT re-do the research."

**Relevance to us:** When our Architect delegates exploration to a subagent or consults
existing analyses, it might redundantly re-read the same files. Explicit anti-duplication
in the role prompt prevents this.

**Adoption path:** Include in every agent role prompt: "If you have delegated a query or
received analysis output, do not repeat the same investigation. Use the results provided."

### 3.5 Worker Sandboxing (Anti-Spawn-Loop)

**Problem:** A worker agent invoked by `$team` might itself invoke `$team`, creating an
infinite spawning loop.

**OMC's solution:** When `OMC_TEAM_WORKER` env is set, the keyword detector exits
immediately with `suppressOutput: true`. The pre-tool-enforcer additionally blocks Task
tool, Skill tool, and orchestration skill invocations (`$team`, `$ralph`, `$autopilot`).
Workers physically cannot spawn more workers.

**Relevance to us:** If our Executor is a Claude Code session, nothing prevents it from
spawning its own subagents or invoking orchestration commands. We need a structural guard.

**Adoption path:** The Executor's CLAUDE.md role prompt should include a "prohibited
actions" section that explicitly forbids spawning agents, invoking orchestration commands,
or modifying files outside the assigned zone. For Claude Code sessions, a PreToolUse hook
can block the Agent tool entirely.

### 3.6 Subagent Cost Tracking

**Problem:** Delegated agents can run up unbounded costs.

**OmO's solution:** `subagent-tracker.mjs` tracks every spawned agent with model, token
usage, and duration. A hard cost limit of $1.00 per subagent triggers intervention. Stale
agent detection (>5 min without progress) flags stuck agents.

**Relevance to us:** An Opus Executor working on a complex feature could burn $10+ in a
single session. We need per-task cost visibility, and a mechanism to intervene when costs
exceed expectations.

**Adoption path:** For API calls, track token usage per call and per task. For Claude Code
Executor sessions, log API usage and compare against a per-task budget (configurable,
default based on task complexity tier). Alert via signal file when 80% of budget consumed.
Kill the session at 100% if no human override.

### 3.7 Informational Intent Filtering

**Problem:** User asks "what is ralph?" and the system activates the ralph skill instead
of explaining it.

**OmX/OMC solution:** Before activating a keyword-triggered skill, check an 80-character
context window around the keyword for informational patterns (`what is`, `how to use`,
`explain`, `tell me about`). If matched, suppress activation.

**Relevance to us:** When we implement Discord command routing, `!status` should trigger
the status command, but "what does !status do?" should not. Simple regex guard, high
value.

**Adoption path:** Any command parser should check for informational intent before
dispatching. Trivial to implement — regex check before command execution.

---

## 4. Prompt Patterns to Base Ours On

### 4.1 Agent Prompt Structure (OMC)

OMC's agent prompts use consistent XML sections. The structure is worth adopting:

```xml
<Agent_Prompt>
  <Role>
    One-paragraph description of what this agent does and doesn't do.
  </Role>
  <Why_This_Matters>
    Why this role exists — the failure mode it prevents.
  </Why_This_Matters>
  <Success_Criteria>
    Concrete, testable criteria for "done."
  </Success_Criteria>
  <Constraints>
    Hard boundaries: what the agent must never do.
  </Constraints>
  <Tool_Usage>
    Which tools to use and when. Preferred vs. forbidden.
  </Tool_Usage>
  <Execution_Policy>
    Step-by-step protocol. Phase 1, Phase 2, etc.
  </Execution_Policy>
  <Output_Format>
    Exact structure of the agent's output (JSON schema, markdown template, etc.)
  </Output_Format>
  <Failure_Modes_To_Avoid>
    Common mistakes with concrete examples of what NOT to do.
  </Failure_Modes_To_Avoid>
</Agent_Prompt>
```

**Why this works:** Each section serves a distinct purpose. `Role` and `Constraints` are
stable — they rarely change. `Execution_Policy` is the variable part that differs by task.
`Failure_Modes_To_Avoid` is the most valuable section — it encodes lessons learned from
actual failures, preventing recurrence.

**What to add for our use case:** A `<Trading_Domain_Rules>` section for agents that touch
trading code (Executor, Reviewer). This section references CLAUDE.md conventions: async
everywhere, no third-party TA libs, atomic JSON writes, etc.

### 4.2 Sisyphus's Intent Gate (OmO)

Every Sisyphus response begins with an intent classification:

1. Verbalize intent (map surface request to routing decision)
2. Classify: Trivial / Explicit / Exploratory / Open-ended / Ambiguous
3. Turn-local intent reset (never auto-carry implementation mode from previous turn)
4. Context-completion gate (3 conditions must pass before implementing)
5. Mandatory delegation check (bias: DELEGATE, work yourself only when trivial)

**Relevance to us:** Our Architect agent should classify incoming tasks before planning.
A bug report from Ops Monitor and a feature directive from the operator require different
decomposition strategies. The intent gate prevents the Architect from treating everything
as "implement a feature."

**Adoption path:** Add an intent classification phase to the Architect role prompt:
```
Before planning, classify this task:
- Bug fix (known broken behavior) → minimal change, focused scope
- Calibration (parameter tuning) → config change or prompt edit only
- Feature (new capability) → full plan with zones and checkpoints
- Refactor (structural improvement) → plan with migration strategy
```

### 4.3 Structured Delegation Prompts (OmO)

When Sisyphus delegates via the `task()` tool, it must provide a 6-section prompt:

```
TASK: [what to do]
EXPECTED OUTCOME: [what success looks like]
REQUIRED TOOLS: [which tools the delegate should use]
MUST DO: [mandatory requirements]
MUST NOT DO: [explicit prohibitions]
CONTEXT: [relevant background the delegate needs]
```

**Why this works:** The MUST NOT DO section prevents the most common delegation failure:
the delegate solving a different problem than intended. "MUST NOT: modify the risk manager"
is clearer than "only modify the ranker."

**Adoption path:** Use this structure for task packets in `state/agent_tasks/`. The Architect
fills in all 6 sections. The Executor receives only these sections plus the zone files — no
ambient context that could distract.

### 4.4 Deep-Interview Ambiguity Scoring (OmX)

The 6-dimension ambiguity scoring with weighted formula and threshold gates. Already
discussed in the plan, but the exact mechanics are worth recording:

**Greenfield weights:**
Intent 0.30, Outcome 0.25, Scope 0.20, Constraints 0.15, Success criteria 0.10

**Brownfield weights (adds context dimension):**
Intent 0.25, Outcome 0.20, Scope 0.20, Constraints 0.15, Success criteria 0.10, Context 0.10

**Depth profiles:**
- Quick: threshold 0.30, max 5 rounds
- Standard: threshold 0.20, max 12 rounds
- Deep: threshold 0.15, max 20 rounds

**Mandatory readiness gates (independent of score):**
- Non-goals must be documented
- Decision boundaries must be documented
- At least one earlier answer must be pressure-tested (revisited)

**Adoption path:** The Dialogue agent's role prompt includes these weights and the readiness
gate requirements. We use brownfield weights (Ozymandias is an existing codebase). Standard
depth profile (threshold 0.20) by default.

---

## 5. How They Handle Agent Problems

### 5.1 Context Boundaries

**OmX approach:** Team workers receive a composed `worker-agents.md` with the project's
AGENTS.md content plus a worker overlay between marker comments
(`<!-- OMX:TEAM:WORKER:START/END -->`). Workers see: their identity, inbox, mailbox, task
directory, leader CWD, and the worker protocol. They don't see the full project docs.

**OMC approach:** Agent definitions specify `disallowedTools` in frontmatter. Read-only agents
(Architect, Reviewer, Critic) have `Write, Edit` disabled. This is enforced at the tool level,
not just the prompt level.

**OmO approach:** Each agent has a `fallbackChain` and tool restrictions defined in code.
The `pre-tool-enforcer` hook emits warnings when the orchestrator tries to write source files
directly (nudges toward delegation).

**Synthesis for us:**
- API-based agents: context is controlled by what we put in the prompt. This is the simplest
  and most reliable enforcement — the agent literally cannot see files we don't include.
- Claude Code Executor: use a custom CLAUDE.md in the working directory (or git worktree)
  that restricts scope. Add a PreToolUse hook that warns/blocks on out-of-zone file access.
- Tool restrictions: our API calls should only provide tools appropriate to the role. Architect
  gets no write tools. Reviewer gets no write tools. Executor gets write tools scoped to the
  zone.

### 5.2 Scope Creep Prevention

**OmX `$ralph`:** Uses max_iterations (default 10) as a hard cap. Each iteration is tracked
in state. The verification step compares output against the original task description, not
against what the agent thinks should be done next.

**OmO Sisyphus:** Turn-local intent reset — every message starts from scratch. The agent
cannot auto-carry "implementation mode" from the previous turn. This prevents scope creep
where the agent keeps building beyond the task.

**OMC Executor:** "Smallest viable diff. Works alone for code changes. Escalates to architect
after 3 failures." The prompt explicitly constrains output size.

**Synthesis for us:**
- Executor role prompt: "Implement exactly what the plan specifies. Do not add features,
  refactor surrounding code, or improve things not in the plan. If the plan is insufficient,
  escalate — do not improvise."
- Per-task iteration limits: configurable in the task packet (default 5 for bug fixes,
  10 for features). After the limit, the task fails and escalates.
- Verification against plan, not against current state: the Reviewer checks "does this
  implement the plan?" not "is this code good in general?"

### 5.3 Session Recovery

**OmX `$ralph`:** State persisted in `.omx/state/sessions/{sessionId}/ralph-state.json`
with fields: `active`, `iteration`, `current_phase`, `context_snapshot_path`. On restart,
the agent reads its state and resumes from the last checkpoint.

**OmO boulder state:** Tracks active plan, session IDs, completion progress (parsed from
markdown checkboxes). Stored at `.sisyphus/boulder.json`. Survives session crashes.

**OMC persistent mode:** The Stop hook prevents premature termination. But if the session
actually dies (OOM, network, context limit), recovery depends on state files.

**Synthesis for us:**
- Zone files are our recovery mechanism (already planned). Each zone file tracks: units
  completed, unit in progress, units remaining, test status, branch name.
- On session crash: the orchestrator detects the dead session, reads the zone file, spawns
  a new Executor with the zone file as context. The new Executor picks up where the old one
  left off.
- For API-based agents: no recovery needed. API calls are stateless. If one fails, retry
  with the same input.

### 5.4 Agent Failure Escalation

**OMC:** "Escalates to Architect after 3 failures." Built into the Executor role prompt.

**OmO:** After 3 consecutive failures: "STOP, REVERT, DOCUMENT, consult Oracle." Oracle is
a read-only strategic advisor that analyzes what went wrong without being able to make
changes itself.

**OmX `$ralph`:** "Same issue recurs 3+ iterations: report as potential fundamental problem."
Does not keep retrying — surfaces the issue as structural.

**Synthesis for us:**
- Executor: after 3 failures on the same unit, stop implementation and write a failure report
  to the signal file. Include: what was attempted, what failed, and a hypothesis about why.
- Orchestrator routes the failure to Architect for re-planning, or to the operator if the
  Architect also fails.
- Never retry the same approach more than 3 times. This is a hard rule in the role prompt.

### 5.5 Ambiguity Handling

**OmX `$deep-interview`:** Formal scoring across 6 dimensions with threshold gates. Asks
one question per round, targeting the weakest clarity dimension. Runs up to 20 rounds.
Challenge modes (Contrarian, Simplifier, Ontologist) injected at specific rounds.

**OmO Metis:** Pre-planning consultant that classifies intent before the planner starts.
Prevents the planner from making assumptions about underspecified requests.

**OmX `$ralph`:** Pre-execution gate — if a prompt has no concrete anchors (file paths,
function names, issue numbers) and is <= 15 words, ralph redirects to `$ralplan` first.
Bypass with `force:` prefix.

**Synthesis for us:**
- Dialogue uses the full deep-interview protocol (6 dimensions, readiness gates)
- Architect has a lightweight ambiguity check: if the task description has no file paths,
  no function names, and no test criteria, ask for clarification before planning. Don't
  plan from a vague directive.
- Executor has the simplest check: if the plan is ambiguous on a specific unit, write a
  `blocked.json` signal file and wait. Don't guess.

---

## 6. clawhip: Adoption Details

clawhip is the one tool we're adopting directly. Here's what we get:

**What it is:** A Rust daemon (tokio + axum) that watches filesystem events, git commits,
tmux sessions, and GitHub activity, then routes notifications to Discord via REST API.

**How it watches:**
- Filesystem: `inotifywait -m -r` on Linux (external binary dep), polling fallback
- Git: polls for new commits and branch changes
- tmux: polls `tmux list-sessions` and `tmux capture-pane`
- GitHub: polls API for issue/PR changes
- HTTP: axum server on port 25294 receives webhook events

**How it routes:**
- TOML config with `[[routes]]` entries: event pattern → Discord channel + mention + format
- Glob-based event matching (`github.*`, `agent.*`, `session.*`)
- Four render formats: compact, alert, inline, raw
- Rate limiting (token bucket: 5 capacity, 5/sec refill)
- Circuit breaker (3 failures → 5-second cooldown)
- Retry with exponential jitter (3 attempts)
- Dead letter queue for failed deliveries
- Batching: routine events batch over 5 seconds, CI events batch over 5 minutes

**Discord integration:** Pure REST API — no gateway connection, no discord.py, no serenity.
Bot token + channel ID for messages. Webhook URL as alternative. Messages truncated to
2000 chars. Mentions parsed from format strings.

**tmux management (bonus):** clawhip can create and monitor tmux sessions:
- `clawhip tmux new -s <name> --channel <id> --keywords "error,complete" -- 'command'`
- Keyword detection in pane output → Discord alert
- Stale session detection (configurable timeout, default 30 min)
- Agent lifecycle events: `agent.started`, `agent.finished`, `agent.failed`

**What it doesn't do:**
- No inbound Discord commands (no gateway = can't receive messages). We need a thin companion
  for `!pause`, `!status`, etc. This is a Python script with discord.py, ~100 lines.
- No agent coordination. It routes events, it doesn't manage workflows.
- No state persistence (mostly in-memory; cron state is the exception).
- The claude-code plugin is a stub (`echo "[clawhip:claude-code] hook=$*"`). We'd be
  configuring clawhip from scratch, not using a pre-built integration.

**Configuration for our project:**
```toml
[providers.discord]
token = "${DISCORD_BOT_TOKEN}"
default_channel = "alerts-channel-id"

[daemon]
bind = "127.0.0.1:25294"

[[monitors]]
kind = "workspace"
path = "state/signals/"
poll_interval_secs = 5

[[monitors]]
kind = "git"
path = "."
poll_interval_secs = 30

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/last_trade.json" }
sink = "discord"
channel = "trades-channel-id"
format = "compact"

[[routes]]
event = "workspace.file.changed"
filter = { path = "**/alerts/*" }
sink = "discord"
channel = "alerts-channel-id"
format = "alert"
mention = "<@operator-id>"

[[routes]]
event = "git.commit"
sink = "discord"
channel = "dev-channel-id"
format = "compact"
```

---

## 7. What NOT to Adopt (and Why)

### 7.1 OmO's Full Agent System

OmO has 11 agents, 26 tools, 52 lifecycle hooks, model-agnostic multi-provider routing,
and 214K lines of TypeScript. It is a product — Sisyphus Labs is the commercial version.
The complexity is justified for a general-purpose multi-provider orchestration platform.
It is not justified for a single-developer Claude-only trading bot.

**Specific problems for us:**
- Multi-provider model routing (GPT, Gemini, Kimi, GLM, Minimax) — we use Claude only
- OpenCode dependency — OmO is a plugin for OpenCode, not standalone
- Hashline editing — clever, but Claude Code's Edit tool already requires exact string
  matching. Hashline solves a problem (edit drift) that matters more with less precise
  editors. Revisit only if we see actual edit conflicts in Phase F parallel work.
- Greek mythology naming — fun but unhelpful for a domain-specific system. Our agents should
  be named for what they do (Ops Monitor, Strategy Analyst), not mythological figures.

**What to take instead:** The patterns documented in sections 3-5 above. The engineering
solutions (context monitoring, failure tracking, anti-duplication) are simple and portable.
The full OmO system is not.

### 7.2 OmX's Rust Runtime Core

OmX has a Rust runtime with four subsystems (Authority, Dispatch, Mailbox, Replay) bridged
to TypeScript via `execFileSync()`. The runtime provides: lease-based ownership, ordered
message dispatch, three-state message lifecycle, and event replay from cursor position.

**Why this is overkill for us:**
- Authority (lease-based ownership) — our zone claiming uses lock files. Simpler, sufficient.
- Dispatch (ordered message queue) — our signal files are unordered. We don't need ordering
  guarantees because our pipeline is sequential.
- Mailbox (three-state lifecycle) — our signal files use filesystem presence as state.
  File exists = message sent. File consumed = message received. No intermediate states needed.
- Replay (crash recovery) — our zone files track state directly. No event sourcing needed.

**What to take instead:** The pattern of atomic file writes with temp-then-rename (which we
already use) and the concept of state scoped to sessions (session_id + project_path in state
files to prevent cross-contamination).

### 7.3 OmX's Team Worker Protocol (Full Version)

The full `$team` protocol includes: tmux pane splitting, env var injection, composed
AGENTS.md overlays, mailbox/ACK handshakes, dispatch queues, heartbeat monitoring, claim
tokens with lease expiry, and a Rust-backed state machine.

**Why this is overkill for us:**
- We have at most 2-3 parallel Executors, not 20 workers
- Our pipeline is Architect → Executor(s) → Reviewer, not an arbitrary DAG
- Worker communication is through signal files, not mailboxes
- We don't need claim tokens — zone lock files are sufficient

**What to take instead:** The pre-launch context snapshot concept (each worker gets only what
it needs), the worker commit protocol (feature branch, structured message, no direct main
commits), and the anti-spawn-loop guard (workers cannot spawn more workers).

### 7.4 Multiple Interacting State Systems

The claw-code ecosystem has state in: `.omx/state/`, `.omc/state/`, `.sisyphus/`,
`.claw/worker-state.json`, git worktrees, tmux session state, and clawhip's in-memory
registries. These systems reference each other but can disagree.

The documented "phantom completions" bug is a direct consequence: "Global session store has
no per-worktree isolation. Parallel lanes silently cross wires: one lane reports success but
the writes went to another worktree's CWD."

**Lesson for us:** One state system. `state/signals/` for everything. Agent task lifecycle,
zone claims, completion status, bot events — all in one directory hierarchy with one schema.
The orchestrator and clawhip both read from this single source of truth.

### 7.5 The Hooks Complexity

OMC registers scripts at 10 lifecycle events with multiple scripts per event. The hooks
interact: `keyword-detector` triggers skills, `pre-tool-enforcer` tracks skill state,
`persistent-mode` reads skill state, `post-tool-verifier` writes state consumed by
`persistent-mode`. Debugging a misbehaving workflow means tracing execution across 6-8
hook scripts that communicate through state files.

**What to take instead:** We need exactly 3 hooks for Claude Code sessions:
1. **Stop hook (persistent mode):** Prevent premature termination during active work
2. **PreToolUse hook (sandboxing):** Block out-of-zone file access for Executors
3. **Context monitoring:** Warn at 70% context usage, preserve state during compaction

Everything else is handled by the orchestrator script or the role prompt. Fewer hooks = fewer
interaction bugs.

---

## 8. Verification and Quality Gates

### 8.1 OMC's Verification Tiers

OMC sizes verification effort by change scope:
- Light (<5 files changed): quick check
- Standard (5-20 files): normal review
- Thorough (>20 files): comprehensive audit

The Reviewer agent (`verifier`) has specific constraints:
- Never self-approve work from the same context
- Must provide fresh test output as evidence
- Must cite file:line for every finding

### 8.2 OmX's Deslop Pass

After implementation, `$ralph` runs `ai-slop-cleaner` on all changed files. This removes:
unnecessary comments, dead code, over-verbose error messages, defensive coding that masks
bugs. Then re-runs tests to ensure cleanup didn't break anything.

**Adoption path:** Our Reviewer role prompt includes a "quality gate" section that checks
for: unnecessary comments, dead code, over-engineering beyond the plan, convention violations
(async, atomic writes, no third-party TA). The Reviewer is not optional — every Executor
output gets reviewed.

### 8.3 OmO's Deliverable Requirements

Team pipeline stages have deliverable requirements checked by the `verify-deliverables` hook:
- `team-plan` stage requires `DESIGN.md` (min 500 bytes, must contain `## File Ownership`)
- `team-verify` stage requires `QA_REPORT.md` (min 200 bytes, must contain `PASS` or `FAIL`)

**Adoption path:** Our task completion signals should include structured evidence:
- Architect completion: plan file exists with non-goals and decision boundaries sections
- Executor completion: all tests pass, branch pushed, zone file shows all units complete
- Reviewer completion: approval or structured feedback with file:line citations

---

## 9. Summary: What We Take

### Architecture (build once, use everywhere)
- Agent roles as markdown files with YAML frontmatter (model, tools, constraints)
- XML-structured agent prompts (Role, Constraints, Execution_Policy, Failure_Modes)
- Three-layer separation: workflow prompt templates / clawhip routing / orchestrator coordination
- Single state system (`state/signals/`) — no competing state stores
- Authoring and review as structurally separate passes (tool restrictions, not just prompts)

### Engineering solutions (specific problems solved)
- Persistent mode Stop hook (prevent premature termination, with safety guards)
- Context window monitoring (70% warning, 78% compaction with state preservation)
- Tool failure retry tracking (3 failures → different approach, 5 → escalate)
- Anti-duplication enforcement in orchestrator prompts
- Anti-spawn-loop guard for worker agents
- Per-task cost tracking with budget limits and intervention
- Informational intent filtering for command parsers
- Stale state detection (2-hour timeout on active states)
- Session isolation (session_id + project_path prevent cross-contamination)

### Prompts (base our role definitions on these)
- OMC's agent prompt structure (9 XML sections)
- Sisyphus's intent gate (classify before acting)
- Structured delegation format (TASK, EXPECTED OUTCOME, MUST DO, MUST NOT DO, CONTEXT)
- Deep-interview ambiguity scoring (6 dimensions, brownfield weights, readiness gates)
- Failure_Modes_To_Avoid sections (encode lessons learned from actual failures)

### clawhip (adopt directly)
- Rust daemon with TOML config
- inotifywait filesystem watching, git polling
- Discord REST API (no gateway dependency)
- Rate limiting, circuit breaker, retry, dead letter queue
- tmux session management (create, monitor, keyword detection, stale timeout)
- Thin Python companion for inbound Discord commands (~100 lines)

### What we explicitly skip
- OmO (too complex, multi-provider, OpenCode dependency)
- OmX Rust runtime (Authority/Dispatch/Mailbox/Replay — overkill for our pipeline)
- OmX full team protocol (20 workers, claim tokens, mailbox/ACK — we have 2-3 Executors)
- Hashline editing (revisit at Phase F if edit conflicts occur)
- Multiple state systems (one source of truth: `state/signals/`)
- Complex hooks (3 hooks max, not 10 events × multiple scripts)
- Multi-provider model routing (Claude only)
