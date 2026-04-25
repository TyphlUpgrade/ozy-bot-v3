/**
 * CW-3 — InboundDispatcher: routes filtered `InboundMessage`s from BotGateway
 * into either `architectManager.relayOperatorInput` (when the message is a
 * Discord reply to an agent's outbound post) or `commandRouter` (everything
 * else). Implements the precedence rules 2a/2b/3/4/5 from the conversational
 * Discord plan.
 *
 * Reply routing always wins over NL parsing: if the operator clicked
 * "reply" on an agent message AND we have a recorded projectId for that
 * message id, the relay fires regardless of the message content shape.
 *
 * `senders[channelId]` lookup uses optional chaining — a missing channel
 * sender (config drift) becomes a `console.warn` + silent skip, never a crash.
 */

import type { ArchitectManager } from "../session/architect.js";
import type { DiscordConfig } from "../lib/config.js";
import type { ProjectStore } from "../lib/project.js";
import type { ChannelContextBuffer } from "./channel-context.js";
import type { CommandRouter } from "./commands.js";
import type { IdentityMap } from "./identity-map.js";
import type { MessageContext } from "./message-context.js";
import type { DiscordSender, InboundMessage } from "./types.js";

// --- CW-4.5 mention extraction (exported for unit test) ---

export interface AgentMention {
  /** Agent key resolved via IdentityMap.lookup. */
  agentKey: string;
  /** Verbatim text matched (e.g., "@architect-x" — for diagnostics/logs). */
  raw: string;
  /** Char index in (already backtick-stripped) content. */
  index: number;
}

export interface ExtractedMentions {
  /** All resolved agent mentions, in occurrence order. v1 dispatches the first only. */
  mentions: AgentMention[];
  /**
   * Content with RESOLVED mentions stripped (replaced by single space) AND
   * any bot-self mention stripped. Unresolved `@strangers` are left intact
   * so the classifier sees them as DATA. Adjacent whitespace collapsed.
   */
  cleanedContent: string;
  /** True iff a `<@<botId>>`, `<@!<botId>>`, or `@<botUsername>` mention was found. */
  botMentioned: boolean;
}

/**
 * Replace backtick-fenced regions with spaces of equal length so subsequent
 * mention regexes see them as whitespace (preserves character indices).
 * Order matters: triple-backtick first (greedy), then single-backtick.
 */
function stripBacktickRegions(content: string): string {
  let s = content;
  // Triple-backtick: ```...```
  s = s.replace(/```[\s\S]*?```/g, (m) => " ".repeat(m.length));
  // Single-backtick: `...`
  s = s.replace(/`[^`]*`/g, (m) => " ".repeat(m.length));
  return s;
}

// CW-4.5 — Discord ID mention forms.
const DISCORD_ID_MENTION = /<@!?(\d+)>/g;
// CW-4.5 — Plain `@username` form. Tightened anchor (Iteration 2 change #4):
// require start-of-string-or-whitespace before `@`, and end-of-string,
// whitespace, or specific punctuation after the name. Avoids matches inside
// `email@host`, `user-architect-x-foo`, or `architect.com`.
const PLAIN_USERNAME_MENTION = /(?:^|\s)@([A-Za-z0-9_-]{1,32})(?=$|\s|[.,!?;:)\]])/gi;

/**
 * CW-4.5 §5.3 — extract `@agent`, `<@id>`, `<@!id>`, and `@<botUsername>`
 * mentions. Strip-only-resolved semantics: only mentions that resolve to a
 * known agent (via IdentityMap.lookup) OR the bot itself are stripped from
 * cleanedContent. Unresolved `@strangers` stay intact so the classifier sees
 * them as data.
 */
export function extractMentions(
  content: string,
  identityMap: IdentityMap,
  selfBotUsername: string | null,
  selfBotId: string | null,
): ExtractedMentions {
  const mentions: AgentMention[] = [];
  let botMentioned = false;
  // Spans (in stripped-coords) to remove from cleanedContent. Each entry is
  // [start, end] inclusive-exclusive; possibly preceded by whitespace.
  const stripSpans: Array<[number, number]> = [];

  const stripped = stripBacktickRegions(content);

  // Pass 1 — Discord ID forms (always preferred for bot-self).
  for (const m of stripped.matchAll(DISCORD_ID_MENTION)) {
    if (m.index === undefined) continue;
    const id = m[1];
    if (selfBotId !== null && id === selfBotId) {
      botMentioned = true;
      stripSpans.push([m.index, m.index + m[0].length]);
    }
    // Agent-IDs not resolved this wave (Iteration 2 change #1) — leave intact.
  }

  // Pass 2 — Plain `@<name>` form.
  const lowerSelf = selfBotUsername !== null ? selfBotUsername.toLowerCase() : null;
  for (const m of stripped.matchAll(PLAIN_USERNAME_MENTION)) {
    if (m.index === undefined) continue;
    const name = m[1];
    // The match[0] includes the leading whitespace if any; compute the @-start.
    const atIndex = stripped.indexOf("@", m.index);
    if (atIndex < 0) continue;
    const endIndex = atIndex + 1 + name.length;
    if (lowerSelf !== null && name.toLowerCase() === lowerSelf) {
      botMentioned = true;
      stripSpans.push([atIndex, endIndex]);
      continue;
    }
    const agentKey = identityMap.lookup(name);
    if (agentKey !== null) {
      mentions.push({ agentKey, raw: `@${name}`, index: atIndex });
      stripSpans.push([atIndex, endIndex]);
    }
    // Else: unresolved — leave intact in cleanedContent.
  }

  if (mentions.length > 1) {
    console.warn("[InboundDispatcher] multi-mention detected; dispatching first only");
  }

  // Build cleanedContent by removing strip spans (sorted by start, no overlaps
  // expected since matches are non-overlapping).
  stripSpans.sort((a, b) => a[0] - b[0]);
  let cleaned = "";
  let cursor = 0;
  for (const [start, end] of stripSpans) {
    if (start < cursor) continue;
    cleaned += stripped.slice(cursor, start);
    cleaned += " ";
    cursor = end;
  }
  cleaned += stripped.slice(cursor);
  // Collapse runs of whitespace to a single space, trim ends.
  cleaned = cleaned.replace(/\s+/g, " ").trim();

  return { mentions, cleanedContent: cleaned, botMentioned };
}

// --- Project resolution result ---

type ResolveProjectResult =
  | { projectId: string; reason: "affinity_hint" | "single_active" }
  | { projectId: null; reason: "no_active" | "multi_active_no_hint" | "no_project_store" };

export type RelayFailureKind =
  | "no_session"
  | "session_terminated"
  | "queue_full"
  | "generic";

export interface InboundDispatcherDeps {
  commandRouter: CommandRouter;
  /** Pick<> so tests can inject a minimal stub without faking the full ArchitectManager surface. */
  architectManager: Pick<ArchitectManager, "relayOperatorInput">;
  identityMap: IdentityMap;
  /** Per-channel sender map keyed by Discord channel id (from sender-factory.ts). */
  senders: Record<string, DiscordSender>;
  config: DiscordConfig;
  messageContext: MessageContext;
  // CW-4.5 v2 additions — all optional for back-compat. When ANY is missing,
  // mention rule 1 short-circuits to "fall through to existing rules".
  projectStore?: Pick<ProjectStore, "getAllProjects" | "getProject">;
  channelBuffer?: Pick<ChannelContextBuffer, "recent">;
  /** Iteration 2 change #2 — narrow seam, no full BotGateway dep. */
  getBotUsername?: () => string | null;
}

/**
 * Classify a `relayOperatorInput` failure into one of four operator-visible
 * kinds. Order matters: "no_session" is the most specific (architect.ts throws
 * `No Architect session for ...` on a missing session), so check it first.
 *
 * Phase 4 H2 (CR) — production reality check: today, `architectManager
 * .relayOperatorInput` only throws `No Architect session for ${projectId}`,
 * so only the `no_session` and `generic` branches are exercised by real
 * traffic. The `session_terminated` (matches /session terminated|aborted/i)
 * and `queue_full` (matches /queue full/i) regexes are intentionally retained
 * forward-looking — they pre-classify error shapes the architect layer is
 * expected to add later (typed termination errors, queue-overflow surface).
 * Removing them now would just force re-introducing them when those errors
 * land. Synthetic test cases for these branches exist in `dispatcher.test.ts`
 * to lock in the routing contract; they are not exercising real production
 * paths today.
 */
export function classifyRelayError(err: Error): RelayFailureKind {
  const msg = err.message;
  if (msg.startsWith("No Architect session for")) return "no_session";
  if (/session terminated|aborted/i.test(msg)) return "session_terminated";
  if (/queue full/i.test(msg)) return "queue_full";
  return "generic";
}

/** Operator-visible reply for each relay failure kind. */
export function relayFailureMessage(
  kind: RelayFailureKind,
  projectId: string,
  raw: string,
): string {
  switch (kind) {
    case "no_session":
      return `Project \`${projectId}\` has no live Architect session — it may have completed or been aborted. Use \`!project ${projectId} status\`.`;
    case "session_terminated":
      return `Architect session for \`${projectId}\` was terminated. Re-issue via \`!project <name>\` to spawn a new one.`;
    case "queue_full":
      return `Discord send queue is full — your reply was dropped. Try again in 30 seconds.`;
    case "generic":
      return `Reply to \`${projectId}\` failed: ${raw.slice(0, 200)}`;
  }
}

export class InboundDispatcher {
  private readonly commandRouter: CommandRouter;
  private readonly architectManager: Pick<ArchitectManager, "relayOperatorInput">;
  private readonly identityMap: IdentityMap;
  private readonly senders: Record<string, DiscordSender>;
  private readonly config: DiscordConfig;
  private readonly messageContext: MessageContext;
  // CW-4.5 — optional deps; rule 1 short-circuits when missing.
  private readonly projectStore?: Pick<ProjectStore, "getAllProjects" | "getProject">;
  private readonly channelBuffer?: Pick<ChannelContextBuffer, "recent">;
  private readonly getBotUsername?: () => string | null;

  constructor(deps: InboundDispatcherDeps) {
    this.commandRouter = deps.commandRouter;
    this.architectManager = deps.architectManager;
    this.identityMap = deps.identityMap;
    this.senders = deps.senders;
    this.config = deps.config;
    this.messageContext = deps.messageContext;
    this.projectStore = deps.projectStore;
    this.channelBuffer = deps.channelBuffer;
    this.getBotUsername = deps.getBotUsername;
  }

  /**
   * Top-level dispatch. Caller is `gateway.on((msg) => void dispatcher.dispatch(msg))`.
   * Errors are caught here so a single bad message never tears down the gateway.
   *
   * Precedence (CW-4.5):
   *   1. mention rule — `@<agent>` / `@<bot>` / `<@<id>>` (NEW)
   *   2. reply-UI rules 2a/2b/3/4 (existing)
   *   3. rule 5 — !command / natural language (existing)
   */
  async dispatch(msg: InboundMessage): Promise<void> {
    try {
      // CW-4.5 rule 1 — mention detection. Runs BEFORE reply-UI so an operator
      // who clicks reply AND types a mention has the mention path win.
      const mentionHandled = await this.tryMentionRoute(msg);
      if (mentionHandled) return;

      // Reply-routing precedence over NL: rules 2a/2b/3/4 short-circuit when the
      // message is a Discord reply, otherwise fall through to rule 5.
      if (msg.repliedToMessageId) {
        const handled = await this.tryReplyRoute(msg);
        if (handled) return;
      }
      // Rule 5 — `!cmd` or natural language via CommandRouter.
      await this.routeCommand(msg);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(`[InboundDispatcher] dispatch failed: ${message}`);
    }
  }

  /**
   * CW-4.5 §6 rule 1 — detect `@<agent>` or `@<bot>` mentions and resolve
   * to a project via affinity-hint OR single-active. Returns `true` when the
   * mention path consumed the message (relay fired or instructive reply
   * sent). Returns `false` to let dispatch fall through to existing rules.
   *
   * Short-circuits to false when any of the optional CW-4.5 deps
   * (`projectStore`, `channelBuffer`, `getBotUsername`) is missing — keeps
   * the dispatcher backward-compatible with existing test setups.
   */
  private async tryMentionRoute(msg: InboundMessage): Promise<boolean> {
    // If any CW-4.5 dep is missing, this rule is disabled.
    if (!this.projectStore || !this.channelBuffer || !this.getBotUsername) {
      return false;
    }

    const selfBotUsername = this.getBotUsername();
    // selfBotId is not directly visible to the dispatcher (gateway-internal);
    // leave it null — bot-self detection via `<@<id>>` form is silently
    // skipped when the dispatcher has no ID seam. Plain `@<botUsername>` form
    // still works via `selfBotUsername`.
    const extracted = extractMentions(msg.content, this.identityMap, selfBotUsername, null);

    if (extracted.mentions.length === 0 && !extracted.botMentioned) return false;

    if (extracted.mentions.length > 0) {
      // Agent mention — try to resolve a project for this channel.
      const resolved = this.resolveProjectForChannel(msg.channelId);
      if (resolved.projectId === null) {
        // 0/multi-active without coherent hint — instructive reply.
        this.sendToChannel(
          msg.channelId,
          "Multiple/no active projects — reply to a specific agent's message or use `!project` commands.",
        );
        return true;
      }
      // Relay the cleaned content (mentions stripped) into the architect.
      try {
        await this.architectManager.relayOperatorInput(resolved.projectId, extracted.cleanedContent);
      } catch (err) {
        const error = err instanceof Error ? err : new Error(String(err));
        const kind = classifyRelayError(error);
        this.sendToChannel(
          msg.channelId,
          relayFailureMessage(kind, resolved.projectId, error.message),
        );
      }
      return true;
    }

    // botMentioned only (no agent) — fall through to existing rules.
    // The `directAddress` flag is plumbed through ClassifyContext for future
    // CW-4.6 consumption; v1 records but doesn't change behavior.
    return false;
  }

  /**
   * CW-4.5 §5.4 — resolve a project for a channel using affinity hints from
   * the buffer first, then single-active fallback.
   */
  private resolveProjectForChannel(channelId: string): ResolveProjectResult {
    if (!this.projectStore) return { projectId: null, reason: "no_project_store" };
    if (!this.channelBuffer) return { projectId: null, reason: "no_project_store" };

    // Step A — affinity hint from recent buffer.
    const recent = this.channelBuffer.recent(channelId, 10);
    const hints = new Set<string>();
    for (const m of recent) {
      if (m.projectIdHint) hints.add(m.projectIdHint);
    }
    if (hints.size === 1) {
      const [pid] = hints;
      const proj = this.projectStore.getProject(pid);
      if (proj && (proj.state === "decomposing" || proj.state === "executing")) {
        return { projectId: pid, reason: "affinity_hint" };
      }
      // Hint references a stale project — fall through to single-active.
    }

    // Step B — single-active fallback.
    const active = this.projectStore.getAllProjects()
      .filter((p) => p.state === "decomposing" || p.state === "executing");
    if (active.length === 1) return { projectId: active[0].id, reason: "single_active" };
    if (active.length === 0) return { projectId: null, reason: "no_active" };
    return { projectId: null, reason: "multi_active_no_hint" };
  }

  /**
   * Attempts reply-routing rules 2a/2b/3/4. Returns `true` if the message was
   * handled (no fall-through to rule 5), `false` to let the caller continue
   * into rule 5 (NL/!command).
   */
  private async tryReplyRoute(msg: InboundMessage): Promise<boolean> {
    const repliedId = msg.repliedToMessageId;
    if (!repliedId) return false;

    // Rule 4 prelude: only attempt agent routing when the replied-to author is a known agent.
    const username = msg.repliedToAuthorUsername ?? "";
    const agentResolution = this.identityMap.lookup(username);
    if (!agentResolution) {
      // Rule 4 — fall through to rule 5.
      return false;
    }

    const projectId = this.messageContext.resolveProjectIdForMessage(repliedId);
    if (!projectId) {
      // Rule 2b — known agent, no record of the message.
      this.sendToChannel(
        msg.channelId,
        `I recognized this as a reply to **${username}**, but I have no record of that message — re-issue your command directly via \`!project\` or by replying to a fresh agent message.`,
      );
      return true;
    }

    // Rule 2a — relay into the Architect session. Errors fall through to Rule 3.
    try {
      await this.architectManager.relayOperatorInput(projectId, msg.content);
    } catch (err) {
      const error = err instanceof Error ? err : new Error(String(err));
      const kind = classifyRelayError(error);
      // Rule 3 — operator-visible reply describing the failure.
      this.sendToChannel(
        msg.channelId,
        relayFailureMessage(kind, projectId, error.message),
      );
    }
    return true;
  }

  /**
   * Rule 5 — `!cmd` → handleCommand; otherwise → handleNaturalLanguage. Reply
   * (if any) goes to the channel sender via `sendToChannel`.
   */
  private async routeCommand(msg: InboundMessage): Promise<void> {
    const text = msg.content;
    const userId = msg.authorId;
    let reply: string;
    if (text.startsWith("!")) {
      const stripped = text.slice(1);
      const space = stripped.indexOf(" ");
      const command = space === -1 ? stripped : stripped.slice(0, space);
      const args = space === -1 ? "" : stripped.slice(space + 1);
      reply = await this.commandRouter.handleCommand(command, args, msg.channelId);
    } else {
      reply = await this.commandRouter.handleNaturalLanguage(text, msg.channelId, userId);
    }
    if (reply && reply.length > 0) {
      this.sendToChannel(msg.channelId, reply);
    }
  }

  /** Channel send with optional-chain safety. Missing channel → warn, no crash. */
  private sendToChannel(channelId: string, content: string): void {
    const sender = this.senders[channelId];
    if (!sender) {
      console.warn(`[InboundDispatcher] no sender configured for channel ${channelId}`);
      return;
    }
    void sender.sendToChannel(channelId, content).catch((err) => {
      const m = err instanceof Error ? err.message : String(err);
      console.error(`[InboundDispatcher] sender failed: ${m}`);
    });
  }

  /**
   * Test/diagnostic accessor: exposes the configured Discord channels. Kept on
   * the public surface so the constructor `config` dep is observably wired
   * even though dispatch logic resolves channels from the inbound message.
   */
  get channels(): { dev: string; ops: string; escalation: string } {
    return {
      dev: this.config.dev_channel,
      ops: this.config.ops_channel,
      escalation: this.config.escalation_channel,
    };
  }
}
