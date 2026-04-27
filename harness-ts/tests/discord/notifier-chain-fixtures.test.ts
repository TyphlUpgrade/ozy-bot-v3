/**
 * Wave E-β commit 2 — end-to-end notifier chain fixtures.
 *
 * Each test emits a sequence of OrchestratorEvents and verifies the
 * `replyToMessageId` field passed to the underlying DiscordSender wires
 * through correctly per the CHAIN_RULES table in `src/discord/notifier.ts`.
 *
 * Conventions:
 *   - The fake sender records every (channel, content, replyToMessageId, returned id).
 *   - Returned ids are deterministic ("msg-1", "msg-2", ...) so chained messages
 *     can be cross-referenced by ordinal.
 *   - A shared InMemoryMessageContext lets the notifier stitch chains across
 *     events (record on send → lookup on next event).
 *   - `vi.resetModules()` clears the module-private restartWarned flag between
 *     tests that exercise the warn-once path.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DiscordSender, AgentIdentity } from "../../src/discord/types.js";
import type { DiscordConfig } from "../../src/lib/config.js";
import type { StateManager } from "../../src/lib/state.js";

interface SentRecord {
  channel: string;
  content: string;
  identity?: AgentIdentity;
  replyToMessageId?: string;
  returnedId: string | null;
}

function makeRecordingSender(opts: { idPrefix?: string } = {}): { sender: DiscordSender; sent: SentRecord[] } {
  const prefix = opts.idPrefix ?? "msg";
  const sent: SentRecord[] = [];
  let counter = 0;
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity, replyToMessageId) {
      counter += 1;
      sent.push({ channel, content, identity, replyToMessageId, returnedId: null });
    },
    async sendToChannelAndReturnId(channel, content, identity, replyToMessageId) {
      counter += 1;
      const returnedId = `${prefix}-${counter}`;
      sent.push({ channel, content, identity, replyToMessageId, returnedId });
      return { messageId: returnedId };
    },
    async addReaction() {
      /* no-op */
    },
  };
  return { sender, sent };
}

function baseConfig(overrides: Partial<DiscordConfig> = {}): DiscordConfig {
  return {
    bot_token_env: "T",
    dev_channel: "dev",
    ops_channel: "ops",
    escalation_channel: "esc",
    agents: {
      orchestrator: { name: "Harness", avatar_url: "" },
      architect: { name: "Architect", avatar_url: "" },
      reviewer: { name: "Reviewer", avatar_url: "" },
      executor: { name: "Executor", avatar_url: "" },
    },
    ...overrides,
  };
}

function makeFakeStateManager(taskToProject: Record<string, string>): StateManager {
  return {
    getTask(taskId: string) {
      const projectId = taskToProject[taskId];
      if (!projectId) return undefined;
      return { id: taskId, projectId };
    },
  } as unknown as StateManager;
}

async function flush(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

describe("DiscordNotifier — Wave E-β chain fixtures (commit 2)", () => {
  beforeEach(() => {
    // Reset the module-private restartWarned flag so warn-once tests are
    // independent. `vi.resetModules` forces the next dynamic `import` to
    // re-evaluate the notifier module.
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("architect chain: project_decomposed (standalone) → arbitration_verdict (replies + re-registers)", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const { sender, sent } = makeRecordingSender();
    const ctx = new InMemoryMessageContext();
    const state = makeFakeStateManager({ "task-arb": "P1" });
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: state });

    notifier.handleEvent({ type: "project_decomposed", projectId: "P1", phaseCount: 3 });
    await flush();
    expect(sent[0].replyToMessageId).toBeUndefined(); // standalone
    expect(sent[0].returnedId).toBe("msg-1");
    // Architect head registered in dev_channel.
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("msg-1");

    // Channel-collapse: arbitration_verdict and architect_arbitration_fired
    // both route to dev_channel now. The architect head from project_decomposed
    // is in dev_channel, so the verdict can chain under it directly without
    // needing architect_arbitration_fired to bootstrap a separate ops head.
    notifier.handleEvent({
      type: "arbitration_verdict",
      taskId: "task-arb",
      projectId: "P1",
      verdict: "retry_with_directive",
      rationale: "tighten",
    });
    await flush();
    expect(sent[1].channel).toBe("dev");
    expect(sent[1].replyToMessageId).toBe("msg-1"); // chains under project_decomposed head
    // Re-registers architect head with the verdict's id.
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("msg-2");
  });

  it("executor chain under architect: project_decomposed → session_complete → merge_result → task_done", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const { sender, sent } = makeRecordingSender();
    const ctx = new InMemoryMessageContext();
    const state = makeFakeStateManager({ "task-7": "P1" });
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: state });

    notifier.handleEvent({ type: "project_decomposed", projectId: "P1", phaseCount: 1 });
    await flush();
    notifier.handleEvent({ type: "session_complete", taskId: "task-7", success: true, errors: [] });
    await flush();
    notifier.handleEvent({ type: "merge_result", taskId: "task-7", result: { status: "merged", commitSha: "deadbeef" } });
    await flush();
    notifier.handleEvent({ type: "task_done", taskId: "task-7" });
    await flush();

    expect(sent).toHaveLength(4);
    expect(sent[0].replyToMessageId).toBeUndefined();    // project_decomposed standalone
    expect(sent[1].replyToMessageId).toBe("msg-1");       // session_complete → architect head
    expect(sent[2].replyToMessageId).toBe("msg-2");       // merge_result → executor head
    expect(sent[3].replyToMessageId).toBe("msg-3");       // task_done → executor head (re-registered)
    expect(ctx.lookupRoleHead("P1", "executor", "dev")).toBe("msg-4");
  });

  it("stale TTL: head older than staleChainMs → standalone fallback + restart-warn fires once", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const consoleSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    let now = 1_000_000;
    const ctx = new InMemoryMessageContext({ staleChainMs: 1000, now: () => now });
    const { sender, sent } = makeRecordingSender();
    const state = makeFakeStateManager({ "task-arb": "P1" });
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: state });

    // Channel-collapse: architect_arbitration_fired now routes to dev_channel
    // (single channel for all events). Register architect head in dev so
    // arbitration_verdict can attempt chain.
    notifier.handleEvent({ type: "architect_arbitration_fired", taskId: "task-arb", projectId: "P1", cause: "escalation" });
    await flush();
    expect(sent[0].replyToMessageId).toBeUndefined();
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("msg-1");

    // Advance well past the TTL.
    now = 1_000_000 + 1100;

    notifier.handleEvent({
      type: "arbitration_verdict",
      taskId: "task-arb",
      projectId: "P1",
      verdict: "retry_with_directive",
      rationale: "rebuild",
    });
    await flush();
    expect(sent[1].replyToMessageId).toBeUndefined(); // stale → standalone
    expect(consoleSpy).toHaveBeenCalledTimes(1);
    expect(String(consoleSpy.mock.calls[0][0])).toMatch(/reply-chain head missing/);
  });

  it("cross-channel guard: pre-seeded head in different channel → standalone (heads are per-channel)", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const consoleSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const ctx = new InMemoryMessageContext();
    const { sender, sent } = makeRecordingSender();
    const state = makeFakeStateManager({ "task-rev": "P1" });
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: state });

    // Channel-collapse: post-collapse the notifier only sends to dev_channel
    // so cross-channel routing is no longer triggered organically. Pre-seed an
    // executor head in a foreign channel ("ops") to exercise the per-channel
    // discipline of the role-head map directly.
    ctx.recordRoleMessage("P1", "executor", "stale-foreign-head", "ops");

    // review_arbitration_entered (channel-collapsed → dev) chains under
    // executor head. With nothing in dev_channel, the lookup misses despite
    // the pre-seeded head sitting in ops — heads are per-channel.
    notifier.handleEvent({
      type: "review_arbitration_entered",
      taskId: "task-rev",
      projectId: "P1",
      reviewerRejectionCount: 1,
    });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].replyToMessageId).toBeUndefined();
    // Reviewer head registered in the SEND channel (dev), not ops.
    expect(ctx.lookupRoleHead("P1", "reviewer", "dev")).toBe("msg-1");
    expect(ctx.lookupRoleHead("P1", "reviewer", "ops")).toBeNull();
    consoleSpy.mockRestore();
  });

  it("disabled flag: reply_threading.enabled=false → no replyToMessageId ever set, no role-head registered", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const ctx = new InMemoryMessageContext();
    const { sender, sent } = makeRecordingSender();
    const state = makeFakeStateManager({ "task-1": "P1" });
    const notifier = new DiscordNotifier(
      sender,
      baseConfig({ reply_threading: { enabled: false } }),
      { messageContext: ctx, stateManager: state },
    );

    notifier.handleEvent({ type: "project_decomposed", projectId: "P1", phaseCount: 2 });
    await flush();
    notifier.handleEvent({ type: "session_complete", taskId: "task-1", success: true, errors: [] });
    await flush();
    notifier.handleEvent({ type: "merge_result", taskId: "task-1", result: { status: "merged", commitSha: "abc" } });
    await flush();
    notifier.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    for (const r of sent) {
      expect(r.replyToMessageId).toBeUndefined();
    }
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBeNull();
    expect(ctx.lookupRoleHead("P1", "executor", "dev")).toBeNull();
  });

  it("session_stalled tier-aware: replies to head matching event.tier (architect)", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const ctx = new InMemoryMessageContext();
    const { sender, sent } = makeRecordingSender();
    const state = makeFakeStateManager({ "task-stall": "P1" });
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: state });

    // Channel-collapse: architect_arbitration_fired and session_stalled both
    // route to dev_channel. Register architect head in dev so the tier-keyed
    // lookup on session_stalled hits the same channel.
    notifier.handleEvent({ type: "architect_arbitration_fired", taskId: "task-stall", projectId: "P1", cause: "escalation" });
    await flush();
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("msg-1");

    notifier.handleEvent({
      type: "session_stalled",
      taskId: "task-stall",
      tier: "architect",
      lastActivityAt: 0,
      stalledForMs: 600_000,
      aborted: true,
    });
    await flush();
    expect(sent[1].channel).toBe("dev");
    expect(sent[1].replyToMessageId).toBe("msg-1");
    // session_stalled does NOT register a new head (per plan B5).
    // Architect head still points at msg-1, not msg-2.
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("msg-1");
  });

  it("no projectId: standalone events (poll_tick, shutdown) bypass chain logic and use plain sendToChannel", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const ctx = new InMemoryMessageContext();
    // Spy lookupRoleHead to ensure it is never invoked for these events.
    let lookups = 0;
    const wrappedCtx = {
      recordAgentMessage: ctx.recordAgentMessage.bind(ctx),
      resolveProjectIdForMessage: ctx.resolveProjectIdForMessage.bind(ctx),
      recordRoleMessage: ctx.recordRoleMessage.bind(ctx),
      lookupRoleHead: (...args: Parameters<typeof ctx.lookupRoleHead>): string | null => {
        lookups += 1;
        return ctx.lookupRoleHead(...args);
      },
    };
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: wrappedCtx });

    notifier.handleEvent({ type: "poll_tick" });
    notifier.handleEvent({ type: "shutdown" });
    await flush();

    // poll_tick + shutdown are silently ignored (not in NOTIFIER_MAP).
    expect(sent).toHaveLength(0);
    expect(lookups).toBe(0);
  });

  it("restart simulation: fresh InMemoryMessageContext + chain-rule event with no head → standalone + warn-once", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const consoleSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const ctx = new InMemoryMessageContext(); // fresh — no heads
    const { sender, sent } = makeRecordingSender();
    const state = makeFakeStateManager({ "task-orphan": "P1" });
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: state });

    // arbitration_verdict expects an architect head; none exists post-restart.
    // Channel-collapse: arbitration_verdict now routes to dev_channel.
    notifier.handleEvent({
      type: "arbitration_verdict",
      taskId: "task-orphan",
      projectId: "P1",
      verdict: "plan_amendment",
      rationale: "first event after restart",
    });
    await flush();
    expect(sent[0].replyToMessageId).toBeUndefined();
    // Re-registers architect head from this verdict in the SEND channel (dev).
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("msg-1");
    // Warn fired exactly once.
    expect(consoleSpy).toHaveBeenCalledTimes(1);

    // A second missing-head event must NOT log again. session_complete chains
    // under executor head — none exists, but verdict already re-registered the
    // architect head so the chain rule lookup misses on executor specifically.
    // Use a different task to avoid re-using the same chain.
    notifier.handleEvent({
      type: "merge_result",
      taskId: "task-orphan",
      result: { status: "merged", commitSha: "abc" },
    });
    await flush();
    // merge_result chains under executor head — none exists in dev.
    expect(sent[1].replyToMessageId).toBeUndefined();
    expect(consoleSpy).toHaveBeenCalledTimes(1); // still 1 (warn-once)
  });
});
