/**
 * CW-4.5 — ChannelContextBuffer: per-channel ring buffer with optional
 * project-affinity hint. Populated on every inbound that survives the
 * gateway filter chain BEFORE any dispatcher rule runs (so the message
 * that triggered the classifier is included in its own context).
 *
 * Memory bound: `channelCap` × `perChannelCap` × ~500B per ChannelMessage.
 * Default 50 × 10 ≈ 250KB upper bound.
 *
 * LRU policy on `channelCap` overflow: a `Map` preserves insertion order, so
 * on every `append` we delete-then-set the channel key (bumps to most recent).
 * When `map.size > channelCap`, the FIRST key in iteration order is the
 * least-recently-appended-to channel — evict it whole (drops all of its
 * messages) before adding the new channel.
 *
 * Per-channel ring eviction: `arr.shift()` when length exceeds `perChannelCap`.
 * Cap is small (10) so the O(n) shift is constant in practice.
 */

export interface ChannelMessage {
  /** Username (NOT discord id; classifier reads names). */
  author: string;
  /** Verbatim content — fence-strip happens in classifier prompt build, NOT here. */
  content: string;
  /** ISO 8601 timestamp from the gateway payload. */
  timestamp: string;
  /**
   * Iteration 2 change #7 — affinity hint. Set by the dispatcher's append
   * wrapper when the inbound message replies to a recorded agent message
   * (signals the operator is engaging that project). Read by
   * `resolveProjectForChannel` to disambiguate multi-active cases.
   */
  projectIdHint?: string;
}

export interface ChannelContextBufferOptions {
  /** Default 10. Per-channel ring cap. */
  perChannelCap?: number;
  /** Default 50. Max distinct channels held. LRU-on-append evicts oldest channel on overflow. */
  maxChannels?: number;
}

const DEFAULT_PER_CHANNEL_CAP = 10;
const DEFAULT_MAX_CHANNELS = 50;

export class ChannelContextBuffer {
  private readonly map = new Map<string, ChannelMessage[]>();
  private readonly perChannelCap: number;
  private readonly maxChannels: number;

  constructor(opts: ChannelContextBufferOptions = {}) {
    this.perChannelCap = opts.perChannelCap ?? DEFAULT_PER_CHANNEL_CAP;
    this.maxChannels = opts.maxChannels ?? DEFAULT_MAX_CHANNELS;
    if (this.perChannelCap <= 0) throw new Error("ChannelContextBuffer: perChannelCap must be > 0");
    if (this.maxChannels <= 0) throw new Error("ChannelContextBuffer: maxChannels must be > 0");
  }

  /**
   * Append at tail. Triggers per-channel oldest-evict on overflow + LRU
   * channel-evict on overflow. Re-promotes the channel to most-recent slot.
   */
  append(channelId: string, msg: ChannelMessage): void {
    const existing = this.map.get(channelId);
    if (existing) {
      // Re-promote: delete + re-insert so this channel becomes most-recent.
      this.map.delete(channelId);
      if (existing.length >= this.perChannelCap) existing.shift();
      existing.push(msg);
      this.map.set(channelId, existing);
      return;
    }

    // New channel — evict oldest channel BEFORE adding to keep size <= maxChannels.
    if (this.map.size >= this.maxChannels) {
      const oldest = this.map.keys().next().value;
      if (oldest !== undefined) this.map.delete(oldest);
    }
    this.map.set(channelId, [msg]);
  }

  /** Last `n` messages (default 5) for `channelId`, oldest→newest. Empty array on miss. */
  recent(channelId: string, n = 5): ReadonlyArray<ChannelMessage> {
    const arr = this.map.get(channelId);
    if (!arr || arr.length === 0) return [];
    if (n >= arr.length) return arr.slice();
    return arr.slice(arr.length - n);
  }

  /** Diagnostic — total messages held across all channels. */
  size(): number {
    let total = 0;
    for (const arr of this.map.values()) total += arr.length;
    return total;
  }
}
