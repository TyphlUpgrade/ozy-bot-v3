/**
 * CW-3 — InMemoryMessageContext unit tests.
 *
 * Covers:
 *   1. record + resolve happy path
 *   2. resolve miss returns null
 *   3. LRU eviction at maxEntries (oldest dropped, newer retained)
 *
 * Wave E-β extends with role-head tests (record/lookup, TTL stale eviction,
 * LRU bound per-map, channel mismatch).
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

describe("InMemoryMessageContext — Wave E-β role-head", () => {
  it("records a role-head and looks it up by (projectId, role, channel)", () => {
    const ctx = new InMemoryMessageContext();
    ctx.recordRoleMessage("proj-a", "architect", "msg-100", "ops");
    expect(ctx.lookupRoleHead("proj-a", "architect", "ops")).toBe("msg-100");
  });

  it("lookupRoleHead returns null on miss without recording anything", () => {
    const ctx = new InMemoryMessageContext();
    expect(ctx.lookupRoleHead("proj-a", "architect", "ops")).toBeNull();
    expect(ctx.roleHeadsSize).toBe(0);
  });

  it("re-recording the same (projectId, role, channel) replaces the head", () => {
    const ctx = new InMemoryMessageContext();
    ctx.recordRoleMessage("proj-a", "architect", "msg-1", "ops");
    ctx.recordRoleMessage("proj-a", "architect", "msg-2", "ops");
    expect(ctx.lookupRoleHead("proj-a", "architect", "ops")).toBe("msg-2");
    expect(ctx.roleHeadsSize).toBe(1);
  });

  it("returns null + evicts entry when older than staleChainMs", () => {
    let now = 1_000_000;
    const ctx = new InMemoryMessageContext({ staleChainMs: 1000, now: () => now });
    ctx.recordRoleMessage("proj-a", "executor", "msg-x", "ops");
    expect(ctx.lookupRoleHead("proj-a", "executor", "ops")).toBe("msg-x");

    // Advance past TTL boundary.
    now = 1_000_000 + 1001;
    expect(ctx.lookupRoleHead("proj-a", "executor", "ops")).toBeNull();
    // Stale entry must have been evicted.
    expect(ctx.roleHeadsSize).toBe(0);
  });

  it("lookup with a different channel returns null (channel embedded in key)", () => {
    const ctx = new InMemoryMessageContext();
    ctx.recordRoleMessage("proj-a", "architect", "msg-1", "ops");
    expect(ctx.lookupRoleHead("proj-a", "architect", "escalation")).toBeNull();
    expect(ctx.lookupRoleHead("proj-a", "architect", "ops")).toBe("msg-1");
  });

  it("lookup with a different role returns null", () => {
    const ctx = new InMemoryMessageContext();
    ctx.recordRoleMessage("proj-a", "architect", "msg-1", "ops");
    expect(ctx.lookupRoleHead("proj-a", "executor", "ops")).toBeNull();
  });

  it("LRU bound applies independently per map (practical capacity = 2 * maxEntries)", () => {
    // Per file docstring: agent-message map and role-head map each cap at
    // maxEntries. Filling one to capacity does NOT evict from the other.
    const ctx = new InMemoryMessageContext({ maxEntries: 2 });
    ctx.recordAgentMessage("a1", "p1");
    ctx.recordAgentMessage("a2", "p1");
    ctx.recordRoleMessage("p1", "architect", "r1", "ops");
    ctx.recordRoleMessage("p1", "executor", "r2", "ops");

    expect(ctx.size).toBe(2);
    expect(ctx.roleHeadsSize).toBe(2);

    // Pushing the role-head map past 2 evicts only the oldest role-head, not
    // the agent-message map.
    ctx.recordRoleMessage("p1", "reviewer", "r3", "ops");
    expect(ctx.size).toBe(2);
    expect(ctx.roleHeadsSize).toBe(2);
    expect(ctx.resolveProjectIdForMessage("a1")).toBe("p1");
    // The architect head was the oldest in the role-head map → evicted.
    expect(ctx.lookupRoleHead("p1", "architect", "ops")).toBeNull();
    expect(ctx.lookupRoleHead("p1", "executor", "ops")).toBe("r2");
    expect(ctx.lookupRoleHead("p1", "reviewer", "ops")).toBe("r3");
  });

  it.todo("notifier chain decisions integration — commit 2");
});
