import { describe, it, expect, vi } from "vitest";
import { WebhookSender } from "../../src/discord/sender.js";
import type { AllowedMentions, WebhookClient } from "../../src/discord/types.js";

function makeWebhook(opts: { fail?: boolean; returnId?: string | null } = {}) {
  const calls: Array<{ content: string; username?: string; avatarURL?: string; allowedMentions?: AllowedMentions; wait?: boolean }> = [];
  const client: WebhookClient = {
    async send(options) {
      calls.push(options);
      if (opts.fail) throw new Error("webhook 500");
      // CW-1 — when `returnId` is set return a fake message object so
      // `sendToChannelAndReturnId` can extract `.id`. `null` simulates a
      // client that doesn't return the id.
      if (opts.returnId !== undefined) {
        return opts.returnId === null ? undefined : { id: opts.returnId };
      }
      return undefined;
    },
  };
  return { client, calls };
}

describe("WebhookSender", () => {
  it("sends with identity → forwards username + avatar", async () => {
    const { client, calls } = makeWebhook();
    const s = new WebhookSender(client, { minSpacingMs: 0 });
    await s.sendToChannel("dev", "hello", { username: "Harness", avatarURL: "https://h" });
    expect(calls).toHaveLength(1);
    expect(calls[0].content).toBe("hello");
    expect(calls[0].username).toBe("Harness");
    expect(calls[0].avatarURL).toBe("https://h");
  });

  it("sends without identity → username + avatarURL undefined", async () => {
    const { client, calls } = makeWebhook();
    const s = new WebhookSender(client, { minSpacingMs: 0 });
    await s.sendToChannel("dev", "plain");
    expect(calls[0].username).toBeUndefined();
    expect(calls[0].avatarURL).toBeUndefined();
  });

  it("every send sets allowedMentions: { parse: [] } (block @everyone/@here)", async () => {
    const { client, calls } = makeWebhook();
    const s = new WebhookSender(client, { minSpacingMs: 0 });
    await s.sendToChannel("dev", "no pings here");
    expect(calls[0].allowedMentions).toEqual({ parse: [] });
  });

  it("webhook errors are swallowed (promise still resolves) and log .message only", async () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { client } = makeWebhook({ fail: true });
    const s = new WebhookSender(client, { minSpacingMs: 0 });
    await expect(s.sendToChannel("dev", "x")).resolves.toBeUndefined();
    expect(consoleSpy).toHaveBeenCalled();
    // Error log should contain the message "webhook 500" but not the full request body "x"
    const logged = consoleSpy.mock.calls[0][0] as string;
    expect(logged).toMatch(/webhook 500/);
    expect(logged).not.toMatch(/: x$/);
    consoleSpy.mockRestore();
  });

  it("addReaction is a no-op", async () => {
    const { client, calls } = makeWebhook();
    const s = new WebhookSender(client, { minSpacingMs: 0 });
    await s.addReaction("ch", "msg", "eyes");
    expect(calls).toHaveLength(0);
  });

  it("enforces minSpacingMs between sends", async () => {
    const { client, calls } = makeWebhook();
    const s = new WebhookSender(client, { minSpacingMs: 50 });
    const t0 = Date.now();
    await Promise.all([
      s.sendToChannel("dev", "a"),
      s.sendToChannel("dev", "b"),
      s.sendToChannel("dev", "c"),
    ]);
    const elapsed = Date.now() - t0;
    expect(calls).toHaveLength(3);
    // 3 sends with 50ms spacing between = at least ~90ms total (timer jitter tolerance)
    expect(elapsed).toBeGreaterThanOrEqual(80);
  });

  it("sendToChannelAndReturnId captures id from webhook client response", async () => {
    const { client } = makeWebhook({ returnId: "wm-42" });
    const s = new WebhookSender(client, { minSpacingMs: 0 });
    const result = await s.sendToChannelAndReturnId("dev", "hi");
    expect(result.messageId).toBe("wm-42");
  });

  it("sendToChannelAndReturnId returns null when webhook client returns no payload", async () => {
    const { client } = makeWebhook({ returnId: null });
    const s = new WebhookSender(client, { minSpacingMs: 0 });
    const result = await s.sendToChannelAndReturnId("dev", "hi");
    expect(result.messageId).toBeNull();
  });

  it("queue overflow drops OLDEST (not newest) when maxQueueSize exceeded", async () => {
    const consoleSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { client, calls } = makeWebhook();
    const s = new WebhookSender(client, { minSpacingMs: 40, maxQueueSize: 2 });

    // Burst 4 messages; queue cap 2, so at least one of the middle two must be
    // dropped. msg1 is already in-flight when msg4 arrives.
    await Promise.all([
      s.sendToChannel("dev", "msg1"),
      s.sendToChannel("dev", "msg2"),
      s.sendToChannel("dev", "msg3"),
      s.sendToChannel("dev", "msg4"),
    ]);

    // Verify drop-oldest semantics: msg1 (first in) sent, msg4 (latest) sent,
    // and at least one of msg2/msg3 dropped.
    const sentContents = calls.map((c) => c.content);
    expect(consoleSpy).toHaveBeenCalled();
    expect(sentContents).toContain("msg1");
    expect(sentContents).toContain("msg4");
    expect(sentContents.length).toBeLessThan(4);
    // The dropped message was an OLDER one, not msg4
    expect(sentContents.length).toBeGreaterThanOrEqual(2);
    consoleSpy.mockRestore();
  });
});
