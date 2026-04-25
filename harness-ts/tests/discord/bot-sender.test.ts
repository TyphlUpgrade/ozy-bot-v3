import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { BotSender } from "../../src/discord/bot-sender.js";

function mockFetchOK(): ReturnType<typeof vi.fn> {
  return vi.fn().mockResolvedValue({ ok: true, status: 200, statusText: "OK" });
}

function readBody(call: unknown): { content: string; allowed_mentions?: { parse: string[] } } {
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
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 429, statusText: "Too Many Requests" });
    const sender = new BotSender("t", { fetch, minSpacingMs: 0 });
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await sender.sendToChannel("c", "x");
    await vi.runAllTimersAsync();
    expect(errSpy).toHaveBeenCalledWith(expect.stringMatching(/429/));
    errSpy.mockRestore();
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
