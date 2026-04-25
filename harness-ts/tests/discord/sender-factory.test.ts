import { describe, it, expect, vi } from "vitest";
import {
  buildSendersForChannels,
  extractWebhookIdFrom,
} from "../../src/discord/sender-factory.js";
import { BotSender } from "../../src/discord/bot-sender.js";
import { WebhookSender } from "../../src/discord/sender.js";
import type { DiscordConfig } from "../../src/lib/config.js";
import type { WebhookClient } from "../../src/discord/types.js";

function baseConfig(overrides: Partial<DiscordConfig> = {}): DiscordConfig {
  return {
    bot_token_env: "T",
    dev_channel: "100",
    ops_channel: "200",
    escalation_channel: "300",
    agents: {},
    ...overrides,
  };
}

describe("extractWebhookIdFrom", () => {
  it("extracts id from /api/webhooks/<id>/<token>", () => {
    expect(
      extractWebhookIdFrom("https://discord.com/api/webhooks/1234567890/abcXYZ"),
    ).toBe("1234567890");
  });

  it("extracts id from /api/v10/webhooks/<id>/<token>", () => {
    expect(
      extractWebhookIdFrom("https://discord.com/api/v10/webhooks/9876543210/tok"),
    ).toBe("9876543210");
  });

  it("returns null on malformed url", () => {
    expect(extractWebhookIdFrom("https://example.com/not-discord")).toBeNull();
    expect(extractWebhookIdFrom("")).toBeNull();
    expect(extractWebhookIdFrom("https://discord.com/api/webhooks/")).toBeNull();
  });

  // Phase 4 M1 (sec) — hostname-anchored regex must reject look-alike URLs.
  it("returns null for non-discord hostnames containing the path shape", () => {
    expect(extractWebhookIdFrom("https://evil.com/api/webhooks/123/abc/")).toBeNull();
    expect(extractWebhookIdFrom("https://evil.com/api/v10/webhooks/123/abc")).toBeNull();
  });

  it("returns null for embedded discord paths inside other-host URLs", () => {
    expect(
      extractWebhookIdFrom("https://evil.com?next=https://discord.com/api/webhooks/123/abc/"),
    ).toBeNull();
    expect(
      extractWebhookIdFrom("https://evil.com#https://discord.com/api/webhooks/123/abc"),
    ).toBeNull();
  });

  it("accepts discordapp.com and subdomains (canary)", () => {
    expect(extractWebhookIdFrom("https://discordapp.com/api/webhooks/111/tok")).toBe("111");
    expect(extractWebhookIdFrom("https://canary.discord.com/api/webhooks/222/tok")).toBe("222");
  });
});

describe("buildSendersForChannels", () => {
  it("channel WITHOUT webhook URL → BotSender (shared across non-webhook channels)", () => {
    const cfg = baseConfig();
    const senders = buildSendersForChannels(cfg, "tok");
    expect(senders["100"]).toBeInstanceOf(BotSender);
    expect(senders["200"]).toBeInstanceOf(BotSender);
    expect(senders["300"]).toBeInstanceOf(BotSender);
    // All share the SAME BotSender instance (rate-limit-bucketed by token).
    expect(senders["100"]).toBe(senders["200"]);
    expect(senders["200"]).toBe(senders["300"]);
  });

  it("channel WITH webhook URL → WebhookSender", () => {
    const cfg = baseConfig({
      webhooks: {
        dev: "https://discord.com/api/webhooks/111/devtok",
      },
    });
    const factoryCalls: string[] = [];
    const fakeClient: WebhookClient = { async send() { return undefined; } };
    const senders = buildSendersForChannels(cfg, "tok", {
      webhookClientFactory: (url) => {
        factoryCalls.push(url);
        return fakeClient;
      },
    });
    expect(senders["100"]).toBeInstanceOf(WebhookSender);
    expect(senders["200"]).toBeInstanceOf(BotSender);
    expect(senders["300"]).toBeInstanceOf(BotSender);
    expect(factoryCalls).toEqual(["https://discord.com/api/webhooks/111/devtok"]);
  });

  it("mixed channels: dev=webhook, ops=bot, escalation=webhook", () => {
    const cfg = baseConfig({
      webhooks: {
        dev: "https://discord.com/api/webhooks/111/dev",
        escalation: "https://discord.com/api/webhooks/333/esc",
      },
    });
    const fakeClient: WebhookClient = { async send() { return undefined; } };
    const senders = buildSendersForChannels(cfg, "tok", {
      webhookClientFactory: () => fakeClient,
    });
    expect(senders["100"]).toBeInstanceOf(WebhookSender);
    expect(senders["200"]).toBeInstanceOf(BotSender);
    expect(senders["300"]).toBeInstanceOf(WebhookSender);
  });

  it("uses MOCKED fetch for webhook client default — never makes a real network call", async () => {
    // Arrange: mock fetch as a vi.fn so we can detect any leakage.
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "msg-id-42" }),
    });
    const cfg = baseConfig({
      webhooks: {
        dev: "https://discord.com/api/webhooks/111/devtok",
      },
    });
    const senders = buildSendersForChannels(cfg, "tok", { fetch });
    const dev = senders["100"];
    expect(dev).toBeInstanceOf(WebhookSender);

    // Act: fire one send.
    const result = await dev.sendToChannelAndReturnId("100", "hi");

    // Assert: fetch invoked exactly once on the discord.com host (under our mock).
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0];
    expect(String(url)).toMatch(/^https:\/\/discord\.com\/api\/webhooks\/111\/devtok\?wait=true$/);
    expect((init as { method: string }).method).toBe("POST");
    expect(result.messageId).toBe("msg-id-42");
  });

  it("BotSender path also uses mocked fetch (no live network)", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "bot-msg-9" }),
    });
    const cfg = baseConfig();
    const senders = buildSendersForChannels(cfg, "tok", { fetch });
    const result = await senders["100"].sendToChannelAndReturnId("100", "hello");
    expect(fetch).toHaveBeenCalledOnce();
    expect(result.messageId).toBe("bot-msg-9");
  });

  it("skips empty channel ids (defensive — guards against config typos)", () => {
    const cfg = baseConfig({ dev_channel: "", ops_channel: "200", escalation_channel: "300" });
    const senders = buildSendersForChannels(cfg, "tok");
    expect(senders[""]).toBeUndefined();
    expect(senders["200"]).toBeInstanceOf(BotSender);
    expect(senders["300"]).toBeInstanceOf(BotSender);
  });

  // Phase 4 H1 (sec) — fetch-based webhook client retries once on 429.
  it("fetchWebhookClient retries once on 429 after retry_after sleep", async () => {
    vi.useFakeTimers();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      const fetch = vi
        .fn()
        .mockResolvedValueOnce({
          ok: false,
          status: 429,
          statusText: "Too Many Requests",
          json: async () => ({ retry_after: 0.5 }),
        })
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          statusText: "OK",
          json: async () => ({ id: "msg-after-429" }),
        });
      const cfg = baseConfig({
        webhooks: { dev: "https://discord.com/api/webhooks/111/devtok" },
      });
      const senders = buildSendersForChannels(cfg, "tok", { fetch });
      const dev = senders["100"];
      const p = dev.sendToChannelAndReturnId("100", "hi");
      // First fetch already issued; advance through 500ms retry-after sleep.
      await vi.advanceTimersByTimeAsync(501);
      await vi.runAllTimersAsync();
      const result = await p;
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(result.messageId).toBe("msg-after-429");
      expect(warnSpy).toHaveBeenCalledWith(expect.stringMatching(/429/));
    } finally {
      warnSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("fetchWebhookClient throws after second 429 (single retry only)", async () => {
    vi.useFakeTimers();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const fetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        statusText: "Too Many Requests",
        json: async () => ({ retry_after: 0.1 }),
      });
      const cfg = baseConfig({
        webhooks: { dev: "https://discord.com/api/webhooks/111/devtok" },
      });
      const senders = buildSendersForChannels(cfg, "tok", { fetch });
      const dev = senders["100"];
      const p = dev.sendToChannelAndReturnId("100", "hi");
      await vi.advanceTimersByTimeAsync(150);
      await vi.runAllTimersAsync();
      const result = await p;
      // Second 429 → WebhookSender outer wrapper logs + resolves messageId null.
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(result.messageId).toBeNull();
      expect(errSpy).toHaveBeenCalled();
    } finally {
      warnSpy.mockRestore();
      errSpy.mockRestore();
      vi.useRealTimers();
    }
  });
});
