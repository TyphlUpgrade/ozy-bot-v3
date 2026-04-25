/**
 * CW-3 — InMemoryMessageContext unit tests.
 *
 * Covers:
 *   1. record + resolve happy path
 *   2. resolve miss returns null
 *   3. LRU eviction at maxEntries (oldest dropped, newer retained)
 */

import { describe, it, expect } from "vitest";
import { InMemoryMessageContext } from "../../src/discord/message-context.js";

describe("InMemoryMessageContext", () => {
  it("records a messageId and resolves back to its projectId", () => {
    const ctx = new InMemoryMessageContext();
    ctx.recordAgentMessage("msg-1", "proj-a");
    expect(ctx.resolveProjectIdForMessage("msg-1")).toBe("proj-a");
  });

  it("returns null on a miss", () => {
    const ctx = new InMemoryMessageContext();
    expect(ctx.resolveProjectIdForMessage("never-recorded")).toBeNull();
  });

  it("evicts oldest entries (LRU) when over maxEntries", () => {
    const ctx = new InMemoryMessageContext({ maxEntries: 3 });
    ctx.recordAgentMessage("m1", "p1");
    ctx.recordAgentMessage("m2", "p2");
    ctx.recordAgentMessage("m3", "p3");
    expect(ctx.size).toBe(3);

    // Adding a 4th evicts the oldest (m1).
    ctx.recordAgentMessage("m4", "p4");
    expect(ctx.size).toBe(3);
    expect(ctx.resolveProjectIdForMessage("m1")).toBeNull();
    expect(ctx.resolveProjectIdForMessage("m2")).toBe("p2");
    expect(ctx.resolveProjectIdForMessage("m3")).toBe("p3");
    expect(ctx.resolveProjectIdForMessage("m4")).toBe("p4");
  });

});
