---
title: v5 Harness Developer Reference
tags: [harness, reference, developer, extension-points]
category: reference
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Developer Reference

Quick-reference for agents and developers working in the v5 harness. Covers extension points, key interfaces, code patterns, and diagnostic flows. For architecture overview see [[v5-harness-architecture]]; for bugs see [[v5-harness-known-bugs]].

---

## Extension Points

### Add a pipeline stage

1. Add stage name to `VALID_STAGES` frozenset in `harness/lib/pipeline.py`
2. Add stage handler function in `harness/orchestrator.py` (async; signature varies per handler — see existing handlers for patterns)
3. Wire into the `match state.stage:` dispatch in the main loop
4. Add default timeout in `max_stage_minutes` dict in `ProjectConfig.load()`
5. Add agent role file in `config/harness/agents/<stage>.md` if the stage uses a dedicated agent

**Current stages**: `classify`, `architect`, `executor`, `reviewer`, `merge`, `wiki`, `escalation_wait`, `escalation_tier1`

### Add a config field

1. Add field with default to `ProjectConfig` dataclass in `harness/lib/pipeline.py`
2. Load from TOML in `ProjectConfig.load()` — convention: `pipeline.get("field_name", default)`
3. Add to config fixture in `harness/tests/conftest.py`

### Add a signal type

1. Define dataclass in `harness/lib/signals.py` (see `TaskSignal`, `EscalationRequest` for patterns)
2. Add reader method on `SignalReader` — convention: `read_<type>(task_id) -> T | None`
3. Create subdirectory under `signals/` (e.g., `signals/new_type/`)
4. Writer: use `write_signal(directory, filename, data_dict)` for atomic writes

### Add an agent role

1. Create role file: `config/harness/agents/<name>.md`
2. Add `AgentDef` entry in `ProjectConfig.agents` (auto-loaded from agents dir by `_default_agents()`)
3. Set lifecycle: `"persistent"` (always running) or `"per-task"` (launched on demand)

---

## Key Interfaces

### PipelineState (`harness/lib/pipeline.py`)

Dataclass — the mutable state of the pipeline. Persisted to JSON via `save()`/`load()`.

| Field | Type | Set by | Cleared by |
|-------|------|--------|------------|
| `active_task` | `str \| None` | `activate()` | `clear_active()` |
| `task_description` | `str \| None` | `activate()` | `clear_active()` |
| `stage` | `str \| None` | `advance()` | `clear_active()` |
| `stage_agent` | `str \| None` | `advance()` | `clear_active()` |
| `stage_started_ts` | `str \| None` | `activate()`, `advance()` | `clear_active()` |
| `escalation_started_ts` | `str \| None` | `advance()` (escalation stages) | `clear_active()`, `advance()` (non-escalation) |
| `pre_escalation_stage` | `str \| None` | set before escalation | `clear_active()`, `resume_from_escalation()` |
| `pre_escalation_agent` | `str \| None` | set before escalation | `clear_active()`, `resume_from_escalation()` |
| `last_renotify_ts` | `str \| None` | orchestrator on renotify | `clear_active()`, `activate()` |
| `worktree` | `str \| None` | orchestrator | `clear_active()` |
| `retry_count` | `int` | orchestrator | `clear_active()` |
| `heartbeat_ts` | `str \| None` | `heartbeat()` | — |
| `shutdown_ts` | `str \| None` | orchestrator (graceful shutdown) | — |

**Key methods**: `activate(task)`, `advance(stage, agent=None)`, `clear_active()`, `resume_from_escalation()`, `save(path)`, `load(path)`, `heartbeat()`

### ProjectConfig TOML keys (`[pipeline]` section)

| TOML key | Field | Default |
|----------|-------|---------|
| `poll_interval` | `poll_interval` | `5.0` |
| `max_retries` | `max_retries` | `3` |
| `escalation_timeout` | `escalation_timeout` | `14400` (4h) |
| `tier1_timeout` | `tier1_timeout` | `1800` (30min) |
| `test_command` | `test_command` | `"python3 -m pytest tests/ -x"` |
| `claude_binary` | `claude_binary` | `"claude"` |
| `{stage}_max_minutes` | `max_stage_minutes[stage]` | classify=10, architect=60, executor=120, reviewer=60, merge=15, wiki=15 |

### SignalReader (`harness/lib/signals.py`)

| Method | Returns | Signal dir |
|--------|---------|------------|
| `next_task(task_dir)` | `TaskSignal \| None` | `task_dir` arg (typically `agent_tasks/`), by mtime |
| `check_stage_complete(stage, task_id)` | `dict \| None` | `signals/{architect,executor,reviewer}/` (pattern per stage) |
| `read_escalation(task_id)` | `EscalationRequest \| None` | `signals/escalation/` |
| `read_architect_resolution(task_id)` | `ArchitectResolution \| None` | `signals/escalation_resolution/` |
| `write_signal(directory, filename, data)` | — | atomic write (tmp + rename) |
| `clear_escalation(task_id)` | — | removes escalation + resolution files |
| `archive(task_id, archive_dir)` | — | moves signal files to `archive_dir/` |

All reader methods return `None` on missing file or malformed JSON (never crash).

### SessionManager (`harness/lib/sessions.py`)

| Method | Description |
|--------|-------------|
| `launch(name, agent_def)` | Start tmux session + open FIFO for writing |
| `send(name, message)` | Write message to agent's FIFO |
| `restart(name)` | Kill tmux session + relaunch (see BUG-022) |
| `inject_caveman_update(name, level)` | Send caveman level change to agent |
| `shutdown()` | Close all FIFOs (EOF), wait 5s, force-kill remaining tmux sessions |

Sessions use `O_NONBLOCK` FIFO pipes. 0.5s sleep after tmux launch before FIFO open (BUG-006 race window).

---

## Code Patterns

**Single asyncio loop** — All harness code runs in one event loop. No threading. Mutable state passed by reference is safe because there's no concurrent mutation.

**Lookup tables over if/elif** — Stage dispatch uses `match/case`. Valid stages, caveman levels, and timeouts are `frozenset` or `dict` constants. To add a new value, add one entry to one table.

**Module-level cache** — `_escalation_cache: dict[str, EscalationRequest]` in `orchestrator.py` avoids TOCTOU re-reads. Must be popped before every `clear_active()` call (9 sites as of Phase 2).

**Late init** — Modules that depend on runtime state (like `_risk_manager`) are initialized in `_startup()`, not `__init__()`. Tests can reassign without stale-reference bugs.

**Atomic file writes** — `write_signal()` writes to a temp file then renames. Prevents partial-read corruption on crash.

---

## Diagnostic Flows

**Pipeline stuck (no progress):**
1. Check `state.stage` — is it `escalation_wait` or `escalation_tier1`?
2. Check `escalation_started_ts` — is it None? (BUG-015/017 fix: now logs warning)
3. Check signal files — was the escalation file deleted? (force-resume after `2 * escalation_timeout`)
4. Check `stage_started_ts` — has `max_stage_minutes` been exceeded? (stage timeout should fire)

**Session won't launch:**
1. FIFO race (BUG-006) — tmux slow to start, `os.open(O_NONBLOCK)` gets `ENXIO`
2. Check tmux: `tmux list-sessions` — is the session alive?
3. Check agent role file exists in `config/harness/agents/`

**Escalation stalled:**
1. Tier 1: `tier1_timeout` (default 30min) auto-promotes to Tier 2
2. Tier 2: `escalation_timeout` (default 4h) auto-proceeds for advisory, re-notifies for blocking
3. Missing signal: force-resumes after `2 * escalation_timeout`
4. Crash during tier1: `lifecycle.reconcile()` re-sends to architect or promotes

**Test failures in merge stage:**
1. `do_merge()` runs `config.test_command` (configurable via TOML)
2. Total suite timeout: `asyncio.wait_for(..., timeout=180)`
3. On failure: `git revert --no-edit -m 1 HEAD`, task cleared

---

## Open Bug Summary

8 open bugs tracked in [[v5-harness-known-bugs]]. Key patterns by area:

| Area | Bugs | Pattern |
|------|------|---------|
| Signals | BUG-001, BUG-005 | Unbounded state (`_processed` set), missing path validation |
| Sessions | BUG-006, BUG-007 | FIFO race window, clawhip bypass |
| Pipeline | BUG-002, BUG-008 | State deserialization fragility, hardcoded stage graph |
| Templates | BUG-003 | Fragile frontmatter stripping regex |
| Tests | BUG-012 | AsyncMock GC warning (cosmetic) |

All open bugs are Low/Info severity. No blocking issues remain.

16 resolved bugs archived in [[v5-harness-known-bugs-archive-2026]].

---

## Cross-References

- [[v5-harness-architecture]] — module overview, stage pipeline
- [[v5-harness-known-bugs]] — open bugs and deferred fixes
- [[v5-harness-design-decisions]] — rationale for O_NONBLOCK, caveman, concurrency
- [[v5-phase3-readiness]] — Phase 3 scope and sign-off status
