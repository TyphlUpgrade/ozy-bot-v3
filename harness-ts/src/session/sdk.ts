/**
 * SDK integration layer — thin wrapper around query() for session lifecycle.
 * Provides spawn, abort, resume, and stream consumption.
 */

import type {
  Query,
  Options,
  SDKMessage,
  SDKResultMessage,
  SDKResultSuccess,
  SDKResultError,
} from "@anthropic-ai/claude-agent-sdk";

// --- Types ---

export interface SessionConfig {
  prompt: string;
  cwd: string;
  systemPrompt?: string;
  model?: string;
  maxBudgetUsd?: number;
  maxTurns?: number;
  allowedTools?: string[];
  disallowedTools?: string[];
  permissionMode?: Options["permissionMode"];
  settingSources?: Options["settingSources"];
  sessionId?: string;
  resume?: string;
  abortController?: AbortController;
  persistSession?: boolean;
  /** Map of plugin-id → enabled (Wave 1 Item 1). Loads via Options.settings.enabledPlugins. */
  enabledPlugins?: Record<string, boolean>;
  /** Programmatic hooks. Always set (default {}) so filesystem-discovered hooks like persistent-mode do not load (Wave 1 Item 2). */
  hooks?: Partial<Record<string, unknown[]>>;
}

export interface SessionResult {
  sessionId: string;
  success: boolean;
  result?: string;
  errors: string[];
  totalCostUsd: number;
  numTurns: number;
  usage: { input_tokens: number; output_tokens: number };
  terminalReason?: string;
  /**
   * WA-5: resolved model name captured from the SDK system_init message.
   * Used by the orchestrator's formatCommitMessage to produce a
   * `Model: <name>` trailer on every phase commit for forensic provenance.
   */
  modelName?: string;
}

export type MessageHandler = (msg: SDKMessage) => void;

// --- Classify SDK messages by type ---

export type MessageCategory =
  | "result_success"
  | "result_error"
  | "assistant"
  | "system_init"
  | "user"
  | "other";

export function classifyMessage(msg: SDKMessage): MessageCategory {
  if (msg.type === "result") {
    return msg.subtype === "success" ? "result_success" : "result_error";
  }
  if (msg.type === "assistant") return "assistant";
  if (msg.type === "system" && msg.subtype === "init") return "system_init";
  if (msg.type === "user") return "user";
  return "other";
}

// --- Parse result message into SessionResult ---

export function parseResult(msg: SDKResultMessage): SessionResult {
  if (msg.subtype === "success") {
    const s = msg as SDKResultSuccess;
    return {
      sessionId: s.session_id,
      success: true,
      result: s.result,
      errors: [],
      totalCostUsd: s.total_cost_usd,
      numTurns: s.num_turns,
      usage: {
        input_tokens: s.usage.input_tokens,
        output_tokens: s.usage.output_tokens,
      },
      terminalReason: s.terminal_reason,
    };
  }
  const e = msg as SDKResultError;
  return {
    sessionId: e.session_id,
    success: false,
    errors: e.errors,
    totalCostUsd: e.total_cost_usd,
    numTurns: e.num_turns,
    usage: {
      input_tokens: e.usage.input_tokens,
      output_tokens: e.usage.output_tokens,
    },
    terminalReason: e.terminal_reason,
  };
}

// --- SDK Wrapper ---

/** Injectable query function for testing */
export type QueryFn = (params: { prompt: string; options?: Options }) => Query;

export class SDKClient {
  private readonly queryFn: QueryFn;
  private activeControllers: Map<string, AbortController> = new Map();

  constructor(queryFn: QueryFn) {
    this.queryFn = queryFn;
  }

  /** Spawn a new agent session */
  spawnSession(config: SessionConfig): { query: Query; abortController: AbortController } {
    const ac = config.abortController ?? new AbortController();

    const options: Options = {
      cwd: config.cwd,
      abortController: ac,
      permissionMode: config.permissionMode ?? "bypassPermissions",
      allowDangerouslySkipPermissions: config.permissionMode === "bypassPermissions" || config.permissionMode === undefined,
      settingSources: config.settingSources ?? ["project"],
      persistSession: config.persistSession ?? true,
    };

    if (config.model) options.model = config.model;
    if (config.maxBudgetUsd) options.maxBudgetUsd = config.maxBudgetUsd;
    if (config.maxTurns) options.maxTurns = config.maxTurns;
    if (config.allowedTools) options.allowedTools = config.allowedTools;
    if (config.disallowedTools) options.disallowedTools = config.disallowedTools;
    if (config.sessionId) options.sessionId = config.sessionId;
    if (config.resume) options.resume = config.resume;
    if (config.systemPrompt) {
      options.systemPrompt = {
        type: "preset",
        preset: "claude_code",
        append: config.systemPrompt,
      };
    }

    // Wave 1 Item 1: plugin loading via Options.settings.enabledPlugins
    // TODO: remove cast when SDK types expose `settings.enabledPlugins` directly.
    // Validated empirically in architect-spike v3 (sonnet+OMC) — see plan Section M.3.
    if (config.enabledPlugins && Object.keys(config.enabledPlugins).length > 0) {
      (options as Options & { settings?: unknown }).settings = {
        enabledPlugins: config.enabledPlugins,
      };
    }

    // Wave 1 Item 2: always set hooks to block filesystem-discovered hooks (e.g. persistent-mode.cjs)
    options.hooks = (config.hooks ?? {}) as Options["hooks"];

    const q = this.queryFn({ prompt: config.prompt, options });
    return { query: q, abortController: ac };
  }

  /**
   * Consume a query stream, collecting messages and returning the final result.
   * Optionally calls onMessage for each SDKMessage.
   */
  async consumeStream(
    q: Query,
    onMessage?: MessageHandler,
  ): Promise<SessionResult> {
    let result: SessionResult | undefined;
    let sessionId = "";
    // WA-5: capture the resolved model from the SDK system_init message so
    // `formatCommitMessage` can record `Model: <name>` as a commit trailer.
    let initModel: string | undefined;

    for await (const msg of q) {
      onMessage?.(msg);

      // Track session ID from any message that has it
      if ("session_id" in msg && typeof msg.session_id === "string") {
        sessionId = msg.session_id;
      }

      if (classifyMessage(msg) === "system_init") {
        const m = (msg as unknown as { model?: unknown }).model;
        if (typeof m === "string" && m.trim().length > 0) initModel = m.trim();
      }

      if (msg.type === "result") {
        result = parseResult(msg as SDKResultMessage);
      }
    }

    if (!result) {
      return {
        sessionId,
        success: false,
        errors: ["Stream ended without result message"],
        totalCostUsd: 0,
        numTurns: 0,
        usage: { input_tokens: 0, output_tokens: 0 },
        modelName: initModel,
      };
    }

    return { ...result, modelName: result.modelName ?? initModel };
  }

  /** Register an abort controller for a session (for external abort) */
  registerController(sessionId: string, ac: AbortController): void {
    this.activeControllers.set(sessionId, ac);
  }

  /** Abort a session by ID */
  abortSession(sessionId: string): boolean {
    const ac = this.activeControllers.get(sessionId);
    if (!ac) return false;
    ac.abort();
    this.activeControllers.delete(sessionId);
    return true;
  }

  /** Clean up a controller after session ends */
  unregisterController(sessionId: string): void {
    this.activeControllers.delete(sessionId);
  }

  /** Resume an existing session */
  resumeSession(
    sessionId: string,
    config: Omit<SessionConfig, "resume"> & { prompt: string },
  ): { query: Query; abortController: AbortController } {
    return this.spawnSession({ ...config, resume: sessionId });
  }

  /** Number of sessions with registered controllers */
  get activeSessionCount(): number {
    return this.activeControllers.size;
  }
}
