/**
 * CW-3 — InboundDispatcher unit tests.
 *
 * Covers all reply-routing precedence rules from
 * `ralplan-conversational-discord.md` §444-454:
 *   - Rule 2a: agent reply with active session → relayOperatorInput
 *   - Rule 2b: known agent username, no project record → operator-visible reply
 *   - Rule 3:  relayOperatorInput throws → classified failure reply
 *              (4 sub-cases: no_session / session_terminated / queue_full / generic)
 *   - Rule 4:  reply to a non-agent message → fall through to rule 5
 *   - Rule 5:  `!cmd` → handleCommand; else handleNaturalLanguage
 *   - Channel sender missing → console.warn, no crash
 *
 * All deps are mocked. No live Discord / SDK / fetch traffic.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { InboundDispatcher } from "../../src/discord/dispatcher.js";
import type { CommandRouter } from "../../src/discord/commands.js";
import type { ArchitectManager } from "../../src/session/architect.js";
import type { IdentityMap } from "../../src/discord/identity-map.js";
import type { MessageContext } from "../../src/discord/message-context.js";
import type { DiscordSender, InboundMessage, AgentIdentity } from "../../src/discord/types.js";
import type { DiscordConfig } from "../../src/lib/config.js";

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
      sent.push({ channel, content, identity });
      return { messageId: null };
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

function makeIdentityMap(known: Record<string, string>): IdentityMap {
  const entries = new Map<string, string>();
  for (const [name, key] of Object.entries(known)) {
    entries.set(name.toLowerCase(), key);
  }
  return {
    lookup(username: string): string | null {
      if (typeof username !== "string") return null;
      return entries.get(username.trim().toLowerCase()) ?? null;
    },
    entries,
  };
}

function makeMessageContext(initial: Record<string, string> = {}): MessageContext & { records: Array<{ id: string; pid: string }> } {
  const map = new Map(Object.entries(initial));
  const records: Array<{ id: string; pid: string }> = [];
  return {
    recordAgentMessage(id, pid) {
      records.push({ id, pid });
      map.set(id, pid);
    },
    resolveProjectIdForMessage(id) {
      return map.get(id) ?? null;
    },
    records,
  };
}

function makeArchitectManager(impl?: (projectId: string, message: string) => Promise<void>) {
  const calls: Array<{ projectId: string; message: string }> = [];
  const fn = vi.fn(async (projectId: string, message: string) => {
    calls.push({ projectId, message });
    if (impl) await impl(projectId, message);
  });
  const am: Pick<ArchitectManager, "relayOperatorInput"> = { relayOperatorInput: fn };
  return { am, fn, calls };
}

function makeCommandRouter() {
  const handleCommand = vi.fn(async (_cmd: string, _args: string, _channel: string) => "cmd-reply");
  const handleNaturalLanguage = vi.fn(async (_text: string, _channel: string, _user: string) => "nl-reply");
  // The dispatcher only consumes these two methods; cast to CommandRouter is
  // safe because the dispatcher's static type is `CommandRouter`. Using
  // `unknown as CommandRouter` keeps the test free of real router wiring.
  const router = { handleCommand, handleNaturalLanguage } as unknown as CommandRouter;
  return { router, handleCommand, handleNaturalLanguage };
}

function inboundMessage(overrides: Partial<InboundMessage> = {}): InboundMessage {
  return {
    messageId: "m-inc-1",
    channelId: "dev",
    authorId: "user-1",
    authorUsername: "operator",
    isBot: false,
    webhookId: null,
    content: "hello",
    repliedToMessageId: null,
    repliedToAuthorUsername: null,
    timestamp: "2026-04-09T00:00:00.000Z",
    ...overrides,
  };
}

// --- Tests ---

describe("InboundDispatcher", () => {
  let senders: Record<string, DiscordSender>;
  let sentDev: Recorded[];

  beforeEach(() => {
    const dev = makeFakeSender();
    sentDev = dev.sent;
    senders = { dev: dev.sender };
  });

  // --- Rule 2a — agent reply with live session ---

  it("rule 2a: agent reply with active session calls relayOperatorInput", async () => {
    const { am, calls } = makeArchitectManager();
    const ctx = makeMessageContext({ "agent-msg-1": "proj-x" });
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({
        content: "make it console.log instead",
        repliedToMessageId: "agent-msg-1",
        repliedToAuthorUsername: "Architect",
      }),
    );

    expect(calls).toEqual([{ projectId: "proj-x", message: "make it console.log instead" }]);
    // No operator-visible reply on success.
    expect(sentDev).toHaveLength(0);
  });

  // --- Rule 3 — error classification (4 sub-cases) ---

  it("rule 3 (no_session): relayOperatorInput throws 'No Architect session for X' → no_session reply", async () => {
    const { am } = makeArchitectManager(async () => {
      throw new Error("No Architect session for proj-x");
    });
    const ctx = makeMessageContext({ "agent-msg-1": "proj-x" });
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({
        content: "ping",
        repliedToMessageId: "agent-msg-1",
        repliedToAuthorUsername: "Architect",
      }),
    );

    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].channel).toBe("dev");
    expect(sentDev[0].content).toMatch(/no live Architect session/);
    expect(sentDev[0].content).toMatch(/proj-x/);
  });

  it("rule 3 (session_terminated): error matches /session terminated/ → session_terminated reply", async () => {
    const { am } = makeArchitectManager(async () => {
      throw new Error("session terminated mid-relay");
    });
    const ctx = makeMessageContext({ "agent-msg-1": "proj-y" });
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({ repliedToMessageId: "agent-msg-1", repliedToAuthorUsername: "Architect" }),
    );

    expect(sentDev[0].content).toMatch(/was terminated/);
    expect(sentDev[0].content).toMatch(/proj-y/);
  });

  it("rule 3 (session_terminated): error matches /aborted/ → session_terminated reply", async () => {
    const { am } = makeArchitectManager(async () => {
      throw new Error("relay aborted by operator");
    });
    const ctx = makeMessageContext({ "agent-msg-1": "proj-y2" });
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({ repliedToMessageId: "agent-msg-1", repliedToAuthorUsername: "Architect" }),
    );

    expect(sentDev[0].content).toMatch(/was terminated/);
  });

  it("rule 3 (queue_full): error matches /queue full/ → queue_full reply", async () => {
    const { am } = makeArchitectManager(async () => {
      throw new Error("Discord queue full");
    });
    const ctx = makeMessageContext({ "agent-msg-1": "proj-z" });
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({ repliedToMessageId: "agent-msg-1", repliedToAuthorUsername: "Architect" }),
    );

    expect(sentDev[0].content).toMatch(/queue is full/);
  });

  it("rule 3 (generic): unrecognized error message → generic reply", async () => {
    const { am } = makeArchitectManager(async () => {
      throw new Error("network unreachable");
    });
    const ctx = makeMessageContext({ "agent-msg-1": "proj-q" });
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({ repliedToMessageId: "agent-msg-1", repliedToAuthorUsername: "Architect" }),
    );

    expect(sentDev[0].content).toMatch(/Reply to `proj-q` failed/);
    expect(sentDev[0].content).toMatch(/network unreachable/);
  });

  // --- Rule 2b — known agent username, no project record ---

  it("rule 2b: known agent reply but no record → 'no record of that message' reply", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext(); // no records
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({
        repliedToMessageId: "old-evicted-msg",
        repliedToAuthorUsername: "Architect",
      }),
    );

    expect(fn).not.toHaveBeenCalled();
    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].content).toMatch(/no record of that message/);
    expect(sentDev[0].content).toMatch(/Architect/);
  });

  // --- Rule 4 — fall-through to rule 5 when replied-to author isn't an agent ---

  it("rule 4: reply to non-agent message falls through to rule 5 (NL)", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext({ "some-msg": "proj-w" });
    const idmap = makeIdentityMap({ Architect: "architect" }); // operator NOT in map
    const { router, handleNaturalLanguage } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({
        content: "start project bar",
        repliedToMessageId: "some-msg",
        repliedToAuthorUsername: "another-operator",
      }),
    );

    expect(fn).not.toHaveBeenCalled();
    expect(handleNaturalLanguage).toHaveBeenCalledWith("start project bar", "dev", "user-1");
    expect(sentDev[0].content).toBe("nl-reply");
  });

  // --- Rule 5 — `!cmd` vs natural language ---

  it("rule 5: content starting with '!' calls commandRouter.handleCommand", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({});
    const { router, handleCommand, handleNaturalLanguage } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(inboundMessage({ content: "!status proj-1" }));

    expect(handleCommand).toHaveBeenCalledWith("status", "proj-1", "dev");
    expect(handleNaturalLanguage).not.toHaveBeenCalled();
    expect(sentDev[0].content).toBe("cmd-reply");
  });

  it("rule 5: NL content (no '!') calls commandRouter.handleNaturalLanguage", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({});
    const { router, handleNaturalLanguage } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(inboundMessage({ content: "what's the status?" }));

    expect(handleNaturalLanguage).toHaveBeenCalledWith("what's the status?", "dev", "user-1");
  });

  // --- Channel sender missing — graceful skip with warn ---

  it("missing senders[channelId] warns and does not crash", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({});
    const { router } = makeCommandRouter();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders: {}, // no sender for any channel
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(inboundMessage({ content: "hello", channelId: "dev" }));

    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  // --- Edge: empty content NL still routes to handleNaturalLanguage ---

  it("rule 5: empty content routes to handleNaturalLanguage (does not crash)", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({});
    const { router, handleNaturalLanguage } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(inboundMessage({ content: "" }));

    expect(handleNaturalLanguage).toHaveBeenCalled();
  });

  // --- Reply with no repliedToAuthorUsername falls through to rule 5 ---

  it("rule 4: reply with no repliedToAuthorUsername (null) falls through to rule 5", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext({ "old-msg": "proj-w" });
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router, handleNaturalLanguage } = makeCommandRouter();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await d.dispatch(
      inboundMessage({
        content: "freeform reply",
        repliedToMessageId: "old-msg",
        repliedToAuthorUsername: null, // unknown — falls through
      }),
    );

    expect(fn).not.toHaveBeenCalled();
    expect(handleNaturalLanguage).toHaveBeenCalled();
  });

  // --- Dispatch never throws on internal errors (logged, not propagated) ---

  it("dispatch never throws even if handler.handleNaturalLanguage rejects", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({});
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const router = {
      handleCommand: vi.fn(async () => "x"),
      handleNaturalLanguage: vi.fn(async () => {
        throw new Error("router exploded");
      }),
    } as unknown as CommandRouter;

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
    });

    await expect(d.dispatch(inboundMessage({ content: "x" }))).resolves.toBeUndefined();
    expect(errSpy).toHaveBeenCalled();
    errSpy.mockRestore();
  });
});
