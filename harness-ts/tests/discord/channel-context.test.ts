/**
 * CW-4.5 — ChannelContextBuffer unit tests (5 tests per plan §10).
 *
 * Covers:
 *   1. append/recent ordering (chronological, fields preserved, empty miss)
 *   2. per-channel ring evicts oldest at cap
 *   3. max-channels LRU eviction (Test 4: 491 total) + bonus existing-channel append (Test 4b: 500)
 *   4. timestamp/projectIdHint preserved
 *   5. recent(n) returns only the last n entries
 */

import { describe, it, expect } from "vitest";
import {
  ChannelContextBuffer,
  type ChannelMessage,
} from "../../src/discord/channel-context.js";

function msg(author: string, content: string, hint?: string): ChannelMessage {
  return {
    author,
    content,
    timestamp: "2026-04-24T00:00:00.000Z",
    projectIdHint: hint,
  };
}

describe("ChannelContextBuffer", () => {
  it("append/recent returns messages oldest→newest, empty miss returns []", () => {
    const buf = new ChannelContextBuffer();
    expect(buf.recent("never-touched")).toEqual([]);

    buf.append("ch1", msg("alice", "first", "p1"));
    buf.append("ch1", msg("bob", "second"));
    buf.append("ch1", msg("carol", "third", "p2"));

    const out = buf.recent("ch1", 5);
    expect(out).toHaveLength(3);
    expect(out.map((m) => m.author)).toEqual(["alice", "bob", "carol"]);
    expect(out.map((m) => m.content)).toEqual(["first", "second", "third"]);
  });

  it("per-channel ring evicts oldest when perChannelCap exceeded", () => {
    const buf = new ChannelContextBuffer({ perChannelCap: 2 });
    buf.append("ch1", msg("a", "A"));
    buf.append("ch1", msg("b", "B"));
    buf.append("ch1", msg("c", "C"));

    const out = buf.recent("ch1", 5);
    expect(out).toHaveLength(2);
    expect(out.map((m) => m.content)).toEqual(["B", "C"]);
  });

  it("max-channels LRU eviction — Test 4 (overflow new) → 491 total; Test 4b (overflow existing) → 500", () => {
    const buf = new ChannelContextBuffer({ perChannelCap: 10, maxChannels: 50 });
    // Fill 50 channels × 10 msgs = 500 total.
    for (let c = 0; c < 50; c++) {
      for (let m = 0; m < 10; m++) {
        buf.append(`ch-${c}`, msg("u", `msg-${c}-${m}`));
      }
    }
    expect(buf.size()).toBe(500);

    // Test 4 — append to a NEW (51st) channel evicts oldest channel whole (10 msgs gone, 1 added) → 491.
    buf.append("ch-50", msg("u", "fresh"));
    expect(buf.size()).toBe(491);
    expect(buf.recent("ch-0")).toEqual([]);
    expect(buf.recent("ch-50")).toHaveLength(1);

    // Test 4b — append to an EXISTING channel: in-array shift drops 1, push adds 1 → still 491.
    // (We're now at 491 because ch-0 was evicted; appending to ch-1 -- 10 msgs already -- shifts oldest, pushes new.)
    buf.append("ch-1", msg("u", "fresh-1"));
    expect(buf.size()).toBe(491);
    const ch1 = buf.recent("ch-1", 10);
    expect(ch1).toHaveLength(10);
    expect(ch1[9].content).toBe("fresh-1");
  });

  it("timestamp + projectIdHint preserved across append/recent", () => {
    const buf = new ChannelContextBuffer();
    const m: ChannelMessage = {
      author: "alice",
      content: "hi",
      timestamp: "2026-04-24T01:02:03.456Z",
      projectIdHint: "proj-A",
    };
    buf.append("ch1", m);
    const out = buf.recent("ch1");
    expect(out).toHaveLength(1);
    expect(out[0].timestamp).toBe("2026-04-24T01:02:03.456Z");
    expect(out[0].projectIdHint).toBe("proj-A");
  });

  it("recent(n) returns only the last n entries", () => {
    const buf = new ChannelContextBuffer();
    for (let i = 0; i < 5; i++) buf.append("ch1", msg("u", `m-${i}`));
    const out = buf.recent("ch1", 3);
    expect(out).toHaveLength(3);
    expect(out.map((m) => m.content)).toEqual(["m-2", "m-3", "m-4"]);
  });
});
