# v5 Harness Phase 4: Wiki + Documentation — Implementation Plan

**Date:** 2026-04-09  
**Status:** Autopilot Phase 1 output

## Summary

Replace 3 placeholder strings in `do_wiki()` with real data accumulated during the pipeline. Add 3 fields to `PipelineState`, collect data at stage-completion points, add fallbacks and a `wiki_failed` event.

## Files to modify

| File | Changes |
|------|---------|
| `harness/lib/pipeline.py` | Add 3 fields to `PipelineState`, reset in `clear_active()`, include in `to_dict()`/`from_dict()` |
| `harness/orchestrator.py` | Collect plan_summary in `check_stage`, diff_stat in `do_merge`, review_verdict in `check_reviewer`, pass real data in `do_wiki` |
| `harness/tests/test_pipeline.py` | Test new fields: default None, persist through save/load, reset on clear_active |
| `harness/tests/test_orchestrator.py` | Test data collection at each stage, fallbacks, diff stat capture, wiki_failed event |

## Step 1: Add PipelineState fields

**File:** `harness/lib/pipeline.py`

Add after `shelved_tasks` (line 263):
```python
plan_summary: str | None = None        # architect plan output, collected in check_stage
diff_stat: str | None = None           # git diff --stat, collected in do_merge
review_verdict: str | None = None      # reviewer verdict, collected in check_reviewer
```

In `activate()` (line 265-277) — add `self.plan_summary = None`, `self.diff_stat = None`, `self.review_verdict = None` after line 277. Without this, consecutive tasks leak wiki metadata.

In `clear_active()` (line 300-311) — add resets for the 3 new fields (alongside existing field resets).

In `shelve()` (line 313-330) — add the 3 fields to the shelved dict so accumulated data survives shelving during escalation.

In `unshelve()` (line 332-352) — restore the 3 fields from the shelved dict with `.get()` defaulting to None.

In `to_dict()` — include the 3 fields (they'll serialize as null when None via `asdict()`).

In `from_dict()` — read them with `.get()` defaulting to None (backwards compatible).

## Step 2: Collect plan_summary in check_stage

**File:** `harness/orchestrator.py`, `check_stage()` (line 49-75)

After the stage completion signal is read (line 53-54), when `stage == "architect"`:
```python
if stage == "architect" and isinstance(result, dict):
    state.plan_summary = result.get("output", "") or result.get("plan", "")
```

This captures the architect's plan output before it gets summarized for the executor. The full text goes to wiki; the summarized version goes to the executor (existing behavior unchanged).

## Step 3: Collect review_verdict in check_reviewer

**File:** `harness/orchestrator.py`, `check_reviewer()` (line 78-110)

On the approval path (line 86-88), store the verdict:
```python
if verdict == "approve" or verdict == "approved":
    state.review_verdict = result.get("feedback", "") or "approved"
    state.advance("merge")
```

This captures any reviewer feedback/notes alongside the approval.

## Step 4: Capture diff_stat in do_merge

**File:** `harness/orchestrator.py`, `do_merge()` (line 113-173)

After merge + tests pass (before `state.advance("wiki")` at line 171), run git diff --stat:
```python
diff_proc = await asyncio.create_subprocess_exec(
    "git", "diff", "--stat", "HEAD~1",
    cwd=cwd,
    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
)
diff_out, _ = await diff_proc.communicate()
state.diff_stat = diff_out.decode().strip() if diff_proc.returncode == 0 else None
```

## Step 5: Pass real data in do_wiki

**File:** `harness/orchestrator.py`, `do_wiki()` (line 176-214)

Replace placeholder arguments (lines 183-186):
```python
success = await claude.document_task(
    task_id=task_id,
    description=description,
    plan_summary=state.plan_summary or "(no architect plan)",
    diff_stat=state.diff_stat or "(no file changes)",
    review_verdict=state.review_verdict or "approved",
    config=config,
)
```

Add `wiki_failed` event:
```python
if not success:
    logger.warning("Wiki documentation failed for %s — continuing", task_id)
    await event_log.record("wiki_failed", {"task": task_id})
```

## Step 6: Tests

### Pipeline tests (`test_pipeline.py`)
- `test_new_fields_default_none` — fresh PipelineState has None for all 3
- `test_new_fields_persist_save_load` — set values, save, load, verify preserved
- `test_clear_active_resets_new_fields` — set values, clear_active, verify None
- `test_activate_resets_wiki_fields` — set values, activate new task, verify None
- `test_shelve_unshelve_preserves_wiki_fields` — set values, shelve, unshelve, verify restored

### Orchestrator tests (`test_orchestrator.py`)
- `test_check_stage_stores_plan_summary` — architect signal with "output" key -> state.plan_summary set
- `test_check_stage_no_plan_summary_on_executor` — executor signal doesn't set plan_summary
- `test_check_reviewer_stores_verdict` — approved signal with feedback -> state.review_verdict set
- `test_check_reviewer_verdict_overwritten_on_retry` — reject then approve -> final verdict stored
- `test_do_merge_captures_diff_stat` — mock git diff --stat -> state.diff_stat set
- `test_do_merge_diff_stat_failure` — git returns non-zero -> state.diff_stat is None
- `test_do_wiki_passes_real_data` — set all 3 fields, verify document_task called with real values
- `test_do_wiki_fallbacks_on_none` — leave fields None, verify fallback strings passed
- `test_do_wiki_records_wiki_failed_event` — document_task returns False -> event_log has wiki_failed
- `test_do_merge_no_worktree_diff_stat_stays_none` — worktree is None, diff_stat stays None after do_merge

## Risks

| Risk | Mitigation |
|------|------------|
| `result` dict structure varies between signal types | Use `.get()` with fallback — never crash on missing keys |
| `git diff --stat HEAD~1` fragile if concurrent merges | Single-task pipeline (no concurrent merges by design) |
| New fields break existing save/load | `.get()` with None default in `from_dict` — backwards compatible |
