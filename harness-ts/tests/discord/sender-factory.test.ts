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
});
