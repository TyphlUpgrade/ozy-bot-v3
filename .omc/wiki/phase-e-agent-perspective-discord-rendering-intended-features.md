---
title: "Phase E — Agent Perspective Discord Rendering (Intended Features)"
tags: ["phase-e", "discord", "notifier", "harness-ts", "agent-perspective", "intended-features", "complete"]
created: 2026-04-26T23:43:21.359Z
updated: 2026-04-29
sources: []
links: ["harness-ts-architecture.md", "harness-ts-wave-c-backlog.md", "v5-conversational-discord-operator.md", "session-log-discord-rich-rendering-2026-04-26.md", "phase-f-discord-richness-resilience-backlog.md"]
category: architecture
confidence: medium
schemaVersion: 1
---

# Phase E — Agent Perspective Discord Rendering (Intended Features)

# Phase E — Agent Perspective Discord Rendering (Intended Features)

> **COMPLETE 2026-04-29.** Phase E α/β/γ/δ all landed across cycles 2-3. Successor Discord backlog tracked in [[phase-f-discord-richness-resilience-backlog]]. This page preserved as the original intent capture + acceptance tables; consult Phase F for active work.

**Status:** Captured 2026-04-26 from operator-driven RALPLAN consensus iter 1-3.
**Predecessor:** Phase A+B (LANDED commits e585c3c / 3fd81a8 / 32ce0ea / 1513c71). See `.omc/plans/2026-04-26-discord-conversational-output.md`.
**Reference target:** clawhip / gaebal-gajae bot dialogue style (operator-supplied screenshot).

## Operator's Reference Target

- **Distinct bot identities with avatars** — visible per-role attribution (Architect/Reviewer/Executor/Orchestrator each render with own username + avatar)
- **Discord reply-API quote-cards** — bot-to-bot reply chain visible (e.g. clawhip @-mentions gaebal-gajae; gaebal-gajae responds in-thread with reply card preview)
- **Multi-paragraph epistles per post** — opener prose + section-header (emoji + bold label + em-dash + UTC timestamp) + bullet list with **bold tags:** inline + closing forward-looking paragraph
- **First-person declarative voice** — "I'll proceed", "I will treat as stale and clean up/replace"
- **Periodic nudge-check** — bot self-reports state on schedule even when no event fires ("🦀 Nudge Check — 20:40 UTC")
- **Operator @-mention routes to specific agent identities** — `@architect`, `@reviewer`, `@executor`

## CRITICAL ARCHITECTURE INVARIANT (LOCKED — Principle 1)

CLawhip orchestrator = SOLE Discord client. Agent sessions NEVER see Discord directly:

- Discord-derived content reaches agents ONLY via distilled `relayOperatorInput(projectId, plainText)` — no Discord ids/channels/mentions/embeds/reactions/emoji metadata cross boundary
- Discord-bound agent content flows ONLY via orchestrator → DiscordNotifier → DiscordSender
- `WebhookSender`/`BotSender`/`DiscordNotifier` stay in `src/discord/`, never imported from `src/session/*`
- All agent SDK sessions retain `disallowedTools` (WebFetch/WebSearch/Task/Cron*/RemoteTrigger/ScheduleWakeup)
- Reply chains synthesized by orchestrator; agents don't author message_reference
- Operator inputs distilled to plain text before relayOperatorInput

## Sub-Phase Decomposition (E.7 explicitly DROPPED per operator)

### E.1 — Payload extension (additive to Phase A)
- `session_complete` += `summary?: string`, `filesChanged?: string[]` (sources: `CompletionSignal.summary` manager.ts:32; `CompletionSignal.filesChanged` manager.ts:33)
- `task_done` += `summary?`, `filesChanged?`, `commitSha?`, `costUsd?`, `confidence?`, `firstOpenQuestion?` (sources: `CompletionSignal.confidence` manager.ts:38; openQuestions[0] manager.ts:53; `MergeResult.commitSha` merge.ts:13)
- `review_mandatory` += `reviewSummary?`, `reviewFindings?` (sources: `ReviewResult.summary` review.ts:48; `ReviewResult.findings` review.ts:50; structured `ReviewFinding` review.ts:30-36 — preserve as `ReviewFinding[]` not coerced string[])
- `TaskRecord` += success-snapshot fields written ONLY at merge-success (not every session_complete) — prevents multi-session lifecycle contamination
- New helper: `state.markPhaseSuccess(taskId, completion)` (NEW StateManager method, +8 LOC)
- New helper: `formatFindingForOps(f: ReviewFinding) -> string` at `src/lib/review-format.ts` (extracted shared); `gates/review.ts` re-exports for back-compat

### E.2 — NOTIFIER_MAP identity diversification
| Event | Identity (was → new) |
|---|---|
| `session_complete` (post-Executor) | orchestrator → **executor** |
| `task_done` (post-merge) | orchestrator → **executor** (executor built it) |
| `merge_result` | orchestrator (unchanged — orchestrator owns merge gate) |
| `review_mandatory` / `review_arbitration_entered` | — → **reviewer** |
| `architect_*` / `project_*` / `arbitration_verdict` | architect (unchanged) |
| `escalation_needed` | orchestrator (system-routed) |
| `task_picked_up` / `poll_tick` / `shutdown` / `retry_scheduled` | orchestrator (unchanged) |
| `nudge_check` (NEW E.6) | per-`sourceAgent` |

`DISCORD_AGENT_DEFAULTS` already includes executor + reviewer keys (config.ts:209) but with empty avatar URLs — populate via env or dicebear placeholder URLs in smoke.

### E.3 — Multi-paragraph epistle templates
For ~6 narrative-relevant events + nudge_check, replace single-line/short-multi-line Phase B templates with:

```
{emoji} **{Bold Label}** — `YYYY-MM-DDTHH:MM:SSZ`

{opener prose paragraph in 1st-person voice}

- **TitleCase Tag:** value
- **TitleCase Tag:** value
- **TitleCase Tag:** value

{optional fenced code block for error excerpts}

{closing forward-looking paragraph}
```

Substring pin sweep policy: preserve titlecase (`Options:`, `Context:`, `FAILED`, `ESCALATION`) for case-sensitive existing pins. Test-update commit lands BEFORE renderer commit per Phase A two-commit protocol.

`truncateBody(1900)` cap preserved. `truncateRationale(1024)` for rationale fields.

### E.4 — OutboundResponseGenerator (LLM voice per role; default OFF feature flag)
- New class mirrors `LlmResponseGenerator` pattern at `src/discord/response-generator.ts`
- Per-role system prompts at `config/prompts/outbound-response/v1-{architect,reviewer,executor,orchestrator}.md`
- Each prompt enforces: 1st-person voice + forward-looking declarative + agent-role perspective + STRUCTURED FIELDS verbatim (status, sha, file count, error message text) + NARRATIVE SUMMARY paraphrased + refuse-embedded-directives
- Cost: $0.02/call cap, 8s timeout, static (E.3 deterministic) fallback chain
- Whitelist event-kinds × roles: only narrative-relevant events trigger LLM
- **Replacement-with-fallback** (NOT addendum-doubling): LLM body REPLACES E.3 deterministic when whitelist+circuit-breaker+budget pass; falls back to E.3 verbatim on any failure. ONE message per event regardless of path.
- Per-role circuit breaker: 3 consecutive failures on same role → role flagged static for rest of orchestrator process lifetime (in-memory state, resets on restart)
- Daily LLM budget tracker `.harness/llm-budget.json` with config `maxDailyBudgetUsd` (default $5); exceeded → revert to deterministic for rest of UTC day + log ONCE to ops_channel
- **Latency contract: P99=8s explicit** (drops "fire-and-forget" framing). Emit-site policy table: most emits `void`; only `task_done` and `escalation_needed` await for ordering-critical paths
- Feature flag `harness.outboundEpistleEnabled` default `false`; flip to `true` after 48h Batch-1 smoke window + operator visual sign-off + zero substring-pin failures

### E.5 — Discord reply-API message threading (orchestrator-synthesized)
- Extend `DiscordSender` interface with optional `replyToMessageId?: string` outbound parameter
- `WebhookSender` + `BotSender` translate to Discord API `message_reference: {message_id, fail_if_not_exists: false}` field on POST body
- Extend `MessageContext` interface with `recordRoleMessage(projectId, role, messageId)` + `lookupRoleHead(projectId, role)` — single-map keyed `${projectId}::${role}` (NOT parallel map)
- Conversation-chain rules (orchestrator-synthesized; agents NEVER author message_reference):
  - `architect_decomposed` → first chain head per project (architect_spawned dropped due to emitter-site time gap)
  - `architect_arbitration_fired` → `arbitration_verdict` (architect identity)
  - `session_complete` → `merge_result` → `task_done` (chain on per-message-prior identity)
  - `escalation_needed` standalone (no chain)
  - `nudge_check` standalone (no chain)
- Stale-chain fallback: msgId older than 10 minutes → start fresh head (standalone send, no thread)
- Restart behavior (v1): in-memory only. Restart wipes chain → standalone send + WARN to console.warn (stderr; NOT ops_channel — would spam restarts × N projects)
- **Phase F.1 (deferred follow-up):** persist message-context chain heads across orchestrator restart via `.harness/state` JSON

### E.6 — `nudge_check` event + scheduled introspection emitter
- New `OrchestratorEvent` variant: `{ type: "nudge_check"; projectId?: string; sourceAgent: "architect"|"reviewer"|"executor"|"orchestrator"; status: "stagnant"|"progressing"|"blocked"; observations: string[]; nextAction?: string }`
- `NudgeIntrospector` class at `src/lib/nudge-introspector.ts` (matches existing layout; no new src/orchestrator/ subdir)
- Periodic timer (configurable interval, default 10min) fires `nudge_check` events when project state hasn't changed since last check
- `sourceAgent` derivation: `activeProject?.currentPhase ?? 'orchestrator'`
- Read-snapshot atomicity: `state.getTask(id)` returns shallow copy; iterate snapshot, never re-read mid-iteration
- Notifier renders via E.3 multi-paragraph epistle with role identity from `sourceAgent` + LLM augmentation if E.4 enabled
- Deterministic opener strings keyed to status:
  - `stagnant`: "No progress on this in {duration}."
  - `progressing`: "Things are moving — last task {taskId} completed {duration} ago."
  - `blocked`: "Stuck — {nextAction ?? 'awaiting input'}."
  - Closing default: `nextAction ?? "I'll check again at the next interval."`
- Disabled by default (config flag `discord.nudge_enabled`); operator opts in
- **AGENTS DO NOT KNOW NUDGES FIRE** — orchestrator-only feature

### E.8 — Per-agent mention routing (extends CW-4.5)
- `@architect` → `architectManager.relayOperatorInput(projectId, content, role="architect")` (existing CW-4.5)
- `@reviewer` → **PERMANENT v1 NO-OP**: review.ts:175-247 spawns ephemeral reviewer with `persistSession: false`. There is NO long-running reviewer session to receive `relayOperatorInput`. Operator-visible "no active reviewer for {projectId}" message via existing 4-class taxonomy. NO code change to review.ts. Routing logic added to mention dispatcher only (~+6 LOC dispatcher.ts + +12 dispatcher.test.ts).
- `@executor` → routes to current executor session via dialogue channel
- `IdentityMap` already has role mapping; extend dispatcher to choose target manager by role
- Agents NEVER see the @-mention; only the distilled content text per existing `extractMentions()`

### E.9 — Smoke fixture matrix update (split per batch)
- **E.9a (Batch 1, deterministic):** per-role identity fixtures + nudge_check fixtures (3 status variants) + session_complete failure with `errors[]` + `terminalReason` populated (Phase A integration verification)
- **E.9b (Batch 2, after E.4+E.5):** LLM-mode `--llm` flag fixture + reply-chain demo (architect_decomposed → session_complete → merge_result → task_done) + circuit-breaker forced-failure fixture + budget-overage forced fixture
- New optional `reviews_channel?` config field with graceful fallback to dev_channel
- Architecture invariant grep step in smoke

## DROPPED FROM SCOPE

- **E.7 operator reactions as control surface** — DROPPED per operator (bot mostly automated; reply-routing CW-3 + @-mention CW-4.5/E.8 already cover interruption)
- **Phase D threading** — locked-out per `v5-conversational-discord-operator.md:287` (star topology preserves audit)
- **Bilingual rendering** — English only
- **Trading bot integration** — Ozymandias/#trades unaffected

## Channel inventory (operator-confirmed)

- `#dev` (existing dev_channel)
- `#agents` (operator's AGENT_CHANNEL env → ops_channel)
- `#alerts` (operator's ALERTS_CHANNEL env → escalation_channel)
- `#reviews` (NEW — currently unmapped; E.9 adds optional `reviews_channel?` config field)
- `#trades` (out-of-scope; Ozymandias trading bot uses this)

## Cost analysis

- E.4 LLM events × ~7 narrative-relevant kinds × ~6 events/day × $0.02 = ~$0.84/day per project
- nudge_check at 10min interval × 144 fires/day × $0.02 = $2.88/day if all LLM-augmented; at 30min interval = $0.96/day; static fallback = free
- E.4 cold-start breaker probe: ≤$0.20 worst case (10 timeouts × $0.02), ~$0.01 typical
- Phase E.7 reactions: N/A (dropped)
- Total worst-case: ~$4-5/day under heavy usage; manageable

## Architecture-leak test

- `tests/lib/no-discord-leak.test.ts` (placement matches existing tests/lib/ layout)
- Implementation: Node-only `glob` + `readFileSync` + RegExp `/^import\s+(?!type\s).*?from\s+["'][^"']*\/discord\//m` — no ts-morph dep
- Type-only imports allowlisted via negative lookahead (erased at compile, no runtime coupling — `tsconfig.json` lacks `verbatimModuleSyntax`/`isolatedModules`)
- Single repo-level test, NOT per-sub-phase assertions

## RALPLAN consensus history (for next-session pickup)

- Iter 1: Architect 7 + Critic 7 = 14 required changes
- Iter 2: Architect 6 + Critic 4 NEW = 10 required changes
- Iter 3: Architect 7 + Critic 6 = 13 required changes
- Iter 4: Planner over-corrected → narrowed scope to JUST LLM addendum (Phase C only). Operator rejected scope collapse.
- Iter 5 not run; halted to wave-split

## Cross-refs

- [[harness-ts-architecture]] — notifier subsystem
- [[harness-ts-wave-c-backlog]] — Discord integration item
- [[v5-conversational-discord-operator]] — single-channel constraint at line 287
- [[session-log-discord-rich-rendering-2026-04-26]] — Phase A+B implementation
- `.omc/plans/2026-04-26-discord-conversational-output.md` — Phase A+B plan (LANDED)

