---
title: v5 Harness Reviewer Findings
tags: [harness, security, review, quality]
category: decision
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Reviewer Findings

Two rounds of review. Round 1: 3 parallel reviews (architect, security, code quality) found 10 issues. Round 2: 3 granular layer reviews (data, session, logic) found 17 more. All fixed, 35/35 tests passing.

## Security Review (was: REJECT, now resolved)

### Fixed: Path Traversal via task_id (HIGH)
`task_id` was used directly in path construction (`signal_dir / f"{task_id}.json"`). A crafted `task_id` containing `../../` could escape the signal directory. The archive glob `*{task_id}*` was particularly dangerous.

**Fix**: Added `_safe_task_id()` validator in `signals.py` — regex `[a-zA-Z0-9_\-]+` enforced on all path-constructing methods (`read_escalation`, `read_architect_resolution`, `check_stage_complete`, `archive`).

### Fixed: FIFO Permissions (MEDIUM)
`os.mkfifo(fifo_path)` used default umask (0o644). Other local users could write to the FIFO in `/tmp/harness-sessions`.

**Fix**: `os.mkfifo(fifo_path, mode=0o600)`.

### Accepted Risk: importlib from TOML (MEDIUM)
`discord_companion.py` loads `commands_module` from TOML via `importlib.import_module()`. Accepted because config files are trusted (same repo, not user-supplied).

### Accepted Risk: Shell command in session launch (MEDIUM)
`bash -c` with f-string. All inputs come from hardcoded `AgentDef`, never from user input. Acceptable for Phase 1.

## Architect Review (APPROVE with fixes)

### Fixed: Log-after-clear in do_wiki
`state.clear_active()` nullified `active_task` before `logger.info()` tried to log it.

**Fix**: Capture `task_id` before calling `clear_active()`.

### Fixed: Missing escalation_wait match arm
`lifecycle.py` sets state to `escalation_wait` during recovery, but the match block had no case for it — silent stall on crash recovery.

**Fix**: Added `case "escalation_wait" | "escalation_tier1": pass` with comment.

### Documented: O_NONBLOCK divergence
Plan specified blocking `os.O_WRONLY`. Implementation uses `O_NONBLOCK` with restart-on-full handler. This is better — blocking would stall the event loop.

## Code Quality Review (COMMENT, no critical)

### Fixed: Triplicated VALID_LEVELS (HIGH)
Same frozenset defined in `pipeline.py`, `sessions.py`, and `discord_companion.py` under two different names.

**Fix**: Single definition in `pipeline.py` as `VALID_CAVEMAN_LEVELS`, imported as `VALID_LEVELS` by the other two.

### Fixed: Shared mutable CAVEMAN_DIRECTIVES (HIGH)
Module-level dict in `sessions.py` copied by reference to `claude.py`. Stale on re-instantiation.

**Fix**: Directives stored on `CavemanConfig.directives` field. Both modules access via `config.caveman.directives`.

### Fixed: do_merge wrong git syntax (MEDIUM)
`git merge --no-ff <worktree-path>` is invalid. Git merge takes a branch ref.

**Fix**: Uses `task/{task_id}` branch name instead.

### Fixed: create_worktree ignoring failure (MEDIUM)
No exit code check after `git worktree add`.

**Fix**: `await proc.communicate()` + `returncode` check with error logging.

### Fixed: classify gets task_id not description (LOW)
LLM classifier received opaque ID like `task-001` instead of the task description.

**Fix**: Added `task_description` field to `PipelineState`, populated on `activate()`.

### Fixed: Inline __import__ (LOW)
Shutdown used `__import__("datetime")` instead of a proper import.

**Fix**: `from datetime import datetime, UTC` at module level.

## Round 2: Granular Layer Reviews

Three focused reviews (data layer, session layer, logic layer) found 17 additional issues. 13 fixed, 4 deferred.

### Fixed: Session.pid never set — health checks dead (CRITICAL)
`Session.pid` defaulted to `None` and was never assigned. `check_sessions()` and `reconcile()` both skipped every session. Health monitoring was a complete no-op.

**Fix**: After `launch()`, capture tmux pane PID via `tmux list-panes -t agent-{name} -F '#{pane_pid}'`.

### Fixed: create_worktree failure doesn't stop pipeline (CRITICAL)
`create_worktree` logged an error on failure but still returned the path. Main loop proceeded to activate the task and launch executor into a nonexistent directory — permanent stall.

**Fix**: Returns `None` on failure. Main loop checks result and skips the task with an error log.

### Fixed: send() only catches BlockingIOError (HIGH)
Dead tmux sessions raise `BrokenPipeError` (EPIPE) or `OSError` (EBADF), not `BlockingIOError`. These crashed the conductor loop.

**Fix**: Catches `OSError` broadly. Retry after restart also wrapped in try/except to prevent recursive crash.

### Fixed: do_merge timeout leaks zombie process (HIGH)
`asyncio.wait_for(proc.communicate(), timeout=180)` raised unhandled `TimeoutError`. Pytest process became an orphan zombie, and the merge was left unreversed.

**Fix**: try/except `TimeoutError` → `proc.kill()` + `await proc.wait()` + `git revert -m 1 HEAD`.

### Fixed: _run_claude zombie on timeout (HIGH)
`proc.kill()` without `await proc.wait()` left zombie PIDs accumulating.

**Fix**: Added `await proc.wait()` after `proc.kill()`.

### Fixed: check_reviewer silent stall on reformulate failure (HIGH)
When `reformulate()` returned None, executor stage was set but no message was sent. Pipeline appeared active but nothing happened until retries exhausted.

**Fix**: Sends raw feedback as fallback: `[RETRY] {feedback}`.

### Fixed: set_all/__all__ sentinel dead code (HIGH)
`set_all()` stored a `__all__` key in overrides, but `level_for()` never checked it. Dynamic agent names (Phase 2) would get the default level, not the "all" override.

**Fix**: `level_for()` now checks `__all__` as fallback before `self.default_level`.

### Fixed: do_merge missing cwd (MEDIUM)
`git merge`, `git revert`, and `pytest` inherited harness process CWD. If launched from a different directory, all commands target the wrong repo.

**Fix**: All `create_subprocess_exec` calls in `do_merge` pass `cwd=config.project_root`.

### Fixed: git revert on merge commit needs -m 1 (MEDIUM)
`git revert --no-edit HEAD` on a `--no-ff` merge commit errors with "commit is a merge but no -m option was given".

**Fix**: `git revert --no-edit -m 1 HEAD`.

### Fixed: set_agent/set_all accept invalid levels (MEDIUM)
Runtime mutation methods had no validation. `!caveman turbo` would silently store garbage.

**Fix**: `set_agent()` validates against `VALID_CAVEMAN_LEVELS`, raises `ValueError`.

### Fixed: from_toml doesn't validate default_level (MEDIUM)
`default_level = "turbo"` in TOML loaded without error.

**Fix**: `default_level` included in the validation loop.

### Fixed: shutdown() no forced kill (MEDIUM)
After 5s grace period, tmux sessions that ignored EOF survived as orphans.

**Fix**: `tmux kill-session` for each session after grace period.

### Deferred to Phase 2

- `_processed` set unbounded growth (bounded by task volume, acceptable for Phase 1)
- `PipelineState.load` unknown key handling (filter vs catch TypeError)
- Frontmatter stripping fragility (works for current SKILL.md)
- Stage string validation enum (match block catches unknown stages silently)

## Cross-References

- [[v5-harness-architecture]] — module overview and pipeline flow
- [[v5-harness-design-decisions]] — rationale behind key choices
- [[v5-harness-known-bugs]] — bugs found during review, tracked for future phases
