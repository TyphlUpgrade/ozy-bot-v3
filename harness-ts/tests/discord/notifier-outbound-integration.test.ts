/**
 * Wave E-γ commit 2 — DiscordNotifier × OutboundResponseGenerator wiring.
 *
 * Covers the integration matrix from the commit-2 spec (D6, AC4, AC6):
 *
 *   1. Default flag false → outbound generator NEVER invoked even if injected
 *   2. Explicit `outbound_epistle_enabled: false` → same
 *   3. Flag true + eligible (event, role) → sent body equals LLM output
 *   4. Flag true + NON-eligible event (e.g. poll_tick / project_declared::architect)
 *      → generator NOT called
 *   5. Flag true + eligible event + no projectId → generator NOT called
 *      (projectId guard is defense-in-depth on top of the whitelist guard)
 *   6. Flag true + generator fallback (returns deterministicBody) →
 *      sent body indistinguishable from E-α deterministic
 *   7. Flag true + reply threading on → replyToMessageId still passes through
 *      correctly post-LLM transform (composes with E-β chain rules)
 *   8. AC6 byte-equal pin: with flag false, the Phase A pin substring at
 *      `notifier.test.ts:309` (em-dash + `; ` glue + bracketed terminalReason)
 *      remains in the rendered body.
 *
 * The OutboundResponseGenerator is mocked to a vi.fn() so SDK / filesystem /
 * budget tracker stay out of the loop.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

import { DiscordNotifier } from "../../src/discord/notifier.js";
import {
  sendToChannelAndReturnIdDefault,
  type DiscordSender,
  type AgentIdentity,
} from "../../src/discord/types.js";
import type { DiscordConfig } from "../../src/lib/config.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";
import type { StateManager } from "../../src/lib/state.js";
import { InMemoryMessageContext } from "../../src/discord/message-context.js";
import type { OutboundResponseGenerator } from "../../src/discord/outbound-response-generator.js";

// --- Fakes ---

interface Recorded {
  channel: string;
  content: string;
  identity?: AgentIdentity;
  replyToMessageId?: string;
  returnedId: string | null;
}

function makeRecordingSender(opts: { idPrefix?: string } = {}): {
  sender: DiscordSender;
  sent: Recorded[];
} {
  const prefix = opts.idPrefix ?? "msg";
  const sent: Recorded[] = [];
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

// Default-fake sender that doesn't return ids — exercises the plain
// sendToChannel path (used for projectId-null fallback assertions).
function makeFakeSender(): { sender: DiscordSender; sent: Recorded[] } {
  const sent: Recorded[] = [];
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity) {
      sent.push({ channel, content, identity, returnedId: null });
    },
    async sendToChannelAndReturnId(channel, content, identity, replyToMessageId) {
      return sendToChannelAndReturnIdDefault(this, channel, content, identity, replyToMessageId);
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

function fakeStateManagerWith(taskToProject: Record<string, string>): StateManager {
  return {
    getTask(taskId: string) {
      const projectId = taskToProject[taskId];
      if (!projectId) return undefined;
      return { id: taskId, projectId };
    },
  } as unknown as StateManager;
}

// Build a mock OutboundResponseGenerator. `behavior` controls what `generate`
// resolves with: "llm" returns a fixed LLM voice string; "fallback" returns
// the deterministic body verbatim (simulates internal failure path).
function makeMockGenerator(
  behavior: "llm" | "fallback" = "llm",
  llmBody = "[LLM voice] this is the rewritten prose body",
): {
  generator: OutboundResponseGenerator;
  generate: ReturnType<typeof vi.fn>;
} {
  const generate = vi.fn(
    async (input: { event: OrchestratorEvent; role: string; deterministicBody: string }) => {
      if (behavior === "fallback") return input.deterministicBody;
      return llmBody;
    },
  );
  const generator = { generate } as unknown as OutboundResponseGenerator;
  return { generator, generate };
}

async function flush(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
}

// --- Tests ---

describe("DiscordNotifier × OutboundResponseGenerator (Wave E-γ commit 2)", () => {
  let sent: Recorded[];

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("AC4 default — flag undefined → generator never invoked even if injected", async () => {
    // Eligible task-keyed event with a resolvable projectId. If the flag
    // weren't gating, the generator would otherwise fire (whitelist + projectId
    // both satisfied). Default flag = undefined must keep it inert.
    const { sender, sent: recorded } = makeRecordingSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const state = fakeStateManagerWith({ "task-1": "proj-1" });
    const { generator, generate } = makeMockGenerator("llm");
    const cfg = baseConfig(); // outbound_epistle_enabled: undefined
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      stateManager: state,
      outboundGenerator: generator,
    });

    notifier.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    expect(generate).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    // Body should be the deterministic epistle body, not the LLM placeholder.
    expect(sent[0].content).not.toContain("[LLM voice]");
  });

  it("AC4 explicit false — flag set false → generator never invoked", async () => {
    const { sender, sent: recorded } = makeRecordingSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const state = fakeStateManagerWith({ "task-1": "proj-1" });
    const { generator, generate } = makeMockGenerator("llm");
    const cfg = baseConfig({ outbound_epistle_enabled: false });
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      stateManager: state,
      outboundGenerator: generator,
    });

    notifier.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    expect(generate).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(sent[0].content).not.toContain("[LLM voice]");
  });

  it("flag true + eligible event → sent body equals LLM output", async () => {
    const { sender, sent: recorded } = makeRecordingSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const state = fakeStateManagerWith({ "task-1": "proj-1" });
    const llmBody = "[LLM voice] I built the requested module and verified the suite is green.";
    const { generator, generate } = makeMockGenerator("llm", llmBody);
    const cfg = baseConfig({ outbound_epistle_enabled: true });
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      stateManager: state,
      outboundGenerator: generator,
    });

    notifier.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    expect(generate).toHaveBeenCalledTimes(1);
    const call = generate.mock.calls[0][0];
    expect(call.event.type).toBe("task_done");
    expect(call.role).toBe("executor");
    expect(typeof call.deterministicBody).toBe("string");
    expect(sent).toHaveLength(1);
    expect(sent[0].content).toBe(llmBody);
  });

  it("flag true + NON-eligible event → generator NOT called", async () => {
    // `compaction_fired` resolves projectId from event.projectId (architect role)
    // but is NOT in OUTBOUND_LLM_WHITELIST. Generator must NOT be invoked.
    const { sender, sent: recorded } = makeRecordingSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const { generator, generate } = makeMockGenerator("llm");
    const cfg = baseConfig({ outbound_epistle_enabled: true });
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      outboundGenerator: generator,
    });

    notifier.handleEvent({ type: "compaction_fired", projectId: "proj-1", generation: 1 });
    await flush();

    expect(generate).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
  });

  it("flag true + eligible event + NO projectId resolution → generator NOT called (defensive)", async () => {
    // task_done is whitelisted for executor — but with no stateManager AND no
    // projectId on the event, the projectId guard short-circuits before the
    // recording path can fire the LLM transform. Also exercises the plain
    // sendToChannel fallback (which never invokes the generator).
    const { sender, sent: recorded } = makeFakeSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const { generator, generate } = makeMockGenerator("llm");
    const cfg = baseConfig({ outbound_epistle_enabled: true });
    // Inject messageContext (so the recording path is reachable) but NO
    // stateManager — task_done with no projectId on the event resolves null.
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      outboundGenerator: generator,
    });

    notifier.handleEvent({ type: "task_done", taskId: "task-orphan" });
    await flush();

    expect(generate).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
  });

  it("flag true + generator returns deterministicBody → sent body indistinguishable from E-α", async () => {
    // Mocks the all-failure-paths-internally case: generator returns the
    // deterministic body verbatim. The sent body must equal what the
    // deterministic path would have produced.
    const { sender, sent: recorded } = makeRecordingSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const state = fakeStateManagerWith({ "task-1": "proj-1" });
    const { generator, generate } = makeMockGenerator("fallback");
    const cfg = baseConfig({ outbound_epistle_enabled: true });
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      stateManager: state,
      outboundGenerator: generator,
    });

    // Capture the deterministic body via a parallel notifier with the flag off.
    const { sender: refSender, sent: refSent } = makeRecordingSender();
    const refState = fakeStateManagerWith({ "task-1": "proj-1" });
    const refCtx = new InMemoryMessageContext();
    const refNotifier = new DiscordNotifier(refSender, baseConfig(), {
      messageContext: refCtx,
      stateManager: refState,
    });
    refNotifier.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    notifier.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    expect(generate).toHaveBeenCalledTimes(1);
    expect(sent).toHaveLength(1);
    expect(refSent).toHaveLength(1);
    expect(sent[0].content).toBe(refSent[0].content);
  });

  it("composes with E-β reply threading: replyToMessageId still passes through post-LLM transform", async () => {
    // First emit project_decomposed (registers architect head in dev_channel),
    // then arbitration_verdict in ops_channel (chain-head registers separately;
    // verdict registers as architect head in ops). Then session_complete (also
    // eligible) replies under architect head — verify replyToMessageId is set
    // even though body went through LLM transform.
    const { sender, sent: recorded } = makeRecordingSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const state = fakeStateManagerWith({ "task-arb": "P1", "task-sess": "P1" });
    const llmBody = "[LLM voice] threaded reply body";
    const { generator } = makeMockGenerator("llm", llmBody);
    const cfg = baseConfig({ outbound_epistle_enabled: true });
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      stateManager: state,
      outboundGenerator: generator,
    });

    notifier.handleEvent({ type: "project_decomposed", projectId: "P1", phaseCount: 3 });
    await flush();
    expect(sent[0].channel).toBe("dev");
    expect(sent[0].replyToMessageId).toBeUndefined(); // standalone (chain head)
    expect(sent[0].content).toBe(llmBody); // LLM transform fired
    expect(ctx.lookupRoleHead("P1", "architect", "dev")).toBe("msg-1");

    // session_complete is whitelisted (executor) AND chains under architect
    // head in dev_channel per CHAIN_RULES.
    notifier.handleEvent({ type: "session_complete", taskId: "task-sess", success: true });
    await flush();
    expect(sent).toHaveLength(2);
    expect(sent[1].channel).toBe("dev");
    expect(sent[1].replyToMessageId).toBe("msg-1"); // E-β chain still wires through LLM-transformed send
    expect(sent[1].content).toBe(llmBody); // LLM transform also fired here
  });

  it("AC6 byte-equal pin preservation: flag false leaves Phase A em-dash + bracketed terminalReason in body", async () => {
    // Mirrors notifier.test.ts:303-313. When flag is unset, the deterministic
    // path is byte-equal to E-β. The substring pin "failure — boom1; boom2 [budget_exceeded]"
    // (em-dash U+2014, "; " glue, square brackets) must still appear.
    const { sender, sent: recorded } = makeFakeSender();
    sent = recorded;
    const { generator, generate } = makeMockGenerator("llm");
    // Flag unset (default) — even with a generator wired, deterministic path runs.
    const notifier = new DiscordNotifier(sender, baseConfig(), {
      outboundGenerator: generator,
    });

    notifier.handleEvent({
      type: "session_complete",
      taskId: "t1",
      success: false,
      errors: ["boom1", "boom2"],
      terminalReason: "budget_exceeded",
    });
    await flush();

    expect(generate).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(sent[0].content).toMatch(/failure — boom1; boom2 \[budget_exceeded\]/);
  });

  it("flag true but no generator injected → behavior identical to E-β (no transform attempted)", async () => {
    // Defense-in-depth: flag-on but generator absent must not crash; the
    // deterministic path runs normally.
    const { sender, sent: recorded } = makeRecordingSender();
    sent = recorded;
    const ctx = new InMemoryMessageContext();
    const state = fakeStateManagerWith({ "task-1": "proj-1" });
    const cfg = baseConfig({ outbound_epistle_enabled: true });
    const notifier = new DiscordNotifier(sender, cfg, {
      messageContext: ctx,
      stateManager: state,
      // no outboundGenerator
    });

    notifier.handleEvent({ type: "task_done", taskId: "task-1" });
    await flush();

    expect(sent).toHaveLength(1);
    // Deterministic body — has the executor identity + projectId encoding.
    expect(sent[0].identity?.username).toBe("Executor");
  });

  it("identity → outbound role mapping covers all 4 whitelisted roles", async () => {
    // Quick smoke that all four IdentityRole values map to OutboundRole and
    // trigger the generator when the event is whitelisted for that role.
    // Pairs chosen from the verbatim 9-tuple whitelist so this stays grounded
    // in OUTBOUND_LLM_WHITELIST contents.
    const cases: Array<{ event: OrchestratorEvent; role: string }> = [
      // architect
      { event: { type: "project_decomposed", projectId: "P-arch", phaseCount: 1 }, role: "architect" },
      // executor
      { event: { type: "task_done", taskId: "task-exec" }, role: "executor" },
      // orchestrator (event role resolves to orchestrator per identity.ts)
      { event: { type: "merge_result", taskId: "task-orch", result: { status: "merged", commitSha: "deadbeef0001234" } }, role: "orchestrator" },
      // reviewer
      { event: { type: "review_mandatory", taskId: "task-rev", projectId: "P-rev" }, role: "reviewer" },
    ];

    for (const c of cases) {
      const { sender, sent: recorded } = makeRecordingSender();
      const ctx = new InMemoryMessageContext();
      const state = fakeStateManagerWith({
        "task-exec": "P-exec",
        "task-orch": "P-orch",
        "task-rev": "P-rev",
      });
      const llmBody = `[LLM voice ${c.role}]`;
      const { generator, generate } = makeMockGenerator("llm", llmBody);
      const notifier = new DiscordNotifier(sender, baseConfig({ outbound_epistle_enabled: true }), {
        messageContext: ctx,
        stateManager: state,
        outboundGenerator: generator,
      });
      notifier.handleEvent(c.event);
      await flush();
      expect(generate).toHaveBeenCalledTimes(1);
      expect(generate.mock.calls[0][0].role).toBe(c.role);
      expect(recorded).toHaveLength(1);
      expect(recorded[0].content).toBe(llmBody);
    }
  });
});
