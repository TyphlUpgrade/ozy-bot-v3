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
import type { CommandRouter } from "./commands.js";
import type { IdentityMap } from "./identity-map.js";
import type { MessageContext } from "./message-context.js";
import type { DiscordSender, InboundMessage } from "./types.js";

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

  constructor(deps: InboundDispatcherDeps) {
    this.commandRouter = deps.commandRouter;
    this.architectManager = deps.architectManager;
    this.identityMap = deps.identityMap;
    this.senders = deps.senders;
    this.config = deps.config;
    this.messageContext = deps.messageContext;
  }

  /**
   * Top-level dispatch. Caller is `gateway.on((msg) => void dispatcher.dispatch(msg))`.
   * Errors are caught here so a single bad message never tears down the gateway.
   */
  async dispatch(msg: InboundMessage): Promise<void> {
    try {
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
