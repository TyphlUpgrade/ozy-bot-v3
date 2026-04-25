---
title: WA-7 Live Verification Evidence
slug: v5-harness-wa7-evidence
date: 2026-04-24
tags: [harness-ts, propose-then-commit, wa-7, live-sdk, evidence]
---

# WA-7 — Live Verification Evidence

Final wave of the propose-then-commit redesign (`ralplan-harness-ts-propose-then-commit.md`). Two real-SDK runs against `master` after the autoCommit hotfix and `.omc/` gitignore landed.

## Run summary

| Run | Script | Result | Cost | Notes |
|---|---|---|---|---|
| r1 | `live-project-3phase.ts` | API 529 | $0.00 | Anthropic capacity, retried |
| r2 | `live-project-3phase.ts` | API 529 | $0.00 | Anthropic capacity, retried |
| r3 | `live-project-3phase.ts` | FAIL | $0.75 | Exposed `git add --all -- :!.omc :!.harness` fatal — fixed in `64f03a0` |
| r4 | `live-project-3phase.ts` | partial | $0.73 | Phase 1+2 pass, Phase 3 hit `.omc/project-memory.json` rebase conflict — fixed in `ba662dc` |
| r5 | `live-project-3phase.ts` | **PASS** | $0.72 | All 3 phases done, propose-then-commit verified |
| r6 | `live-project-mass-phase.ts` | **PASS 11/11** | $0.90 | 7 phases, 7 orchestrator-authored commits, 7 merges |

Combined cost: ~$3.10 of $5 ceiling.

## Trunk invariants (verified on both scratch repos)

- ✅ Zero `.harness/` files on trunk
- ✅ Zero `.omc/` files on trunk
- ✅ All commits authored by orchestrator (configured `user.email`/`user.name`), not Executor
- ✅ Subjects match `formatCommitMessage` template: `harness: <taskId> — <summary…>`
- ✅ Trailers present: `Model: <name>`, `Session: <uuid>`, `Phase: <id>`

Sample trailer block (mass-phase r2, `c801958`):
```
harness: project-9106aaa0-…-phase-06 — Add src/math/pow.ts with pow funct…

Model: claude-sonnet-4-6
Session: d7fba003-f781-48c4-a4d5-59313c284f7f
Phase: phase-06
```

## Mass-phase 11-check breakdown (r6)

| # | Check | Result |
|---|---|---|
| 1 | architect_spawned | ✅ |
| 2 | project_decomposed phaseCount ≥ 7 | ✅ (7) |
| 3 | task_picked_up ≥ 7 | ✅ (7) |
| 4 | session_complete success ≥ 7 | ✅ (11) |
| 5 | task_failed == 0 | ✅ |
| 6 | task_done ≥ 7 | ✅ (7) |
| 7 | project_completed == 1 | ✅ |
| 8 | project.state == completed | ✅ |
| 9 | all 7 files on trunk | ✅ |
| 10 | ≥ 7 merge commits on trunk | ✅ |
| 11 | wall < 30 min | ✅ (8m21s) |

## Defects exposed by live runs (and fixed)

1. **`64f03a0`** — `autoCommit` used pure-exclusion pathspec `:!.omc :!.harness` which fatals against gitignored paths. Resolution: `git add --all` (no pathspec); rely on trunk gitignore.
2. **`ba662dc`** — scratch-repo gitignore omitted `.omc/`; OMC plugin per-session state landed in worktree and rebase-conflicted across phases. Resolution: add `.omc/` to scratch-repo gitignore.

Both defects survived the unit-test layer because the mock `MergeGitOps` did not exercise real `git add` against gitignored paths. The new test in `merge-git.test.ts` (gitignored-`.harness` regression) closes that gap.

## Phase 4 reviewer follow-ups (`9198124`)

Independently of WA-7, three Phase 4 validators ran on commits `5948d3f..9c68cd9`. Findings landed in the follow-up commit:

- Security HIGH ×2 — `mergeNoFf` / `rebase` shell injection (pre-existing; converted to argv form).
- Security LOW ×1 — `formatCommitMessage` newline injection in trailers (sanitize `\r\n`).
- Code-review HIGH ×2 — subject-length budget; recovery summary forgery.
- Code-review MED ×3 — off-by-one in `MAX_RECOVERY_ATTEMPTS`; `scrubHarnessFromHead` partial-failure swallowing; sdk.ts `modelName` clobber risk.

## Final state

- Commits: WA-1..WA-7 + Phase 4 fixes + 2 hotfixes
- Tests: 575/575 (plan target +32 from 543 baseline)
- Lint: clean (`tsc --noEmit`)
- Build: clean (`tsc`)
- Plan: all 7 waves complete; Phase 4 validators all green after fixes; live verification end-to-end PASS.
