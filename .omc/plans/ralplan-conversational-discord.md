# RALPLAN — Conversational Discord Interface (v2)

**Date:** 2026-04-24
**Scope:** `harness-ts/` — Discord conversational layer (live runs)
**Mode:** SHORT consensus (no `--deliberate` flag set; pre-mortem + test plan still included per task spec)
**Status:** Iteration 2 — Architect + Critic feedback addressed

---

## Iteration 2 — Architect + Critic feedback addressed

This revision applies every Architect (V1–V4 + cross-wave) and Critic finding from the v1 review pass. Numbered changes below map to the originating review items.

| # | Source            | Change                                                                                                                                                                                       |
|---|-------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1 | Architect V1      | **Dropped** breaking return-type change to `DiscordSender.sendToChannel`. Added separate method `sendToChannelAndReturnId(channel, content, identity?): Promise<{messageId: string \| null}>` to the interface, with a default implementation: existing senders inherit a base behavior of "delegate to `sendToChannel`, return `{messageId: null}`". Notifier uses the new method only when MessageContext recording is needed. |
| 2 | Architect V2      | Dispatcher uses `senders: Record<string, DiscordSender>` map from the start. Rule-3 graceful-error reply goes to `senders[config.escalation_channel]`; rule-5 default uses `senders[msg.channelId]`. The `escalationSender` shorthand is gone everywhere. Sketch fully rewritten. |
| 3 | Architect V3 (CRIT) | `NOTIFIER_MAP` task-keyed events do not carry `projectId`. Notifier receives an optional `StateManager` to resolve `projectId = state.getTask(taskId)?.projectId`. If undefined (bare task, no project linkage), `MessageContext.recordAgentMessage` is skipped. New explicit step in CW-3. |
| 4 | Architect V4      | `BotGateway` exposes `fetchReferenceUsername(messageId, channelId): Promise<string \| null>` test seam. Test fakes return cached values. The cache-miss path is documented as a TODO with explicit fallback to "rule-4 fall-through" (no live API call inside dispatcher in CW-3; deferred to CW-3.5). |
| 5 | Architect cross-w | Bootstrap fails fatally if any provisioned channel is missing a webhook ID. Replaces the `.filter(Boolean)` shortcut with explicit per-channel validation; prints missing channel name + remediation hint. |
| 6 | Architect cross-w | `MessageContext` SPOF mitigation: at startup, send a one-shot notice to `ops_channel` ("harness-ts started; conversational state lost across restarts; please re-issue commands as `!project ...` or via reply once a new agent message lands"). Added to `live-bot-listen.ts` bootstrap. |
| 7 | Architect cross-w | `DiscordNotifierOptions.messageContext` is **optional**. The 4 non-bot scripts (`live-project*.ts`) pass nothing; reply-routing degrades gracefully. Only `live-bot-listen.ts` passes a real `MessageContext`. |
| 8 | Architect / matrix | **Gateway choice flipped.** `RawWsBotGateway` is now PRIMARY; `DiscordJsBotGateway` is documented as the FALLBACK escape hatch. Rationale: keeps codebase consistent with `BotSender`/`WebhookSender` raw-fetch transport; ~150 LOC for a focused WS impl (verified with skeleton sketch in this plan); avoids 25 MB dep + transitive surface; mocking `WebSocket` is more portable than mocking `discord.js Client`. |
| 9 | Architect cross-w | Dropped the "most recently declared" active-project heuristic entirely. Project resolution is **only** via `MessageContext.resolveProjectIdForMessage(messageId)`. Null → rule-2b error. Aligns with Principle 1. |
| 10 | Architect cross-w | `BotGateway` callbacks no longer use `any` or `discord.js Message`. Internal `RawMessage` shape declared explicitly: `{messageId, channelId, authorId, authorUsername, isWebhook, content, repliedToMessageId?, repliedToAuthorUsername?, timestamp}`. Mapping into `InboundMessage` is explicit. |
| 11 | Critic            | Decision matrix re-balanced — verified raw-WS skeleton is ~150 LOC (cite IDENTIFY+heartbeat+RESUME+sequence-ack); `referenced_message` is included in `MESSAGE_CREATE` payload (no extra REST fetch needed for the common path). Real tradeoff stated as "operator velocity vs dep weight." |
| 12 | Critic            | Test count math reconciled. Per-file totals enumerated; overlaps listed. Net new across CW-1+CW-2+CW-3 = 50 unit + 3 integration = 53 (was 47 with overlaps absorbed). |
| 13 | Critic            | `MESSAGE_CONTENT` intent missing-detection sentinel: gateway tracks first 10 inbounds per channel; if all 10 have empty `content`, log WARN and emit a one-time ops-channel notice "MESSAGE_CONTENT intent missing — enable in Discord Developer Portal." |
| 14 | Critic            | `extractWebhookIdFrom` defined: regex `/\/api\/webhooks\/(\d+)\//` extracts the webhook snowflake; `null` on miss. Unit-tested. |
| 15 | Critic            | `relayOperatorInput` failure modes split into 3 classes (no session / session terminated / queue full / generic) with distinct operator-visible messages in rule-3. Routing keyed on error message string match (cheap) with planned upgrade to typed errors in CW-3.5. |
| 16 | Critic            | Rule-3 channel ambiguity resolved: error reply lands in **the channel where the operator's reply was sent** (`msg.channelId`), so the response appears in conversational context. Rule-3 entry updated. |
| 17 | Critic            | Race-window during `sendToChannel` drain documented: operator may reply before `MessageContext` records the agent message. Result: rule-2 lookup misses → falls through to rule-4 → CommandRouter NL parse. Listed as known limitation; worst case is the operator's reply being parsed as NL (idempotent for unsupported intents). |
| 18 | Critic            | Live-API isolation explicit: `bot-gateway.test.ts` mocks `WebSocket` (no `discord.js`), `sender-factory.test.ts` mocks `fetch`, `dispatcher.test.ts` mocks `BotGateway`/`CommandRouter`/`ArchitectManager` — zero tests issue real Discord traffic. |

---

## Context

The harness already ships outbound Discord delivery (BotSender + WebhookSender + DiscordNotifier + CommandRouter) and Architect-side operator relay (`relayOperatorInput`). What is missing is the conversational loop:

1. Per-agent **avatars** in production (BotSender uses text prefix; WebhookSender supports avatars but lacks per-channel webhook URLs in live config + provisioning script).
2. **Inbound** message ingestion — the bot has no Gateway/WebSocket connection, so operator messages typed in Discord channels never reach the harness.
3. **Reply routing** — Discord's "Reply" UI sets `message_reference.message_id` on the new message; nothing reads this, so threaded replies cannot route to the right Architect session.

End state: operator types `start project X` (or replies to a specific Architect message) → Gateway sees the inbound → dispatcher chooses CommandRouter (NL/!) vs `relayOperatorInput(projectId, msg)` (reply) → conversational back-and-forth with rich per-agent identities.

---

## Principles

1. **Reply-target intent dominates literal text.** A reply to Architect saying "start project X" is operator dialogue, not a new project declaration. Reply path runs before `CommandRouter`.
2. **Single source of truth for identity ↔ webhook ↔ avatar.** The username→agent map derives from `DiscordConfig.agents`. No second registry.
3. **Webhook URLs are runtime config, not source.** Provisioned once via a script, stored in env vars, surfaced through `DiscordConfig.webhooks`.
4. **Outbound resilience already exists; inbound must match it.** Gateway disconnects, Architect-no-session, and unknown reply targets must log + continue, never crash.
5. **Webhook-or-bot is a per-channel choice, not a global one.** Channels with provisioned webhooks get rich avatars (WebhookSender); channels without fall back to BotSender.
6. **Project identity is explicit, not heuristic.** No "most recently declared" guessing. `MessageContext` is the only source. Null → graceful error reply, never a silent fallback.

## Decision Drivers

1. **Time-to-first-conversational-loop.** Operator can start a project from Discord and reply to an agent's message via one runnable script (`live-bot-listen.ts`).
2. **Test fidelity without live API.** Gateway + reply routing unit-testable with mocked WS + fetch.
3. **No regression to 583 passing tests, lint, or build.** Reply routing inserts a precedence step before CommandRouter; existing CommandRouter behavior unchanged when no reply context.

---

## Decision Matrix — Gateway Implementation (rebalanced)

| Dimension                         | **Option A: Raw WebSocket (`ws` package) — PRIMARY**                                                            | Option B: `discord.js` — Fallback                                                          |
|-----------------------------------|------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| Dep cost (install size)           | ~1 MB (`ws` + `@types/ws`)                                                                                       | ~25 MB; pulls `@discordjs/*`, `tweetnacl`, optional `zlib-sync`                            |
| Verified LOC for Gateway impl     | **~150 LOC** (skeleton sketched below; IDENTIFY+heartbeat+RESUME+sequence-ack)                                   | ~30 LOC client init + delegation                                                           |
| Reconnect / resume                | Hand-rolled. Op codes 0/1/7/9/10/11 covered in skeleton. Resume across `IDENTIFY`/`RESUME` is ~20 LOC.           | Built-in, battle-tested.                                                                   |
| Bot self-filter                   | Inspect `author.bot` ourselves (one boolean access)                                                              | `msg.author.bot`                                                                            |
| Webhook-author detection          | Read `webhook_id` from raw payload (`d.webhook_id`)                                                              | `msg.webhookId`                                                                             |
| `message_reference` access        | **`d.referenced_message`** is included in `MESSAGE_CREATE` for replies — no REST fetch needed common-path        | `msg.reference?.messageId` + optional `msg.fetchReference()`                                |
| TypeScript types                  | Hand-typed minimal `MessageCreatePayload` (~40 LOC types — explicit, auditable)                                  | First-class via `@types/discord.js`                                                         |
| Test surface                      | Mock `WebSocket` send/receive + heartbeat scheduler. Protocol-bound (a feature: pins exact wire behavior)        | Mock high-level `Client` (more abstract; less protocol coverage)                            |
| Test mock portability             | Standard `WebSocket` interface — portable across Node, Deno, browser-mocked envs                                 | discord.js `Client` mocks couple tests to discord.js internals                              |
| Risk: Discord protocol changes    | We absorb breakage (op-code shifts are rare; gateway v10 stable for 2+ years)                                    | Tracked by upstream — but upstream cadence is its own dep-update cost                       |
| Risk: dependency creep            | Minimal (`ws` is widely used, slow-moving)                                                                       | Heavy transitive tree (security surface)                                                    |
| Existing harness style            | **Matches `BotSender` + `WebhookSender` raw-fetch + minimal-deps pattern**                                       | Stylistic outlier — only place we'd use a high-level SDK                                    |

**Decision: Raw WebSocket primary; discord.js documented as fallback.**

The real tradeoff is **operator velocity (discord.js) vs dep weight + style consistency (raw WS)**. Critic flagged that the v1 matrix biased toward discord.js by overstating raw-WS LOC and understating the `referenced_message` already-in-payload fact. With those corrected, raw WS wins on:

- Dep weight + transitive security surface (~25 MB → ~1 MB; 50+ transitive packages → ~3).
- Style consistency with existing `BotSender`/`WebhookSender`.
- `referenced_message` in `MESSAGE_CREATE` payload removes the only protocol-level reason to reach for a high-level SDK.
- Test mocks pin wire behavior (operator testing the actual contract, not a third-party adapter).

`BotGateway` interface is implementation-agnostic so swapping is a one-file change if raw-WS proves too brittle in CW-2.

### Raw WS Skeleton (LOC budget verification)

```
src/discord/bot-gateway.ts                  (~150 LOC)
├─ types: RawMessage, GatewayPayload, ReadyEvent     ~25
├─ class RawWsBotGateway                              ~85
│   ├─ ctor(opts)                                      5
│   ├─ start() / stop()                               15
│   ├─ identify() / resume()                          20
│   ├─ heartbeat scheduler (interval-based)           20
│   ├─ onMessage(payload) — op-code dispatch          15
│   └─ filter + emit handler                          10
├─ MESSAGE_CONTENT sentinel                           ~15
└─ helpers (extractWebhookIdFrom, etc.)               ~10
                                              total ~135-150
```

This is a budget; CW-2 acceptance verifies actual LOC ≤ 200 (10% overrun acceptable). If actual exceeds 250, fall back to discord.js.

---

## Architecture: Single Source of Truth — Username → Agent

Reverse map is **derived**, not stored:

```ts
// src/discord/identity-map.ts (new)
export interface IdentityMap {
  resolveAgentByUsername(username: string): { agentKey: string; identity: DiscordAgentIdentity } | null;
}

export function buildIdentityMap(agents: Record<string, DiscordAgentIdentity>): IdentityMap {
  // Build once at construction. Lowercase, trimmed keys to match Discord's
  // case-insensitive username display. Conflicts (two agents with same name)
  // throw at construction time — fail-fast over silent shadowing.
  const byName = new Map<string, { agentKey: string; identity: DiscordAgentIdentity }>();
  for (const [agentKey, identity] of Object.entries(agents)) {
    const norm = identity.name.trim().toLowerCase();
    if (byName.has(norm)) {
      throw new Error(`identity-map: duplicate username "${identity.name}" (agents: ${byName.get(norm)!.agentKey} vs ${agentKey})`);
    }
    byName.set(norm, { agentKey, identity });
  }
  return {
    resolveAgentByUsername(username) {
      return byName.get(username.trim().toLowerCase()) ?? null;
    },
  };
}
```

This is the **only** username→agent resolver in the codebase. Dispatcher and gateway both consume `IdentityMap`. No hardcoded "if author is Architect" branches anywhere.

---

## DiscordSender Interface Evolution (Architect V1 fix)

The v1 plan's breaking return-type change is replaced by a non-breaking additive method.

```ts
// src/discord/types.ts (UPDATED — additive only)

export interface DiscordSender {
  sendToChannel(channel: string, content: string, identity?: AgentIdentity): Promise<void>;
  /**
   * Send and return the resulting Discord message ID. Default implementation
   * (provided in BaseSender mixin or via `sendToChannelAndReturnIdDefault` helper)
   * delegates to `sendToChannel` and returns `{ messageId: null }`. Implementations
   * that can capture the returned ID (BotSender via REST POST response, WebhookSender
   * via discord.js webhook send response, or both via fetch JSON parse) override
   * this to return the real ID.
   *
   * Callers that DO NOT need the message ID (DiscordNotifier when no MessageContext
   * is configured, all 4 live-project*.ts scripts) keep using `sendToChannel` —
   * unchanged contract. Only `live-bot-listen.ts` consumes the new method.
   */
  sendToChannelAndReturnId(
    channel: string,
    content: string,
    identity?: AgentIdentity,
  ): Promise<{ messageId: string | null }>;
  addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
}

// Helper for implementations that don't capture IDs:
export async function sendToChannelAndReturnIdDefault(
  sender: Pick<DiscordSender, "sendToChannel">,
  channel: string,
  content: string,
  identity?: AgentIdentity,
): Promise<{ messageId: string | null }> {
  await sender.sendToChannel(channel, content, identity);
  return { messageId: null };
}
```

`BotSender` and `WebhookSender` implement `sendToChannelAndReturnId` natively (extracting `id` from REST response / webhook send response). All test fakes get the helper as a one-line addition: `async sendToChannelAndReturnId(...args) { return sendToChannelAndReturnIdDefault(this, ...args); }`. **No fake breaks.**

---

## Reply Routing — Precedence Table

Decision tree in dispatcher; first match wins.

| #  | Condition                                                                                                                                            | Action                                                                                                                  | Rationale                                                                                                                                       |
|----|------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------|
| 0  | `authorId === ourBotId`                                                                                                                              | Drop (in gateway)                                                                                                       | Self-message from bot REST.                                                                                                                     |
| 0a | `webhookId` in `selfWebhookIds` set                                                                                                                  | Drop (in gateway)                                                                                                       | Self-message from our webhooks (Architect avatar etc.). Critical — prevents loop.                                                                |
| 0b | `isBot === true` AND not our webhook AND not our bot ID                                                                                              | Drop (in gateway)                                                                                                       | Other bots; future allowlist.                                                                                                                   |
| 1  | `channelId` not in `{dev_channel, ops_channel, escalation_channel}`                                                                                  | Drop (in gateway)                                                                                                       | Channel allowlist (parity with `CommandRouter.channelAllowed`).                                                                                 |
| 2a | `repliedToMessageId` set AND `messageContext.resolveProjectIdForMessage(repliedToMessageId)` returns projectId AND `repliedToAuthorUsername` resolves to an agent | `architectManager.relayOperatorInput(projectId, content)`; on success no further action                                 | **Reply to agent + known project dominates.** Operator's literal text irrelevant; intent is "speak to that agent in that project's session."   |
| 2b | `repliedToMessageId` set AND `repliedToAuthorUsername` resolves to an agent BUT `messageContext` returns null for the message ID                     | Send error reply to **`msg.channelId`** (Critic 16): "I recognized this as a reply to {Agent}, but I have no record of that message — it predates this bot session. Re-issue your command directly." | Restart-induced cache miss; visible to operator in conversational context.                                                                      |
| 3  | `repliedToMessageId` set AND project found AND agent resolved AND `relayOperatorInput` throws                                                        | Classify error (Critic 15): **no session / session terminated / queue full / generic**. Each maps to a distinct error reply sent to **`msg.channelId`** (Critic 16). | "No live agent" is a real state. Different failure modes warrant different operator actions (re-declare, retry, wait).                          |
| 4  | `repliedToMessageId` set BUT `repliedToAuthorUsername` does NOT resolve to a known agent                                                             | Fall through to rule 5                                                                                                  | Reply to non-agent (e.g. operator-to-operator) → treat as normal NL.                                                                            |
| 5  | Default                                                                                                                                              | `commandRouter.handleNaturalLanguage` (or `handleCommand` if leading `!`); reply via `senders[msg.channelId]` (Architect V2) | Existing CommandRouter behavior preserved.                                                                                                      |

**Race window (Critic 17):** if operator replies to an agent message before `DiscordNotifier` has finished `recordAgentMessage` (sender drain queue can delay 2+ s), the lookup misses → falls through to rule 4 → CommandRouter NL parse. **Worst case:** operator's reply parsed as `unknown` intent, returning a confused error. **Documented as known limitation;** mitigation is the 2-second sender drain + the operator's typing latency typically exceeding it. Persistent `MessageContext` would close this; CW-4 follow-up.

**Rule-3 error classification (Critic 15):**

| Failure                            | Detection (CW-3.5 will type these)                                            | Operator-visible message                                                                       |
|------------------------------------|--------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| `no_session` (no Architect for projectId) | Error message starts with "No Architect session for"                          | "Project `<id>` has no live Architect session — it may have completed or been aborted. Use `!project <id> status`." |
| `session_terminated` (SDK returned, was abort/crash) | Error message contains "session terminated" or "aborted"                      | "Architect session for `<id>` was terminated. Re-issue via `!project <name>` to spawn a new one." |
| `queue_full` (sender backed up; not Architect-side but observable here) | `WebhookSender`/`BotSender` warn-log captured                                  | "Discord send queue is full — your reply was dropped. Try again in 30 seconds." |
| `generic`                          | Any other thrown Error                                                         | "Reply to `<id>` failed: {sanitized error message, max 200 chars}."                            |

CW-3 ships with string-match dispatch and `// TODO(CW-3.5): replace with typed RelayError class` comments. CW-3.5 introduces `class RelayError extends Error { kind: "no_session" | "session_terminated" | "queue_full" | "generic" }` — non-blocking improvement.

---

## Waves

### CW-1 — Webhook provisioning + sender swap

**Goal:** Per-agent avatars in `live-project*.ts` and `live-discord-smoke.ts`. Operator can visually distinguish Architect / Reviewer / Executor / Operator messages.

**Files:**

| File                                                  | Status   | Approx LOC | Purpose                                                                                                  |
|-------------------------------------------------------|----------|------------|----------------------------------------------------------------------------------------------------------|
| `scripts/provision-webhooks.ts`                       | NEW      | ~80        | One-shot CLI: bot token + channel IDs from env, POST `/channels/{id}/webhooks`, GET-then-reuse idempotent. Prints env-var-format URLs + webhook IDs to stdout. |
| `src/lib/config.ts`                                   | MODIFIED | +30        | Add `DiscordConfig.webhooks?: { dev?: string; ops?: string; escalation?: string }` and parser.           |
| `src/discord/identity-map.ts`                         | NEW      | ~40        | `buildIdentityMap` — single source of truth. Conflict-checked at construction.                          |
| `src/discord/sender-factory.ts`                       | NEW      | ~70        | `buildSendersForChannels(config, token)` returns `Record<channelId, DiscordSender>`. Webhook URL → WebhookSender; absent → BotSender. Includes `extractWebhookIdFrom` helper (Critic 14). |
| `src/discord/bot-sender.ts`                           | MODIFIED | +20        | Implement `sendToChannelAndReturnId` natively (extract `id` from REST POST JSON response).               |
| `src/discord/sender.ts` (WebhookSender)               | MODIFIED | +15        | Implement `sendToChannelAndReturnId` — discord.js webhook `send()` returns a `Message`; capture `.id`. (If WebhookClient interface lacks ID return, use `wait: true` query param semantics.) |
| `src/discord/notifier.ts`                             | MODIFIED | +25        | Accept `senders: DiscordSender \| Record<string, DiscordSender>` (back-compat). Per-channel routing. Optional `messageContext` and optional `stateManager` deps for projectId resolution on task-keyed events (Architect V3). |
| `scripts/live-discord-smoke.ts`                       | MODIFIED | +20        | Use `buildSendersForChannels`; documents env-var setup.                                                  |
| `scripts/live-project.ts`, `live-project-3phase.ts`, `live-project-arbitration.ts`, `live-project-architect-crash.ts`, `live-project-mass-phase.ts`, `live-concurrent.ts` | MODIFIED | +5 each    | Replace direct sender construction with `buildSendersForChannels`. **No `messageContext` passed** — Architect V7. |
| `src/lib/config.ts` (DISCORD_AGENT_DEFAULTS)          | MODIFIED | +10        | Add `executor` and `operator` defaults (currently only orchestrator/architect/reviewer).                |
| `tests/discord/identity-map.test.ts`                  | NEW      | ~80        | Conflict detection, case-insensitive lookup, miss returns null, empty map, whitespace trim.              |
| `tests/discord/sender-factory.test.ts`                | NEW      | ~110       | Per-channel selection (uses **mocked fetch**, Critic 18). Includes `extractWebhookIdFrom` parsing tests. |
| `tests/discord/bot-sender.test.ts` (modified)         | MODIFIED | +20        | Cover `sendToChannelAndReturnId` ID-extraction.                                                          |
| `tests/discord/sender.test.ts` (modified)             | MODIFIED | +15        | Cover `sendToChannelAndReturnId`.                                                                        |
| `tests/lib/config.test.ts`                            | MODIFIED | +30        | Cover new `webhooks` section parsing, optional fields.                                                   |
| **Existing test fakes** (~6 files)                    | MODIFIED | +1 each    | Add one-line `sendToChannelAndReturnId` via `sendToChannelAndReturnIdDefault` helper. Mechanical.        |

**Acceptance commands:**

```bash
cd /home/typhlupgrade/.local/share/ozy-bot-v3/harness-ts
npm run lint && npm test && npm run build
# Expected: 583 + 18 new = 601 passing.

# Live provisioning (idempotent, GET-then-reuse):
set -a && source ../.env && set +a
npx tsx scripts/provision-webhooks.ts
# Prints (env-var format with webhook IDs):
#   DISCORD_WEBHOOK_DEV=https://discord.com/api/webhooks/<id>/<token>
#   DISCORD_WEBHOOK_DEV_ID=<id>
#   DISCORD_WEBHOOK_OPS=...
#   DISCORD_WEBHOOK_OPS_ID=...
#   ... etc.

# Smoke test → distinct avatars:
DISCORD_WEBHOOK_DEV=... DISCORD_WEBHOOK_OPS=... DISCORD_WEBHOOK_ESCALATION=... \
  npx tsx scripts/live-discord-smoke.ts
# Expected: 4 messages with distinct usernames AND avatars.
```

**Test count delta CW-1:** +18 (identity-map: 6, sender-factory: 7, bot-sender extension: 2, sender extension: 2, config: 1).

---

### CW-2 — Inbound bot gateway (Raw WS PRIMARY)

**Goal:** Bot listens on Discord Gateway via raw WebSocket, emits typed `InboundMessage` events for messages in configured channels after self-filtering. No routing logic — that's CW-3.

**Files:**

| File                                          | Status   | Approx LOC | Purpose                                                                                                  |
|-----------------------------------------------|----------|------------|----------------------------------------------------------------------------------------------------------|
| `package.json`                                | MODIFIED | +2         | Add `ws` + `@types/ws` deps. (Fallback: `discord.js` if budget overrun documented in CW-2 retro.)        |
| `src/discord/bot-gateway.ts`                  | NEW      | ~180       | `RawWsBotGateway` implementing `BotGateway`. IDENTIFY+heartbeat+RESUME, op-code 0/1/7/9/10/11. Filters self-id, self-webhook, other-bots, channel allowlist. |
| `src/discord/types.ts`                        | MODIFIED | +30        | Add `RawMessage`, `InboundMessage`, `BotGateway` interface, `MESSAGE_CONTENT` sentinel callback. |
| `tests/discord/bot-gateway.test.ts`           | NEW      | ~280       | Mock `WebSocket` (Critic 18). All filter cases + protocol skeleton + resume + sentinel detection.        |

**`BotGateway` interface (testable seam, implementation-agnostic):**

```ts
// src/discord/types.ts (additions)

/** Internal raw-decoded payload from Gateway op-code 0 MESSAGE_CREATE. No `any`. */
export interface RawMessage {
  messageId: string;
  channelId: string;
  authorId: string;
  authorUsername: string;
  isBot: boolean;
  webhookId: string | null;          // if message authored by webhook
  content: string;                    // empty string if MESSAGE_CONTENT intent disabled
  repliedToMessageId: string | null;  // from message_reference.message_id
  repliedToAuthorUsername: string | null;  // from referenced_message.author.username (NEW: included in MESSAGE_CREATE payload)
  timestamp: string;                  // ISO 8601
}

/** Stable public shape consumed by the dispatcher. */
export type InboundMessage = RawMessage;  // currently 1:1; reserve type for future divergence

export interface BotGateway {
  start(): Promise<void>;
  stop(): Promise<void>;
  on(handler: (msg: InboundMessage) => void): void;
  /** Self-filter — set BEFORE start(). Throws if called twice. */
  registerSelfWebhookIds(ids: string[]): void;
  /**
   * Test seam (Architect V4): resolve a message ID's author username via
   * gateway-side cache. Returns null on miss. Cache-miss path is a TODO with
   * explicit "rule-4 fall-through" fallback in dispatcher; CW-3.5 may add a
   * REST GET path. Live API NEVER called from this method in CW-3.
   */
  fetchReferenceUsername(messageId: string, channelId: string): Promise<string | null>;
  /**
   * Sentinel hook (Critic 13): fired exactly once if MESSAGE_CONTENT intent
   * appears disabled. Bootstrap wires this to send an ops-channel notice.
   */
  onMessageContentMissing(handler: () => void): void;
}
```

**`RawWsBotGateway` behavioral spec (full impl ≤200 LOC target):**

- **Constants:** `GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"`. Op codes 0/1/2/6/7/9/10/11. Intents bitmask: GUILDS (1<<0) | GUILD_MESSAGES (1<<9) | MESSAGE_CONTENT (1<<15, privileged).
- **State (instance fields):** `ws`, `sessionId`, `resumeUrl`, `lastSeq`, `heartbeatTimer`, `selfWebhookIds` (null until registered), `selfBotId`, `handlers[]`, `contentMissingHandler`, `contentMissingFired`, `emptyContentSampleByChannel`, `referenceCache: Map<messageId, username>` (Architect V4 backing store).
- **`start()`:** open WS via injected `webSocketFactory` (test seam) or default `new WebSocket(GATEWAY_URL)`. Wire `onmessage`/`onclose`/`onerror`.
- **`stop()`:** clear heartbeat timer, `ws.close(1000)`.
- **`registerSelfWebhookIds(ids)`:** throws if `selfWebhookIds !== null` (defense against double-register). Sets the readonly Set.
- **`fetchReferenceUsername(messageId)`:** returns `referenceCache.get(messageId) ?? null`. Cache populated by every `MESSAGE_CREATE` with a `referenced_message`. No live API call.
- **`onMessage` op-code dispatch:**
  - `HELLO (10)` → start heartbeat at advertised interval; if `sessionId` set, `RESUME`; else `IDENTIFY`.
  - `HEARTBEAT_ACK (11)` → tracked for zombie detection (CW-2.1 follow-up).
  - `RECONNECT (7)` / `INVALID_SESSION (9)` → reset session state, close 4000, reconnect via backoff.
  - `DISPATCH (0)` → `onDispatch(t, d)`.
- **`onDispatch`:** `READY` captures `session_id`/`resume_gateway_url`/`user.id` (becomes `selfBotId`). `MESSAGE_CREATE` decodes raw payload, caches `referenced_message.author.username`, runs filter chain (rules 0/0a/0b/1), runs MESSAGE_CONTENT sentinel, then fans out to handlers in a try/catch (handler exceptions never propagate).
- **`checkMessageContentSentinel`:** per-channel counter. Empty content increments; non-empty resets. At count==10, fires `contentMissingHandler` once, sets `contentMissingFired=true` (latch). Critic-3 follow-up: optionally exclude reply-only messages (5 LOC, deferred).
- **`onClose(code)`:** classify per Discord spec. 4004 (auth fail) / 4014 (disallowed intents) → `process.exit(2)` with operator-readable message. 4000–4003, 4005–4009, 1000, 1001 → backoff reconnect.

LOC budget breakdown: types 25 + class fields 15 + start/stop/register/fetch 25 + onMessage/onDispatch 35 + sentinel 15 + decodeMessageCreate 20 + identify/resume/heartbeat 25 + onClose 20 + helpers 10 = **~190 LOC** (within ≤200 target).

`extractWebhookIdFrom` helper (Critic 14):

```ts
// src/discord/sender-factory.ts (new helper exported)
export function extractWebhookIdFrom(url: string | undefined): string | null {
  if (!url) return null;
  const m = url.match(/\/api\/webhooks\/(\d+)\//);
  return m ? m[1] : null;
}
```

Unit tested in `tests/discord/sender-factory.test.ts` (covers happy path, missing URL, malformed URL, trailing-slash variants).

**Acceptance commands:**

```bash
npm run lint && npm test && npm run build
# Expected: 583 + 18 + 14 = 615 passing.
# bot-gateway.test.ts mocks WebSocket (Critic 18) — no live API.
# Covers: self-id filter, self-webhook filter, other-bot filter, channel allowlist,
# IDENTIFY payload, RESUME payload, HEARTBEAT scheduling, MESSAGE_CREATE decode,
# referenced_message extraction, MESSAGE_CONTENT sentinel (fires once at 10),
# registerSelfWebhookIds-twice throws, handler-throws-isolated, fetchReferenceUsername cache.

# CW-2 retro: LOC budget verification
wc -l src/discord/bot-gateway.ts
# Expected: ≤ 200. If > 250, fall back to discord.js (documented in retro).
```

**Test count delta CW-2:** +14.

---

### CW-3 — Reply routing + dispatcher + live-bot-listen

**Goal:** Wire BotGateway → dispatcher → CommandRouter / `relayOperatorInput`. Operator can `start project X` and reply-to-agent.

**Files:**

| File                                          | Status   | Approx LOC | Purpose                                                                                                  |
|-----------------------------------------------|----------|------------|----------------------------------------------------------------------------------------------------------|
| `src/discord/dispatcher.ts`                   | NEW      | ~150       | `InboundDispatcher` implementing precedence rules 2a/2b/3/4/5. Uses `senders: Record<string, DiscordSender>` (Architect V2). Error-classification dispatch (Critic 15, 16). |
| `src/discord/message-context.ts`              | NEW      | ~80        | `InMemoryMessageContext` — LRU cap 1000. `recordAgentMessage(messageId, projectId)` + `resolveProjectIdForMessage(messageId)`. |
| `src/discord/notifier.ts`                     | MODIFIED | +40        | Optional `messageContext` and optional `stateManager` deps (Architect V3, V7). For task-keyed events, resolve `projectId = state.getTask(taskId)?.projectId`; if null, skip recording. Use `sendToChannelAndReturnId` only when `messageContext` is configured. |
| `scripts/live-bot-listen.ts`                  | NEW      | ~180       | Bootstrap. Per-channel webhook-ID validation FATAL on miss (Architect cross-w 5). Startup notice to ops_channel (Architect cross-w 6). Wires gateway sentinel to ops-channel notice (Critic 13). |
| `tests/discord/dispatcher.test.ts`            | NEW      | ~290       | All 5 precedence rules (2a/2b/3 × 4 error classes/4/5). Mocks BotGateway, CommandRouter, ArchitectManager, IdentityMap, MessageContext. Critic 18: no live API. |
| `tests/discord/message-context.test.ts`       | NEW      | ~60        | LRU eviction at cap, record-then-resolve, miss returns null.                                              |
| `tests/discord/notifier.test.ts` (modified)   | MODIFIED | +50        | Architect V3: task-keyed events resolve projectId via injected StateManager; null projectId → no record. Architect V7: no messageContext → notifier doesn't call new method (back-compat). |

**`InboundDispatcher` (Architect V2 sketch — full senders map):**

```ts
// src/discord/dispatcher.ts (new)

export interface InboundDispatcherDeps {
  commandRouter: CommandRouter;
  architectManager: Pick<ArchitectManager, "relayOperatorInput">;
  identityMap: IdentityMap;
  /** Per-channel sender map from sender-factory.ts (Architect V2). */
  senders: Record<string, DiscordSender>;
  config: DiscordConfig;
  messageContext: MessageContext;  // dispatcher always has one (live-bot-listen wires real impl)
}

type RelayFailureKind = "no_session" | "session_terminated" | "queue_full" | "generic";

function classifyRelayError(err: Error): RelayFailureKind {
  const msg = err.message;
  if (msg.startsWith("No Architect session for")) return "no_session";
  if (/session terminated|aborted/i.test(msg)) return "session_terminated";
  if (/queue full/i.test(msg)) return "queue_full";
  return "generic";
}

function relayFailureMessage(kind: RelayFailureKind, projectId: string, raw: string): string {
  switch (kind) {
    case "no_session":
      return `Project \`${projectId}\` has no live Architect session — it may have completed or been aborted. Use \`!project ${projectId} status\`.`;
    case "session_terminated":
      return `Architect session for \`${projectId}\` was terminated. Re-issue via \`!project <name>\` to spawn a new one.`;
    case "queue_full":
      return `Discord send queue is full — your reply was dropped. Try again in 30 seconds.`;
    case "generic":
      return `Reply to \`${projectId}\` failed: ${raw.slice(0, 200)}`;
  }
}

export class InboundDispatcher { /* ctor stores deps; dispatch(msg) implements the precedence table below */ }
```

**`dispatch(msg)` behavior (matches precedence table 2a/2b/3/4/5):**

1. If `msg.repliedToMessageId` is set:
   - Look up `projectId = messageContext.resolveProjectIdForMessage(repliedToMessageId)`.
   - Resolve `agentResolution = identityMap.resolveAgentByUsername(repliedToAuthorUsername)` (or null).
   - **Rule 2a** (`agentResolution && projectId`): `await architectManager.relayOperatorInput(projectId, msg.content)`. On throw → **Rule 3**: classify error, send `relayFailureMessage(kind, projectId, err.message)` via `senders[msg.channelId]` (Critic 16).
   - **Rule 2b** (`agentResolution && !projectId`): send "no record of that message — re-issue directly" via `senders[msg.channelId]`.
   - **Rule 4** (`!agentResolution`): fall through.
2. **Rule 5** (default): if `content` starts with `!`, parse `cmd args` and call `commandRouter.handleCommand`; else `commandRouter.handleNaturalLanguage`. Send reply via `senders[msg.channelId]`.

All `senders[msg.channelId]?.sendToChannel(...)` calls use optional-chaining — a missing channel sender (config drift) becomes a silent skip with a warning log, never a crash.

**`live-bot-listen.ts` bootstrap behavior (Architect cross-w 5 + 6, Critic 13):**

1. `loadDotEnv` + `loadConfig`.
2. **(Cross-w 5)** Build `webhookIdsByChannel: Record<channelId, webhookId>` by iterating over the three channels and calling `extractWebhookIdFrom(process.env.DISCORD_WEBHOOK_<NAME>)`. Any null → `console.error` with channel name + remediation hint ("Run `npx tsx scripts/provision-webhooks.ts`") + `process.exit(2)`. **No `.filter(Boolean)` shortcut.**
3. Construct `SDKClient`, `StateManager`, `ProjectStore`, `SessionManager`, `MergeGate`, `ReviewGate`, `ArchitectManager` (existing live-project pattern).
4. `senders = buildSendersForChannels(config, DISCORD_BOT_TOKEN)`.
5. `messageContext = new InMemoryMessageContext({ maxEntries: 1000 })`.
6. `identityMap = buildIdentityMap(config.discord.agents)`.
7. `notifier = new DiscordNotifier(senders, config.discord, { messageContext, stateManager: state })`.
8. `orch = new Orchestrator({...})`; `orch.on((ev) => notifier.handleEvent(ev))`.
9. `commandRouter = new CommandRouter({ state, config, classifier, abort, taskSink, projectStore, emit, orchestrator: orch })`.
10. `gateway = new RawWsBotGateway({ token, allowedChannelIds: Set(dev/ops/escalation) })`.
11. `gateway.registerSelfWebhookIds(Object.values(webhookIdsByChannel))`.
12. **(Critic 13)** `gateway.onMessageContentMissing(() => senders[ops_channel].sendToChannel(ops_channel, "**Warning:** MESSAGE_CONTENT intent appears disabled — enable in Developer Portal, then restart."))`.
13. `dispatcher = new InboundDispatcher({ commandRouter, architectManager, identityMap, senders, config: config.discord, messageContext })`; `gateway.on((msg) => void dispatcher.dispatch(msg))`.
14. **(Cross-w 6)** Send startup ops-channel notice: "harness-ts started. Conversational state lost across restarts; please re-issue commands as `!project ...` or via reply to a fresh agent message that lands after this notice."
15. `await gateway.start(); orch.start();` `installSigintHandler([gateway.stop, orch.shutdown, architectManager.shutdownAll])`.
16. Park forever until SIGINT.

**Notifier projectId resolution (Architect V3, V7):**

`DiscordNotifier` gets two new optional constructor options: `messageContext?: MessageContext` and `stateManager?: StateManager`. Dispatch logic:

- If `messageContext` is undefined → call `sender.sendToChannel(...)` (existing fire-and-forget). No recording. (Live-project scripts hit this path.)
- If `messageContext` is defined → call `resolveProjectId(event)`:
  - Project-keyed events (`project_*`, `architect_*`, etc.) have `event.projectId` → return it.
  - Task-keyed events (`task_*`, `merge_result`, `escalation_needed`, etc.) → return `stateManager?.getTask(event.taskId)?.projectId ?? null`.
- If projectId resolves → `sender.sendToChannelAndReturnId(...)`; on success with non-null `messageId`, call `messageContext.recordAgentMessage(messageId, projectId)`.
- If projectId is null (bare task, no stateManager configured, or task not found in state) → fall back to `sendToChannel` without recording. Sender errors swallowed as before.

**Acceptance commands:**

```bash
npm run lint && npm test && npm run build
# Expected: 583 + 18 + 14 + 18 = 633 passing.

# Live conversational E2E:
set -a && source ../.env && set +a
DISCORD_WEBHOOK_DEV=... DISCORD_WEBHOOK_OPS=... DISCORD_WEBHOOK_ESCALATION=... \
  npx tsx scripts/live-bot-listen.ts

# In Discord:
# 1. Type in #dev:  start project foo: add a hello.ts \n NON-GOALS:\n - no tests
#    → Operator sees: "Project `<id>` declared: **foo**. Architect spawned."
#    → Architect avatar posts "Architect spawned for project ..."
# 2. Reply (Discord reply UI) to the Architect message: "make it console.log instead"
#    → relayOperatorInput fires; Architect session sees fenced UNTRUSTED operator text.
# 3. Reply to a NON-AGENT message (e.g. another operator) with: "start project bar..."
#    → Falls through to CommandRouter; project bar is declared. (Rule 4 verified.)
# 4. After project completion, reply to the OLD Architect message:
#    → Rule 2b error in #dev: "no record of that message — re-issue directly".
# 5. Force-shutdown architect, reply to a known agent message:
#    → Rule 3: error reply in #dev classifying the failure.
```

**Test count delta CW-3:** +18 (dispatcher: 14 covering rules 2a/2b/3×4err/4/5/`!`-prefix; message-context: 3; notifier extension: 1 net new).

---

## Pre-Mortem (3 scenarios)

### Scenario 1: "Bot loops on its own outbound webhook messages"

**What happens:** Webhook posts an Architect message. Gateway sees that message in the channel. Without a self-webhook filter, dispatcher thinks the bot just typed "Architect spawned for project foo" and runs it through CommandRouter. NL pattern matches `start ... project ...` if message coincidentally contains those words. Loop.

**Mitigation:**

- Rule 0 (`authorId === bot.id`) catches direct bot-token sends — but webhooks have **different author IDs** (the webhook's snowflake).
- Rule 0a (`webhookId in self-set`) is critical. `gateway.registerSelfWebhookIds([...])` is called with all our provisioned webhook IDs at startup.
- Cross-wave fix #5: bootstrap fails fatal if any channel lacks a webhook ID, so `selfWebhookIds` is never accidentally empty.
- `registerSelfWebhookIds` throws if called twice (defense against config drift).
- Test coverage: `bot-gateway.test.ts` includes "self-webhook drops" + "unconfigured webhook IDs throws on second register."

### Scenario 2: "Operator's reply to an old, completed Architect message routes nowhere"

**What happens:** Operator replies to an Architect message from a project that finished hours ago. `MessageContext` cache evicted (LRU). Dispatcher rule 2a finds no projectId.

**Mitigation:**

- Rule 2b (Critic 16): explicit error reply in operator's channel — "I recognized this as a reply to {Agent}, but I have no record of that message — re-issue your command directly."
- Cross-wave fix #6: startup ops-channel notice warns operator that conversational state is lost on restart.
- LRU cap 1000 entries (~200 KB RAM) covers multi-day runs.
- CW-4 follow-up: persistent `MessageContext` (sqlite) closes this gap.
- Race window (Critic 17): operator replies before notifier records — falls to rule 4 → NL parse → `unknown` intent → confused error. Documented limitation; sender drain is typically faster than human typing.

### Scenario 3: "Discord rate limits + reconnect storm crashes the bot"

**What happens:** Operator restarts `live-bot-listen.ts` rapidly during debugging. Discord throttles Gateway logins (5/5min). 6th login: 429. WebSocket onclose with code 4004 / 4014 / similar. Without code, process throws unhandled and exits.

**Mitigation:**

- `onClose` handler classifies close codes per Discord spec: 4004 (auth fail) / 4014 (disallowed intents) → `process.exit(2)` with clear message; 4000-4003 / 4005-4009 → backoff + reconnect with cap.
- No silent retry — operator sees the failure mode.
- Architect/Reviewer/Executor sessions outlive Gateway disconnects (they go via REST/webhook). Inbound goes dark until reconnect; outbound continues.
- Test: `bot-gateway.test.ts` mock-reject login → asserts `process.exit(2)` is called with a specific code (using a process-exit stub).

---

## Test Plan

### Test Count Reconciliation (Critic 12)

| File                                           | New      | Modified | Cases                                                                                                    | Net new |
|------------------------------------------------|----------|----------|----------------------------------------------------------------------------------------------------------|---------|
| `tests/discord/identity-map.test.ts`           | NEW      |          | 6 (conflict, case-insensitive, miss, empty, whitespace trim, multi-agent)                                | +6      |
| `tests/discord/sender-factory.test.ts`         | NEW      |          | 7 (webhook→Webhook, no-webhook→Bot, `extractWebhookIdFrom` happy/missing/malformed/trailing-slash, fetch mock) | +7      |
| `tests/discord/bot-sender.test.ts`             |          | MODIFIED | 2 (sendToChannelAndReturnId ID extraction, ID-null on REST failure)                                      | +2      |
| `tests/discord/sender.test.ts`                 |          | MODIFIED | 2 (sendToChannelAndReturnId via webhook send response)                                                   | +2      |
| `tests/lib/config.test.ts`                     |          | MODIFIED | 1 (webhooks block parses; defaults + optional)                                                            | +1      |
| `tests/discord/bot-gateway.test.ts`            | NEW      |          | 14 (self-id, self-webhook, other-bot, channel-allowlist, IDENTIFY, RESUME, HEARTBEAT, decode + ref-msg, sentinel-fires-once, sentinel-resets-on-content, registerSelfWebhookIds-twice-throws, handler-throws-isolated, fetchReferenceUsername cache, exit-on-bad-close-code) | +14     |
| `tests/discord/dispatcher.test.ts`             | NEW      |          | 14 (rule 2a happy, rule 2b no-project, rule 3 × 4 error classes [no_session/session_terminated/queue_full/generic], rule 4 fall-through, rule 5 NL, rule 5 `!command`, no senders[channel] graceful skip, agent-resolves-but-no-project-and-no-agent, content empty edge) | +14     |
| `tests/discord/message-context.test.ts`        | NEW      |          | 3 (record + resolve, LRU eviction at cap, miss returns null)                                             | +3      |
| `tests/discord/notifier.test.ts`               |          | MODIFIED | 1 (Architect V3: task-keyed event resolves projectId via stateManager; null projectId → no record)       | +1      |
|                                                |          |          | **Total net new**                                                                                        | **+50** |

**Reconciliation note vs v1's "47":** v1 absorbed overlaps in narrative; v2 enumerates explicitly. The 50 includes the +3 above v1's count from breaking out error-class cases in dispatcher (4 rule-3 sub-cases instead of 1).

### Live API Isolation (Critic 18)

| Test file                              | Mock target          | Real Discord API hit?         |
|----------------------------------------|----------------------|--------------------------------|
| `bot-gateway.test.ts`                  | `WebSocket` (via factory injection) | No                             |
| `bot-sender.test.ts`                   | `fetch`              | No                             |
| `sender.test.ts` (WebhookSender)       | `WebhookClient.send` | No                             |
| `sender-factory.test.ts`               | `fetch`              | No                             |
| `dispatcher.test.ts`                   | `BotGateway`, `CommandRouter`, `ArchitectManager` (all interfaces) | No |
| `message-context.test.ts`              | None (pure logic)    | No                             |
| `identity-map.test.ts`                 | None (pure logic)    | No                             |
| `notifier.test.ts` (modified)          | `DiscordSender` fake | No                             |
| `config.test.ts`                       | None                 | No                             |

**Zero unit tests issue real Discord traffic.** Only `live-discord-smoke.ts`, `live-bot-listen.ts`, and live-project scripts hit the real API — all explicitly opt-in via `npx tsx scripts/...`.

### Integration (3 in-process tests, no live API)

`tests/discord/conversational-flow.integration.test.ts` (new, ~220 LOC):

1. **Project declaration flow:** stub gateway emits `start project foo NON-GOALS:- bar`; assert orchestrator's `declareProject` called; assert agent message sent through stub sender; assert `MessageContext` recorded the message via `sendToChannelAndReturnId`.
2. **Reply-to-agent flow:** seed `MessageContext` with `(msg-id-1, project-x)`; stub gateway emits inbound with `repliedToMessageId=msg-id-1`, `repliedToAuthorUsername=Architect`; assert `architectManager.relayOperatorInput("project-x", ...)` called with operator's content.
3. **Self-webhook loop guard:** stub gateway is configured with `selfWebhookIds=[123]`; emit raw inbound with `webhookId=123`; assert dispatcher receives zero calls.

### E2E / Live (manual, runnable but not CI)

`scripts/live-bot-listen.ts` is the gold-standard live test. CW-3 acceptance commands above are the operator runbook.

### Regression

- All existing 583 tests stay green.
- `npm run lint` clean (`tsc --noEmit`).
- `npm run build` clean.
- All 6 existing `live-project*.ts` scripts continue to run unchanged in behavior (they pass no `messageContext` per Architect cross-w 7).

---

## Acceptance Criteria

### CW-1 done when:

- [ ] `provision-webhooks.ts` exists, idempotent (GET-then-reuse), prints env-var URLs + IDs.
- [ ] `DiscordConfig.webhooks` parses correctly from `project.toml`.
- [ ] `buildIdentityMap` enforces uniqueness at construction (test: duplicate → throws).
- [ ] `buildSendersForChannels` returns WebhookSender when URL present, BotSender otherwise.
- [ ] `extractWebhookIdFrom` covers happy + malformed + missing.
- [ ] `BotSender.sendToChannelAndReturnId` extracts ID from REST response; returns null on send failure (no throw).
- [ ] `WebhookSender.sendToChannelAndReturnId` extracts ID from webhook send response.
- [ ] All 6 `live-project*.ts` scripts use the factory; no inline sender construction; no `messageContext` passed (Architect V7).
- [ ] `live-discord-smoke.ts` shows distinct avatars in Discord (manual visual check).
- [ ] Test count: ≥ 583 + 18 = 601. Lint + build green.

### CW-2 done when:

- [ ] `ws` + `@types/ws` added to `package.json`.
- [ ] `RawWsBotGateway` implements `BotGateway`. Filters self-id, self-webhook, other-bots, channel allowlist.
- [ ] LOC budget verified: `wc -l src/discord/bot-gateway.ts` ≤ 200. If > 250, retro documents fallback to discord.js.
- [ ] `MESSAGE_CONTENT` sentinel fires once at 10 consecutive empty-content inbounds; resets on first non-empty.
- [ ] `registerSelfWebhookIds` throws on second call.
- [ ] `fetchReferenceUsername` returns cached username from `referenced_message`; null on miss.
- [ ] Bad close-code (4004/4014) → `process.exit(2)` with clear message.
- [ ] Test count: ≥ 583 + 18 + 14 = 615. Lint + build green.

### CW-3 done when:

- [ ] `InboundDispatcher` implements rules 2a/2b/3×4err/4/5; each has a unit test.
- [ ] `senders: Record<string, DiscordSender>` map used throughout — no `escalationSender` shorthand anywhere.
- [ ] Rule-3 errors classified into 4 kinds with distinct operator-visible messages.
- [ ] Rule-2b and rule-3 replies land in `msg.channelId` (operator's channel).
- [ ] `DiscordNotifier` resolves projectId via injected `StateManager` for task-keyed events; null → skip recording.
- [ ] `DiscordNotifier` works without `messageContext` (back-compat for live-project scripts).
- [ ] `live-bot-listen.ts` FATAL-fails if any channel webhook ID is missing.
- [ ] `live-bot-listen.ts` sends startup ops-channel notice.
- [ ] `live-bot-listen.ts` wires `MESSAGE_CONTENT` sentinel handler to ops-channel.
- [ ] Manual E2E: declare project → reply to agent → relay → see Architect respond. All 5 scenarios in CW-3 acceptance commands verified.
- [ ] Test count: ≥ 583 + 18 + 14 + 18 = 633. Lint + build green.

### Global done when:

- [ ] All three waves' acceptance criteria met.
- [ ] `npm run lint && npm test && npm run build` green.
- [ ] No regressions in any of the 6 existing `live-project*.ts` scripts (manual: each runs to PASS).
- [ ] CLAUDE.md (harness-ts) gains a one-paragraph note pointing at `scripts/live-bot-listen.ts`.
- [ ] Operator's manual verification: full Discord-only conversational round-trip observed.

---

## ADR — Conversational Discord Architecture (v2)

### Decision

Build the conversational interface as three thin layers over the existing harness:

1. **CW-1: Per-channel sender factory** — webhook URLs in `DiscordConfig.webhooks`, WebhookSender for channels with one and BotSender otherwise. `IdentityMap` is the sole username→agent resolver. `DiscordSender` interface gains a non-breaking `sendToChannelAndReturnId` method.
2. **CW-2: `RawWsBotGateway`** — direct WebSocket connection to Discord Gateway (~150 LOC), op-code dispatch, IDENTIFY+heartbeat+RESUME, MESSAGE_CONTENT sentinel, `referenced_message` extraction. discord.js fallback documented if LOC budget overruns.
3. **CW-3: `InboundDispatcher`** — owns precedence rules 2a/2b/3/4/5 with `senders: Record<string, DiscordSender>` map. Rule-3 errors classified into 4 kinds. `MessageContext` is the sole projectId resolver (no heuristic). Live-bot-listen fails fatal on missing webhook IDs and emits a startup ops-channel notice.

### Drivers

1. **Reply-target intent dominance** (Principle 1) — operator dialogue MUST take precedence over textual NL parsing. Enforced in dispatcher rule ordering.
2. **Existing infrastructure leverage** — BotSender, WebhookSender, DiscordNotifier, CommandRouter, ArchitectManager.relayOperatorInput already exist and are tested.
3. **Test fidelity without live API** (Critic 18) — Gateway mock at WebSocket layer; dispatcher mocks all transports; zero unit tests touch real Discord.
4. **Style consistency** — raw-WS gateway aligns with raw-fetch BotSender + WebhookSender patterns. No high-level SDK in the codebase.
5. **Explicit project identity** (Principle 6) — `MessageContext` as sole resolver eliminates the "wrong project" silent failure mode.

### Alternatives considered

| Alternative                                       | Status     | Why                                                                                                                                       |
|---------------------------------------------------|------------|-------------------------------------------------------------------------------------------------------------------------------------------|
| **discord.js gateway**                            | **Fallback** | 25 MB dep + style mismatch. Kept as escape hatch if raw-WS LOC budget exceeds 250 in CW-2 retro.                                          |
| Embed reply routing inside `CommandRouter`        | Rejected   | CommandRouter is intent classification; routing-by-message-reference is a different layer (transport, not parsing).                       |
| Persist `MessageContext` to sqlite                | Deferred (CW-4) | In-memory LRU at 1000 entries covers expected operator session length. Persistence is the documented mitigation if cache misses prove frequent. |
| Single global webhook (one URL, all agents)       | Rejected   | Defeats per-agent avatar UX. Per-channel webhooks provide stable identity and rate-limit isolation.                                       |
| Skip BotGateway, poll `/channels/{id}/messages`   | Rejected   | REST polling at 1 Hz hits Discord's rate limits hard, has 1-sec lag floor, misses messages between polls.                                  |
| Breaking change to `DiscordSender.sendToChannel` return type | Rejected (Architect V1) | Every existing fake/stub breaks. Replaced with additive `sendToChannelAndReturnId` method.                                       |
| "Most recently declared project" heuristic for rule 2 | Rejected (Architect cross-w 9) | Silent wrong-project routing under concurrent projects. `MessageContext` is the only acceptable answer (Principle 6).                     |
| `escalationSender` shorthand in dispatcher        | Rejected (Architect V2) | Hides per-channel routing and creates magic-channel coupling. `senders: Record<string, DiscordSender>` is explicit.                       |

### Why chosen

- **Layering matches harness style** — types in `types.ts`, transport in `bot-gateway.ts`/`bot-sender.ts`, semantics in `dispatcher.ts`/`commands.ts`. Each layer has a clear test surface.
- **Reply-precedence in one table** — if rule order needs tuning, one file (`dispatcher.ts`) and one test file capture truth.
- **Single `IdentityMap` source** prevents the "two registries drifted" failure mode.
- **Raw WS gateway** keeps deps minimal and style consistent. `referenced_message` in `MESSAGE_CREATE` payload removes the only protocol reason to reach for a high-level SDK.
- **Additive `DiscordSender` evolution** preserves all 583 existing tests.

### Consequences

**Positive:**
- Conversational E2E with one new live script.
- Per-agent avatars in production. Operator UX matches screenshot reference.
- Reply-precedence formally specified, error-classified, and tested.
- Zero breaking changes to `DiscordSender` (additive only).
- Style consistency: raw-fetch + raw-WS throughout.

**Negative:**
- Raw-WS gateway = ~150 LOC of protocol code we own. Mitigated by tight test coverage at WebSocket layer.
- `MessageContext` in-memory cap means stale-reply UX after restart; mitigated by rule 2b error + startup notice.
- `MESSAGE_CONTENT` intent dependency — operator must enable in Developer Portal. Sentinel detects + warns.
- `provision-webhooks.ts` requires `MANAGE_WEBHOOKS` permission on the bot — must be in role config.

### Follow-ups

1. **CW-3.5:** Replace string-match `relay` error classification with typed `RelayError { kind: ... }` thrown from `architect.ts`. Non-blocking.
2. **CW-4 (deferred):** Persist `MessageContext` to sqlite or `state.json` — closes restart-cache-miss gap.
3. **CW-5 (deferred):** Cross-bot allowlist for collaborators (e.g. a separate `wikibot`).
4. **CW-6 (deferred):** Rich embeds for project/phase status (currently markdown text).
5. **CW-7 (deferred):** Slash commands (`/project`, `/status`) — Discord interactions API.
6. **Provisioning automation:** `provision-webhooks.ts` could optionally write to `.env.local` (operator opt-in).

---

## Open Questions

(Will be appended to `.omc/plans/open-questions.md` per Planner protocol — additions only since v1.)

1. **`MessageContext` LRU eviction policy choice** — current LRU on `recordAgentMessage` order. Should the evictor prefer to keep messages from currently-active projects? (Probably overkill; LRU at 1000 is fine. Note for CW-4.)
2. **`relayOperatorInput` typed-error migration** — when CW-3.5 lands, dispatcher's `classifyRelayError` becomes a one-line `err.kind` lookup. Track the migration so the `// TODO(CW-3.5)` comment is removed.
3. **`MESSAGE_CONTENT` sentinel false-positive** — if first 10 inbounds happen to be reply-only (where Discord clients send empty content for "+" reactions or similar), sentinel could fire wrongly. **Recommendation:** require 10 *non-reply* empty-content messages before firing. Adds 5 LOC to gateway. Verify with test.
4. **`extractWebhookIdFrom` URL variants** — Discord docs show `/api/v10/webhooks/...` and `/api/webhooks/...` both valid. Regex must accept both. Update test cases.
5. **Operator hot-restart UX** — when bot restarts, the startup ops-channel notice arrives, but operators replying mid-restart see rule 2b errors. Acceptable for Phase 1; Phase 2 (CW-4) persists `MessageContext`.

---

## Plan Summary

**Plan saved to:** `/home/typhlupgrade/.local/share/ozy-bot-v3/.omc/plans/ralplan-conversational-discord.md` (v2)

**Scope:**
- 3 waves; ~13 new files; ~13 modified files
- ~830 LOC product + ~970 LOC tests
- Estimated complexity: MEDIUM (no new agent tier; layering atop existing infra)

**Key Deliverables:**
1. Per-agent avatars in live runs via per-channel WebhookSender + idempotent provisioning script.
2. `RawWsBotGateway` listening on Discord with bot-self / bot-webhook / channel-allowlist filtering and `MESSAGE_CONTENT` sentinel detection.
3. `InboundDispatcher` enforcing 5-rule reply-precedence routing into `relayOperatorInput`, with classified error fallback in 4 kinds and explicit per-channel `senders` map.
4. `live-bot-listen.ts` — first conversational E2E entry point with FATAL webhook-ID validation and startup ops-channel SPOF notice.

**Consensus mode:**
- RALPLAN-DR: 6 principles + 3 drivers + 2 viable options for the Gateway choice (raw WS PRIMARY; discord.js documented fallback).
- ADR present (Decision / Drivers / Alternatives / Why chosen / Consequences / Follow-ups).
- Pre-mortem: 3 scenarios with mitigations.
- Test plan: 50 unit + 3 integration + manual E2E + regression checklist; live-API isolation table explicit.

**Iteration 2 changes:** 18 review findings addressed (full table at top of plan).

**Does this plan capture your intent?**
- "proceed" — Begin implementation via `/oh-my-claudecode:start-work ralplan-conversational-discord`
- "adjust [X]" — Return to interview to modify
- "restart" — Discard and start fresh
