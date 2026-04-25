/**
 * CW-2 — Inbound bot gateway over raw Discord Gateway WebSocket (v10).
 * Performs IDENTIFY/HEARTBEAT/RESUME, decodes MESSAGE_CREATE, applies the
 * filter chain (rule 0/0a/0b/1) and emits `InboundMessage`s. Tests inject a
 * `WSLike` fake via `webSocketFactory`; production uses `ws` (loaded lazily).
 * Heartbeat is `setTimeout`-based so vitest fake timers can drive it.
 */

import { WebSocket } from "ws";
import type { BotGateway, InboundMessage, RawMessage } from "./types.js";

const GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json";
const INTENTS = (1 << 0) | (1 << 9) | (1 << 15); // GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT
const OP_DISPATCH = 0;
const OP_HEARTBEAT = 1;
const OP_IDENTIFY = 2;
const OP_RESUME = 6;
const OP_RECONNECT = 7;
const OP_INVALID_SESSION = 9;
const OP_HELLO = 10;
const OP_HEARTBEAT_ACK = 11;
const CONTENT_MISSING_THRESHOLD = 10;
const FATAL_CLOSE_CODES = new Set([4004, 4014]);
// Phase 4 M3 (CR) — exponential backoff for non-fatal close reconnects.
// 1s base, doubling per consecutive failure, capped at 30s. After 10
// consecutive non-fatal closes we escalate to process.exit(2) on the
// assumption that a non-recoverable issue has set in.
const RECONNECT_BACKOFF_BASE_MS = 1000;
const RECONNECT_BACKOFF_CAP_MS = 30000;
const RECONNECT_FAILURE_LIMIT = 10;

/** Minimal contract the `ws` `WebSocket` satisfies; lets tests inject fakes. */
export interface WSLike {
  onmessage: ((ev: { data: string | Buffer }) => void) | null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null;
  onerror: ((ev: { message?: string }) => void) | null;
  onopen?: (() => void) | null;
  send(data: string): void;
  close(code?: number): void;
  readyState: number;
}

export interface RawWsBotGatewayOptions {
  token: string;
  allowedChannelIds: string[];
  /** Test seam — defaults to `new WebSocket(GATEWAY_URL)` from `ws`. */
  webSocketFactory?: () => WSLike;
}

interface GatewayPayload { op: number; d?: unknown; s?: number | null; t?: string | null; }
interface MessageCreateRaw {
  id: string;
  channel_id: string;
  author?: { id?: string; username?: string; bot?: boolean } | null;
  webhook_id?: string;
  content?: string;
  timestamp?: string;
  message_reference?: { message_id?: string } | null;
  referenced_message?: { id?: string; author?: { username?: string } | null } | null;
}

export class RawWsBotGateway implements BotGateway {
  private readonly token: string;
  private readonly allowedChannelIds: ReadonlySet<string>;
  private readonly factory: () => WSLike;
  private ws: WSLike | null = null;
  private sessionId: string | null = null;
  private resumeUrl: string | null = null;
  private lastSeq: number | null = null;
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatIntervalMs = 0;
  private lastHeartbeatAcked = true;
  private selfWebhookIds: ReadonlySet<string> | null = null;
  private selfBotId: string | null = null;
  // CW-4.5 — captured at READY for `@<botUsername>` mention detection in dispatcher.
  private selfBotUsername: string | null = null;
  private readonly handlers: Array<(m: InboundMessage) => void> = [];
  private contentMissingHandler: (() => void) | null = null;
  private contentMissingFired = false;
  private readonly emptyContentCounters = new Map<string, number>();
  private readonly referenceCache = new Map<string, string>();
  // Phase 4 M3 — consecutive non-fatal close count, reset to 0 on READY.
  private consecutiveReconnectFailures = 0;

  constructor(opts: RawWsBotGatewayOptions) {
    if (!opts.token || opts.token.trim().length === 0) throw new Error("RawWsBotGateway: token must be a non-empty string");
    this.token = opts.token;
    this.allowedChannelIds = new Set(opts.allowedChannelIds);
    this.factory = opts.webSocketFactory ?? defaultFactory;
  }

  async start(): Promise<void> { this.openSocket(); }

  async stop(): Promise<void> {
    if (this.heartbeatTimer) { clearTimeout(this.heartbeatTimer); this.heartbeatTimer = null; }
    if (this.ws) this.ws.close(1000);
    this.ws = null;
  }

  on(handler: (m: InboundMessage) => void): void { this.handlers.push(handler); }

  registerSelfWebhookIds(ids: string[]): void {
    if (this.selfWebhookIds !== null) throw new Error("RawWsBotGateway: registerSelfWebhookIds called twice");
    this.selfWebhookIds = new Set(ids);
  }

  async fetchReferenceUsername(messageId: string, _channelId: string): Promise<string | null> {
    return this.referenceCache.get(messageId) ?? null;
  }

  onMessageContentMissing(handler: () => void): void { this.contentMissingHandler = handler; }

  /** CW-4.5 — bot's own Discord username, captured from READY. Null until READY fires. */
  getBotUsername(): string | null { return this.selfBotUsername; }

  private openSocket(): void {
    const ws = this.factory();
    this.ws = ws;
    ws.onmessage = (ev) => this.onSocketMessage(ev.data);
    ws.onclose = (ev) => this.onSocketClose(ev.code);
    ws.onerror = (ev) => console.error(`[RawWsBotGateway] socket error: ${ev.message ?? "unknown"}`);
  }

  private onSocketMessage(data: string | Buffer): void {
    let payload: GatewayPayload;
    try { payload = JSON.parse(typeof data === "string" ? data : data.toString("utf-8")) as GatewayPayload; }
    catch (err) { console.error(`[RawWsBotGateway] payload parse failed: ${errMsg(err)}`); return; }
    if (typeof payload.s === "number") this.lastSeq = payload.s;
    switch (payload.op) {
      case OP_HELLO: return this.handleHello((payload.d as { heartbeat_interval?: number } | undefined)?.heartbeat_interval ?? 41250);
      case OP_HEARTBEAT_ACK: this.lastHeartbeatAcked = true; return;
      case OP_RECONNECT:
      case OP_INVALID_SESSION: return this.resetAndReconnect();
      case OP_DISPATCH: return this.onDispatch(payload.t ?? "", payload.d);
      default: return;
    }
  }

  private handleHello(intervalMs: number): void {
    this.heartbeatIntervalMs = intervalMs;
    this.scheduleHeartbeat();
    if (this.sessionId && this.lastSeq !== null) this.sendResume();
    else this.sendIdentify();
  }

  private scheduleHeartbeat(): void {
    if (this.heartbeatTimer) clearTimeout(this.heartbeatTimer);
    this.heartbeatTimer = setTimeout(() => this.sendHeartbeat(), this.heartbeatIntervalMs);
  }

  private sendHeartbeat(): void {
    if (!this.ws) return;
    // Phase 4 M4 (CR) — zombie connection detection. If the previous heartbeat
    // was never ACKed by the time this one fires, the connection is stuck:
    // close with 4000 (non-fatal — instructs Discord to drop the session) and
    // reset session state so the reconnect path takes the IDENTIFY branch.
    if (!this.lastHeartbeatAcked) {
      console.warn("[RawWsBotGateway] heartbeat not acked — closing zombie connection (4000)");
      this.ws.close(4000);
      this.sessionId = null;
      this.lastSeq = null;
      if (this.heartbeatTimer) { clearTimeout(this.heartbeatTimer); this.heartbeatTimer = null; }
      return;
    }
    this.lastHeartbeatAcked = false;
    this.ws.send(JSON.stringify({ op: OP_HEARTBEAT, d: this.lastSeq }));
    this.scheduleHeartbeat();
  }

  private sendIdentify(): void {
    this.ws?.send(JSON.stringify({
      op: OP_IDENTIFY,
      d: { token: this.token, intents: INTENTS, properties: { os: "linux", browser: "harness-ts", device: "harness-ts" } },
    }));
  }

  private sendResume(): void {
    this.ws?.send(JSON.stringify({
      op: OP_RESUME,
      d: { token: this.token, session_id: this.sessionId, seq: this.lastSeq },
    }));
  }

  private resetAndReconnect(): void {
    this.sessionId = null;
    this.lastSeq = null;
    if (this.heartbeatTimer) { clearTimeout(this.heartbeatTimer); this.heartbeatTimer = null; }
    if (this.ws) this.ws.close(4000);
    setTimeout(() => this.openSocket(), this.reconnectDelayMs());
  }

  /**
   * Phase 4 M3 (CR) — exponential backoff for non-fatal close reconnects.
   * Returns 1s, 2s, 4s, ... capped at 30s. The counter increments on every
   * non-fatal close in `onSocketClose` and resets to 0 on READY.
   */
  private reconnectDelayMs(): number {
    const exp = Math.min(this.consecutiveReconnectFailures, 5); // 2**5 = 32 → cap engages
    const ms = RECONNECT_BACKOFF_BASE_MS * 2 ** exp;
    return Math.min(ms, RECONNECT_BACKOFF_CAP_MS);
  }

  private onDispatch(t: string, d: unknown): void {
    if (t === "READY") {
      const r = d as { session_id?: string; resume_gateway_url?: string; user?: { id?: string; username?: string } };
      this.sessionId = r.session_id ?? null;
      this.resumeUrl = r.resume_gateway_url ?? null;
      this.selfBotId = r.user?.id ?? null;
      // CW-4.5 — capture username for `@<bot>` mention detection.
      this.selfBotUsername = r.user?.username ?? null;
      // Phase 4 M3 — successful READY clears the consecutive-close counter.
      this.consecutiveReconnectFailures = 0;
      return;
    }
    if (t !== "MESSAGE_CREATE") return;
    const raw = this.decodeMessageCreate(d as MessageCreateRaw);
    if (!raw) return;
    this.checkMessageContentSentinel(raw);
    if (!this.passesFilters(raw)) return;
    for (const h of this.handlers) {
      try { h(raw); } catch (err) { console.error(`[RawWsBotGateway] handler threw: ${errMsg(err)}`); }
    }
  }

  private decodeMessageCreate(d: MessageCreateRaw): RawMessage | null {
    if (!d || !d.id || !d.channel_id) return null;
    if (d.referenced_message?.id && d.referenced_message.author?.username) {
      this.referenceCache.set(d.referenced_message.id, d.referenced_message.author.username);
    }
    return {
      messageId: d.id,
      channelId: d.channel_id,
      authorId: d.author?.id ?? "",
      authorUsername: d.author?.username ?? "",
      isBot: d.author?.bot === true,
      webhookId: typeof d.webhook_id === "string" ? d.webhook_id : null,
      content: typeof d.content === "string" ? d.content : "",
      repliedToMessageId: d.message_reference?.message_id ?? null,
      repliedToAuthorUsername: d.referenced_message?.author?.username ?? null,
      timestamp: d.timestamp ?? "",
    };
  }

  private passesFilters(m: RawMessage): boolean {
    // Phase 4 M2 — fail-closed before READY: if selfBotId is null we haven't
    // received READY yet, so we cannot reliably distinguish our own messages
    // from operator input. Drop non-webhook messages until READY lands.
    // Webhook messages remain filterable by selfWebhookIds set so they are
    // not blanket-dropped here.
    if (this.selfBotId === null && !m.webhookId) return false;
    if (this.selfBotId && m.authorId === this.selfBotId) return false;
    if (m.webhookId && this.selfWebhookIds?.has(m.webhookId)) return false;
    if (m.isBot && (!m.webhookId || !this.selfWebhookIds?.has(m.webhookId))) return false;
    if (!this.allowedChannelIds.has(m.channelId)) return false;
    return true;
  }

  private checkMessageContentSentinel(m: RawMessage): void {
    if (this.contentMissingFired) return;
    if (m.content.length > 0) { this.emptyContentCounters.set(m.channelId, 0); return; }
    const next = (this.emptyContentCounters.get(m.channelId) ?? 0) + 1;
    this.emptyContentCounters.set(m.channelId, next);
    if (next < CONTENT_MISSING_THRESHOLD) return;
    this.contentMissingFired = true;
    try { this.contentMissingHandler?.(); }
    catch (err) { console.error(`[RawWsBotGateway] contentMissingHandler threw: ${errMsg(err)}`); }
  }

  private onSocketClose(code: number): void {
    if (this.heartbeatTimer) { clearTimeout(this.heartbeatTimer); this.heartbeatTimer = null; }
    if (FATAL_CLOSE_CODES.has(code)) {
      console.error(`[RawWsBotGateway] fatal close ${code} — auth failure or disallowed intents; exiting`);
      process.exit(2);
    }
    // Phase 4 M3 (CR) — track consecutive non-fatal closes. After 10 in a row
    // without a successful READY, escalate to operator (process.exit) — at that
    // point we are stuck in a reconnect loop and should not keep silently
    // burning Discord rate limits.
    this.consecutiveReconnectFailures += 1;
    if (this.consecutiveReconnectFailures >= RECONNECT_FAILURE_LIMIT) {
      console.error(
        `[RawWsBotGateway] ${this.consecutiveReconnectFailures} consecutive non-fatal closes — escalating to operator (exit 2)`,
      );
      process.exit(2);
    }
    setTimeout(() => this.openSocket(), this.reconnectDelayMs());
  }
}

function errMsg(err: unknown): string { return err instanceof Error ? err.message : String(err); }

function defaultFactory(): WSLike {
  // Production path. Tests inject `webSocketFactory` and never reach this branch.
  // `ws` exposes browser-style onmessage/onclose/onerror compatible with WSLike.
  return new WebSocket(GATEWAY_URL) as unknown as WSLike;
}
