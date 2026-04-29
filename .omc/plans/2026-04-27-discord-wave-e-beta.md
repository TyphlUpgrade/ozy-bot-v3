# Wave E-β — Discord Reply-API Threading (harness-ts)

**Status:** DRAFT (2026-04-27)
**Predecessor:** Wave E-α LANDED (commits `66801b0` / `5bec3dc` / `72a3ea0`).
**Successor:** Wave E-γ (`.omc/plans/2026-04-27-discord-wave-e-gamma.md`) — voice-per-role; lands cleaner once threading provides chain context.
**Wiki spec:** `.omc/wiki/phase-e-agent-perspective-discord-rendering-intended-features.md` § E.5.

---

## Goal

Synthesize Discord `message_reference` reply chains so multi-event flows render as visible threads instead of disconnected posts. Operator scrolling `ops_channel` sees Architect → arbitration_verdict → executor session → merge → done as one coherent conversation per project.

Orchestrator-synthesized only. Agents never author `message_reference`. Preserves I-1.

## Non-goals

- LLM-generated voice (E-γ scope)
- `nudge_check` event (E-δ scope)
- Per-role mention routing (E-δ scope; existing CW-4.5 architect-only mention untouched)
- Persistent chain-head storage across orchestrator restart (deferred to Phase F.1)
- Cross-channel threading (chain heads are per-channel by construction; reply only works in the same channel as the head)

---

## Why this before E-γ

1. **Zero ongoing cost.** No LLM, no $/day cap, no circuit breaker. Pure orchestrator memory + Discord API field.
2. **Black-box mitigation payoff.** Reply chains directly answer "what is this event a continuation of?" — biggest UX gap surfaced in operator audit concern (this session 2026-04-27).
3. **De-risks E-γ evaluation.** Voice-differentiation reads better when chained; reviewers can compare role voices in-thread instead of scrolling for matching project ids.

---

## Scope (Section by Section)

### B1 — `DiscordSender` interface extension

`harness-ts/src/discord/types.ts:45`. Add optional reply id to send signatures:

```ts
export interface DiscordSender {
  sendToChannel(
    channel: string,
    content: string,
    identity?: AgentIdentity,
    replyToMessageId?: string,
  ): Promise<void>;
  sendToChannelAndReturnId(
    channel: string,
    content: string,
    identity?: AgentIdentity,
    replyToMessageId?: string,
  ): Promise<{ messageId: string | null }>;
}
```

`sendToChannelAndReturnIdDefault` helper (types.ts:61) updated to forward `replyToMessageId`.

**Why optional:** I-3 additive only. All existing call sites compile unchanged.

### B2 — `WebhookSender` reply support

`harness-ts/src/discord/sender.ts`. Pass `message_reference: { message_id: replyToMessageId, fail_if_not_exists: false }` to underlying webhook payload when `replyToMessageId` set. `fail_if_not_exists: false` is critical — Discord drops the reply silently rather than rejecting if the head was deleted; preserves resolve-on-drop discipline.

### B3 — `BotSender` reply support

`harness-ts/src/discord/bot-sender.ts`. Same `message_reference` payload shape on REST POST body. Existing 429 retry-after handling unchanged.

### B4 — `MessageContext` extension

`harness-ts/src/discord/message-context.ts`. ADDITIVE methods on the existing interface:

```ts
export interface MessageContext {
  // existing
  recordAgentMessage(messageId: string, projectId: string): void;
  resolveProjectIdForMessage(messageId: string): string | null;
  // new
  recordRoleMessage(projectId: string, role: AgentRole, messageId: string, channel: string): void;
  lookupRoleHead(projectId: string, role: AgentRole, channel: string): string | null;
}
```

`AgentRole = "architect" | "executor" | "reviewer" | "orchestrator"` (already exists in `identity.ts`). New map keyed `${projectId}::${role}::${channel}` → `{ messageId, recordedAtMs }`. Same Map-LRU pattern as the existing message-id map (deletion + reinsertion, capacity-bounded).

**Channel in key:** Discord reply-API requires head and reply in same channel. Architect arbitration may span ops + escalation channels — keying by channel avoids cross-channel reply attempts that Discord would reject.

**Stale-chain TTL:** `lookupRoleHead` returns null when `now - recordedAtMs > 10 * 60_000` (10 min, hard-coded; configurable in Wave F.1 if operators ask). Stale → standalone send + new head registered.

`InMemoryMessageContext` implements both. Capacity bound shared with existing entries (single LRU; no separate budget).

### B5 — `DiscordNotifier` chain decisions

`harness-ts/src/discord/notifier.ts`. Per the wiki spec, the chain rules per event are:

| Event | Reply target | Chain head registration |
|-------|--------------|-------------------------|
| `project_decomposed` | none (chain head) | yes (architect role, ops_channel) |
| `architect_arbitration_fired` | none (chain head) | yes (architect role, escalation_channel) |
| `arbitration_verdict` | architect head (escalation_channel) | re-registers (latest verdict becomes new head) |
| `session_complete` | architect head (ops_channel) — chains executor turn under decomposed plan | yes (executor role, ops_channel) |
| `merge_result` | executor head (ops_channel) | yes (executor role) — re-registers |
| `task_done` | executor head (ops_channel) | yes (executor role) — re-registers |
| `review_mandatory` / `review_arbitration_entered` | executor head (ops_channel) | yes (reviewer role, ops_channel) |
| `escalation_needed` | none (standalone) | no |
| `task_picked_up` / `poll_tick` / `shutdown` / `retry_scheduled` / `budget_*` / `compaction_fired` | none (standalone) | no |
| `session_stalled` (Wave watchdog, just landed) | role-head matching `event.tier` if recorded | no |

Encoded as a table-driven `CHAIN_RULES` const next to `NOTIFIER_MAP`. NOT inlined into per-event branches — table extension point per project Modularity Philosophy.

### B6 — Chain-decision flow

In `DiscordNotifier.handleEvent`:
1. Resolve `projectId` (existing).
2. If event has chain rule and `projectId` defined:
   a. `replyToMessageId = ctx.lookupRoleHead(projectId, rule.replyToRole, channel)` (null if stale or absent → standalone).
   b. `await sendToChannelAndReturnId(channel, content, identity, replyToMessageId)`.
   c. If rule registers a new head AND messageId !== null: `ctx.recordRoleMessage(projectId, rule.registerRole, messageId, channel)`.
3. Else: standalone path unchanged.

### B7 — Restart behavior

`InMemoryMessageContext` is process-scoped. Orchestrator restart wipes the role map → next event in any chain becomes a new head. Documented behavior; warn once via `console.warn` on first cleared lookup post-restart (NOT ops_channel — restart-spam unacceptable per existing CW-3 design).

Persistent chain heads = Phase F.1 (out of scope here).

### B8 — Reactions stub unchanged

Wave E-β does NOT touch `NoopReactionClient` (CW-5). Reactions need authenticated REST; bot-login lane unrelated to threading. Document boundary in plan only.

---

## Files

| File | Change |
|------|--------|
| `src/discord/types.ts` | +`replyToMessageId?` on DiscordSender; helper update |
| `src/discord/sender.ts` | WebhookSender: emit `message_reference` |
| `src/discord/bot-sender.ts` | BotSender: emit `message_reference` |
| `src/discord/message-context.ts` | +`recordRoleMessage` / `lookupRoleHead` + TTL |
| `src/discord/notifier.ts` | `CHAIN_RULES` table + chain-decision flow in handleEvent |
| `src/lib/config.ts` | optional `[discord.reply_threading]` block (`enabled: bool`, default true; `stale_chain_ms: number`, default 600_000) |
| `tests/discord/message-context.test.ts` | +tests for role-head + TTL + LRU interaction |
| `tests/discord/sender.test.ts` | +reply payload assertion |
| `tests/discord/bot-sender.test.ts` | +reply payload assertion |
| `tests/discord/notifier.test.ts` | +chain-flow assertions for each rule row |
| `tests/discord/notifier-chain-fixtures.test.ts` (new) | end-to-end multi-event sequence: architect_decomposed → session_complete → merge_result → task_done. Verifies reply ids stitch correctly. |

Estimated: ~8 files changed, ~400 LOC + ~250 LOC tests.

---

## Acceptance criteria

- **AC1** Existing 778 tests still pass. No regressions.
- **AC2** New chain-fixture test: full project lifecycle event sequence produces 4 outbound messages where messages 2-4 carry `message_reference.message_id` matching message 1's id.
- **AC3** Stale-chain TTL: head older than 10 min → next event sends standalone, new head registered.
- **AC4** Cross-channel: chain head in `ops_channel`, event routed to `escalation_channel` → standalone send (no cross-channel reply attempt).
- **AC5** Restart simulation: `new InMemoryMessageContext()` after seeding then dropping → first lookup returns null + console.warn fires once per process.
- **AC6** Disabled flag (`discord.reply_threading.enabled = false`): chain decisions short-circuit; behavior identical to Wave E-α.
- **AC7** I-1 preserved: `tests/lib/no-discord-leak.test.ts` still passes. Agent sessions never see message ids.
- **AC8** I-3 preserved: all interface additions optional; KNOWN_KEYS untouched (no TaskRecord field added).
- **AC9** `npm run audit:epistle-pins` clean — substring pins unchanged (chain decisions don't alter rendered content).
- **AC10** Lint clean.

---

## Two-commit split (I-6)

**Commit 1 (mechanical):**
- DiscordSender signature + `sendToChannelAndReturnIdDefault` forward
- WebhookSender + BotSender accept `replyToMessageId` and emit `message_reference` (NOT yet used by notifier)
- MessageContext role-head methods + TTL (NOT yet called by notifier)
- Config block parsing
- Tests use `it.todo("notifier chain decisions — commit 2")` placeholders

**Commit 2 (behavior):**
- `CHAIN_RULES` table in notifier
- handleEvent chain-decision flow + record-on-success
- Un-skip `it.todo` placeholders + add chain-fixtures test
- Restart-warn console.warn

Single `git revert <commit2>` returns to commit-1 mechanical baseline cleanly.

---

## Risks

| Risk | Mitigation |
|------|------------|
| **R1** Discord rejects `message_reference` when head deleted | `fail_if_not_exists: false` — Discord renders without quote card, doesn't 4xx |
| **R2** Stale-TTL too aggressive — chains break for slow projects | 10 min default chosen vs typical phase duration (1-5 min). Configurable in B7. Operator escalates if too short. |
| **R3** Memory leak via unbounded role map | Shared LRU bound with existing message-id map (1000 entries default). Each project has ≤4 role heads → 250 projects supported simultaneously. |
| **R4** Chain visual clutter — every event becomes nested | Only ~6 events chain per spec table. Standalone events (poll_tick, shutdown, etc.) stay flat. |
| **R5** Cross-channel orchestration bug — head registered in wrong channel | Channel embedded in key; lookup with mismatched channel returns null. Tested AC4. |
| **R6** Restart wipes mid-project chain | v1 acceptable per spec; F.1 persistence deferred. Documented in B7. |

---

## Out of scope (explicit)

- Persistent chain-head storage (Phase F.1)
- Voice differentiation per role (Wave E-γ)
- `nudge_check` events (Wave E-δ)
- Per-role mention routing (Wave E-δ)
- Reactions (CW-5 / bot-login lane)
- Cross-channel chain semantics (heads are per-channel by construction)

---

## Estimated effort

- Commit 1 mechanical: ~3 hrs (signature plumbing + payload + map + tests)
- Commit 2 behavior: ~3 hrs (table + flow + chain-fixtures test)
- Verification: ~30 min (full suite + lint + audit + manual operator screenshot of chain in dev_channel)

Total: ~half a session.

---

## Cross-references

- `.omc/wiki/phase-e-agent-perspective-discord-rendering-intended-features.md` § E.5 — source spec
- `.omc/wiki/harness-ts-architecture-snapshot-2026-04-27-as-built.md` — current Discord layer state
- `.omc/wiki/harness-ts-core-invariants.md` — I-1, I-3, I-6, I-10
- `.omc/plans/2026-04-26-discord-wave-e-alpha.md` — predecessor (landed)
- `.omc/plans/2026-04-27-discord-wave-e-gamma.md` — successor (LLM voice)
