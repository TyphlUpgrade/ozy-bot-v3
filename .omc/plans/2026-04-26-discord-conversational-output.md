# Discord Conversational Output — harness-ts notifier rich-format pass

**Created:** 2026-04-26
**Status:** APPROVED via RALPLAN consensus (iter 3 — Architect APPROVE + Critic APPROVE)
**Owner:** harness-ts pipeline
**Touches:** `src/discord/notifier.ts`, `src/orchestrator.ts`, `src/lib/state.ts`,
            `tests/discord/notifier.test.ts`,
            `scripts/live-discord-smoke.ts`

## Problem

Outbound Discord messages from the harness pipeline are single-line, terse,
and truncated. Operator complaint (verbatim):
*"messages received were not informative, partially truncated, and not
conversational. The information was poor and it was hard to understand
what had even been done."*

Reference output today:
```
Task task-b0a...a5e picked up: build a small url parser in src/url that splits scheme/host/port/path/query into...
Session complete for task-b0a...a5e (success)
Response level 3 (dialogue) for task-b0a...a5e: 1 open question(s)
Merge result for task-b0a...a5e: merged
Task task-b0a...a5e complete
```

The data exists (CompletionSignal carries summary, filesChanged, confidence;
MergeResult.commitSha post-WA-1; ConfidenceAssessment.openQuestions) but the
notifier discards it. Wave C P1/P2 backlog flags "Discord integration live —
30+ notifier tests, 0 live deliveries."

## Goal

Multi-line, markdown-ish, operator-friendly Discord output. Surface every
field already on existing types; never invent metrics. Cover failure events
(operators benefit from rich context on failure ≥ on success). Single-channel
posts only (threading is locked out per `v5-conversational-discord-operator.md:287`).

## Non-goals

- New event types (only enrich existing variants with optional fields).
- Inbound dispatcher behavior changes.
- Threading / message grouping (Phase D, deferred — out of scope).
- LLM augmentation (Phase C, deferred at observed cadence ~6 task_done/day).
- Architect/Executor prompt changes.
- StateManager schema migrations beyond optional fields on existing types.

## RALPLAN-DR Summary

**Mode:** SHORT (UX/notifier work; not auth/migration/destructive).

### Principles
1. **Never invent metrics.** Every rendered token traces to an existing
   `type:field`. Enforced per-row by Phase B "Data source" column.
2. **Backward-compatible payloads.** Adding optional fields fine; renaming
   or removing requires call-site sweep.
3. **Test churn proportional to behavior change.** New assertions only where
   rendering changed. No drive-by rewrites.

### Decision Drivers (top 3)
1. **Operator legibility** of dev/ops/escalation channels under live load.
2. **Type safety** preserved (`NotifierEntry<K>` generic must keep narrowing).
3. **Single PR vs split** — minimize blast radius for revert.

### Options

| | A. Lean steelman (rejected) | B. Rich rendering (CHOSEN) | C. Embed-rewrite (rejected) |
|---|---|---|---|
| Scope | task_done + one merge_result.test_failed | All key events rich; failures included | Discord embeds (richer formatting) |
| LOC | ~80 | ~250 | ~350 |
| Cost/day | $0 | $0 | $0 |
| Failure coverage | Partial (test_failed only) | Full (rows 3,5,7,8,10,11,12) | Full |
| Reject reason | **Incrementalism cost.** Payload extensions and renderers are co-designed; splitting ships partial surface for no risk reduction. Two-commit split inside Option B already captures the risk-reduction benefit. | — | Cross-cuts `DiscordSender` interface (`sendToChannel(channel, body)` is string-only). Out of scope; defer. |

Two viable options retained (A, B); C invalidated on interface-signature grounds.

## Phase A — Payload extensions (LOW risk; deterministic; no LLM)

Every new field cites the real upstream `type:field`. Fields are optional
unless noted; existing tests using `events.some(e => e.type === ...)`
type-only asserts (orchestrator.test.ts:363, :1291, :1499) remain green.

### Phase A.1 — extension matrix

| Event | New field | Type | Data source | Notes |
|---|---|---|---|---|
| `session_complete` | `errors: string[]` | `string[]` | `SessionResult.errors` (sdk.ts:42) | Plumbed at orchestrator.ts:321 emit. Empty when `success=true`. |
| `session_complete` | `terminalReason?: string` | optional | `SessionResult.terminalReason` (sdk.ts:46) | Optional; absent on synthesized failures. |
| `task_failed` | `attempt: number` | `number` | `TaskRecord.retryCount` (state.ts:75) | At each emit site (orchestrator.ts:365, 438, 489, 633, 681, 687, 843, 864, 871, 878, 984, 1006, 1019), call `state.getTask(task.id)?.retryCount ?? 0`. Field name `attempt` operator-facing; JSDoc cites retryCount provenance. |
| `project_failed` | `failedPhase?: string` | optional | `TaskRecord.phaseId` (state.ts:89) | OPTIONAL. Cascade-induced failures (orchestrator.ts:843 path) populate from `task.phaseId`. Spawn/decompose-time failures (orchestrator.ts:906, :914) leave absent. JSDoc states the conditional. |
| ~~`merge_result.errorOutput`~~ | **DROPPED** | — | — | `MergeResult.error` already exists (merge.ts:14); stderr already folded at merge.ts:146. Phase B reads `mergeResult.error` directly. |
| `task_done` | `responseLevelName?: string` | optional | `TaskRecord.lastResponseLevelName` (NEW field) ← persisted at response_level emit (orchestrator.ts:520) from `ResponseLevel.name` | State.ts changes: (i) add `lastResponseLevelName?: string` to `TaskRecord` (state.ts:64-94); (ii) extend `KNOWN_KEYS` (state.ts:97); (iii) `state.updateTask({lastResponseLevelName: ev.name})` at orchestrator.ts:520. Temporal invariant: response_level fires inside session loop pre-merge; task_done fires post-merge — ordering holds. |
| `escalation_needed` | NO Phase A change | — | — | `EscalationSignal` already carries `type`, `question`, `context?`, `options?`, `assessment?` (escalation.ts:25-31). Sufficient for Phase B row 11. |

### Phase A acceptance criteria

- A1. `OrchestratorEvent` union compiles with new optional fields. `npm run typecheck` passes.
- A2. `session_complete` emit at orchestrator.ts:321 carries `errors` and (when applicable) `terminalReason`.
- A3. `task_failed` emit at every cited site carries `attempt` populated via `state.getTask(taskId)?.retryCount ?? 0`.
- A4. `project_failed` emit at orchestrator.ts:843 carries `failedPhase: task.phaseId`; emits at :906, :914 omit it.
- A5. `task_done` emit at orchestrator.ts:829 carries `responseLevelName` from `state.getTask(taskId)?.lastResponseLevelName`.
- A6. `TaskRecord` gains optional `lastResponseLevelName`; `KNOWN_KEYS` extended; `state.updateTask({lastResponseLevelName})` invoked at response_level emit.
- A7. `orchestrator.test.ts` remains green unmodified (type-only `events.some(e => e.type === ...)` asserts).

### Phase A verification

- `npm run typecheck` (full repo).
- `npm test orchestrator.test.ts` — green unmodified.
- No live-discord smoke run needed for this phase (no notifier change yet).

### Phase A rollback

`git revert <phase-a-commit>` — drops optional fields from `OrchestratorEvent`
and `TaskRecord`. Emit sites stop populating. Notifier (still on prior
single-line templates without Phase B) loses no behavior because the new
fields are not yet read. Single-commit revert.

---

## Phase B — Rich deterministic rendering

Rewrite NOTIFIER_MAP formatters in `src/discord/notifier.ts`. Sanitize/redact
wrappers around every interpolated string; format strings shown without
sanitize wrappers for clarity. Discord 2000-char hard cap respected via
`truncateBody(body, 1900)`; rationale via `sanitizeRationale(s, 1024)`
(strips ANSI `\x1b\[[0-9;]*m` + control chars `[\x00-\x08\x0B\x0C\x0E-\x1F]`,
caps at 1KB) — closes Wave C P2 LOW item.

### Phase B.1 — Renderer table (15 rows; every rendered field cites type:field)

| # | Event | Channel | Identity | Template | Data source per field |
|---|---|---|---|---|---|
| 1 | `task_picked_up` | dev | orch | `` Task `{id}` picked up: {prompt} `` | id=`event.taskId`; prompt=`event.prompt`. **UNCHANGED.** |
| 2 | `session_complete` (success) | dev | orch | `` Session complete for `{id}`: success `` | UNCHANGED. |
| 3 | `session_complete` (failure) | dev | orch | `` Session complete for `{id}`: failure — {errSummary}{tr?} `` where `errSummary = errors.length>0 ? errors.join("; ").slice(0,200) : "(no error detail)"`; `tr = terminalReason ? \` [${terminalReason}]\` : ""` | errors=`event.errors` (←`SessionResult.errors`); terminalReason=`event.terminalReason?` (←`SessionResult.terminalReason`). |
| 4 | `merge_result` (merged) | dev | orch | `` Merge result for `{id}`: **merged** ({sha7}) `` | sha7=`event.result.commitSha.slice(0,7)` ←`MergeResult.commitSha` (merge.ts:13). |
| 5 | `merge_result` (test_failed) | dev | orch | `` Merge result for `{id}`: **test_failed** — {err} `` where `err=sanitize(event.result.error,200)` | `event.result.error` ←`MergeResult.error` (merge.ts:14). |
| 6 | `merge_result` (test_timeout) | dev | orch | `` Merge result for `{id}`: **test_timeout** `` | UNCHANGED. |
| 7 | `merge_result` (rebase_conflict) | dev | orch | `` Merge result for `{id}`: **rebase_conflict** — {n} files: {first3}{ellipsis} `` | n=`event.result.conflictFiles.length`; first3=`event.result.conflictFiles.slice(0,3).join(", ")` ←`MergeResult.conflictFiles` (merge.ts:16). |
| 8 | `merge_result` (error) | dev | orch | `` Merge result for `{id}`: **error** — {err} `` | `event.result.error` ←`MergeResult.error` (merge.ts:17). |
| 9 | `task_done` | dev | orch | `` Task `{id}` complete{lvl?} `` where `lvl = responseLevelName ? \` (response level: ${responseLevelName})\` : ""` | responseLevelName=`event.responseLevelName?` (←`TaskRecord.lastResponseLevelName`). |
| 10 | `task_failed` | ops | orch | `` Task `{id}` **FAILED** (attempt {n}): {reason} `` | n=`event.attempt` (←`TaskRecord.retryCount`); reason=`event.reason`. |
| 11 | `escalation_needed` | esc | orch | `` **ESCALATION** `{id}` ({type}): {q}{opts?}{ctx?} `` where `opts = options?.length ? "\nOptions: "+options.join(" \| ") : ""`; `ctx = context ? "\nContext: "+context.slice(0,300) : ""` | type=`event.escalation.type`; q=`event.escalation.question`; options?=`event.escalation.options` (escalation.ts:29); context?=`event.escalation.context` (escalation.ts:28). `assessment` deliberately NOT rendered (verbose; low signal at escalation surface). |
| 12 | `project_failed` | ops | arch | `` Project `{pid}` **FAILED**{phase?}: {reason} `` where `phase = failedPhase ? " at phase \`"+failedPhase+"\`" : ""` | pid=`event.projectId`; failedPhase?=`event.failedPhase` (←`TaskRecord.phaseId` for cascade-induced); reason=`event.reason` (passes through `sanitizeRationale(reason, 1024)`). |
| 13 | `project_completed` | dev | arch | `` Project `{pid}` completed ({n} phases, ${cost}) `` | UNCHANGED. cost=`event.totalCostUsd.toFixed(2)`. |
| 14 | `arbitration_verdict` | ops | arch | UNCHANGED (verdict + rationale already render); rationale wrapped in `sanitizeRationale(rationale, 1024)`. |
| 15 | `architect_arbitration_fired` | ops | arch | UNCHANGED (cause already renders). |

Rows 1, 2, 6, 13, 14, 15 = UNCHANGED. Rows 3, 5, 7, 8, 9, 10, 11, 12 carry rendering deltas.

### Phase B.2 — Live smoke fixture matrix (Phase B prerequisite)

`scripts/live-discord-smoke.ts` currently emits only `project_declared`
(verified line 108-112). Phase B prerequisite extends the script with one
fixture per row in B.1 table. Each fixture uses representative dummy values;
operator visually confirms in Discord before the merge of commit 2.

Required fixtures: task_picked_up; session_complete success; session_complete
failure with errors+terminalReason; task_done with and without responseLevelName;
merge_result merged; merge_result test_failed; merge_result test_timeout;
merge_result rebase_conflict; merge_result error; task_failed; escalation_needed
with options+context; arbitration_verdict; budget_ceiling_reached; project_failed
with failedPhase; project_failed without failedPhase (spawn-time).

The fixture matrix is documented in the script header.

### Phase B.3 — Test pin sweep (file:line enumerated; ZERO existing pins modified)

Strategy chosen: **(a) explicit file:line→old-pin→new-pin enumeration in plan body**.

| Test (line range) | Current assertion | Status under Phase B | Action |
|---|---|---|---|
| `task_picked_up → dev_channel` (notifier.test.ts:63–72) | `content` contains shortened id + prompt prefix | Row 1 unchanged | **KEEP** |
| `session_complete → dev (success+failure)` (:74–82) | regex `/success/`, `/failure/` | Rows 2, 3: success matches; failure substring `/failure/` still appears in row 3's body | **KEEP** (existing test fixture passes — no errors plumbed → renders `failure — (no error detail)` which still matches `/failure/`) |
| `merge_result → dev with status` (:84–93) | `/merged/` | Row 4 appends `(sha7)`; substring still matches | **KEEP** |
| `task_done → dev_channel` (:95–99) | channel only | Row 9 appends `(response level: ...)` only when `responseLevelName` present; absent in fixture → unchanged body | **KEEP** |
| `task_failed → ops_channel` (:101–107) | `/FAILED/`, `/boom/` | Row 10 inserts `(attempt N)`. Both regexes still match | **KEEP** |
| `escalation_needed → esc` (:109–119) | `/ESCALATION/`, `/what scope/` | Row 11 expands but base substrings persist (no options/context in fixture) | **KEEP** |
| `budget_exhausted → ops` (:121–126) | unchanged event | Out of scope | **KEEP** |
| `retry_scheduled → dev` (:128–133) | unchanged | Out of scope | **KEEP** |
| `response_level 2+` (:135–143) | unchanged event | Out of scope | **KEEP** |
| `task_shelved → dev` (:145–150) | unchanged | Out of scope | **KEEP** |
| `poll_tick / shutdown / checkpoint_detected / completion_compliance ignored` (:152–167) | unchanged | Out of scope | **KEEP** |
| `sender failure swallowed` (:169–177) | unchanged | Out of scope | **KEEP** |
| Wave 2 project events (:181–296) | various — all unchanged renderers in B.1 except `project_failed` | Row 12 adds `at phase` only when `failedPhase` set; absent from existing fixtures → substring `/budget ceiling/` (line 207) etc still match | **KEEP** all Wave 2 tests |
| Identity/sanitization/redact tests (:300–404) | unchanged | Out of scope | **KEEP** |
| CW-3 messageContext tests (:406–476) | unchanged | Out of scope | **KEEP** |
| `redactSecrets()` (:478–497) | unchanged | Out of scope | **KEEP** |

**Result of sweep: ZERO existing test pins require modification.** All Phase B
render deltas are purely additive on the message body, preserving every
existing substring/regex pin.

### Phase B.3 — NEW additive test cases (8 total; commit 1 adds as `it.skip`, commit 2 un-skips)

| New test | Asserts | Maps to row |
|---|---|---|
| `session_complete failure with errors and terminalReason` | content matches `/failure — boom1; boom2 \[budget_exceeded\]/` | Row 3 |
| `task_failed renders attempt N from TaskRecord.retryCount` | content matches `/attempt 2/` when state has `retryCount=2` (uses fake state) | Row 10 |
| `task_done renders response level name when present` | content matches `/response level: reviewed/` when state has `lastResponseLevelName="reviewed"` | Row 9 |
| `task_done omits response level when absent` | content does NOT contain `response level:` | Row 9 |
| `escalation_needed renders options + context` | content contains `Options:` and `Context:` when those fields set | Row 11 |
| `merge_result rebase_conflict shows file count + first3` | content matches `/3 files: a, b, c/` | Row 7 |
| `project_failed renders failedPhase when set` | content matches `/at phase `phase-1`/` | Row 12 |
| `project_failed without failedPhase (spawn-time)` | content does NOT contain `at phase` | Row 12 |

### Phase B.4 — Drop fence-escape sanitize change (Wave C P2 LOW item closure)

`src/discord/text.ts:44` already escapes EVERY backtick (`/\`/g, "\\\`"`),
so triple-backtick is already triply-escaped. The Wave C LOW "fence-escape
parity" item misdescribed the fix; close as no-op. No code change in this
phase for it.

### Phase B.5 — Two-commit mandate (explicit)

Phase B MUST land as exactly two commits on the feature branch. **No squashed
single commit.**

```
Commit 1 — "test(discord): pin existing notifier assertions ahead of rich rendering"
  Files: tests/discord/notifier.test.ts
  Diff: ZERO substantive changes to existing tests (per §Phase B.3 sweep).
        Adds the 8 NEW test cases listed above, each via `it.skip` /
        `describe.skip` initially. Verifies the sweep claim.

Commit 2 — "feat(discord): rich event rendering with payload extensions (Phase A+B)"
  Files: src/orchestrator.ts (payload extensions, emit-site updates),
         src/lib/state.ts (TaskRecord.lastResponseLevelName, KNOWN_KEYS),
         src/discord/notifier.ts (NOTIFIER_MAP rewrite for rows 3, 5, 7, 8, 9, 10, 11, 12),
         tests/discord/notifier.test.ts (un-skip the NEW tests from commit 1),
         scripts/live-discord-smoke.ts (Phase B.2 fixture matrix)
  Diff: All renderer + payload changes together so commit 2 alone is
        reviewable end-to-end.
```

Revert: `git revert <commit2>` retains the test scaffolding for follow-up;
reverting both rolls back fully.

### Phase B acceptance criteria

- B1. `npm test discord/notifier.test.ts` — 8 NEW tests pass post-commit-2; all existing tests unchanged.
- B2. `npm test` (full suite) green.
- B3. `npm run typecheck` green.
- B4. Body length never exceeds 1900 chars; rationale truncates with "…" via `sanitizeRationale(s, 1024)`.
- B5. All security pins survive: `@everyone`/`@here` neutralized; backticks escaped; `[REDACTED]` for sk-/ghp-/xoxb- patterns.
- B6. Live-discord smoke (`npx tsx scripts/live-discord-smoke.ts`) renders all 15 fixture rows; operator visually confirms before commit 2 merges.

### Phase B verification

- `npm test discord/notifier.test.ts` — 8 NEW + all KEEPs green.
- `npm test` — full suite (including `orchestrator.test.ts` from Phase A).
- `npm run typecheck`.
- `npx tsx scripts/live-discord-smoke.ts` — visual operator review of all 15 rows.

### Phase B rollback

Per §B.5: `git revert <commit2>` for full rollback (single command).
Optionally retain `git revert <commit1>` to remove the skipped test
scaffolding too. Phase A enrichment fields stay; they become unread but
harmless because all are optional.

---

## Phase C — DEFERRED

LLM-augmented addendum bodies on `task_done` deferred. At observed cadence
(~6 task_done/day; Wave C live runs cost ~$1.95 across 6 runs), $0.12/day
yields marginal scannability gain over Phase B markdown.

**Reactivation trigger (qualitative):** if task throughput rises noticeably
(operator review of `dev_channel` cadence — there is no aggregator script
in repo computing 7-day rolling task_done counts; the fabricated ≥210
threshold from earlier iterations is removed), revisit whether dev_channel
notifications need rate-limiting, batching, or LLM augmentation.

**If reactivated:** dispatch shape per architect requirement —
`void (async () => { const body = await llmGen.generate(...); sender.sendToChannel(channel, body, identity); })()`,
fire-and-forget per `notifier.ts:328` existing pattern. Addendum-not-replacement:
deterministic body sent first; LLM recap as separate threaded message.
Substring fact-guard becomes optional (not load-bearing) because the
deterministic block is always present.

---

## Phase D — DEFERRED indefinitely

Batching, grouping, threads. Locked-out by operator constraint
(`v5-conversational-discord-operator.md:287` — star topology preserves audit
trail). Re-decision required from operator before unlocking. Document in
NOTES.md if revisited.

---

## ADR

**Decision:** Implement Phase A (payload plumbing) + Phase B (rich
deterministic rendering) in two commits. Defer Phase C (LLM augmentation)
until cadence rises. Defer Phase D (threading/grouping) indefinitely.

**Drivers:**
1. Operator legibility binding (verbatim complaint cites truncation, terseness, non-conversationality).
2. Type safety preserved (`NotifierEntry<K>` generic narrowing).
3. Single-PR review surface vs split blast radius.

**Alternatives considered:**
- **A (Lean steelman)** — task_done + merge_result.test_failed only, ~80 LOC, one commit. **Rejected on incrementalism cost.** Payload extensions and renderers are co-designed; splitting ships a partially-rendered surface and adds a second review cycle for no risk reduction. The two-commit split inside Option B already captures the risk-reduction benefit Option A claimed, so A's split-PR overhead becomes pure tax. Recharacterized accurately: lean was *minimum-viable*, NOT "exclude failures forever".
- **C (Embed-rewrite)** — Discord embeds for richer formatting. **Rejected.** Cross-cuts `DiscordSender` interface (`sendToChannel(channel, body)` is string-only) + webhook payload migration. Out of scope for this plan's blast radius.
- **Per-event LLM polish** — implicit option C from earlier iterations. **Invalidated** on cost (~$0.40/task at $0.02/call × 20 events) and latency (160s aggregate fire-and-forget under burst). No version survived.
- **Threading** — locked out by `v5-conversational-discord-operator.md:287`.
- **Schema rewrite of `OrchestratorEvent`** — rejected as additive optional fields suffice and don't break orchestrator.test.ts type-only asserts.

**Why chosen:** A+B alone solves the operator complaint at zero LLM cost,
single-extension-point edits, low test blast radius. Two-commit split
mitigates B's diff size; A's split-PR overhead is unmitigatable in single-iteration
planning.

**Consequences:**
- `OrchestratorEvent` union grows by 4 fields (3 events). All optional except
  `errors` which defaults to `[]` on success.
- `TaskRecord` grows by 1 optional field; `KNOWN_KEYS` extended; B7
  unknown-key drop continues to protect downstream readers.
- **Confidence-drop trade-off (explicit):** `task_done` renders
  `responseLevelName` (sourced from `ResponseLevel.name` via
  `TaskRecord.lastResponseLevelName`) as a surrogate for confidence summary.
  The full `ConfidenceAssessment` object lives on `CompletionSignal.confidence`
  per-completion and is **not** propagated to `task_done`. Rationale:
  `task_done` is a one-line success notice; full confidence belongs on
  `completion_compliance` (already wired but currently ignored by notifier —
  out of scope here).
- Discord channel volume per task increases marginally (no new emissions;
  longer existing emissions). Acceptable per Driver 1.
- Phase C reactivation now uses qualitative trigger (no aggregator exists);
  may drift forever if operator never reviews cadence. Acknowledged.

**Follow-ups:**
- (Out of scope, deferred) Render `completion_compliance` to a structured
  ops summary if operators request it post-deploy.
- (Out of scope, deferred) Migrate to Discord embeds (Option C) if
  formatting feedback demands richer surfaces.
- **Phase C reactivation:** qualitative operator judgment on `dev_channel`
  cadence; revisit whether dev_channel notifications need rate-limiting,
  batching, or LLM augmentation.
- **Threading:** explicit re-decision required from operator before
  unlocking. Document in NOTES.md.

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Optional field additions break tests (A.7) | Low (verified) | Low | `events.some(e => e.type === ...)` type-only asserts at orchestrator.test.ts:363, :1291, :1499 |
| Substring pin churn in notifier.test.ts | Low (sweep verified ZERO modifications) | Low | Pin sweep table in §Phase B.3; substring matches survive additive deltas |
| Live Discord rendering looks wrong | Med | Med | Phase B.2 fixture matrix prerequisite to commit 2 merge |
| Failure-event regression | Low | High | Each failure path has fixture in B.2; rows 3, 5, 7, 8, 10, 12 covered |
| 2000-char body overflow | Low | Med | `truncateBody(body, 1900)` + `sanitizeRationale(s, 1024)` |
| Cost overrun from premature Phase C | — | — | Phase C deferred; trigger softened to qualitative |
| LLM-path test pinning (response-generator.ts:138-141) | — | — | Phase C deferred; not active in this plan |
| `responseLevelName` ordering — response_level fires before task_done | Low (verified) | Low | response_level fires inside session loop pre-merge; task_done fires post-merge; ordering invariant holds |

---

## Substring pin changes (full enumeration)

**Preserved (must continue to match; verified by §Phase B.3 sweep):**
- `/success/`, `/failure/` — `session_complete` body still contains these words.
- `/FAILED/` — `task_failed` template keeps "**FAILED**".
- `/ESCALATION/` — `escalation_needed` keeps "**ESCALATION**".
- `/level \*\*2\*\*/` — out-of-scope event; `response_level` notifier formatter UNCHANGED.
- `/5 phase/` — `project_decomposed` UNCHANGED.
- `/\$9\.80/`, `/\$10\.00/`, `/\$1\.23/`, `/\$4\.20/` — money formatting UNCHANGED.
- `/budget ceiling/` — `project_failed` reason interpolated unchanged in test fixtures (no failedPhase).
- `/generation 3/`, `/retry_with_directive/`, `/integration test/`, `/rejection #2/`, `/op-1/`, `/shelved/`, `/compaction/`, `/review_disagreement/` — all out-of-scope events UNCHANGED.
- `/@everyone/`, `/@here/` (negated forms) — sanitization preserved.
- `/[REDACTED]/` — `redactSecrets` preserved.
- Backtick-escape test (`\\``) — sanitize() backtick handling preserved.
- `/proj-xyz/`, `/auth-rewrite/` — unchanged event identity rendering.

**Net new (added asserts in commit 1 as `.skip`, un-skipped in commit 2):**
- `/failure — boom1; boom2 \[budget_exceeded\]/` — Row 3 errors+terminalReason
- `/attempt 2/` — Row 10 attempt rendering
- `/response level: reviewed/` — Row 9 with responseLevelName
- (negative) `response level:` absent — Row 9 without responseLevelName
- `/Options:/`, `/Context:/` — Row 11 escalation enrichment
- `/3 files: a, b, c/` — Row 7 rebase_conflict
- `/at phase `phase-1`/` — Row 12 with failedPhase
- (negative) `at phase` absent — Row 12 spawn-time

**Broken/changed:** ZERO. All deltas are additive substring concatenations preserving prior pins.

---

## Phase ordering and dependencies

- A → B (B consumes A's enriched payload)
- B.3 commit 1 (test scaffolding `it.skip`) → B.5 commit 2 (renderer + un-skip)
- C, D deferred

Suggested commit cadence: A as commit 0 (payload + state.ts schema only),
B as two atomic commits (commit 1 test scaffolding, commit 2 renderer +
un-skip + smoke fixtures). Phase A may also be folded into commit 2 if
the executor judges the diff manageable, but the two-commit split for
Phase B itself is mandatory.

---

## Iter-3 traceability (RALPLAN consensus)

Iter 1 Architect 7 + Critic 7 = 14 required changes
Iter 2 Architect 6 + Critic 4 NEW = 10 required changes
Iter 3 Architect APPROVE + Critic APPROVE → consensus reached.

All 24 prior required changes addressed; references throughout this plan
body.
