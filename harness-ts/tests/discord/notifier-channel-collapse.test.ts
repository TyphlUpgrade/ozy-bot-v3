/**
 * Channel-collapse commit 2 — single-channel routing + operator-mention prepend
 * + noise event suppression. Asserts that:
 *   1. Every emitting event (incl. previously ops/esc routed) lands in dev_channel.
 *   2. Escalation-class events prepend `<@operator_user_id>` and override
 *      allowedMentions to actually ping when operator_user_id is configured.
 *      Without operator_user_id, behavior degrades silently (no prepend, no
 *      mentions override).
 *   3. Noise events (task_picked_up, retry_scheduled, project_declared,
 *      architect_spawned, architect_respawned, compaction_fired,
 *      response_level, task_shelved) are suppressed entirely (no sender call).
 *   4. E-β reply threading still works post-collapse (project_decomposed
 *      registers a head; arbitration_verdict in the same channel chains under it).
 */

import { describe, it, expect } from "vitest";
import { DiscordNotifier } from "../../src/discord/notifier.js";
import {
  type AllowedMentions,
  type DiscordSender,
  type AgentIdentity,
} from "../../src/discord/types.js";
import { InMemoryMessageContext } from "../../src/discord/message-context.js";
import type { DiscordConfig } from "../../src/lib/config.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";
import type { StateManager } from "../../src/lib/state.js";

interface SentRecord {
  channel: string;
  content: string;
  identity?: AgentIdentity;
  replyToMessageId?: string;
  allowedMentions?: AllowedMentions;
  returnedId: string | null;
}

function makeRecordingSender(): { sender: DiscordSender; sent: SentRecord[] } {
  const sent: SentRecord[] = [];
  let counter = 0;
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity, replyToMessageId, allowedMentions) {
      counter += 1;
      sent.push({ channel, content, identity, replyToMessageId, allowedMentions, returnedId: null });
    },
    async sendToChannelAndReturnId(channel, content, identity, replyToMessageId, allowedMentions) {
      counter += 1;
      const returnedId = `cc-msg-${counter}`;
      sent.push({ channel, content, identity, replyToMessageId, allowedMentions, returnedId });
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

const OPERATOR_ID = "249313669337317379";

describe("channel-collapse — single-channel routing", () => {
  it("every emitting event routes to dev_channel (no ops/esc traffic)", async () => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig());
    // Emit one of every emitting (non-suppressed) event type. Suppressed
    // events are excluded (separate test below asserts they emit nothing).
    const events: OrchestratorEvent[] = [
      { type: "session_complete", taskId: "t1", success: true, errors: [] },
      { type: "merge_result", taskId: "t1", result: { status: "merged", commitSha: "abc" } },
      { type: "task_done", taskId: "t1" },
      { type: "task_failed", taskId: "t1", reason: "boom", attempt: 1 },
      { type: "escalation_needed", taskId: "t1", escalation: { type: "clarification_needed", question: "?" } },
      { type: "budget_exhausted", taskId: "t1", totalCostUsd: 1.0 },
      { type: "project_decomposed", projectId: "p1", phaseCount: 2 },
      { type: "project_completed", projectId: "p1", phaseCount: 2, totalCostUsd: 1.0 },
      { type: "project_failed", projectId: "p1", reason: "boom" },
      { type: "project_aborted", projectId: "p1", operatorId: "op1" },
      { type: "architect_arbitration_fired", taskId: "t1", projectId: "p1", cause: "escalation" },
      { type: "arbitration_verdict", taskId: "t1", projectId: "p1", verdict: "plan_amendment", rationale: "r" },
      { type: "review_arbitration_entered", taskId: "t1", projectId: "p1", reviewerRejectionCount: 1 },
      { type: "review_mandatory", taskId: "t1", projectId: "p1" },
      { type: "budget_ceiling_reached", projectId: "p1", currentCostUsd: 9, ceilingUsd: 10 },
      { type: "session_stalled", taskId: "t1", tier: "executor", lastActivityAt: 0, stalledForMs: 60000, aborted: true },
    ];
    for (const ev of events) notifier.handleEvent(ev);
    await flush();

    expect(sent.length).toBe(events.length);
    for (const s of sent) {
      expect(s.channel).toBe("dev");
    }
  });
});

describe("channel-collapse — operator-mention prepend", () => {
  it("escalation_needed with operator_user_id set → body starts with mention, allowedMentions = users", async () => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig({ operator_user_id: OPERATOR_ID }));
    notifier.handleEvent({
      type: "escalation_needed",
      taskId: "t1",
      escalation: { type: "clarification_needed", question: "what scope?" },
    });
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].content.startsWith(`<@${OPERATOR_ID}> `)).toBe(true);
    expect(sent[0].allowedMentions).toEqual({ users: [OPERATOR_ID] });
  });

  it("escalation_needed without operator_user_id → body unchanged, allowedMentions undefined", async () => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig());
    notifier.handleEvent({
      type: "escalation_needed",
      taskId: "t1",
      escalation: { type: "clarification_needed", question: "what scope?" },
    });
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].content.startsWith("<@")).toBe(false);
    expect(sent[0].allowedMentions).toBeUndefined();
  });

  it("task_failed with terminal=false → no mention (non-terminal)", async () => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig({ operator_user_id: OPERATOR_ID }));
    notifier.handleEvent({ type: "task_failed", taskId: "t1", reason: "boom", attempt: 1, terminal: false });
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].content.startsWith("<@")).toBe(false);
    expect(sent[0].allowedMentions).toBeUndefined();
  });

  it("task_failed with terminal=true → mention applied (terminal)", async () => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig({ operator_user_id: OPERATOR_ID }));
    notifier.handleEvent({ type: "task_failed", taskId: "t1", reason: "boom", attempt: 3, terminal: true });
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].content.startsWith(`<@${OPERATOR_ID}> `)).toBe(true);
    expect(sent[0].allowedMentions).toEqual({ users: [OPERATOR_ID] });
  });

  it("task_failed with terminal undefined → no mention (defaults to non-terminal)", async () => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig({ operator_user_id: OPERATOR_ID }));
    notifier.handleEvent({ type: "task_failed", taskId: "t1", reason: "boom", attempt: 3 });
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].content.startsWith("<@")).toBe(false);
    expect(sent[0].allowedMentions).toBeUndefined();
  });

  // Each ALWAYS_OPERATOR_ATTENTION event type tested individually so
  // future regressions to the set are caught at the per-event level.
  it.each<[OrchestratorEvent["type"], OrchestratorEvent]>([
    ["escalation_needed", { type: "escalation_needed", taskId: "t1", escalation: { type: "clarification_needed", question: "?" } }],
    ["review_arbitration_entered", { type: "review_arbitration_entered", taskId: "t1", projectId: "p1", reviewerRejectionCount: 1 }],
    ["budget_ceiling_reached", { type: "budget_ceiling_reached", projectId: "p1", currentCostUsd: 9, ceilingUsd: 10 }],
    ["project_failed", { type: "project_failed", projectId: "p1", reason: "boom" }],
    ["project_aborted", { type: "project_aborted", projectId: "p1", operatorId: "op1" }],
    ["session_stalled", { type: "session_stalled", taskId: "t1", tier: "executor", lastActivityAt: 0, stalledForMs: 60000, aborted: true }],
  ])("ALWAYS_OPERATOR_ATTENTION: %s prepends mention + sets users allowedMentions", async (_label, event) => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig({ operator_user_id: OPERATOR_ID }));
    notifier.handleEvent(event);
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].content.startsWith(`<@${OPERATOR_ID}> `)).toBe(true);
    expect(sent[0].allowedMentions).toEqual({ users: [OPERATOR_ID] });
  });
});

describe("channel-collapse — noise event suppression", () => {
  it.each<[string, OrchestratorEvent]>([
    ["task_picked_up", { type: "task_picked_up", taskId: "t1", prompt: "fix bug" }],
    ["retry_scheduled", { type: "retry_scheduled", taskId: "t1", attempt: 2, maxRetries: 3 }],
    ["project_declared", { type: "project_declared", projectId: "p1", name: "x" }],
    ["architect_spawned", { type: "architect_spawned", projectId: "p1", sessionId: "s1" }],
    ["architect_respawned", { type: "architect_respawned", projectId: "p1", sessionId: "s1", reason: "compaction" }],
    ["compaction_fired", { type: "compaction_fired", projectId: "p1", generation: 1 }],
    ["response_level (level 2)", { type: "response_level", taskId: "t1", level: 2, name: "reviewed", reasons: ["x"] }],
    ["task_shelved", { type: "task_shelved", taskId: "t1", reason: "rebase conflict" }],
  ])("%s is suppressed at the notifier (sender NOT called)", async (_label, event) => {
    const { sender, sent } = makeRecordingSender();
    const notifier = new DiscordNotifier(sender, baseConfig({ operator_user_id: OPERATOR_ID }));
    notifier.handleEvent(event);
    await flush();
    expect(sent).toHaveLength(0);
  });
});

describe("channel-collapse — E-β reply threading still works post-collapse", () => {
  it("project_decomposed (head) → arbitration_verdict (replies under architect head, same channel)", async () => {
    const { sender, sent } = makeRecordingSender();
    const ctx = new InMemoryMessageContext();
    const state = makeFakeStateManager({ "task-arb": "P1" });
    const notifier = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: state });

    // project_decomposed registers an architect head in dev_channel (the only
    // channel post-collapse). Standalone — no replyToMessageId.
    notifier.handleEvent({ type: "project_decomposed", projectId: "P1", phaseCount: 1 });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].replyToMessageId).toBeUndefined();
    expect(sent[0].returnedId).toBe("cc-msg-1");
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("cc-msg-1");

    // arbitration_verdict (channel-collapsed to dev) chains under the
    // architect head registered above — no cross-channel jump anymore.
    notifier.handleEvent({
      type: "arbitration_verdict",
      taskId: "task-arb",
      projectId: "P1",
      verdict: "retry_with_directive",
      rationale: "tighten",
    });
    await flush();

    expect(sent[1].channel).toBe("dev");
    expect(sent[1].replyToMessageId).toBe("cc-msg-1");
    // Re-registers architect head with the verdict's id.
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("cc-msg-2");
  });
});
