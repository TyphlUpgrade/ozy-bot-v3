/**
 * Discord notifier — translates OrchestratorEvent into channel-routed Discord
 * messages with per-agent identity. Uses an injected DiscordSender so tests can
 * swap in fakes; in production, WebhookSender is the concrete implementation.
 *
 * Channel routing and identity selection are data-driven (NOTIFIER_MAP) so new
 * event types add one row instead of adding switch arms in multiple places.
 * The NotifierEntry<T> generic narrows `format`'s event parameter to the exact
 * variant keyed by T — a stale copy-paste guard becomes a compile error.
 *
 * Sender failures are swallowed — Discord hiccups never crash the pipeline.
 *
 * **Untrusted-input defense (Security Wave 2):**
 * Any event-derived string that will be interpolated into a Discord message
 * MUST pass through `sanitize()` before it reaches the body. `sanitize()`
 * neutralizes `@everyone`/`@here`, escapes backticks, and length-caps. The
 * top-level `prompt` on `task_picked_up` (which can echo verbatim from Discord
 * chat once Wave 3 ships) also passes through `redactSecrets()` to scrub
 * common key patterns BEFORE truncation.
 */

import type { OrchestratorEvent } from "../orchestrator.js";
import { DISCORD_AGENT_DEFAULTS, DISCORD_REPLY_THREADING_DEFAULTS, type DiscordConfig, type DiscordAgentIdentity } from "../lib/config.js";
import { sanitize, redactSecrets, truncateRationale } from "../lib/text.js";
import type { AgentIdentity, DiscordSender } from "./types.js";
import type { StateManager } from "../lib/state.js";
import type { MessageContext, AgentRole } from "./message-context.js";
import { renderEpistle, defaultCtx, type EpistleContext } from "./epistle-templates.js";
import { resolveIdentity as resolveIdentityRole } from "./identity.js";

// Re-export for backward-compat — Wave 3 moved these to src/lib/text.ts.
export { sanitize, redactSecrets } from "../lib/text.js";

/**
 * Discord hard cap is 2000 chars. Truncate body to `max` chars (default 1900)
 * to leave headroom for Discord's own formatting overhead.
 */
function truncateBody(body: string, max = 1900): string {
  if (body.length <= max) return body;
  return body.slice(0, max - 1) + "…";
}

type ChannelKey = "dev_channel" | "ops_channel" | "escalation_channel";
type IdentityKey = "orchestrator" | "architect" | "reviewer" | "executor";
type EventType = OrchestratorEvent["type"];
type EventByType<K extends EventType> = Extract<OrchestratorEvent, { type: K }>;

interface NotifierEntry<K extends EventType> {
  channel: ChannelKey;
  identity: IdentityKey;
  /** Build the Discord message body from the event. Null → skip emission.
   *  ctx is optional — epistle-wrapped entries use it for deterministic timestamps;
   *  all other entries ignore it. */
  format: (event: EventByType<K>, ctx?: EpistleContext) => string | null;
}

function shortTaskId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-3)}` : id;
}

function shortProjectId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

// --- Event → route mapping ---

/**
 * Event → (channel, identity, formatter) table.
 * Events not listed are ignored (poll_tick, shutdown, checkpoint_detected,
 * completion_compliance — internal / informational). Adding a new event type
 * is a one-line change here plus the OrchestratorEvent union.
 *
 * The mapped type `{ [K in EventType]?: NotifierEntry<K> }` enforces that each
 * key's `format` callback receives the exact union variant, eliminating the
 * stale `if (e.type !== "...") return null` guards.
 */
type NotifierMap = { [K in EventType]?: NotifierEntry<K> };

const NOTIFIER_MAP: NotifierMap = {
  // --- Phase 2A / standalone task lifecycle ---
  task_picked_up: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e) => {
      const redacted = redactSecrets(e.prompt);
      const sanitized = sanitize(redacted, 80);
      return `Task \`${shortTaskId(e.taskId)}\` picked up: ${sanitized}`;
    },
  },
  session_complete: {
    channel: "dev_channel",
    identity: "executor",
    format: (e, ctx?) => renderEpistle(e, resolveIdentityRole(e), ctx ?? defaultCtx()),
  },
  merge_result: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e, ctx?) => renderEpistle(e, resolveIdentityRole(e), ctx ?? defaultCtx()),
  },
  task_done: {
    channel: "dev_channel",
    identity: "executor",
    format: (e, ctx?) => renderEpistle(e, resolveIdentityRole(e), ctx ?? defaultCtx()),
  },
  task_shelved: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e) => `Task \`${shortTaskId(e.taskId)}\` shelved: ${sanitize(e.reason)}`,
  },
  task_failed: {
    channel: "ops_channel",
    identity: "orchestrator",
    format: (e, ctx?) => renderEpistle(e, resolveIdentityRole(e), ctx ?? defaultCtx()),
  },
  escalation_needed: {
    channel: "escalation_channel",
    identity: "orchestrator",
    format: (e, ctx?) => renderEpistle(e, resolveIdentityRole(e), ctx ?? defaultCtx()),
  },
  budget_exhausted: {
    channel: "ops_channel",
    identity: "orchestrator",
    format: (e) =>
      `Budget exhausted for \`${shortTaskId(e.taskId)}\`: $${e.totalCostUsd.toFixed(2)}`,
  },
  retry_scheduled: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e) => `Retry ${e.attempt}/${e.maxRetries} for \`${shortTaskId(e.taskId)}\``,
  },
  response_level: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e) => {
      if (e.level < 2) return null; // level 0-1 are default; only escalated levels notify
      const reasons = e.reasons.map((r) => sanitize(r, 200)).join("; ");
      return `Response level **${e.level}** (${sanitize(e.name, 40)}) for \`${shortTaskId(e.taskId)}\`: ${reasons}`;
    },
  },

  // --- Wave 2 three-tier project lifecycle ---
  project_declared: {
    channel: "dev_channel",
    identity: "architect",
    format: (e) => `Project \`${shortProjectId(e.projectId)}\` declared: **${sanitize(e.name, 80)}**`,
  },
  project_decomposed: {
    channel: "dev_channel",
    identity: "architect",
    format: (e) => `Project \`${shortProjectId(e.projectId)}\` decomposed into ${e.phaseCount} phase(s)`,
  },
  project_completed: {
    channel: "dev_channel",
    identity: "architect",
    format: (e) =>
      `Project \`${shortProjectId(e.projectId)}\` completed (${e.phaseCount} phases, $${e.totalCostUsd.toFixed(2)})`,
  },
  project_failed: {
    channel: "ops_channel",
    identity: "architect",
    format: (e, ctx?) => renderEpistle(e, resolveIdentityRole(e), ctx ?? defaultCtx()),
  },
  project_aborted: {
    // Architect owns project lifecycle narrative, including operator-abort (Architect review finding LOW-2).
    channel: "ops_channel",
    identity: "architect",
    format: (e) => `Project \`${shortProjectId(e.projectId)}\` aborted by operator ${sanitize(e.operatorId, 64)}`,
  },
  architect_spawned: {
    channel: "dev_channel",
    identity: "architect",
    format: (e) => `Architect spawned for project \`${shortProjectId(e.projectId)}\` (session ${e.sessionId.slice(0, 8)})`,
  },
  architect_respawned: {
    channel: "ops_channel",
    identity: "architect",
    format: (e) =>
      `Architect **respawned** for project \`${shortProjectId(e.projectId)}\` (reason: ${e.reason}, session ${e.sessionId.slice(0, 8)})`,
  },
  architect_arbitration_fired: {
    channel: "ops_channel",
    identity: "architect",
    format: (e) =>
      `Architect arbitration fired on \`${shortTaskId(e.taskId)}\` in project \`${shortProjectId(e.projectId)}\` (cause: ${e.cause})`,
  },
  arbitration_verdict: {
    channel: "ops_channel",
    identity: "architect",
    format: (e) =>
      truncateBody(
        `Arbitration verdict for \`${shortTaskId(e.taskId)}\` in \`${shortProjectId(e.projectId)}\`: **${e.verdict}** — ${sanitize(truncateRationale(e.rationale, 1024))}`,      ),
  },
  review_arbitration_entered: {
    channel: "escalation_channel",
    identity: "reviewer",
    format: (e) =>
      `**review_arbitration** entered for \`${shortTaskId(e.taskId)}\` in \`${shortProjectId(e.projectId)}\` (rejection #${e.reviewerRejectionCount})`,
  },
  review_mandatory: {
    channel: "dev_channel",
    identity: "reviewer",
    format: (e, ctx?) => renderEpistle(e, resolveIdentityRole(e), ctx ?? defaultCtx()),
  },
  budget_ceiling_reached: {
    channel: "escalation_channel",
    identity: "orchestrator",
    format: (e) =>
      `**Budget ceiling** reached for project \`${shortProjectId(e.projectId)}\`: $${e.currentCostUsd.toFixed(2)} / $${e.ceilingUsd.toFixed(2)}`,
  },
  compaction_fired: {
    channel: "dev_channel",
    identity: "architect",
    format: (e) => `Compaction fired for project \`${shortProjectId(e.projectId)}\` (generation ${e.generation})`,
  },

  // --- Wave watchdog (commit `2ae53c9`) ---
  // session_stalled is ops-visible: orchestrator detected a stalled session and
  // aborted it. Wave E-β chain rules also reply this under the matching tier
  // role-head when one is registered (see CHAIN_RULES + handleEvent below).
  session_stalled: {
    channel: "ops_channel",
    identity: "orchestrator",
    format: (e) =>
      `Session stalled (${e.tier}) on \`${shortTaskId(e.taskId)}\` after ${Math.round(e.stalledForMs / 1000)}s — ${e.aborted ? "aborted" : "still running"}`,
  },
};

// --- Wave E-β chain rules ---
//
// Per-event reply / register rules. Encodes the chain-decision table from
// `.omc/plans/2026-04-27-discord-wave-e-beta.md` § B5. Lookups always use the
// channel the message is being sent to; cross-channel chains are NOT supported
// (Discord reply-API requires head + reply in same channel).
//
// IMPORTANT: keys MUST be verbatim strings from the OrchestratorEvent union
// (`src/orchestrator.ts:107-137`). Per harness-ts I-4: do not paraphrase or
// invent names. To add a new event chain, add one row here.
//
// `session_stalled` is NOT in this table — its reply target depends on
// `event.tier`, which is field-conditional. Handled inline in handleEvent.
interface ChainRule {
  /** Role-head to look up for reply target. null = standalone (no reply). */
  replyToRole: AgentRole | null;
  /** Role-head to register with the returned messageId. null = no registration. */
  registerRole: AgentRole | null;
}

const CHAIN_RULES: Partial<Record<EventType, ChainRule>> = {
  // Architect lifecycle — chain heads.
  // (`project_decomposed` is the verbatim source-of-truth name; the planning
  // doc references "architect_decomposed" but no such event exists.)
  project_decomposed: { replyToRole: null, registerRole: "architect" },
  architect_arbitration_fired: { replyToRole: null, registerRole: "architect" },

  // Architect mid-chain — reply under prior architect head, re-register.
  arbitration_verdict: { replyToRole: "architect", registerRole: "architect" },

  // Executor chain under architect head.
  session_complete: { replyToRole: "architect", registerRole: "executor" },
  merge_result: { replyToRole: "executor", registerRole: "executor" },
  task_done: { replyToRole: "executor", registerRole: "executor" },

  // Reviewer chain under executor head.
  review_mandatory: { replyToRole: "executor", registerRole: "reviewer" },
  review_arbitration_entered: { replyToRole: "executor", registerRole: "reviewer" },
};

// --- Wave E-β restart-warn ---
// Module-private flag so the orchestrator emits exactly one console.warn on
// the first chain-head miss after process start. Reset only on process
// restart, which is also the trigger condition. Tests use `vi.resetModules()`
// to clear between cases.
let restartWarned = false;
function warnRestartOnce(): void {
  if (restartWarned) return;
  restartWarned = true;
  console.warn("[notifier] reply-chain head missing — orchestrator likely restarted; chains will rebuild from next event");
}

// --- Runtime ---

export interface DiscordNotifierOptions {
  /**
   * Override agent identities for testing. Precedence (highest-wins):
   *   `config.agents` > `options.agents` > DISCORD_AGENT_DEFAULTS
   *
   * Config is the production source of truth (parsed from project.toml and
   * pre-merged with defaults in `parseDiscord`). `options.agents` is a test-only
   * seam; in production leave undefined.
   */
  agents?: Record<string, DiscordAgentIdentity>;
  /**
   * CW-3 — optional MessageContext store. When provided, the notifier records
   * outbound message ids for project-keyed events so a Discord reply to that
   * message can be resolved back to the originating projectId by
   * `InboundDispatcher`. Without it, the notifier falls back to fire-and-forget
   * `sendToChannel` (no recording).
   */
  messageContext?: MessageContext;
  /**
   * CW-3 — optional state manager. Used to resolve a projectId for task-keyed
   * events (`task_*`, `merge_result`, etc.) via `state.getTask(taskId).projectId`.
   * Without it, task-keyed events skip recording and use the legacy send path.
   */
  stateManager?: StateManager;
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * CW-1 — sender input shape: either a single sender (legacy / single-channel
 * tests) or a per-channel map keyed by Discord channel id. Notifier resolves
 * the channel id from `config.<dev|ops|escalation>_channel`, looks it up in
 * the map, and falls back to a default sender when the map has no entry.
 */
export type SenderInput = DiscordSender | Record<string, DiscordSender>;

function isSenderMap(input: SenderInput): input is Record<string, DiscordSender> {
  // Heuristic: a DiscordSender has `sendToChannel` as a function on itself.
  // A map is a plain object whose values have that method.
  return (
    typeof input === "object" &&
    input !== null &&
    typeof (input as Partial<DiscordSender>).sendToChannel !== "function"
  );
}

export class DiscordNotifier {
  private readonly senders: Record<string, DiscordSender> | null;
  private readonly defaultSender: DiscordSender | null;
  private readonly config: DiscordConfig;
  private readonly agents: Record<string, DiscordAgentIdentity>;
  // CW-3 — optional reply-routing wiring.
  private readonly messageContext: MessageContext | undefined;
  private readonly stateManager: StateManager | undefined;

  constructor(senders: SenderInput, config: DiscordConfig, options: DiscordNotifierOptions = {}) {
    this.config = config;
    if (isSenderMap(senders)) {
      this.senders = senders;
      // Pick any entry as the fallback for channels not in the map.
      const first = Object.values(senders)[0];
      this.defaultSender = first ?? null;
    } else {
      this.senders = null;
      this.defaultSender = senders;
    }
    // Precedence: config > options > defaults. Config is trusted production
    // value; options is a test seam that should not override prod config.
    this.agents = { ...DISCORD_AGENT_DEFAULTS, ...options.agents, ...config.agents };
    this.messageContext = options.messageContext;
    this.stateManager = options.stateManager;
  }

  /** Register with orchestrator: `orch.on((ev) => notifier.handleEvent(ev));` */
  handleEvent(event: OrchestratorEvent): void {
    this.dispatch(event);
  }

  /**
   * Dispatch with generic narrowing: a single generic function parameterized
   * over the event type gives `entry.format(event)` a statically-narrowed
   * argument, eliminating runtime `e.type !== "..."` guards.
   */
  private dispatch<K extends EventType>(event: EventByType<K>): void {
    const entry = (NOTIFIER_MAP as NotifierMap)[event.type as K] as NotifierEntry<K> | undefined;
    if (!entry) return; // silently ignored (poll_tick, shutdown, checkpoint_detected, completion_compliance)

    const body = entry.format(event);
    if (body === null) return;

    const channel = this.resolveChannel(entry.channel);
    const identity = this.resolveIdentity(entry.identity);
    const sender = this.resolveSender(channel);
    if (!sender) return;

    // CW-3 — record path: when MessageContext is wired and we can resolve a
    // projectId for this event, capture the Discord-assigned message id so
    // operator replies route back. Without messageContext, use the legacy
    // fire-and-forget path (no recording, no API change).
    //
    // Wave E-β — chain decisions (reply target lookup + role-head register)
    // also live on this path. They consult `CHAIN_RULES[event.type]` and the
    // role-head map keyed `(projectId, role, channel)` to synthesize Discord
    // `message_reference` reply chains. Disabled when
    // `config.discord.reply_threading.enabled === false`.
    if (this.messageContext) {
      const projectId = this.resolveProjectId(event);
      if (projectId !== null) {
        const ctx = this.messageContext;
        const replyToMessageId = this.computeReplyTarget(event, projectId, channel);
        const chainRule = CHAIN_RULES[event.type];
        const registerRole = this.threadingEnabled() ? (chainRule?.registerRole ?? null) : null;
        void sender
          .sendToChannelAndReturnId(channel, body, identity, replyToMessageId ?? undefined)
          .then(({ messageId }) => {
            if (messageId !== null) {
              ctx.recordAgentMessage(messageId, projectId);
              if (registerRole !== null) {
                ctx.recordRoleMessage(projectId, registerRole, messageId, channel);
              }
            }
          })
          .catch((err) => {
            console.error(`[DiscordNotifier] sender failed for ${event.type} -> ${entry.channel}: ${errMessage(err)}`);
          });
        return;
      }
      // projectId null → fall through to plain sendToChannel (no recording, no chain).
    }

    // Swallow sender failures — Discord hiccups never crash the pipeline.
    // Log only .message to avoid echoing request bodies back into logs.
    void sender.sendToChannel(channel, body, identity).catch((err) => {
      console.error(`[DiscordNotifier] sender failed for ${event.type} -> ${entry.channel}: ${errMessage(err)}`);
    });
  }

  /**
   * Wave E-β — true when reply-threading is enabled (default). Honors the
   * `[discord.reply_threading].enabled` config flag; absent block applies the
   * `DISCORD_REPLY_THREADING_DEFAULTS.enabled` (true) fallback.
   */
  private threadingEnabled(): boolean {
    return this.config.reply_threading?.enabled ?? DISCORD_REPLY_THREADING_DEFAULTS.enabled;
  }

  /**
   * Wave E-β — compute reply-target messageId for an outbound event. Returns
   * null when threading disabled, no chain rule applies, no projectId, or the
   * lookup misses (stale or absent). On the first miss for an event with an
   * actual reply expectation, fires `warnRestartOnce()` (likely orchestrator
   * restart wiped the in-memory map).
   */
  private computeReplyTarget(
    event: OrchestratorEvent,
    projectId: string,
    channel: string,
  ): string | null {
    if (!this.threadingEnabled()) return null;
    if (!this.messageContext) return null;

    // session_stalled — tier-aware lookup. The event carries `tier` directly;
    // reply target is the head matching that tier in the same channel. Per
    // plan B5: session_stalled does NOT register a new head (the stalled
    // session is being aborted; no continuity to thread).
    if (event.type === "session_stalled") {
      const tierRole = event.tier as AgentRole;
      const head = this.messageContext.lookupRoleHead(projectId, tierRole, channel);
      if (head === null) warnRestartOnce();
      return head;
    }

    const rule = CHAIN_RULES[event.type];
    if (!rule || rule.replyToRole === null) return null;
    const head = this.messageContext.lookupRoleHead(projectId, rule.replyToRole, channel);
    if (head === null) warnRestartOnce();
    return head;
  }

  /**
   * CW-3 — resolve a projectId for any OrchestratorEvent variant. Project-keyed
   * events return `event.projectId` directly; task-keyed events go through the
   * StateManager (`getTask(taskId)?.projectId`). Returns `null` when:
   *   - the event is neither project- nor task-keyed (poll_tick / shutdown);
   *   - a task-keyed event has no stateManager configured;
   *   - the task is not in state, or has no projectId (standalone task).
   */
  private resolveProjectId(event: OrchestratorEvent): string | null {
    if ("projectId" in event && typeof event.projectId === "string") {
      return event.projectId;
    }
    if ("taskId" in event && typeof event.taskId === "string") {
      const task = this.stateManager?.getTask(event.taskId);
      return task?.projectId ?? null;
    }
    return null;
  }

  private resolveChannel(key: ChannelKey): string {
    return this.config[key];
  }

  private resolveIdentity(key: IdentityKey): AgentIdentity {
    const cfg = this.agents[key] ?? DISCORD_AGENT_DEFAULTS[key];
    return { username: cfg.name, avatarURL: cfg.avatar_url };
  }

  private resolveSender(channelId: string): DiscordSender | null {
    if (this.senders) {
      return this.senders[channelId] ?? this.defaultSender;
    }
    return this.defaultSender;
  }
}
