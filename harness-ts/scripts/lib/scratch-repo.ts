/**
 * Shared scratch-repo scaffolding for live-run scripts.
 *
 * Three live scripts previously duplicated ~120 lines of init/config boilerplate:
 *   - scripts/live-project.ts
 *   - scripts/live-project-3phase.ts
 *   - scripts/live-project-arbitration.ts
 *
 * This module consolidates:
 *   - initScratchRepo: `mkdtempSync` for symlink-race safety + git init + prompt copy.
 *   - buildBaseConfig: HarnessConfig skeleton with sensible defaults + caller overrides.
 *   - Terminal-state + timeout constants.
 *   - SIGINT cleanup wiring so Ctrl-C during a long run still aborts sessions.
 */

import { mkdtempSync, mkdirSync, writeFileSync, copyFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import type { HarnessConfig } from "../../src/lib/config.js";

export const DEFAULT_POLL_INTERVAL_SEC = 3;
export const DEFAULT_POLL_LOOP_MS = 2_000;
export const DEFAULT_RUN_TIMEOUT_MS = 25 * 60 * 1_000;
export const DEFAULT_RETRY_DELAY_MS = 5_000;

const HARNESS_ROOT = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

export interface InitScratchRepoOpts {
  /** Prefix for the scratch dir under tmpdir. */
  prefix: string;
  /** Git commit author email. */
  gitEmail?: string;
  /** Git commit author name. */
  gitName?: string;
  /** Which prompt files to copy from harness-ts/config/harness/ into the scratch repo. */
  promptFiles?: readonly string[];
}

/**
 * Create an isolated scratch git repo under tmpdir. Returns the root path.
 * Uses `mkdtempSync` so collisions + symlink-race attacks are impossible.
 */
export function initScratchRepo(opts: InitScratchRepoOpts): string {
  const root = mkdtempSync(join(tmpdir(), `${opts.prefix}-`));
  mkdirSync(join(root, "tasks"), { recursive: true });
  mkdirSync(join(root, "worktrees"), { recursive: true });
  mkdirSync(join(root, "sessions"), { recursive: true });
  if (opts.promptFiles && opts.promptFiles.length > 0) {
    mkdirSync(join(root, "config", "harness"), { recursive: true });
    for (const promptFile of opts.promptFiles) {
      copyFileSync(
        join(HARNESS_ROOT, "config", "harness", promptFile),
        join(root, "config", "harness", promptFile),
      );
    }
  }
  // argv form (no shell) — safe even when callers thread untrusted text into
  // gitEmail/gitName. Each git call is its own execFileSync.
  execFileSync("git", ["init", "-b", "main"], { cwd: root, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", opts.gitEmail ?? "live@harness.test"], {
    cwd: root,
    stdio: "ignore",
  });
  execFileSync("git", ["config", "user.name", opts.gitName ?? "live"], {
    cwd: root,
    stdio: "ignore",
  });
  writeFileSync(join(root, "README.md"), `# scratch ${opts.prefix}\n`);
  // `.harness/` (completion-signal dir) and `.omc/` (OMC plugin state) must
  // be gitignored — without it both rebase-conflict across phases on trunk.
  writeFileSync(
    join(root, ".gitignore"),
    "tasks/\nworktrees/\nsessions/\nstate.json\nprojects.json\nstate.log.jsonl\n.harness/\n.omc/\n",
  );
  execFileSync("git", ["add", "-A"], { cwd: root, stdio: "ignore" });
  execFileSync("git", ["commit", "-m", "init"], { cwd: root, stdio: "ignore" });
  return root;
}

export interface BuildBaseConfigOpts {
  root: string;
  projectName: string;
  /** Override the default pipeline section. Merged on top. */
  pipelineOverrides?: Partial<HarnessConfig["pipeline"]>;
  /** Reviewer config (optional). */
  reviewer?: HarnessConfig["reviewer"];
  /** Architect config (optional). */
  architect?: HarnessConfig["architect"];
  /** Executor system prompt override. */
  systemPrompt?: string;
}

export function buildBaseConfig(opts: BuildBaseConfigOpts): HarnessConfig {
  const cfg: HarnessConfig = {
    project: {
      name: opts.projectName,
      root: opts.root,
      task_dir: join(opts.root, "tasks"),
      state_file: join(opts.root, "state.json"),
      worktree_base: join(opts.root, "worktrees"),
      session_dir: join(opts.root, "sessions"),
    },
    pipeline: {
      poll_interval: DEFAULT_POLL_INTERVAL_SEC,
      test_command: "true",
      max_retries: 1,
      test_timeout: 120,
      escalation_timeout: 600,
      retry_delay_ms: DEFAULT_RETRY_DELAY_MS,
      max_session_retries: 1,
      max_budget_usd: 1.0,
      auto_escalate_on_max_retries: false,
      max_tier1_escalations: 1,
      ...opts.pipelineOverrides,
    },
    discord: {
      bot_token_env: "UNUSED",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {
        orchestrator: { name: "Harness", avatar_url: "" },
        architect: { name: "Architect", avatar_url: "" },
        reviewer: { name: "Reviewer", avatar_url: "" },
      },
    },
  };
  if (opts.reviewer) cfg.reviewer = opts.reviewer;
  if (opts.architect) cfg.architect = opts.architect;
  if (opts.systemPrompt !== undefined) cfg.systemPrompt = opts.systemPrompt;
  return cfg;
}

/**
 * Unified terminal-state predicate for live scripts: returns true when the
 * project (if injected) or all known tasks are in a terminal state.
 */
export function isProjectTerminal(
  projectState: string | undefined,
): boolean {
  return projectState === "completed" || projectState === "failed" || projectState === "aborted";
}

/**
 * Install a SIGINT handler that aborts all live sessions before the process
 * exits. Accepts any object exposing a shutdown-able Orchestrator-like shape.
 */
export interface GracefulShutdown {
  shutdown: () => Promise<void>;
}

export function installSigintHandler(components: GracefulShutdown[]): void {
  let fired = false;
  process.on("SIGINT", () => {
    if (fired) return;
    fired = true;
    console.error("[scratch-repo] SIGINT received — shutting down components...");
    Promise.all(components.map((c) => c.shutdown().catch(() => undefined))).finally(() => {
      process.exit(130);
    });
  });
}
