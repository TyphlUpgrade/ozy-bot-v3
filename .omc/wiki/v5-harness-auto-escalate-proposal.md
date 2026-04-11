---
title: "v5 Harness: Auto-Escalate + Circuit Breaker"
tags: [harness, escalation, proposal, retry, gap, circuit-breaker]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# Auto-Escalate + Circuit Breaker

**Status:** Proposal (identified gap, not yet scheduled)
**Area:** `harness/orchestrator.py:check_reviewer()`, `check_for_escalation()`, escalation pipeline

## The Gaps

### Gap 1: Silent task abandonment

Two escalation-related mechanisms exist but are **disconnected**:

| Mechanism | Trigger | Outcome after exhaustion |
|-----------|---------|------------------------|
| Reviewer rejection retry loop | Reviewer rejects executor work | 3 retries (`max_retries`), then **task silently abandoned** |
| Agent-initiated escalation | Agent writes signal file (blocked) | Tier 1 (architect) -> Tier 2 (operator) |

When executor fails 3 reviewer rounds, `check_reviewer()` calls `state.clear_active()` and the task vanishes. Nobody is notified.

```python
# orchestrator.py:97-101 — current behavior
if state.retry_count >= config.max_retries:
    logger.error("Task %s failed after %d retries", state.active_task, state.retry_count)
    _escalation_cache.pop(state.active_task or "", None)
    state.clear_active()
    return  # task silently dropped
```

### Gap 2: No circuit breaker on architect

Nothing prevents infinite architect↔executor cycles:

```
executor fails review 3x → architect replans
    → executor fails review 3x → architect replans
        → executor fails review 3x → architect replans → ∞
```

Same for agent-initiated escalations:

```
executor blocked → architect resolves → executor blocked again
    → architect resolves → executor blocked again → ∞
```

Architect keeps getting the same task, keeps producing plans that fail. No limit, no operator involvement.

## Proposed Fix: Two Mechanisms

### 1. Auto-Escalate After Max Retries

Replace silent abandonment with auto-escalation to Tier 1 (architect):

```python
# orchestrator.py:97-101 — proposed behavior
if state.retry_count >= config.max_retries:
    if not config.auto_escalate_on_max_retries:
        state.clear_active()  # legacy behavior
        return

    # Guard against overwriting existing escalation
    existing = await signal_reader.read_escalation(state.active_task or "")
    if existing:
        logger.info("Task %s already has escalation — skipping auto-escalate",
                     state.active_task)
        state.clear_active()
        return

    logger.warning("Task %s failed after %d retries — auto-escalating",
                   state.active_task, state.retry_count)

    esc = EscalationRequest(
        task_id=state.active_task,
        agent=state.stage_agent or "executor",
        stage=state.stage or "reviewer",
        severity="blocking",
        category="persistent_failure",
        question=f"Task failed {state.retry_count} reviewer rounds. Last rejection: {feedback[:200]}",
        options=["replan_approach", "simplify_scope", "escalate_to_operator"],
        context=f"retry_count={state.retry_count}, last_feedback={feedback[:500]}",
        retry_count=state.retry_count,
    )
    write_signal(config.signal_dir / "escalation", f"{state.active_task}.json", esc)

    # Route through circuit breaker (see below)
    tier = _route_with_circuit_breaker(state, esc, config)
    if tier == "tier1":
        _escalation_cache[esc.task_id] = esc
        msg = escalation.format_escalation_for_architect(esc)
        await session_mgr.send("architect", msg)
        state.pre_escalation_stage = state.stage
        state.pre_escalation_agent = state.stage_agent
        state.advance("escalation_tier1", "architect")
    else:
        summary = escalation.format_tier2_notification(esc)
        await notify("blocked", esc.agent, summary)
        state.pre_escalation_stage = state.stage
        state.pre_escalation_agent = state.stage_agent
        state.advance("escalation_wait")

    await event_log.record("auto_escalated", {
        "task": state.active_task,
        "retry_count": state.retry_count,
        "tier": 1 if tier == "tier1" else 2,
        "reason": "max_retries_exhausted",
        "circuit_breaker_count": state.tier1_escalation_count,
    })
    return
```

### 2. Escalation Circuit Breaker

**New state field:** `tier1_escalation_count: int = 0` on `PipelineState`.

After N architect attempts for the same task, skip architect and go straight to operator:

```python
def _route_with_circuit_breaker(state: PipelineState, esc: EscalationRequest,
                                 config: ProjectConfig) -> str:
    """Route escalation through standard tiers, with circuit breaker override."""
    tier = escalation.route_escalation(esc)
    if tier == "tier1":
        if state.tier1_escalation_count >= config.max_tier1_escalations:
            logger.warning("Circuit breaker: Tier 1 exhausted for %s (%d attempts) — forcing Tier 2",
                           state.active_task, state.tier1_escalation_count)
            return "tier2"
        state.tier1_escalation_count += 1
    return tier
```

**Integration:** Apply circuit breaker in BOTH escalation paths:
- `check_for_escalation()` — agent-initiated escalations
- `check_reviewer()` auto-escalate — retry exhaustion escalations

Both call `_route_with_circuit_breaker()` instead of `escalation.route_escalation()` directly.

**Counter lifecycle:**

| Event | Action |
|-------|--------|
| `activate()` (new task) | Reset to 0 |
| `clear_active()` (task done) | Reset to 0 |
| Enter `escalation_tier1` | Increment |
| `resume_from_escalation()` | **NOT reset** — persists across cycles |
| `shelve()` | Preserved in shelved dict |
| `unshelve()` | Restored from shelved dict |

**Full escalation lifecycle with both mechanisms:**

```
Agent blocked OR executor fails 3 reviews
    ↓
Overwrite guard: existing escalation? → skip, let existing resolve
    ↓
Circuit breaker: tier1_count < max_tier1_escalations (default: 2)?
    ├─ YES → Tier 1 (architect), counter++
    │   ↓
    │   Architect resolves → resume (counter NOT reset)
    │       → if executor fails again, next escalation sees higher counter
    │   Architect fails (low confidence / timeout) → promote to Tier 2
    │
    └─ NO → Tier 2 (operator) directly
            ↓
            Operator replies → resume
            Advisory timeout → auto-proceed
            Blocking timeout → re-notify
```

**Worst case before operator sees it** (defaults: `max_retries=3`, `max_tier1_escalations=2`):

```
Round 1: executor tries 3x → architect replans (tier1_count=1)
Round 2: executor tries 3x → architect replans (tier1_count=2)
Round 3: executor tries 3x → circuit breaker fires → OPERATOR NOTIFIED
Total: 9 executor attempts, 2 architect attempts, then human
```

## Why Tier 1 (Architect) First?

Architect planned the task initially but didn't see the reviewer feedback. With 3 rounds of rejection context, architect may:
- **Replan** — different approach entirely
- **Simplify scope** — break task into smaller reviewable pieces
- **Escalate with analysis** — promote to Tier 2 with richer context

The circuit breaker ensures this isn't infinite — architect gets `max_tier1_escalations` shots across all escalation cycles for a task, then operator takes over.

## Why `persistent_failure` Category?

Aligns with existing escalation categories. OMC Agent Integration roadmap defines `persistent_failure` as the trigger for Debugger + Tracer agents (Tier 2). When those agents land, auto-escalated tasks that architect can't resolve route to debugger/tracer before hitting operator.

## Multiple Escalation Requests (Futureproofing)

### Current limitation

Pipeline supports **one escalation per task at a time**. `_escalation_cache` keyed by `task_id`, `read_escalation()` reads single file at `escalation/{task_id}.json`. Second escalation overwrites first.

### Phased approach

**Phase 1 (implement now):** Overwrite guard. Before writing synthetic escalation, check if one exists:

```python
existing = await signal_reader.read_escalation(state.active_task or "")
if existing:
    logger.info("Task %s already has escalation (category=%s) — skipping",
                state.active_task, existing.category)
    state.clear_active()
    return
```

**Phase 2 (future):** Replace single-file escalation with ordered queue:

```
signals/escalation/{task_id}/
    001-design_question.json      # original agent escalation
    002-persistent_failure.json   # auto-escalate from retry exhaustion
```

Changes needed:
- `SignalReader.read_escalation()` → returns list, ordered by timestamp
- `SignalReader.clear_escalation()` → clears single entry or all
- `_escalation_cache` → keyed by `(task_id, esc_id)` or stores list
- `handle_escalation_tier1` / `handle_escalation_wait` → process queue FIFO
- `EscalationRequest` → add `esc_id: str` field (auto-generated)

**Phase 3 (future):** Escalation merging. Architect sees full queue holistically. Useful when retry failure + agent blocker overlap.

```
Phase 1: Overwrite guard (now, ~5 lines)
    ↓
Phase 2: Escalation queue (when concurrent escalations needed)
    ↓
Phase 3: Escalation merging (when architect needs holistic view)
```

## Config

```toml
# clawhip.toml
[pipeline]
auto_escalate_on_max_retries = true   # false = silent abandon (legacy)
max_tier1_escalations = 2             # architect attempts before circuit breaker fires
```

## PipelineState Changes

```python
# pipeline.py — new field
tier1_escalation_count: int = 0   # circuit breaker counter

# activate() — reset
self.tier1_escalation_count = 0

# clear_active() — reset
self.tier1_escalation_count = 0

# resume_from_escalation() — NOT reset (persists across cycles)

# shelve() — preserve
"tier1_escalation_count": self.tier1_escalation_count,

# unshelve() — restore
self.tier1_escalation_count = task.get("tier1_escalation_count", 0)
```

## Scope

**Phase 1 (auto-escalate + circuit breaker + overwrite guard):**
- `orchestrator.py:check_reviewer()` — ~30 lines (auto-escalate + routing)
- `orchestrator.py:check_for_escalation()` — ~5 lines (use circuit breaker)
- `orchestrator.py` — ~10 lines (`_route_with_circuit_breaker` function)
- `pipeline.py:PipelineState` — ~10 lines (field + activate/clear/shelve/unshelve)
- `pipeline.py:ProjectConfig` — ~5 lines (2 config keys)
- **Total: ~60 lines, no new files**

**Phase 2 (escalation queue):** ~80 lines across `signals.py`, `orchestrator.py`, `escalation.py`. Separate plan needed.

## Dependencies

- None for Phase 1. Can be implemented independently.
- Benefits from Escalation Dialogue (Piece 3) — operator can have multi-turn conversation about persistent failures.
- Benefits from OMC Agent Integration (Debugger + Tracer) — `persistent_failure` routes to debugger before operator.

## Related Pages

- [[v5-harness-architecture]] — Orchestrator, escalation routing
- [[v5-harness-roadmap]] — Escalation pipeline phases
- [[v5-omc-agent-integration]] — Debugger + Tracer for `persistent_failure`
- [[v5-conversational-discord-operator]] — Piece 3 escalation dialogue
