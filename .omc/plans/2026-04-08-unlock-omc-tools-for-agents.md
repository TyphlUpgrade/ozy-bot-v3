# Migration Plan: Unlock OMC Tools for Pipeline Agents

**Date:** 2026-04-08
**Status:** Draft
**Scope:** `tools/conductor.sh` (primary), agent role files (secondary)

## Problem Statement

The agentic pipeline was designed as "a harness for multiple OMC instances" but agents
run with `claude -p --allowedTools "Read,Bash,Glob,Grep"` — a whitelist of 4-6 basic
tools. Meanwhile, **50+ OMC tools are loaded and available** but agents can't reach them
because unapproved tools silently fail in `-p` mode with no TTY for permission prompts.

### Empirical Evidence

Tested `claude -p --verbose --include-hook-events --output-format stream-json` and confirmed:

- **All hooks fire in `-p` mode**: SessionStart (3 hooks), UserPromptSubmit (2), PreToolUse,
  PostToolUse, Stop (3) — full OMC lifecycle
- **All MCP servers connect**: `plugin:oh-my-claudecode:t` (connected), `context7` (connected),
  `exa` (connected), `filesystem` (connected)
- **All 50+ OMC tools available**: LSP (goto-definition, find-references, hover, diagnostics,
  code-actions, rename), AST grep (search/replace), python REPL, notepad, project memory,
  state management, session search, trace tools
- **Plugins load**: oh-my-claudecode v4.11.1, caveman — both loaded from cache
- **Skills and agents registered**: all OMC skills and agent types available

The migration does **not** require switching from `-p` to interactive mode. The fix is
purely a permission model change.

### Secondary Bug

`--output-format stream-json` requires `--verbose` in `-p` mode. Current `spawn_agent`
omits `--verbose`, so audit logging is broken (empty or error output in agent logs).

## Requirements Summary

1. Agents must have access to all OMC MCP tools (LSP, AST grep, python REPL, etc.)
2. Read-only agents (architect, reviewer) must not be able to write/edit files
3. Audit logging must capture tool calls and hook events
4. Per-role model routing (opus for architect, sonnet for executor/reviewer)
5. Caveman compression must continue working for executor/reviewer
6. No changes to signal-file bus, completion detection, or pipeline flow

## Acceptance Criteria

- [ ] Architect agent can use LSP go-to-definition and AST grep search in `-p` mode
- [ ] Executor agent can use LSP diagnostics, AST grep replace, and python REPL in worktree
- [ ] Reviewer agent can use LSP diagnostics and AST grep search
- [ ] Architect and reviewer cannot use Write or Edit tools (enforced by `--disallowedTools`)
- [ ] Agent log files contain JSONL with tool call records and hook events
- [ ] Architect runs on opus, executor and reviewer run on sonnet
- [ ] Caveman compression active for executor/reviewer, inactive for architect
- [ ] End-to-end: `!fix` task uses MCP tools during execution (visible in agent logs)
- [ ] Project deny rules (`rm -rf`, `sudo`, `force push`) still enforced under new permission model

## Design

### Core Insight

Replace the **whitelist** model (`--allowedTools` = only these 4 tools) with a
**blacklist** model (`--permission-mode dontAsk` + `--disallowedTools` = everything
except these). This inverts the default from "deny all, allow few" to "allow all,
deny specific" — which is correct for controlled pipeline agents.

Safety net:
- `--disallowedTools "Write,Edit,NotebookEdit"` enforces read-only for architect/reviewer
- Project `.claude/settings.json` deny rules block `rm -rf`, `sudo`, `git push --force`, etc.
- Executor runs in isolated worktree — damage cannot reach main repo
- Agent prompts are controlled by the conductor, not user-facing

### Changes to `spawn_agent()` — `tools/conductor.sh:169-177`

**Current:**
```bash
spawn_agent() {
  local prompt_file="$1" workdir="$2" log_file="$3" allowed_tools="${4:-}"
  local tools_flag=""
  [ -n "$allowed_tools" ] && tools_flag="--allowedTools $allowed_tools"
  tmux split-window -t "$TMUX_SESSION" -d -P -F '#{pane_id}' \
    "cd '$workdir' && claude -p $tools_flag --output-format stream-json \
     < '$prompt_file' > '$log_file' 2>&1; echo '[AGENT_DONE]' >> '$log_file'"
}
```

**New:**
```bash
spawn_agent() {
  local prompt_file="$1" workdir="$2" log_file="$3"
  local disallowed_tools="${4:-}" model="${5:-}"
  local deny_flag="" model_flag=""
  [ -n "$disallowed_tools" ] && deny_flag="--disallowedTools $disallowed_tools"
  [ -n "$model" ] && model_flag="--model $model"
  tmux split-window -t "$TMUX_SESSION" -d -P -F '#{pane_id}' \
    "cd '$workdir' && claude -p --verbose --permission-mode dontAsk \
     $deny_flag $model_flag \
     --output-format stream-json --include-hook-events \
     < '$prompt_file' > '$log_file' 2>&1; echo '[AGENT_DONE]' >> '$log_file'"
}
```

Key changes:
- `--allowedTools` (whitelist) → `--disallowedTools` (blacklist) — inverted permission model
- `--permission-mode dontAsk` — auto-approve all non-denied tools (unlocks MCP)
- `--verbose` — required for `--output-format stream-json` (fixes broken audit logging)
- `--include-hook-events` — hook lifecycle visible in audit logs
- `--model` — per-role model routing
- Parameter order changed: `allowed_tools` (4th) → `disallowed_tools` (4th), added `model` (5th)

### Changes to `launch_architect()` — `tools/conductor.sh:279-314`

**Current call (line 310):**
```bash
pane_id=$(spawn_agent "$prompt_file" "$PROJECT_ROOT" "$agent_log" "Read,Bash,Glob,Grep")
```

**New call:**
```bash
pane_id=$(spawn_agent "$prompt_file" "$PROJECT_ROOT" "$agent_log" "Write,Edit,NotebookEdit" "opus")
```

- 4th param: disallowed tools (read-only enforcement)
- 5th param: model (opus for deep architectural analysis)

### Changes to `launch_executor()` — `tools/conductor.sh:316-369`

**Current call (line 365):**
```bash
pane_id=$(spawn_agent "$prompt_file" "$wt_path" "$agent_log" "Read,Write,Edit,Bash,Glob,Grep")
```

**New call:**
```bash
pane_id=$(spawn_agent "$prompt_file" "$wt_path" "$agent_log" "" "sonnet")
```

- 4th param: empty string (no restrictions — full access in worktree)
- 5th param: model (sonnet for efficient execution)

### Changes to `launch_reviewer()` — `tools/conductor.sh:372-428`

**Current call (line 425):**
```bash
pane_id=$(spawn_agent "$prompt_file" "$PROJECT_ROOT" "$agent_log" "Read,Bash,Glob,Grep")
```

**New call:**
```bash
pane_id=$(spawn_agent "$prompt_file" "$PROJECT_ROOT" "$agent_log" "Write,Edit,NotebookEdit" "sonnet")
```

- 4th param: disallowed tools (read-only enforcement)
- 5th param: model (sonnet for efficient review)

### Comment updates — `tools/conductor.sh:160-168`

**Current:**
```bash
# Spawn a claude -p agent in a tmux pane. Returns pane ID.
# Permission model: --allowedTools pre-approves known tools (no prompt). Tools not
# in the list trigger an interactive permission prompt, which the conductor detects
# via check_permission_prompt() and proxies to Discord for operator approval.
# Per-role tool lists:
#   Architect: Read,Bash,Glob,Grep           (read-only + signal writes via Bash)
#   Executor:  Read,Write,Edit,Bash,Glob,Grep (full access in worktree)
#   Reviewer:  Read,Bash,Glob,Grep           (read-only + signal writes via Bash)
# Usage: pane_id=$(spawn_agent <prompt_file> <workdir> <log_file> [allowed_tools])
```

**New:**
```bash
# Spawn a claude -p agent in a tmux pane with full OMC tool ecosystem.
# Permission model: --permission-mode dontAsk auto-approves all tools.
# --disallowedTools enforces per-role restrictions (read-only for architect/reviewer).
# OMC hooks fire in -p mode: SessionStart, PreToolUse, PostToolUse, Stop.
# MCP tools available: LSP, AST grep, python REPL, notepad, project memory, etc.
# Audit: --verbose + --output-format stream-json + --include-hook-events = full JSONL trail.
# Per-role restrictions:
#   Architect: Write,Edit,NotebookEdit denied  (read-only, model: opus)
#   Executor:  no restrictions                 (full access in worktree, model: sonnet)
#   Reviewer:  Write,Edit,NotebookEdit denied  (read-only, model: sonnet)
# Usage: pane_id=$(spawn_agent <prompt_file> <workdir> <log_file> [disallowed_tools] [model])
```

### Caveman: No changes needed

Caveman is a **skill** (not a hook). It requires explicit invocation (`/caveman`) and
does not auto-activate in `-p` sessions. The conductor's manual injection via
`get_caveman_block()` is the correct mechanism and should be kept as-is.

- `launch_executor()`: caveman injected (lines 358-362) — keep
- `launch_reviewer()`: caveman injected (lines 419-422) — keep
- `launch_architect()`: no caveman (line 307 comment) — keep

### What does NOT change

- **`run_judgment()` / `run_judgment_no_tools()`** — stateless classification calls,
  don't need MCP tools. Keep `claude -p --permission-mode dontAsk` as-is.
- **Signal file bus** — agents write signal files via Bash tool (unchanged)
- **`check_pipeline()`** — signal file detection, timeout, pane death checks (unchanged)
- **`do_merge()`** — merge, test, revert logic (unchanged)
- **Permission proxy** (`check_permission_prompt`, `handle_permission_prompt`) — still
  useful as a safety net. With `--permission-mode dontAsk`, tools are auto-approved
  and won't trigger prompts. But if a future role uses a different permission mode,
  the proxy is ready. Keep as-is.
- **Main polling loop** — unchanged
- **Caveman injection** — kept for executor/reviewer

## Implementation Steps

### Unit 1: Rewrite `spawn_agent()` signature and flags
- File: `tools/conductor.sh`
- Lines: 160-177
- Change parameter names: `allowed_tools` → `disallowed_tools`, add `model`
- Replace `--allowedTools` with `--permission-mode dontAsk` + `--disallowedTools`
- Add `--verbose`, `--include-hook-events`
- Add `--model` flag
- Update comment block

### Unit 2: Update all three `launch_*()` calls
- File: `tools/conductor.sh`
- `launch_architect()` line 310: `"Read,Bash,Glob,Grep"` → `"Write,Edit,NotebookEdit" "opus"`
- `launch_executor()` line 365: `"Read,Write,Edit,Bash,Glob,Grep"` → `"" "sonnet"`
- `launch_reviewer()` line 425: `"Read,Bash,Glob,Grep"` → `"Write,Edit,NotebookEdit" "sonnet"`

### Unit 3: Smoke test — verify agent gets MCP tools
- Start conductor: `bash tools/ozy up`
- Submit task: `bash tools/ozy task "add a comment to core/trigger_engine.py docstring"`
- Verify in architect log: `init` event shows MCP tools in available tools list
- Verify in architect log: LSP or AST grep tool calls appear
- Verify model field shows `claude-opus-4-6` for architect

### Unit 4: End-to-end validation
- Submit a task that benefits from MCP tools (e.g., "rename _check_triggers to evaluate_triggers")
- Verify executor uses AST grep replace (not manual find-and-replace)
- Verify reviewer uses LSP diagnostics (not just pytest)
- Verify agent logs have hook events (SessionStart, PreToolUse, PostToolUse, Stop)
- Verify deny rules work: architect log should NOT contain Write/Edit tool calls

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Agent uses dangerous bash command | Low | Medium | Project deny rules block rm -rf, sudo, force push. Executor in worktree. |
| MCP server fails to start in -p mode | Low | Low | Graceful degradation — agent falls back to basic tools. Visible in hook events. |
| --disallowedTools flag not recognized | Low | Medium | Verify flag exists in `claude --help`. Fallback: use --allowedTools with full MCP list. |
| Verbose output bloats log files | Medium | Low | Logs already gzipped after 7 days (line 642). Monitor sizes. |
| Model flag conflicts with user settings | Low | Low | Explicit --model overrides user default. Intentional — pipeline controls model choice. |

## Non-Goals

- Switching from `-p` to interactive mode (hooks fire in `-p`, no need)
- Changing caveman mechanism (skill-based, manual injection is correct)
- Modifying signal file bus or completion detection
- Adding new MCP servers
- Changing judgment call infrastructure
