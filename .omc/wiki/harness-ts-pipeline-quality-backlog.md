---
title: Harness-TS Pipeline Quality Backlog (cycle 5+)
description: Quality-lift items observed across ralph cycles 2-4. Each is independently shippable. Listed in ROI order with the design questions that need scoping before code.
category: architecture
tags: ["harness-ts", "backlog", "quality", "pipeline", "cycle-5"]
created: 2026-04-29
updated: 2026-04-29
---

# Harness-TS Pipeline Quality Backlog

Captured 2026-04-29 after ralph cycles 2 (R3 quality gates), 3 (R4 architect §2 amendment + escalation channel + R5 scratch hygiene), and 4 (R6 cascade fail-fast + multi-language test gates + reviewer rubric upgrade). Each item below addresses a failure class observed in those cycles' e2e runs but is out of scope for what was shipped.

Order: highest-ROI first. Each item has a self-contained design-question section so a future ralph or ralplan run can scope it without re-discovery.

## R7 — Test-artifact pollution in autoCommit (REDESIGN PENDING)

**Problem class:** every new tool Architect introduces (pytest, pytest-cov, vitest, tsc, mypy, ...) generates files in the per-phase worktree. autoCommit's `git add -A` sweeps those untracked files into the phase commit. Trunk's later runTests run regenerates the same artifact (untracked), and the next phase's `git merge --no-ff` collides on "untracked files would be overwritten by merge". Cycle 3 patched `__pycache__/` + `*.pyc`; cycle 4 added `.coverage`/`htmlcov/`/`dist/`/`.vitest-cache/`. Each new test runner is a new patch.

**Original "snapshot pre/post Executor + diff" design — REJECTED on closer read.** MergeGate flow (verified 2026-04-29 against `src/gates/merge.ts:315-373`):

1. Worktree starts clean (fresh `git worktree add`).
2. Executor session writes files. Side effects: Executor may run pytest/vitest via Bash to validate work → test artifacts in worktree.
3. autoCommit (`git add --all`) in worktree.
4. Rebase + `merge --no-ff` to trunk.
5. **runTests in TRUNK**, not worktree.

Because worktree starts clean, "snapshot at session start" yields the empty set. Diff at session end = all changes (Executor's intended writes + Bash side effects). Stage = same as `git add --all`. Snapshot adds no signal.

**Fix candidates (re-scoped):**

a) **Forbid Executor from running tests in its session.** Update `DEFAULT_EXECUTOR_SYSTEM_PROMPT` (config.ts) with "Do not run tests via Bash. The harness runs them after Reviewer approval." Executor stops generating test artifacts. autoCommit stays clean. Risk: Executor commits broken code → caught by Reviewer + harness's runTests. ~5 LOC prompt change. **Cheapest, highest ROI.**

b) **SDK tool-use parsing.** Subscribe to SDK Edit/Write/MultiEdit/NotebookEdit tool_use messages during Executor session. Collect file_path arguments. autoCommit stages exactly those paths via `git add <path>`. Bash side-effect files never enter the snapshot. ~80 LOC SDK message parser + edge cases (Executor using Bash sed/cp/echo to write files would be missed).

c) **`git clean -fd` + pattern deny-list before autoCommit.** Run `git clean -fd <known-test-pattern-paths>` to nuke common artifact dirs before staging. Then `git add -A` as today. ~10 LOC + maintained pattern list.

d) **Status quo + R5+ gitignore extension.** Already in cycle 4. Maintenance overhead.

**Recommend:** (a) first — minimal change, highest ROI, defensive backstop via existing R5+ gitignore. If Executor's "no tests" instruction proves unreliable in practice, escalate to (b).

**Files:** `src/lib/config.ts` (DEFAULT_EXECUTOR_SYSTEM_PROMPT), or for (b) `src/session/manager.ts` (subscribe to SDK messages, capture tool-use paths).

**Estimated size:** (a) ~5 LOC + 1-2 tests verifying prompt content. (b) ~80 LOC + tests.

**ROI:** still high. Permanent fix to the gitignore-extension treadmill class.

**History:** original snapshot-based design captured 2026-04-29 in this backlog entry was rejected after reading the actual MergeGate flow (clean worktree start makes pre-snapshot empty → snapshot equivalent to `git add -A`). Corrected analysis above.

## Cross-Session Architect Memory

**Problem class:** same defect classes recur across cycles. Cycle 2 vague-math hit broken pyproject (no `[build-system]`); cycle 4 same project hit it again. ES2024 tsconfig appeared in 2 separate cycle-4 runs. Architect spawns fresh each project with no memory of past arbitrations, so it keeps repeating its own bugs.

**Fix sketch:** extend `.omc/project-memory.json` (or per-repo `.harness/architect-memory.json`) to log plan_amendment rationale strings + verdict triggers; prime Architect's spawn prompt with "Recent fixes touched: <bullet list>" so the model reads it as continuation of its own thought.

**Open questions:**
- **Format:** raw rationale strings (verbose, easy to log) vs curated bug-class summaries (better signal, requires a curation step — who curates? Reviewer? A new agent?).
- **Scope:** per-project (no leakage) vs cross-project (broader learning, possible privacy/leakage concerns when projects diverge).
- **Token tax:** each Architect spawn pays for the memory section. Cap to N recent? Filter by project shape (Python-shape projects only see Python-amendment memories)?
- **Pruning policy:** TTL (entries >30 days drop) vs count-cap vs relevance score. Without pruning, memory grows forever.
- **Bootstrap:** fresh repos start with nothing — first cycles get no benefit. Acceptable.
- **Measurement:** does it actually reduce re-occurrence? Two-arm experiment: same vague prompt, with vs without memory. Need ≥3 runs each arm.
- **Empirical risk:** Architect may anchor too hard on past fixes ("avoid Python 3.10+ syntax forever even when 3.10+ is fine") — risk of over-correction. Phrase memory entries as "consider" not "do not".

**Files:** `.omc/project-memory.json` schema extension, `src/session/architect.ts` (spawn prompt build), `config/harness/architect-prompt.md` (new section explaining how to read the memory block).

**Estimated size:** ~30 LOC + new prompt section + measurement scaffold.

**ROI:** high. Hits the load-bearing failure mode (Architect repeating own bugs).

## Per-Phase Architect Cost Ceiling

**Problem class:** plan_amendment loops can spiral. Cycle 4 r4 phase-01 went 4 retries (~$0.40 alone). No upper bound on amendment cycles per phase — Architect can churn indefinitely on a spec it can't resolve.

**Fix sketch:** add `architect.max_amendments_per_phase` config (default 3?). Track per-phase amendment count in projectStore. When cap hit, force `escalate_operator` regardless of Architect's verdict; reuse cycle-3 R4 escalation channel.

**Open questions:**
- **Granularity:** per-phase vs per-project ceiling. Phase = doom-loop on individual phase. Project = total-spend cap.
- **Unit:** retry count (cleaner mental model) vs $ (varies per retry; project-budget already exists). Probably retry count for this; per-project $ already covered by `project.budgetCeilingUsd`.
- **Trigger semantics:** is the synthetic escalation a `project_failed` (operator-visible failure) or an `escalation_needed` (operator dialogue request)? Latter reuses the dialogue path nicely; former is more honest about "Architect gave up".
- **Conflict with `auto_escalate_on_max_retries`:** that config fires on Executor failures (`max_session_retries`). This is for Architect arbitration loops — different code path. Don't shadow names; pick a distinct field.
- **Same-root-cause detection:** "5 different bugs at 1 retry each" vs "same bug 5 times" should differ. Detection is hard (compare rationale strings? embedding similarity?). Pragmatic v1: count regardless.
- **Edge case:** `retry_with_directive` vs `plan_amendment` — both Architect verdicts trigger another Executor run. Both count toward cap, or only `plan_amendment`? Different semantics: retry_with_directive = "Executor erred"; plan_amendment = "spec was wrong". Latter is what spirals.
- **Test:** mock Architect always emitting plan_amendment → assert orchestrator forces escalation after N.

**Files:** `src/orchestrator.ts` (cap counter + force-escalate logic), `src/lib/config.ts` (new field), `src/lib/project.ts` (per-phase amendment count).

**Estimated size:** ~40 LOC + 2-3 tests.

**ROI:** medium-high. Hardens against doom loops; safety net rather than primary feature.

## Architect Self-Review Pre-Emit

**Problem class:** Architect emits phase prompts that contain self-inconsistent specs (Python `requires-python = '>=3.9'` + `float | int` syntax requiring 3.10+; tsconfig `target: ES2024` + installed `typescript` not supporting it). Reviewer can catch some via the new cycle-4 rubric, but Reviewer is read-only and runs after Executor work.

**Fix sketch:** extend Architect's decomposition prompt with a "Before writing phase files, verify each phase prompt against this checklist" section. Hard-coded landmines: TS target ≤ ES2022; Python type annotation syntax ≤ requires-python version; etc. Architect performs this check itself before writing phase files.

**Open questions:**
- **Maintenance:** landmine list grows over time. Where lives — prompt section or external file?
- **Effectiveness:** prompt-engineered self-checks are flaky. Model may skip the check under generation pressure.
- **Alternative:** static lint stage on Architect's emitted phase prompts (regex / TS AST check) BEFORE Executor ingest. Mechanical, no prompt-engineering drift.
- **Coverage:** can only catch known classes. Each new tooling Architect picks up generates new landmines.

**Files:** `config/harness/architect-prompt.md` (prompt extension), or new `src/lib/phase-validator.ts` (static lint).

**Estimated size:** ~15-30 LOC.

**ROI:** medium. Substitutes for Reviewer's inability to execute on a narrow class of spec defects.

## README-Runs-As-Test Smoke

**Problem class:** Architect produces README install/usage instructions that don't match reality. Cycle 3 r2 README claimed `npm install vague-math` works (package never published). Reviewer rubric upgrade in cycle 4 catches some of this in static review; runtime check would close the gap.

**Fix sketch:** in `final_test_command`, extract code blocks from `README.md`, write to a temp script, execute. If README claims `from vague_math import divide; print(divide(10, 2))` returns 5.0, run it. Failure → project_failed.

**Open questions:**
- **Code block extraction:** triple-backtick fences only, language-tagged (`python`/`typescript`/`javascript`/`bash`). Skip non-code fences.
- **Multi-block handling:** independent execution vs concatenated. Concatenated is simpler; user expects independence usually.
- **Failure attribution:** which block failed? Better to execute one at a time and surface specific block.
- **False positives:** README example may legitimately throw (e.g., showing error case). Need a "this block intended to throw" convention or skip blocks with obvious throw demonstrations.
- **Cost:** adds 10-30s to final smoke. Acceptable.

**Files:** new `scripts/lib/readme-smoke.ts` or extend `scripts/lib/scratch-repo.ts`.

**Estimated size:** ~40 LOC.

**ROI:** medium. Closes a specific lie class; not the load-bearing failure mode.

## Lower ROI / Defer

- **Coverage threshold gate** — Architect's autogen tests usually hit ~80%; diminishing returns vs setup overhead.
- **Mutation / property-based testing** (fast-check / hypothesis) — high signal, high overhead, requires Architect to spec generative tests.
- **Lint/format gate** (eslint / black) — cosmetic; Architect output is already consistent enough.
- **Reviewer-dynamic mode** (runs probes) — breaks load-bearing read-only contract. Rearchitect-tier.

## Suggested cycle order

1. **Cycle 5:** R7 worktree-snapshot-and-diff. Self-contained, retires the gitignore treadmill, no cross-system coupling.
2. **Cycle 6 (after ralplan):** Cross-session Architect memory. Needs design pass before code (format, scope, pruning, measurement).
3. **Cycle 7 (after ralplan):** Per-phase Architect cost ceiling. Policy work; needs scoping (granularity, trigger, conflict with existing escalation paths).
4. **Cycle 8+:** Architect self-review + README smoke if real defects keep slipping. Consider sequencing after empirical evidence from cycles 5-7.

## See also

- [[harness-ts-wave-c-backlog]] — older Wave C items (mostly closed by cycles 2-4)
- [[phase-f-discord-richness-resilience-backlog]] — Discord UX backlog (orthogonal axis)
- [[harness-ts-architecture]] — overall architecture
- [[harness-ts-core-invariants]] — invariants any cycle 5+ change must preserve
