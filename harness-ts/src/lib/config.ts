/**
 * TOML config loader for harness configuration.
 * Reads config/harness/project.toml and returns typed HarnessConfig.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
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

export interface HarnessConfig {
  project: ProjectConfig;
  pipeline: PipelineConfig;
  discord: DiscordConfig;
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

function parsePipeline(raw: Record<string, unknown>): PipelineConfig {
  const section = "pipeline";
  return {
    poll_interval: optionalNumber(raw, "poll_interval", PIPELINE_DEFAULTS.poll_interval!),
    test_command: requireString(raw, "test_command", section),
    max_retries: optionalNumber(raw, "max_retries", PIPELINE_DEFAULTS.max_retries!),
    test_timeout: optionalNumber(raw, "test_timeout", PIPELINE_DEFAULTS.test_timeout!),
    escalation_timeout: optionalNumber(raw, "escalation_timeout", PIPELINE_DEFAULTS.escalation_timeout!),
    retry_delay_ms: optionalNumber(raw, "retry_delay_ms", PIPELINE_DEFAULTS.retry_delay_ms!),
  };
}

function parseDiscordAgent(raw: Record<string, unknown>, agentName: string): DiscordAgentIdentity {
  return {
    name: requireString(raw, "name", `discord.agents.${agentName}`),
    avatar_url: requireString(raw, "avatar_url", `discord.agents.${agentName}`),
  };
}

function parseDiscord(raw: Record<string, unknown>): DiscordConfig {
  const section = "discord";
  const agentsRaw = (raw.agents ?? {}) as Record<string, Record<string, unknown>>;
  const agents: Record<string, DiscordAgentIdentity> = {};
  for (const [name, agentData] of Object.entries(agentsRaw)) {
    agents[name] = parseDiscordAgent(agentData, name);
  }
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

  return {
    project: parseProject(projectRaw as Record<string, unknown>),
    pipeline: parsePipeline(pipelineRaw as Record<string, unknown>),
    discord: parseDiscord(discordRaw as Record<string, unknown>),
  };
}
