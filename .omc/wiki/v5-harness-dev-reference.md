---
title: v5 Harness Developer Reference
tags: [harness, reference, developer, extension-points]
category: reference
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Developer Reference

Quick-reference for agents and developers working in the v5 harness. Covers extension points and key interfaces. For architecture overview see [[v5-harness-architecture]]; for bugs see [[v5-harness-known-bugs]].

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
| `shelved_tasks` | `list[dict]` | `shelve()` | `unshelve()`, `clear_active()` does NOT clear |

**Key methods**: `activate(task)`, `advance(stage, agent=None)`, `clear_active()`, `resume_from_escalation()`, `shelve()`, `unshelve()`, `save(path)`, `load(path)`, `heartbeat()`

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
| `token_rotation_threshold` | `token_rotation_threshold` | `100000` |

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

---

## Cross-References

- [[v5-harness-architecture]] — module overview, stage pipeline
- [[v5-harness-known-bugs]] — open bugs and deferred fixes
- [[v5-harness-design-decisions]] — rationale for O_NONBLOCK, caveman, concurrency
- [[v5-phase3-readiness]] — Phase 3 scope and sign-off status
