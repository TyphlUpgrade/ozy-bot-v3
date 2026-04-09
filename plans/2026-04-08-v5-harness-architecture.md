# v5 Harness Architecture: Clawhip + Python Orchestrator + OMC Sessions

**Date:** 2026-04-08
**Status:** Draft (Revision 5 — caveman review fixes applied)
**Scope:** Full replacement of conductor.sh with extensible multi-session harness
**Supersedes:** `plans/2026-04-08-persistent-sessions.md` (FIFO-only plan)

## Vision

A person types a sentence in Discord on their phone and puts it down. They go make
coffee. They go to sleep. The agents read the message, break the work into tasks,
assign roles, write code, test it, argue over it, fix what fails, push when everything
passes, and document what they built. The person checks back in the morning. The work
is done.

If the agents hit something they can't resolve — an ambiguous requirement, a design
choice that needs human judgment, a security concern — they escalate to Discord. The
person sees the notification, types a response, and the agents pick up where they left
off. Full autonomy with an escape hatch.

No terminal. No IDE. No SSH. Discord.

## Architecture

```
Discord (phone)
    ↕ outbound: clawhip agent lifecycle events, escalations
    ↕ inbound:  discord_companion → orchestrator (shared process)
clawhip (Rust daemon — infrastructure layer)
    ├── launches all sessions via `clawhip tmux new`
    ├── monitors health (stale detection, keyword alerts)
    ├── routes events to Discord channels
    ├── watches signal files
    ├── manages session lifecycle (restart on death)
    ↕
Orchestrator (Python asyncio + on-demand `claude -p` subprocesses)
    ├── async poll loop: tasks, signals, operator feedback
    ├── deterministic routing: 80% of decisions (signal → next stage)
    ├── on-demand `claude -p`: 20% (complexity, disputes, summaries)
    ├── tiered escalation (architect-first for technical, operator for business)
    ├── routes operator mid-session feedback to correct agent FIFO
    ├── discord_companion runs in same process (no signal file bridge)
    ├── triggers git operations (merge, test, revert)
    ├── writes wiki documentation post-merge
    ├── emits lifecycle events via `clawhip agent`
    ↕  FIFO (send) + signal files (receive)
Session Registry (extensible — any number of sessions)
    ├── [dev] Architect    — OMC/Opus, persistent, read-only
    ├── [dev] Executor     — OMC/Sonnet, per-task (fresh worktree each task), full access
    ├── [dev] Reviewer     — OMC/Sonnet, persistent, read-only
    ├── [bot] Ops Monitor  — OMC/Haiku, persistent, read-only, always-on
    ├── [bot] Dialogue     — OMC/Sonnet, persistent, read-only, on-demand
    └── [bot] Analyst      — OMC/Sonnet, persistent, read-only, post-market
```

### Three Layers, Three Concerns

| Layer | Technology | Cost | Responsibility |
|-------|-----------|------|----------------|
| Infrastructure | clawhip (Rust) | $0 | Launch, monitor, health, Discord, restart |
| Intelligence | Python + on-demand `claude -p` | Low (judgment calls only) | Route, summarize, resolve, escalate, verify, document |
| Work | OMC sessions (Opus/Sonnet/Haiku) | Variable (actual work) | Plan, code, review, monitor, analyze |

Each layer does one thing. Clawhip never reasons. The orchestrator never writes code.
Sessions never talk to each other. The orchestrator is the only message hub.

### Why Python (not bash)

conductor.sh reached 792 lines for a simpler pipeline and became hard to debug and
iterate. v5 adds persistent sessions, tiered escalation, operator feedback, wiki,
session rotation, and health monitoring — roughly doubling complexity. At this scale:

- **Testable**: Every module gets pytest coverage matching the rest of the project
- **Typed**: Dataclasses for signal schemas, type hints throughout — no hoping jq parsed right
- **Debuggable**: Stack traces, proper exception handling — not `set -x` and `|| true`
- **Shared process**: discord_companion runs in the same asyncio loop — Discord commands
  go straight to the pipeline state machine, eliminating the signal file bridge between
  companion and orchestrator (removes an entire class of race conditions)
- **Still $0 idle**: `asyncio.sleep()` in a poll loop costs nothing
- **Familiar**: The entire Ozymandias project is Python/asyncio. Same patterns, same
  test infrastructure, same debugging tools.

The on-demand LLM calls use `asyncio.create_subprocess_exec("claude", "-p", ...)` —
same `claude -p` invocations, just managed by Python instead of bash.

### Why a Thin Bash Launcher

Bash does what it's good at: process setup. `start_harness.sh` (~50 lines) launches
clawhip, creates tmux sessions, and exec's into the Python orchestrator. No logic,
no state, no polling. Just process management.

## Project Separation

The harness is a **general-purpose multi-agent orchestration system**, not an
Ozymandias-specific tool. It must be cleanly separated so it can be reused in any
future project.

### Directory Structure

```
harness/                              # Standalone — no project-specific imports
├── start.sh                          # Thin bash launcher (~50 lines)
├── orchestrator.py                   # Entry point + async main loop (~80 lines)
├── lib/
│   ├── __init__.py
│   ├── sessions.py                   # FIFO management, session lifecycle (~150 lines)
│   ├── escalation.py                 # Tiered escalation logic (~120 lines)
│   ├── pipeline.py                   # Stage state machine (~100 lines)
│   ├── signals.py                    # Signal file I/O + dataclass schemas (~80 lines)
│   ├── claude.py                     # All claude -p calls: judgment + wiki (~100 lines)
│   └── lifecycle.py                  # Startup recovery + session health (~110 lines)
├── discord_companion.py              # Inbound Discord (moved from tools/, shared process)
├── tests/
│   ├── test_escalation.py
│   ├── test_pipeline.py
│   ├── test_sessions.py
│   ├── test_signals.py
│   └── conftest.py                   # Shared fixtures (mock FIFO, temp signal dirs)
└── README.md
                                      # ~750 lines Python total, 6 modules, none >150
config/harness/                       # Project-specific configuration
├── project.toml                      # Project paths, Discord channels, signal dirs
├── clawhip.toml.template             # Template — start.sh generates clawhip.toml from this
├── agents/                           # Agent definitions (moved from config/agent_roles/)
│   ├── architect.md
│   ├── executor.md
│   └── reviewer.md
├── commands.py                       # (optional) Project-specific Discord commands
└── sessions.toml                     # (Phase 5) Session registry
```

### Separation Rules

- **harness/** has zero imports from `ozymandias/` or any project code. It reads
  signal files and writes signal files. It doesn't know what the project does.
- **All path references** use variables from `config/harness/project.toml` from day
  one — no hardcoded project paths, not even "temporarily" in Phase 1.
- **Agent role files** reference project conventions (e.g., "use get_logger()") but
  the harness doesn't parse or understand them — it just passes them to sessions.
- **discord_companion.py** is part of the harness. Project-specific commands (like
  `!pause`, `!exit` for trading) are loaded from `config/harness/commands.py` via a
  plugin contract (see below).
- **Signal file paths** are relative to a configurable root, not hardcoded.
- **clawhip.toml** is generated from a template at startup, with paths substituted
  from `project.toml`. No hardcoded absolute paths in version control.

### Plugin Contract for Project Commands

`config/harness/commands.py` exports a dict of command handlers:

```python
# config/harness/commands.py — project-specific Discord commands
from pathlib import Path

COMMANDS: dict[str, str] = {
    "!pause": "Suppress new entries (PAUSE_ENTRIES signal)",
    "!exit": "Emergency exit all positions",
    # ...
}

async def handle_command(cmd: str, args: str, signal_dir: Path) -> str | None:
    """Dispatch project-specific commands. Return response text or None."""
    if cmd == "!pause":
        (signal_dir / "PAUSE_ENTRIES").touch()
        return "Entries paused."
    # ...
    return None
```

The orchestrator loads this at startup via `importlib.import_module()`. If the file
doesn't exist, no project commands are loaded — harness-native commands (`!tell`,
`!reply`, `!fix`, `!status`) still work.

### Reuse in a New Project

To use the harness in a different project:
1. Copy `harness/` directory (or install as a package / git submodule)
2. Create `config/harness/project.toml` with project-specific paths
3. Write agent role files in `config/harness/agents/`
4. (Optional) Add project-specific Discord commands to `config/harness/commands.py`
5. Start: `./harness/start.sh`

No changes to harness code. All project coupling is in config.

## Operator-in-the-Loop

### Tiered Escalation Protocol

Agents can escalate when they hit something they can't resolve autonomously.
Escalations are **category-aware**: technical questions go to the architect first
(cheaper, faster, no human needed for 60-70% of cases). Only business/risk/scope
questions — or architect-unresolvable technical questions — reach the operator.

**Escalation categories and routing:**

| Category | First Tier | Rationale |
|----------|-----------|-----------|
| `ambiguous_requirement` | Architect | Technical ambiguity — architect can often infer intent from codebase context |
| `design_choice` | Architect | Architecture is the architect's job |
| `persistent_failure` | Architect (if retry_count < 2) | Architect may spot root cause executor missed |
| `persistent_failure` | **Operator direct** (if retry_count >= 2) | Circular replanning risk — human judgment needed |
| `security_concern` | **Operator direct** | Human judgment required for risk acceptance |
| `cost_approval` | **Operator direct** | Human judgment required for spend decisions |
| `scope_question` | **Operator direct** | Business priority — only the operator knows |
| `permission_request` | **Operator direct** | Runtime permission grant — human authority |

**Escalation flow:**

```
Agent hits blocker
  → writes signals/escalation/$task_id.json
  → orchestrator reads category + retry_count

Category in [ambiguous_requirement, design_choice]:
  → Tier 1: Route to architect via FIFO
  → Architect responds with resolution + confidence
  → If high confidence: inject resolution into original agent, resume
  → If cannot_resolve or low confidence: promote to Tier 2 (operator)

Category == persistent_failure AND retry_count < 2:
  → Tier 1 (architect gets first shot)

Category == persistent_failure AND retry_count >= 2:
  → Skip Tier 1 — circular replanning risk

Category in [security_concern, cost_approval, scope_question, permission_request]:
  → Skip Tier 1, go directly to Tier 2 (operator)

Tier 2 (operator):
  → Post to Discord via clawhip agent blocked
  → Pipeline stage paused (state: escalation_wait)
  → Timeout + re-notify behavior based on severity
```

**Signal file schemas (Python dataclasses):**

```python
@dataclass
class EscalationRequest:
    task_id: str
    agent: str                    # who is escalating
    stage: str                    # pipeline stage when escalation occurred
    severity: str                 # "blocking" | "advisory" | "informational"
    category: str                 # routing key (see table above)
    question: str
    options: list[str]
    context: str
    retry_count: int = 0          # for persistent_failure routing threshold
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

@dataclass
class ArchitectResolution:
    task_id: str
    resolved_by: str = "architect"
    resolution: str               # chosen option or "cannot_resolve"
    reasoning: str
    confidence: str               # "high" | "low" — low auto-promotes to Tier 2
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

@dataclass
class EscalationReply:
    task_id: str
    response: str
    operator_note: str = ""
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
```

If `confidence` is `"low"` or the architect writes `"resolution": "cannot_resolve"`,
the orchestrator promotes to Tier 2 automatically. This prevents architect
rubber-stamping — the architect must be confident to unblock without human input.

**Tier 1 orchestrator handling (architect-first):**
1. Detects escalation signal file during poll loop
2. Checks category + retry_count → routes to architect if eligible
3. Injects the escalation question into architect's FIFO:
   ```python
   await session_manager.send(
       "architect",
       f"[ESCALATION from {esc.agent}] {esc.question}\nContext: {esc.context}\nOptions: {esc.options}"
   )
   ```
4. Polls `signals/escalation_resolution/$task_id.json` for architect's answer
5. If resolved with high confidence: inject resolution into original agent's FIFO, resume
6. If cannot_resolve or low confidence: promote to Tier 2

**Tier 2 orchestrator handling (operator):**
1. Posts to Discord: `clawhip agent blocked --name {agent} --summary "Needs operator: ..."`
2. Includes the full question, options, and (if Tier 1 was attempted) the architect's
   assessment of why it couldn't resolve
3. Pipeline state set to `escalation_wait`
4. Timeout: default 4 hours, configurable in `project.toml`. On timeout:
   - `blocking` → re-notify, keep waiting
   - `advisory` → agent proceeds with best guess, logs the decision

**Severity levels:**

| Severity | Pipeline Effect | Timeout Behavior |
|----------|----------------|------------------|
| `blocking` | Stage paused until response | Re-notify every 4 hours (configurable) |
| `advisory` | Stage continues, response welcomed | Agent proceeds with best guess |
| `informational` | No pause, FYI to operator | No timeout, no re-notify |

**Audit trail:** Every escalation — whether resolved by architect or operator — is
logged to the JSONL audit log with: category, tier reached, resolver, resolution,
and time-to-resolution. This data shows which categories the architect handles well
and which consistently need operator input, informing future routing adjustments.

### Operator Mid-Session Feedback

The operator can send feedback to any active agent session at any time, without
waiting for an escalation. This enables course correction while agents are working.

**Discord commands (harness-native):**

```
!tell <agent> <message>     — Send feedback to a specific agent
!tell executor stop using print statements, use get_logger()
!tell reviewer focus on the error handling, the rest looks fine
!tell architect keep it simple, no new abstractions

!reply <task_id> <response> — Reply to an escalation
!reply task-20260408T1530 structured_json_all
!reply task-20260408T1530 just the fill handler, and add to position_manager too
```

**Because discord_companion shares the orchestrator process**, `!tell` and `!reply`
are method calls, not signal files:

```python
# In discord_companion's on_message handler:
if cmd == "!tell":
    agent, message = parse_tell(args)
    await orchestrator.inject_feedback(agent, message)
    return f"Feedback sent to {agent}."

if cmd == "!reply":
    task_id, response = parse_reply(args)
    await orchestrator.handle_escalation_reply(task_id, response)
    return f"Reply sent for {task_id}."
```

No signal file intermediate. No polling delay. Direct method call.

For agents that escalate via signal files (since they can't call the orchestrator
directly), the orchestrator still polls `signals/escalation/`. But Discord→orchestrator
communication is in-process.

**Why this works with persistent sessions:** Because agents are persistent stream-json
sessions, operator feedback arrives as a new turn in the agent's conversation. The
agent has full context from prior turns and can incorporate the feedback naturally.
With single-shot `claude -p`, this would be impossible — the agent has no memory.

## Communication Protocol

### Orchestrator → Session (stream-json FIFO)

Proven protocol from Phase 29 investigation, managed by `lib/sessions.py`:

```python
class SessionManager:
    """Manages persistent OMC sessions via FIFO + stream-json."""

    def __init__(self, session_dir: Path, config: ProjectConfig):
        self.sessions: dict[str, Session] = {}
        self.session_dir = session_dir
        self.config = config

    async def launch(self, name: str, agent_def: AgentDef) -> None:
        """Launch a persistent session via clawhip tmux new."""
        fifo_path = self.session_dir / f"{name}.fifo"
        log_path = self.session_dir / f"{name}.log"

        # Clean up stale FIFO and file handles (crash recovery)
        fifo_path.unlink(missing_ok=True)
        os.mkfifo(fifo_path)

        # Launch via clawhip tmux new. The tmux shell's `< fifo` redirection
        # blocks in the tmux pane's process, NOT in our event loop. This avoids
        # the FIFO open deadlock (POSIX: read-end open blocks until writer exists).
        cmd = (
            f"claude -p --verbose"
            f" --input-format stream-json --output-format stream-json"
            f" --permission-mode dontAsk {agent_def.deny_flags_str}"
            f" --model {agent_def.model}"
            f" --include-hook-events"
            f" < '{fifo_path}' > '{log_path}' 2>&1"
        )
        await asyncio.create_subprocess_exec(
            "clawhip", "tmux", "new",
            "--session", f"agent-{name}",
            "-n", name,                    # window name for tmux observability
            "--stale-minutes", str(agent_def.stale_minutes),
            "--keywords", agent_def.keywords,
            "--channel", agent_def.discord_channel,
            "--", "bash", "-c", cmd,
        )

        # clawhip tmux new returns immediately (async tmux launch).
        # Brief wait for the tmux pane to open the read end of the FIFO.
        await asyncio.sleep(0.5)

        # Now safe to open write end — reader exists in the tmux pane.
        fd = os.open(str(fifo_path), os.O_WRONLY)
        self.sessions[name] = Session(
            name=name, fd=fd, fifo=fifo_path, log=log_path,
        )

    async def send(self, name: str, content: str) -> None:
        """Send a message to a session via its FIFO."""
        session = self.sessions[name]
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": content}
        })
        payload = (msg + "\n").encode()
        try:
            os.write(session.fd, payload)
        except BlockingIOError:
            logger.warning(f"FIFO buffer full for {name}, restarting session")
            await self.restart(name)
            # Re-send after restart (session has fresh buffer)
            os.write(self.sessions[name].fd, payload)

    async def restart(self, name: str) -> None:
        """Restart a dead session. Cleans up old FIFO, launches fresh."""
        session = self.sessions.pop(name, None)
        if session:
            try:
                os.close(session.fd)
            except OSError:
                pass
            # Kill the tmux session (which kills the claude process)
            await asyncio.create_subprocess_exec(
                "tmux", "kill-session", "-t", f"agent-{name}",
            )
            await asyncio.sleep(1)
            session.fifo.unlink(missing_ok=True)
        await self.launch(name, self.config.agents[name])
```

Message format: `{"type":"user","message":{"role":"user","content":"<prompt>"}}` — type
is `"user"`, NOT `"user_message"` (documented in Phase 29 findings).

### Session → Orchestrator (signal files)

Sessions write signal files when they complete a stage:
- Architect: `signals/architect/$task_id/plan.json`
- Executor: `signals/executor/completion-$task_id.json`
- Reviewer: `signals/reviewer/$task_id/verdict.json`
- Any agent: `signals/escalation/$task_id.json` (escalation request)

Orchestrator polls for these via `lib/signals.py` each cycle.

### Orchestrator → Discord (via clawhip)

**Note:** `clawhip agent` may be an automatic event emitted by tmux session
monitoring, not a CLI subcommand. Verify during Phase 1 implementation. If
`clawhip agent` doesn't exist as a CLI command, use the fallback: write a
signal file to a clawhip-watched directory (the proven pattern from
`clawhip.toml` routes).

```python
async def notify(self, event: str, agent: str, summary: str) -> None:
    """Post lifecycle event to Discord. Falls back to signal file if CLI fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "clawhip", "agent", event,
            "--name", agent, "--summary", summary,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.warning(f"clawhip agent failed: {stderr.decode()}")
            await self._notify_via_signal_file(event, agent, summary)
    except (asyncio.TimeoutError, FileNotFoundError):
        await self._notify_via_signal_file(event, agent, summary)
```

### Discord → Orchestrator (in-process)

discord_companion runs in the same asyncio event loop. Commands that affect the
pipeline (`!tell`, `!reply`, `!fix`) call orchestrator methods directly. No signal
file bridge, no polling latency.

Project-specific commands (`!pause`, `!exit`, etc.) are loaded from
`config/harness/commands.py` and dispatched separately — they write signal files
to the project's state directory, which the project (not the harness) reads.

## Recovery Protocol

Adapted from conductor.sh (lines 664-675), implemented in `lib/lifecycle.py`:

```python
async def reconcile(state: PipelineState, session_mgr: SessionManager) -> None:
    """Reconcile pipeline state after crash/restart."""
    if state.active_task:
        # Re-send pending escalation to Discord
        if state.stage == "escalation_wait":
            esc = await signals.read_escalation(state.active_task)
            if esc:
                await notify("blocked", esc.agent, f"ESCALATION (re-sent): {esc.question}")

        # Check worktree existence
        if state.worktree and not state.worktree.exists():
            logger.warning(f"Worktree missing for {state.active_task}, clearing")
            state.clear_active()
        else:
            logger.info(f"Resuming {state.active_task} at stage {state.stage}")

    # Verify all sessions alive, restart dead ones
    for name, session in list(session_mgr.sessions.items()):
        if not is_alive(session.pid):
            logger.warning(f"Session {name} dead, restarting")
            await session_mgr.restart(name)
            # Re-send current task context if this session was active
            if state.active_task and state.stage_agent == name:
                await session_mgr.send(name, build_reinit_prompt(state))
```

### Session Death Recovery

When a persistent session dies mid-task:
1. Clawhip detects stale session → alerts Discord
2. Orchestrator detects via `is_alive(pid)` on next health check
3. `session_mgr.restart()`: kill old process, wait, remove FIFO, recreate, relaunch
4. Re-sends init message + current task context
5. **Context is lost** — the trade-off of persistent sessions. The orchestrator
   re-sends the original task + any prior stage outputs as first message.
6. Logs restart for audit trail

### Permission Proxy Removal

The Phase 29 permission proxy (MCP-based `request_permission` / `approve_permission`)
is **removed**. `--permission-mode dontAsk` + `--disallowedTools` gives agents full
MCP access minus the explicitly denied tools (Phase 29 finding).

## Wiki Documentation Step

After every successful merge, the orchestrator documents the work via `claude -p`
with the `/wiki` skill. Implemented in `lib/claude.py`:

```python
# Default timeouts for claude -p subprocess calls
CLASSIFY_TIMEOUT = 120    # 2 min — simple routing decision
SUMMARIZE_TIMEOUT = 120   # 2 min — context compression
REFORMULATE_TIMEOUT = 120 # 2 min — rejection reformulation
WIKI_TIMEOUT = 300        # 5 min — wiki write may be longer

async def _run_claude(system_prompt: str, user_prompt: str,
                      timeout: int, tools: str | None = None) -> str | None:
    """Run a claude -p subprocess with timeout. Returns stdout or None on failure."""
    cmd = ["claude", "-p", "--permission-mode", "dontAsk"]
    if tools:
        cmd.extend(["--allowedTools", tools])
    cmd.append(system_prompt)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(user_prompt.encode()), timeout=timeout
        )
        return stdout.decode() if proc.returncode == 0 else None
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning(f"claude -p timed out after {timeout}s")
        return None

async def classify(task_description: str) -> str:
    """Classify task complexity. Returns 'complex' or 'simple'."""
    result = await _run_claude(
        "Classify this task as 'complex' or 'simple'. Respond with JSON.",
        task_description, timeout=CLASSIFY_TIMEOUT,
    )
    # Parse result; default to "complex" on failure (safe fallback)
    ...

async def document_task(task_id: str, description: str, plan_summary: str,
                        diff_stat: str, review_verdict: str) -> bool:
    """Write a wiki entry for a completed task via claude -p."""
    prompt = f"""Write a wiki entry for this completed task:
Task: {task_id} — {description}
Plan summary: {plan_summary}
Changes: {diff_stat}
Review verdict: {review_verdict}"""
    result = await _run_claude(
        "You are a documentation writer. Use /wiki to document this completed task.",
        prompt, timeout=WIKI_TIMEOUT,
    )
    return result is not None
```

**OMC hook dependency:** The `/wiki` skill requires OMC hooks to fire in the
`claude -p` session. If OMC hooks don't fire (e.g., different environment), the
wiki step fails gracefully and logs a warning — it doesn't block the pipeline.

## Orchestrator Design

### What It Is

A Python asyncio application (`orchestrator.py`, ~80 lines) that imports modules
from `lib/`. Launched by `start.sh` after clawhip and tmux sessions are up.

No file exceeds 150 lines. Total: ~750 lines of Python across 9 modules + tests.

### Main Loop

```python
async def main_loop(config: ProjectConfig) -> None:
    state = PipelineState.load(config.state_file)
    session_mgr = SessionManager(config.session_dir, config)
    signal_reader = SignalReader(config.signal_dir)
    pending_mutations: list[Callable] = []  # concurrency discipline (see below)

    # Launch persistent sessions (architect, reviewer). Executor is per-task.
    for name, agent_def in config.agents.items():
        if agent_def.auto_start and agent_def.lifecycle == "persistent":
            await session_mgr.launch(name, agent_def)

    # Reconcile after crash
    await lifecycle.reconcile(state, session_mgr)

    # Start discord companion in same event loop.
    # Uses client.start() (not client.run()) to coexist in our asyncio loop.
    # Mutations from !tell/!reply are queued in pending_mutations, not applied
    # directly — prevents state corruption between await points.
    companion = DiscordCompanion(config, pending_mutations)
    asyncio.create_task(companion.start())

    while True:
        # 0. Apply pending mutations from Discord (concurrency-safe)
        for mutation in pending_mutations:
            await mutation(state, session_mgr)
        pending_mutations.clear()

        # 1. Check pipeline progress
        if state.active_task:
            match state.stage:
                case "classify":          await classify_task(state, session_mgr)
                case "architect":         await check_stage(state, signal_reader, "architect")
                case "executor":          await check_stage(state, signal_reader, "executor")
                case "reviewer":          await check_reviewer(state, signal_reader, session_mgr)
                case "merge":             await do_merge(state, config)
                case "wiki":              await do_wiki(state)
                case "escalation_tier1":  await check_architect_resolution(state, signal_reader)
                case "escalation_wait":   await check_operator_reply(state, signal_reader)
        else:
            # Check for new tasks
            new_task = await signal_reader.next_task(config.task_dir)
            if new_task:
                state.activate(new_task)
                # Executor is per-task: launch with fresh worktree as cwd
                if "executor" not in session_mgr.sessions:
                    worktree = await create_worktree(new_task, config)
                    executor_def = config.agents["executor"].with_cwd(worktree)
                    await session_mgr.launch("executor", executor_def)

        # 2. Health checks (persistent sessions only; executor checked via signal)
        await lifecycle.check_sessions(session_mgr, state)

        # 3. Heartbeat
        state.heartbeat()
        state.save()

        await asyncio.sleep(config.poll_interval)
```

**Concurrency discipline:** discord_companion's `on_message` callbacks run between
`await` points in the main loop. To prevent state corruption, `!tell` and `!reply`
don't mutate `PipelineState` directly — they append lambdas to `pending_mutations`.
The main loop applies them once per cycle at the top (step 0), matching the trading
bot's fast loop pattern where mutations are batched, not interleaved.

**Executor lifecycle:** The executor is **per-task, not persistent**. When a new task
arrives, the orchestrator creates a git worktree and launches a fresh executor session
with that worktree as cwd. After merge (or failure), the executor session is killed
and the worktree cleaned up. Architect and reviewer persist across tasks — they're
read-only and don't need per-task isolation.

### What It Does

1. **Task intake**: Polls `agent_tasks/` for new tasks (from Discord or bot agents)
2. **Route decision**: `claude.classify()` judges complexity via `claude -p`
3. **Context assembly**: Builds prompts for each session with relevant context only
4. **Dispatch**: Sends messages to sessions via FIFO (`session_mgr.send()`)
5. **Completion detection**: Polls signal files (`signal_reader`)
6. **Context transfer**: Reads result, summarizes via `claude.summarize()`, builds next prompt
7. **Tiered escalation**: Technical → architect (Tier 1). Unresolved + business → operator (Tier 2)
8. **Operator feedback**: `!tell` / `!reply` from Discord → direct method call → FIFO injection
9. **Dispute resolution**: Reviewer rejects → `claude.reformulate()` → executor retry
10. **Verification loop**: Executor ↔ reviewer, max 3 retries
11. **Merge**: `git merge --no-ff`, pytest, revert on failure
12. **Wiki**: Post-merge `claude -p` with `/wiki` skill
13. **Discord**: Via `clawhip agent` lifecycle events
14. **Health**: `lifecycle.check_sessions()` verifies sessions alive, restarts dead ones

### What It Does NOT Do

- Write code
- Read code (beyond signal file content and git diff stats)
- Maintain persistent LLM context (all state in `PipelineState` JSON file)
- Spawn OMC subagents or use OMC skills (except wiki via `claude -p`)

### Cost Controls

- **Orchestrator**: $0 when idle. On-demand `claude -p` only for judgment calls.
- **Model routing**: Haiku for monitors, Sonnet for workers, Opus only for architect
- **Caveman**: Per-agent compression levels (`project.toml [caveman]`), `caveman-compress` for input tokens, specialized skills for commits/reviews. See **Output Compression** section.
- **Context summarization**: Forwards summaries, not raw content. A 500-line diff
  becomes "changed 3 files in core/: added retry logic to fill handler, updated
  tests, fixed type annotation"
- **Session rotation**: When agent session token usage (parsed from stream-json output
  in session log) exceeds threshold, `session_mgr.restart()` with re-injected context.
- **Deterministic shortcuts**: Simple routing (plan appeared → send to executor) is
  a Python `match` branch. No LLM call needed.

## Startup Sequence

### `start.sh` — Thin Bash Launcher (~50 lines)

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 1. Launch clawhip (if not already running)
if ! pgrep -f "clawhip.*start" >/dev/null 2>&1; then
  # Generate clawhip.toml from template with project paths
  envsubst < "$PROJECT_ROOT/config/harness/clawhip.toml.template" \
    > "$PROJECT_ROOT/clawhip.toml"
  clawhip start --config "$PROJECT_ROOT/clawhip.toml"
fi

# 2. Restart loop — if orchestrator dies, restart it
while true; do
  echo "[harness] Starting orchestrator..."
  python3 "$SCRIPT_DIR/orchestrator.py" \
    --config "$PROJECT_ROOT/config/harness/project.toml" \
    || true

  echo "[harness] Orchestrator exited. Restarting in 5s..."
  sleep 5
done
```

### Orchestrator Startup (Python)

```python
# orchestrator.py
async def main():
    config = ProjectConfig.load(args.config)
    await main_loop(config)

if __name__ == "__main__":
    asyncio.run(main())
```

Session launches happen inside `main_loop` via `session_mgr.launch()`.

## Shutdown Sequence

```python
async def shutdown(session_mgr: SessionManager, state: PipelineState) -> None:
    """Graceful shutdown on SIGTERM/SIGINT."""
    # 1. Close all FIFOs (sends EOF → sessions exit)
    for session in list(session_mgr.sessions.values()):
        try:
            os.close(session.fd)
        except OSError:
            pass

    # 2. Wait for sessions to exit
    await asyncio.sleep(5)

    # 3. Clean up FIFO files
    for session in list(session_mgr.sessions.values()):
        session.fifo.unlink(missing_ok=True)

    # 4. Final status
    await notify("finished", "orchestrator", "Harness shut down gracefully")
    state.shutdown_ts = datetime.now(UTC).isoformat()
    state.save()
```

**Signal file cleanup:** Processed signal files are moved to `logs/signals/` after
each task completes (merge or failure). This prevents unbounded growth in the active
signal directories.

## Extensibility

### Phase 1 Hardcoded, Phase 5 Configurable

Agent definitions are hardcoded as Python dataclasses in Phase 1:

```python
# Phase 1: hardcoded agent definitions
DEFAULT_AGENTS = {
    "architect": AgentDef(model="opus", stale_minutes=15, mode="read-only", ...),
    "executor": AgentDef(model="sonnet", stale_minutes=30, mode="full", ...),
    "reviewer": AgentDef(model="sonnet", stale_minutes=15, mode="read-only", ...),
}
```

Phase 5: Read from `config/harness/sessions.toml` via `tomllib`. Same `AgentDef`
dataclass, different source. No behavioral change.

### Extensibility Scenarios

**Add a "Designer" agent:**
Phase 1: Add entry to `DEFAULT_AGENTS` dict + role file. One line of Python.
Phase 5: Add `[sessions.designer]` to sessions.toml.

**New OMC version adds `/deep-review` skill:**
All sessions automatically get it. No harness changes.

**New clawhip version adds route actions (shell command triggers):**
Update clawhip.toml template. Potentially replace polling with event-driven.

**Ops monitor detects a recurring bug:**
Writes `agent_tasks/bug-auto-xxx.json`. Orchestrator picks it up. Same pipeline.

**Operator sees agent going wrong:**
Types `!tell executor stop, the bug is in fill_handler not position_sync`.
Direct method call → FIFO injection. Agent course-corrects with full context.

## Output Compression (Caveman)

Token efficiency is a first-class concern in a multi-agent pipeline. Every token saved
on agent prose is a token available for reasoning, context, and code. The caveman
plugin provides configurable output compression at multiple levels, plus specialized
skills for commits, reviews, and input token reduction.

**Empirical basis:** The caveman plugin ships with evals (`evals/`) that measure
real token compression across 10 dev questions under three arms: no system prompt
(baseline), "Answer concisely" (terse), and caveman SKILL.md (caveman). The honest
delta is caveman vs. terse — isolating the skill's contribution from generic brevity.
Results are snapshot-committed for CI reproducibility. Run `uv run --with tiktoken
python evals/measure.py` in the caveman repo to see current numbers.

### Per-Agent Configuration

Not all agents benefit equally from compression. An architect reasoning about trade-offs
needs full articulation. An executor writing code needs compressed prose around unchanged
code blocks. The orchestrator's `claude -p` judgment calls are ephemeral and benefit most
from aggressive compression.

**Default configuration in `config/harness/project.toml`:**

```toml
[caveman]
# Global default — applies to any agent without an explicit override
default_level = "full"

# Per-agent overrides
[caveman.agents]
architect = "off"          # Needs full reasoning quality for design decisions
executor = "full"          # Prose compressed, code untouched
reviewer = "lite"          # Professional but tight — verdicts must be clear
ops_monitor = "ultra"      # Terse status reports, save tokens on long-running session
dialogue = "off"           # User-facing responses need natural language
analyst = "lite"           # Analysis needs clarity, but no filler

# Orchestrator claude -p calls (ephemeral, never user-facing)
[caveman.orchestrator]
classify = "ultra"         # One-word answer wrapped in JSON — compress everything else
summarize = "ultra"        # Internal context transfer, nobody reads this
reformulate = "full"       # Executor needs to understand the reformulation
wiki = "off"               # Documentation must be readable by humans

# Wenyan mode (classical Chinese token compression)
# Experimental — dramatically reduces token count but may reduce accuracy
# on non-Chinese-aware models. Keep off until benchmarked.
[caveman.wenyan]
enabled = false
default_level = "lite"     # wenyan-lite if enabled globally
# Per-agent wenyan overrides (only used if wenyan.enabled = true)
# agents.architect = "off"
# agents.executor = "wenyan-full"
```

**Compression levels (from caveman plugin):**

| Level | Style | Use Case |
|-------|-------|----------|
| `off` | No compression | Architect reasoning, wiki docs, user-facing dialogue |
| `lite` | No filler/hedging. Full sentences. Professional. | Reviewer verdicts, analyst reports |
| `full` | Drop articles, fragments OK, short synonyms. | Executor prose, general agents |
| `ultra` | Abbreviations, arrows for causality, stripped conjunctions. | Orchestrator internals, monitors |
| `wenyan-lite` | Classical Chinese structure, modern vocab | Experimental |
| `wenyan-full` | Full classical Chinese compression | Experimental |
| `wenyan-ultra` | Maximal classical Chinese compression | Experimental |

### Injection Mechanism

Caveman is activated via a directive block prepended to the agent's system prompt
(first FIFO message). The directive is **not** hardcoded — it's read from the caveman
plugin's `SKILL.md` at startup and parameterized with the agent's configured level.

**`CAVEMAN_DIRECTIVES` construction:** At startup, `sessions.py` reads the caveman
`SKILL.md` (path configurable, default `~/.claude/plugins/marketplaces/caveman/caveman/SKILL.md`).
The SKILL.md contains the full directive with level descriptions. The `CAVEMAN_DIRECTIVES`
dict maps level strings to the directive body with `CAVEMAN_LEVEL` substituted, matching
the existing conductor.sh pattern (`sed "s/CAVEMAN_LEVEL/$level/g"`). Wenyan levels
use the same SKILL.md — they're listed in the intensity table alongside standard levels.

```python
# In lib/sessions.py — startup, load once
def _load_caveman_directives(skill_path: Path) -> dict[str, str]:
    """Load caveman SKILL.md and build per-level directive dict."""
    template = skill_path.read_text()
    # Strip YAML frontmatter
    if template.startswith("---"):
        _, _, template = template.split("---", 2)
    return {
        level: template.replace("CAVEMAN_LEVEL", level).strip()
        for level in ("lite", "full", "ultra",
                      "wenyan-lite", "wenyan-full", "wenyan-ultra")
    }

CAVEMAN_DIRECTIVES: dict[str, str] = {}  # populated in SessionManager.__init__()

# In launch() — builds the init message
async def launch(self, name: str, agent_def: AgentDef) -> None:
    # ... FIFO setup, clawhip tmux new ...

    # Build init message with caveman directive if configured
    init_parts = [agent_def.role_content]
    caveman_level = self.config.caveman.level_for(name)
    if caveman_level != "off":
        directive = CAVEMAN_DIRECTIVES[caveman_level]
        init_parts.insert(0, directive)

    await self.send(name, "\n\n".join(init_parts))
```

**`inject_caveman_update()`** — called by `!caveman <agent> <level>` at runtime.
Sends a directive-update message to the agent's FIFO as a new conversation turn.
The agent sees it as operator guidance mid-conversation. This matches the `!tell`
pattern — it's a FIFO injection, not a session restart.

```python
async def inject_caveman_update(self, name: str, level: str) -> None:
    """Send updated caveman directive to a running session."""
    if level == "off":
        await self.send(name, "[SYSTEM] Caveman mode disabled. Resume normal output.")
    else:
        directive = CAVEMAN_DIRECTIVES[level]
        await self.send(name, f"[SYSTEM] Update compression level to {level}.\n\n{directive}")
```

**Limitation:** Mid-conversation directive changes rely on the agent honoring the
new instruction. LLMs generally do, but it's not guaranteed — especially if the
agent is mid-generation. For guaranteed level changes, restart the session
(`!caveman <agent> <level> --restart`). The default (FIFO injection) is
non-disruptive and sufficient for experimentation.

For orchestrator `claude -p` calls, the caveman directive is prepended to the system
prompt string:

```python
# In lib/claude.py
async def _run_claude(system_prompt: str, user_prompt: str,
                      timeout: int, call_type: str = "classify",
                      tools: str | None = None) -> str | None:
    caveman_level = config.caveman.orchestrator.get(call_type, "ultra")
    if caveman_level != "off":
        system_prompt = f"{CAVEMAN_DIRECTIVES[caveman_level]}\n\n{system_prompt}"
    # ... subprocess exec ...
```

### Specialized Skills

Three caveman skills integrate at specific pipeline points:

**caveman-commit** (executor only): After executor completes code changes, commit
messages use conventional commits format with ≤50 char subjects. Activated by including
the skill directive in the executor's role file.

```toml
# In project.toml
[caveman.skills]
commit = true              # Executor uses caveman-commit for git commits
review = true              # Reviewer uses caveman-review for verdicts
compress = true            # Input compression for CLAUDE.md and role files
```

**caveman-review** (reviewer only): Review output uses one-line format per finding:
`L42: 🔴 bug: user null. Add guard.` — Severity-rated, line-referenced, minimal prose.
Dramatically reduces reviewer output tokens while preserving all actionable information.

**caveman-compress** (input side — preprocessing step, NOT inline):

caveman-compress is **LLM-based**, not a regex transformer. It calls `claude -p`
(or Anthropic API directly) to compress prose in markdown files, then validates the
output with deterministic checks (headings preserved, code blocks exact, URLs intact,
file paths present). If validation fails, it calls Claude again with a targeted fix
prompt (up to 2 retries). On final failure, it restores the original.

The compressed file **overwrites the original on disk**; the original is backed up
as `FILE.original.md`. This means compression is a **one-time preprocessing step**,
not an inline transformation on every session launch.

**Integration in the harness:**

```python
# In start.sh or orchestrator startup — run ONCE, not per-session
# Compresses CLAUDE.md and role files before any sessions launch.
# Compressed files persist on disk until manually reverted.
async def precompress_inputs(config: ProjectConfig) -> None:
    """Run caveman-compress on configured input files. One-time preprocessing."""
    if not config.caveman.skills.compress:
        return
    compress_script = Path(config.caveman.compress_script)  # path to caveman-compress/scripts/
    for filepath in config.caveman.compress_targets:
        # Skip if already compressed (backup exists)
        if filepath.with_name(filepath.stem + ".original.md").exists():
            logger.info(f"Already compressed: {filepath}")
            continue
        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", "scripts", str(filepath),
            cwd=str(compress_script),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            logger.info(f"Compressed: {filepath}")
        else:
            logger.warning(f"Compress failed for {filepath}: {stderr.decode()}")
```

```toml
# In project.toml
[caveman.skills]
commit = true              # Executor uses caveman-commit for git commits
review = true              # Reviewer uses caveman-review for verdicts
compress = true            # Pre-compress CLAUDE.md and role files (LLM-based, run once)

# Files to compress (absolute paths resolved from project root at startup)
compress_targets = [
    "CLAUDE.md",
    "config/harness/agents/architect.md",
    "config/harness/agents/executor.md",
    "config/harness/agents/reviewer.md",
]
compress_script = "~/.claude/plugins/marketplaces/caveman/caveman-compress"
```

**Key properties of caveman-compress:**
- LLM call per file (~2-5s each, Sonnet by default, configurable via `CAVEMAN_MODEL`)
- Deterministic validation: headings count match, code blocks byte-identical, all URLs
  preserved, file paths preserved, bullet count within 15%
- Original backed up as `.original.md` — `!caveman decompress` restores originals
- Only compresses `.md`/`.txt` files; skips code/config via `detect.py` heuristics
- Idempotent: skips files that already have a `.original.md` backup
- Runs at orchestrator startup, NOT per-session or per-rotation. Sessions read the
  already-compressed files from disk. Session rotation re-reads from disk (gets the
  compressed version — no re-compression needed).

### Auto-Clarity

Caveman compression automatically disengages for:

- **Security warnings** — full clarity required for risk communication
- **Irreversible action confirmations** — operator must understand exactly what happens
- **Escalation questions** — ambiguity in an escalation defeats its purpose
- **Error diagnostics** — compressed error descriptions lose critical debug context

This is built into the caveman directive itself (the "Auto-Clarity" section in
`caveman/SKILL.md`). No orchestrator logic needed — the agent self-governs.

**Known limitation:** Auto-Clarity relies entirely on the LLM honoring the directive.
If an agent compresses a security warning despite the directive, the harness has no
enforcement mechanism. Mitigation: the orchestrator's own escalation messages (Tier 2
Discord posts) are composed by Python code, not by the agent — those are always
full-verbosity regardless of caveman level. Agent-authored escalation signal files
are the risk surface; review these during early operation to verify Auto-Clarity works.

### Runtime Control

The operator can change caveman levels at runtime via Discord:

```
!caveman <agent> <level>   — Change an agent's compression level
!caveman executor ultra    — Make executor maximally terse
!caveman architect lite    — Give architect light compression
!caveman all off           — Disable compression everywhere (debugging)
!caveman reset             — Restore project.toml defaults
!caveman status            — Show current levels for all agents
```

Implementation in `discord_companion.py`:

```python
VALID_LEVELS = {"off", "lite", "full", "ultra", "wenyan-lite", "wenyan-full", "wenyan-ultra"}

if cmd == "!caveman":
    agent, level = parse_caveman(args)
    if agent == "status":
        return format_caveman_status(config.caveman)
    if agent == "reset":
        config.caveman.reset_to_defaults()
        return "Caveman levels reset to project.toml defaults."
    # Validate level
    if level and level not in VALID_LEVELS:
        return f"Unknown level '{level}'. Valid: {', '.join(sorted(VALID_LEVELS))}"
    if agent == "all":
        config.caveman.set_all(level)
        return f"All agents set to caveman {level}."
    # Validate agent name
    if agent not in config.agents and agent not in ("all", "status", "reset"):
        return f"Unknown agent '{agent}'. Active: {', '.join(config.agents.keys())}"
    config.caveman.set_agent(agent, level)
    # Inject updated directive into running session.
    # NOTE: Use default-argument binding (a=agent, l=level) to avoid Python's
    # late-binding closure bug — without it, multiple !caveman commands queued
    # in one poll cycle would all use the last values of agent/level.
    pending_mutations.append(
        lambda s, sm, a=agent, l=level: sm.inject_caveman_update(a, l)
    )
    return f"{agent} caveman level → {level}."
```

**Convention:** All `pending_mutations` lambdas must use default-argument binding
(`a=agent` not bare `agent`) to capture values by value, not by reference. This
applies to `!caveman`, `!tell`, `!reply`, and any future Discord command that
queues a mutation.

**Backward compatibility:** The current `discord_companion.py` implements `!caveman
[level]` as a global toggle. v5 changes this to `!caveman <agent> <level>`. For
backward compat, `parse_caveman()` treats a single argument as `!caveman all <level>`:

```python
def parse_caveman(args: str) -> tuple[str, str]:
    parts = args.strip().split(maxsplit=1)
    if len(parts) == 1 and parts[0] in VALID_LEVELS:
        return ("all", parts[0])  # backward compat: !caveman full → !caveman all full
    if len(parts) == 1:
        return (parts[0], "")     # !caveman status, !caveman reset
    return (parts[0], parts[1])
```

**Runtime changes are ephemeral** — they last until session restart or orchestrator
restart. `project.toml` is the source of truth. This is intentional: runtime overrides
are for experimentation, not permanent config changes. On session rotation (restart
due to token threshold), the session gets the `project.toml` default, not the runtime
override. This is a deliberate design choice — if an experimental level causes problems,
rotation auto-reverts to the known-good default.

### Why Configurable

Caveman levels will change as we learn what works. Early experiments may reveal that:
- Architect actually benefits from `lite` (less filler in design docs)
- `ultra` degrades executor code quality (too terse in comments)
- `wenyan-full` is surprisingly effective for monitors (or completely useless)
- Input compression (`caveman-compress`) causes information loss in specific sections

Every setting is a knob in `project.toml`. No code changes needed to experiment.
The `!caveman` command enables mid-session A/B testing without restarting the pipeline.

## Acceptance Criteria

### Core Pipeline
- [ ] Architect + reviewer launch as persistent sessions via `clawhip tmux new -s`
- [ ] Executor launches per-task with fresh worktree as cwd, killed after merge/failure
- [ ] Orchestrator (Python) routes tasks through dev pipeline (architect → executor → reviewer)
- [ ] `claude.classify()` classifies task complexity via `claude -p` — complex → architect, simple → executor direct
- [ ] All `claude -p` subprocess calls have timeout protection (120s judgment, 300s wiki)
- [ ] Reviewer rejection triggers executor retry with reformulated feedback
- [ ] Max 3 rejection retries before task is marked failed
- [ ] Signal file bus carries all agent→orchestrator communication

### Operator Loop
- [ ] Agent escalation writes `signals/escalation/$task_id.json` with category + retry_count + stage
- [ ] Technical categories route to architect first (Tier 1) when retry_count < 2
- [ ] Architect resolution with high confidence unblocks agent without operator
- [ ] Architect cannot_resolve or low confidence promotes to Tier 2
- [ ] persistent_failure with retry_count >= 2 skips Tier 1 (anti-circular-replanning)
- [ ] Operator-direct categories (security, cost, scope) skip Tier 1
- [ ] Tier 2 posts to Discord with question + options + architect assessment
- [ ] `!reply <task_id>` resumes blocked agent (direct method call, no signal file)
- [ ] Blocking escalations re-notify every 4 hours (configurable) until operator responds
- [ ] Advisory escalations let agent proceed with best guess after timeout
- [ ] `!tell <agent>` injects feedback into agent's persistent session (direct method call)
- [ ] All escalations logged with category, tier, resolver, time-to-resolution

### Project Separation
- [ ] `harness/` directory contains all orchestration code — zero project imports
- [ ] All paths from `config/harness/project.toml` — no hardcoded project paths in harness/
- [ ] `clawhip.toml` generated from template at startup (no hardcoded absolute paths)
- [ ] discord_companion in same process — `!tell`/`!reply` are method calls, not signal files
- [ ] Project-specific Discord commands loaded from `config/harness/commands.py` plugin
- [ ] Agent role files in `config/harness/agents/`

### Documentation & Recovery
- [ ] Successful merge triggers wiki documentation via `claude -p` + `/wiki`
- [ ] Failed tasks documented in wiki with failure reason
- [ ] Startup reconciliation recovers pipeline state after crash (including pending escalations)
- [ ] Session death detected and restarted (FIFO cleanup → relaunch → context re-injection)
- [ ] Orchestrator death detected by `start.sh` wrapper and restarted

### Observability
- [ ] Discord shows full pipeline trail (task accepted, stages, escalations, completion/failure)
- [ ] All orchestrator decisions logged to JSONL audit log (structured via Python logging)
- [ ] `clawhip agent` lifecycle events for every state transition
- [ ] Session token usage parsed from stream-json output for rotation decisions

### Testing
- [ ] `test_escalation.py` — category routing, tier promotion, retry threshold, confidence gating
- [ ] `test_pipeline.py` — stage transitions, retry limits, merge/revert
- [ ] `test_sessions.py` — FIFO lifecycle, restart, send, health check
- [ ] `test_signals.py` — schema validation, read/write round-trip
- [ ] All tests use pytest, mock subprocess for `claude -p` and `clawhip` calls

### Output Compression (Caveman)
- [ ] `CAVEMAN_DIRECTIVES` dict loaded from caveman `SKILL.md` at `SessionManager.__init__()`
- [ ] Per-agent caveman levels read from `config/harness/project.toml` `[caveman]` section
- [ ] Invalid levels in `project.toml` rejected at startup with clear error (valid: off/lite/full/ultra/wenyan-*)
- [ ] Caveman directive prepended to agent init message in `sessions.py launch()`
- [ ] Orchestrator `claude -p` calls use per-call-type caveman levels from `[caveman.orchestrator]`
- [ ] `caveman-compress` runs at orchestrator startup (LLM-based, one-time preprocessing)
- [ ] `caveman-compress` skips files that already have `.original.md` backup (idempotent)
- [ ] `caveman-compress` validation passes: headings exact, code blocks byte-identical, URLs preserved
- [ ] `caveman-commit` directive included in executor role file for conventional commit format
- [ ] `caveman-review` directive included in reviewer role file for one-line verdict format
- [ ] `inject_caveman_update()` sends directive-update message to agent FIFO (non-disruptive)
- [ ] `!caveman <agent> <level>` Discord command changes levels at runtime; validates agent + level
- [ ] `!caveman <level>` (no agent) applies globally for backward compat with v4 `!caveman` command
- [ ] `!caveman status` shows current levels for all agents
- [ ] `!caveman reset` restores `project.toml` defaults
- [ ] Runtime changes are ephemeral — session rotation reverts to `project.toml` defaults
- [ ] All `pending_mutations` lambdas use default-argument binding (no late-binding closures)
- [ ] Wenyan mode configurable via `[caveman.wenyan]` — disabled by default, all levels available

### Future (Phase 4-5)
- [ ] Ops monitor runs continuously alongside dev pipeline
- [ ] Ops monitor-generated tasks flow through dev pipeline (cross-pipeline trigger)
- [ ] Adding a new session requires only config + role file (sessions.toml, Phase 5)

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Agent context overflow (long task) | Medium | Medium | Caveman + session rotation on token threshold (parsed from log). Re-inject task context. |
| Session stale/hung | Medium | Low | Clawhip stale detection → alert → restart. `lifecycle.check_sessions()` on every poll. |
| FIFO write blocks (session not reading) | Low | High | `send()` catches `BlockingIOError`, triggers `restart()`. Launch uses tmux shell redirection to avoid deadlock. |
| Architect rubber-stamps escalation | Low | Medium | Confidence field required. Low confidence auto-promotes. retry_count >= 2 skips Tier 1. Audit trail. |
| Escalation ignored (operator away) | Medium | Medium | Re-notify every 4h. Advisory escalations auto-proceed. Tier 1 handles 60-70% without operator. |
| Pipeline frozen during Tier 2 wait | Medium | Medium | Phase 1 limitation (single operator, fast response). Phase 3+: shelve blocked task, process next. |
| Operator feedback arrives after task done | Low | Low | Check task state before injecting. Stale feedback → log + discard. |
| Race: escalation reply + session death | Low | Medium | On restart, check for pending replies before re-sending escalation. |
| Cost overrun from persistent sessions | Medium | Medium | Haiku for monitors, Sonnet for workers, caveman, summarization. |
| `clawhip tmux new` flag changes | Low | Medium | Fallback: launch tmux + claude directly, use clawhip only for monitoring. |
| OMC hooks don't fire in `claude -p` (wiki) | Low | Low | Wiki step fails gracefully, logs warning, doesn't block pipeline. |
| Caveman degrades output quality | Medium | Medium | Per-agent levels, not blanket. Architect off by default. All configurable — `!caveman` for runtime A/B testing. |
| caveman-compress loses critical info | Low | Medium | LLM-based with deterministic validation (headings, code blocks, URLs, paths). Fails safe: restores original on validation failure. Backup as `.original.md`. Disable via `project.toml`. |
| Wenyan mode confuses models | Medium | Low | Disabled by default. Experimental flag. No production use until benchmarked. |

## Implementation Phases

### Phase 1: Core Harness + Project Separation + Caveman
- `harness/start.sh` — thin launcher (clawhip + restart loop)
- `harness/orchestrator.py` — async main loop with stage `match` dispatch
- `harness/lib/sessions.py` — `SessionManager` (launch, send, restart, health check)
- `harness/lib/signals.py` — signal file I/O with dataclass schemas
- `harness/lib/pipeline.py` — `PipelineState` dataclass, stage transitions
- `harness/lib/claude.py` — `classify()`, `summarize()` via `asyncio.create_subprocess_exec`
- `harness/lib/lifecycle.py` — startup reconciliation + session health monitoring
- `config/harness/project.toml` — all project-specific paths + `[caveman]` section
- `config/harness/clawhip.toml.template` — template with `$PROJECT_ROOT` substitution
- `harness/discord_companion.py` — moved, shared process, plugin loader for project commands
- `CAVEMAN_DIRECTIVES` loaded from caveman `SKILL.md` at startup, cached per-level
- Per-agent caveman: `project.toml` `[caveman.agents]` → directive prepended in `sessions.py launch()`
- Per-call-type caveman: `[caveman.orchestrator]` → directive prepended in `claude.py _run_claude()`
- `caveman-compress`: LLM-based preprocessing at startup — compresses CLAUDE.md + role files on disk
- `caveman-commit` directive in executor role file, `caveman-review` directive in reviewer role file
- `inject_caveman_update()`: FIFO injection for runtime level changes (matches `!tell` pattern)
- `!caveman` Discord command with backward compat (`!caveman full` → `!caveman all full`)
- Config validation at startup: reject unknown levels/agents with clear error
- Wenyan config support (`[caveman.wenyan]`), disabled by default
- All `pending_mutations` lambdas use default-argument binding convention
- `harness/tests/` — pytest suite for all modules (including caveman config parsing + injection)
- **Hardcoded**: 3 agents, dev pipeline, `DEFAULT_AGENTS` dict

### Phase 2: Tiered Escalation + Operator Loop
- `harness/lib/escalation.py` — category routing, tier promotion, confidence gating
- `EscalationRequest` / `ArchitectResolution` / `EscalationReply` dataclasses in signals.py
- Tier 1: architect FIFO injection, resolution polling
- Tier 2: Discord notification, `escalation_wait` state, timeout + re-notify
- `persistent_failure` retry_count threshold (skip Tier 1 after 2 retries)
- `!tell` / `!reply` as direct method calls from discord_companion
- Escalation audit logging
- `harness/tests/test_escalation.py`

### Phase 3: Intelligence + Disputes
- `claude.reformulate()` for dispute resolution (rejection → reformulated feedback)
- `claude.summarize()` for context transfer between stages
- Verification loop management (executor ↔ reviewer, max retries)
- Session rotation: parse token usage from stream-json log, restart on threshold
- Pipeline-frozen mitigation: shelve blocked task, process next from queue

### Phase 4: Wiki + Documentation
- `claude.document_task()` / `claude.document_failure()` — post-merge wiki via `claude -p` + `/wiki`
- Failed task documentation
- OMC hook dependency check (graceful fallback if hooks don't fire)

### Phase 5: Bot Pipeline + Extensibility
- Ops monitor session (continuous, alongside dev pipeline)
- Cross-pipeline triggers (bug detected → dev task)
- `sessions.toml` parser (replace `DEFAULT_AGENTS` hardcoded dict)
- Pipeline definitions config
- Dynamic session start/stop
- Dialogue + Analyst sessions (on-demand / scheduled)

## What We Keep

| From | What | Why |
|------|------|-----|
| conductor.sh | Polling loop pattern | Proven, deterministic, $0 idle cost (now in Python asyncio) |
| conductor.sh | `run_judgment()` concept | On-demand LLM for intelligence (now `claude.py` via subprocess) |
| conductor.sh | Startup reconciliation logic | Crash recovery (now `lifecycle.py`) |
| conductor.sh | Signal polling for stage transitions | Deterministic (now `signals.py`) |
| conductor.sh | `do_merge()` pattern | Merge + test + revert (now in `pipeline.py`) |
| Phase 29 | Stream-json FIFO protocol | Persistent sessions, core communication |
| Phase 29 | `--disallowedTools` enforcement | Read-only for architect/reviewer |
| Phase 29 | `--permission-mode dontAsk` | MCP tools accessible without proxy |
| Current | clawhip.toml routes | Discord notification routing (now generated from template) |
| Current | discord_companion.py | Inbound Discord (moved to harness/, shared process) |
| Current | Signal file bus | Agent→orchestrator completion signaling |
| Current | Agent role files | Session role definitions (moved to config/harness/agents/) |
| Current | Worktree isolation | Executor per-task safety |

## What We Replace

| Old | New | Why |
|-----|-----|-----|
| conductor.sh (792-line bash, coupled) | harness/ (750-line Python, modular, project-agnostic) | Testable, typed, debuggable, reusable. No file >150 lines. |
| String-based JSON via jq pipes | Dataclass schemas with type hints | Validation, IDE support, no silent parse failures |
| Signal file bridge (companion → orchestrator) | Shared asyncio process | Eliminates race conditions, no polling delay for Discord commands |
| Fixed 3-stage check_pipeline | Extensible pipeline (hardcoded Phase 1, config Phase 5) | Future multi-pipeline support |
| No operator feedback loop | Tiered escalation + mid-session feedback | Human-in-the-loop without breaking autonomy |
| No documentation step | Wiki post-merge | Institutional memory |
| Manual Discord notifications only | `clawhip agent` lifecycle events + escalation posts | Consistent, automatic, bidirectional |

## What We Remove

| Removed | Why |
|---------|-----|
| Permission proxy (`request_permission` / `approve_permission` MCP) | `dontAsk` + `disallowedTools` handles this natively |
| `clawhip omc` for session launch | Incompatible with stream-json FIFOs; `clawhip tmux new -s` is correct |
| Persistent LLM orchestrator | Python + on-demand `claude -p` is cheaper, more reliable |
| sessions.toml / pipelines.toml (Phase 1) | YAGNI — hardcode first, extract to config in Phase 5 |
| All bash orchestrator logic | Python does this better at 1000+ line scale |
