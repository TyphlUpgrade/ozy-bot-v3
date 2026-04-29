# Plan: Escalation Dialogue (Piece 3)

**Status:** Approved (Planner + Architect + Critic consensus, 2026-04-09)
**Scope:** 6 code files, ~250 lines + ~200 lines tests
**Context:** Piece 3 of the v5 Conversational Discord Operator. Pieces 1+2 already implemented.
**Precursor to:** Discord Presence plan

---

## ADR

**Decision:** Implement `escalation_dialogue` pipeline stage with orchestrator-driven resolution classification and explicit operator confirmation.

**Drivers:** (1) Resolution safety — no false-positive auto-resume, (2) mutation-only companion contract preserved, (3) pattern reuse from `classify_target`/`_apply_reply`.

**Alternatives considered:**
- **Option B: Companion-driven auto-resume** — Rejected. Breaks mutation-only contract (LLM call in message handler creates back-pressure on Discord gateway). False-positive auto-resume sends garbled instructions to blocked agent. Unacceptable.
- **Light alternative: Re-notify suppression only** — Deferred. Add `last_operator_message_ts` to suppress re-notify during active conversation (~15 lines). Achieves 80% of value but insufficient for Discord Presence prerequisite. If operators find `!reply` sufficient after Pieces 1+2, this lighter path is available. The field becomes `dialogue_last_message_ts` in the full plan — no wasted work.

**Why chosen:** Option A preserves all architectural constraints. Two-step confirmation trades UX friction for safety — correct tradeoff for a pipeline controller. `!reply` remains the fast-path escape hatch.

**Consequences:** Two-step resolution UX. 3 new PipelineState fields. 1 new callback on companion. 1 new haiku LLM call per operator message during escalation. `!reply` short-circuits the dialogue flow (backward compat).

**Follow-ups:** Optional reaction-based confirmation (Discord emoji). Feature flag for dialogue mode.

---

## RALPLAN-DR Summary

### Principles
1. **Star topology preserved** — All operator messages route through companion -> mutation -> orchestrator
2. **Backward compatibility** — `!reply` always works as immediate escalation resolution
3. **Safe defaults** — LLM failure -> "continuation" (no premature resume). Resolution requires explicit confirmation
4. **Companion stays mutation-only** — No direct state reads; callbacks provide routing info
5. **Orchestrator owns classification** — Resolution detection runs in orchestrator poll loop

### Decision Drivers
1. Resolution safety — no false-positive auto-resume
2. Minimal coupling — read-only callbacks, no state reference sharing
3. Existing pattern reuse — `classify_resolution` follows `classify_target`; `_apply_dialogue_message` follows `_apply_reply`

### Viable Options
| | Option A: Orchestrator-driven classification | Option B: Companion-driven auto-resume |
|---|---|---|
| **How** | Companion routes NL to agent + stores message on state. Orchestrator classifies, prompts for confirm | Companion classifies inline, auto-resumes on "resolution" |
| **Pros** | Clean separation, safe (no auto-resume), leverages existing patterns | Faster UX, single-step resolution |
| **Cons** | Two-step resolution, one poll-cycle latency | Breaks mutation-only, LLM in message handler, false-positive risk |

**Option B invalidation:** Breaks mutation-only contract. Classification in message handler adds latency to Discord gateway processing. False-positive auto-resume sends garbled instructions to blocked agent.

---

## Requirements

1. New `escalation_dialogue` stage between `escalation_wait` and resume
2. Transition: `escalation_wait` -> operator sends NL -> `escalation_dialogue`
3. NL messages during dialogue route directly to blocked agent (skip classify_target)
4. Orchestrator classifies each operator message as resolution/continuation
5. Resolution detected -> notify operator with confirmation prompt
6. Operator confirms ("yes"/`!reply`) -> resume pipeline
7. `!reply` works at any point during dialogue (immediate resume, backward compat)
8. Dialogue timeout -> fall back to `escalation_wait` (re-notify)
9. Crash recovery: `escalation_dialogue` -> demote to `escalation_wait`
10. Shelving during dialogue preserves dialogue state

## Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC-1 | NL during `escalation_wait` transitions to `escalation_dialogue` | Unit test: mutation sets stage, sends to agent |
| AC-2 | NL during `escalation_dialogue` routes to `pre_escalation_agent` without `classify_target` | Unit test: `classify_target` not called, message delivered |
| AC-3 | `classify_resolution` returns "resolution" or "continuation" | Unit test: mock `_run_claude`, verify normalized output |
| AC-4 | Resolution -> Discord notify with confirm prompt including task_id | Unit test: `notify` called with confirmation message |
| AC-5 | NL "yes" during `dialogue_pending_confirmation` -> resume pipeline + clear escalation | Unit test: `resume_from_escalation` + `clear_escalation` + dialogue fields nulled |
| AC-6 | `!reply` during `escalation_dialogue` -> immediate resume (documented short-circuit) | Unit test: `_apply_reply` accepts stage, dialogue fields cleared |
| AC-7 | Dialogue timeout -> fall back to `escalation_wait` | Unit test: elapsed > timeout -> stage change + dialogue fields nulled |
| AC-8 | Crash -> `reconcile()` demotes to `escalation_wait`, clears ALL dialogue fields | Unit test: all 3 fields nulled after reconcile |
| AC-9 | Shelve preserves dialogue fields; unshelve restores AND clears `dialogue_pending_confirmation` | Unit test: round-trip + confirmation cleared on unshelve |
| AC-10 | Full test suite passes (272 existing + ~22 new) | `pytest harness/tests/ -x -q` green |
| AC-11 | `DIALOGUE_CONFIRM_WORDS` defined as module-level `frozenset` constant | Code inspection |
| AC-12 | `resume_from_escalation()` clears all 3 dialogue fields | Unit test: fields nulled after call |

**Documentation requirements** (not ACs):
- Multi-mutation-per-cycle: if two operator messages arrive in one cycle, only the last is classified. All messages delivered to agent. Code comment in `_apply_dialogue_message`.
- `!reply` short-circuits dialogue flow. Code comment in `_apply_reply` stage guard.
- `escalation_dialogue` excluded from `max_stage_minutes`. Code comment in `handle_escalation_dialogue`.

---

## Implementation Steps

### Step 1: PipelineState changes (`harness/lib/pipeline.py`, ~30 lines)

**VALID_STAGES** (line 240-243): Add `"escalation_dialogue"`:
```python
VALID_STAGES = frozenset({
    "classify", "architect", "executor", "reviewer", "merge", "wiki",
    "escalation_wait", "escalation_tier1", "escalation_dialogue",
})
```

**PipelineState fields** (after line 267): Add:
```python
dialogue_last_message_ts: str | None = None    # refreshed per exchange, drives dialogue timeout
dialogue_last_message: str | None = None       # stored for orchestrator to classify
dialogue_pending_confirmation: bool = False    # resolution detected, awaiting operator confirm
```

**`activate()`** (line 269-284): Add resets:
```python
self.dialogue_last_message_ts = None
self.dialogue_last_message = None
self.dialogue_pending_confirmation = False
```

**`resume_from_escalation()`** (line 299-305): Add dialogue field clearing:
```python
def resume_from_escalation(self) -> None:
    """Restore pre-escalation stage/agent and clear escalation context."""
    original_stage = self.pre_escalation_stage or "executor"
    original_agent = self.pre_escalation_agent
    self.advance(original_stage, original_agent)
    self.pre_escalation_stage = None
    self.pre_escalation_agent = None
    self.dialogue_last_message_ts = None
    self.dialogue_last_message = None
    self.dialogue_pending_confirmation = False
```
*Critic finding: prevents stale `dialogue_pending_confirmation` after `!reply` short-circuit through re-escalation cycles.*

**`clear_active()`** (line 307-321): Add resets (same 3 lines as `activate`).

**`shelve()`** (line 323-343): Add to dict:
```python
"dialogue_last_message_ts": self.dialogue_last_message_ts,
"dialogue_last_message": self.dialogue_last_message,
"dialogue_pending_confirmation": self.dialogue_pending_confirmation,
```

**`unshelve()`** (line 345-368): Restore with defaults, **force-clear pending confirmation**:
```python
self.dialogue_last_message_ts = task.get("dialogue_last_message_ts")
self.dialogue_last_message = task.get("dialogue_last_message")
self.dialogue_pending_confirmation = False  # stale after context-switch
```
*Architect rec: stale confirmation after shelve/unshelve is a bug.*

**ProjectConfig.load() timeouts** (line 217-222): Add:
```python
"classify_resolution": pipeline.get("classify_resolution_timeout", 10),
```

**ProjectConfig field** (after line 191): Add dialogue timeout as a separate field (not in `timeouts` dict — it's a stage duration, not a Claude call timeout):
```python
dialogue_timeout: int = 1800  # seconds before dialogue falls back to escalation_wait
```
And in `load()`:
```python
dialogue_timeout=pipeline.get("dialogue_timeout", 1800),
```

### Step 2: `classify_resolution()` (`harness/lib/claude.py`, ~20 lines)

New function, follows `classify_target` pattern exactly:

```python
async def classify_resolution(
    message: str,
    config: "ProjectConfig",
) -> str:
    """Classify operator message during escalation dialogue.

    Returns 'resolution' (decision made, go-ahead) or 'continuation'
    (question, discussion, clarification). Defaults to 'continuation'
    on failure — safe because it keeps dialogue open.
    """
    system = (
        "You are an escalation dialogue classifier. The operator is having a multi-turn "
        "conversation with a blocked agent. Classify the operator's message:\n"
        "- 'resolution': The operator has made a decision, given a go-ahead, or indicated "
        "the issue is resolved. Examples: 'go with approach B', 'approved', 'yes do that'\n"
        "- 'continuation': The operator is asking questions, providing context, or continuing "
        "discussion. Examples: 'what about X?', 'can you explain?', 'also consider...'\n"
        "Reply with exactly one word: 'resolution' or 'continuation'."
    )
    timeout = config.timeouts.get("classify_resolution", 10)
    result = await _run_claude(system, message, timeout, "classify_resolution", config, model="haiku")
    if result is None:
        return "continuation"
    normalized = result.strip().lower()
    if normalized not in ("resolution", "continuation"):
        logger.warning("classify_resolution returned %r — defaulting to continuation", result)
        return "continuation"
    return normalized
```

### Step 3: Companion changes (`harness/discord_companion.py`, ~45 lines)

**Module constant** (after line 28):
```python
# Deterministic confirmation words — no LLM call for confirm detection.
# Extend this set if operators use other affirmatives.
DIALOGUE_CONFIRM_WORDS = frozenset({
    "yes", "y", "confirm", "go", "approved", "ok", "okay", "proceed",
})
```

**Constructor** (line 70-80): Add `pipeline_stage_fn` parameter:
```python
def __init__(self, config, pending_mutations, signal_reader,
             active_agents_fn=None,
             pipeline_stage_fn=None):
    ...
    self._pipeline_stage_fn = pipeline_stage_fn or (lambda: (None, None))
```
**Exact signature:** `Callable[[], tuple[str | None, str | None]]` — returns `(current_stage, pre_escalation_agent)`.

**`_route_natural_language()`** (line 104-121): Insert escalation check at TOP, before classify_target:
```python
async def _route_natural_language(self, text: str) -> str | None:
    """Route a non-prefixed operator message to the correct agent."""
    from lib.claude import classify_target

    # Escalation dialogue: route directly to blocked agent (skip classify)
    stage, pre_esc_agent = self._pipeline_stage_fn()
    if stage in ("escalation_wait", "escalation_dialogue") and pre_esc_agent:
        sr = self.signal_reader
        self.pending_mutations.append(
            lambda s, sm, m=text, _sr=sr: _apply_dialogue_message(s, sm, m, _sr)
        )
        return f"Message sent to {pre_esc_agent} (escalation dialogue)."

    # Normal NL routing (existing code unchanged)
    agents = self._active_agents_fn()
    ...
```
*Critic Major #1 resolved: exact check location, lambda binding, return text specified.*
*Critic Major #2 resolved: agent derived from `state.pre_escalation_agent` inside mutation, not passed as param.*

**New module-level function `_apply_dialogue_message()`** (~25 lines):
```python
async def _apply_dialogue_message(state: "PipelineState", session_mgr: "SessionManager",
                                   message: str, signal_reader: "SignalReader") -> None:
    """Handle operator message during escalation dialogue.

    Dual responsibility: (1) deliver message to blocked agent, (2) update state
    for orchestrator classification. If multiple messages queued in one poll cycle,
    each overwrites dialogue_last_message — orchestrator classifies most recent only.
    All messages are delivered to the agent regardless.
    """
    from datetime import datetime, UTC

    agent = state.pre_escalation_agent
    if not agent:
        logger.warning("Dialogue message but no pre_escalation_agent — dropping")
        return

    # Confirmation of previously detected resolution
    if state.dialogue_pending_confirmation:
        normalized = message.strip().lower()
        if normalized in DIALOGUE_CONFIRM_WORDS:
            if agent in session_mgr.sessions:
                await session_mgr.send(agent, f"[OPERATOR REPLY] {message}")
            task_id = state.active_task or ""
            state.resume_from_escalation()  # clears all dialogue fields
            signal_reader.clear_escalation(task_id)
            logger.info("Dialogue confirmed for %s — resuming at %s", task_id, state.stage)
            return

    # Normal dialogue message
    if agent in session_mgr.sessions:
        await session_mgr.send(agent, f"[OPERATOR] {message}")
    state.dialogue_last_message = message
    state.dialogue_last_message_ts = datetime.now(UTC).isoformat()
    if state.stage == "escalation_wait":
        state.advance("escalation_dialogue")
    state.dialogue_pending_confirmation = False  # new message cancels pending confirmation
```

**`_apply_reply()` stage guards** (lines 262, 279): Add `"escalation_dialogue"`:
```python
# Line 262 (active task):
if state.stage not in ("escalation_wait", "escalation_tier1", "escalation_dialogue"):
    # !reply short-circuits dialogue flow — bypasses classify+confirm
    logger.warning(...)
    return

# Line 279 (shelved task):
if stage not in ("escalation_wait", "escalation_tier1", "escalation_dialogue"):
```

### Step 4: Orchestrator changes (`harness/orchestrator.py`, ~35 lines)

**Companion construction** (line 472-477): Wire `pipeline_stage_fn`:
```python
companion = dc.DiscordCompanion(
    config=config,
    pending_mutations=pending_mutations,
    signal_reader=signal_reader,
    active_agents_fn=lambda: list(session_mgr.sessions.keys()),
    pipeline_stage_fn=lambda: (state.stage, state.pre_escalation_agent),
)
```

**New `handle_escalation_dialogue()`** (~25 lines):
```python
async def handle_escalation_dialogue(state: PipelineState, config: ProjectConfig,
                                      event_log: EventLog) -> None:
    """Handle active escalation dialogue — classify messages, detect resolution.

    Dialogue timeout is separate from max_stage_minutes — do not add
    'escalation_dialogue' to that dict. This handler manages its own timeout
    via dialogue_last_message_ts.
    """
    # Timeout: no operator message in dialogue_timeout seconds -> fall back to wait
    if state.dialogue_last_message_ts:
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(state.dialogue_last_message_ts)).total_seconds()
        if elapsed > config.dialogue_timeout:
            logger.warning("Escalation dialogue timed out for %s after %.0fs",
                           state.active_task, elapsed)
            state.advance("escalation_wait")
            state.dialogue_last_message_ts = None
            state.dialogue_last_message = None
            state.dialogue_pending_confirmation = False
            await event_log.record("dialogue_timeout", {"task": state.active_task})
            return

    # Classify new operator message (if any, and no pending confirmation)
    if state.dialogue_last_message and not state.dialogue_pending_confirmation:
        intent = await claude.classify_resolution(state.dialogue_last_message, config)
        if intent == "resolution":
            state.dialogue_pending_confirmation = True
            msg = state.dialogue_last_message
            await notify("dialogue_confirm", state.pre_escalation_agent or "unknown",
                        f'Resolution detected: "{msg[:100]}". '
                        f'Confirm: say "yes" or `!reply {state.active_task} <instruction>`')
            await event_log.record("dialogue_resolution_detected", {
                "task": state.active_task, "message_preview": msg[:200],
            })
        state.dialogue_last_message = None  # consumed — classify once per message
```

**Match block** (line 537-553): Add case:
```python
case "escalation_dialogue":
    await handle_escalation_dialogue(state, config, event_log)
```

**Shelving guard** (line 556): Expand:
```python
# F3: Check for new tasks while current is blocked in escalation
if state.stage in ("escalation_wait", "escalation_dialogue"):
```

### Step 5: Lifecycle changes (`harness/lib/lifecycle.py`, ~15 lines)

**`reconcile()` active task** (after line 91, before `elif state.worktree`):
```python
elif state.stage == "escalation_dialogue":
    # Crash during dialogue — demote to escalation_wait for re-notify.
    # Dialogue context (which message, pending confirmation) is ephemeral.
    esc = await signal_reader.read_escalation(state.active_task)
    if esc is not None:
        logger.info("Re-notifying escalation for task %s (was in dialogue)", state.active_task)
        await notify_fn(esc)
    else:
        logger.warning("escalation_dialogue for %s but no escalation signal", state.active_task)
    state.advance("escalation_wait")
    state.dialogue_last_message_ts = None
    state.dialogue_last_message = None
    state.dialogue_pending_confirmation = False
```

**`reconcile()` shelved tasks** (line 108-127): Expand stage check:
```python
if sstage in ("escalation_wait", "escalation_tier1", "escalation_dialogue"):
```

Add handling for `escalation_dialogue` shelved tasks:
```python
if sstage == "escalation_dialogue":
    logger.warning("Shelved task %s was in escalation_dialogue — reverting to escalation_wait", stask_id)
    shelved["stage"] = "escalation_wait"
    shelved["dialogue_last_message_ts"] = None
    shelved["dialogue_last_message"] = None
    shelved["dialogue_pending_confirmation"] = False
```

### Step 6: Tests (`harness/tests/`, ~200 lines, 22 new tests)

**Companion tests (12) in `test_discord_companion.py`:**
1. `test_nl_during_escalation_wait_routes_to_pre_esc_agent` — routes to blocked agent, classify_target not called
2. `test_nl_during_escalation_dialogue_routes_to_pre_esc_agent` — same during active dialogue
3. `test_nl_during_normal_stage_uses_classify` — non-escalation uses normal flow
4. `test_nl_during_escalation_returns_dialogue_response` — returns "Message sent to {agent} (escalation dialogue)."
5. `test_dialogue_message_mutation_sends_to_agent` — `sm.send` called with `[OPERATOR]` prefix
6. `test_dialogue_message_mutation_transitions_wait_to_dialogue` — `state.stage == "escalation_dialogue"` after mutation
7. `test_dialogue_message_mutation_refreshes_timestamp` — `dialogue_last_message_ts` set
8. `test_dialogue_message_mutation_clears_pending_confirmation` — new message cancels pending
9. `test_dialogue_confirmation_yes_resumes_pipeline` — "yes" during pending -> resume + fields cleared
10. `test_dialogue_confirmation_clears_escalation_signal` — `signal_reader.clear_escalation` called
11. `test_apply_reply_accepts_escalation_dialogue` — `!reply` works during dialogue stage
12. `test_apply_reply_shelved_dialogue_accepted` — `!reply` on shelved task in dialogue

**classify_resolution tests (4) in `test_discord_companion.py`:**
13. `test_classify_resolution_returns_resolution` — mock returns "resolution"
14. `test_classify_resolution_returns_continuation` — mock returns "continuation"
15. `test_classify_resolution_timeout_defaults_continuation` — mock returns None -> "continuation"
16. `test_classify_resolution_uses_haiku` — verify `model="haiku"` in call

**PipelineState tests (3) in `test_pipeline.py` (or existing test file):**
17. `test_escalation_dialogue_in_valid_stages` — frozenset membership
18. `test_shelve_preserves_dialogue_fields` — all 3 present in shelved dict
19. `test_unshelve_clears_pending_confirmation` — restored but confirmation forced False

**Lifecycle tests (2):**
20. `test_reconcile_dialogue_falls_back_to_wait` — active task demoted + fields nulled
21. `test_reconcile_shelved_dialogue_reverts_to_wait` — shelved task demoted + fields nulled

**State machine test (1):**
22. `test_resume_from_escalation_clears_dialogue_fields` — all 3 nulled after call

---

## Files Modified

| File | Change | Lines |
|------|--------|-------|
| `harness/lib/pipeline.py` | VALID_STAGES, 3 fields, activate/resume/clear_active/shelve/unshelve, dialogue_timeout, classify_resolution timeout | ~30 |
| `harness/lib/claude.py` | `classify_resolution()` | ~20 |
| `harness/discord_companion.py` | `pipeline_stage_fn`, escalation routing, `_apply_dialogue_message`, `_apply_reply` guards, `DIALOGUE_CONFIRM_WORDS` | ~45 |
| `harness/orchestrator.py` | `pipeline_stage_fn` wire-up, `handle_escalation_dialogue()`, match case, shelving guard | ~35 |
| `harness/lib/lifecycle.py` | `reconcile()` for `escalation_dialogue` active + shelved | ~15 |
| `harness/tests/test_discord_companion.py` + others | 22 new tests | ~200 |

**Total: ~345 lines across 6+ files**

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|-----------|
| classify_resolution false positive | Medium | Requires explicit confirmation via DIALOGUE_CONFIRM_WORDS or `!reply` |
| Operator ignores confirmation prompt | Low | Dialogue timeout (30 min default) -> fall back to escalation_wait + re-notify |
| Stale dialogue_pending_confirmation after !reply | None | `resume_from_escalation()` clears all dialogue fields (Critic finding) |
| Stale dialogue_pending_confirmation after unshelve | None | `unshelve()` force-clears to False (Architect rec) |
| Multi-messages-per-cycle | Low | Only last classified; all delivered to agent. Documented intentional behavior |
| Crash during dialogue | Low | `reconcile()` demotes to escalation_wait, nulls all dialogue fields |
| State bloat from dialogue_last_message | Low | Consumed (nulled) after classification each cycle |
| pipeline_stage_fn stale read | None | Same asyncio event loop — reads always current |

## Verification Steps

1. `python3 -m pytest harness/tests/ -x -q` — full suite passes (294+ tests)
2. Verify `escalation_dialogue` in VALID_STAGES
3. Verify `_apply_reply` accepts `escalation_dialogue` for both active and shelved
4. Verify `classify_resolution` uses haiku and 10s timeout
5. Verify `resume_from_escalation()` clears all 3 dialogue fields
6. Verify `reconcile()` demotes `escalation_dialogue` to `escalation_wait` + nulls fields
7. Verify shelve/unshelve round-trips fields with `dialogue_pending_confirmation` forced False
8. Verify DIALOGUE_CONFIRM_WORDS is a frozenset module constant

## Dependency

This plan depends on the **Discord Integration Revisions** plan (`.omc/plans/2026-04-09-discord-integration-revisions.md`) being executed first. That plan adds:
- Three-way NL intent classification (control/feedback/new_task)
- Pipeline pause/resume mechanism
- NL-initiated task creation

Without it, this plan still works but the NL routing is simpler (two-way: feedback only).

## Changelog (from review)

- Added `DIALOGUE_CONFIRM_WORDS` frozenset (Architect Rec 2)
- Added `dialogue_pending_confirmation` clearing on unshelve (Architect Rec 3)
- Added dialogue field clearing on crash demotion in reconcile (Architect Rec 4)
- Added multi-mutation-per-cycle documentation requirement (Architect Rec 5)
- Added `!reply` short-circuit documentation (Architect Rec 6)
- Specified exact `pipeline_stage_fn` signature, check location, wiring (Critic Major 1)
- Changed `_apply_dialogue_message` to derive agent from `state.pre_escalation_agent` (Critic Major 2)
- Added dialogue field clearing in `resume_from_escalation()` (Critic bug finding)
- Moved `dialogue_timeout` to separate ProjectConfig field (Critic Minor 1)
- Revised test line estimate from ~110 to ~200 (Critic Minor 2)
- Removed AC-11 (documentation task) from AC table, moved to documentation reqs (Critic Minor 3)
- Added Discord response text for dialogue routing (Critic Minor 4)
