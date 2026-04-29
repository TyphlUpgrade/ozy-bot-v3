# Wave E-α — Deterministic Identity & Templates (harness-ts Discord)

**Created:** 2026-04-26
**Status:** Plan body iter-4 (Architect+Critic consensus halted at iter 4 of 5; 9 known fixes integrated below)
**Predecessor:** Phase A+B LANDED (commits e585c3c / 3fd81a8 / 32ce0ea / 1513c71). See `.omc/plans/2026-04-26-discord-conversational-output.md`.
**Wave context:** First of 4 Phase E waves per `.omc/wiki/phase-e-agent-perspective-discord-rendering-intended-features.md`. Subsequent waves E-β (reply chains), E-γ (LLM voice), E-δ (periodic+routing) ship as separate consensus passes.

---

## Closure table — iter-1 through iter-4 prior requireds

| Item | Origin | Resolution |
|---|---|---|
| A1 | iter-1 Architect (commit policy) | §10 Commit policy — commit-1 mechanical extraction (skipped tests); commit-2 behavioral + un-skip + audit |
| A2-1 | iter-2 Architect (allow-list) | D2 §3 switch over verbatim 27-event union; AC7 grep enforces |
| A2-2 | iter-2 Architect (fixture format) | D4 inline TS array (Phase B precedent at scripts/live-discord-smoke.ts:127-161) |
| A2-3 | iter-2 Architect (markPhaseSuccess precondition) | D1 §2 — collapse pattern (a); precondition "merging"; transitions to "done" internally |
| A2-4 | iter-2 Architect (D6 path) | D6 — `tests/discord/fixtures/epistle-timestamp.ts` (matches existing tests/discord/ subdir) |
| A2-5 | iter-2 Architect (D4 lean trim) | D4 — 6 fixtures (was 20); only events that change identity + Phase A integration |
| A2-6 | iter-2 Architect (AC1 audit functional) | AC1 — runnable script extracts toContain literals via regex, instantiates renderer, asserts pin coverage |
| N-1 | iter-2 Critic (markPhaseSuccess asymmetry) | D1 §3 — success-only ship; failure path stays untouched (rationale: structured completion vs lastError-only) |
| N-2 | iter-2 Critic (verbatim allow-list) | AC7 — `tests/discord/fixtures/allowed-events.txt` checked into repo; comm verification |
| N-3 | iter-2 Critic (D6 tests-only) | D6 — doc note "tests-only; production uses defaultCtx() wall-clock"; ESLint guard deferred |
| A3-1 | iter-3 Architect (D2 dicebear DROP) | D2 §1 — DROPPED from Wave E-α; smoke fixtures use webhook-bound default avatars (no DISCORD_AGENT_DEFAULTS edit) |
| A3-2 | iter-3 Architect (D3 honest re-scope) | D3 §3 — explicit per-file LOC accounting + per-event listing |
| A3-3 | iter-3 Architect (identity.test.ts) | D2 §4 — table-driven test with one assertion per 27 event types |
| A3-4 | iter-3 Architect (D1 re-read between updateTask + emit) | D1 §2 — explicit step 5 single getTask after updateTask; result reused in steps 6+7 |
| A3-5 | iter-3 Architect (cascadePhaseOutcome takes TaskRecord) | D1 §2 — helper internally `getTask(taskId)`, passes refreshed TaskRecord to cascade |
| A3-6 | iter-3 Architect (smoke fixture export) | AC1 §2 — smoke refactored to `export const SMOKE_FIXTURES`; entrypoint guard preserved per R-IT5-4 |
| A3-7 | iter-3 Architect (EpistleContext type ordering) | §10 — D3 + D6 land same commit; D6 imports EpistleContext from D3 (commit-1 atomic) |
| A3-T | iter-3 Architect (closure table) | THIS TABLE |
| C-1 | iter-3 Critic (regex parser commit) | AC1 — regex `/\.toContain\(["']([^"']+)["']\)/g` pinned; no AST/ts-morph |
| C-2 | iter-3 Critic (task_done emit fields) | D1 §2 emit shape includes summary+filesChanged from re-read |
| C-3 | iter-3 Critic (F6 byte-equality) | AC8 — explicit substring assertion preserving Phase A pin :309 (em-dash U+2014 + glue chars) |
| **R-IT5-1** | iter-4 Architect (FABRICATED events) | D2 §3 — corrected switch uses ONLY 27 verbatim events; `review_arbitration_resolved`/`architect_phase_start`/`architect_phase_end` REMOVED; `architect_spawned`/`architect_respawned`/`architect_arbitration_fired`/`review_arbitration_entered` map to architect/reviewer respectively |
| **R-IT5-2** | iter-4 Architect (NotifierEvent rename) | Global — plan uses `OrchestratorEvent` (orchestrator.ts:107) throughout; no `NotifierEvent` references |
| **R-IT5-3** | iter-4 Architect (AC7 process substitution) | AC7 — temp-file form: `sort union > /tmp/u.txt; sort allow > /tmp/a.txt; comm -23 /tmp/u.txt /tmp/a.txt`. POSIX-portable. |
| **R-IT5-4** | iter-4 Architect (smoke entrypoint preservation) | AC1 §2 — smoke gains `if (import.meta.url === pathToFileURL(process.argv[1]).href) main()` guard; SMOKE_FIXTURES becomes top-level export |
| **R-IT5-5** | iter-4 Critic (union extension blast radius) | D1 §2 — `OrchestratorEvent.task_done` variant gains optional `summary?`, `filesChanged?`. AC2 typecheck verifies all consumers (orchestrator.ts, notifier.ts, dispatcher.ts, response-generator.ts, message-context.ts; verified type-only `events.some(e => e.type === ...)` asserts in tests are unaffected per Phase A iter-3 pattern) |
| **R-IT5-6** | iter-4 Critic (single re-read pass-by-ref) | D1 §2 — exactly ONE getTask call at step 5; result `refreshed` passed to step 6 cascade and step 7 emit. No double re-read. |
| **R-IT5-7** | iter-4 Critic (D4 fixture URLs) | D4 §3 — webhook routing handles avatar via WebhookSender's `username + avatarURL` from notifier-resolved identity (not hardcoded URLs in fixture body). DISCORD_AGENT_DEFAULTS remain empty avatar URL → webhook-default avatar (Wave E-β adds dicebear placeholders). |
| **R-IT5-8** | iter-4 Critic (AC1 template-literal exclusion) | AC1 §1 — code comment "Regex intentionally handles single+double quotes only; if backtick template literals introduced in notifier.test.ts, tighten regex." |
| **R-IT5-9** | iter-4 Critic (circular import direction) | §11 Risk — explicit dependency-direction assertion: `identity.ts → OrchestratorEvent (orchestrator.ts)` only; orchestrator.ts MUST NOT import identity.ts. AC2 typecheck catches reverse-direction; manual review at PR time confirms. |

---

## Principles (5)

1. **CRITICAL ARCHITECTURE INVARIANT (locked):** CLawhip orchestrator = SOLE Discord client. Agent sessions NEVER see Discord directly. `WebhookSender` / `BotSender` / `DiscordNotifier` stay in `src/discord/`, never imported from `src/session/*` (enforced by `tests/lib/no-discord-leak.test.ts`).
2. **Field-source verified.** Every payload field traces to existing type:field at filename:line.
3. **Substring pin titlecase preserved** — `Options:`, `Context:`, `FAILED`, `ESCALATION` retained verbatim.
4. **Wrapper extraction over rewrite** — preserve NOTIFIER_MAP shape; preserve dispatch (notifier.ts:357-395).
5. **Two-commit atomic split** — commit-1 mechanical (no behavior change); commit-2 behavioral.

## Decision Drivers (top 3)

1. Operator-visible identity diversification (executor/reviewer/architect/orchestrator distinct webhooks)
2. Preserve CW-3 dispatch contract (no message-id, projectId, error-handling regressions)
3. Preserve Phase A pin :309 byte equality

---

## Field-source matrix (verified type:field)

| Field | Source | Citation |
|---|---|---|
| `session_complete.summary` | `CompletionSignal.summary` | src/session/manager.ts:32 |
| `session_complete.filesChanged` | `CompletionSignal.filesChanged` | src/session/manager.ts:33 |
| `task_done.summary` | `TaskRecord.summary` (post-merge write via D1) | src/lib/state.ts:81 |
| `task_done.filesChanged` | `TaskRecord.filesChanged` (post-merge write via D1) | src/lib/state.ts:82 |
| `task_done.commitSha` | `MergeResult.commitSha` (merged variant only) | src/gates/merge.ts:13 |
| `task_done.costUsd` | `TaskRecord.totalCostUsd` | src/lib/state.ts:74 |
| `task_done.confidence` | `CompletionSignal.confidence` | src/session/manager.ts:38 |
| `task_done.firstOpenQuestion` | derived `CompletionSignal.confidence?.openQuestions?.[0]` | src/session/manager.ts:53 |
| `review_mandatory.reviewSummary` | `ReviewResult.summary` | src/gates/review.ts:51 |
| `review_mandatory.reviewFindings` | `ReviewResult.findings: ReviewFinding[]` | src/gates/review.ts:30-36, 50 |
| Phase A `errors[]` | `SessionResult.errors` | src/session/sdk.ts:42 |
| Phase A `terminalReason?` | `SessionResult.terminalReason` | src/session/sdk.ts:46 |

---

## D0 — `formatFindingForOps` helper (NEW src/lib/review-format.ts)

**File:** `src/lib/review-format.ts` (NEW, ~25 LOC).

Renders a single `ReviewFinding` to a single-line ops-channel string:
```ts
import type { ReviewFinding } from "../gates/review.js";  // type-only import (no runtime coupling)

export function formatFindingForOps(f: ReviewFinding): string {
  const line = f.line !== undefined ? String(f.line) : "?";
  return `[${f.severity}] ${f.file}:${line} — ${f.description}`;
}
```

Used by D3 epistle template for `review_mandatory` to render `reviewFindings: ReviewFinding[]` as bullet list (one `formatFindingForOps(f)` line per finding).

**`src/gates/review.ts` re-exports for back-compat:**
```ts
export { formatFindingForOps } from "../lib/review-format.js";
```

Type-only import from `gates/review.ts` to `lib/review-format.ts` is safe — TypeScript handles type-only imports at compile time (tsconfig.json lacks `verbatimModuleSyntax` so import type is fully erased; no circular runtime dep).

**Acceptance:**
- D0.1 `npm run typecheck` clean — type-only import does not create runtime cycle.
- D0.2 `tests/lib/review-format.test.ts` covers: (a) renders `[critical] foo.ts:42 — desc`; (b) substitutes `?` for missing line; (c) preserves whitespace in description.

**Lands in commit 1** alongside D2/D3/D6 mechanical extraction.

---

## D1 — markPhaseSuccess (success-only, collapse pattern)

**File:** `src/lib/state.ts` (NEW StateManager method, ~12 LOC).
**Call site:** Replaces `case "merged"` body at `src/orchestrator.ts:830-838`.

**Asymmetry rationale (N-1):** failure path (orchestrator.ts:867-879) writes only `lastError` (already plumbed) and does not have multi-field structured completion. Extracting `markPhaseFailure` would add ceremony without payoff. Failed path stays as `transition→failed + updateTask({lastError}) + emit task_failed + cascadePhaseOutcome`.

**Signature:**
```ts
markPhaseSuccess(
  taskId: string,
  completion: { summary: string; filesChanged: string[] }
): void
```

**Step sequence (R-IT5-6 single re-read):**
```
1. const task = state.getTask(taskId);
2. if (task?.state !== "merging") throw new Error(`markPhaseSuccess requires merging state, got ${task?.state}`);
3. state.transition(taskId, "done");
4. state.updateTask(taskId, { summary: completion.summary, filesChanged: completion.filesChanged });
5. const refreshed = state.getTask(taskId);  // SINGLE re-read; pass-by-ref to 6+7
6. cascadePhaseOutcome(refreshed, "success");  // R-IT5-5 — TaskRecord, not taskId
7. emit({
     type: "task_done",
     taskId,
     responseLevelName: refreshed?.lastResponseLevelName,
     summary: refreshed?.summary,
     filesChanged: refreshed?.filesChanged,
   });
```

**Union extension (R-IT5-5):** `OrchestratorEvent.task_done` gains optional `summary?: string`, `filesChanged?: string[]`. Additive optional → no consumer break. AC2 typecheck verifies orchestrator.ts/notifier.ts/dispatcher.ts/response-generator.ts/message-context.ts unaffected.

---

## D2 — Identity resolver (`src/discord/identity.ts` NEW)

**§1 Dicebear DROP (A3-1):** Wave E-α does NOT touch `DISCORD_AGENT_DEFAULTS` (config.ts:209-213). Smoke fixtures use webhook-default avatar; per-identity URLs deferred to Wave E-β.

**§3 src/discord/identity.ts** (~40 LOC; R-IT5-1 corrected event names):
```ts
import type { OrchestratorEvent } from "../orchestrator.js";

export type IdentityRole = "executor" | "reviewer" | "architect" | "orchestrator";

export function resolveIdentity(event: OrchestratorEvent): IdentityRole {
  switch (event.type) {
    // Executor — built/ran the work
    case "session_complete":
    case "task_done":
      return "executor";

    // Reviewer — gates review verdict
    case "review_mandatory":
    case "review_arbitration_entered":
      return "reviewer";

    // Architect — project/phase/arbitration lifecycle
    case "architect_spawned":
    case "architect_respawned":
    case "architect_arbitration_fired":
    case "arbitration_verdict":
    case "project_declared":
    case "project_decomposed":
    case "project_completed":
    case "project_failed":
    case "project_aborted":
    case "compaction_fired":
      return "architect";

    // Orchestrator — system/lifecycle events (default)
    case "task_picked_up":
    case "merge_result":
    case "task_shelved":
    case "task_failed":
    case "poll_tick":
    case "shutdown":
    case "escalation_needed":
    case "checkpoint_detected":
    case "response_level":
    case "completion_compliance":
    case "retry_scheduled":
    case "budget_exhausted":
    case "budget_ceiling_reached":
      return "orchestrator";
  }
}
```

All 27 OrchestratorEvent variants covered exactly once; TypeScript exhaustive switch verifies. Identity assignment per event:
- `executor` (2): session_complete, task_done
- `reviewer` (2): review_mandatory, review_arbitration_entered
- `architect` (10): architect_spawned, architect_respawned, architect_arbitration_fired, arbitration_verdict, project_declared, project_decomposed, project_completed, project_failed, project_aborted, compaction_fired
- `orchestrator` (13): task_picked_up, merge_result, task_shelved, task_failed, poll_tick, shutdown, escalation_needed, checkpoint_detected, response_level, completion_compliance, retry_scheduled, budget_exhausted, budget_ceiling_reached

**§4 tests/discord/identity.test.ts** — table-driven, exactly 27 cases (one per OrchestratorEvent type):
```ts
const CASES: Array<[OrchestratorEvent["type"], IdentityRole]> = [
  ["task_picked_up", "orchestrator"],
  ["session_complete", "executor"],
  ["merge_result", "orchestrator"],
  ["task_shelved", "orchestrator"],
  ["task_failed", "orchestrator"],
  ["task_done", "executor"],
  ["poll_tick", "orchestrator"],
  ["shutdown", "orchestrator"],
  ["escalation_needed", "orchestrator"],
  ["checkpoint_detected", "orchestrator"],
  ["response_level", "orchestrator"],
  ["completion_compliance", "orchestrator"],
  ["retry_scheduled", "orchestrator"],
  ["budget_exhausted", "orchestrator"],
  ["project_declared", "architect"],
  ["project_decomposed", "architect"],
  ["project_completed", "architect"],
  ["project_failed", "architect"],
  ["project_aborted", "architect"],
  ["architect_spawned", "architect"],
  ["architect_respawned", "architect"],
  ["architect_arbitration_fired", "architect"],
  ["arbitration_verdict", "architect"],
  ["review_arbitration_entered", "reviewer"],
  ["review_mandatory", "reviewer"],
  ["budget_ceiling_reached", "orchestrator"],
  ["compaction_fired", "architect"],
];
it.each(CASES)("resolveIdentity(%s) = %s", (type, expected) => {
  // construct minimal event of given type with required fields
  const event = makeEvent(type);
  expect(resolveIdentity(event)).toBe(expected);
});
```

---

## D3 — Renderer extraction (`src/discord/epistle-templates.ts` NEW + NOTIFIER_MAP wrapper)

**§3 Honest line accounting (A3-2):**

| File | Action | LOC |
|---|---|---|
| `src/discord/identity.ts` | NEW (D2) | +40 |
| `src/discord/epistle-templates.ts` | NEW — `renderEpistle(event, identity, ctx)` switch over event.type, one template per epistle-eligible event (~6) | +150 |
| `src/discord/notifier.ts` NOTIFIER_MAP | Modified — wrap 6 entries with `format: (e, ctx?) => renderEpistle(e, resolveIdentity(e), ctx ?? defaultCtx())`; identity field updated for ~5 entries per E.2 | ~30 modified |
| `src/discord/notifier.ts` dispatch (357-395) | UNCHANGED — CW-3 message-id recording, projectId resolution, error handling preserved |  |

**Per-event listing — gain renderEpistle wrapper:**
- `session_complete` (success + failure branches)
- `task_done`
- `merge_result` (merged + test_failed branches)
- `task_failed`
- `escalation_needed`
- `project_failed`
- `review_mandatory`

All other ~16 NOTIFIER_MAP entries keep existing inline format lambda untouched.

**Wrapper shape:**
```ts
// Before (Phase B):
{ channel: "dev_channel", identity: "orchestrator", format: (e) => `Task done: ${e.taskId}` }
// After (Wave E-α):
{ channel: "dev_channel", format: (e, ctx?) => renderEpistle(e, resolveIdentity(e), ctx ?? defaultCtx()) }
// identity field removed — resolveIdentity handles per-event
```

`defaultCtx() = { timestamp: new Date().toISOString() }` exported from epistle-templates.ts.

**Multi-paragraph epistle template** (renderEpistle return):
```
{emoji} **{Bold Label}** — `YYYY-MM-DDTHH:MM:SSZ`

{opener prose paragraph (deterministic; no LLM)}

- **TitleCase Tag:** value
- **TitleCase Tag:** value

{optional fenced code block for error excerpts}

{closing forward-looking paragraph (deterministic; no LLM)}
```

`truncateBody(body, 1900)` wraps every output. `truncateRationale(s, 1024)` for rationale fields (existing helper from src/lib/text.ts:66).

---

## D4 — 6 inline TS fixtures (extends scripts/live-discord-smoke.ts SMOKE_FIXTURES)

**Format (A2-2 / R-IT5-7):** Inline TS array `Parameters<typeof notifier.handleEvent>[0][]` per Phase B precedent. Avatar resolution via webhook-bound default (no hardcoded URLs in fixture body — DISCORD_AGENT_DEFAULTS deferred to Wave E-β).

| # | Event | Pre identity | Post identity | Purpose |
|---|---|---|---|---|
| F1 | `session_complete` (success: true, errors: []) | orchestrator | **executor** | Identity change baseline |
| F2 | `task_done` (with summary + filesChanged + responseLevelName) | orchestrator | **executor** | Identity change + D1 helper output rendering |
| F3 | `review_mandatory` (with reviewSummary + reviewFindings ReviewFinding[]) | orchestrator | **reviewer** | Identity change + structured findings render |
| F4 | `review_arbitration_entered` | orchestrator | **reviewer** | Identity baseline |
| F5 | `arbitration_verdict` | architect | **architect** (UNCHANGED) | Phase B baseline preservation regression guard |
| F6 | `session_complete` (success: false, errors: ["boom1","boom2"], terminalReason: "budget_exceeded") | orchestrator | **executor** | Phase A pin :309 byte-equality verification (AC8) |

---

## D6 — Reproducible timestamp helper

**File:** `tests/discord/fixtures/epistle-timestamp.ts`

```ts
import type { EpistleContext } from "../../../src/discord/epistle-templates.js";

// TESTS-ONLY — production callers use defaultCtx() from epistle-templates.ts (wall-clock).
// Importing this file from src/** is forbidden (ESLint rule deferred — no eslint config in harness-ts at iter-4).
export const FIXED_EPISTLE_TIMESTAMP = "2026-04-26T20:00:00.000Z";
export const frozenCtx = (): EpistleContext => ({ timestamp: FIXED_EPISTLE_TIMESTAMP });
```

Lands in commit-1 alongside D3 (A3-7 type-ordering — `EpistleContext` exported from epistle-templates.ts in same commit).

---

## AC1 — Audit script (`scripts/audit-epistle-pins.ts`)

**§1 Parser commitment (C-1, R-IT5-8):**
```ts
// Regex intentionally handles single+double quotes only.
// If backtick template literals introduced in notifier.test.ts, tighten regex.
const TO_CONTAIN_RE = /\.toContain\(["']([^"']+)["']\)/g;
```

**§2 Smoke fixture loading (A3-6, R-IT5-4):** `scripts/live-discord-smoke.ts` refactored to:
```ts
export const SMOKE_FIXTURES: Parameters<typeof DiscordNotifier.prototype.handleEvent>[0][] = [
  // existing 16 + 6 NEW from D4 = 22 total
  ...
];

async function main(): Promise<void> { /* existing flow; reads SMOKE_FIXTURES */ }

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => { console.error("[discord-smoke] FATAL", e); process.exit(2); });
}
```

Audit script imports the constant.

**Algorithm:**
1. Read `tests/discord/notifier.test.ts` source; extract every `.toContain("X")` literal via TO_CONTAIN_RE → `pinSet`.
2. Import `SMOKE_FIXTURES` from refactored smoke.
3. For each fixture: invoke `renderEpistle(fixture, resolveIdentity(fixture), frozenCtx())`.
4. For each pin in `pinSet`: assert at least one rendered output contains it.
5. Exit 1 if any pin unmatched. Print unmatched list.

`package.json` adds: `"audit:epistle-pins": "tsx scripts/audit-epistle-pins.ts"`.

---

## AC7 — Allow-list grep verification

**Inputs:**
- `tests/discord/fixtures/allowed-events.txt` — 27-event verbatim list, one per line, checked into repo.
- `tests/discord/notifier.test.ts` (D4 fixtures live here).

**Command (R-IT5-3 POSIX-portable temp-file form):**
```bash
grep -oE '"type":\s*"[a-z_]+"' tests/discord/notifier.test.ts \
  | sed -E 's/.*"([a-z_]+)"$/\1/' | sort -u > /tmp/wave-e-alpha-types.txt
sort tests/discord/fixtures/allowed-events.txt > /tmp/wave-e-alpha-allowed.txt
comm -23 /tmp/wave-e-alpha-types.txt /tmp/wave-e-alpha-allowed.txt
```

**Pass condition:** empty stdout (zero fixture event types outside allow-list).

---

## AC8 — F6 byte-equality regression test

```ts
import { renderEpistle } from "../../src/discord/epistle-templates.js";
import { resolveIdentity } from "../../src/discord/identity.js";
import { frozenCtx } from "./fixtures/epistle-timestamp.js";

it("F6 preserves Phase A pin :309 byte equality", () => {
  const event = {
    type: "session_complete" as const,
    taskId: "t1",
    success: false,
    errors: ["boom1", "boom2"],
    terminalReason: "budget_exceeded",
  };
  const out = renderEpistle(event, resolveIdentity(event), frozenCtx());
  // em-dash U+2014 + space + semicolon-space + brackets — preserves notifier.test.ts:309 contract
  expect(out).toContain("failure — boom1; boom2 [budget_exceeded]");
});
```

---

## Substring pin sweep (titlecase preservation)

| Pin | Strategy |
|---|---|
| `Options:` titlecase | bullet `**Options:**` not `**options:**` |
| `Context:` titlecase | bullet `**Context:**` |
| `FAILED` allcaps | header allcaps |
| `ESCALATION` allcaps | header allcaps |
| `merged` lowercase | body `**merged**` lowercase OK |
| `failure — boom1; boom2 [budget_exceeded]` (Phase A pin :309) | failure renderer joins errors[] with `"; "` + appends ` [terminalReason]` (em-dash U+2014); enforced by AC8 |
| All other Phase B pins | preserved by Phase B sweep guarantee |

---

## §10 Commit policy (atomic 2-split)

**Commit 1 — mechanical extraction (~245 LOC NEW; zero behavior change):**
- `src/lib/review-format.ts` NEW (D0, ~25 LOC) — `formatFindingForOps(f: ReviewFinding) -> string`
- `tests/lib/review-format.test.ts` NEW (D0.2)
- `src/gates/review.ts` — add `export { formatFindingForOps } from "../lib/review-format.js"` re-export (D0)
- `src/discord/identity.ts` NEW (D2 §3)
- `tests/discord/identity.test.ts` NEW (D2 §4)
- `src/discord/epistle-templates.ts` NEW (D3) — exports `renderEpistle`, `EpistleContext`, `defaultCtx`; uses `formatFindingForOps` for review_mandatory bullets
- `tests/discord/fixtures/epistle-timestamp.ts` NEW (D6 — depends on D3 EpistleContext within same commit)
- `tests/discord/fixtures/allowed-events.txt` NEW (AC7)
- `tests/lib/no-discord-leak.test.ts` NEW (Architecture Invariant guard)
- `tests/discord/epistle-fixtures.test.ts` NEW with all `it.todo("description")` strings (NO symbol imports of commit-2 work)
- `scripts/audit-epistle-pins.ts` NEW
- `scripts/live-discord-smoke.ts` refactor — `export const SMOKE_FIXTURES` + entrypoint guard (R-IT5-4)
- `package.json` — `audit:epistle-pins` script
- `src/discord/notifier.ts` NOTIFIER_MAP — wrapper `format: (e, ctx?) => renderEpistle(...)` on 6 entries (D3)

Commit 1 acceptance: `npm run lint` (typecheck) green; `npm test` green (existing tests pass; new it.todo show as todo).

**Commit 2 — behavioral + un-skip (~80 LOC):**
- `src/lib/state.ts` — NEW `markPhaseSuccess` method (D1, +12 LOC)
- `src/orchestrator.ts:830-838` — replace inline block with `markPhaseSuccess` call (D1)
- `src/orchestrator.ts` `OrchestratorEvent.task_done` union variant — gain `summary?: string`, `filesChanged?: string[]` (R-IT5-5; additive optional)
- `tests/discord/epistle-fixtures.test.ts` — un-skip 6 fixtures + add F6 byte-equality test (AC8); F2 emit fixture asserts summary+filesChanged
- `tests/discord/notifier.test.ts` — atomic update of any identity assertions for the 5 events that change (per D2)

Commit 2 acceptance: `npm run lint` + `npm test` + `npm run audit:epistle-pins` all green; AC7 grep verifies allow-list; live-discord-smoke.ts visual operator review.

**Pre-commit-2 audit gate (executor agent runs):**
```bash
npm test discord/notifier.test.ts && \
  npm run audit:epistle-pins && \
  bash -c '
    grep -oE "\"type\":\s*\"[a-z_]+\"" tests/discord/notifier.test.ts | sed -E "s/.*\"([a-z_]+)\"$/\1/" | sort -u > /tmp/types.txt
    sort tests/discord/fixtures/allowed-events.txt > /tmp/allowed.txt
    test -z "$(comm -23 /tmp/types.txt /tmp/allowed.txt)"
  '
```

---

## §11 Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Substring pin breaks (titlecase regression) | Med | High | AC1 audit script + AC8 byte-equality test |
| F6 Phase A pin :309 drifts | Low (verified) | High | AC8 explicit substring assertion with em-dash U+2014 |
| identity.ts misroutes new event | Low | Med | AC7 allow-list grep blocks merge; identity.test.ts table-driven 27 cases |
| markPhaseSuccess race | Low | Med | Single-thread Node event loop; single re-read step 5 (R-IT5-6) passed by reference |
| OrchestratorEvent.task_done union extension breaks consumer | Low (verified) | Med | Additive optional fields; existing `events.some(e => e.type === ...)` type-only asserts unaffected (Phase A iter-3 pattern) |
| Identity diversification breaks downstream filters | Low | Med | Smoke fixture covers all 4 identities; AC7 catches drift |
| Renderer non-determinism (Date inside renderer) | Low | Med | Caller injects timestamp via ctx; defaultCtx wall-clock at notifier.handleEvent dispatch site |
| Circular import identity.ts ↔ orchestrator.ts (R-IT5-9) | Low | Med | Direction asserted: `identity.ts → OrchestratorEvent (orchestrator.ts)` only; orchestrator.ts MUST NOT import identity.ts. AC2 typecheck catches reverse-direction |
| Audit script regex false-negative (template literals) | Low | Low | Comment in AC1 §1; tighten regex if backticks introduced |
| Smoke entrypoint break post-refactor | Low | Med | R-IT5-4 entrypoint guard preserves script behavior; manual smoke run verifies |
| Scope creep into E.4/E.5/E.6/E.8 | Med | High | Critic reviewer must reject any code touching LLM/reply-chains/nudge/mentions |

---

## §12 Rollback per commit

- **Commit 1 revert:** `git revert <c1>` removes scaffolding + identity.ts + epistle-templates.ts + smoke refactor. Phase A+B baseline preserved.
- **Commit 2 revert:** `git revert <c2>` restores Phase A+B emit logic at orchestrator.ts:830-838 + leaves commit-1 scaffolding as harmless skipped tests + un-tested epistle templates. Single-command rollback.
- **Audit-fail hotfix path:** if AC1 substring audit fails post-merge, ~5 LOC patch to epistle-templates.ts restoring titlecase token in renderer; re-run pin :309 to confirm green; open follow-up issue.

---

## §13 ADR

- **Decision:** Ship Wave E-α as 5-deliverable bundle (D1 markPhaseSuccess + D2 identity + D3 renderEpistle wrapper + D4 6 fixtures + D6 timestamp helper) in 2 atomic commits.
- **Drivers:** Operator-visible identity diversification; CW-3 dispatch contract preservation; Phase A pin :309 byte equality.
- **Alternatives considered:**
  - Lean E.1+E.2+E.3 only (rejected — leaves Principle 1 unguarded by no-discord-leak test)
  - Full NOTIFIER_MAP rewrite (rejected — touches generic narrowing at notifier.ts:362; expands blast radius)
  - markPhaseFailure pair (rejected — failed path writes only lastError; no multi-field atomicity payoff)
  - External JSON fixtures (rejected — breaks Phase B precedent)
  - DISCORD_AGENT_DEFAULTS dicebear in this wave (rejected — deferred to Wave E-β to keep wave bounded)
- **Why chosen:** minimal reversible diff that fully addresses E.2 identity drift + epistle unification; preserves all Phase A+B contracts.
- **Consequences:**
  - NOTIFIER_MAP `.format` signature widens to `(e, ctx?) => string` (additive optional ctx)
  - `OrchestratorEvent.task_done` union grows by 2 optional fields (summary?, filesChanged?)
  - StateManager grows by 1 method (markPhaseSuccess, +12 LOC)
  - 4 NEW files: identity.ts, epistle-templates.ts, identity.test.ts, epistle-timestamp.ts, no-discord-leak.test.ts
  - audit-epistle-pins.ts + allowed-events.txt + AC7 grep enforce allow-list discipline going forward
  - Renderer signature `renderEpistle(event, identity, ctx) -> string` reused for E-γ LLM tier
- **Follow-ups:**
  - Wave E-β: reply-API threading via message_reference; DISCORD_AGENT_DEFAULTS dicebear placeholders
  - Wave E-γ: LLM voice per role (OutboundResponseGenerator)
  - Wave E-δ: nudge_check + per-role mention routing
  - ESLint `no-restricted-imports` rule blocking `tests/discord/fixtures/` import from `src/**` once eslint config lands in harness-ts

---

## RALPLAN consensus history (for next-session pickup)

- Iter 1: Architect 7 + Critic 7 = 14 required
- Iter 2: Architect 6 + Critic 4 NEW = 10 required
- Iter 3: Architect 7 + Critic 6 = 13 required
- Iter 4: Architect 4 + Critic 5 NEW = 9 required
- Halted iter 4 → manual integration of all 33+ accumulated requireds into this plan body. See lead-engineer postmortem in session log.
