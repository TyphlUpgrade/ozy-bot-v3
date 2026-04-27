/**
 * Universal transcript module unit tests. Filesystem isolated via tmpdir +
 * randomUUID; no live Discord traffic.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

import {
  TranscriptWriter,
  wrapWithRecording,
  recordInbound,
} from "../../src/discord/transcript.js";
import type { DiscordSender } from "../../src/discord/types.js";

function tmpPaths(): { dir: string; jsonl: string; md: string } {
  const dir = join(tmpdir(), `transcript-test-${randomUUID()}`);
  mkdirSync(dir, { recursive: true });
  return { dir, jsonl: join(dir, "out.jsonl"), md: join(dir, "out.md") };
}

function makeFakeSender(opts: { failSend?: boolean; returnId?: string | null } = {}) {
  const sent: Array<{
    channel: string;
    content: string;
    identity?: { username: string; avatarURL?: string };
    replyToMessageId?: string;
  }> = [];
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity, replyToMessageId) {
      if (opts.failSend) throw new Error("discord down");
      sent.push({ channel, content, identity, replyToMessageId });
    },
    async sendToChannelAndReturnId(channel, content, identity, replyToMessageId) {
      if (opts.failSend) throw new Error("discord down");
      sent.push({ channel, content, identity, replyToMessageId });
      return { messageId: opts.returnId ?? null };
    },
    async addReaction() {
      /* no-op */
    },
  };
  return { sender, sent };
}

describe("TranscriptWriter", () => {
  it("creates files at constructed paths", () => {
    const p = tmpPaths();
    new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md, runHeader: "test-run" });
    expect(existsSync(p.jsonl)).toBe(true);
    expect(existsSync(p.md)).toBe(true);
  });

  it("truncates files on construct (default)", () => {
    const p = tmpPaths();
    // Pre-populate both files with stale content.
    writeFileSync(p.jsonl, "stale-jsonl\n", "utf-8");
    writeFileSync(p.md, "stale-md\n", "utf-8");
    new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md, runHeader: "fresh" });
    expect(readFileSync(p.jsonl, "utf-8")).toBe("");
    const md = readFileSync(p.md, "utf-8");
    expect(md).toContain("# Discord transcript");
    expect(md).toContain("fresh");
    expect(md).not.toContain("stale-md");
  });

  it("appends when append=true (does not truncate)", () => {
    const p = tmpPaths();
    writeFileSync(p.jsonl, '{"prior":"jsonl"}\n', "utf-8");
    writeFileSync(p.md, "prior-md-content\n", "utf-8");
    const w = new TranscriptWriter({
      jsonlPath: p.jsonl,
      mdPath: p.md,
      append: true,
    });
    w.record({
      direction: "out",
      ts: "2026-04-27T14:02:31.000Z",
      channelId: "dev",
      content: "appended",
    });
    const jsonl = readFileSync(p.jsonl, "utf-8");
    expect(jsonl).toContain('{"prior":"jsonl"}');
    expect(jsonl).toContain('"content":"appended"');
    const md = readFileSync(p.md, "utf-8");
    expect(md).toContain("prior-md-content");
    expect(md).toContain("appended");
  });

  it("record() writes a JSONL line that round-trips parse", () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    w.record({
      direction: "out",
      ts: "2026-04-27T14:02:31.000Z",
      channelId: "dev-123",
      identity: { username: "Architect", avatarURL: "https://a" },
      content: "hello world",
      resultMessageId: "msg-99",
    });
    const lines = readFileSync(p.jsonl, "utf-8").split("\n").filter((l) => l.length > 0);
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0]) as Record<string, unknown>;
    expect(parsed.direction).toBe("out");
    expect(parsed.channelId).toBe("dev-123");
    expect(parsed.content).toBe("hello world");
    expect(parsed.resultMessageId).toBe("msg-99");
    expect((parsed.identity as { username: string }).username).toBe("Architect");
  });

  it("record() writes a Markdown block with timestamp + direction + channel + identity", () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md, runHeader: "smoke" });
    w.record({
      direction: "out",
      ts: "2026-04-27T14:02:31.000Z",
      channelId: "dev",
      identity: { username: "Architect", avatarURL: "https://a" },
      content: "I've decomposed proj-eg-1 into 3 phases.",
    });
    const md = readFileSync(p.md, "utf-8");
    expect(md).toContain("14:02:31");
    expect(md).toContain("out");
    expect(md).toContain("`dev`");
    expect(md).toContain("Architect");
    expect(md).toContain("I've decomposed proj-eg-1 into 3 phases.");
  });

  it("inbound markdown rendered with blockquote prefix", () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    w.record({
      direction: "in",
      ts: "2026-04-27T14:02:35.000Z",
      channelId: "dev",
      authorUsername: "alice",
      content: "@architect can you check redis?",
      messageId: "in-1",
    });
    const md = readFileSync(p.md, "utf-8");
    expect(md).toContain("14:02:35");
    expect(md).toContain("in");
    expect(md).toContain("alice");
    expect(md).toContain("> @architect can you check redis?");
  });

  it("md content truncates to 2000 chars; jsonl preserves full content", () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    const big = "x".repeat(2500);
    w.record({
      direction: "out",
      ts: "2026-04-27T14:00:00.000Z",
      channelId: "dev",
      content: big,
    });
    const jsonl = readFileSync(p.jsonl, "utf-8");
    const parsed = JSON.parse(jsonl.split("\n").filter((l) => l.length > 0)[0]) as {
      content: string;
    };
    expect(parsed.content.length).toBe(2500);
    const md = readFileSync(p.md, "utf-8");
    // Truncated body has 2000 x's followed by an ellipsis.
    expect(md).toContain("x".repeat(2000) + "…");
    // Full 2500-char body must NOT appear in md.
    expect(md.includes("x".repeat(2500))).toBe(false);
  });
});

describe("wrapWithRecording", () => {
  it("delegates sendToChannel to inner sender on success path", async () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    const { sender, sent } = makeFakeSender();
    const wrapped = wrapWithRecording(sender, "dev", w);
    await wrapped.sendToChannel("dev", "hello", { username: "Architect", avatarURL: "https://a" });
    expect(sent).toHaveLength(1);
    expect(sent[0].content).toBe("hello");
    const lines = readFileSync(p.jsonl, "utf-8").split("\n").filter((l) => l.length > 0);
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0]) as Record<string, unknown>;
    expect(parsed.direction).toBe("out");
    expect(parsed.content).toBe("hello");
    expect(parsed.error).toBeUndefined();
  });

  it("delegates sendToChannelAndReturnId and records resultMessageId on success", async () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    const { sender } = makeFakeSender({ returnId: "msg-42" });
    const wrapped = wrapWithRecording(sender, "dev", w);
    const result = await wrapped.sendToChannelAndReturnId("dev", "yo");
    expect(result.messageId).toBe("msg-42");
    const lines = readFileSync(p.jsonl, "utf-8").split("\n").filter((l) => l.length > 0);
    const parsed = JSON.parse(lines[0]) as Record<string, unknown>;
    expect(parsed.resultMessageId).toBe("msg-42");
    expect(parsed.error).toBeUndefined();
  });

  it("records error case and re-throws when inner sender throws (sendToChannel)", async () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    const { sender } = makeFakeSender({ failSend: true });
    const wrapped = wrapWithRecording(sender, "dev", w);
    await expect(wrapped.sendToChannel("dev", "boom")).rejects.toThrow("discord down");
    const lines = readFileSync(p.jsonl, "utf-8").split("\n").filter((l) => l.length > 0);
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0]) as Record<string, unknown>;
    expect(parsed.error).toBe("discord down");
    expect(parsed.resultMessageId).toBeUndefined();
  });

  it("records error case and re-throws when inner sender throws (sendToChannelAndReturnId)", async () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    const { sender } = makeFakeSender({ failSend: true });
    const wrapped = wrapWithRecording(sender, "dev", w);
    await expect(wrapped.sendToChannelAndReturnId("dev", "boom")).rejects.toThrow("discord down");
    const lines = readFileSync(p.jsonl, "utf-8").split("\n").filter((l) => l.length > 0);
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0]) as Record<string, unknown>;
    expect(parsed.error).toBe("discord down");
  });

  it("does NOT record reactions (signal-to-noise)", async () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    const { sender } = makeFakeSender();
    const wrapped = wrapWithRecording(sender, "dev", w);
    await wrapped.addReaction("dev", "msg-1", "👀");
    const jsonl = readFileSync(p.jsonl, "utf-8");
    expect(jsonl).toBe("");
  });

  it("forwards allowedMentions + replyToMessageId through to inner sender and into record", async () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    const { sender, sent } = makeFakeSender();
    const wrapped = wrapWithRecording(sender, "dev", w);
    await wrapped.sendToChannel(
      "dev",
      "ping",
      { username: "Op", avatarURL: "https://o" },
      "head-1",
      { users: ["123"] },
    );
    expect(sent[0].replyToMessageId).toBe("head-1");
    const parsed = JSON.parse(
      readFileSync(p.jsonl, "utf-8").split("\n").filter((l) => l.length > 0)[0],
    ) as Record<string, unknown>;
    expect(parsed.replyToMessageId).toBe("head-1");
    expect((parsed.allowedMentions as { users: string[] }).users).toEqual(["123"]);
  });
});

describe("recordInbound", () => {
  it("writes inbound block to jsonl + md with direction='in'", () => {
    const p = tmpPaths();
    const w = new TranscriptWriter({ jsonlPath: p.jsonl, mdPath: p.md });
    recordInbound(w, {
      ts: "2026-04-27T14:05:00.000Z",
      channelId: "dev",
      authorUsername: "alice",
      authorId: "111",
      content: "hi",
      isBot: false,
      messageId: "in-7",
    });
    const lines = readFileSync(p.jsonl, "utf-8").split("\n").filter((l) => l.length > 0);
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0]) as Record<string, unknown>;
    expect(parsed.direction).toBe("in");
    expect(parsed.authorUsername).toBe("alice");
    expect(parsed.messageId).toBe("in-7");
    const md = readFileSync(p.md, "utf-8");
    expect(md).toContain("> hi");
    expect(md).toContain("alice");
  });
});
