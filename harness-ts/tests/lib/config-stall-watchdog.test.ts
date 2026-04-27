import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadConfig, STALL_WATCHDOG_DEFAULTS } from "../../src/lib/config.js";

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

  it.todo("orchestrator integration — commit 2");
});
