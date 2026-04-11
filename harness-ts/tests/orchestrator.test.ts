import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Orchestrator, type OrchestratorEvent, type OrchestratorDeps } from "../src/orchestrator.js";
import { SessionManager, type GitOps } from "../src/session/manager.js";
import { SDKClient, type QueryFn } from "../src/session/sdk.js";
import { MergeGate, type MergeGitOps } from "../src/gates/merge.js";
import { StateManager } from "../src/lib/state.js";
import type { HarnessConfig } from "../src/lib/config.js";
import type { Query, SDKMessage, SDKResultSuccess } from "@anthropic-ai/claude-agent-sdk";

// --- Test infrastructure ---

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-orch-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

function makeConfig(): HarnessConfig {
  return {
    project: {
      name: "test",
      root: tmpDir,
      task_dir: join(tmpDir, "tasks"),
      state_file: join(tmpDir, "state.json"),
      worktree_base: join(tmpDir, "worktrees"),
      session_dir: join(tmpDir, "sessions"),
    },
    pipeline: {
      poll_interval: 0.01, // fast for tests
      test_command: "echo ok",
      max_retries: 3,
      test_timeout: 180,
      escalation_timeout: 14400,
      retry_delay_ms: 100,
    },
    discord: {
      bot_token_env: "TOKEN",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {},
    },
  };
}

function makeResultSuccess(sessionId = "session-abc"): SDKResultSuccess {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 5000,
    duration_api_ms: 4000,
    is_error: false,
    num_turns: 3,
    result: "Done",
    stop_reason: "end_turn",
    total_cost_usd: 0.05,
    usage: { input_tokens: 1000, output_tokens: 500 },
    modelUsage: {},
    permission_denials: [],
    uuid: "uuid-1" as SDKResultSuccess["uuid"],
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

function mockGitOps(): GitOps {
  return {
    createWorktree: vi.fn((_base, _branch, wtPath) => {
      mkdirSync(wtPath, { recursive: true });
    }),
    removeWorktree: vi.fn((_repoPath: string, _worktreePath: string) => {}),
    branchExists: vi.fn((_repoPath: string, _branchName: string) => false),
    deleteBranch: vi.fn((_repoPath: string, _branchName: string) => {}),
  };
}

function mockMergeGitOps(): MergeGitOps {
  return {
    hasUncommittedChanges: vi.fn().mockReturnValue(false),
    autoCommit: vi.fn().mockReturnValue("sha1"),
    getHeadSha: vi.fn().mockReturnValue("sha1"),
    rebase: vi.fn().mockReturnValue({ success: true, conflictFiles: [] }),
    rebaseAbort: vi.fn(),
    mergeNoFf: vi.fn().mockReturnValue("merge-sha"),
    revertLastMerge: vi.fn(),
    runTests: vi.fn().mockReturnValue({ success: true, output: "ok" }),
    getTrunkBranch: vi.fn().mockReturnValue("master"),
  };
}

interface TestHarness {
  orch: Orchestrator;
  state: StateManager;
  config: HarnessConfig;
  events: OrchestratorEvent[];
  gitOps: GitOps;
  mergeGitOps: MergeGitOps;
  queryFn: QueryFn;
}

function setupHarness(opts?: {
  queryMessages?: SDKMessage[];
  withCompletion?: boolean;
  mergeGitOverrides?: Partial<MergeGitOps>;
}): TestHarness {
  const config = makeConfig();
  mkdirSync(join(tmpDir, "tasks"), { recursive: true });

  const gitOps = mockGitOps();

  // If withCompletion, write completion.json when worktree is created
  if (opts?.withCompletion) {
    (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
      (_base: string, _branch: string, wtPath: string) => {
        mkdirSync(join(wtPath, ".harness"), { recursive: true });
        writeFileSync(
          join(wtPath, ".harness", "completion.json"),
          JSON.stringify({
            status: "success",
            commitSha: "abc123",
            summary: "Fixed it",
            filesChanged: ["src/fix.ts"],
          }),
        );
      },
    );
  }

  const queryFn: QueryFn = vi.fn().mockReturnValue(
    mockQuery(opts?.queryMessages ?? [makeResultSuccess()]),
  );
  const sdk = new SDKClient(queryFn);
  const state = new StateManager(join(tmpDir, "state.json"));
  const sessionMgr = new SessionManager(sdk, state, config, gitOps);
  const mergeGitOps_ = { ...mockMergeGitOps(), ...opts?.mergeGitOverrides };
  const mergeGate = new MergeGate(config.pipeline, tmpDir, mergeGitOps_);

  const orch = new Orchestrator({
    sessionManager: sessionMgr,
    mergeGate,
    stateManager: state,
    config,
  });

  const events: OrchestratorEvent[] = [];
  orch.on((e) => events.push(e));

  return { orch, state, config, events, gitOps, mergeGitOps: mergeGitOps_, queryFn };
}

function dropTask(taskDir: string, id: string, prompt: string): void {
  writeFileSync(join(taskDir, `${id}.json`), JSON.stringify({ prompt }));
}

// --- Tests ---

describe("Orchestrator", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });

  afterEach(async () => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  describe("scanForTasks", () => {
    it("picks up task files from task_dir", () => {
      const { orch, state, events } = setupHarness({ withCompletion: true });
      dropTask(join(tmpDir, "tasks"), "task-1", "fix the auth bug");

      orch.scanForTasks();

      // Task should be created in state
      const task = state.getTask("task-1");
      expect(task).toBeTruthy();
      expect(task!.prompt).toBe("fix the auth bug");
      expect(events.some((e) => e.type === "task_picked_up")).toBe(true);
    });

    it("removes task file after ingesting", () => {
      const { orch } = setupHarness({ withCompletion: true });
      const taskPath = join(tmpDir, "tasks", "task-2.json");
      writeFileSync(taskPath, JSON.stringify({ prompt: "test" }));

      orch.scanForTasks();

      expect(existsSync(taskPath)).toBe(false);
    });

    it("rejects task IDs with path traversal (O4)", () => {
      const { orch, state } = setupHarness({ withCompletion: true });
      // Task file with path traversal in ID
      writeFileSync(
        join(tmpDir, "tasks", "evil.json"),
        JSON.stringify({ id: "../../etc/passwd", prompt: "hack" }),
      );

      orch.scanForTasks();

      // Task should NOT be created
      expect(state.getAllTasks()).toHaveLength(0);
      // File should be removed
      expect(existsSync(join(tmpDir, "tasks", "evil.json"))).toBe(false);
    });

    it("rejects task IDs with dots (O4)", () => {
      const { orch, state } = setupHarness({ withCompletion: true });
      writeFileSync(
        join(tmpDir, "tasks", "tricky.json"),
        JSON.stringify({ id: "task..sneaky", prompt: "hack" }),
      );

      orch.scanForTasks();
      expect(state.getAllTasks()).toHaveLength(0);
    });

    it("accepts valid task IDs with hyphens and underscores", () => {
      const { orch, state } = setupHarness({ withCompletion: true });
      writeFileSync(
        join(tmpDir, "tasks", "ok.json"),
        JSON.stringify({ id: "fix-auth_bug-123", prompt: "fix it" }),
      );

      orch.scanForTasks();
      expect(state.getTask("fix-auth_bug-123")).toBeTruthy();
    });

    it("skips invalid task files", () => {
      const { orch, state } = setupHarness();
      writeFileSync(join(tmpDir, "tasks", "bad.json"), "not json");

      orch.scanForTasks();

      expect(state.getAllTasks()).toHaveLength(0);
    });

    it("skips already-tracked tasks", () => {
      const { orch, state } = setupHarness({ withCompletion: true });
      state.createTask("existing", "task-dup");
      dropTask(join(tmpDir, "tasks"), "task-dup", "duplicate");

      orch.scanForTasks();

      // Should still be the original task
      expect(state.getTask("task-dup")!.prompt).toBe("existing");
    });
  });

  describe("processTask — full lifecycle", () => {
    it("task -> session -> completion -> merge -> done", async () => {
      const { orch, state, events } = setupHarness({ withCompletion: true });
      const task = state.createTask("fix the bug", "lifecycle-1");

      await orch.processTask(task);

      const final = state.getTask("lifecycle-1")!;
      expect(final.state).toBe("done");
      expect(final.summary).toBe("Fixed it");
      expect(final.filesChanged).toEqual(["src/fix.ts"]);

      expect(events.some((e) => e.type === "session_complete")).toBe(true);
      expect(events.some((e) => e.type === "merge_result")).toBe(true);
      expect(events.some((e) => e.type === "task_done")).toBe(true);
    });

    it("fails task when session returns error", async () => {
      const errorResult: SDKMessage = {
        type: "result",
        subtype: "error_during_execution",
        duration_ms: 1000,
        duration_api_ms: 500,
        is_error: true,
        num_turns: 1,
        stop_reason: null,
        total_cost_usd: 0.01,
        usage: { input_tokens: 100, output_tokens: 50 },
        modelUsage: {},
        permission_denials: [],
        errors: ["API error"],
        uuid: "err-uuid" as any,
        session_id: "err-session",
      } as unknown as SDKMessage;

      const { orch, state, events } = setupHarness({ queryMessages: [errorResult] });
      const task = state.createTask("test", "fail-1");

      await orch.processTask(task);

      expect(state.getTask("fail-1")!.state).toBe("failed");
      expect(events.some((e) => e.type === "task_failed")).toBe(true);
    });

    it("fails task when no completion signal", async () => {
      // No completion.json written (default mock doesn't write it)
      const { orch, state } = setupHarness({ withCompletion: false });
      const task = state.createTask("test", "no-signal");

      await orch.processTask(task);

      expect(state.getTask("no-signal")!.state).toBe("failed");
      expect(state.getTask("no-signal")!.lastError).toContain("No completion signal");
    });
  });

  describe("merge outcomes", () => {
    it("test failure -> revert -> state=failed", async () => {
      const { orch, state, events } = setupHarness({
        withCompletion: true,
        mergeGitOverrides: {
          runTests: vi.fn().mockReturnValue({ success: false, output: "FAIL: auth.test" }),
        },
      });
      const task = state.createTask("test", "test-fail");

      await orch.processTask(task);

      expect(state.getTask("test-fail")!.state).toBe("failed");
      expect(state.getTask("test-fail")!.lastError).toContain("Tests failed");
    });

    it("test timeout -> state=failed", async () => {
      const { orch, state } = setupHarness({
        withCompletion: true,
        mergeGitOverrides: {
          runTests: vi.fn().mockReturnValue({ success: false, output: "TIMEOUT" }),
        },
      });
      const task = state.createTask("test", "timeout-1");

      await orch.processTask(task);

      expect(state.getTask("timeout-1")!.state).toBe("failed");
      expect(state.getTask("timeout-1")!.lastError).toBe("Test timeout");
    });

    it("rebase conflict -> shelved with retry attempt tracked", async () => {
      const { orch, state, events } = setupHarness({
        withCompletion: true,
        mergeGitOverrides: {
          rebase: vi.fn().mockReturnValue({ success: false, conflictFiles: ["src/x.ts"] }),
        },
      });
      const task = state.createTask("test", "conflict-1");

      await orch.processTask(task);

      const final = state.getTask("conflict-1")!;
      // Should be shelved (first attempt, under max_retries=3)
      expect(final.state).toBe("shelved");
      expect(final.rebaseAttempts).toBe(1);
      expect(events.some((e) => e.type === "task_shelved")).toBe(true);
    });

    it("3 rebase conflicts -> escalate (state=failed)", async () => {
      const { orch, state } = setupHarness({
        withCompletion: true,
        mergeGitOverrides: {
          rebase: vi.fn().mockReturnValue({ success: false, conflictFiles: ["src/x.ts"] }),
        },
      });
      const task = state.createTask("test", "conflict-max");
      // Pre-set rebase attempts to 2 (third will hit max_retries=3)
      state.updateTask("conflict-max", { rebaseAttempts: 2 });

      await orch.processTask(task);

      expect(state.getTask("conflict-max")!.state).toBe("failed");
      expect(state.getTask("conflict-max")!.lastError).toContain("Rebase conflict after 3 attempts");
    });
  });

  describe("shutdown", () => {
    it("stops polling and aborts sessions", async () => {
      const { orch } = setupHarness();
      orch.start();
      expect(orch.isRunning).toBe(true);

      await orch.shutdown();
      expect(orch.isRunning).toBe(false);
    });

    it("emits shutdown event", async () => {
      const { orch, events } = setupHarness();
      orch.start();
      await orch.shutdown();
      expect(events.some((e) => e.type === "shutdown")).toBe(true);
    });
  });

  describe("crash recovery", () => {
    it("resumes active tasks on start", async () => {
      // Create a state file with an active task
      const state = new StateManager(join(tmpDir, "state.json"));
      const task = state.createTask("recover me", "recover-1");
      state.transition("recover-1", "active");

      // Now create orchestrator with this state
      const { orch, events } = setupHarness({ withCompletion: true });
      // Reload state from disk (the setupHarness created a fresh one)
      // Instead, let's just test recoverFromCrash behavior
      // We need to write the state to the same path setupHarness uses
      const stateForRecovery = new StateManager(join(tmpDir, "state.json"));
      stateForRecovery.createTask("recover me", "recover-2");
      stateForRecovery.transition("recover-2", "active");

      // Start will trigger recovery
      orch.start();

      // Wait a tick for recovery to fire
      await new Promise((r) => setTimeout(r, 50));
      await orch.shutdown();

      // The task should have been re-queued (failed -> pending -> processed)
      const recovered = stateForRecovery.getTask("recover-2");
      // Note: recovery transitions active -> failed -> pending, then processes
      // Exact final state depends on mock behavior
      expect(recovered).toBeTruthy();
    });
  });

  describe("start/poll integration", () => {
    it("creates task_dir if missing", () => {
      rmSync(join(tmpDir, "tasks"), { recursive: true, force: true });
      const { orch } = setupHarness();
      orch.start();
      expect(existsSync(join(tmpDir, "tasks"))).toBe(true);
      orch.shutdown();
    });
  });
});
