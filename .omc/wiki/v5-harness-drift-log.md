---
title: v5 Harness Drift Log
tags: [harness, drift, deviations, v5-plan]
category: decision
created: 2026-04-09
updated: 2026-04-09
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

---

## Cross-References

- [[v5-harness-architecture]] — the architecture that this log tracks deviations from
- [[v5-harness-open-concerns]] — engineering concerns (not plan deviations)
- [[v5-harness-known-bugs]] — bugs (with repro steps, not design choices)
