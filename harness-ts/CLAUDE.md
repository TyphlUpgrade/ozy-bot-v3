# harness-ts — TypeScript Dev Pipeline Harness

## What This Is
TypeScript rewrite of the Ozy v5 dev pipeline harness. Built on `@anthropic-ai/claude-agent-sdk` + vitest.

Loaded into every Architect / Executor / Reviewer / Dialogue agent session via `settingSources: ["project"]`. Rules below reach all spawned agents automatically.

## Commands
- `npm test` — run all tests (vitest)
- `npm run build` — compile TypeScript
- `npm run lint` — typecheck only (tsc --noEmit)
- `npm run audit:epistle-pins` — verify Discord renderer substring pins (Wave E-α AC1)

## ⚠️ READ FIRST (before any planning, implementation, or review)

These wiki pages are load-bearing. Read once at session start. Re-consult by `wiki_read` when in doubt:

- `[[harness-ts-core-invariants]]` — 10 architectural rules (I-1..I-10) that must not break
- `[[harness-ts-types-reference-source-of-truth]]` — verbatim type signatures + 27-event allow-list; PREVENTS fabrication
- `[[harness-ts-common-mistakes]]` — 12 documented mistakes (M-1..M-12) with root cause + prevention
- `[[harness-ts-live-setup]]` — recipes for `live-bot-listen`, `live-project`, `live-discord-smoke`; project.toml template

For phase delivery history: `[[harness-ts-phase-roadmap]]`. For plan files: `[[harness-ts-plan-index]]`.

## Core Invariants (1-liner each; full text in wiki)

- **I-1 Discord opaque to agents (LOCKED).** Agent sessions never see Discord directly. Operator input arrives ONLY via `relayOperatorInput(projectId, plainText)`. No Discord ids / mentions / embeds / reactions cross the boundary. `WebhookSender` / `BotSender` / `DiscordNotifier` stay in `src/discord/`, never imported from `src/session/*`. Enforced by `tests/lib/no-discord-leak.test.ts`.
- **I-2 Never invent metrics.** Every event field, every rendered token must trace to existing `type:field` cited at `filename:line`. No derived / invented quantities. Confidence lives on `CompletionSignal.confidence` (manager.ts:38), NOT `SessionResult`.
- **I-3 Additive optional fields only.** Extensions to `OrchestratorEvent` / `TaskRecord` / `CompletionSignal` add NEW optional fields. No removes or renames without documented migration. Update `KNOWN_KEYS` (state.ts:97) when `TaskRecord` gains a field.
- **I-4 Verbatim allow-lists over inferred names.** Citing an event type? `grep "type: \"" src/orchestrator.ts:107-136` first. Citing a TaskState? `grep src/lib/state.ts:30-82`. Citing an EscalationType? Check the 5 valid values. Don't generate from "what feels right".
- **I-5 Type-only imports allowed across layers.** `import type` is erased at compile time. `src/lib/review-format.ts` doing `import type { ReviewFinding } from "../gates/review.js"` is SAFE. Negative-lookahead regex in `no-discord-leak.test.ts` allows type-only imports.
- **I-6 Two-commit atomic split for wave work.** Commit 1: mechanical extraction, ZERO behavior change, tests use `it.todo` strings (no symbol imports of commit-2 work). Commit 2: behavior + un-skip + identity assertion updates. Single revert restores baseline.
- **I-7 Substring pin titlecase preservation.** `tests/discord/notifier.test.ts` has ~30 case-sensitive `.toContain()` asserts. `Options:` not `options:`. `Context:` not `context:`. `FAILED` allcaps. Phase A pin :309 = `failure — boom1; boom2 [budget_exceeded]` (em-dash U+2014, `; ` glue).
- **I-9 markPhaseSuccess single re-read.** `state.markPhaseSuccess(taskId, completion)` does state writes only. Caller does ONE `getTask(taskId)` after, then reuses for both `cascadePhaseOutcome` and `emit`. NO double `getTask`. Failure paths NOT extracted (asymmetric — fail writes only `lastError`).
- **I-10 Single owner per file layer.** `src/discord/*` imports lib/gates/session/orchestrator; nothing imports it back. `src/session/*` imports lib/gates only. `src/lib/*` imports itself + type-imports from gates/orchestrator. `src/gates/*` imports lib/session.

## Anti-patterns observed in past sessions (M-1..M-12)

- **DO NOT** invent event type names. Past fabrications: `phase_started`, `architect_phase_start`, `review_arbitration_resolved`, `NotifierEvent` (correct: `OrchestratorEvent`), `ambiguous_scope` (correct: `scope_unclear`). Grep before citing.
- **DO NOT** specify state-machine helper preconditions without reading the actual call site. M-2 case: assumed `transition` happened inside helper; actually was at call site, would have fired every time.
- **DO NOT** conflate phase concepts with event types. Phases = state-machine entities (`PhaseStore`). Events = bus signals. Phase outcomes flow through `cascadePhaseOutcome → project_completed/failed`. NO `phase_started` event exists.
- **DO NOT** leave dead code as "future intent". When two render forms exist, branch on data presence (`if (event.summary) { return structured } else { return compact }`). Don't keep unused branch with a comment.
- **DO NOT** trust `PostToolUse: Edit hook additional context: Edit operation failed.` reminders. Hook signal is independent of tool result. Trust the tool result. Verify via grep / Read if uncertain.
- **DO NOT** drop scope silently across plan revisions. If scope shrinks vs prior iter, halt + clarify. See [[ralplan-procedure-failure-modes-and-recommended-mitigations]].

## Wiki Maintenance Directives

- **autoCapture disabled** at `.omc-config.json` (`wiki.autoCapture: false`). SessionEnd hook does NOT write generic session-log stubs. See [[wiki-curation-policy]].
- **When to write a curated session log:** non-trivial work + future-self utility + not already documented. Otherwise commit message + git history are canonical record.
- **New repeated mistake observed?** Append `M-N` row to `[[harness-ts-common-mistakes]]` with sessions affected, root cause, prevention.
- **New load-bearing rule established?** Append `I-N` row to `[[harness-ts-core-invariants]]`. Inline a 1-liner here in CLAUDE.md.
- **New type added to source-of-truth scope?** Update `[[harness-ts-types-reference-source-of-truth]]` verbatim block. Don't paraphrase.
- **Page rename or delete?** Sweep CLAUDE.md + index.md for stale `[[page-name]]` pointers.
- **Wiki tools:** `wiki_query` (search), `wiki_read` (full page), `wiki_add` (new page), `wiki_lint` (health check). Pages live at `.omc/wiki/*.md` (flat, no subdirs).

## Project Philosophies

- **Star topology.** Orchestrator = sole Discord client + sole audit-trail owner. Agent sessions are content producers, not endpoints. Preserves I-1.
- **Verbatim over derivation.** When citing externally-grounded identifiers (event types, role names, file paths, EscalationTypes), copy verbatim from source. Trust grep / Read against source code over memory.
- **Additive over breaking.** Schema extensions ship as optional fields. KNOWN_KEYS drop is the load-bearing safety net for unknown-key tolerance.
- **Two-commit revertability.** Wave-type work splits mechanical extraction from behavior change. Single `git revert <commit2>` returns to mechanical baseline cleanly.
- **Pointer-heavy CLAUDE.md, content-heavy wiki.** This file is INDEX into wiki, not duplicate of it. Update `[[wiki-link]]` targets when pages move; don't inline full content.
- **Caveman / verbosity.** User session may run in caveman mode (terse). DOES NOT apply to code, commit messages, security warnings, or this file. Project docs stay normal prose.

## Architecture

See `[[harness-ts-architecture]]` for core architecture (modules, state machine, merge queue, completion signal). See `[[harness-ts-phase-roadmap]]` for delivery history (Phase 0 → Phase 4+ pending).

## OMC State Cleanup (Known Issue)

When CWD is `harness-ts/` (a subdirectory, not git root), OMC writes mode state to
`harness-ts/.omc/state/sessions/{id}/` but `state_clear` resolves relative to the
**git root** (`ozy-bot-v3/.omc/state/`). This causes phantom stop-hook loops where
`/oh-my-claudecode:cancel` reports "nothing to clear" but the hook keeps firing.

**To fix:** Check both locations when cancelling modes:
```bash
# CWD-relative state (where the hook actually reads)
find .omc/state/sessions -name "*-state.json" -delete 2>/dev/null
# Git-root state (where state_clear operates)
find "$(git rev-parse --show-toplevel)"/.omc/state/sessions -name "*-state.json" -delete 2>/dev/null
```

This is an upstream OMC bug — `persistent-mode.cjs` and `state_clear` disagree on
which `.omc/state/` directory is authoritative when CWD != git root.
