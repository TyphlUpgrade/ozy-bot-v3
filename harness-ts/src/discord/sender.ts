/**
 * Webhook-based Discord sender — implements DiscordSender via an injectable
 * WebhookClient.
 *
 * **Rate limiting:** Discord webhook rate limit is 30 msg / 60s per channel.
 * Enforced via a minimum-spacing token bucket (`minSpacingMs` between sends)
 * with a bounded queue; overflow drops the oldest message and logs a warning.
 *
 * **Error swallowing:** send failures never throw out of `sendToChannel` —
 * they log the error message (not the full object, which may echo request
 * content) and mark the promise resolved. Discord hiccups must not crash the
 * pipeline.
 *
 * **Mention injection defense:** every outgoing payload carries
 * `allowedMentions: { parse: [] }` so `@everyone`/`@here`/role pings in the
 * message body cannot actually ping anyone. Defense-in-depth alongside
 * `sanitize()` in notifier.ts.
 *
 * **Resolve-on-drop semantics (documented contract):** when the queue is at
 * capacity and a new message arrives, the oldest queued message's promise is
 * resolved (not rejected). Callers that need to distinguish dropped-vs-sent
 * must observe the warning log. See DiscordSender docstring in types.ts.
 */

import type { AllowedMentions, DiscordSender, WebhookClient } from "./types.js";
import type { AgentIdentity } from "./types.js";

const DEFAULT_MIN_SPACING_MS = 2000;
const DEFAULT_MAX_QUEUE_SIZE = 50;
const NO_MENTIONS: AllowedMentions = { parse: [] };

interface QueuedMessage {
  channel: string;
  content: string;
  identity?: AgentIdentity;
  /** Wave E-β — when set, sent as Discord `message_reference` for reply threading. */
  replyToMessageId?: string;
  /** CW-1 — settle with `{messageId}`; senders that ignore id pass () => undefined. */
  resolve: (messageId: string | null) => void;
}

export interface WebhookSenderOptions {
  minSpacingMs?: number;
  maxQueueSize?: number;
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export class WebhookSender implements DiscordSender {
  private readonly webhook: WebhookClient;
  private readonly minSpacingMs: number;
  private readonly maxQueueSize: number;
  private queue: QueuedMessage[] = [];
  private lastSendTime = 0;
  private draining = false;

  constructor(webhook: WebhookClient, options: WebhookSenderOptions = {}) {
    this.webhook = webhook;
    this.minSpacingMs = options.minSpacingMs ?? DEFAULT_MIN_SPACING_MS;
    this.maxQueueSize = options.maxQueueSize ?? DEFAULT_MAX_QUEUE_SIZE;
  }

  async sendToChannel(
    channel: string,
    content: string,
    identity?: AgentIdentity,
    replyToMessageId?: string,
  ): Promise<void> {
    // Queue overflow: drop oldest (FIFO cap). The dropped caller's promise
    // resolves, matching the DiscordSender contract — callers are fire-and-
    // forget and the console.warn is the observable signal of drop.
    if (this.queue.length >= this.maxQueueSize) {
      const dropped = this.queue.shift();
      dropped?.resolve(null);
      console.warn(`[WebhookSender] queue full, dropping oldest message (queue=${this.queue.length})`);
    }

    return new Promise<void>((resolve) => {
      this.queue.push({ channel, content, identity, replyToMessageId, resolve: () => resolve() });
      void this.drain();
    });
  }

  /**
   * CW-1 — same delivery contract as `sendToChannel` but resolves with the
   * Discord-assigned message id when the underlying WebhookClient returns a
   * payload with `.id` (discord.js + `wait: true`). Returns `{messageId: null}`
   * when the client returns nothing parseable, on overflow drop, or send failure.
   *
   * Wave E-β — accepts optional `replyToMessageId` to thread the send as a
   * reply to a prior message in the same channel.
   */
  async sendToChannelAndReturnId(
    channel: string,
    content: string,
    identity?: AgentIdentity,
    replyToMessageId?: string,
  ): Promise<{ messageId: string | null }> {
    if (this.queue.length >= this.maxQueueSize) {
      const dropped = this.queue.shift();
      dropped?.resolve(null);
      console.warn(`[WebhookSender] queue full, dropping oldest message (queue=${this.queue.length})`);
    }
    return new Promise<{ messageId: string | null }>((resolve) => {
      this.queue.push({
        channel,
        content,
        identity,
        replyToMessageId,
        resolve: (messageId) => resolve({ messageId }),
      });
      void this.drain();
    });
  }

  /** Reactions require a real bot client — webhook sender is a no-op. Wave 3 implements. */
  async addReaction(_channelId: string, _messageId: string, _emoji: string): Promise<void> {
    // intentional no-op
  }

  private async drain(): Promise<void> {
    if (this.draining) return;
    this.draining = true;
    try {
      while (this.queue.length > 0) {
        const elapsed = Date.now() - this.lastSendTime;
        if (elapsed < this.minSpacingMs) {
          await new Promise((r) => setTimeout(r, this.minSpacingMs - elapsed));
        }
        const next = this.queue.shift();
        if (!next) break;
        let messageId: string | null = null;
        try {
          // CW-1 — `wait: true` asks Discord to return the created message
          // object so we can extract `.id`. discord.js WebhookClient.send
          // surfaces this as a `Message` instance whose `.id` is the message id.
          const result = await this.webhook.send({
            content: next.content,
            username: next.identity?.username,
            avatarURL: next.identity?.avatarURL,
            allowedMentions: NO_MENTIONS,
            wait: true,
            // Wave E-β — `failIfNotExists: false` is mandatory: Discord
            // renders without a quote-card if head was deleted instead of
            // 4xx-rejecting the send. discord.js v14+ accepts `messageReference`.
            ...(next.replyToMessageId
              ? { messageReference: { messageId: next.replyToMessageId, failIfNotExists: false } }
              : {}),
          });
          if (result && typeof result === "object" && "id" in result) {
            const id = (result as { id?: unknown }).id;
            if (typeof id === "string") messageId = id;
          }
        } catch (err) {
          // Log only .message — avoid echoing request content (incl. any
          // secrets that slipped through redaction) into the error stream.
          console.error(`[WebhookSender] send failed for channel=${next.channel}: ${errMessage(err)}`);
        }
        this.lastSendTime = Date.now();
        next.resolve(messageId);
      }
    } finally {
      this.draining = false;
    }
  }

  /** Queue depth for tests / diagnostics. */
  get pendingCount(): number {
    return this.queue.length;
  }
}
