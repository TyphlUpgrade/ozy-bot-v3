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
import { DISCORD_AGENT_DEFAULTS, type DiscordConfig, type DiscordAgentIdentity } from "../lib/config.js";
import { sanitize, redactSecrets } from "../lib/text.js";
import type { AgentIdentity, DiscordSender } from "./types.js";
import type { StateManager } from "../lib/state.js";

// Re-export for backward-compat — Wave 3 moved these to src/lib/text.ts.
export { sanitize, redactSecrets } from "../lib/text.js";

type ChannelKey = "dev_channel" | "ops_channel" | "escalation_channel";
type IdentityKey = "orchestrator" | "architect" | "reviewer";
type EventType = OrchestratorEvent["type"];
type EventByType<K extends EventType> = Extract<OrchestratorEvent, { type: K }>;

interface NotifierEntry<K extends EventType> {
  channel: ChannelKey;
  identity: IdentityKey;
  /** Build the Discord message body from the event. Null → skip emission. */
  format: (event: EventByType<K>) => string | null;
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
    identity: "orchestrator",
    format: (e) =>
      `Session complete for \`${shortTaskId(e.taskId)}\` (${e.success ? "success" : "failure"})`,
  },
  merge_result: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e) =>
      `Merge result for \`${shortTaskId(e.taskId)}\`: **${sanitize(e.result.status, 40)}**`,
  },
  task_done: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e) => `Task \`${shortTaskId(e.taskId)}\` complete`,
  },
  task_shelved: {
    channel: "dev_channel",
    identity: "orchestrator",
    format: (e) => `Task \`${shortTaskId(e.taskId)}\` shelved: ${sanitize(e.reason)}`,
  },
  task_failed: {
    channel: "ops_channel",
    identity: "orchestrator",
    format: (e) => `Task \`${shortTaskId(e.taskId)}\` **FAILED**: ${sanitize(e.reason)}`,
  },
  escalation_needed: {
    channel: "escalation_channel",
    identity: "orchestrator",
    format: (e) =>
      `**ESCALATION** \`${shortTaskId(e.taskId)}\`: ${sanitize(e.escalation.question ?? e.escalation.type)}`,
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
    format: (e) => `Project \`${shortProjectId(e.projectId)}\` **FAILED**: ${sanitize(e.reason)}`,
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
      `Arbitration verdict for \`${shortTaskId(e.taskId)}\` in \`${shortProjectId(e.projectId)}\`: **${e.verdict}** — ${sanitize(e.rationale)}`,
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
    format: (e) => `Mandatory review firing for \`${shortTaskId(e.taskId)}\` in project \`${shortProjectId(e.projectId)}\``,
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
};

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
   * CW-1 — optional MessageContext store. CW-3 will record outbound message
   * ids here so reply chains can resolve back to taskId / projectId. Wired now
   * but unused (no-op) until CW-3 introduces the MessageContext class.
   */
  messageContext?: MessageContextLike;
  /**
   * CW-1 — optional state manager for projectId resolution on task-keyed
   * events. Wired now, used by CW-3.
   */
  stateManager?: StateManager;
}

/**
 * Forward-compatible shape for CW-3's MessageContext. Empty for CW-1; CW-3
 * extends with `record(messageId, taskId | projectId, ...)` etc. Defined
 * inline so CW-1 doesn't depend on CW-3 source files.
 */
export interface MessageContextLike {
  // CW-3 will add methods here; CW-1 only wires the dependency.
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
  // CW-1 wiring — unused until CW-3 lands MessageContext.
  private readonly messageContext: MessageContextLike | undefined;
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

    // Swallow sender failures — Discord hiccups never crash the pipeline.
    // Log only .message to avoid echoing request bodies back into logs.
    void sender.sendToChannel(channel, body, identity).catch((err) => {
      console.error(`[DiscordNotifier] sender failed for ${event.type} -> ${entry.channel}: ${errMessage(err)}`);
    });
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
