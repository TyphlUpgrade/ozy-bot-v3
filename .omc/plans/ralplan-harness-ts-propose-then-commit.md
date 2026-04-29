# Propose-Then-Commit Plan — harness-ts Executor → Reviewer → Orchestrator-Commit Redesign

**Date:** 2026-04-24
**Status:** PLANNER REVISION 2 (DELIBERATE consensus mode — incorporates Architect + Critic iteration-2 feedback; see appendix Q)
**Depends:** Phase 2A (COMPLETE), Phase 2B-3 (`ralplan-harness-ts-three-tier-architect.md` IN-FLIGHT through Wave A/B), Bug 1+2 projectId propagation (`d2388b3`), 543 tests green (commit `8b7a1e4`)
**Working dir:** `/home/typhlupgrade/.local/share/ozy-bot-v3/harness-ts/`

---

## A. Executive Summary

Today the Executor commits its own work into a per-phase git worktree before the Reviewer ever inspects it. Reviewer reads the committed branch state, the orchestrator either merges that branch into trunk or has to tear down a now-tainted branch on reject. This plan inverts the order: Executor writes files but **never** runs `git add` / `git commit`. Reviewer reads the dirty worktree (`git status --porcelain` / `git diff` / `git diff --cached`) and produces a verdict. Orchestrator stages-and-commits **only on Reviewer approve**, then the existing FIFO merge gate rebases + tests + merges into trunk.

**Net effect:** zero unsanctioned Executor commits, no tainted branches on reject, root-cause elimination of `.harness/` rebase conflicts that surfaced in mass-phase stress (hotfixed by gitignore in `8b7a1e4`), simpler retry semantics on below-threshold reject (no commit to revert).

**Core change:** an `enqueueProposed(taskId, worktreePath, branchName, commitMessage)` entry point on `MergeGate` that stages, commits with an orchestrator-authored message, then runs the existing rebase-test-merge pipeline. The legacy `enqueue(taskId, worktreePath, branchName)` becomes a thin compat wrapper detecting "branch already has commits" and skipping the orchestrator-commit step. CompletionSignal.commitSha becomes optional (executor must not have committed; field is preserved for legacy executors that still commit).

---

## B. Principles (load-bearing)

1. **Approval gates state.** No artifact reaches a state that survives reject. A Reviewer reject means the branch never existed in any meaningful sense — the worktree is wiped, the branch (which has zero commits) is deleted. Reviewer approve is the *only* path that produces a commit on the branch and a merge into trunk.
2. **Orchestrator owns commit authorship.** Commit messages, authors, and timestamps must be deterministic across re-runs. Executors may not author commits the operator can't reproduce.
3. **Additive over disruptive.** 543 tests must stay green after expected schema-test updates. Wave boundaries are chosen so `npm run lint` + `npm run build` + `npm test` all pass at every wave-end.
4. **Single source of truth for the diff.** Reviewer reads the *uncommitted* working tree — the same bytes the orchestrator will commit on approve. There is exactly one diff under review.
5. **Backward-compatible degradation.** Legacy Executors that still call `git commit` (older prompts, third-party agents) must continue to work via a fallback path. MergeGate detects the case and skips its own commit step.

---

## C. Decision Drivers (top 3)

1. **`.harness/` rebase conflicts in concurrent phase merges (mass-phase stress).** Hotfix `8b7a1e4` solved the symptom (gitignore + prompt tightening). Root cause: Executor commits orchestrator-internal files. Eliminating Executor commit eliminates the class.
2. **Tainted-branch cleanup on reject.** Below-threshold reject at `21c87a5` already does cleanup-worktree + shelve + scheduleRetry. With propose-then-commit, the cleanup is structurally trivial: branch has zero commits, no orphan SHAs, no revert-merge dance.
3. **Reviewer trust boundary.** A Reviewer that reads committed state implicitly trusts the Executor's commit-message + commit-author metadata. A Reviewer that reads `git diff` reads only file content. The redesign tightens the trust boundary by collapsing it to one source.

---

## D. Viable Options

### Option A — **Full propose-then-commit** (CHOSEN)

Executor writes files only. Reviewer reads `git diff` / `git status --porcelain`. Orchestrator commits on approve. Below-threshold reject → discard worktree (zero commits to clean). MergeGate gets a new `enqueueProposed` entry point.

**Pros:**
- Eliminates `.harness/` rebase-conflict class structurally (root cause).
- Simplest reject semantics (no branch to clean of orphan commits).
- Deterministic commit authorship via orchestrator config (per-phase message from `completion.summary`).
- Reviewer trust surface narrows to file content only.
- Tighter security boundary: a malicious Executor cannot land a forged commit on trunk if it never reaches the orchestrator's stage step.

**Cons:**
- Schema-level break: `commitSha` becomes optional. Touches every test file that asserts a commitSha.
- Three live scripts (`live-project*.ts`) and one default prompt need updating; one re-run under live SDK to verify wave-7 no-Executor-author goal.
- Recovery semantics gain a new edge case: orchestrator crash post-commit, pre-merge. Bounded by inspection of branch-has-1-commit state on restart.

### Option B — **Keep Executor-commit, strip `.harness/` pre-merge via MergeGate**

Status-quo Executor commit; MergeGate adds an explicit `git rm --cached .harness/` step before rebase if the branch contains it. Doesn't change Reviewer mechanics. Note: this is *incremental* to the gitignore hotfix in `8b7a1e4`, not a replacement.

**Pros:**
- Localized change (~30 LoC in `realMergeGitOps.autoCommit` / a new `stripHarness` step).
- Minimal test churn — preserves the existing 543-test surface almost verbatim.
- Preserves Executor's git-native commit ergonomics — model authors a commit, which can be useful as forensic provenance for *what the model thought it did* even when the orchestrator overrides authorship later.
- Lower live-SDK churn — no prompt rewrite required, so existing operator-tuned prompts (third-party scripts, hand-rolled live runs) continue to work without re-validation.
- Easier rollback if a future change breaks the redesign — Option B is a single MergeGate-internal step, can be ablated independently.

**Cons:**
- Treats symptom, not cause. A future orchestrator-internal directory will repeat the bug.
- Tainted-branch problem unsolved (Reviewer still reads committed state, reject still leaves a commit).
- Reviewer-trust-boundary unchanged.
- Doesn't unlock retry-simplification benefit.

**Invalidation rationale (revised):** Option B addresses driver 1 only. The `.harness/` symptom-class can recur for any future orchestrator-internal directory (e.g. `.review/` artifacts). Drivers 2 (tainted-branch cleanup) and 3 (Reviewer trust boundary) require committed-state to never appear before approval — which Option B structurally cannot provide. The trade-off is YAGNI-vs-long-term-architecture: Option B avoids the WA-1/WA-2 schema churn now but pays it back at every future internal-directory addition. We choose A because the redesign cost is bounded (~18 net tests, mechanical) and pays compounding dividends; the forensic-provenance argument for Option B is genuine but addressable in Option A by including the model's intent in `completion.summary` (which already lands in the orchestrator commit message).

### Option C — **Hybrid: Executor commits to a staging branch, orchestrator cherry-picks on approve**

Executor commits as today, but to `harness/staging-{taskId}` instead of `harness/task-{taskId}`. Reviewer reads staging-branch diff. On approve, orchestrator cherry-picks staging onto a fresh `harness/task-{taskId}` and proceeds. On reject, drop the staging branch.

**Pros:**
- Preserves Executor's git-native commit ergonomics.
- Trunk only ever sees orchestrator-cherry-picked commits, so commit-author determinism is achievable.

**Cons:**
- Two branches per task instead of one (worktree + branch lifecycle complexity doubles).
- Cherry-pick can fail with conflicts when `.harness/` lives in staging — same problem class as Option B.
- Reviewer still reads committed state; trust-boundary unchanged.
- Cleanup now needs to track *two* branches per shelved/aborted task; recovery state machine grows.

**Invalidation rationale:** doubles state complexity to recover ergonomics that Executor doesn't actually need (the SDK doesn't care if the model "committed" — only the prompt asks it to). The cherry-pick step reintroduces the conflict surface Option A removes.

### Why A wins

- **Strictly fewer moving parts in the steady state** than B + C combined.
- **Only option that addresses all three drivers.**
- **Test surface is large but mechanical** (mostly removing or making-optional a single field).
- **Live-script churn is bounded** to four prompt strings.

---

## E. RALPLAN-DR Summary

### Principles
1. Approval gates state.
2. Orchestrator owns commit authorship.
3. Additive over disruptive.
4. Single source of truth for the diff.
5. Backward-compatible degradation.

### Decision Drivers
1. Eliminate `.harness/` rebase-conflict class at the root.
2. Trivialize reject cleanup (no committed state to tear down).
3. Narrow Reviewer trust boundary to file content.

### Viable Options
A (chosen), B, C — each pro/con bounded above. A is the only option that scores on all three drivers.

### ADR (advance summary; full ADR in section N)
- **Decision:** propose-then-commit redesign per Option A.
- **Drivers:** see C; principal risk is recovery-state edge case bounded by Wave 6.
- **Alternatives considered:** B (symptom-only, single-driver), C (state explosion).
- **Why chosen:** only option that addresses all three drivers; test surface mechanical.
- **Consequences:** 7 waves, ~543→~561 tests, schema break (commitSha optional), one live-script re-run.
- **Follow-ups:** post-Wave 7, archive Option B's `:!.harness` pathspec exclude in `autoCommit` since the directory will no longer be on the branch.

### Mode
DELIBERATE — high-risk schema change touching every existing completion.json test plus a live-mass-phase re-run. Pre-mortem (section F) and expanded test plan (section I) included.

---

## F. Pre-Mortem (3 failure scenarios + mitigations)

### Scenario 1: Reviewer reads stale committed state because legacy Executor committed before Reviewer ran

**Trigger:** an older `live-project*.ts` script (or third-party operator-supplied prompt) instructs the Executor to commit. Reviewer is wired to read `git diff` (uncommitted) — sees an empty diff, returns approve-on-trivial. Orchestrator's stage-step finds nothing to commit, MergeGate's new `enqueueProposed` short-circuits, but the existing branch commit gets merged unreviewed.

**Mitigation:**
- Reviewer prompt (`config/harness/review-prompt.md` rewrite, **WA-3 in revised order**) instructs the Reviewer to:
  - First check `git status --porcelain` for uncommitted changes.
  - If empty AND `git log {branchName} ^trunk --oneline` is non-empty → review the *committed* diff via `git diff trunk...HEAD`.
  - If both empty → return reject with finding "no diff to review".
- Orchestrator detects "branch has commits AND no uncommitted changes" and emits `legacy_executor_commit` log warning. Compat path proceeds (skip orchestrator-commit, run rebase+tests+merge).
- Wave 7 live verification asserts every default-prompt run produces zero Executor-authored commits.

### Scenario 2: Orchestrator crashes after commit, before merge enqueue

**Trigger:** `enqueueProposed` stages + commits inside the worktree, then the daemon process dies before pushing onto the FIFO queue. On restart, the phase branch has 1 commit, trunk doesn't, TaskRecord state is `merging`.

**Mitigation:**
- WA-6 extends `recoverFromCrash` to handle `state === "merging"` with a per-task scan: if the branch exists, has 1 commit ahead of trunk, and a worktree path that exists on disk → re-enqueue via `enqueueProposed` with `_alreadyCommitted=true` flag (skip stage step, run rebase+tests+merge as normal).
- If the orchestrator-commit was somehow malformed (e.g. partial stage), discard branch + worktree + transition to `pending` for retry.
- If the worktree path is undefined OR the path does not exist on disk, transition to `failed` with `lastError = "merging_recovery_worktree_missing"` and emit a `task_failed` event (see WA-6 code snippet for both guards).
- Property test in WA-6 spawns 100 simulated crash points across the new commit-then-merge boundary and asserts terminal state is one of {merged, failed, retried-and-merged} — never wedged.

### Scenario 3: Reviewer needs file content the orchestrator excludes from the commit

**Trigger:** Reviewer reads a file under `.harness/` (e.g. checks the completion.json contents for self-consistency) and bases its approve on it. Orchestrator's stage-step uses the existing `:!.harness` pathspec exclude — the file the Reviewer just read never reaches trunk.

**Mitigation:**
- Reviewer prompt (WA-3 in revised order) explicitly says `.harness/` is *signal*, not artifact, and Reviewer must not base its verdict on contents the orchestrator will not commit.
- A unit test in WA-3 asserts the Reviewer prompt contains the literal `MUST NOT base its verdict on .harness/` clause.
- Orchestrator-commit message (WA-4 in revised order) includes a stable suffix `(orchestrator-staged from worktree, .omc/.harness excluded)` so the audit trail makes the omission explicit.

---

## G. Implementation Waves (revised order)

**Wave-ordering rationale (from Critic feedback):** the Reviewer prompt (former WA-5) must ship BEFORE the orchestrator wiring switch (former WA-4). Reason: the orchestrator wiring change (`enqueueProposed`) makes the Reviewer the *only* gate between Executor-written-uncommitted-files and trunk. If the wiring lands before the Reviewer prompt knows how to read uncommitted state, every approve passes through a Reviewer reading an empty diff (the prompt still says "read the committed branch"). Reordering ensures every wave is green at its boundary AND every wave is functionally consistent at its boundary.

**Final wave order:** WA-1 (schema) → WA-2 (Executor prompts) → **WA-3 (Reviewer prompt — was WA-5)** → **WA-4 (MergeGate enqueueProposed — was WA-3)** → **WA-5 (orchestrator wiring — was WA-4)** → WA-6 (crash recovery) → WA-7 (live).

Waves are ordered so typecheck (`npm run lint`) + tests (`npm test`) + build (`npm run build`) stay green at each wave boundary.

### WA-1 — CompletionSignal schema: `commitSha` becomes optional

**Files touched:**
- `src/session/manager.ts` lines 21–32 (`CompletionSignal` interface), lines 56–89 (`validateCompletion` body, specifically line 61 `if (typeof obj.commitSha !== "string" || obj.commitSha.length === 0) return null;`)
- `src/lib/config.ts` line 304 (`DEFAULT_EXECUTOR_SYSTEM_PROMPT` JSON template — keep `commitSha` field but mark it as `optional, omit if you did not commit` for the WA-1 cycle; full removal from the template lands in WA-2)
- `tests/session/manager.test.ts` line 364 (`rejects completion.json with empty commitSha`) → reword to `accepts completion.json without commitSha` + `rejects empty-string commitSha`
- `tests/session/manager.test.ts` line 390 (`rejects completion.json with empty commitSha`) → keep (empty string still invalid; missing is now allowed)

**Methods added/removed:**
- Modify `validateCompletion` body so the commitSha branch becomes:
  ```
  if (obj.commitSha !== undefined) {
    if (typeof obj.commitSha !== "string" || obj.commitSha.length === 0) return null;
    signal.commitSha = obj.commitSha;
  }
  ```
- Change interface to `commitSha?: string;`.

**Tests added/changed:**
- `tests/session/manager.test.ts`: split the "empty commitSha" test into "missing commitSha (accepted)" + "empty-string commitSha (rejected)". +1 net test.
- All other manager.test.ts cases at lines 334, 379, 397, 415, 452, 477, 503, 537 already pass commitSha — no change required.

**Acceptance criteria (concrete commands):**
- `npm run lint 2>&1 | tail -3` → last line MUST be `> tsc --noEmit` (no stderr).
- `npm test 2>&1 | grep -E "Tests.*passed"` → match `Tests *[0-9]+ passed \([0-9]+\)` with count ≥ 544 (was 543; +1 from split test).
- `npm run build 2>&1 | tail -1` → `> tsc`.
- `validateCompletion({status:"success", summary:"x", filesChanged:[]})` returns a non-null `CompletionSignal` with `commitSha === undefined`.

**Blast radius:** type-only change visible to two consumers: (1) `src/gates/review.ts:291` does `completion.commitSha.replace(...)` unguarded — must update to `(completion.commitSha ?? "").replace(...)`; (2) `src/lib/response.ts` consumes the field (see `tests/lib/response.test.ts:9`) — no behavioral change, the helper handles undefined gracefully via the same `??` pattern. All 543 existing tests stay green because none constructs a CompletionSignal without `commitSha` today; the schema change is permissive. **Wave-boundary state:** WA-1 introduces no behavioral change to the live pipeline — purely permissive schema relaxation.

**Subtle dependency:** `src/gates/review.ts:291` does `completion.commitSha.replace(...)` unguarded. WA-1 must update that to `(completion.commitSha ?? "").replace(...)`.

---

### WA-2 — Executor prompts: drop the `git commit` instruction

**Files touched:**
- `src/lib/config.ts` lines 294–325 (`DEFAULT_EXECUTOR_SYSTEM_PROMPT`)
- `config/harness/executor-prompt.md` lines 1–30 (file-based system prompt; mirror the same change)
- `scripts/live-project.ts` lines 60–85 (script-local Executor prompt)
- `scripts/live-project-3phase.ts` lines 42–60 (same)
- `scripts/live-project-mass-phase.ts` lines 39–60 (same)
- `scripts/live-project-arbitration.ts` (verify; instructions follow the same template)
- `scripts/live-concurrent.ts` lines 32–45 (script-local prompt)
- `scripts/live-run.ts` lines 70–145 (multiple per-task prompts plus a vague-task control block)

**Prompt rewrite (canonical text for `DEFAULT_EXECUTOR_SYSTEM_PROMPT`):**
```
You are working inside a harness-managed git worktree.

When you finish your task, you MUST:
1. Write your code changes into the worktree. DO NOT run `git add`.
   DO NOT run `git commit`. The orchestrator will stage and commit your
   work after the Reviewer approves it.
2. Create directory `.harness/` if missing.
3. Write `.harness/completion.json` with this JSON shape (commitSha is
   no longer required — omit it):

{
  "status": "success" | "failure",
  "summary": "<one sentence — used as the orchestrator commit message>",
  "filesChanged": ["path1", "path2"],
  "understanding": "...",
  "assumptions": [...],
  "nonGoals": [...],
  "confidence": { ... }
}

The completion file is how the orchestrator knows you are done. If you do
not write it, the task will be marked failed. If you commit anyway, the
orchestrator's compat path will accept your commit, but the canonical
behavior is to leave the work uncommitted.
```

**Live-script prompt rewrite:** for each `live-project*.ts`, replace the numbered list "1. Commit your CODE CHANGES only…" with the WA-2 numbered list above (same content, script-templated).

**Methods added/removed:** none (string-only change).

**Tests added/changed:**
- `tests/lib/config.test.ts`: new test `DEFAULT_EXECUTOR_SYSTEM_PROMPT does NOT instruct `git commit``. Asserts `expect(prompt).not.toMatch(/git commit/i)` and `expect(prompt).toMatch(/orchestrator will stage and commit/i)`.
- Existing `live-project*.ts` end-to-end runs are NOT live-tested in WA-2; WA-7 covers that.

**Acceptance criteria (concrete commands):**
- `npm run lint 2>&1 | tail -3` → last line MUST be `> tsc --noEmit` (no stderr).
- `npm test 2>&1 | grep -E "Tests.*passed"` → match with count ≥ 545 (+1 from this wave's prompt-content test).
- `npm run build 2>&1 | tail -1` → `> tsc`.
- New regex test `DEFAULT_EXECUTOR_SYSTEM_PROMPT does NOT instruct git commit` is in the passing-test list.

**Blast radius:** prompt-only. The Executor still functions if the model accidentally commits — the WA-4 (was WA-3) compat path catches it. No schema or wiring changes. **Wave-boundary state:** orchestrator pipeline unchanged; new prompt language is dormant until WA-5 wires the new path. *If a live run is initiated between WA-2 and WA-3 land, Reviewer reads stale-prompted committed state and may emit false rejects; live runs are gated to WA-7 by plan rule.*

---

### WA-3 — Reviewer prompt update: read uncommitted diff (was WA-5)

**Wave reordering note:** This was WA-5 in revision 1. Moving it ahead of MergeGate `enqueueProposed` (now WA-4) and orchestrator wiring (now WA-5) ensures the Reviewer prompt is in place *before* any wiring change can route an uncommitted-diff request to it. At this wave's boundary, the orchestrator still calls the legacy `enqueue` path and Executor-prompt changes (WA-2) have not yet hit production — so the new Reviewer prompt reads committed state when present (existing behavior path) AND is now correctly configured to read uncommitted state when present (forward-compat for WA-5).

**Files touched:**
- `config/harness/review-prompt.md` (full rewrite of the "Ground truth" + add a new "Reading the proposal" section)
- `src/gates/review.ts` lines 260–297 (`buildPrompt` body — update the per-task message that frames what's under review)
- `src/gates/review.ts` constructor + `ReviewGateDeps` — add `getTrunkBranch: () => string` injection (no `"master"` default). Either (a) the orchestrator passes `() => mergeGate.getTrunkBranch()` into the ReviewGate constructor, or (b) the orchestrator passes the trunk-branch string directly. Choose (a): keeps the dep flow uniform with other gate deps.

**Reviewer prompt rewrite (canonical text for the new "Ground truth" + "Reading the proposal" sections):**

```
## Ground truth

You have **read-only** access to the worktree. Do not modify files. Do not
commit. Do not run the code. Form your judgment from the proposed diff,
the file contents, and the agent's completion signal.

## Reading the proposal

The agent has written files into the worktree but has NOT committed them.
Inspect the proposal as follows:

1. Run `git status --porcelain` to enumerate proposed changes.
2. Run `git diff` to see the uncommitted diff (mode: untracked + modified).
3. Run `git diff --cached` if anything was staged (the harness does not stage,
   but third-party paths might).
4. If `git status --porcelain` is empty AND `git log <branchName> ^<trunk>
   --oneline` is non-empty (legacy executor that committed despite the new
   prompt), inspect the committed diff via `git diff <trunk>...HEAD` and
   note the deviation in your summary as a `low` severity finding (`agent
   committed contrary to harness contract`). Do not reject solely on this.
5. If both are empty → return `reject` with finding "no diff to review".

You MUST NOT base your verdict on the contents of `.harness/` — it is a
per-worktree signal directory that the orchestrator will exclude from any
commit. Read `.harness/completion.json` only as supplementary metadata
about the agent's intent, never as part of the diff under review.
```

**`buildPrompt` rewrite:** the per-task framing message at `review.ts:273–296` updates:
- Replace `**Commit SHA:** ${completion.commitSha.replace(...)}` with `**Branch:** ${task.branchName} (uncommitted proposal — see system prompt step 1).`
- Add `**Trunk:** ${this.deps.getTrunkBranch()}` so the Reviewer can run `git diff trunk...HEAD` if the legacy fallback fires.
- The trunk branch is supplied via `deps.getTrunkBranch()` — there is NO `"master"` default. If `deps.getTrunkBranch` is undefined the constructor throws `Error("ReviewGate requires getTrunkBranch dep")`.

**Methods modified:**
- `ReviewGate` constructor adds required dep `getTrunkBranch: () => string`. The orchestrator wires it via `() => this.mergeGate.getTrunkBranch()` (where `MergeGate.getTrunkBranch(): string` is added in WA-4). **Fresh-1 — construction-order fix:** to remove the WA-3→WA-4 ordering dependency entirely, this wave introduces a single shared module-scope const `DEFAULT_TRUNK_BRANCH = "master"` in `src/lib/config.ts` (or `src/gates/merge.ts` if config.ts is the wrong locality). The orchestrator wires ReviewGate at WA-3 boundary via `() => DEFAULT_TRUNK_BRANCH`. This is NOT a temporary placeholder — it is the canonical default-trunk source. In WA-4, `MergeGate` reads the same const for its own trunk-branch default; in WA-5, the orchestrator switches the ReviewGate wire to `() => this.mergeGate.getTrunkBranch()` (which itself is backed by the same const when no explicit override is configured). **Construction order in `_startup()` is now order-agnostic** — both gates can be constructed in either order because neither requires the other to exist. Justification: avoids a cross-wave TODO comment, makes the trunk-branch convention discoverable at one source location, and means `git grep "master"` returns one canonical hit instead of scattered literals.
- `ReviewGate.runReview(task, worktreePath, completion)` signature unchanged.

**Tests added/changed:**
- `tests/gates/review.test.ts`:
  - +1 test: `buildPrompt includes branch name and trunk reference instead of commitSha`. Constructs ReviewGate with `getTrunkBranch: () => "main-test"` and asserts the rendered prompt contains `Trunk: main-test`.
  - +1 test: `buildPrompt does not crash when completion.commitSha is undefined` (covers WA-1's optional field).
  - +1 test: `ReviewGate constructor throws when getTrunkBranch dep missing`.
  - Existing test at line 147 (`commitSha: "abc123"`) keeps the field but the assertion shifts to "branch name appears in prompt" (already there) and "commit-SHA-formatting block is absent OR conditional".
- `tests/lib/config.test.ts`: +1 test — `review-prompt.md (if present) contains 'git status --porcelain'`.

**Acceptance criteria (concrete commands):**
- `npm run lint 2>&1 | tail -3` → last line MUST be `> tsc --noEmit`.
- `npm test 2>&1 | grep -E "Tests.*passed"` → count ≥ 549 (+4 from this wave: 3 review.test + 1 config.test).
- `npm run build 2>&1 | tail -1` → `> tsc`.
- `runReview` still terminates within `reviewer.timeout_ms` (180s default) — covered by existing timing test.
- Backward-compat: a Reviewer that follows the new prompt and reads `git diff` returns a verdict against the same bytes the orchestrator will commit.

**Blast radius:** prompt + a thin gate-builder change + a new constructor dep on ReviewGate. ReviewGate construction sites are: (a) `src/orchestrator.ts` startup, (b) `tests/gates/review.test.ts` (~10 sites), (c) `tests/integration/pipeline.test.ts`, (d) `tests/integration/discord-roundtrip.test.ts`, (e) `tests/integration/notifier-integration.test.ts`. All test sites add `getTrunkBranch: () => "test-trunk"` to their construction. Reviewer behavior is downstream of model interpretation; live verification (WA-7) confirms the rewrite produces the intended verdict pattern. **Wave-boundary state:** the wiring switch hasn't happened yet (WA-5), so the orchestrator still calls legacy `enqueue` and Reviewer reads committed state via the legacy fallback branch in the new prompt — fully consistent.

---

### WA-4 — MergeGate `enqueueProposed`: stage + commit, then existing pipeline (was WA-3)

**Files touched:**
- `src/gates/merge.ts` (full file — adds `enqueueProposed`, `MergeRequest.commitMessage?`, `MergeRequest.alreadyCommitted?`, `getTrunkBranch()`, modifies `processMerge` to branch on those, modifies `MergeGitOps` interface signatures for shell-safety)

**Methods added:**
- `enqueueProposed(taskId: string, worktreePath: string, branchName: string, commitMessage: string, opts?: { alreadyCommitted?: boolean }): Promise<MergeResult>`. Pushes a `MergeRequest` with `commitMessage` (and optional `alreadyCommitted` flag) set onto the queue.
- `enqueueProposed` is the new canonical entry point. The existing `enqueue(taskId, worktreePath, branchName)` becomes a thin wrapper that calls `enqueueProposed(..., commitMessage="harness: auto-commit agent work")` for compat.
- `MergeGate.getTrunkBranch(): string` — returns the trunk branch name configured at construction. Called by the orchestrator to wire `ReviewGate.getTrunkBranch` (replacing the temporary `() => "master"` from WA-3). At this wave's end, remove the `// TODO(WA-4)` comment and switch the wire to `mergeGate.getTrunkBranch.bind(mergeGate)`.
- **M3 — Lazy first-call precondition probe (Option A chosen).** Rather than a constructor probe (which would require updating 5 test-mock sites with a `getUserEmail: () => "test@example"` mock), the precondition lives inside the first invocation of `enqueueProposed`. Implementation: `MergeGate` carries a private `_userEmailProbed: boolean = false`. On every `enqueueProposed` entry, if `!this._userEmailProbed`, call `gitOps.getUserEmail(trunkCwd)`; if undefined or empty, throw `Error("MergeGate: 'git config user.email' must be set on the daemon host before orchestrator-staged commits can run. Set it with 'git config --global user.email <addr>'.")`. On non-empty return, set `_userEmailProbed = true` so subsequent calls skip the probe. Justification: keeps test-mock burden localized to the few tests that actually exercise `enqueueProposed` (those tests already pass `getUserEmail: () => "test@example"` per the WA-4 mock-update enumeration); construction sites stay minimal. Risk K-6 (commits-fail-because-no-user-email) is still downgraded MED → LOW because the failure is fast and actionable on first commit attempt rather than at startup. The probe is added to `MergeGitOps` as `getUserEmail(cwd: string): string | undefined` and falls back to `git config --get user.email`.

**Methods modified:**
- `processMerge(req)` step 1 (`Auto-commit uncommitted changes (O7)`):
  - If `req.alreadyCommitted === true` → skip stage entirely (recovery path; assumes `branchHasCommitsAheadOfTrunk` is already true).
  - Else if both `gitOps.branchHasCommitsAheadOfTrunk(worktreePath, trunk)` is true AND `gitOps.hasUncommittedChanges(worktreePath)` is true → **sub-case (a): legacy executor committed AND has additional uncommitted changes**. Choice: log `legacy_executor_commit` warning, then `gitOps.autoCommit(worktreePath, req.commitMessage, { amend: true })` so the additional changes are folded into the existing commit (no double-commit, no orphan tree). If amend fails for any reason, return `{status:"error", error:"legacy_commit_amend_failed: <stderr>"}`. Justification: amend preserves the legacy author trail while ensuring the orchestrator's stage step does not produce two commits. Rejecting outright would surprise existing operator scripts.
  - Else if `gitOps.branchHasCommitsAheadOfTrunk(worktreePath, trunk)` is true AND `gitOps.hasUncommittedChanges(worktreePath)` is false → **sub-case (b): legacy executor committed cleanly**. First, scrub `.harness/` if present in any committed file: `gitOps.scrubHarnessFromHead(worktreePath)` runs `git ls-files .harness/` on HEAD; if non-empty, runs `git rm --cached -r .harness/ && git commit --amend --no-edit` on the phase branch. Then log `legacy_executor_commit` warning + skip stage step + proceed to rebase+test+merge. Justification: amending HEAD is safe because the branch has not yet been pushed/merged anywhere; `git filter-branch` would rewrite multiple commits and is overkill for a 1-commit branch.
  - Else if `gitOps.hasUncommittedChanges(worktreePath)` is true → call `gitOps.autoCommit(worktreePath, req.commitMessage)` (canonical path).
  - Else (branch empty AND worktree clean) → **sub-case (c): empty proposal**. Detector additionally runs `gitOps.diffNameOnly(worktreePath, trunk)` (which executes `git diff ${trunk}...HEAD --name-only | wc -l`); if zero, returns `{status:"error", error:"empty_executor_commit"}`. This catches `git commit --allow-empty` and zero-tree-delta commits that pass `branchHasCommitsAheadOfTrunk` but produce nothing on trunk.

**Git ops added:**
- `MergeGitOps.branchHasCommitsAheadOfTrunk(cwd: string, trunk: string): boolean` — runs `git rev-list --count ${trunk}..HEAD` and returns >0.
- `MergeGitOps.diffNameOnly(cwd: string, trunk: string): string[]` — runs `git diff ${trunk}...HEAD --name-only` and returns the list. Empty list = empty proposal.
- `MergeGitOps.scrubHarnessFromHead(cwd: string): boolean` — runs `git ls-files HEAD -- .harness/` (idempotent check); if non-empty, runs `git rm --cached -r .harness/` then `git commit --amend --no-edit`. Returns true if scrub happened, false if `.harness/` was clean.
- `MergeGitOps.getUserEmail(cwd: string): string | undefined` — runs `git config --get user.email`. Used in `MergeGate` constructor for startup precondition check.

**Shell-safety: `autoCommit` signature change.** All `MergeGitOps.autoCommit` implementations switch to argv form. The interface signature changes from:
```
autoCommit(cwd: string): string;
```
to:
```
autoCommit(cwd: string, message: string, opts?: { amend?: boolean }): string;
```
Implementation MUST use `spawnSync("git", ["commit", "-m", message, ...(opts?.amend ? ["--amend", "--no-edit"] : [])], { cwd, ... })` (or argv-form `execFileSync`) — NOT `execSync` with shell concatenation. This neutralizes the prior risk that `completion.summary` contains shell metacharacters (backticks, `$()`, `;`) that would have been interpolated under the old shell-string commit form.

**All `MergeGitOps` implementor sites that need updating (verified by grep):**
- `src/gates/merge.ts:51` — `realMergeGitOps`. Update `autoCommit` to argv form; add `branchHasCommitsAheadOfTrunk`, `diffNameOnly`, `scrubHarnessFromHead`, `getUserEmail`.
- `tests/gates/merge.test.ts:22` — mock `autoCommit: vi.fn().mockReturnValue("abc123")`. Update signature: `autoCommit: vi.fn((cwd, msg, opts) => "abc123")`. Add mocks for the four new ops returning sensible defaults (false / [] / false / "test@example").
- `tests/orchestrator.test.ts:107` — `mockMergeGitOps()`. Same updates as above.
- `tests/integration/pipeline.test.ts:144` — `mergeGitOps` literal. Same updates.
- `tests/integration/notifier-integration.test.ts:160` — `mergeGitOps` literal. Same updates.
- `tests/integration/discord-roundtrip.test.ts:161` — `mergeGitOps` literal. Same updates.

**Audit command (must return 0 pre-commit on this wave):**
```
grep -rn "autoCommit:" tests/ src/ | grep -v "autoCommit: vi.fn((cwd, msg" | grep -v "autoCommit(cwd: string, message: string" | wc -l
```
Expected: 0 (every implementor matches the new argv-form signature).

**Commit author:** the orchestrator-commit uses the worktree's git config (whatever `user.email` / `user.name` is set on the host where the daemon runs). MergeGate's constructor verifies `user.email` is set at startup (precondition probe above).

**Tests added/changed:**
- `tests/gates/merge.test.ts`: +9 tests:
  1. `enqueueProposed stages, commits with the supplied message, then merges`.
  2. `enqueueProposed with empty worktree + empty branch returns status:error "empty proposal"`.
  3. `enqueueProposed with already-committed-but-clean branch (legacy compat sub-case b) skips stage step and merges (after .harness/ scrub if present)`.
  4. `enqueueProposed passes commitMessage through to autoCommit` — asserts argv `(cwd, message, undefined)`.
  5. `enqueue (legacy) wrapper still works and uses default commit message`.
  6. `enqueueProposed with alreadyCommitted=true skips both stage and detection and just merges`.
  7. **Sub-case (a):** `enqueueProposed with already-committed branch AND additional uncommitted changes amends the existing commit (does NOT create a second)`.
  8. **Sub-case (b) scrub path:** `enqueueProposed with legacy commit containing .harness/ files scrubs them via amend before rebase`.
  9. **Sub-case (c):** `enqueueProposed with --allow-empty commit (no tree delta vs trunk) returns empty_executor_commit error`.
  10. **Shell-safety:** `enqueueProposed with commit message containing shell metacharacters ($();\`backtick) commits the literal message without shell interpretation` (asserts the resulting `git log -1 --format=%s` matches the literal input).
  11. **Lazy first-call precondition (M3):** `enqueueProposed throws on first call when getUserEmail returns undefined; subsequent calls (after setting non-empty email) succeed without re-probing`.
- `tests/validation/merge-git.test.ts`: +4 tests:
  1. `branchHasCommitsAheadOfTrunk returns true for a branch with one commit ahead`.
  2. `branchHasCommitsAheadOfTrunk returns false for a branch even with trunk`.
  3. `diffNameOnly returns the list of files changed vs trunk`.
  4. `scrubHarnessFromHead removes .harness/ from HEAD via amend, returns true`.

**Test-mock migration audit (must pass before merge):**
```
grep -rn "commitSha:" tests/ | grep -v "commitSha: \"merge-" | grep -v "MergeResult" | wc -l
```
This counts CompletionSignal.commitSha occurrences in tests. Pre-WA-4: ~16 occurrences (acceptable — each is in a test that intentionally constructs a legacy completion). Post-WA-4 + WA-5: same count is acceptable (tests retain commitSha to exercise the optional path); the audit is a sanity-check that no NEW unintentional uses crept in. Pre-commit gate: number must not increase from baseline.

**Acceptance criteria (concrete commands):**
- `npm run lint 2>&1 | tail -3` → last line MUST be `> tsc --noEmit`.
- `npm test 2>&1 | grep -E "Tests.*passed"` → count ≥ 562 (+13 from this wave: 11 merge.test + 4 merge-git.test - 2 baseline tests rewritten in place).
- `npm run build 2>&1 | tail -1` → `> tsc`.
- All existing 22 merge.test.ts tests still pass (the new wrapper doesn't change behavior for the legacy entry point).

**Blast radius:** isolated to `gates/merge.ts` + its tests + the validation merge-git test + 5 mock sites. No orchestrator changes yet — the legacy `enqueue` continues to be the call site, so behavior is byte-identical at this wave boundary except for the new precondition check (which is benign in test environments where `git config user.email` is set, and required-correct in production). **Wave-boundary state:** orchestrator still calls legacy `enqueue`; new path lies dormant until WA-5 wires it in.

---

### WA-5 — Orchestrator wiring: switch call sites to `enqueueProposed` (was WA-4)

**Files touched:**
- `src/orchestrator.ts` lines 533–545 (`routeDirectMerge`), lines 547–582 (`routeReview`), ReviewGate construction site (~line 220 — to remove the WA-3 temporary `() => "master"` and replace with `() => this.mergeGate.getTrunkBranch()`)

**Methods modified:**
- `routeDirectMerge(task, completion)` — change line 537–541 from:
  ```
  const mergeResult = await this.mergeGate.enqueue(task.id, updatedTask.worktreePath!, updatedTask.branchName!);
  ```
  to:
  ```
  const commitMessage = this.formatCommitMessage(task, completion, sessionResult);
  const mergeResult = await this.mergeGate.enqueueProposed(
    task.id, updatedTask.worktreePath!, updatedTask.branchName!, commitMessage,
  );
  ```
- Same change at routeReview line 570–574 (the approve-and-merge path).
- **Decision M1 — `SessionResult.modelName` source (Option A chosen).** The SDK system-init message (`SDKSystemMessage` at `node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts:2798–2827`) carries a `model: string` field on `subtype: 'init'`. This is the *actual* resolved model (the SDK may diverge from a requested override under fallback/routing). Capture this for accurate commit provenance, rather than reading `task.modelOverride ?? config.defaultModel` which records the *requested* model only. **WA-5 work item (additional)**: extend `SessionResult` in `src/session/sdk.ts:38–47` with `modelName?: string`. Update `parseResult(msg: SDKResultMessage)` is insufficient on its own (the result message carries no model field) — instead, the SDK consumer (the message-loop in `src/session/manager.ts` runSession or equivalent) must capture the model from the system-init message at the start of the stream and thread it into the SessionResult constructed at the result message. Concrete change in `src/session/manager.ts`: in the message classification loop, when `classifyMessage(msg) === "system_init"` (msg is `SDKSystemMessage`), capture `(msg as SDKSystemMessage).model` into a local `let initModel: string | undefined`. When the result message arrives, after calling `parseResult(msg)`, set `result.modelName = initModel` before returning. Add a unit test `tests/session/sdk.test.ts`: `runSession captures model from system_init message into SessionResult.modelName`.
- Add `private formatCommitMessage(task: TaskRecord, completion: CompletionSignal, sessionResult: SessionResult): string` — deterministic per-phase message:
  ```typescript
  // Final message template (≤100 chars first line; multi-line trailers).
  const subject = `harness: ${task.id} — ${truncate(completion.summary, 72)}`;
  const trailers = [
    `Model: ${sessionResult.modelName ?? "unknown"}`,
    `Session: ${sessionResult.sessionId}`,
    `Phase: ${task.phaseId ?? "standalone"}`,
  ].join("\n");
  return `${subject}\n\n${trailers}`;
  ```
  where `truncate` is imported from `src/lib/text.ts` (`truncateRationale` exists at line 17; if its semantics differ, add `truncate(s, n)` as a 1-liner). The first line is bounded to ≤ 100 chars (subject = `harness: ${taskId} — ` ≈ 24 chars + truncated summary ≤ 72 chars + ellipsis ≤ 3 = ≤ 99 chars) so `git log --oneline` stays readable.
- Wire ReviewGate dep `getTrunkBranch` from `() => DEFAULT_TRUNK_BRANCH` (WA-3 default) to `() => this.mergeGate.getTrunkBranch()`. No TODO removal needed — Fresh-1 const-based design eliminated the cross-wave TODO. Construction order in `_startup()` is order-agnostic; either gate can be constructed first.

**Tests added/changed:**
- `tests/orchestrator.test.ts` — every `enqueue` mock-spy assertion needs to migrate to `enqueueProposed`. Mocks at lines 107 (`autoCommit`) and 117 (`enqueue` itself if mocked — confirm) update. Approximately 12 test sites across `orchestrator.test.ts` reference the merge mock. Each site adds the new `enqueueProposed` mock alongside `enqueue` (both should exist on `MergeGate` post-WA-4 so legacy mocks still resolve).
- New test: `routeDirectMerge passes formatted commit message to enqueueProposed with Model/Session/Phase trailers`. Asserts the mock saw `harness: {taskId} — {summary}\n\nModel: {modelName}\nSession: {sessionId}\nPhase: {phaseId}`.
- New test: `routeReview approve path passes formatted commit message to enqueueProposed`. Mirrors the above on the review path.
- New test: `formatCommitMessage subject line stays under 100 chars even with long summary`. Constructs a 200-char summary and asserts the first newline-bounded line is ≤ 100 chars.
- New test: `formatCommitMessage uses 'unknown' modelName when sessionResult.modelName is undefined`.
- Updates to ReviewGate construction in tests: now wired through MergeGate (the test scaffold creates a MergeGate first and passes `() => mergeGate.getTrunkBranch()` to ReviewGate).

**Acceptance criteria (concrete commands):**
- `npm run lint 2>&1 | tail -3` → last line MUST be `> tsc --noEmit`.
- `npm test 2>&1 | grep -E "Tests.*passed"` → count ≥ 567 (+5 from this wave: 4 new orchestrator-test cases + 1 new sdk.test M1 case).
- `npm run build 2>&1 | tail -1` → `> tsc`.
- All existing orchestrator.test.ts tests still pass with the test-double migration.
- The `formatCommitMessage` is unit-testable in isolation (separate test in `tests/orchestrator.test.ts` or, if extracted, `tests/lib/text.test.ts`).
- Reviewer's `runReview` continues to use `git diff` semantics (WA-3 wires the prompt) — orchestrator must not commit before calling Reviewer. WA-5 enforces this by removing the implicit dependency on Executor's commit: `routeReview` already calls `runReview` before `enqueue`, so the order is preserved; the new commit happens inside `enqueueProposed` which is on the merge path AFTER review approve.

**Wave-ordering re-verification (Critic point 1):** under the revised order, WA-3's Reviewer prompt has already shipped, so the moment WA-5 flips the call site to `enqueueProposed`, the Reviewer is reading uncommitted state via the new prompt — no inconsistent state. The previous WA-4 claim (in revision 1) that "tests are green at wave boundary" still holds: existing orchestrator tests pass because the mock supports both `enqueue` and `enqueueProposed`; legacy `enqueue` callers (none after this wave inside the orchestrator, but external tests still exercise it) continue to work via the wrapper installed in WA-4.

**Blast radius:** orchestrator.ts and its 1700-line test file. Mechanical mock-name migration; no logic change for non-project tasks. Three-tier Architect/Reviewer paths inherit the new behavior automatically (they already route through `routeReview` → `enqueue`). **Wave-boundary state:** the system is now fully on the propose-then-commit path; legacy compat is via WA-4's branchHasCommitsAheadOfTrunk detection only, which fires only when an old prompt sneaks through.

---

### WA-6 — Crash recovery for "branch has 1 orchestrator commit, not yet merged"

**Files touched:**
- `src/orchestrator.ts` lines 921–953 (`recoverFromCrash`)

**Methods added/modified:**
- **Fresh-2 — Recovery-depth bound.** `TaskRecord` (in `src/lib/state.ts` or wherever the state schema lives) gains a new field `recoveryAttempts?: number` (default 0 when absent). Add to the schema validator. The const `MAX_RECOVERY_ATTEMPTS = 3` lives in `src/lib/config.ts` alongside other tuning constants. Inside `recoverFromCrash`, *before* attempting the failed→pending→processTask transition (sub-case (a)) OR the alreadyCommitted re-enqueue (sub-case (b)), increment `recoveryAttempts`. If `recoveryAttempts >= MAX_RECOVERY_ATTEMPTS` (post-increment), transition to `failed` permanently with `lastError = "max_recovery_attempts_exceeded"` and emit `task_failed` with that reason — do NOT recurse into recovery again. This prevents the wedge case where `processTask` crashes mid-recovery, leaving the task in `pending` state, which would re-enter recovery on the next startup unboundedly.
- Extend `recoverFromCrash` with a new branch:
  ```typescript
  if (task.state === "merging") {
    const updated = this.state.getTask(task.id)!;
    // Guard 1: worktreePath must be defined.
    // Guard 2: worktreePath must exist on disk.
    if (!updated.worktreePath || !fs.existsSync(updated.worktreePath)) {
      this.state.setLastError(task.id, "merging_recovery_worktree_missing");
      this.state.transition(task.id, "failed");
      this.events.emit("task_failed", { taskId: task.id, reason: "merging_recovery_worktree_missing" });
      return;
    }
    // Guard 3 (Fresh-2): bound the recovery depth.
    const attempts = (updated.recoveryAttempts ?? 0) + 1;
    this.state.setRecoveryAttempts(task.id, attempts);
    if (attempts >= MAX_RECOVERY_ATTEMPTS) {
      this.state.setLastError(task.id, "max_recovery_attempts_exceeded");
      this.state.transition(task.id, "failed");
      this.events.emit("task_failed", { taskId: task.id, reason: "max_recovery_attempts_exceeded" });
      return;
    }
    // Two crash points possible past the guards:
    //   (a) before stage     → branch is empty + worktree dirty   → re-enqueue from start
    //   (b) after stage      → branch has 1 commit + worktree clean → re-enqueue with alreadyCommitted=true
    //   (c) after merge done → state would already be "done"; not reachable here
    if (this.mergeGate.branchHasCommitsAheadOfTrunk(updated.worktreePath, this.mergeGate.getTrunkBranch())) {
      // (b)
      this.mergeGate.enqueueProposed(
        task.id, updated.worktreePath, updated.branchName!, "harness: orchestrator-recovered",
        { alreadyCommitted: true },
      ).then((r) => this.handleMergeResult(task.id, r));
    } else {
      // (a) — discard worktree, transition to pending for retry
      this.sessions.cleanupWorktree(task.id);
      this.state.transition(task.id, "failed");
      this.state.transition(task.id, "pending");
      this.processTask(this.state.getTask(task.id)!);
    }
  }
  ```
- `MergeGate.branchHasCommitsAheadOfTrunk(worktreePath, trunk?)` is a thin public wrapper around `gitOps.branchHasCommitsAheadOfTrunk` (added in WA-4). Add it as a public method on `MergeGate`. If `trunk` is omitted, defaults to `this.getTrunkBranch()`.
- `src/lib/state.ts` KNOWN_KEYS: add string `"recoveryAttempts"` to the allowlist set at `src/lib/state.ts:96` so the field persists across atomic write/load. Without this, the recovery bound silently resets to 0 on every daemon restart.
- `StateManager.setRecoveryAttempts(taskId: string, n: number): void` — new method on StateManager. Persists via the standard atomic-write path. Used by `recoverFromCrash` Guard 3 (line 495).

**Tests added/changed:**
- `tests/orchestrator.test.ts`:
  - +1 test: `recoverFromCrash re-enqueues "merging" task with branch-1-commit as alreadyCommitted=true`.
  - +1 test: `recoverFromCrash re-runs "merging" task with empty branch from scratch`.
  - +1 test: `recoverFromCrash transitions "merging" task to failed when worktreePath is undefined`. Asserts `lastError === "merging_recovery_worktree_missing"` and `task_failed` event emitted.
  - +1 test: `recoverFromCrash transitions "merging" task to failed when worktreePath does not exist on disk`. Asserts same as above.
  - +1 test (**Fresh-2**): `recoverFromCrash bounds recovery to MAX_RECOVERY_ATTEMPTS (3)`. Pre-seeds `recoveryAttempts = 2`, calls recoverFromCrash, asserts the third invocation transitions to `failed` with `lastError === "max_recovery_attempts_exceeded"` and emits `task_failed` with that reason; does NOT call `processTask` or `enqueueProposed` again.
  - +1 test (**Fresh-2**): `recoverFromCrash increments recoveryAttempts on each call`. Pre-seeds `recoveryAttempts = 0`; after first recovery call asserts `getTask(id).recoveryAttempts === 1`. After second asserts `=== 2`. After third (which should hit the bound) asserts `=== 3` AND state is `failed`.
  - +1 test (**Iteration 4 — persistence**): `recoveryAttempts persists across save/load`. Create task with `recoveryAttempts = 2`; construct fresh StateManager pointing at same state file; assert `getTask(id).recoveryAttempts === 2`. Guards against KNOWN_KEYS regression silently dropping the field on atomic write.
- `tests/gates/merge.test.ts`:
  - +1 test: `enqueueProposed with alreadyCommitted: true skips stage and runs rebase+test+merge` (already in WA-4 case 6 — confirm coverage; if duplicate, drop here).

**Acceptance criteria (concrete commands):**
- `npm run lint 2>&1 | tail -3` → last line MUST be `> tsc --noEmit`.
- `npm test 2>&1 | grep -E "Tests.*passed"` → count ≥ 573 (+7 from this wave's 7 new recovery cases — 4 baseline + 2 Fresh-2 depth-bound + 1 Iteration 4 persistence).
- `npm run build 2>&1 | tail -1` → `> tsc`.
- A manual fault-injection script (optional — `scripts/fault-injection-merging.ts`, not built in this wave) can be added later to actually crash the daemon mid-commit; for WA-6 the unit-test fault model is sufficient.

**Blast radius:** orchestrator + merge-gate. `recoverFromCrash` is called once at startup; the new branch is exclusive on `state === "merging"` so existing recovery paths (active, reviewing, review_arbitration, shelved, failed) are untouched. **Wave-boundary state:** all unit + integration tests green; WA-7 live verification remains.

---

### WA-7 — Live verification

**Scripts re-run:**
- `npm run script -- scripts/live-project-3phase.ts` (3-phase live run; re-confirms basic propose-then-commit flow). Budget: $1.5.
- `npm run script -- scripts/live-project-mass-phase.ts` (7-phase live run; re-confirms `.harness/` no-conflict + per-phase commit author = orchestrator). Budget: $3.

**Acceptance criteria (concrete commands):**
1. Both scripts complete with their existing pass criteria (`mass-phase.ts:264` checks ≥7 merge commits on trunk).
2. `git log --pretty=format:%an trunk | sort -u` outputs only the orchestrator's git config user (no `Claude` or model-named authors). Command line: `git log --pretty=format:%an HEAD | sort -u | wc -l` → 1 (single author).
3. `git log --all --diff-filter=A -- ".harness/*"` returns zero results on trunk (the `.harness/` directory is never committed by the orchestrator and was never committed by the Executor).
4. Per-phase commit message regex matches `^harness: phase-\d+ — .+` for every per-phase commit; trailers (`Model: …`, `Session: …`, `Phase: …`) appear on lines 3-5.
5. Total budget under $5 across both runs (sum of `pipeline.actual_cost_usd` from each script's exit log).

**Tests added/changed:**
- Update each live script's "post-run validation" block to encode the new assertions (same pattern as `live-project-mass-phase.ts:239–264`).

**Blast radius:** SDK + Anthropic API spend. Failure to meet criterion 2 would mean the WA-2 prompt rewrite did not stop the Executor from committing — quick fix in WA-2 follow-up. Criterion 3 is the headline regression check. **Wave-boundary state:** plan complete; all acceptance criteria met.

---

## H. Wave summary table (revised order)

| Wave | Title | Primary file(s) | Tests Δ | Live | Wave-end safety | Wave-ordering rationale |
|---|---|---|---|---|---|---|
| WA-1 | CompletionSignal optional commitSha | `src/session/manager.ts`, `src/lib/config.ts` | +1 | no | lint+test+build green; behavioral no-op | Schema relaxation must precede prompt rewrite so legacy consumers don't break mid-rollout. |
| WA-2 | Drop `git commit` from Executor prompts | `src/lib/config.ts` + 5 scripts + `executor-prompt.md` | +1 | no | lint+test+build green; new prompt dormant | Prompt changes are inert until orchestrator wiring (WA-5) — safe to land early. |
| WA-3 | Reviewer prompt rewrite + dep injection | `config/harness/review-prompt.md`, `src/gates/review.ts` | +4 | no | lint+test+build green; Reviewer reads either committed-or-uncommitted | **MOVED FROM WA-5.** Must precede orchestrator wiring (WA-5) so when wiring flips, Reviewer is already trained on uncommitted state. |
| WA-4 | MergeGate `enqueueProposed` + git-ops + shell-safety + precondition | `src/gates/merge.ts` + 5 mock sites | +13 | no | lint+test+build green; legacy `enqueue` still default call site | **MOVED FROM WA-3.** Must precede orchestrator wiring (WA-5) and follow Reviewer prompt (WA-3) so wiring switch finds a ready Reviewer + ready MergeGate. |
| WA-5 | Orchestrator wiring → `enqueueProposed`, formatCommitMessage with trailers, SessionResult.modelName (M1) | `src/orchestrator.ts`, `src/session/sdk.ts`, `src/session/manager.ts` | +5 (+12 mock migrations) | no | lint+test+build green; pipeline now fully on propose-then-commit | **MOVED FROM WA-4.** This is the wiring switch; safe only after WA-3+WA-4 ship. |
| WA-6 | Crash recovery for `state === "merging"` (incl. worktree-missing guard + Fresh-2 depth bound + Iteration 4 persistence) | `src/orchestrator.ts`, `src/gates/merge.ts`, `src/lib/state.ts`, `src/lib/config.ts` | +7 | no | lint+test+build green; recovery covers all 3 sub-cases AND bounds attempts at MAX_RECOVERY_ATTEMPTS AND persists across restart | After wiring switches, recovery must handle the new in-flight states. |
| WA-7 | Live verification | scripts, no src changes | (live) | yes | live runs <$5; trunk audit clean | Final empirical proof point. |

**Cumulative test delta:** +32 net tests (was +28; +1 from M1 sdk.test, +2 from Fresh-2 depth-bound tests, +1 from Iteration 4 persistence test); ~+12 mechanical mock renames (no count change). Final test count target: 543 + 32 = 575. Acceptance criterion in section L is ≥574 to allow ±1 for any mock-rename-as-test-rename slippage.

---

## I. Test plan (DELIBERATE)

### I.1 Unit tests (per wave, in-process)

- **WA-1:** `tests/session/manager.test.ts` — split commit-SHA test. Note: `tests/lib/response.test.ts:9` constructs `commitSha: "abc123"` — this test is for `evaluateResponseLevel` (not session validation) and the field is independent of the new schema; no update required (documented for completeness).
- **WA-2:** `tests/lib/config.test.ts` — assert default prompt does NOT instruct commit.
- **WA-3:** `tests/gates/review.test.ts` — buildPrompt-no-commitSha test, branch/trunk framing test, getTrunkBranch dep-throws test; `tests/lib/config.test.ts` — review-prompt content test.
- **WA-4:** `tests/gates/merge.test.ts` — 11 enqueueProposed cases (including 3 sub-cases, shell-safety, precondition); `tests/validation/merge-git.test.ts` — 4 git-op cases.
- **WA-5:** `tests/orchestrator.test.ts` — 4 commit-message-formatting cases + ~12 mock-name migrations.
- **WA-6:** `tests/orchestrator.test.ts` — recoverFromCrash merging-state branch tests (×4: branch-with-commit, empty-branch, worktreePath-undefined, worktreePath-not-on-disk).

**MergeResult.commitSha — explicit non-update list (Critic point 8).** The following tests reference `commitSha` on a `MergeResult` value (the trunk merge SHA produced by `mergeNoFf`), NOT on a `CompletionSignal`. They DO NOT need updating because `MergeResult.commitSha` remains required across all waves:
- `tests/discord/notifier.test.ts:85` — `result: { status: "merged", commitSha: "abc123" }`.
- `tests/integration/discord-roundtrip.test.ts:145` — `commitSha: "sha-rt"` inside a MergeResult-shaped object.
- `tests/integration/pipeline.test.ts:123` — `commitSha: "integ-sha-…"` inside a MergeResult.
- `tests/validation/merge-pipeline.test.ts:95-97` — `expect(result.commitSha).toBeTruthy()` on a `MergeResult` returned from `gate.enqueue(...)`.

### I.2 Integration tests (cross-module, no real Discord/SDK)

- `tests/integration/pipeline.test.ts` (currently 18 it() cases) — extend two cases to verify `enqueueProposed` is called with a deterministic commit message containing the trailer block; the other 16 use mocked merge gate so the change is mock-name only. Mocks at line 144 update `autoCommit` to argv form and add the four new git ops.
- `tests/integration/notifier-integration.test.ts` (mock at line 142 references commitSha) — keep the field set (legacy event payload still includes it for `task_done` events); no behavior change. Update merge-gate mock at line 160 to argv-form `autoCommit`.
- `tests/integration/discord-roundtrip.test.ts` — same merge-gate-mock update at line 161.

### I.3 E2E live (mandatory; budget bounded)

- `scripts/live-project-3phase.ts` re-run, 3 phases, propose-then-commit verified. ≤ $1.5.
- `scripts/live-project-mass-phase.ts` re-run, 7 phases, headline test. ≤ $3.

Pass conditions:
- All existing per-script pass criteria.
- Trunk has zero `.harness/` adds (`git log --diff-filter=A -- '.harness/*'`).
- All per-phase commits are orchestrator-authored (criterion via author regex).
- Per-phase commit message has `Model:`, `Session:`, `Phase:` trailers.

### I.4 Observability tests

- New event-emission test: `enqueueProposed` failure with empty proposal emits `merge_result` event with `status === "error"` and `error === "empty_executor_commit"`. (No new event variant; existing `merge_result` channel suffices.)
- **Structured-log assertion for `legacy_executor_commit` warning (Critic point 12):** in `tests/gates/merge.test.ts`, every test that exercises the legacy compat path (sub-cases a + b — tests 3, 7, 8) installs a `vi.spyOn(console, "warn")` and asserts: `expect(console.warn).toHaveBeenCalledWith(expect.stringMatching(/legacy.*executor.*commit/i))` exactly once per affected phase. This guards against silent regression of the warning text or its emission count.
- New event-emission test: `recoverFromCrash` for `merging_recovery_worktree_missing` emits `task_failed` event with `reason: "merging_recovery_worktree_missing"`.

### I.5 Property tests

- WA-6: 100-iteration crash-point test — pick a random tick during `routeReview → enqueueProposed` execution, simulate process exit (clear queue, kill pollTimer, snapshot state), then call `recoverFromCrash` and assert terminal state is one of {merged, retried-and-merged, failed-with-recoverable-state, failed-with-merging_recovery_worktree_missing}.

---

## J. Migration / backward-compatibility

### Two `commitSha` fields — explicit distinction (Critic point 7)

- **`CompletionSignal.commitSha`** (optional after WA-1): historically the Executor's commit sha when Executor committed inside the worktree. Now typically *absent* (the canonical Executor prompt does not commit). When present, it is *informational only* — the orchestrator does not act on it; the merge gate computes its own commit metadata. **Decision:** keep the field name `commitSha` (do NOT rename to `executorCommitSha`) to minimize churn across ~16 existing tests and the validation surface. Add a JSDoc comment on the field in `src/session/manager.ts`:
  ```typescript
  /**
   * Optional informational sha when a legacy Executor committed its own work.
   * The canonical Executor prompt does NOT commit; this field is preserved
   * for legacy compat and external operator scripts only. The orchestrator
   * does not use this value for merge decisions — see MergeResult.commitSha
   * for the trunk merge sha.
   */
  commitSha?: string;
  ```
  Justification: renaming would touch every existing test and prompt template at the cost of saving documentation. The doc comment plus this section J entry is sufficient for future code archaeologists.

- **`MergeResult.commitSha`** (required, all waves): the trunk merge commit sha produced by `mergeNoFf`. This is what callers use to identify the merged commit on trunk. Type: `{ status: "merged"; commitSha: string }` at `src/gates/merge.ts:13`. Unchanged across all waves.

### Legacy Executor handling

If a running Executor (typically an older script-supplied prompt or a third-party operator prompt) still runs `git commit`:

1. Branch will have 1 commit ahead of trunk before `processMerge` runs.
2. Three sub-cases are handled (see WA-4):
   - **(a)** Legacy commit + additional uncommitted changes → log `legacy_executor_commit`, amend the existing commit.
   - **(b)** Legacy commit, worktree clean → log `legacy_executor_commit`, scrub `.harness/` if present (amend), proceed with rebase+test+merge.
   - **(c)** Legacy commit with empty tree delta → return `empty_executor_commit` error.
3. Result: legacy commit lands on trunk under the legacy author, but the pipeline does not break and the audit log captures the deviation.

### Deprecation timeline

- WA-4 emits the `legacy_executor_commit` warning unconditionally on the legacy compat path.
- After two release cycles (or sooner if no live runs trigger the warning over a 30-day window) the legacy path can be hardened to reject (return `status:error`). Out of scope for this plan; tracked as a follow-up in the ADR.

### CompletionSignal field migration

- `commitSha` becomes optional in the validator (WA-1) and removed from the canonical prompt (WA-2). Existing tests asserting non-empty commitSha continue to pass for the legacy path. New default-prompt-driven tests do not require the field.

### Discord notifier compat

- `tests/discord/notifier.test.ts:85` and `tests/integration/discord-roundtrip.test.ts:145` still construct merge results with `commitSha` (the MergeResult-shaped one). The MergeResult type is unchanged (line 13: `{ status: "merged"; commitSha: string }`). The `commitSha` is the *trunk merge SHA* — set by the orchestrator after `mergeNoFf` — not the Executor's commit. No change needed.

---

## K. Risks identified (with severity and concrete mitigations)

| # | Risk | Severity | Concrete mitigation (code-level) |
|---|---|---|---|
| K-1 | Reviewer reads wrong diff because legacy Executor committed | HIGH | WA-3 prompt explicitly handles both uncommitted + already-committed cases; WA-4 logs `legacy_executor_commit` warning on the orchestrator side too; structured-log assertion in I.4 guards the warning. |
| K-2 | Orchestrator crashes mid-commit (between stage and merge enqueue) | MED | WA-6 adds `recoverFromCrash` branch for `state === "merging"`; bounded to two sub-cases (empty-branch vs commit-on-branch) plus a worktree-missing guard, all terminating safely. |
| K-3 | Reviewer bases verdict on `.harness/` content that won't be committed | MED | WA-3 prompt explicitly forbids; assertion test in WA-3; orchestrator commit message annotates the exclusion. |
| K-4 | Test-mock migration (~5 sites for autoCommit, ~12 for enqueue mocks) misses one and ships a hung test | LOW | **Concrete audit command (must return 0 pre-commit on WA-4):** `grep -rn "autoCommit:" tests/ src/ \| grep -v "autoCommit: vi.fn((cwd, msg" \| grep -v "autoCommit(cwd: string, message: string" \| wc -l`. Plus per-wave acceptance criterion runs full `npm test` with the concrete count threshold. |
| K-5 | Live-mass-phase budget overrun on WA-7 | LOW | Hard cap `pipeline.max_budget_usd = 3` per project in the script; abort at threshold per existing budget guard. |
| K-6 | `git config user.email` not set on host where daemon runs → orchestrator commits fail | **LOW** (was MED) | **Concrete fix (M3):** WA-4 adds a lazy first-call precondition probe inside `enqueueProposed`. The probe calls `gitOps.getUserEmail(trunkCwd)` once on first invocation; throws a clear actionable error if undefined or empty; sets a `_userEmailProbed` flag so subsequent calls skip the probe. Severity downgraded MED → LOW because the failure is fast, deterministic on first commit attempt, and fixable in one line of operator config; lazy-probe choice keeps test-mock burden localized to the few tests that exercise `enqueueProposed`. |
| K-7 | Approve-then-commit-then-test_failed → trunk has a revert pair | LOW | Existing `revertLastMerge` handles this case (already exercised in 543 tests); orchestrator-staged commit is still on the per-phase branch which gets garbage-collected on cleanupWorktree (WA-5 already calls cleanupWorktree on the test_failed handleMergeResult path). |
| K-8 | Empty proposal (Executor wrote nothing, branch stayed empty) | LOW | WA-4 returns `status:"error"` with `error: "empty_executor_commit"` (sub-case c) including `--allow-empty` and zero-tree-delta detection; orchestrator's `handleMergeResult` already routes `error` to `failed`. |
| K-9 | `completion.summary` contains shell metacharacters, breaking the commit | RESOLVED | WA-4 switches `autoCommit` to argv-form `spawnSync("git", ["commit", "-m", message])`. No shell interpolation possible. |
| K-10 | Reviewer dep `getTrunkBranch` left as hardcoded `"master"` | RESOLVED | WA-3 ships with required dep injection (no default). WA-5 wires it through `MergeGate.getTrunkBranch()`. |

---

## L. Acceptance criteria (testable, plan-wide)

The plan is complete when ALL of the following hold:

1. `npm run lint 2>&1 | tail -3` last line is `> tsc --noEmit` (no stderr; clean type-check).
2. `npm run build 2>&1 | tail -1` is `> tsc` (TypeScript compiles).
3. `npm test 2>&1 | grep -E "Tests.*passed"` matches `Tests *[0-9]+ passed \([0-9]+\)` with count ≥574 (target 575), 0 failing, 0 skipped (excluding pre-existing skips).
4. `tests/lib/config.test.ts` includes the new `DEFAULT_EXECUTOR_SYSTEM_PROMPT does NOT instruct git commit` test and it passes.
5. `tests/gates/merge.test.ts` includes `enqueueProposed`-suite tests including all 3 compat sub-cases (a/b/c) and shell-safety; all pass.
6. `tests/orchestrator.test.ts` includes the recoverFromCrash-merging tests including worktree-missing guards; all pass.
7. One re-run of `scripts/live-project-mass-phase.ts` under propose-then-commit completes 7 phases:
   - All 7 trunk merge commits exist (per existing `mergeCommits >= 7` check).
   - All 7 per-phase commits are authored by the orchestrator's git config user (NEW assertion).
   - `git log --diff-filter=A -- '.harness/*'` on trunk returns zero results (NEW assertion).
   - Per-phase commit messages contain `Model: …`, `Session: …`, `Phase: …` trailers (NEW assertion).
   - Total run cost < $4.
8. A separate re-run of `scripts/live-project-3phase.ts` completes 3 phases under the same conditions; total cost < $1.5.
9. Combined live spend (criteria 7 + 8) under $5.
10. Reviewer prompt at `config/harness/review-prompt.md` contains the literal string `git status --porcelain`.
11. Audit command returns 0: `grep -rn "autoCommit:" tests/ src/ | grep -v "autoCommit: vi.fn((cwd, msg" | grep -v "autoCommit(cwd: string, message: string" | wc -l` → 0.

---

## M. Open questions for operator

1. **Commit author identity.** The orchestrator-staged commit will use the host's `git config user.email`/`user.name`. Acceptable for the staging environment? Or should the daemon enforce `harness@ozy-bot.local` via per-commit `-c user.email=...`? (Default plan: host-config; documented as startup precondition; WA-4 startup probe enforces presence.)
2. **Deprecation window for legacy commit path.** Plan emits a warning on the legacy compat path (WA-4). When (after how many release cycles) should this be hardened to reject? (Default plan: two release cycles, tracked as ADR follow-up.)
3. **Reviewer fallback on already-committed branch.** WA-3 instructs the Reviewer to inspect the committed diff via `git diff trunk...HEAD` and tag a `low` finding. Should this instead be `medium` to surface operator visibility? (Default plan: `low` — the work is still being reviewed, the protocol violation is procedural.)
4. **Per-phase commit subject format.** Plan uses `harness: ${taskId} — ${truncate(completion.summary, 72)}` with `Model:` / `Session:` / `Phase:` trailers. Operator prefers a different format? (Default plan: that exact template.)
5. **`CompletionSignal.commitSha` rename.** Plan keeps the field name (with a stronger doc-comment) instead of renaming to `executorCommitSha`. Operator wants the rename for clarity? (Default plan: keep — minimizes test churn.)

These do not block plan execution; defaults above are operationally safe.

---

## N. ADR

**Decision:** Adopt full propose-then-commit redesign (Option A). Executor writes files only; Reviewer reads uncommitted diff; orchestrator stages + commits on Reviewer approve and proceeds through the existing FIFO merge gate.

**Drivers:**
- Eliminate `.harness/` rebase-conflict class at root (mass-phase stress hotfix `8b7a1e4` was a band-aid).
- Trivialize reject cleanup (no committed state to tear down — branch has zero commits).
- Narrow Reviewer trust boundary (read file content, not committed-state metadata).

**Alternatives considered:**
- Option B (strip `.harness/` pre-merge in MergeGate): treats symptom only, leaves tainted-branch and trust-boundary problems unsolved. Stronger pros (lower test churn, forensic provenance, easier rollback) — but only addresses driver 1.
- Option C (Executor commits to staging branch, orchestrator cherry-picks on approve): doubles state complexity, reintroduces conflict surface via cherry-pick.

**Why chosen:** Option A is the only choice that scores on all three drivers. Test surface is large but mechanical (~28 net new tests, ~12 mock-name migrations). Live-script re-run is bounded to <$5.

**Consequences:**
- 7 sequenced waves (revised order: schema → executor prompts → reviewer prompt → mergegate → wiring → recovery → live) with test/lint/build green at each wave-end.
- Schema break: `CompletionSignal.commitSha` becomes optional (still set by legacy compat path; consumers must guard against undefined). Field name retained for compat; doc-comment clarifies role.
- Orchestrator assumes commit-authorship responsibility; deterministic per-phase message format with `Model:` / `Session:` / `Phase:` trailers.
- New crash-recovery edge case (state==="merging") added to `recoverFromCrash`, including worktree-missing guard.
- One default prompt + 5 script-local prompts rewritten to remove the commit instruction.
- Reviewer prompt rewritten to inspect `git status --porcelain` + `git diff` first; ReviewGate gains required `getTrunkBranch` dep injection.
- `MergeGitOps.autoCommit` switches to argv form (shell-safety); 5 mock implementors updated.
- MergeGate startup probe asserts `git config user.email` is set; fast-fails with actionable error otherwise.
- Operator burden: ensure `git config user.email/name` set on daemon host (now enforced at startup via WA-4 probe).

**Follow-ups:**
- After 2 release cycles, harden the legacy compat path in MergeGate to reject branches with non-orchestrator-authored commits.
- Optionally retire the `:!.harness` pathspec exclude in `autoCommit` once we confirm no Executor variant ever writes there.
- Track open question 3 as an operator-visible setting if it surfaces in live runs.
- Track open question 5: revisit `CompletionSignal.commitSha` rename after one release cycle — if the field remains universally absent on success completions, deprecate it via removal in a subsequent major.

---

## O. Final checklist

- [ ] WA-1 lands: CompletionSignal.commitSha optional, validator + 1 test updated, build/test/lint green.
- [ ] WA-2 lands: 1 default prompt + 5 script prompts rewritten, +1 test, build/test/lint green.
- [ ] WA-3 lands: review-prompt rewrite + buildPrompt update + getTrunkBranch dep injection + 4 tests, build/test/lint green.
- [ ] WA-4 lands: MergeGate.enqueueProposed + 11 merge-gate tests + 4 git-op tests + shell-safety argv migration across 5 mock sites + startup precondition probe, build/test/lint green; audit command returns 0.
- [ ] WA-5 lands: orchestrator routeDirectMerge + routeReview migrated to enqueueProposed, formatCommitMessage with Model/Session/Phase trailers, SessionResult.modelName captured from SDK system_init message (M1), +5 tests + ~12 mock renames, build/test/lint green.
- [ ] WA-6 lands: recoverFromCrash merging-state branch + worktree-missing guards + Fresh-2 recovery-depth bound (MAX_RECOVERY_ATTEMPTS=3) + Iteration 4 KNOWN_KEYS persistence + 7 tests, build/test/lint green.
- [ ] WA-7 lands: 2 live runs (3-phase + mass-phase) under <$5 total; trunk audit clean (zero .harness/ files, every per-phase commit orchestrator-authored, all commits carry Model/Session/Phase trailers).
- [ ] Final test count ≥ 574 (target 575), all passing.
- [ ] ADR follow-ups (deprecation hardening, pathspec retirement, commitSha-rename revisit) recorded in `.omc/plans/open-questions.md`.

---

## P. Iteration 2 revision log

This section enumerates each Architect/Critic revision item from iteration 2 and where it was incorporated. Listed in the order of the Critic enumeration.

1. **Reorder waves so Reviewer prompt ships before orchestrator wiring.** Done. New order: WA-1 → WA-2 → WA-3 (was WA-5: Reviewer prompt) → WA-4 (was WA-3: MergeGate) → WA-5 (was WA-4: orchestrator wiring) → WA-6 → WA-7. Updated section G headers, section H summary table (with explicit per-wave ordering rationale column), all internal cross-references in sections F/I/K/L/M/N/O. Re-verified the "tests green at wave boundary" claim under the new order in WA-5's "Wave-ordering re-verification" subsection.

2. **WA-3 trunk-branch injection (no `"master"` default).** Done. ReviewGate constructor now takes a required `getTrunkBranch: () => string` dep. The `"master"` literal lives only in a temporary 1-line orchestrator-construction TODO that WA-5 deletes (replaced by `() => mergeGate.getTrunkBranch()`). Constructor throws if dep is missing. See WA-3 "Methods modified" + the new constructor-throws test.

3. **WA-4 compat — three sub-cases enumerated with concrete handler text.** Done. WA-4 `processMerge(req)` step 1 now spells out:
   - Sub-case (a): committed + uncommitted → log + `autoCommit(..., { amend: true })` (no double-commit). Justification recorded.
   - Sub-case (b): committed clean → scrub `.harness/` via `gitOps.scrubHarnessFromHead` (amend) before rebase. Justification recorded.
   - Sub-case (c): empty proposal (--allow-empty / zero tree delta) → detector uses `gitOps.diffNameOnly`; returns `error: "empty_executor_commit"`.
   Added 3 new test cases (WA-4 tests 7, 8, 9).

4. **WA-4 shell-safety: switch `autoCommit` to argv form.** Done. Interface signature changed from `autoCommit(cwd: string): string` to `autoCommit(cwd: string, message: string, opts?: { amend?: boolean }): string`. Implementation MUST use `spawnSync("git", ["commit", "-m", message, ...])`. Enumerated implementor sites: `src/gates/merge.ts:51` (real), `tests/gates/merge.test.ts:22`, `tests/orchestrator.test.ts:107`, `tests/integration/pipeline.test.ts:144`, `tests/integration/notifier-integration.test.ts:160`, `tests/integration/discord-roundtrip.test.ts:161` — all 5 mock sites enumerated under WA-4 "All `MergeGitOps` implementor sites". Audit command provided. New shell-safety test added (WA-4 test 10).

5. **WA-5 `Model:` trailer + commit message template.** Done. `formatCommitMessage` now produces:
   ```
   harness: ${task.id} — ${truncate(completion.summary, 72)}

   Model: ${modelName}
   Session: ${sessionId}
   Phase: ${task.phaseId ?? 'standalone'}
   ```
   Subject ≤ 100 chars enforced; signature now takes `sessionResult: SessionResult` to access `modelName`. Removed contradictory "No co-author trailers" line from the original WA-3 (now WA-4). Added two new tests (subject-length-bound + missing-modelName fallback).

6. **WA-6 worktree-path-missing case.** Done. WA-6 `recoverFromCrash` snippet now enumerates two guards:
   - Guard 1: `!updated.worktreePath`.
   - Guard 2: `!fs.existsSync(updated.worktreePath)`.
   Both transition to `failed` with `lastError = "merging_recovery_worktree_missing"` and emit `task_failed`. Added 2 tests (one per guard).

7. **Section J — two `commitSha` fields, distinction documented.** Done. Section J opens with "Two `commitSha` fields — explicit distinction" subsection. Decision: keep field name `CompletionSignal.commitSha` (do NOT rename) and add a strong JSDoc comment in `src/session/manager.ts`. Justification recorded (test churn vs documentation savings). Open question 5 added so the operator can override.

8. **Section I.1 — explicit non-update list for MergeResult.commitSha tests.** Done. Section I.1 ends with a "MergeResult.commitSha — explicit non-update list" subsection enumerating: `tests/discord/notifier.test.ts:85`, `tests/integration/discord-roundtrip.test.ts:145`, `tests/integration/pipeline.test.ts:123`, `tests/validation/merge-pipeline.test.ts:95-97`. Each annotated with the rationale that the field references `MergeResult.commitSha` (the merge sha, not the CompletionSignal sha) and remains required. `tests/lib/response.test.ts:9` added to WA-1's bullet (no update required, field is independent of session validation).

9. **Section K — risk-mitigation concreteness.** Done. K-6 (`git config user.email` not set) downgraded MED → LOW; mitigation now points to a concrete `MergeGate` constructor probe via `gitOps.getUserEmail(trunkCwd)` that throws an actionable error at startup. K-4 (test-mock migration) replaced "vitest lists every spec" with the concrete grep-based audit command that must return 0 pre-commit. K-9 (shell-safety) added and marked RESOLVED. K-10 (trunk-branch hardcode) added and marked RESOLVED.

10. **Acceptance criteria per wave — concrete commands.** Done. Every wave's "Acceptance criteria" subsection now lists three concrete commands plus expected output pattern: `npm run lint 2>&1 | tail -3` (last line `> tsc --noEmit`), `npm test 2>&1 | grep -E "Tests.*passed"` (with explicit minimum count per wave), `npm run build 2>&1 | tail -1` (`> tsc`). Plan-wide acceptance in section L mirrors the pattern.

11. **Section D — Option B steelman rebalance.** Done. Removed the "we already paid for Option B as the hotfix" framing. Option B pros expanded to 5 (was 2): minimal LoC, minimal test churn, preserves git-native ergonomics for forensic provenance, lower live-SDK churn, easier rollback. Invalidation rationale rewritten on the merits (drivers 2 & 3 require committed-state-never-precedes-approval, which Option B structurally cannot provide; YAGNI-vs-long-term-architecture trade-off named explicitly).

12. **Observability — structured-log assertion for `legacy_executor_commit`.** Done. Section I.4 adds an explicit assertion: every test in `tests/gates/merge.test.ts` exercising the legacy compat path (sub-cases a + b — tests 3, 7, 8) installs `vi.spyOn(console, "warn")` and asserts `expect(console.warn).toHaveBeenCalledWith(expect.stringMatching(/legacy.*executor.*commit/i))` exactly once per affected phase.

**Sections preserved unchanged from revision 1:** A (Executive Summary), B (Principles), C (Decision Drivers), the C-options-A/C-pros-cons text in section D except where noted in revision items, F.scenarios (mitigations expanded only where ordering changed wave numbers), the WA-1, WA-2, WA-7 wave bodies (numbering unchanged but cross-references updated). All other text was either added or modified per the revision items above.

---

## P.3 Iteration 3 revision log — M1/M2/M3/F1/F2 incorporated

Iteration 3 — M1/M2/M3/F1/F2 incorporated.

1. **M1 — `SessionResult.modelName` source decision (Option A — extend SessionResult, capture from SDK system_init message).** Reasoning: SDK system-init message at `node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts:2811` carries `model: string` (the *actual* resolved model under any SDK fallback/routing), which is more accurate for commit provenance than `task.modelOverride ?? config.defaultModel` (the *requested* model). Implementation specified in WA-5 "Methods modified" subsection: extend `SessionResult` in `src/session/sdk.ts:38–47` with `modelName?: string`; capture from `(msg as SDKSystemMessage).model` in the message-loop in `src/session/manager.ts` when `classifyMessage(msg) === "system_init"`; thread into the result before returning. New unit test `tests/session/sdk.test.ts: runSession captures model from system_init message into SessionResult.modelName`. Cross-references at lines 442, 453, 456, 778, 782 are now valid (no stale references). **Lives at:** WA-5 Methods modified subsection (the "Decision M1" bullet immediately preceding the `formatCommitMessage` definition).

2. **M2 — WA-2→WA-3 micro-window invariant documented.** Added the verbatim sentence to WA-2 "Wave-boundary state" (in the Blast radius paragraph): *"If a live run is initiated between WA-2 and WA-3 land, Reviewer reads stale-prompted committed state and may emit false rejects; live runs are gated to WA-7 by plan rule."* **Lives at:** WA-2 Blast radius paragraph (final sentence).

3. **M3 — Lazy first-call precondition probe (Option A chosen).** Replaced the constructor probe with a lazy first-call probe inside `enqueueProposed`. `MergeGate` carries private `_userEmailProbed: boolean = false`; on first `enqueueProposed` call, calls `gitOps.getUserEmail(trunkCwd)`, throws if empty, sets the flag. Justification: avoids forcing all 5 test-mock sites (`tests/gates/merge.test.ts:22`, `tests/orchestrator.test.ts:107`, `tests/integration/pipeline.test.ts:144`, `tests/integration/notifier-integration.test.ts:160`, `tests/integration/discord-roundtrip.test.ts:161`) to add a `getUserEmail: () => "test@example"` mock at construction time; only the WA-4 tests that exercise `enqueueProposed` need the mock. Updated K-6 risk row to match. Updated WA-4 test 11 from "MergeGate constructor throws when getUserEmail returns undefined" to "enqueueProposed throws on first call when getUserEmail returns undefined; subsequent calls succeed without re-probing". **Lives at:** WA-4 "Methods added" Decision M3 bullet (replacing the prior constructor-probe bullet); WA-4 test 11; K-6 row in section K.

4. **Fresh-1 — WA-3/WA-4 construction-order independence via shared const.** Replaced the WA-3 temporary `() => "master"` orchestrator-construction TODO with a canonical module-scope const `DEFAULT_TRUNK_BRANCH = "master"` in `src/lib/config.ts`. WA-3 wires ReviewGate to `() => DEFAULT_TRUNK_BRANCH`; WA-4 reads the same const for MergeGate's default; WA-5 switches the ReviewGate wire to `() => this.mergeGate.getTrunkBranch()` (which is itself backed by the same const when no explicit override is configured). Construction order in `_startup()` is now order-agnostic. No cross-wave TODO remains. **Lives at:** WA-3 "Methods modified" Fresh-1 bullet (replacing the prior TODO-bullet); WA-5 "Methods modified" wiring bullet (referencing the const-based design).

5. **Fresh-2 — Recovery-depth bound via `recoveryAttempts` counter.** `TaskRecord` gains optional `recoveryAttempts?: number`; `MAX_RECOVERY_ATTEMPTS = 3` const lives in `src/lib/config.ts`. Inside `recoverFromCrash`, increment the counter and bail to `failed` with `lastError = "max_recovery_attempts_exceeded"` after the third attempt; emits `task_failed` with that reason. Prevents wedge case where mid-recovery `processTask` crash leaves a task in `pending` that re-enters recovery unboundedly. Added 2 new tests: depth-bound enforcement at attempt 3 and counter increment per call. WA-6 acceptance count updated from ≥570 (+4) to ≥572 (+6). **Lives at:** WA-6 "Methods added/modified" Fresh-2 bullet + extended `recoverFromCrash` snippet (Guard 3); WA-6 tests added (last 2 in `tests/orchestrator.test.ts`); H summary table WA-6 row; cumulative test delta line (now +31 net, target 574).

**Validation:**
- `grep modelName` post-fix: all references now refer to `SessionResult.modelName` defined in WA-5 Methods modified subsection (M1 decision bullet); no stale references remain.
- File line count after iteration 3 edits: see final tool report below.

Iteration 4 — KNOWN_KEYS + setRecoveryAttempts + persistence test added (lines 522, 523, 533).
