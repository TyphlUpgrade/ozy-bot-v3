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
 *
 * Wave E-β — adds role-head tracking: `(projectId, role, channel) →
 * (messageId, recordedAtMs)` so `DiscordNotifier` can synthesize Discord
 * `message_reference` reply chains across multi-event project flows
 * (e.g. architect_decomposed → session_complete → merge_result → task_done).
 *
 * Role-head storage is intentionally a SEPARATE Map from the agent-message
 * map. Each map is bounded independently to `maxEntries`; practical capacity
 * is therefore `2 * maxEntries`. This trade-off avoids cross-map LRU
 * ordering complexity. Memory ceiling is still bounded.
 */

import type { IdentityRole } from "./identity.js";

/**
 * Wave E-β — re-export of the four canonical agent roles. `IdentityRole` is
 * the single source of truth (defined in `identity.ts` for the
 * OrchestratorEvent → role resolver). Re-exported here as `AgentRole` for
 * the role-head API; both names refer to the same string-literal union.
 */
export type AgentRole = IdentityRole;

export interface RoleHeadEntry {
  messageId: string;
  channel: string;
  recordedAtMs: number;
}

export interface MessageContext {
  /** Record an outbound agent message id and the project it belongs to. */
  recordAgentMessage(messageId: string, projectId: string): void;
  /** Resolve a recorded message id back to its projectId, or `null` on miss. */
  resolveProjectIdForMessage(messageId: string): string | null;
  /**
   * Wave E-β — record the latest outbound message id for a given
   * (projectId, role, channel) chain head. Subsequent events that should
   * thread under this role can call `lookupRoleHead` to retrieve it.
   */
  recordRoleMessage(projectId: string, role: AgentRole, messageId: string, channel: string): void;
  /**
   * Wave E-β — lookup the chain-head messageId for a (projectId, role, channel)
   * tuple. Returns `null` on miss OR when the entry is older than the
   * stale-chain TTL (entry is also evicted in that case). Lookup never auto-
   * creates a head; that must be done explicitly via `recordRoleMessage`.
   */
  lookupRoleHead(projectId: string, role: AgentRole, channel: string): string | null;
}

export interface InMemoryMessageContextOptions {
  /** Maximum entries before LRU eviction kicks in. Defaults to 1000. */
  maxEntries?: number;
  /**
   * Wave E-β — TTL in milliseconds for role-head entries. Lookups older than
   * this return null AND delete the stale entry. Defaults to 600_000 (10 min).
   */
  staleChainMs?: number;
  /**
   * Wave E-β — injectable clock for deterministic TTL tests. Defaults to
   * `Date.now`. Tests can substitute a controllable function or use
   * `vi.useFakeTimers()` (the default reads through to the faked clock).
   */
  now?: () => number;
}

const DEFAULT_MAX_ENTRIES = 1000;
const DEFAULT_STALE_CHAIN_MS = 600_000;

function roleHeadKey(projectId: string, role: AgentRole, channel: string): string {
  return `${projectId}::${role}::${channel}`;
}

/**
 * In-memory `MessageContext` with insertion-order LRU eviction. Backed by a
 * single `Map` — JavaScript `Map` preserves insertion order, so when capacity
 * is hit we drop the oldest key. `recordAgentMessage` re-promotes an existing
 * key to the most-recent slot by deleting + re-inserting.
 *
 * Wave E-β — `roleHeads` is a parallel Map with the same LRU discipline,
 * bounded independently to `maxEntries`. See file docstring.
 */
export class InMemoryMessageContext implements MessageContext {
  private readonly entries = new Map<string, string>();
  private readonly roleHeads = new Map<string, RoleHeadEntry>();
  private readonly maxEntries: number;
  private readonly staleChainMs: number;
  private readonly now: () => number;

  constructor(options: InMemoryMessageContextOptions = {}) {
    const cap = options.maxEntries ?? DEFAULT_MAX_ENTRIES;
    if (cap <= 0) throw new Error("InMemoryMessageContext: maxEntries must be > 0");
    this.maxEntries = cap;
    this.staleChainMs = options.staleChainMs ?? DEFAULT_STALE_CHAIN_MS;
    this.now = options.now ?? Date.now;
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

  recordRoleMessage(projectId: string, role: AgentRole, messageId: string, channel: string): void {
    const key = roleHeadKey(projectId, role, channel);
    if (this.roleHeads.has(key)) {
      // Re-promote: delete + re-insert so this entry becomes most-recent.
      this.roleHeads.delete(key);
    }
    this.roleHeads.set(key, { messageId, channel, recordedAtMs: this.now() });
    if (this.roleHeads.size > this.maxEntries) {
      const oldestKey = this.roleHeads.keys().next().value;
      if (oldestKey !== undefined) this.roleHeads.delete(oldestKey);
    }
  }

  lookupRoleHead(projectId: string, role: AgentRole, channel: string): string | null {
    const key = roleHeadKey(projectId, role, channel);
    const entry = this.roleHeads.get(key);
    if (entry === undefined) return null;
    if (this.now() - entry.recordedAtMs > this.staleChainMs) {
      // Stale: evict and report miss.
      this.roleHeads.delete(key);
      return null;
    }
    return entry.messageId;
  }

  /** Test seam — exposes current size without leaking the underlying Map. */
  get size(): number {
    return this.entries.size;
  }

  /** Wave E-β — test seam — exposes role-head map size. */
  get roleHeadsSize(): number {
    return this.roleHeads.size;
  }
}
