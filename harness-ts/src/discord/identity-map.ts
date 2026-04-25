/**
 * CW-1 — IdentityMap: case-insensitive, whitespace-trimmed lookup from a
 * Discord username back to its agent key (orchestrator/architect/reviewer/
 * executor/operator/...). Single source of truth derived from
 * `DiscordConfig.agents`; later waves (CW-3 dispatcher) consult this map to
 * route inbound replies to the right agent lane.
 *
 * Construction throws on duplicate (lowercased) usernames so a misconfigured
 * project.toml fails loudly at startup rather than silently routing one
 * agent's replies to another.
 */

import type { DiscordConfig } from "../lib/config.js";

export type AgentKey = string;

export interface IdentityMap {
  /** Returns the agent key for a Discord username, or null on miss. */
  lookup(username: string): AgentKey | null;
  /** Underlying entries keyed by lowercased username — exposed for tests / diagnostics. */
  readonly entries: ReadonlyMap<string, AgentKey>;
}

function normalize(username: string): string {
  return username.trim().toLowerCase();
}

/**
 * Build an IdentityMap from `DiscordConfig.agents`. Throws on duplicate
 * lowercased usernames. Agents with empty `name` are skipped (no identity to
 * route on).
 */
export function buildIdentityMap(config: Pick<DiscordConfig, "agents">): IdentityMap {
  const entries = new Map<string, AgentKey>();
  for (const [agentKey, identity] of Object.entries(config.agents)) {
    const username = identity?.name;
    if (typeof username !== "string" || username.trim().length === 0) continue;
    const key = normalize(username);
    if (entries.has(key)) {
      const existing = entries.get(key)!;
      throw new Error(
        `IdentityMap: duplicate username "${username}" maps to both "${existing}" and "${agentKey}"`,
      );
    }
    entries.set(key, agentKey);
  }
  return {
    lookup(username: string): AgentKey | null {
      if (typeof username !== "string") return null;
      const key = normalize(username);
      return entries.get(key) ?? null;
    },
    entries,
  };
}
