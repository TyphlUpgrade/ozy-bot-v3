import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { MessageAccumulator } from "../../src/discord/accumulator.js";

describe("MessageAccumulator", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("debounces and concatenates rapid NL messages from same (user, channel)", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator(
      (u, c, t) => flushes.push([u, c, t]),
      { debounceMs: 100 },
    );
    acc.push("u1", "c1", "hello");
    acc.push("u1", "c1", "world");
    vi.advanceTimersByTime(100);
    expect(flushes).toHaveLength(1);
    expect(flushes[0]).toEqual(["u1", "c1", "hello world"]);
  });

  it("'!' command bypasses debounce — flushed immediately", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator((u, c, t) => flushes.push([u, c, t]), { debounceMs: 100 });
    acc.push("u1", "c1", "!task add validation");
    // Flushed synchronously, no timer advance required
    expect(flushes).toEqual([["u1", "c1", "!task add validation"]]);
  });

  it("'!' command flushes any pending NL from same (user, channel) first, then the command", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator((u, c, t) => flushes.push([u, c, t]), { debounceMs: 100 });
    acc.push("u1", "c1", "partial thought");
    acc.push("u1", "c1", "!status");
    expect(flushes).toEqual([
      ["u1", "c1", "partial thought"],
      ["u1", "c1", "!status"],
    ]);
  });

  it("different users tracked independently", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator((u, c, t) => flushes.push([u, c, t]), { debounceMs: 100 });
    acc.push("u1", "c1", "from user1");
    acc.push("u2", "c1", "from user2");
    vi.advanceTimersByTime(100);
    expect(flushes).toHaveLength(2);
    expect(flushes).toContainEqual(["u1", "c1", "from user1"]);
    expect(flushes).toContainEqual(["u2", "c1", "from user2"]);
  });

  it("different channels tracked independently for same user", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator((u, c, t) => flushes.push([u, c, t]), { debounceMs: 100 });
    acc.push("u1", "c1", "in channel 1");
    acc.push("u1", "c2", "in channel 2");
    vi.advanceTimersByTime(100);
    expect(flushes).toHaveLength(2);
    expect(flushes).toContainEqual(["u1", "c1", "in channel 1"]);
    expect(flushes).toContainEqual(["u1", "c2", "in channel 2"]);
  });

  it("timer resets on each new push from same (user, channel)", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator((u, c, t) => flushes.push([u, c, t]), { debounceMs: 100 });
    acc.push("u1", "c1", "first");
    vi.advanceTimersByTime(50);
    expect(flushes).toHaveLength(0);
    acc.push("u1", "c1", "second");
    vi.advanceTimersByTime(50);
    // Only 50ms since 'second' — not flushed yet because timer reset
    expect(flushes).toHaveLength(0);
    vi.advanceTimersByTime(50);
    expect(flushes).toHaveLength(1);
    expect(flushes[0][2]).toBe("first second");
  });

  it("flushAll() drains every pending (user, channel) immediately", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator((u, c, t) => flushes.push([u, c, t]), { debounceMs: 100 });
    acc.push("u1", "c1", "pending a");
    acc.push("u2", "c1", "pending b");
    acc.push("u1", "c2", "pending c");
    expect(acc.pendingCount).toBe(3);
    acc.flushAll();
    expect(flushes).toHaveLength(3);
    expect(acc.pendingCount).toBe(0);
  });

  it("flushAll() with nothing pending is a no-op", () => {
    const flushes: Array<[string, string, string]> = [];
    const acc = new MessageAccumulator((u, c, t) => flushes.push([u, c, t]), { debounceMs: 100 });
    acc.flushAll();
    expect(flushes).toHaveLength(0);
  });
});
