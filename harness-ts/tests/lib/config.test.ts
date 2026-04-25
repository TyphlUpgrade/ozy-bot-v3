import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadConfig, loadSystemPrompt } from "../../src/lib/config.js";

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

  it("parses Phase 2A optional pipeline fields", () => {
    const toml = VALID_TOML + `
max_session_retries = 5
max_budget_usd = 2.50
auto_escalate_on_max_retries = false
max_tier1_escalations = 3
`;
    const path = writeTempToml(toml);
    const config = loadConfig(path);

    expect(config.pipeline.max_session_retries).toBe(5);
    expect(config.pipeline.max_budget_usd).toBe(2.50);
    expect(config.pipeline.auto_escalate_on_max_retries).toBe(false);
    expect(config.pipeline.max_tier1_escalations).toBe(3);
  });

  it("leaves Phase 2A fields undefined when not in TOML", () => {
    const path = writeTempToml(VALID_TOML);
    const config = loadConfig(path);

    expect(config.pipeline.max_session_retries).toBeUndefined();
    expect(config.pipeline.max_budget_usd).toBeUndefined();
    expect(config.pipeline.auto_escalate_on_max_retries).toBeUndefined();
    expect(config.pipeline.max_tier1_escalations).toBeUndefined();
  });
});

describe("loadSystemPrompt", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = join(tmpdir(), `harness-prompt-test-${Date.now()}`);
    mkdirSync(tmpDir, { recursive: true });
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("loads prompt content from a file", () => {
    const promptPath = join(tmpDir, "system-prompt.md");
    writeFileSync(promptPath, "# Agent Protocol\nDo the thing.\n");
    const content = loadSystemPrompt(promptPath);
    expect(content).toContain("# Agent Protocol");
    expect(content).toContain("Do the thing.");
  });

  it("returns empty string when file does not exist", () => {
    const content = loadSystemPrompt(join(tmpDir, "nonexistent.md"));
    expect(content).toBe("");
  });

  it("loads the real system-prompt.md", () => {
    const realPath = join(process.cwd(), "..", "config", "harness", "system-prompt.md");
    const content = loadSystemPrompt(realPath);
    expect(content).toContain("Harness Agent Protocol");
    expect(content).toContain("completion.json");
    expect(content.length).toBeGreaterThan(100);
  });

  it("review-prompt.md carries the WA-3 propose-then-commit instructions", () => {
    const realPath = join(process.cwd(), "config", "harness", "review-prompt.md");
    const content = loadSystemPrompt(realPath);
    // New "Reading the proposal" section
    expect(content).toContain("git status --porcelain");
    expect(content).toContain("git diff");
    expect(content).toMatch(/agent has written files.*NOT committed/s);
    // Ground-truth section framing updated
    expect(content).toContain("proposed diff");
  });
});
