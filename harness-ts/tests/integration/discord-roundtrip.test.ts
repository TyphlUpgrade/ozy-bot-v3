/**
 * Integration — Wave 2 + Wave 3 Discord surface end-to-end.
 *
 * Wires the full inbound+outbound stack (MessageAccumulator → CommandRouter →
 * TaskSink / ProjectStore / Orchestrator → DiscordNotifier → fake sender)
 * and drives representative operator flows through it. Asserts that:
 *   - NL "!task" via accumulator creates a task file, orchestrator picks it up
 *     through scanForTasks, and notifier emits the expected Discord messages.
 *   - `!project` declaration → project_declared event routes to dev channel
 *     with architect identity.
 *   - `!project <id> abort confirm` → projectStore state flips + project_aborted
 *     routes to ops channel.
 *   - `!status` round-trips through router.
 *   - Split NL messages get concatenated by accumulator before router sees them.
 *
 * All SDK + git operations are mocked so this runs in CI without API cost.
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
import { ProjectStore } from "../../src/lib/project.js";
import type { HarnessConfig } from "../../src/lib/config.js";
import { DiscordNotifier } from "../../src/discord/notifier.js";
import { sendToChannelAndReturnIdDefault, type DiscordSender, type AgentIdentity } from "../../src/discord/types.js";
import {
  CommandRouter,
  FileTaskSink,
  UnknownIntentClassifier,
} from "../../src/discord/commands.js";
import { MessageAccumulator } from "../../src/discord/accumulator.js";
import type { Query, SDKMessage, SDKResultSuccess } from "@anthropic-ai/claude-agent-sdk";

// --- Shared infra ---

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-roundtrip-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

function makeResultSuccess(sessionId = "session-roundtrip"): SDKResultSuccess {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 500,
    duration_api_ms: 400,
    is_error: false,
    num_turns: 1,
    result: "Done",
    stop_reason: "end_turn",
    total_cost_usd: 0.01,
    usage: { input_tokens: 50, output_tokens: 25 },
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

function buildConfig(root: string): HarnessConfig {
  return {
    project: {
      name: "roundtrip",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "worktrees"),
      session_dir: join(root, "sessions"),
    },
    pipeline: {
      poll_interval: 0.01,
      test_command: "echo ok",
      max_retries: 2,
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

interface RoundtripHarness {
  orch: Orchestrator;
  state: StateManager;
  projectStore: ProjectStore;
  router: CommandRouter;
  accumulator: MessageAccumulator;
  sent: Array<{ channel: string; content: string; identity?: AgentIdentity }>;
  events: OrchestratorEvent[];
  flushCalls: Array<{ userId: string; channelId: string; text: string }>;
}

function setup(): RoundtripHarness {
  const config = buildConfig(tmpDir);
  mkdirSync(config.project.task_dir, { recursive: true });

  // git + SDK mocks so orchestrator runs the happy path
  const gitOps: GitOps = {
    createWorktree: vi.fn((_base, _branch, wtPath) => {
      mkdirSync(join(wtPath, ".harness"), { recursive: true });
      writeFileSync(
        join(wtPath, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "sha-rt",
          summary: "roundtrip done",
          filesChanged: ["hello.ts"],
        }),
      );
    }),
    removeWorktree: vi.fn(),
    branchExists: vi.fn(() => false),
    deleteBranch: vi.fn(),
  };
  const queryFn: QueryFn = vi.fn().mockImplementation(() => mockQuery([makeResultSuccess()]));
  const sdk = new SDKClient(queryFn);
  const state = new StateManager(config.project.state_file);
  const sessions = new SessionManager(sdk, state, config, gitOps);
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
  };
  const mergeGate = new MergeGate(config.pipeline, tmpDir, mergeGitOps);
  const orch = new Orchestrator({ sessionManager: sessions, mergeGate, stateManager: state, config });

  // Discord outbound
  const sent: Array<{ channel: string; content: string; identity?: AgentIdentity }> = [];
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity) {
      sent.push({ channel, content, identity });
    },
    async sendToChannelAndReturnId(channel, content, identity) {
      return sendToChannelAndReturnIdDefault(this, channel, content, identity);
    },
    async addReaction() { /* noop */ },
  };
  const notifier = new DiscordNotifier(sender, config.discord);
  const events: OrchestratorEvent[] = [];
  orch.on((e) => events.push(e));
  orch.on((e) => notifier.handleEvent(e));

  // Discord inbound
  const projectStore = new ProjectStore(join(tmpDir, "projects.json"), config.project.worktree_base);
  const router = new CommandRouter({
    state,
    config,
    classifier: new UnknownIntentClassifier(),
    projectStore,
    abort: { abortTask: () => undefined },
    taskSink: new FileTaskSink(config.project.task_dir),
    emit: (e) => { events.push(e); notifier.handleEvent(e); },
  });

  const flushCalls: Array<{ userId: string; channelId: string; text: string }> = [];
  const accumulator = new MessageAccumulator(
    (userId, channelId, text) => {
      flushCalls.push({ userId, channelId, text });
      if (text.startsWith("!")) {
        // Strip leading "!" and first word as command; rest is args.
        const after = text.slice(1);
        const spaceIdx = after.indexOf(" ");
        const cmd = spaceIdx < 0 ? after : after.slice(0, spaceIdx);
        const args = spaceIdx < 0 ? "" : after.slice(spaceIdx + 1);
        void router.handleCommand(cmd, args, channelId);
      } else {
        void router.handleNaturalLanguage(text, channelId, userId);
      }
    },
    { debounceMs: 50 },
  );

  return { orch, state, projectStore, router, accumulator, sent, events, flushCalls };
}

async function flushMicrotasks(ms = 0): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
}

describe("Discord roundtrip — Wave 2 + Wave 3 end-to-end", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("!task via accumulator → file ingest → session → notifier flow (full lifecycle)", async () => {
    const h = setup();

    // Operator types !task (commands bypass debounce, flush immediately)
    h.accumulator.push("op-1", "dev", "!task add input validation");
    // Router wrote file; drive orchestrator to ingest
    h.orch.scanForTasks();
    // Wait for lifecycle (fire-and-forget processTask)
    for (let i = 0; i < 40; i++) {
      await flushMicrotasks();
      if (h.sent.some((s) => /complete/i.test(s.content))) break;
    }

    // Router-created task went through pipeline
    expect(h.flushCalls).toContainEqual({ userId: "op-1", channelId: "dev", text: "!task add input validation" });
    expect(h.events.some((e) => e.type === "task_picked_up")).toBe(true);
    expect(h.events.some((e) => e.type === "task_done")).toBe(true);

    // Notifier routed the lifecycle to dev
    expect(h.sent.some((s) => s.channel === "dev" && /picked up/.test(s.content))).toBe(true);
    expect(h.sent.some((s) => s.channel === "dev" && /merged/.test(s.content))).toBe(true);
    expect(h.sent.some((s) => s.channel === "dev" && /complete/.test(s.content))).toBe(true);
  });

  it("!project declare emits project_declared → notifier routes to dev with architect identity", async () => {
    const h = setup();
    const args =
      "auth-rewrite\nReplace legacy auth with JWT\nNON-GOALS:\n- no UI changes\n- no DB migration";
    await h.router.handleCommand("project", args, "dev");
    await flushMicrotasks();

    expect(h.projectStore.getAllProjects()).toHaveLength(1);
    const declared = h.sent.find((s) => /declared/.test(s.content));
    expect(declared).toBeTruthy();
    expect(declared!.channel).toBe("dev");
    expect(declared!.identity?.username).toBe("Architect");
  });

  it("!project <id> abort confirm → project_aborted → notifier routes to ops", async () => {
    const h = setup();
    const p = h.projectStore.createProject("short-lived", "desc", ["nothing"]);
    const msg = await h.router.handleCommand("project", `${p.id} abort confirm`, "dev");
    await flushMicrotasks();

    expect(msg).toMatch(/aborted/);
    expect(h.projectStore.getProject(p.id)!.state).toBe("aborted");
    const aborted = h.sent.find((s) => /aborted/.test(s.content));
    expect(aborted).toBeTruthy();
    expect(aborted!.channel).toBe("ops");
  });

  it("!status lists projects + tasks (inbound only, no outbound triggered)", async () => {
    const h = setup();
    h.projectStore.createProject("proj-live", "desc", []);
    h.state.createTask("a prompt", "task-live");
    const reply = await h.router.handleCommand("status", "", "dev");

    expect(reply).toMatch(/Projects:/);
    expect(reply).toMatch(/proj-live/);
    expect(reply).toMatch(/Tasks:/);
    expect(reply).toMatch(/task-live/);
    // Status is a synchronous router response; no orchestrator events fire → no sender traffic
    expect(h.sent).toHaveLength(0);
  });

  it("split NL messages are concatenated by accumulator before router classifies", async () => {
    const h = setup();
    // Two halves of a "create ..." NL task, within debounce window
    h.accumulator.push("op-1", "dev", "create");
    h.accumulator.push("op-1", "dev", "a user profile page");

    // Fire debounce window
    await new Promise((r) => setTimeout(r, 100));
    await flushMicrotasks();

    expect(h.flushCalls).toContainEqual({ userId: "op-1", channelId: "dev", text: "create a user profile page" });
    // Router saw the combined text, recognized "create" → created a task file
    h.orch.scanForTasks();
    await flushMicrotasks();
    expect(h.events.some((e) => e.type === "task_picked_up")).toBe(true);
  });
});
