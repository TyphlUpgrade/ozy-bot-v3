import { describe, it, expect, beforeEach, vi } from "vitest";
import { DiscordNotifier, sanitize, redactSecrets } from "../../src/discord/notifier.js";
import { sendToChannelAndReturnIdDefault, type DiscordSender, type AgentIdentity } from "../../src/discord/types.js";
import type { DiscordConfig } from "../../src/lib/config.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";

// --- Fakes ---

interface Recorded {
  channel: string;
  content: string;
  identity?: AgentIdentity;
}

function makeFakeSender(failSend = false) {
  const sent: Recorded[] = [];
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity) {
      if (failSend) throw new Error("discord down");
      sent.push({ channel, content, identity });
    },
    async sendToChannelAndReturnId(channel, content, identity) {
      return sendToChannelAndReturnIdDefault(this, channel, content, identity);
    },
    async addReaction() {
      /* no-op */
    },
  };
  return { sender, sent };
}

function baseConfig(): DiscordConfig {
  return {
    bot_token_env: "T",
    dev_channel: "dev",
    ops_channel: "ops",
    escalation_channel: "esc",
    agents: {
      orchestrator: { name: "Harness", avatar_url: "https://h" },
      architect: { name: "Architect", avatar_url: "https://a" },
      reviewer: { name: "Reviewer", avatar_url: "https://r" },
    },
  };
}

async function flush() {
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

describe("DiscordNotifier", () => {
  let sent: Recorded[];
  let notifier: DiscordNotifier;

  beforeEach(() => {
    const f = makeFakeSender();
    sent = f.sent;
    notifier = new DiscordNotifier(f.sender, baseConfig());
  });

  // --- Phase 2A / standalone events (baseline) ---

  it("task_picked_up → suppressed (channel-collapse: noise event, no emission)", async () => {
    notifier.handleEvent({ type: "task_picked_up", taskId: "task-long-id-xyz-123456", prompt: "fix the auth bug" });
    await flush();
    // Channel-collapse (2026-04-27): task_picked_up has format: () => null.
    // The event remains on the OrchestratorEvent bus for audit trail; only
    // Discord emission is suppressed.
    expect(sent).toHaveLength(0);
  });

  it("session_complete → dev_channel (success + failure)", async () => {
    notifier.handleEvent({ type: "session_complete", taskId: "t1", success: true });
    notifier.handleEvent({ type: "session_complete", taskId: "t2", success: false });
    await flush();
    expect(sent).toHaveLength(2);
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].identity?.username).toBe("Executor");
    expect(sent[0].content).toMatch(/success/);
    expect(sent[1].identity?.username).toBe("Executor");
    expect(sent[1].content).toMatch(/failure/);
  });

  it("merge_result → dev_channel with status", async () => {
    notifier.handleEvent({
      type: "merge_result",
      taskId: "t1",
      result: { status: "merged", commitSha: "abc123" },
    });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/merged/);
  });

  it("task_done → dev_channel", async () => {
    notifier.handleEvent({ type: "task_done", taskId: "t1" });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].identity?.username).toBe("Executor");
  });

  it("task_failed → dev_channel (channel-collapse: all events single channel)", async () => {
    notifier.handleEvent({ type: "task_failed", taskId: "t1", reason: "boom", attempt: 1 });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/FAILED/);
    expect(sent[0].content).toMatch(/boom/);
  });

  it("escalation_needed → dev_channel (channel-collapse: all events single channel)", async () => {
    notifier.handleEvent({
      type: "escalation_needed",
      taskId: "t1",
      escalation: { type: "clarification_needed", question: "what scope?" },
    });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/ESCALATION/);
    expect(sent[0].content).toMatch(/what scope/);
  });

  it("budget_exhausted → dev_channel (channel-collapse: all events single channel)", async () => {
    notifier.handleEvent({ type: "budget_exhausted", taskId: "t1", totalCostUsd: 4.2 });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/\$4\.20/);
  });

  it("retry_scheduled → suppressed (channel-collapse: retry cadence noise)", async () => {
    notifier.handleEvent({ type: "retry_scheduled", taskId: "t1", attempt: 2, maxRetries: 3 });
    await flush();
    // Channel-collapse: retry_scheduled is suppressed (followed by terminal task_failed).
    expect(sent).toHaveLength(0);
  });

  it("response_level → all levels suppressed (channel-collapse: debug-only signal)", async () => {
    notifier.handleEvent({ type: "response_level", taskId: "t1", level: 0, name: "direct", reasons: [] });
    notifier.handleEvent({ type: "response_level", taskId: "t2", level: 1, name: "enriched", reasons: [] });
    notifier.handleEvent({ type: "response_level", taskId: "t3", level: 2, name: "reviewed", reasons: ["guessing"] });
    await flush();
    // Channel-collapse: response_level is suppressed at all levels.
    expect(sent).toHaveLength(0);
  });

  it("task_shelved → suppressed (channel-collapse: cooldown noise)", async () => {
    notifier.handleEvent({ type: "task_shelved", taskId: "t1", reason: "rebase conflict" });
    await flush();
    // Channel-collapse: task_shelved is suppressed (auto-retried by orchestrator).
    expect(sent).toHaveLength(0);
  });

  it("poll_tick / shutdown / checkpoint_detected / completion_compliance are ignored (no emission)", async () => {
    notifier.handleEvent({ type: "poll_tick" });
    notifier.handleEvent({ type: "shutdown" });
    notifier.handleEvent({ type: "checkpoint_detected", taskId: "t1", checkpoints: [] });
    notifier.handleEvent({
      type: "completion_compliance",
      taskId: "t1",
      hasConfidence: false,
      hasUnderstanding: false,
      hasAssumptions: false,
      hasNonGoals: false,
      complianceScore: 0,
    });
    await flush();
    expect(sent).toHaveLength(0);
  });

  it("sender failure is swallowed (pipeline continues)", async () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const f = makeFakeSender(true);
    const n = new DiscordNotifier(f.sender, baseConfig());
    n.handleEvent({ type: "task_done", taskId: "t1" });
    await flush();
    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  // --- Wave 2 three-tier events ---

  it("project_declared → suppressed (channel-collapse: project_decomposed is the operator-relevant signal)", async () => {
    notifier.handleEvent({ type: "project_declared", projectId: "proj-abc", name: "auth-rewrite" });
    await flush();
    // Channel-collapse: project_declared is suppressed; project_decomposed is the operator-relevant signal.
    expect(sent).toHaveLength(0);
  });

  it("project_decomposed → dev_channel with phase count", async () => {
    notifier.handleEvent({ type: "project_decomposed", projectId: "p", phaseCount: 5 });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/5 phase/);
  });

  it("project_completed → dev_channel with cost", async () => {
    notifier.handleEvent({ type: "project_completed", projectId: "p", phaseCount: 4, totalCostUsd: 1.23 });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/\$1\.23/);
  });

  it("project_failed → dev_channel (channel-collapse: all events single channel)", async () => {
    notifier.handleEvent({ type: "project_failed", projectId: "p", reason: "budget ceiling" });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/budget ceiling/);
  });

  it("project_aborted → dev_channel (channel-collapse: all events single channel)", async () => {
    notifier.handleEvent({ type: "project_aborted", projectId: "p", operatorId: "op-1" });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/op-1/);
  });

  it("architect_spawned → suppressed (channel-collapse: lifecycle noise)", async () => {
    notifier.handleEvent({ type: "architect_spawned", projectId: "p", sessionId: "session-uuid-1234" });
    await flush();
    // Channel-collapse: architect_spawned is suppressed (lifecycle noise).
    expect(sent).toHaveLength(0);
  });

  it("architect_respawned → suppressed (channel-collapse: lifecycle noise)", async () => {
    notifier.handleEvent({ type: "architect_respawned", projectId: "p", sessionId: "s", reason: "compaction" });
    await flush();
    // Channel-collapse: architect_respawned is suppressed (lifecycle noise; ops sees this in logs).
    expect(sent).toHaveLength(0);
  });

  it("architect_arbitration_fired → dev_channel (channel-collapse: all events single channel)", async () => {
    notifier.handleEvent({
      type: "architect_arbitration_fired",
      taskId: "t1",
      projectId: "p",
      cause: "review_disagreement",
    });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/review_disagreement/);
  });

  it("arbitration_verdict → dev_channel with verdict + rationale (channel-collapse)", async () => {
    notifier.handleEvent({
      type: "arbitration_verdict",
      taskId: "t1",
      projectId: "p",
      verdict: "retry_with_directive",
      rationale: "add integration test",
    });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/retry_with_directive/);
    expect(sent[0].content).toMatch(/integration test/);
  });

  it("review_arbitration_entered → dev_channel with reviewer identity (channel-collapse)", async () => {
    notifier.handleEvent({
      type: "review_arbitration_entered",
      taskId: "t1",
      projectId: "p",
      reviewerRejectionCount: 2,
    });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].identity?.username).toBe("Reviewer");
    expect(sent[0].content).toMatch(/rejection #2/);
  });

  it("review_mandatory → dev_channel with reviewer identity", async () => {
    notifier.handleEvent({ type: "review_mandatory", taskId: "t1", projectId: "p" });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].identity?.username).toBe("Reviewer");
  });

  it("budget_ceiling_reached → dev_channel with cost + ceiling (channel-collapse)", async () => {
    notifier.handleEvent({
      type: "budget_ceiling_reached",
      projectId: "p",
      currentCostUsd: 9.8,
      ceilingUsd: 10,
    });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].content).toMatch(/\$9\.80/);
    expect(sent[0].content).toMatch(/\$10\.00/);
  });

  it("compaction_fired → suppressed (channel-collapse: internal context-management)", async () => {
    notifier.handleEvent({ type: "compaction_fired", projectId: "p", generation: 3 });
    await flush();
    // Channel-collapse: compaction_fired is suppressed (internal context-management).
    expect(sent).toHaveLength(0);
  });

  // --- Phase B rich rendering (skipped until Commit 2 lands renderer) ---

  it("session_complete failure with errors and terminalReason", async () => {
    notifier.handleEvent({
      type: "session_complete",
      taskId: "t1",
      success: false,
      errors: ["boom1", "boom2"],
      terminalReason: "budget_exceeded",
    });
    await flush();
    expect(sent[0].content).toMatch(/failure — boom1; boom2 \[budget_exceeded\]/);
  });

  it("task_failed renders attempt N from event.attempt", async () => {
    notifier.handleEvent({ type: "task_failed", taskId: "t1", reason: "boom", attempt: 2 });
    await flush();
    expect(sent[0].content).toMatch(/attempt 2/);
    expect(sent[0].content).toMatch(/FAILED/);
    expect(sent[0].content).toMatch(/boom/);
  });

  it("task_done renders response level name when present", async () => {
    notifier.handleEvent({ type: "task_done", taskId: "t1", responseLevelName: "reviewed" });
    await flush();
    expect(sent[0].content).toMatch(/response level: reviewed/);
  });

  it("task_done omits response level when absent", async () => {
    notifier.handleEvent({ type: "task_done", taskId: "t1" });
    await flush();
    expect(sent[0].content).not.toContain("response level:");
  });

  it("escalation_needed renders options + context", async () => {
    notifier.handleEvent({
      type: "escalation_needed",
      taskId: "t1",
      escalation: {
        type: "scope_unclear",
        question: "what scope",
        options: ["a", "b"],
        context: "background details",
      },
    });
    await flush();
    expect(sent[0].content).toContain("Options:");
    expect(sent[0].content).toContain("Context:");
    expect(sent[0].content).toContain("a");
    expect(sent[0].content).toContain("b");
    expect(sent[0].content).toContain("background details");
  });

  it("merge_result rebase_conflict shows file count + first3", async () => {
    notifier.handleEvent({
      type: "merge_result",
      taskId: "t1",
      result: { status: "rebase_conflict", conflictFiles: ["a", "b", "c"] },
    });
    await flush();
    expect(sent[0].content).toMatch(/3 files: a, b, c/);
  });

  it("project_failed renders failedPhase when set", async () => {
    notifier.handleEvent({
      type: "project_failed",
      projectId: "proj-xyz",
      reason: "boom",
      failedPhase: "phase-1",
    });
    await flush();
    expect(sent[0].content).toMatch(/at phase `phase-1`/);
  });

  it("project_failed without failedPhase (spawn-time)", async () => {
    notifier.handleEvent({
      type: "project_failed",
      projectId: "proj-xyz",
      reason: "spawn failed",
    });
    await flush();
    expect(sent[0].content).not.toContain("at phase");
  });

  // --- Identity + defaults ---

  it("falls back to config-free defaults when agents section is empty", async () => {
    const cfg: DiscordConfig = { ...baseConfig(), agents: {} };
    const f = makeFakeSender();
    const n = new DiscordNotifier(f.sender, cfg);
    // Channel-collapse: project_declared is suppressed; use project_decomposed
    // (also architect identity) to exercise the agent identity fallback path.
    n.handleEvent({ type: "project_decomposed", projectId: "p", phaseCount: 1 });
    await flush();
    expect(f.sent[0].identity?.username).toBe("Architect"); // DISCORD_AGENT_DEFAULTS fallback
  });

  // --- Sanitization / injection defense ---

  it("neutralizes @everyone / @here in event content (does not ping channel)", async () => {
    notifier.handleEvent({
      type: "task_failed",
      taskId: "t1",
      reason: "broke because @everyone ignored feedback",
    });
    notifier.handleEvent({
      type: "project_failed",
      projectId: "p1",
      reason: "@here urgent",
    });
    await flush();
    for (const s of sent) {
      // zero-width-joined form is still human-readable but does not trigger Discord ping
      expect(s.content).not.toMatch(/(^|[^​])@everyone/);
      expect(s.content).not.toMatch(/(^|[^​])@here/);
    }
  });

  it("task_picked_up suppression: secret-bearing prompts never reach Discord (channel-collapse)", async () => {
    notifier.handleEvent({
      type: "task_picked_up",
      taskId: "t1",
      prompt: "debug why request with key sk-live-abcdefghijklmnop1234567890 fails auth",
    });
    await flush();
    // Channel-collapse: task_picked_up is suppressed entirely. Defense in depth:
    // even if the upstream redactor missed something, no Discord emission means
    // no leak from this path. The redactSecrets unit tests below cover the
    // function's contract in isolation.
    expect(sent).toHaveLength(0);
  });

  it("escapes backticks in event content (no code-block breakout)", async () => {
    notifier.handleEvent({
      type: "task_failed",
      taskId: "t1",
      reason: "error: `unclosed backtick injection",
    });
    await flush();
    // Backtick must be escaped so the surrounding inline code span stays intact
    expect(sent[0].content).toContain("\\`");
  });

  it("project-related event carries projectId in formatted content", async () => {
    // Channel-collapse (2026-04-27): project_declared, architect_spawned,
    // architect_respawned, compaction_fired are suppressed (format: () => null).
    // Only the operator-relevant project events emit; each must still carry the
    // shortened projectId prefix in its rendered body.
    const emittingProjectEvents: OrchestratorEvent[] = [
      { type: "project_decomposed", projectId: "proj-xyz-2", phaseCount: 1 },
      { type: "project_completed", projectId: "proj-xyz-3", phaseCount: 1, totalCostUsd: 0 },
      { type: "project_failed", projectId: "proj-xyz-4", reason: "r" },
      { type: "project_aborted", projectId: "proj-xyz-5", operatorId: "op" },
      { type: "architect_arbitration_fired", taskId: "t", projectId: "proj-xyz-8", cause: "escalation" },
      {
        type: "arbitration_verdict",
        taskId: "t",
        projectId: "proj-xyz-9",
        verdict: "plan_amendment",
        rationale: "r",
      },
      { type: "review_arbitration_entered", taskId: "t", projectId: "proj-xyza", reviewerRejectionCount: 1 },
      { type: "review_mandatory", taskId: "t", projectId: "proj-xyzb" },
      { type: "budget_ceiling_reached", projectId: "proj-xyzc", currentCostUsd: 1, ceilingUsd: 2 },
    ];
    for (const ev of emittingProjectEvents) notifier.handleEvent(ev);
    await flush();
    expect(sent).toHaveLength(emittingProjectEvents.length);
    // Every emission references the shortened projectId prefix "proj-xyz"
    for (const s of sent) expect(s.content).toMatch(/proj-xyz/);
  });
});

// --- Sanitization helpers (unit-level) ---

describe("sanitize()", () => {
  it("neutralizes @everyone and @here with zero-width joiner", () => {
    const out = sanitize("ping @everyone also @here now");
    expect(out).not.toMatch(/(^|[^​])@everyone/);
    expect(out).not.toMatch(/(^|[^​])@here/);
  });

  it("escapes backticks to prevent code-span breakout", () => {
    expect(sanitize("hello `world`")).toBe("hello \\`world\\`");
  });

  it("truncates strings over maxLen with ellipsis", () => {
    const out = sanitize("x".repeat(600));
    expect(out.length).toBeLessThanOrEqual(501); // 500 + "…"
    expect(out.endsWith("…")).toBe(true);
  });

  it("passes short strings through unchanged (aside from mention/backtick escapes)", () => {
    expect(sanitize("nothing special")).toBe("nothing special");
  });
});

describe("DiscordNotifier — CW-3 messageContext recording", () => {
  function senderWithIdReturn(messageId: string | null) {
    const sent: Array<{ channel: string; content: string; method: "plain" | "withId" }> = [];
    const sender: DiscordSender = {
      async sendToChannel(channel, content) {
        sent.push({ channel, content, method: "plain" });
      },
      async sendToChannelAndReturnId(channel, content) {
        sent.push({ channel, content, method: "withId" });
        return { messageId };
      },
      async addReaction() {
        /* no-op */
      },
    };
    return { sender, sent };
  }

  function makeMessageContext() {
    const records: Array<{ messageId: string; projectId: string }> = [];
    return {
      ctx: {
        recordAgentMessage(messageId: string, projectId: string) {
          records.push({ messageId, projectId });
        },
        resolveProjectIdForMessage() {
          return null;
        },
        // Wave E-β commit 2 — role-head methods stubbed for CW-3 tests; chain
        // behavior is exercised in tests/discord/notifier-chain-fixtures.test.ts.
        recordRoleMessage() {
          /* no-op */
        },
        lookupRoleHead() {
          return null;
        },
      },
      records,
    };
  }

  it("task-keyed event with stateManager + task has projectId → records via sendToChannelAndReturnId", async () => {
    const { sender, sent } = senderWithIdReturn("disc-msg-99");
    const { ctx, records } = makeMessageContext();
    const fakeState = {
      getTask(taskId: string) {
        if (taskId === "task-1") return { id: "task-1", projectId: "proj-77" };
        return undefined;
      },
    } as unknown as import("../../src/lib/state.js").StateManager;

    const n = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: fakeState });
    n.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe("withId");
    expect(records).toEqual([{ messageId: "disc-msg-99", projectId: "proj-77" }]);
  });

  it("task-keyed event with stateManager but task missing → falls back to sendToChannel, no record", async () => {
    const { sender, sent } = senderWithIdReturn("ignored");
    const { ctx, records } = makeMessageContext();
    const fakeState = {
      getTask() {
        return undefined; // task not in state
      },
    } as unknown as import("../../src/lib/state.js").StateManager;

    const n = new DiscordNotifier(sender, baseConfig(), { messageContext: ctx, stateManager: fakeState });
    n.handleEvent({ type: "task_done", taskId: "task-missing" });
    await flush();

    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe("plain");
    expect(records).toEqual([]);
  });

});

describe("redactSecrets()", () => {
  it("redacts Anthropic/OpenAI-style sk- keys", () => {
    expect(redactSecrets("use key sk-live-abc123DEF456ghi789jkl"))
      .toBe("use key [REDACTED]");
  });

  it("redacts GitHub personal tokens (ghp_)", () => {
    expect(redactSecrets("token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"))
      .toBe("token [REDACTED]");
  });

  it("redacts Slack tokens", () => {
    expect(redactSecrets("slack xoxb-abcdefghijk"))
      .toBe("slack [REDACTED]");
  });

  it("passes ordinary prose through untouched", () => {
    expect(redactSecrets("please fix the auth bug")).toBe("please fix the auth bug");
  });
});
