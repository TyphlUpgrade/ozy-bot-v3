import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadConfig } from "../../src/lib/config.js";

const VALID_TOML = `
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

describe("loadConfig", () => {
  beforeEach(() => {
    tmpDir = join(tmpdir(), `harness-config-test-${Date.now()}`);
    mkdirSync(tmpDir, { recursive: true });
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("parses valid TOML into typed HarnessConfig", () => {
    const path = writeTempToml(VALID_TOML);
    const config = loadConfig(path);

    expect(config.project.name).toBe("test-project");
    expect(config.project.root).toBe(".");
    expect(config.project.task_dir).toBe("state/tasks");
    expect(config.project.worktree_base).toBe("/tmp/worktrees");
    expect(config.pipeline.test_command).toBe("npm test");
    expect(config.discord.bot_token_env).toBe("DISCORD_TOKEN");
    expect(config.discord.agents.executor.name).toBe("test-bot");
    expect(config.discord.agents.executor.avatar_url).toBe("https://example.com/avatar.png");
  });

  it("applies default values for optional pipeline fields", () => {
    const path = writeTempToml(VALID_TOML);
    const config = loadConfig(path);

    expect(config.pipeline.poll_interval).toBe(5);
    expect(config.pipeline.max_retries).toBe(3);
    expect(config.pipeline.test_timeout).toBe(180);
    expect(config.pipeline.escalation_timeout).toBe(14400);
  });

  it("uses explicit values over defaults", () => {
    const toml = VALID_TOML + `
poll_interval = 10
max_retries = 5
test_timeout = 300
escalation_timeout = 7200
`;
    const path = writeTempToml(toml);
    const config = loadConfig(path);

    expect(config.pipeline.poll_interval).toBe(10);
    expect(config.pipeline.max_retries).toBe(5);
    expect(config.pipeline.test_timeout).toBe(300);
    expect(config.pipeline.escalation_timeout).toBe(7200);
  });

  it("throws on missing [project] section", () => {
    const toml = `
[pipeline]
test_command = "npm test"
[discord]
bot_token_env = "T"
dev_channel = "d"
ops_channel = "o"
escalation_channel = "e"
`;
    const path = writeTempToml(toml);
    expect(() => loadConfig(path)).toThrow("Missing required [project] section");
  });

  it("throws on missing required field in [project]", () => {
    const toml = `
[project]
name = "test"
[pipeline]
test_command = "npm test"
[discord]
bot_token_env = "T"
dev_channel = "d"
ops_channel = "o"
escalation_channel = "e"
`;
    const path = writeTempToml(toml);
    expect(() => loadConfig(path)).toThrow("Missing required field 'root' in [project]");
  });

  it("throws on missing [pipeline] section", () => {
    const toml = `
[project]
name = "t"
root = "."
task_dir = "t"
state_file = "s"
worktree_base = "/tmp/w"
session_dir = "/tmp/s"
[discord]
bot_token_env = "T"
dev_channel = "d"
ops_channel = "o"
escalation_channel = "e"
`;
    const path = writeTempToml(toml);
    expect(() => loadConfig(path)).toThrow("Missing required [pipeline] section");
  });

  it("throws on missing config file", () => {
    expect(() => loadConfig("/nonexistent/path.toml")).toThrow("Cannot read config file");
  });

  it("throws on invalid TOML syntax", () => {
    const path = writeTempToml("this is not valid [toml");
    expect(() => loadConfig(path)).toThrow("Invalid TOML");
  });

  it("parses the real project.toml", () => {
    const realPath = join(process.cwd(), "..", "config", "harness", "project.toml");
    const config = loadConfig(realPath);

    expect(config.project.name).toBe("ozymandias-v3");
    expect(config.discord.agents.orchestrator.name).toBeTruthy();
    expect(config.pipeline.test_command).toContain("pytest");
  });
});
