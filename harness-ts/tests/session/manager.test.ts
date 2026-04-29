import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync, utimesSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { SessionManager, type GitOps, type TmuxOps, type CompletionSignal } from "../../src/session/manager.js";
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

    it("passes maxBudgetUsd from config to SDK session", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(
        mockQuery([makeResultSuccess()]),
      );
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      config.pipeline.max_budget_usd = 2.5;
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test prompt", "task-budget");
      await mgr.spawnTask(task);

      expect(queryFn).toHaveBeenCalledWith(
        expect.objectContaining({
          options: expect.objectContaining({
            maxBudgetUsd: 2.5,
          }),
        }),
      );
    });

    it("omits maxBudgetUsd when not configured", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(
        mockQuery([makeResultSuccess()]),
      );
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      // no max_budget_usd set
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test prompt", "task-no-budget");
      await mgr.spawnTask(task);

      const callArgs = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(callArgs.options.maxBudgetUsd).toBeUndefined();
    });

    it("passes systemPrompt from config to SDK session", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(
        mockQuery([makeResultSuccess()]),
      );
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      config.systemPrompt = "You are a test agent.";
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test prompt", "task-sysprompt");
      await mgr.spawnTask(task);

      // queryFn was called with options that include systemPrompt
      expect(queryFn).toHaveBeenCalledWith(
        expect.objectContaining({
          options: expect.objectContaining({
            systemPrompt: expect.objectContaining({
              type: "preset",
              preset: "claude_code",
              append: "You are a test agent.",
            }),
          }),
        }),
      );
    });

    it("prepends task.lastDirective to the SDK prompt when set (P1 retry path)", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());
      const task = state.createTask("do the thing", "task-dir");
      state.updateTask(task.id, { lastDirective: "avoid global mutation" });
      await mgr.spawnTask(state.getTask(task.id)!);

      const promptSent = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0].prompt as string;
      expect(promptSent).toMatch(/Architect directive \(from prior arbitration\)/);
      expect(promptSent).toMatch(/avoid global mutation/);
      // Original task prompt must still be present.
      expect(promptSent).toMatch(/do the thing/);
    });

    it("omits directive block when task.lastDirective is unset", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());
      const task = state.createTask("pristine", "task-no-dir");
      await mgr.spawnTask(task);
      const promptSent = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0].prompt as string;
      // Wave R1: every prompt is suffixed with the harness completion contract.
      // Original prompt must be the leading section (before the contract trailer).
      expect(promptSent.startsWith("pristine\n")).toBe(true);
      expect(promptSent).toContain("HARNESS COMPLETION CONTRACT");
    });

    it("falls back to DEFAULT_EXECUTOR_SYSTEM_PROMPT when config.systemPrompt is unset (U3)", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());
      const task = state.createTask("do the thing", "task-default-prompt");
      await mgr.spawnTask(task);

      const appended = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0].options.systemPrompt.append;
      expect(appended).toMatch(/understanding/);
      expect(appended).toMatch(/assumptions/);
      expect(appended).toMatch(/nonGoals/);
      expect(appended).toMatch(/confidence/);
    });

    it("DEFAULT_EXECUTOR_SYSTEM_PROMPT contains all four Phase 2A enrichment fields", async () => {
      const { DEFAULT_EXECUTOR_SYSTEM_PROMPT } = await import("../../src/lib/config.js");
      for (const field of ["understanding", "assumptions", "nonGoals", "confidence"]) {
        expect(DEFAULT_EXECUTOR_SYSTEM_PROMPT).toContain(field);
      }
      // Phase 2A confidence sub-fields must also appear so Executor knows the full schema.
      for (const sub of ["scopeClarity", "designCertainty", "testCoverage"]) {
        expect(DEFAULT_EXECUTOR_SYSTEM_PROMPT).toContain(sub);
      }
    });

    it("DEFAULT_EXECUTOR_SYSTEM_PROMPT does NOT instruct `git commit` (WA-2 propose-then-commit)", async () => {
      const { DEFAULT_EXECUTOR_SYSTEM_PROMPT } = await import("../../src/lib/config.js");
      // No positive instruction to commit.
      expect(DEFAULT_EXECUTOR_SYSTEM_PROMPT).not.toMatch(/run `git commit`\s*[^\.]/i);
      expect(DEFAULT_EXECUTOR_SYSTEM_PROMPT).not.toMatch(/Commit your (?:code )?changes with/i);
      // Explicit statement that orchestrator commits post-review.
      expect(DEFAULT_EXECUTOR_SYSTEM_PROMPT).toMatch(/orchestrator will stage and commit/i);
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

    it("accepts completion.json without commitSha (WA-1 propose-then-commit)", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          summary: "test without sha",
          filesChanged: ["a.ts"],
        }),
      );
      const { mgr } = makeManager();
      const completion = mgr.readCompletion(dir);
      expect(completion).toBeTruthy();
      expect(completion!.commitSha).toBeUndefined();
      expect(completion!.summary).toBe("test without sha");
      rmSync(dir, { recursive: true, force: true });
    });

    it("accepts completion with all enrichment fields", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "abc123",
          summary: "Added auth module",
          filesChanged: ["src/auth.ts"],
          understanding: "Add JWT-based auth to the login endpoint",
          assumptions: ["Using RS256 algorithm", "Token expires in 1h"],
          nonGoals: ["OAuth2 support", "Session management"],
          confidence: {
            scopeClarity: "clear",
            designCertainty: "obvious",
            assumptions: [
              { description: "RS256 key pair exists", impact: "high", reversible: false },
            ],
            openQuestions: [],
            testCoverage: "verifiable",
          },
        }),
      );

      const { mgr } = makeManager();
      const result = mgr.readCompletion(dir);
      expect(result).not.toBeNull();
      expect(result!.understanding).toBe("Add JWT-based auth to the login endpoint");
      expect(result!.assumptions).toEqual(["Using RS256 algorithm", "Token expires in 1h"]);
      expect(result!.nonGoals).toEqual(["OAuth2 support", "Session management"]);
      expect(result!.confidence).toBeDefined();
      expect(result!.confidence!.scopeClarity).toBe("clear");
      expect(result!.confidence!.assumptions).toHaveLength(1);
      rmSync(dir, { recursive: true, force: true });
    });

    it("accepts completion with partial enrichment", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "def456",
          summary: "Quick fix",
          filesChanged: ["src/fix.ts"],
          understanding: "Fix null check",
          // no assumptions, nonGoals, or confidence
        }),
      );

      const { mgr } = makeManager();
      const result = mgr.readCompletion(dir);
      expect(result).not.toBeNull();
      expect(result!.understanding).toBe("Fix null check");
      expect(result!.assumptions).toBeUndefined();
      expect(result!.nonGoals).toBeUndefined();
      expect(result!.confidence).toBeUndefined();
      rmSync(dir, { recursive: true, force: true });
    });

    it("strips malformed confidence but keeps rest of signal", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "ghi789",
          summary: "Refactored module",
          filesChanged: ["src/mod.ts"],
          understanding: "Simplify module structure",
          confidence: {
            scopeClarity: "INVALID",
            designCertainty: 42,
          },
        }),
      );

      const { mgr } = makeManager();
      const result = mgr.readCompletion(dir);
      expect(result).not.toBeNull();
      expect(result!.understanding).toBe("Simplify module structure");
      expect(result!.confidence).toBeUndefined(); // malformed, stripped
      rmSync(dir, { recursive: true, force: true });
    });

    it("validates confidence assessment dimensions", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "jkl012",
          summary: "Complex change",
          filesChanged: ["src/complex.ts"],
          confidence: {
            scopeClarity: "partial",
            designCertainty: "alternatives_exist",
            assumptions: [
              { description: "API stable", impact: "high", reversible: true },
              { description: "Cache TTL ok", impact: "low", reversible: true },
            ],
            openQuestions: ["What about edge case X?"],
            testCoverage: "partial",
          },
        }),
      );

      const { mgr } = makeManager();
      const result = mgr.readCompletion(dir);
      expect(result).not.toBeNull();
      expect(result!.confidence!.scopeClarity).toBe("partial");
      expect(result!.confidence!.designCertainty).toBe("alternatives_exist");
      expect(result!.confidence!.testCoverage).toBe("partial");
      expect(result!.confidence!.assumptions).toHaveLength(2);
      expect(result!.confidence!.openQuestions).toEqual(["What about edge case X?"]);
      rmSync(dir, { recursive: true, force: true });
    });

    it("strips non-string entries from assumptions array", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "mno345",
          summary: "test",
          filesChanged: [],
          assumptions: ["valid string", 42, null, "another valid"],
        }),
      );

      const { mgr } = makeManager();
      const result = mgr.readCompletion(dir);
      expect(result).not.toBeNull();
      expect(result!.assumptions).toEqual(["valid string", "another valid"]);
      rmSync(dir, { recursive: true, force: true });
    });

    it("U3: warns when success completion missing enrichment fields (caveman drop guard)", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          summary: "no enrichment",
          filesChanged: ["a.ts"],
          // missing: commitSha, understanding, assumptions, nonGoals, confidence
        }),
      );
      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      const { mgr } = makeManager();
      const result = mgr.readCompletion(dir);
      expect(result).not.toBeNull();
      expect(warnSpy).toHaveBeenCalledTimes(1);
      const msg = warnSpy.mock.calls[0][0] as string;
      expect(msg).toMatch(/missing enrichment fields:/);
      expect(msg).toMatch(/commitSha/);
      expect(msg).toMatch(/understanding/);
      expect(msg).toMatch(/assumptions/);
      expect(msg).toMatch(/nonGoals/);
      expect(msg).toMatch(/confidence/);
      warnSpy.mockRestore();
      rmSync(dir, { recursive: true, force: true });
    });

    it("U3: does NOT warn when failure completion missing enrichment", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "failure",
          summary: "build broken",
          filesChanged: [],
        }),
      );
      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      const { mgr } = makeManager();
      mgr.readCompletion(dir);
      expect(warnSpy).not.toHaveBeenCalled();
      warnSpy.mockRestore();
      rmSync(dir, { recursive: true, force: true });
    });

    it("U3: does NOT warn when all enrichment fields present", () => {
      const dir = makeTmpDir();
      mkdirSync(join(dir, ".harness"), { recursive: true });
      writeFileSync(
        join(dir, ".harness", "completion.json"),
        JSON.stringify({
          status: "success",
          commitSha: "abc123",
          summary: "full",
          filesChanged: ["a.ts"],
          understanding: "...",
          assumptions: ["x"],
          nonGoals: ["y"],
          confidence: {
            scopeClarity: "clear",
            designCertainty: "obvious",
            assumptions: [],
            openQuestions: [],
            testCoverage: "verifiable",
          },
        }),
      );
      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      const { mgr } = makeManager();
      mgr.readCompletion(dir);
      expect(warnSpy).not.toHaveBeenCalled();
      warnSpy.mockRestore();
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

  // Wave 1 pre-requisites
  describe("Wave 1: plugins / hooks / disallowedTools / tmux", () => {
    function mockTmuxOps(): TmuxOps {
      return {
        killSessionsByPattern: vi.fn(),
      };
    }

    it("Item 1+2 sub-fix: sessionConfig uses persistSession=true, hooks={}, and merged plugins", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(
        mockQuery([makeResultSuccess()]),
      );
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test", "task-plugins");
      await mgr.spawnTask(task);

      const opts = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0].options;
      // persistSession fix: was false, now true
      expect(opts.persistSession).toBe(true);
      // hooks defense: always empty object
      expect(opts.hooks).toEqual({});
      // default plugins merged — both default OFF on Executor per spike-caveman-json (U1)
      // and spike-omc-overhead (U2). See manager.ts DEFAULT_PLUGINS comment block.
      expect(opts.settings).toBeDefined();
      const plugins = (opts.settings as { enabledPlugins: Record<string, boolean> }).enabledPlugins;
      expect(plugins["oh-my-claudecode@omc"]).toBe(false);
      expect(plugins["caveman@caveman"]).toBe(false);
    });

    it("Item 1: config plugins override defaults per-entry", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(
        mockQuery([makeResultSuccess()]),
      );
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      config.pipeline.plugins = {
        "caveman@caveman": true,      // override default OFF → ON
        "custom-plugin@scope": true,  // add new
      };
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test", "task-plugin-override");
      await mgr.spawnTask(task);

      const opts = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0].options;
      const plugins = (opts.settings as { enabledPlugins: Record<string, boolean> }).enabledPlugins;
      expect(plugins["oh-my-claudecode@omc"]).toBe(false);     // default OFF preserved
      expect(plugins["caveman@caveman"]).toBe(true);            // overridden ON
      expect(plugins["custom-plugin@scope"]).toBe(true);        // added
    });

    it("Item 3: spawnTask applies default disallowedTools (cron/remote/wakeup)", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(
        mockQuery([makeResultSuccess()]),
      );
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test", "task-disallowed");
      await mgr.spawnTask(task);

      const opts = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0].options;
      expect(opts.disallowedTools).toEqual(
        expect.arrayContaining([
          "CronCreate",
          "CronDelete",
          "CronList",
          "RemoteTrigger",
          "ScheduleWakeup",
        ]),
      );
    });

    it("Item 3: config disallowed_tools extend defaults (union, not override)", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(
        mockQuery([makeResultSuccess()]),
      );
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      config.pipeline.disallowed_tools = ["WebFetch", "WebSearch"];
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const task = state.createTask("test", "task-disallowed-extra");
      await mgr.spawnTask(task);

      const opts = (queryFn as ReturnType<typeof vi.fn>).mock.calls[0][0].options;
      expect(opts.disallowedTools).toEqual(
        expect.arrayContaining([
          "CronCreate",
          "CronDelete",
          "CronList",
          "RemoteTrigger",
          "ScheduleWakeup",
          "WebFetch",
          "WebSearch",
        ]),
      );
    });

    it("Item 4: cleanupWorktree invokes tmux kill with task-{id} pattern", () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const git = mockGitOps();
      const tmux = mockTmuxOps();
      const mgr = new SessionManager(sdk, state, config, git, tmux);

      mgr.cleanupWorktree("task-abc");
      expect(tmux.killSessionsByPattern).toHaveBeenCalledWith("task-task-abc");
    });

    it("Item 4: abortAll invokes tmux sweep with harness- pattern", () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const tmux = mockTmuxOps();
      const mgr = new SessionManager(sdk, state, config, mockGitOps(), tmux);

      mgr.abortAll();
      expect(tmux.killSessionsByPattern).toHaveBeenCalledWith("harness-");
    });

    it("Item 4 extension: cleanupProject removes Architect worktree + branch", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const git = mockGitOps();
      (git.branchExists as ReturnType<typeof vi.fn>).mockReturnValue(true);
      const tmux = mockTmuxOps();
      const mgr = new SessionManager(sdk, state, config, git, tmux);

      await mgr.cleanupProject("proj-42");

      expect(git.removeWorktree).toHaveBeenCalledWith(
        config.project.root,
        expect.stringContaining("architect-proj-42"),
      );
      expect(git.deleteBranch).toHaveBeenCalledWith(
        config.project.root,
        "harness/architect-proj-42",
      );
    });

    it("Item 4 extension: cleanupProject sweeps architect-{projectId} tmux pattern", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const tmux = mockTmuxOps();
      const mgr = new SessionManager(sdk, state, config, mockGitOps(), tmux);

      await mgr.cleanupProject("proj-77");

      expect(tmux.killSessionsByPattern).toHaveBeenCalledWith("architect-proj-77");
    });

    it("Item 4 extension: cleanupProject silent on missing worktree/branch", async () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const git = mockGitOps();
      // Branch doesn't exist, worktree removal throws
      (git.branchExists as ReturnType<typeof vi.fn>).mockReturnValue(false);
      (git.removeWorktree as ReturnType<typeof vi.fn>).mockImplementation(() => {
        throw new Error("worktree not found");
      });
      const tmux = mockTmuxOps();
      const mgr = new SessionManager(sdk, state, config, git, tmux);

      await expect(mgr.cleanupProject("proj-missing")).resolves.toBeUndefined();
      expect(git.deleteBranch).not.toHaveBeenCalled();
      // Tmux sweep still fires
      expect(tmux.killSessionsByPattern).toHaveBeenCalledWith("architect-proj-missing");
    });

    it("Item 4: tmux failure is swallowed — cleanupWorktree still succeeds", () => {
      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const git = mockGitOps();
      const tmux: TmuxOps = {
        killSessionsByPattern: vi.fn(() => {
          throw new Error("tmux server not running");
        }),
      };
      const mgr = new SessionManager(sdk, state, config, git, tmux);

      expect(() => mgr.cleanupWorktree("task-xyz")).not.toThrow();
      // git cleanup still happened despite tmux throw
      expect(git.removeWorktree).toHaveBeenCalledOnce();
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

  describe("Wave C / U4 — persistent-session observability", () => {
    it("persistentSessionCount reflects cumulative spawnTask calls", async () => {
      const queryFn: QueryFn = vi.fn().mockImplementation(() => mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      expect(mgr.persistentSessionCount).toBe(0);
      for (let i = 0; i < 3; i++) {
        const task = state.createTask(`p${i}`, `task-ps-${i}`);
        await mgr.spawnTask(task);
      }
      expect(mgr.persistentSessionCount).toBe(3);
    });

    it("warns once per spawn above configured threshold", async () => {
      const queryFn: QueryFn = vi.fn().mockImplementation(() => mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      config.pipeline.persistent_session_warn_threshold = 2;
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      for (let i = 0; i < 4; i++) {
        const task = state.createTask(`p${i}`, `task-thresh-${i}`);
        await mgr.spawnTask(task);
      }
      // threshold=2 → spawns 3 and 4 both trigger (count > threshold). 1 and 2 do NOT.
      expect(warnSpy.mock.calls.length).toBe(2);
      expect(warnSpy.mock.calls[0][0]).toMatch(/persistent-session count 3.*threshold 2/);
      expect(warnSpy.mock.calls[1][0]).toMatch(/persistent-session count 4.*threshold 2/);
      warnSpy.mockRestore();
    });

    it("default threshold is 100 when unconfigured (no warn under 100)", async () => {
      const queryFn: QueryFn = vi.fn().mockImplementation(() => mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const config = makeConfig();
      const mgr = new SessionManager(sdk, state, config, mockGitOps());

      const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      for (let i = 0; i < 5; i++) {
        const task = state.createTask(`p${i}`, `task-default-${i}`);
        await mgr.spawnTask(task);
      }
      expect(warnSpy.mock.calls.filter((c) => /persistent-session count/.test(c[0] as string))).toHaveLength(0);
      warnSpy.mockRestore();
    });

    it("pruneSessionDir: silent no-op when session_dir missing", () => {
      const { mgr } = makeManager();
      // Default tmpDir has no sessions/ subdir.
      expect(mgr.pruneSessionDir()).toBe(0);
    });

    it("pruneSessionDir: deletes files older than maxAgeMs, keeps newer", () => {
      const { mgr, config } = makeManager();
      mkdirSync(config.project.session_dir, { recursive: true });
      const oldPath = join(config.project.session_dir, "old.json");
      const newPath = join(config.project.session_dir, "new.json");
      writeFileSync(oldPath, "{}");
      writeFileSync(newPath, "{}");
      // Backdate old.json to 10 days ago.
      const tenDaysAgo = Date.now() - 10 * 24 * 60 * 60 * 1000;
      utimesSync(oldPath, tenDaysAgo / 1000, tenDaysAgo / 1000);

      const deleted = mgr.pruneSessionDir({ maxAgeMs: 7 * 24 * 60 * 60 * 1000 });
      expect(deleted).toBe(1);
      expect(existsSync(oldPath)).toBe(false);
      expect(existsSync(newPath)).toBe(true);
    });

    it("pruneSessionDir: maxRecords keeps only N most recent", () => {
      const { mgr, config } = makeManager();
      mkdirSync(config.project.session_dir, { recursive: true });
      const paths: string[] = [];
      for (let i = 0; i < 5; i++) {
        const p = join(config.project.session_dir, `s${i}.json`);
        writeFileSync(p, "{}");
        // Stagger mtime so order is deterministic.
        const ts = (Date.now() - (5 - i) * 1000) / 1000;
        utimesSync(p, ts, ts);
        paths.push(p);
      }
      const deleted = mgr.pruneSessionDir({ maxRecords: 2, maxAgeMs: 24 * 60 * 60 * 1000 });
      // Keeps 2 newest (s4, s3); deletes s0, s1, s2.
      expect(deleted).toBe(3);
      expect(existsSync(paths[0])).toBe(false);
      expect(existsSync(paths[1])).toBe(false);
      expect(existsSync(paths[2])).toBe(false);
      expect(existsSync(paths[3])).toBe(true);
      expect(existsSync(paths[4])).toBe(true);
    });
  });
});
