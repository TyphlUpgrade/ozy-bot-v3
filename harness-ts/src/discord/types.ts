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
 */
export interface DiscordSender {
  sendToChannel(channel: string, content: string, identity?: AgentIdentity): Promise<void>;
  addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
}

/** Minimal contract the discord.js WebhookClient satisfies. Keeps us decoupled from the SDK. */
export interface WebhookClient {
  send(options: {
    content: string;
    username?: string;
    avatarURL?: string;
    allowedMentions?: AllowedMentions;
  }): Promise<unknown>;
}
