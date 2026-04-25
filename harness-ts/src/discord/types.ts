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
 */
export interface AllowedMentions {
  parse?: Array<"everyone" | "roles" | "users">;
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
  sendToChannel(channel: string, content: string, identity?: AgentIdentity): Promise<void>;
  sendToChannelAndReturnId(
    channel: string,
    content: string,
    identity?: AgentIdentity,
  ): Promise<{ messageId: string | null }>;
  addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
}

/**
 * CW-1 default helper for test fakes / minimal senders that only implement
 * `sendToChannel`. Delegates to `sendToChannel` and returns `{messageId: null}`.
 * Real senders (BotSender, WebhookSender) implement `sendToChannelAndReturnId`
 * natively to capture the Discord-assigned message id.
 */
export async function sendToChannelAndReturnIdDefault(
  sender: { sendToChannel: (channel: string, content: string, identity?: AgentIdentity) => Promise<void> },
  channel: string,
  content: string,
  identity?: AgentIdentity,
): Promise<{ messageId: string | null }> {
  await sender.sendToChannel(channel, content, identity);
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
