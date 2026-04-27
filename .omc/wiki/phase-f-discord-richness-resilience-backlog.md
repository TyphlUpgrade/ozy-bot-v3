---
title: "Phase F — Discord Richness & Resilience Backlog"
tags: ["phase-f", "discord", "harness-ts", "backlog", "richness-ceiling", "resilience"]
created: 2026-04-27T18:44:12.556Z
updated: 2026-04-27T18:44:12.556Z
sources: []
links: ["phase-e-agent-perspective-discord-rendering-intended-features.md", "harness-ts-architecture-snapshot-2026-04-27-as-built.md", "reference-screenshot-analysis-conversational-discord-operator-st.md", "harness-ts-core-invariants.md"]
category: architecture
confidence: medium
schemaVersion: 1
---

# Phase F — Discord Richness & Resilience Backlog

## Phase F — Discord Richness & Resilience Backlog

**Captured:** 2026-04-27 from session analyzing reference screenshot gap vs Phase E outcomes.
**Predecessor:** Phase E (α landed, β landed, γ landed, δ pending). See `[[phase-e-agent-perspective-discord-rendering-intended-features]]`.
**Source:** lead-engineer analysis of LLM interface, signal-file richness, and reference target gap.

### Why Phase F exists

Phase E delivers role identity, reply threading, LLM voice, and (pending δ) nudges. After E-α/β/γ ship, residual gap vs reference target falls into items that require structural changes outside Phase E scope. This page is the parking lot.

### Backlog items (priority order)

#### F.1 — Per-test status reporting in MergeGate

**Why:** Reference shows `cargo fmt ✅ / cargo clippy ✅ / cargo test ✅` per-step badges. Current `MergeGate` runs ONE `test_command` and reports binary pass/fail. Cannot render checkmark lists without per-step data.

**Approaches (pick one):**
- **F.1a:** Multi-command config — `test_commands: string[]` instead of `test_command: string`. Run sequentially, capture each `{cmd, exitCode, durationMs}`. Surface as `MergeResult.testSteps?: TestStep[]`. Renderer + LLM prompt produce checkmark list.
- **F.1b:** Test output parser — keep single command, parse stdout for per-test-case results (vitest/jest/cargo formats). More fragile, supports existing config.
- **Recommend F.1a** — explicit, framework-agnostic, no parser maintenance.

**Files:** `src/gates/merge.ts`, `src/discord/epistle-templates.ts`, `config/prompts/outbound-response/v2-orchestrator.md`.

#### F.2 — Persistent MessageContext across orchestrator restart

**Why:** E-β chains rebuild from next event after restart. Mid-flight chains lose continuity. Plan E.5 explicitly deferred persistence to "Phase F.1" (actually F.2 here per renumbering).

**Approach:** Persist `recordRoleMessage` map to `.harness/discord-message-context.json` on every write. Atomic temp+rename per O3 pattern. Constructor reads file at startup. TTL still applies on lookup.

**Risk:** stale heads after long downtime (operator restarts after weekend) reply to dead messages. TTL handles this — heads older than `stale_chain_ms` already return null.

**Files:** `src/discord/message-context.ts`, new tests.

#### F.3 — Persistent PerRoleCircuitBreaker with TTL half-open probes

**Why:** E-γ breaker is in-memory; tripped role stays degraded until restart. Plan E.4 explicit but operator pain point if model has transient outage during business hours.

**Approach:** Persist breaker state per role to `.harness/llm-breaker.json`. After N minutes (e.g. 30) since last failure, allow ONE probe call ("half-open"). On success, close breaker; on failure, re-open.

**Files:** `src/discord/llm-budget.ts` (co-located with breaker), new tests.

#### F.4 — Agent narrative hints (opt-in `currentlyDoing` / `nextAction` on signals)

**Why:** Nudges (E.6) are orchestrator-synthesized but operator richness benefits from agent-fresh narrative context when available. Cheap addition that piggybacks on existing signal emissions.

**Approach:** Add OPTIONAL fields to `CompletionSignal`, `EscalationSignal`, `ArchitectVerdict`:
- `currentlyDoing?: string` — what the agent thinks it's working on now
- `nextAction?: string` — what the agent plans to do next

Orchestrator caches most-recent value per role. NudgeIntrospector uses cached hint when present, falls back to derived state when absent.

**I-1 preserved:** agents don't know nudges fire; they just optionally annotate signals with narrative hints.

**Files:** `src/session/manager.ts`, `src/session/architect.ts`, `src/lib/escalation.ts`, schema docs in agent prompts.

#### F.5 — Per-tool stall detection (richer than stream-level)

**Why:** Wave watchdog (commit `2ae53c9`) detects SDK stream stall but cannot distinguish "model thinking deeply" from "Bash subprocess hung." Future false positives will erode trust.

**Approach:** Inspect SDK message types — if last message is `tool_use` for a long-running tool (Bash, WebFetch), use a longer threshold than for `assistant` text deltas. Per-tool timeout config.

**Files:** `src/session/sdk.ts` (extract tool name from last yielded message), `src/orchestrator.ts` (tier × tool threshold table).

#### F.6 — True bot-to-bot @-mention dialogue (vs render-time fictions)

**Why:** Reference shows `clawhip @gaebal-gajae` as actual @-mention. E-γ can render this as render-time fiction (orchestrator inserts "@reviewer" in synthesized prose), but operator-visible @-mentions could enable real bidirectional bot conversation if orchestrator routes Discord-side @-mentions back through `relayOperatorInput`-style routing.

**Approach:** Extend dispatcher precedence rules (CW-4.5) to detect bot-to-bot @-mentions and route to the appropriate manager's relayOperatorInput. NOT operator-initiated — orchestrator-internal routing.

**Risk:** convolutes the I-1 boundary if not carefully gated. Defer until concrete need surfaces.

**Files:** `src/discord/dispatcher.ts`, `src/discord/identity-map.ts`.

#### F.7 — Reactions support (depends on bot-login lane)

**Why:** CW-5 placeholder. Reference shows operator ❤️/👍/👀 reactions as approval/dismissal signals.

**Blocker:** Requires authenticated REST client (bot login), not webhook-based. Separate lane in CW-5 plan.

**Files:** `src/discord/client.ts` (`NoopReactionClient` → real impl), bot-gateway integration.

#### F.8 — Per-event-type validation in OutboundResponseGenerator

**Why:** Current `validateOutput` is near-no-op for 8/9 whitelist tuples. Could ship LLM gibberish on weak prompts. (Documented in lead-eng E-γ analysis.)

**Approach:** Per-event required-substring table:
- `merge_result` → sha7 (already done)
- `task_done` → task id
- `session_complete` → task id + status literal
- `arbitration_verdict` → task id + verdict literal
- etc.

**Files:** `src/discord/outbound-response-generator.ts`.

**Note:** could ship as part of E-γ tightening (Wave E-γ-2) instead of waiting for Phase F. Decide based on observed quality after SDK overhead fix.

#### F.9 — Bilingual rendering

OUT OF SCOPE per all prior plans. Documented for completeness.

#### F.10 — Sender queue extraction

**Why:** ~140 LOC of bounded-FIFO + drain + overflow logic is duplicated between `WebhookSender` (`src/discord/sender.ts`) and `BotSender` (`src/discord/bot-sender.ts`). The two queues differ only in the transmit primitive (webhook POST vs bot REST) and BotSender's extra 429 retry path. Real maintenance smell — flagged in 2026-04-27 review.

**Approach:** Extract a generic `RateLimitedQueue<T>` (in `src/discord/rate-limited-queue.ts`) with:
- bounded FIFO + min-spacing token bucket
- pluggable `transmit(item: T): Promise<R>` callback
- pluggable retry/backoff hook (BotSender supplies the 429 handler; WebhookSender passes a no-op)
- overflow callback (resolve-on-drop semantics, log a warning)

Both senders construct the queue with their own callbacks; the queue owns scheduling state. Estimated ~140 LOC saved + single source of truth for backpressure invariants.

**Risk:** non-trivial refactor. Comprehensive parity tests required (queue ordering, drain timing, overflow resolution, 429 retry path). Estimated ~3 hours including tests.

**Files:** `src/discord/rate-limited-queue.ts` (new), `src/discord/sender.ts`, `src/discord/bot-sender.ts`, new tests in `tests/discord/rate-limited-queue.test.ts`.

**Decision:** deferred from 2026-04-27 review pass per scope bound — original review marked HIGH but cost-of-change exceeded the review cycle's budget. Tracking comment lives at the top of `sender.ts`.

### Cross-references

- `[[harness-ts-architecture-snapshot-2026-04-27-as-built]]` — current as-built map
- `[[phase-e-agent-perspective-discord-rendering-intended-features]]` — Phase E intent
- `[[reference-screenshot-analysis-conversational-discord-operator-st]]` — reference target gap analysis
- `[[harness-ts-core-invariants]]` — I-1, I-3, I-6 governance
- `.omc/plans/2026-04-27-discord-wave-e-beta.md`
- `.omc/plans/2026-04-27-discord-wave-e-gamma.md`

### Open question for prioritization

Should **F.8** (per-event validation) ship as part of Phase E tightening rather than wait for F? Argument for E: small, defensive, addresses a quality risk surfaced in current implementation. Argument for F: clean phase boundary; E-γ already shipping; defer to later iteration cycle.

Recommendation: F.8 in E if SDK fix exposes shipping rate >50% LLM, else hold for F.
