/**
 * Reaction client stub — the webhook-based `WebhookSender` cannot add
 * reactions (reactions require a real discord.js Client). Wave 3 declares
 * the interface; Wave 4 (or whenever a bot-login lane ships) provides the
 * production implementation.
 *
 * A no-op `NoopReactionClient` is exported so Wave 3 consumers can wire
 * the full interface without gating on the real client. Call sites that
 * need reactions as acknowledgement fall back to the no-op today and light
 * up once a real `ReactionClient` is injected.
 */

export interface ReactionClient {
  /**
   * Add an emoji reaction to a Discord message. Implementations MUST swallow
   * errors — reactions are cosmetic acknowledgements, never blocking.
   */
  addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
}

/** Emoji presets used across the harness for cosmetic acknowledgements. */
export const REACTION_EMOJI = {
  received: "👀",
  success: "✅",
  error: "❌",
} as const;

/** No-op reaction client — used until a real discord.js Client is wired (Wave 4). */
export class NoopReactionClient implements ReactionClient {
  async addReaction(_channelId: string, _messageId: string, _emoji: string): Promise<void> {
    // intentional no-op
  }
}
