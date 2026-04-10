---
title: v5 Harness Drift Log
tags: [harness, drift, deviations, v5-plan]
category: decision
created: 2026-04-09
updated: 2026-04-09
entries: 7
---

# v5 Harness Drift Log

Implementation deviations from `plans/2026-04-08-v5-harness-architecture.md`. Format matches Ozy DRIFT_LOG Spec/Impl/Why triple.

**Filing heuristic:** Only file a drift entry when the deviation would surprise a future developer reading the v5 plan — not for every minor implementation choice.

**Size ceiling:** 8KB (matches `decision` category cap).

**Scope:** Harness v5 plan deviations only. Trading bot spec deviations go in `DRIFT_LOG.md` (project root).

---

### `orchestrator-pure-python` · v5 plan §Architecture (lines 29-34) · `harness/orchestrator.py`
- **Plan:** Clawhip launches all sessions, watches signal files, manages session lifecycle. Orchestrator is a coordination layer above clawhip.
- **Impl:** Python orchestrator does both session management and signal polling directly. Clawhip only handles tmux launch + Discord routing.
- **Why:** Simpler to keep signal polling in Python where state management lives. Avoids split-brain between clawhip state and Python state.

### `bot-sessions-not-implemented` · v5 plan §Session Registry (lines 47-52) · N/A
- **Plan:** Session registry includes ops_monitor, dialogue, and analyst sessions alongside dev sessions (architect, executor, reviewer).
- **Impl:** Only dev sessions exist (architect, executor, reviewer). No bot sessions implemented.
- **Why:** Phase 5 scope (bot pipeline integration). Current harness only handles dev workflow.

### `stage-timeout-clears-task` · v5 plan §Stage Pipeline (line 291) · `harness/orchestrator.py`
- **Plan:** Timeout kills the session and retries the stage (implied by timeout → escalation flow).
- **Impl:** `handle_stage_timeout` calls `kill()` on the session AND clears the active task via `clear_active()`. No automatic retry.
- **Why:** A timed-out stage indicates the task itself is stuck, not just the session. Restarting without clearing would re-enter the same stuck state.

### `shelved-tasks-list-dict` · v5 plan §Pipeline-frozen mitigation (lines 1254-1305) · `harness/lib/pipeline.py`
- **Plan:** "Shelve blocked task, process next from queue" implies a structured task queue (dataclass or typed container).
- **Impl:** LIFO list of plain dicts on `PipelineState.shelved_tasks`.
- **Why:** Save/load simplicity — dicts serialize to JSON directly without custom encoder. Dataclass adds ceremony with no runtime benefit at current scale.

### `clawhip-toml-schema-v055` · v5 plan §Clawhip Config · `config/harness/clawhip.toml.template`
- **Plan:** Template used `[[routes]]` with `watch`/`patterns` fields and `[discord]` section, `[[sessions]]` with `name` field.
- **Impl:** Rewritten for clawhip v0.5.5: routes require `event` field (e.g. `workspace.file.changed`), Discord config under `[providers.discord]`, tmux monitors under `[[monitors.tmux.sessions]]` with `session` field, workspace monitors require `channel`.
- **Why:** Original template was written against an assumed schema. clawhip v0.5.5 requires event-based routing, not filesystem-watch-based routes. Schema validated against working v4 config in worktree.

### `start-sh-env-export` · v5 plan §Launcher · `harness/start.sh`
- **Plan:** `source .env` then `envsubst` to generate clawhip.toml.
- **Impl:** Added `set -a` before source and `set +a` after, so all `.env` variables are exported for `envsubst`.
- **Why:** Plain `VAR=value` lines in `.env` (no `export` keyword) are not visible to `envsubst` unless `set -a` (allexport) is active. All channel IDs and tokens were substituted as empty strings.

### `discord-companion-orchestrator-wiring` · Discord Operator Piece 2 · `harness/orchestrator.py`, `harness/discord_companion.py`
- **Plan:** Placeholder comment at orchestrator line 469: "In Phase 2, discord_companion.start() runs as asyncio.create_task() here."
- **Impl:** Added `start()` async coroutine to `discord_companion.py` (discord.py Client with `on_message` → `handle_raw_message()` delegation, message dedup, channel filtering). Orchestrator constructs `DiscordCompanion` with `active_agents_fn=lambda: list(session_mgr.sessions.keys())` and launches via `asyncio.create_task(dc.start(companion, channel_ids))`.
- **Why:** Completes the Piece 2 integration — NL inbound routing was implemented but had no Discord client to receive messages.

---

## Cross-References

- [[v5-harness-architecture]] — the architecture that this log tracks deviations from
- [[v5-harness-open-concerns]] — engineering concerns (not plan deviations)
- [[v5-harness-known-bugs]] — bugs (with repro steps, not design choices)
