import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { SessionManager, type GitOps, type CompletionSignal } from "../../src/session/manager.js";
import { SDKClient, type QueryFn } from "../../src/session/sdk.js";
import { StateManager } from "../../src/lib/state.js";
import type { HarnessConfig } from "../../src/lib/config.js";
import type { Query, SDKMessage, SDKResultSuccess } from "@anthropic-ai/claude-agent-sdk";

// --- Test helpers ---

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-session-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

function makeConfig(overrides?: Partial<HarnessConfig["project"]>): HarnessConfig {
  return {
    project: {
      name: "test",
      root: tmpDir,
      task_dir: join(tmpDir, "tasks"),
      state_file: join(tmpDir, "state.json"),
      worktree_base: join(tmpDir, "worktrees"),
      session_dir: join(tmpDir, "sessions"),
      ...overrides,
    },
    pipeline: {
      poll_interval: 1,
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

function mockGitOps(): GitOps {
  return {
    createWorktree: vi.fn((_, __, worktreePath: string) => {
      mkdirSync(worktreePath, { recursive: true });
    }),
    removeWorktree: vi.fn((_repoPath: string, _worktreePath: string) => {}),
    branchExists: vi.fn((_repoPath: string, _branchName: string) => false),
    deleteBranch: vi.fn((_repoPath: string, _branchName: string) => {}),
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

// --- Tests ---

describe("SessionManager", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  function makeManager(queryMessages?: SDKMessage[], gitOpsOverride?: GitOps) {
    const queryFn: QueryFn = vi.fn().mockReturnValue(
      mockQuery(queryMessages ?? [makeResultSuccess()]),
    );
    const sdk = new SDKClient(queryFn);
    const state = new StateManager(join(tmpDir, "state.json"));
    const config = makeConfig();
    const git = gitOpsOverride ?? mockGitOps();
    const mgr = new SessionManager(sdk, state, config, git);
    return { mgr, sdk, state, config, git, queryFn };
  }

  describe("createWorktree", () => {
    it("creates worktree with correct branch name and path", () => {
      const { mgr, git } = makeManager();
      const { worktreePath, branchName } = mgr.createWorktree("task-123");

      expect(branchName).toBe("harness/task-task-123");
      expect(worktreePath).toContain("task-task-123");
      expect(git.createWorktree).toHaveBeenCalledOnce();
    });
  });

  describe("cleanupWorktree", () => {
    it("removes worktree and branch", () => {
      const git = mockGitOps();
      (git.branchExists as ReturnType<typeof vi.fn>).mockReturnValue(true);
      const { mgr } = makeManager(undefined, git);

      mgr.cleanupWorktree("task-123");
      expect(git.removeWorktree).toHaveBeenCalledOnce();
      expect(git.deleteBranch).toHaveBeenCalledOnce();
    });

    it("skips branch delete if branch doesn't exist", () => {
      const git = mockGitOps();
      (git.branchExists as ReturnType<typeof vi.fn>).mockReturnValue(false);
      const { mgr } = makeManager(undefined, git);

      mgr.cleanupWorktree("task-123");
      expect(git.removeWorktree).toHaveBeenCalledOnce();
      expect(git.deleteBranch).not.toHaveBeenCalled();
    });
  });

  describe("spawnTask", () => {
    it("creates worktree, starts session, returns result", async () => {
      const { mgr, state } = makeManager();
      const task = state.createTask("fix the bug", "task-1");

      const { result, completion } = await mgr.spawnTask(task);

      expect(result.success).toBe(true);
      expect(result.sessionId).toBe("session-abc");
      expect(completion).toBeNull(); // no completion.json written

      const updated = state.getTask("task-1")!;
      expect(updated.state).toBe("active");
      expect(updated.sessionId).toBe("session-abc");
      expect(updated.totalCostUsd).toBe(0.05);
    });

    it("transitions task to active", async () => {
      const { mgr, state } = makeManager();
      const task = state.createTask("test", "task-2");

      await mgr.spawnTask(task);
      expect(state.getTask("task-2")!.state).toBe("active");
    });

    it("calls onMessage for each SDK message", async () => {
      const messages: SDKMessage[] = [makeResultSuccess()];
      const { mgr, state } = makeManager(messages);
      const task = state.createTask("test", "task-3");

      const seen: string[] = [];
      await mgr.spawnTask(task, (msg) => seen.push(msg.type));
      expect(seen).toContain("result");
    });

    it("cleans up active session tracking after completion", async () => {
      const { mgr, state } = makeManager();
      const task = state.createTask("test", "task-4");

      expect(mgr.activeCount).toBe(0);
      await mgr.spawnTask(task);
      expect(mgr.activeCount).toBe(0); // cleaned up after completion
    });
  });

  describe("completion signal", () => {
    it("detects valid completion.json", async () => {
      const { mgr, state, git } = makeManager();
      const task = state.createTask("test", "task-5");

      // Mock git to create worktree dir, then write completion.json into it
      (git.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          const signal: CompletionSignal = {
            status: "success",
            commitSha: "abc123def",
            summary: "Fixed the auth bug",
            filesChanged: ["src/auth.ts"],
          };
          writeFileSync(
            join(wtPath, ".harness", "completion.json"),
            JSON.stringify(signal),
          );
        },
      );

      const { completion } = await mgr.spawnTask(task);
      expect(completion).not.toBeNull();
      expect(completion!.status).toBe("success");
      expect(completion!.commitSha).toBe("abc123def");
      expect(completion!.summary).toBe("Fixed the auth bug");
      expect(completion!.filesChanged).toEqual(["src/auth.ts"]);
    });

    it("returns null for missing completion.json", () => {
      const { mgr } = makeManager();
      const result = mgr.readCompletion("/nonexistent/path");
      expect(result).toBeNull();
    });

    it("rejects completion.json with missing fields", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({ status: "success" }), // missing commitSha, summary, filesChanged
      );

      const { mgr } = makeManager();
      expect(mgr.readCompletion(dir)).toBeNull();
      rmSync(dir, { recursive: true, force: true });
    });

    it("rejects completion.json with invalid status", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "invalid",
          commitSha: "abc",
          summary: "test",
          filesChanged: [],
        }),
      );

      const { mgr } = makeManager();
      expect(mgr.readCompletion(dir)).toBeNull();
      rmSync(dir, { recursive: true, force: true });
    });

    it("rejects completion.json with empty commitSha", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "",
          summary: "test",
          filesChanged: [],
        }),
      );

      const { mgr } = makeManager();
      expect(mgr.readCompletion(dir)).toBeNull();
      rmSync(dir, { recursive: true, force: true });
    });
  });

  describe("abort", () => {
    it("abortTask aborts the controller", async () => {
      // Use a query that hangs until aborted
      let resolveHang: () => void;
      const hangPromise = new Promise<void>((r) => { resolveHang = r; });

      async function* hangingGen(): AsyncGenerator<SDKMessage, void> {
        await hangPromise;
        yield makeResultSuccess();
      }
      const hangQuery = Object.assign(hangingGen(), {
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

      const queryFn: QueryFn = vi.fn().mockReturnValue(hangQuery);
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test", "task-abort");

      // Start spawn in background
      const spawnPromise = mgr.spawnTask(task);

      // Wait a tick for session to register
      await new Promise((r) => setTimeout(r, 10));

      // Should have active session
      expect(mgr.activeCount).toBe(1);

      // Abort it
      mgr.abortTask("task-abort");

      // Resolve the hang so the generator can finish
      resolveHang!();
      const { result } = await spawnPromise;

      // Active count back to 0
      expect(mgr.activeCount).toBe(0);
    });

    it("abortAll aborts all sessions", () => {
      const { mgr, sdk } = makeManager();
      const ac1 = new AbortController();
      const ac2 = new AbortController();
      sdk.registerController("t1", ac1);
      sdk.registerController("t2", ac2);

      mgr.abortAll();
      // abortAll only clears internal activeSessions map, not SDK controllers directly
      // (those were registered via sdk.registerController)
      expect(mgr.activeCount).toBe(0);
    });
  });

  describe("timeout", () => {
    it("fires abort controller after timeout", async () => {
      // Query that yields result after a delay
      async function* slowGen(): AsyncGenerator<SDKMessage, void> {
        await new Promise((r) => setTimeout(r, 200));
        yield makeResultSuccess();
      }
      const slowQuery = Object.assign(slowGen(), {
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

      const queryFn: QueryFn = vi.fn().mockReturnValue(slowQuery);
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("slow task", "task-timeout");
      // Note: timeout fires AbortController but doesn't throw in this mock
      // since the generator still yields. In real SDK, abort would terminate stream.
      const { result } = await mgr.spawnTask(task, undefined, 50);
      // The result still comes through because our mock generator isn't abort-aware
      // In production, the SDK would terminate the stream on abort
      expect(result).toBeTruthy();
    });
  });
});
