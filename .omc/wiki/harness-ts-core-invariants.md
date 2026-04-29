---
title: Harness-TS Core Invariants
description: Load-bearing architectural rules. Read FIRST before any harness-ts work.
category: architecture
tags: ["harness-ts", "invariants", "principles", "architecture", "load-bearing"]
updated: 2026-04-27
---

# Harness-TS Core Invariants

**Read this page FIRST before planning, implementing, or reviewing any harness-ts work.** These rules have been violated repeatedly across sessions and are documented here as load-bearing — breaking any of them is a P0 regression.

If a plan, code change, or new design appears to require breaking one of these, STOP and re-read the rule. Almost always there's an additive path that preserves the invariant.

---

## I-1 — Discord Opaque to Agents (locked)

**CLawhip orchestrator is the SOLE Discord client.** Agent sessions (Architect / Reviewer / Executor / Dialogue) NEVER see Discord directly.

- Discord-derived content reaches agents ONLY via distilled `relayOperatorInput(projectId, plainText)` calls. Plain text fenced inside untrusted blocks; no Discord ids, channel ids, message ids, mentions, embeds, reactions, or emoji metadata cross the boundary.
- Discord-bound agent content flows ONLY via orchestrator → DiscordNotifier → DiscordSender.
- `WebhookSender` / `BotSender` / `DiscordNotifier` stay in `src/discord/`, NEVER imported from `src/session/*`.
- Test enforces this: `tests/lib/no-discord-leak.test.ts` regex with negative lookahead for type-only imports.
- Agent SDK `disallowedTools` (WebFetch, WebSearch, Task, Cron*, RemoteTrigger, ScheduleWakeup) NOT relaxed.
- Inter-bot reply chains (Phase E.5) are SYNTHESIZED orchestrator-side — agents never author `message_reference`.

**Why locked:** prevents agent sessions from being conversational endpoints. Operator inputs are content, not directives. Star topology preserves audit trail.

---

## I-2 — Never Invent Metrics

Every rendered token, every payload field, every event property MUST trace to a real existing `type:field` cited at `filename:line`. NO derived/invented quantities.

- `confidence: SessionResult` — WRONG. Confidence lives on `CompletionSignal.confidence` (manager.ts:38).
- `firstOpenQuestion: SessionResult` — WRONG. Derived from `CompletionSignal.confidence?.openQuestions[0]` (manager.ts:53).
- `formatConfidence(c): N/5` — REJECTED. Pseudo-quantitative score from categorical fields = invention.

**Where to verify field provenance:** [[harness-ts-types-reference-source-of-truth]] has authoritative copies. Always READ that page before citing any type:field.

---

## I-3 — Additive Optional Fields Only (no breaking schema changes)

When extending `OrchestratorEvent`, `TaskRecord`, `CompletionSignal`, etc., add NEW fields as optional. Do NOT remove or rename existing fields without a documented migration path.

- Backwards-compat invariant: existing `events.some(e => e.type === ...)` type-only asserts in tests must continue to pass.
- `KNOWN_KEYS` in state.ts:97 must be extended whenever TaskRecord gains a field; B7 unknown-key drop is the load-bearing safety net.
- Field is REQUIRED only if it has a sensible default (e.g. `errors: string[]` defaults to `[]`).

---

## I-4 — Verbatim Allow-Lists Over Inferred Names

When listing event types, role names, file paths, or any externally-grounded identifiers in plans/prompts, COPY VERBATIM from source. Do NOT generate from "what feels right".

- 27 event types: see [[harness-ts-types-reference-source-of-truth]] for verbatim list.
- 4 IdentityRoles: `executor`, `reviewer`, `architect`, `orchestrator` — exhaustive, no others.
- 5 EscalationTypes: `clarification_needed`, `design_decision`, `blocked`, `scope_unclear`, `persistent_failure`.
- 10 TaskStates: see types-reference page.

**FABRICATIONS to refuse on sight (past hallucinations):**
- `phase_started`, `phase_succeeded`, `phase_failed` — phases use `project_completed`/`project_failed`
- `architect_phase_start`, `architect_phase_end` — no such events
- `review_arbitration_resolved` — no such event; only `review_arbitration_entered` exists; resolution flows through `arbitration_verdict`
- `NotifierEvent` — WRONG name; actual type is `OrchestratorEvent`
- `ambiguous_scope` for EscalationType — WRONG; valid is `scope_unclear`

---

## I-5 — Type-Only Imports Allowed Across Layer Boundaries

`tsconfig.json` lacks `verbatimModuleSyntax` and `isolatedModules`. So `import type { ... }` is COMPLETELY ERASED at compile time — no runtime coupling.

- `src/lib/review-format.ts` doing `import type { ReviewFinding } from "../gates/review.js"` is SAFE — no runtime cycle.
- `tests/lib/no-discord-leak.test.ts` regex uses negative lookahead `(?!type\s)` to ALLOW type-only imports.
- Layer rule: `discord` may import from `lib`, `gates`, `session`. None may runtime-import from `discord`.

---

## I-6 — Two-Commit Atomic Split for Wave-Type Work

When a wave introduces both behavior change AND structural extraction:
- **Commit 1**: mechanical extraction; ZERO behavior change. Tests use `it.todo("description")` strings only — NO symbol imports of commit-2 work (would break `tsc --noEmit`).
- **Commit 2**: behavioral changes + un-skip tests + atomic identity assertion updates. Single revert restores baseline.

**Why:** `git revert <commit2>` returns to commit-1 baseline cleanly. Commit-1 stubs become harmless skipped tests on revert.

**Past wave that used this protocol:** Phase B (commits 3fd81a8 + 32ce0ea), Wave E-α (commits 66801b0 + 5bec3dc).

---

## I-7 — Substring Pin Titlecase Preservation

`tests/discord/notifier.test.ts` has ~30+ substring asserts via `.toContain("...")`. Many are case-sensitive.

- `Options:` (titlecase, NOT `options:`)
- `Context:` (titlecase, NOT `context:`)
- `FAILED` (allcaps)
- `ESCALATION` (allcaps)
- `merged` (lowercase, OK in `**merged**`)
- Phase A pin :309 — `failure — boom1; boom2 [budget_exceeded]` (em-dash U+2014 + `; ` glue + bracketed terminalReason)

When refactoring renderers, preserve EXACT bytes for these pins. Audit script `npm run audit:epistle-pins` extracts pins via regex `/\.toContain\(["']([^"']+)["']\)/g` and verifies coverage.

---

## I-8 — Two-Commit Test Update Before Renderer

If renderer change WILL break existing test pins, the test-update commit lands BEFORE the renderer commit. Per Phase A two-commit protocol. Commit body enumerates `file:line → old → new` for every pin change.

---

## I-9 — markPhaseSuccess Single Re-Read Pass-By-Reference

Wave E-α D1: `markPhaseSuccess(taskId, {summary, filesChanged})` does state writes only (transition + updateTask). Caller (orchestrator) does ONE `getTask(taskId)` after, then reuses result for both `cascadePhaseOutcome(refreshed)` and `emit({...refreshed})`.

NO double `getTask` calls. NO race window between `updateTask` and `emit`.

Failure paths NOT extracted (asymmetric) — failed path writes only `lastError`, no multi-field structured completion to atomicize.

---

## I-10 — Single Owner Per File Layer

- `src/discord/*` — Discord transport + rendering. Imports from `lib`, `gates`, `session`, `orchestrator`. NEVER imported by `src/session/*`.
- `src/session/*` — agent sessions (architect, executor, reviewer, dialogue). Imports from `lib`, `gates`. Never imports `discord`.
- `src/lib/*` — shared utilities + state + types. Imports from itself. May type-import from `gates` or `orchestrator`.
- `src/gates/*` — review + merge gates. Imports from `lib`, `session`. Pure ops, no side effects beyond merge gate's git operations.
- `src/orchestrator.ts` — top-level event bus + state machine. Imports everything.

---

## Cross-refs

- [[harness-ts-types-reference-source-of-truth]] — verbatim type signatures
- [[harness-ts-common-mistakes]] — actual repeated mistakes + how to avoid
- [[harness-ts-architecture]] — full architecture overview
- [[ralplan-procedure-failure-modes-and-recommended-mitigations]] — RALPLAN consensus failure modes
- [[phase-e-agent-perspective-discord-rendering-intended-features]] — Phase E intended scope
