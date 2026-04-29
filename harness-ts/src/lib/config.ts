/**
 * TOML config loader for harness configuration.
 * Reads config/harness/project.toml and returns typed HarnessConfig.
 */

import { readFileSync, existsSync } from "node:fs";
import { resolve, join } from "node:path";
import { parse } from "smol-toml";

// --- Types ---

export interface ProjectConfig {
  name: string;
  root: string;
  task_dir: string;
  state_file: string;
  worktree_base: string;
  session_dir: string;
  signal_dir?: string;
  /**
   * Wave R3 — optional project-level smoke test. When set, the orchestrator
   * runs this shell command in `root` after the last phase completes; if it
   * exits non-zero, `project_failed` is emitted instead of `project_completed`.
   * Catches "all phases green individually but the trunk doesn't actually
   * compose" failures (e.g. missing build-system block, broken imports across
   * phase boundaries). Unset = no smoke (default, backward-compatible).
   */
  final_test_command?: string;
}

export interface PipelineConfig {
  poll_interval: number;
  test_command: string;
  max_retries: number;
  test_timeout: number;
  escalation_timeout: number;
  retry_delay_ms: number;
  max_session_retries?: number;           // session-failure retries (default 3, separate from max_retries which caps rebase retries)
  max_budget_usd?: number;                // per-task budget cap — budget_report events at 50%/80%
  auto_escalate_on_max_retries?: boolean; // auto-escalate after max_session_retries exhausted (default true)
  max_tier1_escalations?: number;         // circuit breaker — max escalation cycles before permanent failure (default 2)
  plugins?: Record<string, boolean>;      // Wave 1 Item 1 — OMC/caveman plugin enablement, merges with defaults
  disallowed_tools?: string[];            // Wave 1 Item 3 — additional tools to block, extends default blocklist
  persistent_session_warn_threshold?: number; // Wave C / U4 — warn after N cumulative Executor spawns (default 100)
}

export const PERSISTENT_SESSION_WARN_THRESHOLD_DEFAULT = 100;

export interface DiscordAgentIdentity {
  name: string;
  avatar_url: string;
}

/**
 * CW-1 — per-channel webhook URLs. Optional; when present they upgrade the
 * given channel from BotSender to WebhookSender so per-agent avatars render
 * natively. Runtime-only — populated from env (DISCORD_WEBHOOK_DEV/OPS/...) at
 * script startup, never embedded in TOML.
 */
export interface DiscordWebhookUrls {
  dev?: string;
  ops?: string;
  escalation?: string;
}

/**
 * Wave E-β — `[discord.reply_threading]` block. Optional. Controls Discord
 * `message_reference` synthesis for multi-event project chains in the
 * orchestrator. `enabled` defaults to TRUE because chain rendering is pure
 * UX (no LLM, no $ cost) and operators have already requested it. See
 * `.omc/plans/2026-04-27-discord-wave-e-beta.md`.
 */
export interface DiscordReplyThreadingConfig {
  /** Default true — chains are pure UX, no cost. Set false to fall back to Wave E-α flat output. */
  enabled?: boolean;
  /** Default 600_000 (10 min). Lookups for role heads older than this evict + return null. */
  stale_chain_ms?: number;
}

export const DISCORD_REPLY_THREADING_DEFAULTS = {
  enabled: true,
  stale_chain_ms: 600_000,
} as const;

/**
 * Wave E-γ — outbound LLM-voice scaffolding defaults. The flag is OFF by
 * default; commit 2 wires the consumer behind this flag. The daily cap is the
 * soft ceiling on per-project LLM-rewrite spend; per-call is bounded
 * separately by `OutboundResponseGenerator.maxBudgetUsd` (default $0.02).
 */
export const OUTBOUND_EPISTLE_DEFAULTS = {
  outbound_epistle_enabled: false,
  llm_daily_cap_usd: 5.0,
} as const;

/**
 * Wave E-δ — NudgeIntrospector defaults. The flag is OFF by default; commit
 * 2b wires the consumer at bootstrap. `nudge_interval_ms` is the periodic
 * timer interval (default 10 min).
 */
export const NUDGE_DEFAULTS = {
  nudge_enabled: false,
  nudge_interval_ms: 600_000,
} as const;

export interface DiscordConfig {
  bot_token_env: string;
  dev_channel: string;
  ops_channel: string;
  escalation_channel: string;
  webhook_url?: string;
  webhooks?: DiscordWebhookUrls;
  agents: Record<string, DiscordAgentIdentity>;
  /**
   * Wave E-β — opt-in/opt-out of reply-chain rendering. Block absent →
   * `undefined`; consumer applies `DISCORD_REPLY_THREADING_DEFAULTS`.
   */
  reply_threading?: DiscordReplyThreadingConfig;
  /**
   * Wave E-γ — when true, eligible (event, role) tuples route through
   * `OutboundResponseGenerator` for first-person LLM voice. Default false
   * until 48h Batch-1 smoke window completes + operator visual sign-off.
   * Commit 1 (scaffolding) only adds the field; commit 2 wires the consumer.
   */
  outbound_epistle_enabled?: boolean;
  /**
   * Wave E-γ — daily LLM spend cap (USD) for outbound rewrites. Default 5.0.
   * Tracked in `<project.root>/.harness/llm-budget.json` (atomic temp+rename).
   */
  llm_daily_cap_usd?: number;
  /**
   * Discord user ID (snowflake) for operator. When set, escalation-class
   * events prepend a mention `<@operator_user_id>` to the body and use
   * allowedMentions: { users: [operator_user_id] } so the ping fires.
   * Without this set, escalation events still emit but without ping.
   * Wave channel-collapse (2026-04-27) — ops wants single channel with
   * operator ping for attention rather than separate escalation_channel.
   */
  operator_user_id?: string;
  /**
   * Wave E-δ N7 — when true, NudgeIntrospector is constructed and started at
   * bootstrap. Default false — operator opts in. Commit 2b wires consumer.
   */
  nudge_enabled?: boolean;
  /**
   * Wave E-δ N7 — periodic timer interval. Default 600_000 (10 min). Min
   * 60_000 enforced at construction (commit 2b).
   */
  nudge_interval_ms?: number;
}

export interface ReviewerConfig {
  model?: string;                  // default claude-sonnet-4-6 (Reviewer tier, M.13.4 locked)
  max_budget_usd?: number;         // per-review cap
  reject_threshold?: number;       // weightedRisk above which default verdict is reject
  timeout_ms?: number;
  arbitration_threshold?: number;  // default 2 — transitions to review_arbitration on N-th project reject
}

export interface ArchitectFileConfig {
  model?: string;                         // default claude-opus-4-7 (Architect tier, plan M.12.2)
  max_budget_usd?: number;                // per-Architect-session cap
  compaction_threshold_pct?: number;      // default 0.60 — fraction of budget ceiling that fires compaction
  arbitration_timeout_ms?: number;        // default 300_000 — handleEscalation/handleReviewArbitration budget
  prompt_path?: string;                   // path to architect-prompt.md (relative to project.root if not absolute)
}

/**
 * Stall watchdog — opt-in interval timer (commit 2 wires the orchestrator side).
 * Commit 1 ships only the config block + `lastActivityAt` tap on SessionResult.
 * Per-tier thresholds reflect typical session shapes: Reviewer < Executor < Architect.
 */
export interface StallWatchdogConfig {
  enabled?: boolean;                  // default false (opt-in)
  check_interval_ms?: number;         // default 30_000
  executor_threshold_ms?: number;     // default 300_000 (5 min)
  architect_threshold_ms?: number;    // default 600_000 (10 min)
  reviewer_threshold_ms?: number;     // default 240_000 (4 min)
}

export const STALL_WATCHDOG_DEFAULTS = {
  enabled: false,
  check_interval_ms: 30_000,
  executor_threshold_ms: 300_000,
  architect_threshold_ms: 600_000,
  reviewer_threshold_ms: 240_000,
} as const;

export interface HarnessConfig {
  project: ProjectConfig;
  pipeline: PipelineConfig;
  discord: DiscordConfig;
  reviewer?: ReviewerConfig;       // optional — ReviewGate defaults apply when omitted
  architect?: ArchitectFileConfig; // optional — ArchitectManager defaults apply when omitted
  stall_watchdog?: StallWatchdogConfig; // optional — orchestrator interval timer (commit 2)
  systemPrompt?: string;           // loaded from prompt file at startup, cached
}

// --- Defaults ---

const PIPELINE_DEFAULTS: Partial<PipelineConfig> = {
  poll_interval: 5,
  max_retries: 3,
  test_timeout: 180,
  escalation_timeout: 14400,
  retry_delay_ms: 300_000, // 5 minutes — cooldown before shelved task auto-retry
};

// --- Loader ---

function requireField(obj: Record<string, unknown>, field: string, section: string): unknown {
  const val = obj[field];
  if (val === undefined || val === null) {
    throw new Error(`Missing required field '${field}' in [${section}]`);
  }
  return val;
}

function requireString(obj: Record<string, unknown>, field: string, section: string): string {
  const val = requireField(obj, field, section);
  if (typeof val !== "string") {
    throw new Error(`Field '${field}' in [${section}] must be a string, got ${typeof val}`);
  }
  return val;
}

function optionalNumber(obj: Record<string, unknown>, field: string, fallback: number): number {
  const val = obj[field];
  if (val === undefined || val === null) return fallback;
  if (typeof val !== "number") return fallback;
  return val;
}

function optionalString(obj: Record<string, unknown>, field: string): string | undefined {
  const val = obj[field];
  if (val === undefined || val === null) return undefined;
  if (typeof val !== "string") return undefined;
  return val;
}

function parseProject(raw: Record<string, unknown>): ProjectConfig {
  const section = "project";
  return {
    name: requireString(raw, "name", section),
    root: requireString(raw, "root", section),
    task_dir: requireString(raw, "task_dir", section),
    state_file: requireString(raw, "state_file", section),
    worktree_base: requireString(raw, "worktree_base", section),
    session_dir: requireString(raw, "session_dir", section),
    signal_dir: optionalString(raw, "signal_dir"),
    final_test_command: optionalString(raw, "final_test_command"),
  };
}

function optionalBoolean(obj: Record<string, unknown>, field: string, fallback: boolean): boolean {
  const val = obj[field];
  if (val === undefined || val === null) return fallback;
  if (typeof val !== "boolean") return fallback;
  return val;
}

function parsePipeline(raw: Record<string, unknown>): PipelineConfig {
  const section = "pipeline";
  const config: PipelineConfig = {
    poll_interval: optionalNumber(raw, "poll_interval", PIPELINE_DEFAULTS.poll_interval!),
    test_command: requireString(raw, "test_command", section),
    max_retries: optionalNumber(raw, "max_retries", PIPELINE_DEFAULTS.max_retries!),
    test_timeout: optionalNumber(raw, "test_timeout", PIPELINE_DEFAULTS.test_timeout!),
    escalation_timeout: optionalNumber(raw, "escalation_timeout", PIPELINE_DEFAULTS.escalation_timeout!),
    retry_delay_ms: optionalNumber(raw, "retry_delay_ms", PIPELINE_DEFAULTS.retry_delay_ms!),
  };
  // Phase 2A optional fields
  if (raw.max_session_retries !== undefined) config.max_session_retries = optionalNumber(raw, "max_session_retries", 3);
  if (raw.max_budget_usd !== undefined) config.max_budget_usd = optionalNumber(raw, "max_budget_usd", 0);
  if (raw.auto_escalate_on_max_retries !== undefined) config.auto_escalate_on_max_retries = optionalBoolean(raw, "auto_escalate_on_max_retries", true);
  if (raw.max_tier1_escalations !== undefined) config.max_tier1_escalations = optionalNumber(raw, "max_tier1_escalations", 2);
  // Wave 1 Item 1: plugin enablement
  if (raw.plugins !== undefined && raw.plugins && typeof raw.plugins === "object" && !Array.isArray(raw.plugins)) {
    const plugins: Record<string, boolean> = {};
    for (const [name, enabled] of Object.entries(raw.plugins as Record<string, unknown>)) {
      if (typeof enabled === "boolean") plugins[name] = enabled;
    }
    config.plugins = plugins;
  }
  // Wave 1 Item 3: additional disallowed tools
  if (Array.isArray(raw.disallowed_tools)) {
    const tools = (raw.disallowed_tools as unknown[]).filter(
      (s): s is string => typeof s === "string",
    );
    config.disallowed_tools = tools;
  }
  // Wave C / U4: persistent-session warn threshold
  if (raw.persistent_session_warn_threshold !== undefined) {
    config.persistent_session_warn_threshold = optionalNumber(
      raw,
      "persistent_session_warn_threshold",
      PERSISTENT_SESSION_WARN_THRESHOLD_DEFAULT,
    );
  }
  return config;
}

function parseDiscordAgent(raw: Record<string, unknown>, agentName: string): DiscordAgentIdentity {
  return {
    name: requireString(raw, "name", `discord.agents.${agentName}`),
    avatar_url: requireString(raw, "avatar_url", `discord.agents.${agentName}`),
  };
}

/**
 * Config-free Discord agent identity defaults. Any agent key missing from
 * `[discord.agents.*]` in project.toml resolves to these. Wave 2 establishes
 * the three conventional keys; CW-1 adds `executor` and `operator` so the
 * conversational pipeline can route per-agent identity without forcing every
 * deployment to declare them. New agent tiers should add their default here.
 */
export const DISCORD_AGENT_DEFAULTS: Readonly<Record<string, DiscordAgentIdentity>> = {
  orchestrator: { name: "Harness", avatar_url: "https://api.dicebear.com/9.x/bottts-neutral/png?seed=harness-orchestrator" },
  architect: { name: "Architect", avatar_url: "https://api.dicebear.com/9.x/bottts-neutral/png?seed=harness-architect" },
  reviewer: { name: "Reviewer", avatar_url: "https://api.dicebear.com/9.x/bottts-neutral/png?seed=harness-reviewer" },
  executor: { name: "Executor", avatar_url: "https://api.dicebear.com/9.x/bottts-neutral/png?seed=harness-executor" },
  operator: { name: "Operator", avatar_url: "https://api.dicebear.com/9.x/bottts-neutral/png?seed=harness-operator" },
};

function parseDiscordWebhooks(raw: unknown): DiscordWebhookUrls | undefined {
  // CW-1 — `[discord.webhooks]` is optional. URL values themselves usually come
  // from env at runtime; TOML support exists for completeness/local dev.
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const obj = raw as Record<string, unknown>;
  const out: DiscordWebhookUrls = {};
  if (typeof obj.dev === "string") out.dev = obj.dev;
  if (typeof obj.ops === "string") out.ops = obj.ops;
  if (typeof obj.escalation === "string") out.escalation = obj.escalation;
  return out;
}

function parseDiscordReplyThreading(raw: unknown): DiscordReplyThreadingConfig | undefined {
  // Wave E-β — block absent → undefined (consumer applies defaults).
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const obj = raw as Record<string, unknown>;
  const out: DiscordReplyThreadingConfig = {};
  if (typeof obj.enabled === "boolean") out.enabled = obj.enabled;
  if (typeof obj.stale_chain_ms === "number") out.stale_chain_ms = obj.stale_chain_ms;
  return out;
}

function parseDiscord(raw: Record<string, unknown>): DiscordConfig {
  const section = "discord";
  const agentsRaw = (raw.agents ?? {}) as Record<string, Record<string, unknown>>;
  const parsed: Record<string, DiscordAgentIdentity> = {};
  for (const [name, agentData] of Object.entries(agentsRaw)) {
    parsed[name] = parseDiscordAgent(agentData, name);
  }
  // Project config overrides defaults; unknown agent keys pass through.
  const agents: Record<string, DiscordAgentIdentity> = { ...DISCORD_AGENT_DEFAULTS, ...parsed };
  const cfg: DiscordConfig = {
    bot_token_env: requireString(raw, "bot_token_env", section),
    dev_channel: requireString(raw, "dev_channel", section),
    ops_channel: requireString(raw, "ops_channel", section),
    escalation_channel: requireString(raw, "escalation_channel", section),
    webhook_url: optionalString(raw, "webhook_url"),
    agents,
  };
  const webhooks = parseDiscordWebhooks(raw.webhooks);
  if (webhooks) cfg.webhooks = webhooks;
  const replyThreading = parseDiscordReplyThreading(raw.reply_threading);
  if (replyThreading) cfg.reply_threading = replyThreading;
  // Wave E-γ — flat optional fields under [discord]. Block absent → undefined
  // (consumer applies OUTBOUND_EPISTLE_DEFAULTS). Snake_case to match TOML
  // convention used elsewhere in DiscordConfig.
  if (typeof raw.outbound_epistle_enabled === "boolean") {
    cfg.outbound_epistle_enabled = raw.outbound_epistle_enabled;
  }
  if (typeof raw.llm_daily_cap_usd === "number") {
    cfg.llm_daily_cap_usd = raw.llm_daily_cap_usd;
  }
  // Channel-collapse plumbing (2026-04-27) — optional snowflake string. Block
  // absent → undefined (notifier default: no operator ping). When set, commit 2
  // wires the per-event mention prepend + allowedMentions override.
  const operatorUserId = optionalString(raw, "operator_user_id");
  if (operatorUserId) cfg.operator_user_id = operatorUserId;
  // Wave E-δ — flat optional fields under [discord]. Block absent → undefined
  // (consumer applies NUDGE_DEFAULTS). Snake_case to match TOML convention.
  if (typeof raw.nudge_enabled === "boolean") {
    cfg.nudge_enabled = raw.nudge_enabled;
  }
  if (typeof raw.nudge_interval_ms === "number") {
    cfg.nudge_interval_ms = raw.nudge_interval_ms;
  }
  return cfg;
}

export function loadConfig(configPath: string): HarnessConfig {
  const absPath = resolve(configPath);
  let content: string;
  try {
    content = readFileSync(absPath, "utf-8");
  } catch (err) {
    throw new Error(`Cannot read config file: ${absPath} — ${(err as Error).message}`);
  }

  let parsed: Record<string, unknown>;
  try {
    parsed = parse(content) as Record<string, unknown>;
  } catch (err) {
    throw new Error(`Invalid TOML in ${absPath}: ${(err as Error).message}`);
  }

  const projectRaw = parsed.project;
  if (!projectRaw || typeof projectRaw !== "object") {
    throw new Error("Missing required [project] section in config");
  }

  const pipelineRaw = parsed.pipeline;
  if (!pipelineRaw || typeof pipelineRaw !== "object") {
    throw new Error("Missing required [pipeline] section in config");
  }

  const discordRaw = parsed.discord;
  if (!discordRaw || typeof discordRaw !== "object") {
    throw new Error("Missing required [discord] section in config");
  }

  const reviewerRaw = parsed.reviewer;
  const architectRaw = parsed.architect;
  const stallWatchdogRaw = parsed.stall_watchdog;
  const cfg: HarnessConfig = {
    project: parseProject(projectRaw as Record<string, unknown>),
    pipeline: parsePipeline(pipelineRaw as Record<string, unknown>),
    discord: parseDiscord(discordRaw as Record<string, unknown>),
  };
  if (reviewerRaw && typeof reviewerRaw === "object") {
    cfg.reviewer = parseReviewer(reviewerRaw as Record<string, unknown>);
  }
  if (architectRaw && typeof architectRaw === "object") {
    cfg.architect = parseArchitect(architectRaw as Record<string, unknown>);
  }
  if (stallWatchdogRaw && typeof stallWatchdogRaw === "object") {
    cfg.stall_watchdog = parseStallWatchdog(stallWatchdogRaw as Record<string, unknown>);
  }
  return cfg;
}

function parseReviewer(raw: Record<string, unknown>): ReviewerConfig {
  const out: ReviewerConfig = {};
  if (typeof raw.model === "string") out.model = raw.model;
  if (typeof raw.max_budget_usd === "number") out.max_budget_usd = raw.max_budget_usd;
  if (typeof raw.reject_threshold === "number") out.reject_threshold = raw.reject_threshold;
  if (typeof raw.timeout_ms === "number") out.timeout_ms = raw.timeout_ms;
  if (typeof raw.arbitration_threshold === "number") out.arbitration_threshold = raw.arbitration_threshold;
  return out;
}

function parseArchitect(raw: Record<string, unknown>): ArchitectFileConfig {
  const out: ArchitectFileConfig = {};
  if (typeof raw.model === "string") out.model = raw.model;
  if (typeof raw.max_budget_usd === "number") out.max_budget_usd = raw.max_budget_usd;
  if (typeof raw.compaction_threshold_pct === "number") out.compaction_threshold_pct = raw.compaction_threshold_pct;
  if (typeof raw.arbitration_timeout_ms === "number") out.arbitration_timeout_ms = raw.arbitration_timeout_ms;
  if (typeof raw.prompt_path === "string") out.prompt_path = raw.prompt_path;
  return out;
}

function parseStallWatchdog(raw: Record<string, unknown>): StallWatchdogConfig {
  const out: StallWatchdogConfig = {};
  if (typeof raw.enabled === "boolean") out.enabled = raw.enabled;
  if (typeof raw.check_interval_ms === "number") out.check_interval_ms = raw.check_interval_ms;
  if (typeof raw.executor_threshold_ms === "number") out.executor_threshold_ms = raw.executor_threshold_ms;
  if (typeof raw.architect_threshold_ms === "number") out.architect_threshold_ms = raw.architect_threshold_ms;
  if (typeof raw.reviewer_threshold_ms === "number") out.reviewer_threshold_ms = raw.reviewer_threshold_ms;
  return out;
}

// --- Executor system prompt default (Wave C / U3) ---

/**
 * Phase 2A graduated-response routing requires `understanding`, `assumptions`,
 * `nonGoals`, and `confidence` on every completion.json; a base-fields-only
 * completion silently lands at response_level 1. Body kept identical to
 * `scripts/live-run.ts` SYSTEM_PROMPT_ENRICHED (validated 4/4 compliance).
 */
/**
 * Canonical default trunk branch name used when no explicit override is
 * configured. Single source of truth referenced by both MergeGate and
 * ReviewGate so construction order is symmetric and `git grep "master"`
 * returns one canonical hit.
 */
export const DEFAULT_TRUNK_BRANCH = "master";

/** WA-6 / Fresh-2: caps `recoverFromCrash` recursion to prevent wedge loops. */
export const MAX_RECOVERY_ATTEMPTS = 3;

export const DEFAULT_EXECUTOR_SYSTEM_PROMPT = `You are working inside a harness-managed git worktree.

When you finish your task, you MUST:
1. Write your code changes into the worktree. DO NOT run \`git add\`.
   DO NOT run \`git commit\`. The orchestrator will stage and commit your
   work after the Reviewer approves it.
2. Create directory \`.harness/\` if missing.
3. Write \`.harness/completion.json\` with this JSON shape (commitSha is
   no longer required — omit it):

\`\`\`
{
  "status": "success" | "failure",
  "summary": "<one sentence — used as the orchestrator commit message>",
  "filesChanged": ["path1", "path2"],
  "understanding": "<one-paragraph restatement of the task as you interpreted it>",
  "assumptions": ["<assumption 1>", "<assumption 2>"],
  "nonGoals": ["<thing you deliberately did not do 1>", "<thing 2>"],
  "confidence": {
    "scopeClarity": "clear" | "partial" | "unclear",
    "designCertainty": "obvious" | "alternatives_exist" | "guessing",
    "testCoverage": "verifiable" | "partial" | "untestable",
    "assumptions": [
      { "description": "<same as top-level assumption>", "impact": "high" | "low", "reversible": true | false }
    ],
    "openQuestions": ["<question the operator may need to answer>"]
  }
}
\`\`\`

All enrichment fields are required. Be honest about uncertainty: if the scope is not fully clear, say so; if you are guessing on design, say so. Do not fabricate certainty.

The completion file is how the orchestrator knows you are done. If you do not write it, the task will be marked failed. If you commit anyway, the orchestrator's compat path will accept your commit, but the canonical behavior is to leave the work uncommitted.
`;

// --- System Prompt Loader ---

/**
 * Load system prompt from a markdown file.
 * Returns empty string if the file does not exist (prompt is optional).
 */
export function loadSystemPrompt(promptPath: string): string {
  const absPath = resolve(promptPath);
  if (!existsSync(absPath)) return "";
  try {
    return readFileSync(absPath, "utf-8");
  } catch {
    return "";
  }
}
