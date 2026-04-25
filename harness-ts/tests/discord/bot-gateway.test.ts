import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { RawWsBotGateway, type WSLike } from "../../src/discord/bot-gateway.js";
import type { InboundMessage } from "../../src/discord/types.js";

/**
 * Fake `WSLike` — captures sent payloads and exposes inject helpers so each
 * test drives the gateway via op-code/dispatch frames. No real WebSocket.
 */
class FakeWS implements WSLike {
  onmessage: ((ev: { data: string | Buffer }) => void) | null = null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null;
  onerror: ((ev: { message?: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  readyState = 1;
  sent: string[] = [];
  closedWith: number | null = null;
  send(data: string): void { this.sent.push(data); }
  close(code?: number): void { this.closedWith = code ?? 1000; }
  inject(payload: unknown): void {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(payload) });
  }
  parsedSent(): Array<{ op: number; d?: unknown }> {
    return this.sent.map((s) => JSON.parse(s) as { op: number; d?: unknown });
  }
}

function buildGateway(opts: {
  allowed?: string[];
  selfWebhookIds?: string[] | "skip";
  fake?: FakeWS;
} = {}): { gw: RawWsBotGateway; ws: FakeWS } {
  const ws = opts.fake ?? new FakeWS();
  const gw = new RawWsBotGateway({
    token: "test-token",
    allowedChannelIds: opts.allowed ?? ["chan-allowed"],
    webSocketFactory: () => ws,
  });
  if (opts.selfWebhookIds !== "skip") gw.registerSelfWebhookIds(opts.selfWebhookIds ?? ["wh-self"]);
  return { gw, ws };
}

function readyFrame(userId = "bot-self"): unknown {
  return {
    op: 0,
    s: 1,
    t: "READY",
    d: { session_id: "sess-1", resume_gateway_url: "wss://resume", user: { id: userId } },
  };
}

function helloFrame(intervalMs = 41250): unknown {
  return { op: 10, d: { heartbeat_interval: intervalMs } };
}

interface MessagePayload {
  id: string;
  channelId: string;
  authorId?: string;
  username?: string;
  bot?: boolean;
  webhookId?: string;
  content?: string;
  refMsgId?: string;
  refUsername?: string;
  refMsgIdInRefBody?: string;
  timestamp?: string;
}

function messageCreateFrame(p: MessagePayload, seq = 2): unknown {
  const d: Record<string, unknown> = {
    id: p.id,
    channel_id: p.channelId,
    author: { id: p.authorId ?? "u-1", username: p.username ?? "alice", bot: p.bot ?? false },
    content: p.content ?? "hello",
    timestamp: p.timestamp ?? "2026-04-24T00:00:00.000Z",
  };
  if (p.webhookId) d.webhook_id = p.webhookId;
  if (p.refMsgId) d.message_reference = { message_id: p.refMsgId };
  if (p.refUsername) {
    d.referenced_message = {
      id: p.refMsgIdInRefBody ?? p.refMsgId ?? "m-ref",
      author: { username: p.refUsername },
    };
  }
  return { op: 0, s: seq, t: "MESSAGE_CREATE", d };
}

describe("RawWsBotGateway", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("filters self-id (authorId == selfBotId)", async () => {
    const { gw, ws } = buildGateway();
    const got: InboundMessage[] = [];
    gw.on((m) => got.push(m));
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame("bot-self"));
    ws.inject(messageCreateFrame({ id: "m-1", channelId: "chan-allowed", authorId: "bot-self" }));
    expect(got).toHaveLength(0);
  });

  it("filters self-webhook (webhookId in registered set)", async () => {
    const { gw, ws } = buildGateway({ selfWebhookIds: ["wh-self"] });
    const got: InboundMessage[] = [];
    gw.on((m) => got.push(m));
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({ id: "m-1", channelId: "chan-allowed", webhookId: "wh-self", bot: true }));
    expect(got).toHaveLength(0);
  });

  it("filters other-bot (isBot && webhookId not registered)", async () => {
    const { gw, ws } = buildGateway();
    const got: InboundMessage[] = [];
    gw.on((m) => got.push(m));
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({ id: "m-1", channelId: "chan-allowed", bot: true, webhookId: "wh-other" }));
    ws.inject(messageCreateFrame({ id: "m-2", channelId: "chan-allowed", bot: true }, 3));
    expect(got).toHaveLength(0);
  });

  it("filters channel allowlist (channelId not in allowed set)", async () => {
    const { gw, ws } = buildGateway({ allowed: ["chan-allowed"] });
    const got: InboundMessage[] = [];
    gw.on((m) => got.push(m));
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({ id: "m-1", channelId: "chan-other" }));
    expect(got).toHaveLength(0);
  });

  it("emits handler for allowed messages", async () => {
    const { gw, ws } = buildGateway();
    const got: InboundMessage[] = [];
    gw.on((m) => got.push(m));
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({
      id: "m-1", channelId: "chan-allowed", authorId: "u-9", username: "bob", content: "hi",
      timestamp: "2026-04-24T01:02:03.000Z",
    }));
    expect(got).toHaveLength(1);
    expect(got[0]).toMatchObject({
      messageId: "m-1",
      channelId: "chan-allowed",
      authorId: "u-9",
      authorUsername: "bob",
      isBot: false,
      webhookId: null,
      content: "hi",
      repliedToMessageId: null,
      repliedToAuthorUsername: null,
      timestamp: "2026-04-24T01:02:03.000Z",
    });
  });

  it("sends IDENTIFY with token, intents bitmask, and properties on first HELLO", async () => {
    const { gw, ws } = buildGateway();
    await gw.start();
    ws.inject(helloFrame());
    const sent = ws.parsedSent();
    expect(sent).toHaveLength(1);
    const identify = sent[0];
    expect(identify.op).toBe(2);
    const d = identify.d as { token: string; intents: number; properties: Record<string, string> };
    expect(d.token).toBe("test-token");
    // GUILDS (1<<0) | GUILD_MESSAGES (1<<9) | MESSAGE_CONTENT (1<<15) = 1 | 512 | 32768 = 33281
    expect(d.intents).toBe(33281);
    expect(d.properties).toEqual({ os: "linux", browser: "harness-ts", device: "harness-ts" });
  });

  it("sends RESUME with session_id and seq when session is already known", async () => {
    const { gw, ws } = buildGateway();
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({ id: "m-1", channelId: "chan-allowed" }, 7));
    // Simulate a reconnect: HELLO again now that session is established.
    ws.sent = [];
    ws.inject(helloFrame());
    const sent = ws.parsedSent();
    const resume = sent.find((p) => p.op === 6);
    expect(resume).toBeDefined();
    expect(resume?.d).toEqual({ token: "test-token", session_id: "sess-1", seq: 7 });
  });

  it("schedules HEARTBEAT at the HELLO-advertised interval", async () => {
    const { gw, ws } = buildGateway();
    await gw.start();
    ws.inject(helloFrame(5000));
    ws.inject(readyFrame()); // s=1 → lastSeq tracked
    ws.inject(messageCreateFrame({ id: "m-1", channelId: "chan-allowed" }, 7));
    ws.sent = []; // discard IDENTIFY before measuring heartbeat
    await vi.advanceTimersByTimeAsync(4999);
    expect(ws.parsedSent().filter((p) => p.op === 1)).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(2);
    const beats = ws.parsedSent().filter((p) => p.op === 1);
    expect(beats).toHaveLength(1);
    expect(beats[0].d).toBe(7); // lastSeq from latest dispatch
  });

  it("decodes MESSAGE_CREATE payload (full roundtrip)", async () => {
    const { gw, ws } = buildGateway();
    const got: InboundMessage[] = [];
    gw.on((m) => got.push(m));
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({
      id: "msg-42",
      channelId: "chan-allowed",
      authorId: "user-7",
      username: "carol",
      bot: false,
      content: "do the thing",
      timestamp: "2026-04-24T12:34:56.789Z",
    }));
    expect(got).toHaveLength(1);
    expect(got[0]).toEqual({
      messageId: "msg-42",
      channelId: "chan-allowed",
      authorId: "user-7",
      authorUsername: "carol",
      isBot: false,
      webhookId: null,
      content: "do the thing",
      repliedToMessageId: null,
      repliedToAuthorUsername: null,
      timestamp: "2026-04-24T12:34:56.789Z",
    });
  });

  it("extracts referenced_message and caches author username", async () => {
    const { gw, ws } = buildGateway();
    const got: InboundMessage[] = [];
    gw.on((m) => got.push(m));
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({
      id: "m-reply",
      channelId: "chan-allowed",
      content: "thanks",
      refMsgId: "m-orig",
      refMsgIdInRefBody: "m-orig",
      refUsername: "dave",
    }));
    expect(got).toHaveLength(1);
    expect(got[0].repliedToMessageId).toBe("m-orig");
    expect(got[0].repliedToAuthorUsername).toBe("dave");
    await expect(gw.fetchReferenceUsername("m-orig", "chan-allowed")).resolves.toBe("dave");
  });

  it("MESSAGE_CONTENT sentinel fires once at 10 empty messages and stays latched", async () => {
    const { gw, ws } = buildGateway();
    const sentinel = vi.fn();
    gw.onMessageContentMissing(sentinel);
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    for (let i = 0; i < 9; i++) {
      ws.inject(messageCreateFrame({ id: `m-${i}`, channelId: "chan-allowed", content: "" }, 10 + i));
    }
    expect(sentinel).not.toHaveBeenCalled();
    ws.inject(messageCreateFrame({ id: "m-9", channelId: "chan-allowed", content: "" }, 100));
    expect(sentinel).toHaveBeenCalledTimes(1);
    // Latched: further empty messages do NOT fire again.
    for (let i = 0; i < 12; i++) {
      ws.inject(messageCreateFrame({ id: `m-late-${i}`, channelId: "chan-allowed", content: "" }, 200 + i));
    }
    expect(sentinel).toHaveBeenCalledTimes(1);
  });

  it("registerSelfWebhookIds called twice throws", () => {
    const ws = new FakeWS();
    const gw = new RawWsBotGateway({
      token: "t",
      allowedChannelIds: ["c"],
      webSocketFactory: () => ws,
    });
    gw.registerSelfWebhookIds(["wh-1"]);
    expect(() => gw.registerSelfWebhookIds(["wh-2"])).toThrow(/twice/);
  });

  it("isolates handler exceptions (one bad handler doesn't break siblings)", async () => {
    const { gw, ws } = buildGateway();
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const goodA = vi.fn();
    const goodB = vi.fn();
    gw.on(goodA);
    gw.on(() => { throw new Error("boom"); });
    gw.on(goodB);
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({ id: "m-1", channelId: "chan-allowed" }));
    expect(goodA).toHaveBeenCalledTimes(1);
    expect(goodB).toHaveBeenCalledTimes(1);
    expect(errSpy).toHaveBeenCalledWith(expect.stringMatching(/handler threw/));
    errSpy.mockRestore();
  });

  it("fetchReferenceUsername returns cached value and null on miss", async () => {
    const { gw, ws } = buildGateway();
    await gw.start();
    ws.inject(helloFrame());
    ws.inject(readyFrame());
    ws.inject(messageCreateFrame({
      id: "m-reply",
      channelId: "chan-allowed",
      refMsgId: "m-cached",
      refMsgIdInRefBody: "m-cached",
      refUsername: "eve",
    }));
    await expect(gw.fetchReferenceUsername("m-cached", "chan-allowed")).resolves.toBe("eve");
    await expect(gw.fetchReferenceUsername("m-missing", "chan-allowed")).resolves.toBeNull();
  });
});
