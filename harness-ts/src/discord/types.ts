/**
 * Shared Discord interfaces — sender abstraction, agent identity, webhook payload.
 * No runtime code; other Discord files import from here.
 */

export interface AgentIdentity {
  username: string;
  avatarURL: string;
}

/**
 * Allowed-mentions discord.js payload shape. `parse: []` blocks all auto-pings
 * (@everyone, @here, role, user). Wave 2 always sends with `parse: []` as
 * defense-in-depth against Discord message-ping injection from untrusted input.
 *
 * Channel-collapse plumbing (2026-04-27) — additive optional fields `users`,
 * `roles`, `repliedUser` allow per-call escalation pings while keeping the
 * default `{ parse: [] }` for non-escalation traffic. WebhookSender accepts
 * the camelCase shape as-is (discord.js shape); BotSender translates each
 * field to its snake_case Discord API equivalent (`replied_user`).
 */
export interface AllowedMentions {
  parse?: Array<"everyone" | "roles" | "users">;
  users?: string[];
  roles?: string[];
  repliedUser?: boolean;
}

/**
 * Injectable abstraction for Discord outbound. WebhookSender implements this;
 * tests use fakes.
 *
 * **Resolve-on-drop semantics:** when the queue is at capacity, the oldest
 * pending message is resolved (not rejected) and a warning is logged. Callers
 * that need to distinguish dropped-vs-delivered must observe the warning log.
 * This is intentional: Wave 2 notifier is fire-and-forget, so a resolved
 * promise keeps it cheap; Wave 3+ bot-client dialogue flows should not route
 * through this sender.
 *
 * **CW-1 — sendToChannelAndReturnId:** identical contract to `sendToChannel`
 * but returns the resulting Discord message id (or `null` if not available —
 * e.g., dropped on overflow, fake sender, or webhook send without `wait=true`).
 * Conversational flows (CW-3+) need the id to wire reply ↔ task linkage; the
 * fire-and-forget notifier does not.
 *
 * **CW-5 — addReaction:** Discord webhooks cannot post reactions; reactions
 * require an authenticated bot REST call. `BotSender.addReaction` POSTs the
 * `PUT /channels/{c}/messages/{m}/reactions/{e}/@me` route, while
 * `WebhookSender.addReaction` is intentionally a no-op. Callers that need
 * reactions cross-cutting all channels (e.g., the inbound dispatcher) should
 * inject a separate `reactionClient: DiscordSender` — typically a `BotSender`
 * instance — independent of the per-channel content senders.
 */
export interface DiscordSender {
  /**
   * Wave E-β — `replyToMessageId` is optional. When set, the underlying
   * Discord send carries `message_reference: { message_id, fail_if_not_exists: false }`
   * so the post renders as an in-channel reply. `fail_if_not_exists: false` is
   * mandatory: if the head message was deleted, Discord renders without the
   * quote-card instead of 4xx-rejecting the send.
   *
   * Channel-collapse plumbing (2026-04-27) — `allowedMentions` is optional.
   * When omitted, senders default to `{ parse: [] }` (CW-3 mention-injection
   * defense). Commit 2 sets this for escalation-class events so the operator
   * mention `<@operator_user_id>` actually pings.
   */
  sendToChannel(
    channel: string,
    content: string,
    identity?: AgentIdentity,
    replyToMessageId?: string,
    allowedMentions?: AllowedMentions,
  ): Promise<void>;
  sendToChannelAndReturnId(
    channel: string,
    content: string,
    identity?: AgentIdentity,
    replyToMessageId?: string,
    allowedMentions?: AllowedMentions,
  ): Promise<{ messageId: string | null }>;
  addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
}

/**
 * CW-1 default helper for test fakes / minimal senders that only implement
 * `sendToChannel`. Delegates to `sendToChannel` and returns `{messageId: null}`.
 * Real senders (BotSender, WebhookSender) implement `sendToChannelAndReturnId`
 * natively to capture the Discord-assigned message id.
 *
 * Wave E-β — forwards optional `replyToMessageId` to inner `sendToChannel`.
 * Channel-collapse plumbing (2026-04-27) — forwards optional `allowedMentions`
 * to inner `sendToChannel`.
 */
export async function sendToChannelAndReturnIdDefault(
  sender: {
    sendToChannel: (
      channel: string,
      content: string,
      identity?: AgentIdentity,
      replyToMessageId?: string,
      allowedMentions?: AllowedMentions,
    ) => Promise<void>;
  },
  channel: string,
  content: string,
  identity?: AgentIdentity,
  replyToMessageId?: string,
  allowedMentions?: AllowedMentions,
): Promise<{ messageId: string | null }> {
  await sender.sendToChannel(channel, content, identity, replyToMessageId, allowedMentions);
  return { messageId: null };
}

/** Minimal contract the discord.js WebhookClient satisfies. Keeps us decoupled from the SDK. */
export interface WebhookClient {
  send(options: {
    content: string;
    username?: string;
    avatarURL?: string;
    allowedMentions?: AllowedMentions;
    /** CW-1 — request the message object back (Discord `?wait=true` semantics). */
    wait?: boolean;
    /**
     * Wave E-β — Discord `message_reference` payload for reply chains. When
     * present, the new message renders as a reply to `messageId` in the same
     * channel. `failIfNotExists: false` mirrors the wire field
     * `fail_if_not_exists: false` so a deleted head doesn't 4xx the send.
     * discord.js v14+ surfaces this field as `messageReference` on the
     * WebhookClient.send options.
     */
    messageReference?: {
      messageId: string;
      failIfNotExists: boolean;
    };
  }): Promise<unknown>;
}

/**
 * CW-2 — Internal raw-decoded payload from Gateway op-code 0 MESSAGE_CREATE.
 * No `any`. Empty `content` indicates the MESSAGE_CONTENT intent is disabled
 * (or the message genuinely has no text — see `RawWsBotGateway` sentinel).
 */
export interface RawMessage {
  messageId: string;
  channelId: string;
  authorId: string;
  authorUsername: string;
  isBot: boolean;
  /** Set when the message was authored by a webhook (channel webhook id). */
  webhookId: string | null;
  /** Empty string if MESSAGE_CONTENT intent is disabled. */
  content: string;
  /** From `message_reference.message_id` — set when this message is a reply. */
  repliedToMessageId: string | null;
  /** From `referenced_message.author.username` — populated alongside the reply. */
  repliedToAuthorUsername: string | null;
  /** ISO 8601 timestamp from the gateway payload. */
  timestamp: string;
}

/**
 * CW-2 — Stable public shape consumed by the dispatcher. Currently 1:1 with
 * `RawMessage`; reserve type for future divergence without churning callers.
 */
export type InboundMessage = RawMessage;

/**
 * CW-2 — Bot-side gateway abstraction. Implementations (raw WS, discord.js
 * fallback) consume Discord MESSAGE_CREATE events and emit filtered
 * `InboundMessage`s. Self-filter rules are owned by the gateway so dispatchers
 * never see bot's-own-output noise.
 */
export interface BotGateway {
  start(): Promise<void>;
  stop(): Promise<void>;
  on(handler: (msg: InboundMessage) => void): void;
  /** Self-filter — set BEFORE start(). Throws if called twice. */
  registerSelfWebhookIds(ids: string[]): void;
  /**
   * Architect V4 test seam: resolve a message id's author username via
   * gateway-side cache (populated from `referenced_message` payloads). Returns
   * `null` on miss. Live REST API is NEVER called from this method in CW-3.
   */
  fetchReferenceUsername(messageId: string, channelId: string): Promise<string | null>;
  /**
   * Sentinel hook (Critic 13): fired exactly once if MESSAGE_CONTENT intent
   * appears disabled (latched). Bootstrap wires this to send an ops-channel notice.
   */
  onMessageContentMissing(handler: () => void): void;
  /** CW-4.5 — bot's own Discord username, captured from READY. Null until READY fires. */
  getBotUsername(): string | null;
}
