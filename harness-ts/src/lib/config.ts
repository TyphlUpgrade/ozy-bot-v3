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
}

export interface DiscordAgentIdentity {
  name: string;
  avatar_url: string;
}

export interface DiscordConfig {
  bot_token_env: string;
  dev_channel: string;
  ops_channel: string;
  escalation_channel: string;
  webhook_url?: string;
  agents: Record<string, DiscordAgentIdentity>;
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

export interface HarnessConfig {
  project: ProjectConfig;
  pipeline: PipelineConfig;
  discord: DiscordConfig;
  reviewer?: ReviewerConfig;       // optional — ReviewGate defaults apply when omitted
  architect?: ArchitectFileConfig; // optional — ArchitectManager defaults apply when omitted
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
 * the three conventional keys; new agent tiers should add their default here.
 */
export const DISCORD_AGENT_DEFAULTS: Readonly<Record<string, DiscordAgentIdentity>> = {
  orchestrator: { name: "Harness", avatar_url: "" },
  architect: { name: "Architect", avatar_url: "" },
  reviewer: { name: "Reviewer", avatar_url: "" },
};

function parseDiscord(raw: Record<string, unknown>): DiscordConfig {
  const section = "discord";
  const agentsRaw = (raw.agents ?? {}) as Record<string, Record<string, unknown>>;
  const parsed: Record<string, DiscordAgentIdentity> = {};
  for (const [name, agentData] of Object.entries(agentsRaw)) {
    parsed[name] = parseDiscordAgent(agentData, name);
  }
  // Project config overrides defaults; unknown agent keys pass through.
  const agents: Record<string, DiscordAgentIdentity> = { ...DISCORD_AGENT_DEFAULTS, ...parsed };
  return {
    bot_token_env: requireString(raw, "bot_token_env", section),
    dev_channel: requireString(raw, "dev_channel", section),
    ops_channel: requireString(raw, "ops_channel", section),
    escalation_channel: requireString(raw, "escalation_channel", section),
    webhook_url: optionalString(raw, "webhook_url"),
    agents,
  };
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
