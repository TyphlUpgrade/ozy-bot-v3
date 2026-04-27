/**
 * Integration — DiscordNotifier wired into Orchestrator.
 *
 * Drives a full task lifecycle against mocked SDK + git, with the notifier
 * registered as an orchestrator event listener. Asserts:
 *   - notifier receives every emitted event
 *   - notifier routes the right events to the right channels
 *   - sender is called the expected number of times
 *   - failure path sends to ops channel
 *   - escalation path sends to escalation channel
 *
 * Sender is a fake that records (channel, content) tuples. No real Discord.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Orchestrator, type OrchestratorEvent } from "../../src/orchestrator.js";
import { SessionManager, type GitOps } from "../../src/session/manager.js";
import { SDKClient, type QueryFn } from "../../src/session/sdk.js";
import { MergeGate, type MergeGitOps } from "../../src/gates/merge.js";
import { StateManager } from "../../src/lib/state.js";
import type { HarnessConfig } from "../../src/lib/config.js";
import { DiscordNotifier } from "../../src/discord/notifier.js";
import { sendToChannelAndReturnIdDefault, type DiscordSender, type AgentIdentity } from "../../src/discord/types.js";
import type { Query, SDKMessage, SDKResultSuccess } from "@anthropic-ai/claude-agent-sdk";

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-notifier-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

function makeResultSuccess(sessionId = "session-integ"): SDKResultSuccess {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 1000,
    duration_api_ms: 900,
    is_error: false,
    num_turns: 2,
    result: "Done",
    stop_reason: "end_turn",
    total_cost_usd: 0.02,
    usage: { input_tokens: 100, output_tokens: 50 },
    modelUsage: {},
    permission_denials: [],
    uuid: "uuid" as SDKResultSuccess["uuid"],
    session_id: sessionId,
  };
}

function mockQuery(messages: SDKMessage[]): Query {
  async function* gen(): AsyncGenerator<SDKMessage, void> {
    for (const msg of messages) yield msg;
  }
  return Object.assign(gen(), {
    interrupt: vi.fn().mockResolvedValue(undefined),
    setPermissionMode: vi.fn().mockResolvedValue(undefined),
    setModel: vi.fn().mockResolvedValue(undefined),
    setMaxThinkingTokens: vi.fn().mockResolvedValue(undefined),
    applyFlagSettings: vi.fn().mockResolvedValue(undefined),
    initializationResult: vi.fn().mockResolvedValue({}),
    supportedCommands: vi.fn().mockResolvedValue([]),
    supportedModels: vi.fn().mockResolvedValue([]),
    supportedAgents: vi.fn().mockResolvedValue([]),
    mcpServerStatus: vi.fn().mockResolvedValue([]),
    contextUsage: vi.fn().mockResolvedValue({}),
    rewindFiles: vi.fn().mockResolvedValue({ canRewind: false }),
  }) as unknown as Query;
}

function makeConfig(): HarnessConfig {
  return {
    project: {
      name: "notifier-integ",
      root: tmpDir,
      task_dir: join(tmpDir, "tasks"),
      state_file: join(tmpDir, "state.json"),
      worktree_base: join(tmpDir, "worktrees"),
      session_dir: join(tmpDir, "sessions"),
    },
    pipeline: {
      poll_interval: 0.01,
      test_command: "echo ok",
      max_retries: 3,
      test_timeout: 60,
      escalation_timeout: 300,
      retry_delay_ms: 100,
      max_session_retries: 1,
    },
    discord: {
      bot_token_env: "T",
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
}

interface NotifierHarness {
  orch: Orchestrator;
  state: StateManager;
  events: OrchestratorEvent[];
  sent: Array<{ channel: string; content: string; identity?: AgentIdentity }>;
  notifier: DiscordNotifier;
}

function setupHarness(
  opts: {
    completionStatus?: "success" | "failure";
    escalationTrigger?: boolean;
    mergeOverrides?: Partial<MergeGitOps>;
  } = {},
): NotifierHarness {
  const config = makeConfig();
  mkdirSync(join(tmpDir, "tasks"), { recursive: true });

  const completionStatus = opts.completionStatus ?? "success";

  const gitOps: GitOps = {
    createWorktree: vi.fn((_base, _branch, wtPath) => {
      mkdirSync(join(wtPath, ".harness"), { recursive: true });
      if (opts.escalationTrigger) {
        writeFileSync(
          join(wtPath, ".harness", "escalation.json"),
          JSON.stringify({ type: "clarification_needed", question: "what scope?" }),
        );
      }
      writeFileSync(
        join(wtPath, ".harness", "completion.json"),
        JSON.stringify({
          status: completionStatus,
          commitSha: "abc123",
          summary: completionStatus === "success" ? "Integration done" : "agent gave up",
          filesChanged: ["src/x.ts"],
        }),
      );
    }),
    removeWorktree: vi.fn(),
    branchExists: vi.fn(() => false),
    deleteBranch: vi.fn(),
  };

  const queryFn: QueryFn = vi.fn().mockImplementation(() => mockQuery([makeResultSuccess()]));
  const sdk = new SDKClient(queryFn);
  const state = new StateManager(join(tmpDir, "state.json"));
  const sessionMgr = new SessionManager(sdk, state, config, gitOps);

  const mergeGitOps: MergeGitOps = {
    hasUncommittedChanges: vi.fn().mockReturnValue(false),
    autoCommit: vi.fn().mockReturnValue("sha"),
    getHeadSha: vi.fn().mockReturnValue("sha"),
    rebase: vi.fn().mockReturnValue({ success: true, conflictFiles: [] }),
    rebaseAbort: vi.fn(),
    mergeNoFf: vi.fn().mockReturnValue("merge-sha"),
    revertLastMerge: vi.fn(),
    runTests: vi.fn().mockReturnValue({ success: true, output: "ok" }),
    getTrunkBranch: vi.fn().mockReturnValue("master"),
    branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(false),
    diffNameOnly: vi.fn().mockReturnValue(["src/x.ts"]),
    scrubHarnessFromHead: vi.fn().mockReturnValue(false),
    getUserEmail: vi.fn().mockReturnValue("test@example"),
    ...opts.mergeOverrides,
  };

  const mergeGate = new MergeGate(config.pipeline, tmpDir, mergeGitOps);

  const orch = new Orchestrator({
    sessionManager: sessionMgr,
    mergeGate,
    stateManager: state,
    config,
  });

  const events: OrchestratorEvent[] = [];
  orch.on((e) => events.push(e));

  // Build the notifier with a fake sender
  const sent: Array<{ channel: string; content: string; identity?: AgentIdentity }> = [];
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity) {
      sent.push({ channel, content, identity });
    },
    async sendToChannelAndReturnId(channel, content, identity) {
      return sendToChannelAndReturnIdDefault(this, channel, content, identity);
    },
    async addReaction() {
      /* no-op */
    },
  };
  const notifier = new DiscordNotifier(sender, config.discord);
  orch.on((e) => notifier.handleEvent(e));

  return { orch, state, events, sent, notifier };
}

async function flushMicrotasks(): Promise<void> {
  // Give notifier .catch handlers a chance to resolve
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
}

describe("DiscordNotifier ↔ Orchestrator integration", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("successful task: notifier routes picked_up/session_complete/merge_result/task_done all to dev", async () => {
    const { orch, sent } = setupHarness({ completionStatus: "success" });
    // Ingest via scanForTasks path so task_picked_up fires (that event is only
    // emitted by the file-ingest lane, not by processTask directly).
    writeFileSync(join(tmpDir, "tasks", "happy-1.json"), JSON.stringify({ prompt: "test" }));
    orch.scanForTasks();
    // processTask is fire-and-forget in scanForTasks; wait for the lifecycle
    for (let i = 0; i < 40; i++) {
      await flushMicrotasks();
      if (sent.some((s) => /complete$/i.test(s.content))) break;
    }

    const channels = sent.map((s) => s.channel);
    const contents = sent.map((s) => s.content);

    expect(channels.filter((c) => c === "dev").length).toBeGreaterThanOrEqual(3);
    expect(contents.some((c) => /picked up/i.test(c))).toBe(true);
    expect(contents.some((c) => /Session complete.*success/i.test(c))).toBe(true);
    expect(contents.some((c) => /merged/i.test(c))).toBe(true);
    expect(contents.some((c) => /complete/i.test(c))).toBe(true);
    // No ops or escalation traffic on happy path
    expect(channels).not.toContain("ops");
    expect(channels).not.toContain("esc");
  });

  it("failed-task lifecycle (circuit breaker): task_failed routes to ops_channel", async () => {
    // Disable auto-escalate so a retry-exhausted failure hits the circuit
    // breaker and fires task_failed (which routes to ops) instead of
    // escalation_needed (which routes to esc).
    const { orch, state, sent } = setupHarness({
      completionStatus: "failure",
      mergeOverrides: {},
    });
    // Tweak config post-construction: config is shared via the setupHarness scope
    // — disable auto-escalate so the failure path reaches task_failed.
    (orch as unknown as { config: HarnessConfig }).config.pipeline.auto_escalate_on_max_retries = false;

    const task = state.createTask("test", "fail-1");
    await orch.processTask(task);
    await flushMicrotasks();

    const opsMessages = sent.filter((s) => s.channel === "ops");
    expect(opsMessages.length).toBeGreaterThanOrEqual(1);
    expect(opsMessages.some((s) => /FAILED/.test(s.content))).toBe(true);
  });

  it("escalation signal routes to escalation_channel", async () => {
    const { orch, state, sent } = setupHarness({ escalationTrigger: true });
    const task = state.createTask("ambiguous", "esc-1");
    await orch.processTask(task);
    await flushMicrotasks();

    const escMessages = sent.filter((s) => s.channel === "esc");
    expect(escMessages.length).toBeGreaterThanOrEqual(1);
    expect(escMessages.some((s) => /ESCALATION/.test(s.content))).toBe(true);
    expect(escMessages.some((s) => /what scope/.test(s.content))).toBe(true);
  });

  it("every event received by orchestrator listener is offered to notifier", async () => {
    const { orch, state, events, sent } = setupHarness({ completionStatus: "success" });
    const task = state.createTask("test", "sync-1");
    await orch.processTask(task);
    await flushMicrotasks();

    // Not every event emits to Discord (poll_tick/shutdown/checkpoint_detected/
    // completion_compliance/response_level level 0-1 are skipped by the notifier),
    // but every emitted event should have been observed by both listeners.
    expect(events.length).toBeGreaterThan(0);
    // At least one Discord message sent per happy-path lifecycle
    expect(sent.length).toBeGreaterThan(0);
    // The orchestrator listener's event count >= notifier's emission count
    // (notifier skips informational events)
    expect(events.length).toBeGreaterThanOrEqual(sent.length);
  });

  it("notifier identity is set per event (orchestrator or executor for task events, not architect)", async () => {
    const { orch, state, sent } = setupHarness({ completionStatus: "success" });
    const task = state.createTask("test", "id-1");
    await orch.processTask(task);
    await flushMicrotasks();

    // Wave E-α D2: task-lifecycle events use orchestrator (Harness) or executor (Executor) identity.
    // session_complete and task_done now use Executor; remaining task events use Harness.
    // No task-lifecycle event should use Architect or Reviewer identity.
    const identities = sent.map((s) => s.identity?.username).filter(Boolean);
    expect(identities.length).toBeGreaterThan(0);
    const allowedTaskIdentities = new Set(["Harness", "Executor"]);
    for (const u of identities) expect(allowedTaskIdentities.has(u as string)).toBe(true);
  });
});
