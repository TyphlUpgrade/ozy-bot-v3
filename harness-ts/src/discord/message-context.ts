/**
 * CW-3 — MessageContext: a small in-memory cache mapping outbound agent
 * Discord message ids → projectId. Populated by `DiscordNotifier` when an
 * agent posts a project-keyed message; consulted by `InboundDispatcher` so
 * a Discord reply to that message resolves back to the originating project
 * and can be relayed into the Architect session via `relayOperatorInput`.
 *
 * Pure in-memory + LRU eviction at `maxEntries`. Process-restart loses the
 * cache; cross-w 6 prints an ops-channel notice on bootstrap so operators
 * know to re-issue commands. CW-4 follow-up: persistent (sqlite) backing.
 */

export interface MessageContext {
  /** Record an outbound agent message id and the project it belongs to. */
  recordAgentMessage(messageId: string, projectId: string): void;
  /** Resolve a recorded message id back to its projectId, or `null` on miss. */
  resolveProjectIdForMessage(messageId: string): string | null;
}

export interface InMemoryMessageContextOptions {
  /** Maximum entries before LRU eviction kicks in. Defaults to 1000. */
  maxEntries?: number;
}

const DEFAULT_MAX_ENTRIES = 1000;

/**
 * In-memory `MessageContext` with insertion-order LRU eviction. Backed by a
 * single `Map` — JavaScript `Map` preserves insertion order, so when capacity
 * is hit we drop the oldest key. `recordAgentMessage` re-promotes an existing
 * key to the most-recent slot by deleting + re-inserting.
 */
export class InMemoryMessageContext implements MessageContext {
  private readonly entries = new Map<string, string>();
  private readonly maxEntries: number;

  constructor(options: InMemoryMessageContextOptions = {}) {
    const cap = options.maxEntries ?? DEFAULT_MAX_ENTRIES;
    if (cap <= 0) throw new Error("InMemoryMessageContext: maxEntries must be > 0");
    this.maxEntries = cap;
  }

  recordAgentMessage(messageId: string, projectId: string): void {
    if (this.entries.has(messageId)) {
      // Re-promote: delete + re-insert so this messageId becomes most-recent.
      this.entries.delete(messageId);
    }
    this.entries.set(messageId, projectId);
    if (this.entries.size > this.maxEntries) {
      // Evict oldest (first key in insertion order).
      const oldestKey = this.entries.keys().next().value;
      if (oldestKey !== undefined) this.entries.delete(oldestKey);
    }
  }

  resolveProjectIdForMessage(messageId: string): string | null {
    return this.entries.get(messageId) ?? null;
  }

  /** Test seam — exposes current size without leaking the underlying Map. */
  get size(): number {
    return this.entries.size;
  }
}
