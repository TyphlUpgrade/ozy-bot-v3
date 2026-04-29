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

import { mkdtempSync, mkdirSync, writeFileSync, copyFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
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
 * Create an isolated scratch git repo. Returns the root path.
 *
 * Wave R5 — scratch dirs now live under `harness-ts/.scratch/` (gitignored)
 * instead of `/tmp/`, so `npm run scratch:clean` can nuke them in one go and
 * leftovers from prior e2e/smoke runs don't clutter `/tmp` between sessions.
 * Override via `HARNESS_SCRATCH_BASE=/path/to/dir` (e.g. honor `/tmp` for CI
 * runs that need cross-mount isolation).
 *
 * `mkdtempSync` keeps collisions + symlink-race attacks impossible.
 */
export function initScratchRepo(opts: InitScratchRepoOpts): string {
  const baseDir = process.env.HARNESS_SCRATCH_BASE ?? join(HARNESS_ROOT, ".scratch");
  if (!existsSync(baseDir)) mkdirSync(baseDir, { recursive: true });
  const root = mkdtempSync(join(baseDir, `${opts.prefix}-`));
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
    [
      "tasks/",
      "worktrees/",
      "sessions/",
      "state.json",
      "projects.json",
      "state.log.jsonl",
      ".harness/",
      ".omc/",
      // Wave R5 — Python build/test artifacts. The new pytest-driven
      // test_command (cycle 2 US-A) writes `__pycache__/` + `*.pyc` into
      // the per-phase worktree; those untracked files then collide with
      // git merge --no-ff on the next phase's land. Same logic for
      // `.pytest_cache/`, eggs, and node_modules so JS-shaped projects
      // are also covered.
      "__pycache__/",
      "*.pyc",
      "*.pyo",
      ".pytest_cache/",
      "*.egg-info/",
      "node_modules/",
    ].join("\n") + "\n",
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
  /** Wave R3 — project-level smoke test run after the last phase merges. */
  finalTestCommand?: string;
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
      ...(opts.finalTestCommand ? { final_test_command: opts.finalTestCommand } : {}),
    },
    pipeline: {
      poll_interval: DEFAULT_POLL_INTERVAL_SEC,
      // Wave R3 — detection-at-eval-time quality gate. MergeGate runs this in
      // the per-phase worktree, so detection must happen NOW (a phase may have
      // just created pyproject.toml or tests/). A literal "true" passed
      // anything; this snippet (a) fails when pyproject.toml is missing the
      // [build-system] block (PEP 517 / pip-editable hard requirement), and
      // (b) runs pytest when test files are present. Non-Python phases short-
      // circuit cleanly because both checks are guarded by file/dir presence.
      test_command: "bash -c 'if [ -f pyproject.toml ] && ! grep -q \"^\\[build-system\\]\" pyproject.toml; then echo \"pyproject.toml missing [build-system] section\"; exit 1; fi; if [ -d tests ] && ls tests/test_*.py >/dev/null 2>&1; then PYTHONPATH=. python -m pytest -q --no-header tests 2>&1; fi; exit 0'",
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
      // When a real DISCORD_BOT_TOKEN is exported, env-supplied snowflakes
      // (DEV_CHANNEL / AGENT_CHANNEL / ALERTS_CHANNEL) replace the placeholder
      // slugs so BotSender's REST POSTs hit valid channels. Without these
      // overrides, BotSender would POST /channels/dev/messages and 400.
      bot_token_env: "DISCORD_BOT_TOKEN",
      dev_channel: process.env.DEV_CHANNEL ?? "dev",
      ops_channel: process.env.AGENT_CHANNEL ?? process.env.DEV_CHANNEL ?? "ops",
      escalation_channel: process.env.ALERTS_CHANNEL ?? process.env.DEV_CHANNEL ?? "esc",
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
