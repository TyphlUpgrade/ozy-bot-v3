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
import { InboundDispatcher, extractMentions } from "../../src/discord/dispatcher.js";
import { UNKNOWN_INTENT_REPLY_SENTINELS, type CommandRouter } from "../../src/discord/commands.js";
import type { ArchitectManager } from "../../src/session/architect.js";
import type { IdentityMap } from "../../src/discord/identity-map.js";
import type { MessageContext } from "../../src/discord/message-context.js";
import type { DiscordSender, InboundMessage, AgentIdentity } from "../../src/discord/types.js";
import type { DiscordConfig } from "../../src/lib/config.js";
import type { ChannelContextBuffer, ChannelMessage } from "../../src/discord/channel-context.js";
import type { ProjectRecord } from "../../src/lib/project.js";

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
    // Wave E-δ MR3 — lookupRole maps the four canonical IdentityRole literals
    // case-insensitively. Test helper mirrors the production allow-list at
    // src/discord/identity-map.ts:76 (case-insensitive switch on the four
    // literals; unknown returns null).
    lookupRole(name: string) {
      if (typeof name !== "string") return null;
      const k = name.trim().toLowerCase();
      if (k === "architect" || k === "reviewer" || k === "executor" || k === "orchestrator") {
        return k;
      }
      return null;
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

  // Phase 4 H2 (CR) — synthetic test only. Today, `relayOperatorInput` only
  // throws `No Architect session for ${projectId}` — the session_terminated /
  // queue_full branches below are forward-looking and not exercised by real
  // production paths. They lock in the routing contract so when architect.ts
  // adds typed termination errors / queue-overflow signals later, the
  // dispatcher already knows how to surface them. See classifyRelayError
  // docstring in dispatcher.ts.
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

  // ============================================================
  // CW-4.5 — mention routing + ChannelContextBuffer integration
  // ============================================================

  function makeProjectStore(projects: Array<Pick<ProjectRecord, "id" | "state">>): {
    getAllProjects: () => ProjectRecord[];
    getProject: (id: string) => ProjectRecord | undefined;
  } {
    const map = new Map<string, ProjectRecord>();
    for (const p of projects) {
      map.set(p.id, { id: p.id, state: p.state } as ProjectRecord);
    }
    return {
      getAllProjects: () => Array.from(map.values()),
      getProject: (id: string) => map.get(id),
    };
  }

  function makeChannelBuffer(byChannel: Record<string, ChannelMessage[]> = {}): Pick<ChannelContextBuffer, "recent"> {
    return {
      recent(channelId: string, n = 5): ReadonlyArray<ChannelMessage> {
        const arr = byChannel[channelId] ?? [];
        if (n >= arr.length) return arr.slice();
        return arr.slice(arr.length - n);
      },
    };
  }

  // ----- extractMentions: 3 forms × happy path -----

  it("CW-4.5 mention form 1: plain `@architect-x` resolves via IdentityMap", () => {
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const out = extractMentions("@architect-x do foo", idmap, null, null);
    expect(out.mentions).toHaveLength(1);
    expect(out.mentions[0].agentKey).toBe("architect");
    expect(out.cleanedContent).toBe("do foo");
    expect(out.botMentioned).toBe(false);
  });

  it("CW-4.5 mention form 2: `<@123>` resolves to bot when selfBotId matches", () => {
    const idmap = makeIdentityMap({});
    const out = extractMentions("<@123> abort it", idmap, null, "123");
    expect(out.botMentioned).toBe(true);
    expect(out.mentions).toHaveLength(0);
    expect(out.cleanedContent).toBe("abort it");
  });

  it("CW-4.5 mention form 3: `<@!123>` (nickname form) resolves to bot when selfBotId matches", () => {
    const idmap = makeIdentityMap({});
    const out = extractMentions("hey <@!123> status please", idmap, null, "123");
    expect(out.botMentioned).toBe(true);
    expect(out.cleanedContent).toBe("hey status please");
  });

  // ----- extractMentions: edge cases -----

  it("CW-4.5 backtick escape: `@architect-x` inside backticks NOT detected", () => {
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const out = extractMentions("see `@architect-x` for spec", idmap, null, null);
    expect(out.mentions).toHaveLength(0);
  });

  it("CW-4.5 triple-backtick fence: ```@architect-x``` NOT detected (Test 9b)", () => {
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const out = extractMentions("see ```@architect-x``` for spec", idmap, null, null);
    expect(out.mentions).toHaveLength(0);
  });

  it("CW-4.5 word-boundary anchors: trailing punctuation matches; embedded does not", () => {
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    // Trailing period — matches.
    const a = extractMentions("@architect-x.", idmap, null, null);
    expect(a.mentions).toHaveLength(1);
    // Embedded in word — does NOT match.
    const b = extractMentions("user@architect-x is fine", idmap, null, null);
    expect(b.mentions).toHaveLength(0);
    // Suffixed alphanumerics — does NOT match (regex permits then anchor fails).
    const c = extractMentions("@architect-xfoo", idmap, null, null);
    expect(c.mentions).toHaveLength(0);
  });

  // ----- Active-project resolution -----

  it("CW-4.5 0 active projects: agent mention → instructive reply, no relay", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([]); // no projects
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@architect-x do foo" }));

    expect(fn).not.toHaveBeenCalled();
    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].content).toMatch(/Multiple\/no active projects/);
  });

  it("CW-4.5 1 active project: agent mention relays cleanedContent", async () => {
    const { am, calls } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@architect-x make it console.log" }));

    expect(calls).toEqual([{ projectId: "proj-A", message: "make it console.log" }]);
    expect(sentDev).toHaveLength(0);
  });

  it("CW-4.5 2+ active no hint: agent mention → instructive reply, no relay", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([
      { id: "proj-A", state: "executing" },
      { id: "proj-B", state: "decomposing" },
    ]);
    const channelBuffer = makeChannelBuffer(); // empty buffer = no hints

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@architect-x what's blocking" }));

    expect(fn).not.toHaveBeenCalled();
    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].content).toMatch(/Multiple\/no active projects/);
  });

  it("CW-4.5 affinity hint coherent: 2 active but recent buffer agrees → routes via hint (Test 13b)", async () => {
    const { am, calls } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([
      { id: "proj-A", state: "executing" },
      { id: "proj-B", state: "executing" },
    ]);
    const buf = makeChannelBuffer({
      dev: [
        { author: "Architect", content: "phase 1 ok", timestamp: "t", projectIdHint: "proj-A" },
        { author: "Architect", content: "phase 2 ok", timestamp: "t", projectIdHint: "proj-A" },
        { author: "operator", content: "ok continue", timestamp: "t", projectIdHint: "proj-A" },
      ],
    });

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer: buf,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@architect-x what's blocking us?" }));

    expect(calls).toEqual([{ projectId: "proj-A", message: "what's blocking us?" }]);
    expect(sentDev).toHaveLength(0);
  });

  // ----- Relay error path -----

  it("CW-4.5 relay throw: mention path classifies via classifyRelayError → operator-visible reply", async () => {
    const { am } = makeArchitectManager(async () => {
      throw new Error("No Architect session for proj-A");
    });
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@architect-x ping" }));

    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].content).toMatch(/no live Architect session/);
    expect(sentDev[0].content).toMatch(/proj-A/);
  });

  // ----- Classifier recentMessages plumbing -----

  it("CW-4.5 classifier recentMessages populated from buffer (CommandRouter integration)", async () => {
    // This test imports CommandRouter directly to pin the recentMessages plumb.
    const { CommandRouter } = await import("../../src/discord/commands.js");
    const { StateManager } = await import("../../src/lib/state.js");
    const { mkdirSync, rmSync } = await import("node:fs");
    const { join } = await import("node:path");
    const { tmpdir } = await import("node:os");
    const dir = join(tmpdir(), `disp-recent-${Date.now()}`);
    mkdirSync(dir, { recursive: true });
    try {
      const state = new StateManager(join(dir, "state.json"));
      const cfg = {
        project: {
          name: "t", root: dir, task_dir: dir, state_file: join(dir, "state.json"),
          worktree_base: dir, session_dir: dir,
        },
        pipeline: {
          poll_interval: 1, test_command: "true", max_retries: 1,
          test_timeout: 60, escalation_timeout: 300, retry_delay_ms: 100,
        },
        discord: baseConfig(),
      };
      const captured: Array<{ recentMessages?: ReadonlyArray<{ author: string; content: string; timestamp: string }> }> = [];
      const classifier = {
        async classify(_text: string, c: { recentMessages?: ReadonlyArray<{ author: string; content: string; timestamp: string }> }) {
          captured.push({ recentMessages: c.recentMessages });
          return { type: "unknown" as const };
        },
      };
      const router = new CommandRouter({
        state, config: cfg, classifier,
        abort: { abortTask() { /* noop */ } },
        taskSink: { createTask: () => "t" },
        recentMessagesProvider: (_chan) => [
          { author: "alice", content: "hi", timestamp: "2026-01-01T00:00:00Z" },
          { author: "bob", content: "yo", timestamp: "2026-01-01T00:00:01Z" },
        ],
      });
      await router.handleNaturalLanguage("ambiguous text", "dev", "u1");
      expect(captured).toHaveLength(1);
      expect(captured[0].recentMessages).toHaveLength(2);
      expect(captured[0].recentMessages?.[0].author).toBe("alice");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  // ----- getBotUsername null at startup -----

  it("CW-4.5 getBotUsername null pre-READY: bot mention does not crash and falls through", async () => {
    const { am, fn } = makeArchitectManager();
    const c = makeMessageContext();
    const idmap = makeIdentityMap({});
    const { router, handleNaturalLanguage } = makeCommandRouter();
    const projectStore = makeProjectStore([]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: c,
      projectStore,
      channelBuffer,
      getBotUsername: () => null, // pre-READY
    });

    // `@ozy abort it` — selfBotUsername is null, so botMentioned = false.
    // No agent mentions either → falls through to NL.
    await d.dispatch(inboundMessage({ content: "@ozy abort it" }));

    expect(fn).not.toHaveBeenCalled();
    expect(handleNaturalLanguage).toHaveBeenCalledWith("@ozy abort it", "dev", "user-1");
  });

  // ----- Security LOW-1 — multi-mention instructive reply -----

  it("CW-4.5 LOW-1 multi-mention: sends instructive reply AND dispatches first mention", async () => {
    const { am, calls } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({
      "architect-x": "architect",
      "reviewer-y": "reviewer",
    });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(
      inboundMessage({ content: "@architect-x and @reviewer-y please coordinate" }),
    );

    // Instructive reply sent first.
    expect(sentDev.length).toBeGreaterThanOrEqual(1);
    expect(sentDev[0].content).toMatch(/Multiple agent mentions detected/);
    expect(sentDev[0].content).toMatch(/@architect-x/);
    // First mention is still dispatched.
    expect(calls).toEqual([
      { projectId: "proj-A", message: "and please coordinate" },
    ]);
  });

  // ============================================================
  // Wave E-δ MR1 — per-role mention routing
  // ============================================================

  it("E-δ MR1: @architect mention routes to relayOperatorInput (cleanedContent)", async () => {
    const { am, fn, calls } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ Architect: "architect" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@Architect ship it" }));

    expect(fn).toHaveBeenCalledOnce();
    expect(calls).toEqual([{ projectId: "proj-A", message: "ship it" }]);
    // No no_active_role notice on the architect path.
    expect(sentDev.filter((s) => s.content.includes("No active"))).toHaveLength(0);
  });

  it("E-δ MR2: @reviewer mention emits no_active_role notice (no relay)", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ Reviewer: "reviewer" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@Reviewer please look" }));

    // I-1 reverse direction: relay NEVER called for reviewer.
    expect(fn).not.toHaveBeenCalled();
    // Operator gets a visible notice via no_active_role static template.
    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].content).toMatch(/No active reviewer session/);
    expect(sentDev[0].content).toMatch(/proj-A/);
  });

  it("E-δ MR4: @executor mention emits no_active_role notice (no relay)", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ Executor: "executor" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@Executor go faster" }));

    expect(fn).not.toHaveBeenCalled();
    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].content).toMatch(/No active executor session/);
    expect(sentDev[0].content).toMatch(/proj-A/);
  });

  it("E-δ MR1: @orchestrator mention emits no_active_role notice (no relay)", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ Harness: "orchestrator" });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@Harness status" }));

    expect(fn).not.toHaveBeenCalled();
    expect(sentDev).toHaveLength(1);
    expect(sentDev[0].content).toMatch(/No active orchestrator session/);
  });

  it("E-δ MR1: unknown agent (lookupRole returns null) falls through to NL", async () => {
    // Construct an IdentityMap whose agentKey resolves to a non-canonical
    // string (e.g. legacy / typo) so lookupRole returns null. Dispatcher
    // should fall through to the existing rules (NL parser).
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ Stranger: "stranger" });
    const { router, handleNaturalLanguage } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(inboundMessage({ content: "@Stranger hello" }));

    expect(fn).not.toHaveBeenCalled();
    // Stranger resolves via IdentityMap.lookup but lookupRole returns null
    // → fall through to NL parser.
    expect(handleNaturalLanguage).toHaveBeenCalled();
  });

  it("E-δ MR1: multi-mention with @reviewer first → reviewer NO-OP, architect skipped (CW-4.5 first-mention precedence)", async () => {
    const { am, fn } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({
      Reviewer: "reviewer",
      Architect: "architect",
    });
    const { router } = makeCommandRouter();
    const projectStore = makeProjectStore([{ id: "proj-A", state: "executing" }]);
    const channelBuffer = makeChannelBuffer();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
    });

    await d.dispatch(
      inboundMessage({ content: "@Reviewer @Architect coordinate" }),
    );

    // First mention wins: @Reviewer routed to NO-OP notice; @Architect
    // skipped (relay never called) — preserves CW-4.5 v1 behavior.
    expect(fn).not.toHaveBeenCalled();
    // Two messages sent: multi-mention notice + no_active_role notice.
    const noActiveRole = sentDev.filter((s) => /No active reviewer session/.test(s.content));
    expect(noActiveRole).toHaveLength(1);
  });

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

  // ============================================================
  // CW-5 — reaction acknowledgments (reactionClient dep)
  // ============================================================

  function makeReactionClient(): { client: DiscordSender; reactions: Array<{ channelId: string; messageId: string; emoji: string }> } {
    const reactions: Array<{ channelId: string; messageId: string; emoji: string }> = [];
    const client: DiscordSender = {
      async sendToChannel() { /* unused */ },
      async sendToChannelAndReturnId() { return { messageId: null }; },
      async addReaction(channelId, messageId, emoji) {
        reactions.push({ channelId, messageId, emoji });
      },
    };
    return { client, reactions };
  }

  it("CW-5: reaction `👀` fires on inbound entry (every dispatch)", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({});
    const { router } = makeCommandRouter();
    const { client: reactionClient, reactions } = makeReactionClient();

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      reactionClient,
    });

    await d.dispatch(inboundMessage({ messageId: "m-7", content: "hello" }));

    // First reaction is the receipt acknowledgment; subsequent ones may be
    // status (e.g., 🤔 on unknown intent) — only the first emoji is asserted.
    expect(reactions.length).toBeGreaterThanOrEqual(1);
    expect(reactions[0]).toEqual({ channelId: "dev", messageId: "m-7", emoji: "👀" });
  });

  it("CW-5: reaction `✅` fires on rule 1 mention success", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const { router } = makeCommandRouter();
    const { client: reactionClient, reactions } = makeReactionClient();
    const projectStore = {
      getAllProjects: () => [{ id: "proj-A", state: "executing" as const }] as never,
      getProject: (id: string) => (id === "proj-A" ? ({ id: "proj-A", state: "executing" } as never) : undefined),
    };
    const channelBuffer = { recent: () => [] };

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
      reactionClient,
    });

    await d.dispatch(inboundMessage({ messageId: "m-8", content: "@architect-x ping" }));

    // Receipt 👀 then success ✅.
    expect(reactions.map((r) => r.emoji)).toContain("✅");
    expect(reactions[0].emoji).toBe("👀");
  });

  it("CW-5: reaction `❌` fires on rule 1 mention error path", async () => {
    const { am } = makeArchitectManager(async () => {
      throw new Error("No Architect session for proj-A");
    });
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({ "architect-x": "architect" });
    const { router } = makeCommandRouter();
    const { client: reactionClient, reactions } = makeReactionClient();
    const projectStore = {
      getAllProjects: () => [{ id: "proj-A", state: "executing" as const }] as never,
      getProject: (id: string) => (id === "proj-A" ? ({ id: "proj-A", state: "executing" } as never) : undefined),
    };
    const channelBuffer = { recent: () => [] };

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      projectStore,
      channelBuffer,
      getBotUsername: () => null,
      reactionClient,
    });

    await d.dispatch(inboundMessage({ messageId: "m-9", content: "@architect-x ping" }));

    expect(reactions.map((r) => r.emoji)).toContain("❌");
    expect(reactions[0].emoji).toBe("👀");
  });

  it("CW-5: reaction `🤔` fires when CommandRouter returns an unknown-intent sentinel", async () => {
    const { am } = makeArchitectManager();
    const ctx = makeMessageContext();
    const idmap = makeIdentityMap({});
    const { client: reactionClient, reactions } = makeReactionClient();

    // Mock CommandRouter to return the first unknown-intent sentinel verbatim.
    const router = {
      handleCommand: vi.fn(async () => UNKNOWN_INTENT_REPLY_SENTINELS[0]),
      handleNaturalLanguage: vi.fn(async () => UNKNOWN_INTENT_REPLY_SENTINELS[0]),
    } as unknown as CommandRouter;

    const d = new InboundDispatcher({
      commandRouter: router,
      architectManager: am,
      identityMap: idmap,
      senders,
      config: baseConfig(),
      messageContext: ctx,
      reactionClient,
    });

    await d.dispatch(inboundMessage({ messageId: "m-10", content: "asdf" }));

    expect(reactions.some((r) => r.emoji === "🤔")).toBe(true);
  });
});
