/**
 * Bot-token Discord sender — POSTs directly to `/channels/{id}/messages` via
 * the Discord REST API, sidestepping `discord.js` and webhook-URL setup.
 *
 * Inherits the same operational guarantees as `WebhookSender`:
 * - Token-bucket rate limiting (`minSpacingMs` between sends; default 2 s).
 * - Bounded FIFO queue (`maxQueueSize`; default 50). Overflow drops the oldest
 *   message and resolves its promise — fire-and-forget contract preserved.
 * - Send failures are logged (`.message` only, never the request body) and
 *   never thrown out of `sendToChannel`. Discord hiccups never crash the
 *   pipeline.
 * - `allowed_mentions: { parse: [] }` defense-in-depth against `@everyone`,
 *   `@here`, and role pings landing in user content.
 *
 * Identity caveat: bot REST cannot override `username` / `avatar_url` per
 * message (that's a webhook-only feature). We render the agent name as a
 * `**[Architect]**` prefix on the body so the per-agent identity is at least
 * visible in chat. If you need true per-agent avatars, use `WebhookSender`
 * with one webhook URL per channel.
 */

import type { AgentIdentity, DiscordSender } from "./types.js";

const DEFAULT_MIN_SPACING_MS = 2000;
const DEFAULT_MAX_QUEUE_SIZE = 50;
const DISCORD_API_BASE = "https://discord.com/api/v10";

interface QueuedMessage {
  channelId: string;
  content: string;
  identity?: AgentIdentity;
  /** CW-1 — settle with `{messageId}` for `sendToChannelAndReturnId`; senders that ignore id pass () => undefined. */
  resolve: (messageId: string | null) => void;
}

export interface BotSenderOptions {
  minSpacingMs?: number;
  maxQueueSize?: number;
  /** Override fetch (test injection). Defaults to global fetch. */
  fetch?: typeof globalThis.fetch;
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export class BotSender implements DiscordSender {
  private readonly token: string;
  private readonly minSpacingMs: number;
  private readonly maxQueueSize: number;
  private readonly fetchImpl: typeof globalThis.fetch;
  private queue: QueuedMessage[] = [];
  private lastSendTime = 0;
  private draining = false;

  constructor(token: string, options: BotSenderOptions = {}) {
    if (!token || token.trim().length === 0) {
      throw new Error("BotSender: token must be a non-empty string");
    }
    this.token = token;
    this.minSpacingMs = options.minSpacingMs ?? DEFAULT_MIN_SPACING_MS;
    this.maxQueueSize = options.maxQueueSize ?? DEFAULT_MAX_QUEUE_SIZE;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
  }

  async sendToChannel(channelId: string, content: string, identity?: AgentIdentity): Promise<void> {
    if (this.queue.length >= this.maxQueueSize) {
      const dropped = this.queue.shift();
      dropped?.resolve(null);
      console.warn(`[BotSender] queue full, dropping oldest message (queue=${this.queue.length})`);
    }
    return new Promise<void>((resolve) => {
      this.queue.push({ channelId, content, identity, resolve: () => resolve() });
      void this.drain();
    });
  }

  /**
   * CW-1 — same delivery contract as `sendToChannel` but resolves with the
   * Discord-assigned message id (extracted from POST response JSON). Returns
   * `{messageId: null}` on overflow drop, network error, or non-2xx response.
   */
  async sendToChannelAndReturnId(
    channelId: string,
    content: string,
    identity?: AgentIdentity,
  ): Promise<{ messageId: string | null }> {
    if (this.queue.length >= this.maxQueueSize) {
      const dropped = this.queue.shift();
      dropped?.resolve(null);
      console.warn(`[BotSender] queue full, dropping oldest message (queue=${this.queue.length})`);
    }
    return new Promise<{ messageId: string | null }>((resolve) => {
      this.queue.push({
        channelId,
        content,
        identity,
        resolve: (messageId) => resolve({ messageId }),
      });
      void this.drain();
    });
  }

  async addReaction(channelId: string, messageId: string, emoji: string): Promise<void> {
    // PUT /channels/{channel.id}/messages/{message.id}/reactions/{emoji}/@me
    const path = `/channels/${channelId}/messages/${messageId}/reactions/${encodeURIComponent(emoji)}/@me`;
    try {
      const res = await this.fetchImpl(`${DISCORD_API_BASE}${path}`, {
        method: "PUT",
        headers: this.authHeaders(),
      });
      if (!res.ok) {
        console.error(`[BotSender] addReaction ${channelId}/${messageId} ${emoji} -> ${res.status}`);
      }
    } catch (err) {
      console.error(`[BotSender] addReaction failed: ${errMessage(err)}`);
    }
  }

  private authHeaders(): Record<string, string> {
    return {
      "Authorization": `Bot ${this.token}`,
      "Content-Type": "application/json",
      "User-Agent": "harness-ts (https://github.com/anthropics/claude-code, 0.1)",
    };
  }

  private renderBody(content: string, identity?: AgentIdentity): string {
    if (!identity?.username) return content;
    return `**[${identity.username}]** ${content}`;
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
          const res = await this.fetchImpl(
            `${DISCORD_API_BASE}/channels/${next.channelId}/messages`,
            {
              method: "POST",
              headers: this.authHeaders(),
              body: JSON.stringify({
                content: this.renderBody(next.content, next.identity),
                allowed_mentions: { parse: [] },
              }),
            },
          );
          if (!res.ok) {
            console.error(`[BotSender] send to ${next.channelId} -> ${res.status} ${res.statusText}`);
          } else {
            // CW-1 — Discord returns the created message object on 200/201; capture id.
            try {
              const body = (await res.json()) as { id?: unknown };
              if (body && typeof body.id === "string") messageId = body.id;
            } catch {
              // Body parse failure shouldn't abort the send — leave messageId null.
            }
          }
        } catch (err) {
          console.error(`[BotSender] send failed for channel=${next.channelId}: ${errMessage(err)}`);
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
