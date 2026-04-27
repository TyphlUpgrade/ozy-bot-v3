import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { writeFileSync, mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadConfig, loadSystemPrompt, DISCORD_REPLY_THREADING_DEFAULTS, OUTBOUND_EPISTLE_DEFAULTS } from "../../src/lib/config.js";

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

  it("CW-1 parses [discord.webhooks] with optional dev/ops/escalation URLs", () => {
    const toml = VALID_TOML + `
[discord.webhooks]
dev = "https://discord.com/api/webhooks/111/dev-tok"
ops = "https://discord.com/api/webhooks/222/ops-tok"
`;
    const path = writeTempToml(toml);
    const config = loadConfig(path);

    expect(config.discord.webhooks?.dev).toBe("https://discord.com/api/webhooks/111/dev-tok");
    expect(config.discord.webhooks?.ops).toBe("https://discord.com/api/webhooks/222/ops-tok");
    expect(config.discord.webhooks?.escalation).toBeUndefined();
  });

  it("Wave E-β parses [discord.reply_threading] with explicit enabled + stale_chain_ms", () => {
    const toml = VALID_TOML + `
[discord.reply_threading]
enabled = false
stale_chain_ms = 90000
`;
    const path = writeTempToml(toml);
    const config = loadConfig(path);

    expect(config.discord.reply_threading?.enabled).toBe(false);
    expect(config.discord.reply_threading?.stale_chain_ms).toBe(90000);
  });

  it("Wave E-β leaves discord.reply_threading undefined when block absent", () => {
    const path = writeTempToml(VALID_TOML);
    const config = loadConfig(path);
    expect(config.discord.reply_threading).toBeUndefined();
  });

  it("Wave E-β DISCORD_REPLY_THREADING_DEFAULTS exports documented values", () => {
    expect(DISCORD_REPLY_THREADING_DEFAULTS.enabled).toBe(true);
    expect(DISCORD_REPLY_THREADING_DEFAULTS.stale_chain_ms).toBe(600_000);
  });

  it("Wave E-γ parses [discord] outbound_epistle_enabled + llm_daily_cap_usd", () => {
    // VALID_TOML ends inside [discord.agents.executor], so re-open [discord]
    // explicitly before adding the flat E-γ fields.
    const toml = `
[project]
name = "test-project"
root = "."
task_dir = "state/tasks"
state_file = "state/pipeline.json"
worktree_base = "/tmp/worktrees"
session_dir = "/tmp/sessions"

[pipeline]
test_command = "npm test"

[discord]
bot_token_env = "DISCORD_TOKEN"
dev_channel = "dev"
ops_channel = "ops"
escalation_channel = "escalations"
outbound_epistle_enabled = true
llm_daily_cap_usd = 3.0

[discord.agents.executor]
name = "test-bot"
avatar_url = "https://example.com/avatar.png"
`;
    const path = writeTempToml(toml);
    const config = loadConfig(path);
    expect(config.discord.outbound_epistle_enabled).toBe(true);
    expect(config.discord.llm_daily_cap_usd).toBe(3.0);
  });

  it("Wave E-γ leaves outbound_epistle_enabled + llm_daily_cap_usd undefined when absent", () => {
    const path = writeTempToml(VALID_TOML);
    const config = loadConfig(path);
    expect(config.discord.outbound_epistle_enabled).toBeUndefined();
    expect(config.discord.llm_daily_cap_usd).toBeUndefined();
  });

  it("Wave E-γ OUTBOUND_EPISTLE_DEFAULTS exports documented values", () => {
    expect(OUTBOUND_EPISTLE_DEFAULTS.outbound_epistle_enabled).toBe(false);
    expect(OUTBOUND_EPISTLE_DEFAULTS.llm_daily_cap_usd).toBe(5.0);
  });

  // Channel-collapse plumbing (2026-04-27).
  it("channel-collapse: parses [discord] operator_user_id snowflake string", () => {
    const toml = `
[project]
name = "test-project"
root = "."
task_dir = "state/tasks"
state_file = "state/pipeline.json"
worktree_base = "/tmp/worktrees"
session_dir = "/tmp/sessions"

[pipeline]
test_command = "npm test"

[discord]
bot_token_env = "DISCORD_TOKEN"
dev_channel = "dev"
ops_channel = "ops"
escalation_channel = "escalations"
operator_user_id = "249313669337317379"

[discord.agents.executor]
name = "test-bot"
avatar_url = "https://example.com/avatar.png"
`;
    const path = writeTempToml(toml);
    const config = loadConfig(path);
    expect(config.discord.operator_user_id).toBe("249313669337317379");
  });

  it("channel-collapse: leaves operator_user_id undefined when absent", () => {
    const path = writeTempToml(VALID_TOML);
    const config = loadConfig(path);
    expect(config.discord.operator_user_id).toBeUndefined();
  });

  it.todo("notifier prepends operator mention for escalation events when operator_user_id set — commit 2");

  it("Wave E-β notifier consults config flag — commit 2: reply_threading.enabled=false skips lookupRoleHead and never sets replyToMessageId", async () => {
    const { DiscordNotifier } = await import("../../src/discord/notifier.js");
    const { InMemoryMessageContext } = await import("../../src/discord/message-context.js");
    const { sendToChannelAndReturnIdDefault } = await import("../../src/discord/types.js");

    // Spy MessageContext: tracks lookupRoleHead invocations.
    let lookups = 0;
    const ctx = new InMemoryMessageContext();
    const wrappedCtx = {
      recordAgentMessage: ctx.recordAgentMessage.bind(ctx),
      resolveProjectIdForMessage: ctx.resolveProjectIdForMessage.bind(ctx),
      recordRoleMessage: ctx.recordRoleMessage.bind(ctx),
      lookupRoleHead: (projectId: string, role: import("../../src/discord/message-context.js").AgentRole, channel: string): string | null => {
        lookups += 1;
        return ctx.lookupRoleHead(projectId, role, channel);
      },
    };
    // Pre-seed an architect head so a chain rule WOULD trigger if enabled.
    ctx.recordRoleMessage("P1", "architect", "head-x", "dev");

    // Sender that records replyToMessageId on the actual outbound call. The
    // notifier dispatches via `sendToChannelAndReturnId` whenever
    // messageContext is wired (CW-3 path), so only that branch records here.
    const sentReplyTos: Array<string | undefined> = [];
    const sender: import("../../src/discord/types.js").DiscordSender = {
      async sendToChannel(_c, _b, _i, replyToMessageId) {
        sentReplyTos.push(replyToMessageId);
      },
      async sendToChannelAndReturnId(_c, _b, _i, replyToMessageId) {
        sentReplyTos.push(replyToMessageId);
        return { messageId: null };
      },
      async addReaction() {
        /* no-op */
      },
    };
    // sendToChannelAndReturnIdDefault is intentionally unused below — kept the
    // import shape for clarity; the local fake returns directly.
    void sendToChannelAndReturnIdDefault;

    const fakeState = {
      getTask(taskId: string) {
        if (taskId === "task-X") return { id: "task-X", projectId: "P1" };
        return undefined;
      },
    } as unknown as import("../../src/lib/state.js").StateManager;

    const config = {
      bot_token_env: "T",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {},
      reply_threading: { enabled: false },
    };
    const notifier = new DiscordNotifier(sender, config, { messageContext: wrappedCtx, stateManager: fakeState });
    notifier.handleEvent({ type: "session_complete", taskId: "task-X", success: true, errors: [] });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(lookups).toBe(0);
    expect(sentReplyTos).toEqual([undefined]);
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
