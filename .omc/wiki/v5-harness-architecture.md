---
title: v5 Harness Architecture
tags: [harness, architecture, pipeline, agents]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Architecture

The v5 harness is a Python asyncio orchestrator that manages Claude Code agent sessions for automated development workflows. A person types a sentence in Discord, and agents break work into tasks, assign roles, write code, test it, review it, and merge when everything passes.

## Three-Layer Stack

```
clawhip (infrastructure) — tmux session management, Discord relay, health monitoring
    |
Python asyncio orchestrator (intelligence) — task routing, stage dispatch, state machine
    |
Claude Code sessions (work) — persistent FIFO-fed agent sessions doing actual dev work
```

## Seven Modules

| Module | Responsibility | Key Types |
|--------|---------------|-----------|
| `signals.py` | Signal file I/O, dataclass schemas | `TaskSignal`, `EscalationRequest`, `SignalReader` |
| `pipeline.py` | State machine, config, agent defs | `PipelineState`, `ProjectConfig`, `CavemanConfig`, `AgentDef` |
| `sessions.py` | FIFO session management | `SessionManager`, `Session` |
| `claude.py` | On-demand `claude -p` subprocess calls | `classify()`, `summarize()`, `reformulate()`, `document_task()` |
| `lifecycle.py` | Recovery and health monitoring | `reconcile()`, `check_sessions()`, `is_alive()` |
| `orchestrator.py` | Main async loop, stage dispatch | `main_loop()`, stage handlers |
| `discord_companion.py` | Discord command handler | `DiscordCompanion`, `parse_caveman()`, `parse_tell()` |

## Stage Pipeline

```
classify -> architect -> executor -> reviewer -> merge -> wiki
                                        |
                                    (reject) -> reformulate -> executor (retry, max 3)
```

- **classify**: `claude -p` call decides "complex" (needs architect) or "simple" (straight to executor)
- **architect**: persistent Opus session plans the work
- **executor**: per-task Sonnet session in a git worktree implements the plan
- **reviewer**: persistent Sonnet session reviews the diff
- **merge**: `git merge --no-ff task/{id}`, run tests, revert on failure
- **wiki**: `claude -p` documents the completed task

## FIFO Session Model

Persistent sessions use named FIFOs for multi-turn communication:

1. `os.mkfifo(path, mode=0o600)` — create FIFO with restrictive perms
2. `clawhip tmux new` launches `claude -p --input-format stream-json < fifo` in tmux
3. Orchestrator opens write end with `O_NONBLOCK` after 0.5s delay
4. Messages sent as `{"type":"user","message":{"role":"user","content":"..."}}` + newline
5. On `BlockingIOError` (buffer full), session is restarted
6. Closing the write fd sends EOF, terminating the session

## Agent Roles (Phase 1)

| Agent | Model | Lifecycle | Mode |
|-------|-------|-----------|------|
| architect | opus | persistent | read-only |
| executor | sonnet | per-task | full |
| reviewer | sonnet | persistent | read-only |

Read-only agents have `Edit`, `Write`, `NotebookEdit`, and MCP filesystem write tools denied.

## Concurrency Model

Single asyncio event loop. No threading. Discord commands queue mutations via `pending_mutations` list — applied at the top of each poll cycle. Lambda closures use default-argument binding (`a=agent, l=level`) to avoid Python's late-binding closure bug.

## Configuration

- `config/harness/project.toml` — all paths, timeouts, caveman levels, pipeline settings
- `config/harness/clawhip.toml.template` — clawhip config with `$PROJECT_ROOT` substitution
- `config/harness/agents/*.md` — agent role prompts

## Cross-References

- [[v5-harness-reviewer-findings]] — security and quality review results
- [[v5-harness-design-decisions]] — key design choices and rationale
