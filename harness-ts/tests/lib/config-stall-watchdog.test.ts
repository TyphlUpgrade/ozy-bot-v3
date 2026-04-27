import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  loadConfig,
  STALL_WATCHDOG_DEFAULTS,
  type HarnessConfig,
  type StallWatchdogConfig,
} from "../../src/lib/config.js";
import { Orchestrator } from "../../src/orchestrator.js";
import { SessionManager, type GitOps } from "../../src/session/manager.js";
import { SDKClient } from "../../src/session/sdk.js";
import { MergeGate, type MergeGitOps } from "../../src/gates/merge.js";
import { StateManager } from "../../src/lib/state.js";

const BASE_TOML = `
[project]
name = "test-project"
root = "."
task_dir = "state/tasks"
state_file = "state/pipeline.json"
worktree_base = "/tmp/worktrees"
session_dir = "/tmp/sessions"

[discord]
bot_token_env = "DISCORD_TOKEN"
dev_channel = "dev"
ops_channel = "ops"
escalation_channel = "escalations"

[discord.agents.executor]
name = "test-bot"
avatar_url = "https://example.com/avatar.png"

[pipeline]
test_command = "npm test"
`;

let tmpDir: string;

function writeTempToml(content: string): string {
  const path = join(tmpDir, "project.toml");
  writeFileSync(path, content, "utf-8");
  return path;
}

describe("loadConfig — [stall_watchdog] block (commit 1/2)", () => {
  beforeEach(() => {
    tmpDir = join(tmpdir(), `harness-stall-watchdog-test-${Date.now()}`);
    mkdirSync(tmpDir, { recursive: true });
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("parses [stall_watchdog] block into typed StallWatchdogConfig", () => {
    const toml = BASE_TOML + `
[stall_watchdog]
enabled = true
check_interval_ms = 15000
executor_threshold_ms = 120000
architect_threshold_ms = 480000
reviewer_threshold_ms = 90000
`;
    const path = writeTempToml(toml);
    const config = loadConfig(path);

    expect(config.stall_watchdog).toBeDefined();
    expect(config.stall_watchdog?.enabled).toBe(true);
    expect(config.stall_watchdog?.check_interval_ms).toBe(15000);
    expect(config.stall_watchdog?.executor_threshold_ms).toBe(120000);
    expect(config.stall_watchdog?.architect_threshold_ms).toBe(480000);
    expect(config.stall_watchdog?.reviewer_threshold_ms).toBe(90000);
  });

  it("leaves stall_watchdog undefined when block omitted", () => {
    const path = writeTempToml(BASE_TOML);
    const config = loadConfig(path);

    expect(config.stall_watchdog).toBeUndefined();
  });

  it("STALL_WATCHDOG_DEFAULTS exports documented values", () => {
    expect(STALL_WATCHDOG_DEFAULTS.enabled).toBe(false);
    expect(STALL_WATCHDOG_DEFAULTS.check_interval_ms).toBe(30_000);
    expect(STALL_WATCHDOG_DEFAULTS.executor_threshold_ms).toBe(300_000);
    expect(STALL_WATCHDOG_DEFAULTS.architect_threshold_ms).toBe(600_000);
    expect(STALL_WATCHDOG_DEFAULTS.reviewer_threshold_ms).toBe(240_000);
  });

  it("orchestrator integration: stall_watchdog.enabled=false does NOT schedule the watchdog interval", async () => {
    vi.useFakeTimers();
    try {
      const root = join(tmpDir, "no-watchdog");
      mkdirSync(join(root, "tasks"), { recursive: true });
      const config = baseHarnessConfig(root, { enabled: false });
      const orch = makeOrchestrator(root, config);
      const before = vi.getTimerCount();
      orch.start();
      // start() synchronously schedules the watchdog interval (when enabled)
      // and kicks off poll() which eventually self-schedules a setTimeout
      // (the poll timer is registered after an `await` so we don't count it).
      // Disabled watchdog ⇒ no synchronous timer added.
      expect(vi.getTimerCount() - before).toBe(0);
      await orch.shutdown();
    } finally {
      vi.useRealTimers();
    }
  });

  it("orchestrator integration: stall_watchdog.enabled=true DOES schedule the watchdog interval", async () => {
    vi.useFakeTimers();
    try {
      const root = join(tmpDir, "with-watchdog");
      mkdirSync(join(root, "tasks"), { recursive: true });
      const config = baseHarnessConfig(root, { enabled: true, check_interval_ms: 60_000 });
      const orch = makeOrchestrator(root, config);
      const before = vi.getTimerCount();
      orch.start();
      // Enabled watchdog ⇒ +1 setInterval registered synchronously inside start().
      expect(vi.getTimerCount() - before).toBe(1);
      await orch.shutdown();
      // shutdown clears the interval.
      expect(vi.getTimerCount()).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });
});

// --- Helpers for orchestrator integration assertions ---

function baseHarnessConfig(root: string, watchdog: StallWatchdogConfig): HarnessConfig {
  return {
    project: {
      name: "test",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "worktrees"),
      session_dir: join(root, "sessions"),
    },
    pipeline: {
      poll_interval: 60,
      test_command: "echo ok",
      max_retries: 3,
      test_timeout: 180,
      escalation_timeout: 14400,
      retry_delay_ms: 1000,
    },
    discord: {
      bot_token_env: "TOKEN",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {},
    },
    stall_watchdog: watchdog,
  };
}

function makeOrchestrator(root: string, config: HarnessConfig): Orchestrator {
  const sdk = new SDKClient(() => { throw new Error("query not used in this test"); });
  const state = new StateManager(join(root, "state.json"));
  const sessionMgr = new SessionManager(sdk, state, config, stubGitOps());
  const mergeGate = new MergeGate(config.pipeline, root, stubMergeGitOps());
  return new Orchestrator({ sessionManager: sessionMgr, mergeGate, stateManager: state, config });
}

function stubGitOps(): GitOps {
  return {
    createWorktree: () => { /* no-op */ },
    removeWorktree: () => { /* no-op */ },
    branchExists: () => false,
    deleteBranch: () => { /* no-op */ },
  };
}

function stubMergeGitOps(): MergeGitOps {
  return {
    hasUncommittedChanges: () => false,
    autoCommit: () => "sha-stub",
    getHeadSha: () => "sha-stub",
    rebase: () => ({ success: true, conflictFiles: [] }),
    rebaseAbort: () => { /* no-op */ },
    mergeNoFf: () => "merge-sha",
    revertLastMerge: () => { /* no-op */ },
    runTests: () => ({ success: true, output: "ok" }),
    getTrunkBranch: () => "master",
    branchHasCommitsAheadOfTrunk: () => false,
    diffNameOnly: () => [],
    scrubHarnessFromHead: () => false,
    getUserEmail: () => "test@example",
  };
}
