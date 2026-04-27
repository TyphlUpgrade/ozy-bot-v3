/**
 * Universal Discord transcript recorder. Captures every outbound (sent by
 * orchestrator) and inbound (received from operator) Discord message to a
 * single file pair on disk so non-Discord readers (developers, AI assistants
 * helping with debugging) can inspect what was sent / received without
 * watching the channel live.
 *
 * Two responsibilities live here:
 *
 *   - `RecordingSender` (built via `wrapWithRecording`) — wraps any
 *     `DiscordSender`, records each send AFTER delegation completes (success
 *     or error path) so every record reflects "what actually happened" with
 *     full info (Discord-assigned messageId on success, error text on failure).
 *
 *   - `recordInbound(...)` — called by the inbound dispatcher to record
 *     received messages. The dispatcher gets an optional `transcriptWriter`
 *     constructor opt; when set, every `dispatch()` entry calls
 *     `recordInbound` with the inbound message details.
 *
 * Two parallel files are written:
 *
 *   - `*.jsonl` (machine-readable) — one JSON object per line, no trailing
 *     newline at EOF. Crash-safe: each line is a complete record.
 *
 *   - `*.md` (human-readable) — chat-style blocks with `>` blockquote prefix
 *     for inbound, no prefix for outbound. Truncated to 2000 chars to match
 *     Discord's send-content cap.
 *
 * **Performance**: `appendFileSync` is synchronous; for high-volume production
 * this could block the event loop. Acceptable for v1 — Discord rate limits
 * cap us at ~5 sends/sec anyway. Document the trade-off.
 *
 * **File rotation**: not implemented. Files grow forever. Operator can rotate
 * manually (move/truncate). Future work.
 *
 * **Privacy**: transcripts capture everything sent + received including
 * operator messages. `.harness/discord-transcript.*` lives behind the
 * `.harness/` gitignore prefix.
 *
 * I-1 preserved: this module imports types only from sibling discord modules.
 * No agent-layer / orchestrator / state coupling.
 */

import { existsSync, mkdirSync, writeFileSync, appendFileSync } from "node:fs";
import { dirname } from "node:path";
import type { AgentIdentity, AllowedMentions, DiscordSender } from "./types.js";

export type TranscriptDirection = "out" | "in";

export interface OutboundRecord {
  direction: "out";
  ts: string; // ISO8601
  channelId: string;
  identity?: { username: string; avatarURL?: string };
  content: string;
  allowedMentions?: AllowedMentions;
  replyToMessageId?: string;
  /** Discord-assigned id when sender returns one (sendToChannelAndReturnId). null otherwise. */
  resultMessageId?: string | null;
  error?: string; // if send threw
}

export interface InboundRecord {
  direction: "in";
  ts: string;
  channelId: string;
  authorUsername?: string;
  authorId?: string;
  content: string;
  isBot?: boolean;
  isOperatorMention?: boolean;
  /** Discord message id of the inbound message (for reply-chain debugging). */
  messageId?: string;
}

export type TranscriptRecord = OutboundRecord | InboundRecord;

export interface TranscriptWriterOpts {
  /** Absolute path to JSONL file. Will be truncated on construct unless append=true. */
  jsonlPath: string;
  /** Absolute path to Markdown file. Will be truncated on construct unless append=true. */
  mdPath: string;
  /** When true, append to existing files instead of truncating. Default false. */
  append?: boolean;
  /** Run header for the Markdown file (e.g., "smoke 2026-04-27 llm=true"). Ignored when append=true. */
  runHeader?: string;
}

const MD_CONTENT_TRUNCATE = 2000;

function errMsg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function ensureDir(filePath: string): void {
  const dir = dirname(filePath);
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
}

/**
 * Format the time-of-day component of an ISO timestamp into HH:MM:SS for the
 * Markdown header. Falls back to the raw ts string on any parse hiccup so the
 * record is never lost.
 */
function shortTime(ts: string): string {
  const m = ts.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : ts;
}

function truncateForMd(content: string): string {
  if (content.length <= MD_CONTENT_TRUNCATE) return content;
  return content.slice(0, MD_CONTENT_TRUNCATE) + "…";
}

function renderMarkdown(rec: TranscriptRecord): string {
  const time = shortTime(rec.ts);
  const channel = rec.channelId;
  if (rec.direction === "out") {
    const who = rec.identity?.username ?? "(no identity)";
    const errSuffix = rec.error ? ` — error: ${rec.error}` : "";
    const idSuffix = rec.resultMessageId ? ` — id ${rec.resultMessageId}` : "";
    const replySuffix = rec.replyToMessageId ? ` — reply→${rec.replyToMessageId}` : "";
    const header = `### ${time} — out — channel \`${channel}\` — ${who}${idSuffix}${replySuffix}${errSuffix}`;
    return `${header}\n\n${truncateForMd(rec.content)}\n\n---\n\n`;
  }
  // direction === "in"
  const who = rec.authorUsername ?? "(unknown)";
  const botSuffix = rec.isBot ? " [bot]" : "";
  const mentionSuffix = rec.isOperatorMention ? " [@operator]" : "";
  const idSuffix = rec.messageId ? ` — id ${rec.messageId}` : "";
  const header = `### ${time} — in — channel \`${channel}\` — ${who}${botSuffix}${mentionSuffix}${idSuffix}`;
  // Blockquote-prefix every line of inbound content.
  const body = truncateForMd(rec.content)
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n");
  return `${header}\n\n${body}\n\n---\n\n`;
}

export class TranscriptWriter {
  private readonly jsonlPath: string;
  private readonly mdPath: string;

  constructor(opts: TranscriptWriterOpts) {
    this.jsonlPath = opts.jsonlPath;
    this.mdPath = opts.mdPath;
    ensureDir(this.jsonlPath);
    ensureDir(this.mdPath);
    if (opts.append === true) {
      // Touch each file so subsequent appendFileSync calls land in a known
      // location even if no records arrive (operator can grep an empty file).
      if (!existsSync(this.jsonlPath)) writeFileSync(this.jsonlPath, "", "utf-8");
      if (!existsSync(this.mdPath)) {
        writeFileSync(this.mdPath, "", "utf-8");
      }
      return;
    }
    // Truncate both files on construction.
    writeFileSync(this.jsonlPath, "", "utf-8");
    const header =
      opts.runHeader !== undefined && opts.runHeader.length > 0
        ? `# Discord transcript — ${opts.runHeader}\n\n`
        : "# Discord transcript\n\n";
    writeFileSync(this.mdPath, header, "utf-8");
  }

  /** Append a record to both files (atomic per-line for JSONL; markdown formatted). */
  record(rec: TranscriptRecord): void {
    try {
      appendFileSync(this.jsonlPath, JSON.stringify(rec) + "\n", "utf-8");
    } catch (err) {
      console.error(`[TranscriptWriter] jsonl append failed: ${errMsg(err)}`);
    }
    try {
      appendFileSync(this.mdPath, renderMarkdown(rec), "utf-8");
    } catch (err) {
      console.error(`[TranscriptWriter] md append failed: ${errMsg(err)}`);
    }
  }
}

/**
 * Wrap a `DiscordSender` so every send is recorded to the supplied writer.
 * Records on completion (success path with resultMessageId) AND on catch
 * (error path with error message). A crash mid-await is the only case where
 * info is lost; at that point the process is dying anyway.
 *
 * `channelId` arg is captured for record-keeping; the actual channel passed
 * to the inner sender at call time is used for the record's `channelId`
 * field so per-channel routing remains observable.
 */
export function wrapWithRecording(
  inner: DiscordSender,
  _channelId: string,
  writer: TranscriptWriter,
): DiscordSender {
  return {
    async sendToChannel(channel, content, identity, replyToMessageId, allowedMentions) {
      const ts = new Date().toISOString();
      try {
        await inner.sendToChannel(channel, content, identity, replyToMessageId, allowedMentions);
        writer.record({
          direction: "out",
          ts,
          channelId: channel,
          identity,
          content,
          allowedMentions,
          replyToMessageId,
          resultMessageId: undefined,
        });
      } catch (err) {
        writer.record({
          direction: "out",
          ts,
          channelId: channel,
          identity,
          content,
          allowedMentions,
          replyToMessageId,
          error: errMsg(err),
        });
        throw err;
      }
    },
    async sendToChannelAndReturnId(channel, content, identity, replyToMessageId, allowedMentions) {
      const ts = new Date().toISOString();
      try {
        const result = await inner.sendToChannelAndReturnId(
          channel,
          content,
          identity,
          replyToMessageId,
          allowedMentions,
        );
        writer.record({
          direction: "out",
          ts,
          channelId: channel,
          identity,
          content,
          allowedMentions,
          replyToMessageId,
          resultMessageId: result.messageId,
        });
        return result;
      } catch (err) {
        writer.record({
          direction: "out",
          ts,
          channelId: channel,
          identity,
          content,
          allowedMentions,
          replyToMessageId,
          error: errMsg(err),
        });
        throw err;
      }
    },
    async addReaction(channelId, messageId, emoji) {
      // Reactions intentionally not recorded — they are bot-side acknowledgments,
      // not part of the message conversation flow. Keeps the transcript
      // signal-to-noise high.
      return inner.addReaction(channelId, messageId, emoji);
    },
  };
}

/** Record an inbound message (called from dispatcher). */
export function recordInbound(
  writer: TranscriptWriter,
  rec: Omit<InboundRecord, "direction">,
): void {
  writer.record({ direction: "in", ...rec });
}

// Type-only re-export so callers can spell `AgentIdentity` / `AllowedMentions`
// from this module if it's already imported. Prevents incidental round-trip
// imports through types.js for transcript consumers.
export type { AgentIdentity, AllowedMentions, DiscordSender };
