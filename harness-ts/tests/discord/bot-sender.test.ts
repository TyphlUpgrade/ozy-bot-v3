import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { BotSender } from "../../src/discord/bot-sender.js";

function mockFetchOK(): ReturnType<typeof vi.fn> {
  return vi.fn().mockResolvedValue({ ok: true, status: 200, statusText: "OK" });
}

function readBody(call: unknown): {
  content: string;
  allowed_mentions?: { parse: string[] };
  message_reference?: { message_id: string; fail_if_not_exists: boolean };
} {
  const arg = (call as unknown[])[1] as { body: string };
  return JSON.parse(arg.body);
}

describe("BotSender", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("throws on empty token", () => {
    expect(() => new BotSender("")).toThrow(/token/);
    expect(() => new BotSender("   ")).toThrow(/token/);
  });

  it("posts content to the channel messages endpoint", async () => {
    const fetch = mockFetchOK();
    const sender = new BotSender("test-token", { fetch, minSpacingMs: 0 });
    await sender.sendToChannel("123456", "hello");
    await vi.runAllTimersAsync();
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("https://discord.com/api/v10/channels/123456/messages");
    expect((init as { method: string }).method).toBe("POST");
    expect((init as { headers: Record<string, string> }).headers.Authorization).toBe("Bot test-token");
  });

  it("renders identity.username as a `**[name]**` body prefix (bot REST cannot override per-message)", async () => {
    const fetch = mockFetchOK();
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    await sender.sendToChannel("123", "did the thing", { username: "Architect" });
    await vi.runAllTimersAsync();
    const body = readBody(fetch.mock.calls[0]);
    expect(body.content).toBe("**[Architect]** did the thing");
  });

  it("attaches `allowed_mentions: {parse: []}` to neutralize @everyone/@here pings", async () => {
    const fetch = mockFetchOK();
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    await sender.sendToChannel("123", "@everyone bad");
    await vi.runAllTimersAsync();
    const body = readBody(fetch.mock.calls[0]);
    expect(body.allowed_mentions).toEqual({ parse: [] });
  });

  it("logs (does not throw) on non-2xx responses", async () => {
    // Use 500 here — 429 is exercised by the dedicated retry-after tests below.
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 500, statusText: "Server Error" });
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await sender.sendToChannel("c", "x");
    await vi.runAllTimersAsync();
    expect(errSpy).toHaveBeenCalledWith(expect.stringMatching(/500/));
    errSpy.mockRestore();
  });

  // Phase 4 H1 — Discord 429 handling: parse retry_after (seconds) from JSON
  // body, sleep, then continue draining the next message.
  it("on 429 sleeps for retry_after seconds before processing next message", async () => {
    const fetch = vi
      .fn()
      // First call → 429 with retry_after=0.5s
      .mockResolvedValueOnce({
        ok: false,
        status: 429,
        statusText: "Too Many Requests",
        json: async () => ({ retry_after: 0.5 }),
      })
      // Second call (different message) → success
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => ({ id: "msg-after-429" }),
      });
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    void sender.sendToChannel("c", "first");
    const second = sender.sendToChannelAndReturnId("c", "second");
    // Run the 500ms retry-after sleep + any subsequent timers
    await vi.advanceTimersByTimeAsync(499);
    expect(fetch).toHaveBeenCalledOnce(); // still in retry-after sleep
    await vi.advanceTimersByTimeAsync(2);
    await vi.runAllTimersAsync();
    const result = await second;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.messageId).toBe("msg-after-429");
    expect(warnSpy).toHaveBeenCalledWith(expect.stringMatching(/429/));
    warnSpy.mockRestore();
  });

  it("on 429 with malformed body falls back to 1000ms sleep", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 429,
        statusText: "Too Many Requests",
        json: async () => { throw new Error("bad json"); },
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => ({ id: "msg-1" }),
      });
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    void sender.sendToChannel("c", "first");
    const second = sender.sendToChannelAndReturnId("c", "second");
    await vi.advanceTimersByTimeAsync(999);
    expect(fetch).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(2);
    await vi.runAllTimersAsync();
    const result = await second;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.messageId).toBe("msg-1");
    warnSpy.mockRestore();
  });

  it("logs (does not throw) when fetch rejects", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("network down"));
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await expect(sender.sendToChannel("c", "x")).resolves.toBeUndefined();
    await vi.runAllTimersAsync();
    expect(errSpy).toHaveBeenCalledWith(expect.stringMatching(/network down/));
    errSpy.mockRestore();
  });

  it("drops oldest queued message when queue is full and resolves the dropped promise", async () => {
    // Hanging fetch + maxQueueSize=1 → first send drains and blocks on the
    // network, second send queues, third send forces queue overflow → second
    // is dropped (resolved) so its promise settles.
    const fetch = vi.fn().mockImplementation(() => new Promise(() => undefined));
    const sender = new BotSender("t", { fetch, minSpacingMs: 0, maxQueueSize: 1 });
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    void sender.sendToChannel("c", "first");
    const p2 = sender.sendToChannel("c", "second");
    void sender.sendToChannel("c", "third");
    await expect(p2).resolves.toBeUndefined();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it("sendToChannelAndReturnId extracts id from response JSON", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "msg-7777" }),
    });
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    const p = sender.sendToChannelAndReturnId("123", "hi");
    await vi.runAllTimersAsync();
    const result = await p;
    expect(result.messageId).toBe("msg-7777");
  });

  it("sendToChannelAndReturnId returns null when response body has no id", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({}),
    });
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    const p = sender.sendToChannelAndReturnId("123", "hi");
    await vi.runAllTimersAsync();
    const result = await p;
    expect(result.messageId).toBeNull();
  });

  it("Wave E-β replyToMessageId set → POST body carries message_reference with fail_if_not_exists:false", async () => {
    const fetch = mockFetchOK();
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    await sender.sendToChannel("123", "reply body", undefined, "head-msg-77");
    await vi.runAllTimersAsync();
    const body = readBody(fetch.mock.calls[0]);
    expect(body.message_reference).toEqual({ message_id: "head-msg-77", fail_if_not_exists: false });
  });

  it("Wave E-β replyToMessageId unset → POST body omits message_reference field entirely", async () => {
    const fetch = mockFetchOK();
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    await sender.sendToChannel("123", "standalone");
    await vi.runAllTimersAsync();
    const body = readBody(fetch.mock.calls[0]);
    expect(body.message_reference).toBeUndefined();
    expect("message_reference" in body).toBe(false);
  });

  it("Wave E-β sendToChannelAndReturnId forwards replyToMessageId into POST body", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "msg-9999" }),
    });
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    const p = sender.sendToChannelAndReturnId("123", "threaded", undefined, "head-x");
    await vi.runAllTimersAsync();
    const result = await p;
    expect(result.messageId).toBe("msg-9999");
    const body = readBody(fetch.mock.calls[0]);
    expect(body.message_reference).toEqual({ message_id: "head-x", fail_if_not_exists: false });
  });

  it("Wave E-β notifier replyToMessageId routing — commit 2: chained event passes message_reference into BotSender POST body", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "bot-msg-2" }),
    });
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const ctx = new InMemoryMessageContext();
    ctx.recordRoleMessage("P1", "executor", "exec-head-9", "dev");
    const fakeState = {
      getTask(taskId: string) {
        if (taskId === "task-B") return { id: "task-B", projectId: "P1" };
        return undefined;
      },
    } as unknown as import("../../src/lib/state.js").StateManager;
    const config = {
      bot_token_env: "T",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {},
    };
    const notifier = new DiscordNotifier(sender, config, { messageContext: ctx, stateManager: fakeState });
    notifier.handleEvent({ type: "task_done", taskId: "task-B" });
    await vi.runAllTimersAsync();
    expect(fetch).toHaveBeenCalledOnce();
    const body = readBody(fetch.mock.calls[0]);
    expect(body.message_reference).toEqual({ message_id: "exec-head-9", fail_if_not_exists: false });
  });

  it("addReaction PUTs to the reactions endpoint with url-encoded emoji", async () => {
    const fetch = mockFetchOK();
    const sender = new BotSender("t", { fetch });
    await sender.addReaction("123", "456", "👍");
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe(`https://discord.com/api/v10/channels/123/messages/456/reactions/${encodeURIComponent("👍")}/@me`);
    expect((init as { method: string }).method).toBe("PUT");
  });
});
