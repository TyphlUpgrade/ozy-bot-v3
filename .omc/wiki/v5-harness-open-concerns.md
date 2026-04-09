---
title: v5 Harness Open Concerns
tags: [harness, concerns, open-issues, engineering]
category: debugging
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Open Concerns

Engineering concerns that are not bugs (no repro steps, no crash) but represent latent risks, performance issues, or unvalidated assumptions. Migrated from Ozy NOTES.md pattern.

**Filing rule:** Bugs go in [[v5-harness-known-bugs]] (repro steps required). Concerns go here (analysis + proposed mitigation).

---

### PERF-1: parse_token_usage O(n) re-read
**Status:** deferred | **Severity:** Low | **First observed:** 2026-04-09 | **Area:** `sessions.py:266-296`

`parse_token_usage()` re-reads the entire stream-json log every poll cycle. O(n) growth per call — each invocation scans from byte 0.

**Mitigation:** Track file offset on Session object, seek to last position, parse only new lines. Deferred because poll frequency is low (~5s) and log files are small during dev.

*Moved from [[v5-harness-known-bugs]] — this is a performance concern, not a bug.*

---

### CONCERN-1: shelved_tasks dict shape not validated
**Status:** open | **Severity:** Low | **First observed:** 2026-04-09 | **Area:** `harness/lib/pipeline.py`

`unshelve()` trusts that dict keys exist (`task_id`, `stage`, `task_description`, etc.). If a shelved dict is corrupted (e.g., manual edit of `state.json`), `KeyError` crash.

**Mitigation:** Add `.get()` with sensible defaults, or validate shape on `unshelve()`. Low priority — corruption requires manual state file editing.

---

### CONCERN-2: Signal file cleanup on task completion
**Status:** open | **Severity:** Low | **First observed:** 2026-04-09 | **Area:** `harness/orchestrator.py`

`archive()` moves signal files on task completion, but if the process crashes between `clear_active()` and `archive()`, orphan signal files accumulate in signal directories. Over long runs, these orphans grow unbounded.

**Mitigation:** Add a startup sweep that detects orphan signals (signal files with no matching active or shelved task) and archives them. Low priority — orphans are inert (read but ignored due to task ID mismatch).

---

### CONCERN-3: Escalation cache not bounded
**Status:** open | **Severity:** Low | **First observed:** 2026-04-09 | **Area:** `harness/orchestrator.py`

`_escalation_cache` grows by one entry per escalated task. Entries are popped on `clear_active()`, but if tasks are abandoned without clearing (crash, manual intervention), the cache grows unbounded. Same class of issue as BUG-001 (`_processed` set growth).

**Mitigation:** Add a bounded cache (e.g., max 100 entries, LRU eviction) or periodic sweep that removes entries for task IDs not in active/shelved state. Low priority — escalation frequency is low.

---

## Cross-References

- [[v5-harness-known-bugs]] — bugs with repro steps (different from concerns)
- [[v5-harness-drift-log]] — plan deviations (different from engineering concerns)
- [[v5-harness-architecture]] — module overview
