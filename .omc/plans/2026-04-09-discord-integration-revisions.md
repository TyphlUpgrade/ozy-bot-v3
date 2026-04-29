# Plan: v5 Harness Discord Integration Revisions

**Status:** Approved (Architect + Critic consensus, 2026-04-09)
**Scope:** 5 files, ~230 lines
**Context:** Design gaps discovered during live Discord testing session

---

## ADR

**Decision:** Implement three-way NL intent classification with deterministic control pre-filter, pipeline pause mechanism, and NL-initiated task creation.

**Drivers:** (1) NL classify accuracy for control commands must be deterministic, (2) NL-initiated tasks must enter full pipeline, (3) changes must layer on existing infrastructure.

**Alternatives considered:**
- **Option A: Pure LLM three-way router** — Clean single path but LLM latency/failure on control commands is unacceptable. Partially adopted for feedback/new_task classification.
- **Option B: Prefix convention hybrid** — Zero latency, zero misroute for control. Poor UX for task submission and feedback. Partially adopted: deterministic pre-filter for control IS prefix-style reliability. Rejected only for task submission and feedback where NL provides genuine value.

**Why chosen:** Hybrid approach — deterministic pre-filter for control (Option B reliability) + LLM classification for feedback vs new_task (Option A expressiveness). Best of both.

**Consequences:** Two code paths for NL messages (pre-filter + LLM fallback). Pre-filter keyword list must be maintained. LLM misroute risk remains for feedback/new_task boundary (~5%).

**Follow-ups:** Feature flag to disable NL classification and fall back to prefix-only. Rate limiting on NL messages. Operator confirmation UX refinement.

---

## RALPLAN-DR Summary

### Principles
1. **Star topology preserved** — All Discord I/O through clawhip
2. **Pipeline authority** — Every work item flows through full stage pipeline
3. **Semantic event separation** — Different lifecycle events are distinct types
4. **Minimal coupling** — Extend existing interfaces, no new communication primitives
5. **Operator clarity** — Discord as natural control plane

### Decision Drivers
1. NL classify accuracy — control commands must be deterministic (pre-filter)
2. Pipeline integrity — NL-initiated tasks enter full pipeline via TaskSignal
3. Implementation cost — layer on existing `_run_claude`, mutations, signals

---

## Requirements

1. Three-way NL intent classification: control, feedback, new_task
2. Deterministic pre-filter for control keywords before LLM
3. Pipeline pause/resume via NL or `!` command
4. NL-initiated task creation via TaskSignal → full pipeline
5. Task completion notification distinct from subtask commits
6. All existing tests continue to pass

## Acceptance Criteria

| # | Criterion | Verification |
|---|-----------|-------------|
| AC-1 | "stop" or "pause" in Discord → pipeline pauses, confirmation sent | Send NL, verify `state.paused == True`, Discord confirms with task/stage info |
| AC-2 | "resume" in Discord → pipeline resumes | Send NL, verify `state.paused == False`, stage dispatch resumes |
| AC-3 | "fix the auth bug in broker.py" → TaskSignal created → classify → full pipeline | Send NL, verify signal file in task_dir, state activates and advances |
| AC-4 | "tell the architect to focus on error handling" → routed to architect FIFO | Send NL feedback, verify `[OPERATOR]` in architect FIFO |
| AC-5 | Ambiguous intent → clarification or safe fallback | Send ambiguous NL, verify no misroute to control path |
| AC-6 | Local git commit → `#dev` channel via clawhip `git.commit` route | Verify existing clawhip route works (no new code) |
| AC-7 | Task merge completes → `notify('task_completed', ...)` → `#agents` | do_merge + do_wiki succeeds, verify notification sent |
| AC-8 | Full test suite passes (294 tests, 65 discord companion) | `pytest harness/tests/ -x -q` green |
| AC-9 | 10+ new tests covering pre-filter, classify_intent, control, new_task | Count new test functions |
| AC-10 | `!status` shows paused state when paused | Pause pipeline, run `!status`, verify output |

## Implementation Steps

### Step 1: Deterministic control pre-filter in `harness/discord_companion.py` (~15 lines)

Add to `handle_raw_message()` BEFORE any LLM call:

```python
_CONTROL_WORDS = {"stop", "pause", "halt", "resume", "unpause", "status"}
_CONTROL_PATTERN = re.compile(
    r"^(stop|pause|halt|resume|unpause|status)(\s+(the\s+)?(pipeline|harness|everything))?[.!]?$",
    re.IGNORECASE
)
```

If match: dispatch directly to `_handle_control()`. No LLM call.
If no match: proceed to `classify_intent()` for LLM classification.

### Step 2: `classify_intent()` in `harness/lib/claude.py` (~25 lines)

- Signature: `async def classify_intent(message: str, has_active_task: bool, config: ProjectConfig) -> str`
- Returns: `"feedback"` or `"new_task"` (control already handled by pre-filter)
- Model: haiku via `_run_claude(..., model="haiku")`
- System prompt: "Classify operator message as 'feedback' (comment/instruction for active agent) or 'new_task' (request for new work). Reply with exactly one word."
- Timeout: 10s (reuse `classify_target_timeout` from config.timeouts)
- On failure/timeout: default to `"feedback"` (safe — routes to agent via existing path)
- Note: `classify_intent()` is a separate function from `classify_target()`. Different output space, different purpose.

### Step 3: Three-way dispatch in `harness/discord_companion.py` (~50 lines)

Modify `handle_raw_message()` flow:
1. `!` prefix → existing `handle_message()` (unchanged)
2. Control pre-filter match → `_handle_control(text)`
3. `classify_intent()` → `"new_task"` → `_handle_new_task(text)`
4. `classify_intent()` → `"feedback"` → `_route_natural_language(text)` (existing)

New method `_handle_control(text: str) -> str`:
- "stop"/"pause"/"halt" → queue mutation: `state.paused = True`
- "resume"/"unpause" → queue mutation: `state.paused = False`
- "status" → delegate to `_format_status()`
- Return confirmation: "Pipeline paused. Active task {X} frozen at stage {Y}. Health checks continue."

New method `_handle_new_task(text: str) -> str`:
- Generate task_id from timestamp via existing pattern
- Write TaskSignal via `write_signal()` from `harness/lib/signals.py`
- Return: "Task created: {task_id} — '{description}'"
- Orchestrator's existing `next_task()` polling picks it up

Constructor addition: `pipeline_state_fn` NOT added. Companion stays mutation-only. Control reads happen inside mutation closures at orchestrator apply time.

### Step 4: Pipeline pause mechanism in `harness/lib/pipeline.py` (~15 lines)

- Add `paused: bool = False` field to `PipelineState` dataclass
- Included in `asdict()` serialization / `load()` deserialization automatically
- Persistent across restarts (correct behavior — operator must explicitly unpause)

In `harness/orchestrator.py` main loop (~10 lines):
- Check `state.paused` at line ~504, BEFORE the `match state.stage` block
- When paused: skip stage advancement AND new task pickup
- Continue: health checks, session rotation, mutation processing
- `!status` output updated to show paused state

### Step 5: Task completion notification in `harness/orchestrator.py` (~10 lines)

- In `do_wiki()`, after `event_log.record("task_completed", ...)`:
  - Add `await notify("task_completed", "orchestrator", f"Task {task_id} completed: {description}")`
- Subtask commits: already handled by clawhip `git.commit` route → `#dev` channel. No new code needed. Verify route works during testing.

### Step 6: Tests in `harness/tests/test_discord_companion.py` (~80 lines)

- `test_control_prefilter_stop()` — "stop" → `_handle_control`, not LLM
- `test_control_prefilter_pause_the_pipeline()` — "pause the pipeline" matches pattern
- `test_control_prefilter_case_insensitive()` — "STOP" matches
- `test_noncontrol_goes_to_classify()` — "fix the bug" → `classify_intent()`, not pre-filter
- `test_classify_intent_feedback()` — mock `_run_claude` returns "feedback" → route to agent
- `test_classify_intent_new_task()` — mock returns "new_task" → TaskSignal created
- `test_classify_intent_timeout_defaults_feedback()` — mock timeout → defaults to feedback
- `test_handle_control_pause_mutation()` — verify mutation sets `state.paused = True`
- `test_handle_control_resume_mutation()` — verify mutation clears `state.paused`
- `test_handle_new_task_writes_signal()` — verify TaskSignal file written
- `test_paused_pipeline_skips_dispatch()` — orchestrator test: paused → no stage advancement
- `test_paused_pipeline_applies_mutations()` — orchestrator test: paused → mutations still apply

### Step 7: Agent presence documentation (wiki only, no code)
- Already documented in `v5-conversational-discord-operator.md` (Agent Presence Model section added this session)
- No code changes needed — presence is emergent from pieces 1+2

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Control keyword misroute | High → Low | Deterministic pre-filter catches unambiguous control. LLM never sees "stop" alone |
| "tell executor to stop" misclassified as control | Medium | Pre-filter only matches standalone words/phrases, not embedded in longer sentences |
| NL task from casual remark | Low | Task creation returns confirmation with task_id. Operator can `!cancel` |
| classify_intent failure | Low | Pre-filter handles control. Failure defaults to feedback (safe route to agent) |
| Paused pipeline not visible | Low | `!status` updated to show paused state and frozen task info |
| Pause persists across crash | None | Correct behavior — operator explicitly paused, must explicitly resume |

## Verification Steps

1. `python3 -m pytest harness/tests/ -x -q` — full suite passes (294+ tests)
2. Start harness, send "stop" in Discord → verify pipeline pauses, confirmation received
3. Send "resume" → verify pipeline resumes
4. Send "fix the broker timeout bug" → verify TaskSignal created, pipeline activates
5. Send "tell the architect to check the escalation flow" → verify routed as feedback to architect FIFO
6. Send "halt the pipeline" → verify pre-filter catches it (no LLM call)
7. Trigger executor commit → verify `#dev` notification via clawhip git.commit route
8. Complete a task through merge → verify task_completed notification in `#agents`

## Files Modified

| File | Change | Lines |
|------|--------|-------|
| `harness/discord_companion.py` | Control pre-filter, three-way dispatch, `_handle_control()`, `_handle_new_task()` | ~65 |
| `harness/lib/claude.py` | `classify_intent()` | ~25 |
| `harness/lib/pipeline.py` | `paused` field on PipelineState | ~5 |
| `harness/orchestrator.py` | Pause guard in main loop, task completion notify | ~20 |
| `harness/tests/test_discord_companion.py` | 12 new tests | ~80 |

**Total: ~195 lines across 5 files**

## Changelog (from review)

- Added deterministic control pre-filter before LLM (Architect recommendation)
- Specified paused flag location in main loop: before `match state.stage`, skips stage advancement + new task pickup, continues health checks + mutations (Critic Major 1)
- Clarified AC-6: clawhip git.commit route already covers subtask commits, verify don't duplicate (Critic Major 2)
- Added `notify('task_completed', ...)` in `do_wiki()` for AC-7 (Critic Major 2)
- Revised Option B analysis: pre-filter IS partial Option B adoption (Critic Major 3)
- Added AC-10: `!status` shows paused state
- Corrected AC-8 test count: 294 total (65 discord companion)
- classify_intent reduced to two-way (feedback/new_task) since control handled by pre-filter
