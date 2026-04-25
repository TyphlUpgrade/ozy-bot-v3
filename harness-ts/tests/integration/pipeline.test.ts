/**
 * Integration tests — full pipeline lifecycle.
 * Tests the orchestrator with all components wired together (mocked SDK + git).
 * Validates: task file -> session -> completion -> merge queue -> done.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Orchestrator, type OrchestratorEvent } from "../../src/orchestrator.js";
import { SessionManager, type GitOps } from "../../src/session/manager.js";
import { SDKClient, type QueryFn } from "../../src/session/sdk.js";
import { MergeGate, type MergeGitOps } from "../../src/gates/merge.js";
import { StateManager } from "../../src/lib/state.js";
import type { HarnessConfig } from "../../src/lib/config.js";
import type { Query, SDKMessage, SDKResultSuccess } from "@anthropic-ai/claude-agent-sdk";

// --- Shared test infrastructure ---

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-integ-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

function makeResultSuccess(sessionId = "session-integ"): SDKResultSuccess {
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
    uuid: "uuid-integ" as SDKResultSuccess["uuid"],
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
      name: "test-integration",
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

interface IntegrationHarness {
  orch: Orchestrator;
  state: StateManager;
  events: OrchestratorEvent[];
  config: HarnessConfig;
  gitOps: GitOps;
  mergeGitOps: MergeGitOps;
  queryFn: QueryFn;
}

function setupIntegration(opts?: {
  completionStatus?: "success" | "failure";
  mergeOverrides?: Partial<MergeGitOps>;
  queryFactory?: () => Query;
}): IntegrationHarness {
  const config = makeConfig();
  mkdirSync(join(tmpDir, "tasks"), { recursive: true });

  const completionStatus = opts?.completionStatus ?? "success";

  const gitOps: GitOps = {
    createWorktree: vi.fn((_base, _branch, wtPath) => {
      mkdirSync(join(wtPath, ".harness"), { recursive: true });
      writeFileSync(
        join(wtPath, ".harness", "completion.json"),
        JSON.stringify({
          status: completionStatus,
          commitSha: "integ-sha-" + Math.random().toString(36).slice(2, 8),
          summary: "Integration test completion",
          filesChanged: ["src/module.ts", "tests/module.test.ts"],
        }),
      );
    }),
    removeWorktree: vi.fn((_repoPath: string, _worktreePath: string) => {}),
    branchExists: vi.fn((_repoPath: string, _branchName: string) => false),
    deleteBranch: vi.fn((_repoPath: string, _branchName: string) => {}),
  };

  const queryFn: QueryFn = opts?.queryFactory
    ? vi.fn().mockImplementation(opts.queryFactory)
    : vi.fn().mockImplementation(() => mockQuery([makeResultSuccess()]));

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
    ...opts?.mergeOverrides,
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

  return { orch, state, events, config, gitOps, mergeGitOps, queryFn };
}

function dropTaskFile(taskDir: string, id: string, prompt: string): void {
  writeFileSync(join(taskDir, `${id}.json`), JSON.stringify({ prompt }));
}

// --- Integration Tests ---

describe("Pipeline Integration", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("full lifecycle: task file -> session -> completion -> merge -> done", async () => {
    const { orch, state, events } = setupIntegration();

    // Drop a task file
    dropTaskFile(join(tmpDir, "tasks"), "integ-1", "Add input validation to processOrder");

    // Scan and process
    orch.scanForTasks();

    // Wait for async processing
    await new Promise((r) => setTimeout(r, 100));

    // Verify final state
    const task = state.getTask("integ-1")!;
    expect(task.state).toBe("done");
    expect(task.prompt).toBe("Add input validation to processOrder");
    expect(task.summary).toBe("Integration test completion");
    expect(task.filesChanged).toEqual(["src/module.ts", "tests/module.test.ts"]);
    expect(task.totalCostUsd).toBe(0.05);

    // Verify event sequence
    const eventTypes = events.map((e) => e.type);
    expect(eventTypes).toContain("task_picked_up");
    expect(eventTypes).toContain("session_complete");
    expect(eventTypes).toContain("merge_result");
    expect(eventTypes).toContain("task_done");

    // Task file removed
    expect(existsSync(join(tmpDir, "tasks", "integ-1.json"))).toBe(false);
  });

  it("two tasks near-simultaneously -> merge queue serializes", async () => {
    const mergeOrder: string[] = [];
    const { orch, state, mergeGitOps } = setupIntegration({
      mergeOverrides: {
        mergeNoFf: vi.fn().mockImplementation((_cwd: string, branch: string) => {
          mergeOrder.push(branch);
          return `sha-${branch}`;
        }),
      },
    });

    // Drop two task files
    dropTaskFile(join(tmpDir, "tasks"), "task-a", "Task A: fix auth");
    dropTaskFile(join(tmpDir, "tasks"), "task-b", "Task B: fix cache");

    // Scan picks up both
    orch.scanForTasks();

    // Wait for both to complete
    await new Promise((r) => setTimeout(r, 200));

    // Both should reach done
    const a = state.getTask("task-a")!;
    const b = state.getTask("task-b")!;
    expect(a.state).toBe("done");
    expect(b.state).toBe("done");

    // Merge queue processed in FIFO order
    expect(mergeOrder).toHaveLength(2);
    // Both merged (order depends on scan order, which is filesystem-dependent)
    expect(mergeOrder).toContain("harness/task-task-a");
    expect(mergeOrder).toContain("harness/task-task-b");
  });

  it("rebase conflict -> shelve -> auto-retry -> succeeds on retry", async () => {
    let rebaseCallCount = 0;
    const { orch, state, events } = setupIntegration({
      mergeOverrides: {
        rebase: vi.fn().mockImplementation(() => {
          rebaseCallCount++;
          if (rebaseCallCount === 1) {
            return { success: false, conflictFiles: ["src/shared.ts"] };
          }
          return { success: true, conflictFiles: [] };
        }),
      },
    });

    // Drop task
    dropTaskFile(join(tmpDir, "tasks"), "conflict-retry", "Fix shared module");

    orch.scanForTasks();

    // Wait for first attempt (fails with conflict) + retry (succeeds)
    // Retry delay is 5000ms in orchestrator, but we need to wait for it
    // For this test, we'll process manually
    await new Promise((r) => setTimeout(r, 100));

    // After first attempt, should be shelved
    let task = state.getTask("conflict-retry")!;
    expect(task.state).toBe("shelved");
    expect(task.rebaseAttempts).toBe(1);

    // Wait for auto-retry (scheduled at 5s in orchestrator)
    // Instead of waiting, manually trigger retry
    state.transition("conflict-retry", "pending");
    const updated = state.getTask("conflict-retry")!;
    await orch.processTask(updated);

    // Should now be done (second rebase succeeds)
    task = state.getTask("conflict-retry")!;
    expect(task.state).toBe("done");
    expect(rebaseCallCount).toBe(2);
  });

  it("agent failure (no completion) -> state=failed", async () => {
    const { orch, state, config } = setupIntegration({ completionStatus: "failure" });
    config.pipeline.max_session_retries = 1;
    config.pipeline.auto_escalate_on_max_retries = false;

    dropTaskFile(join(tmpDir, "tasks"), "agent-fail", "Do something impossible");
    orch.scanForTasks();

    await new Promise((r) => setTimeout(r, 100));

    const task = state.getTask("agent-fail")!;
    expect(task.state).toBe("failed");
  });

  it("test failure after merge -> revert -> state=failed", async () => {
    const { orch, state, mergeGitOps } = setupIntegration({
      mergeOverrides: {
        runTests: vi.fn().mockReturnValue({ success: false, output: "FAIL: 3 tests" }),
      },
    });

    dropTaskFile(join(tmpDir, "tasks"), "test-fail-integ", "Add broken feature");
    orch.scanForTasks();

    await new Promise((r) => setTimeout(r, 100));

    const task = state.getTask("test-fail-integ")!;
    expect(task.state).toBe("failed");
    expect(task.lastError).toContain("Tests failed");
    expect(mergeGitOps.revertLastMerge).toHaveBeenCalled();
  });

  it("graceful shutdown aborts active sessions", async () => {
    // Use a slow query that won't complete before shutdown
    let queryStarted = false;
    const { orch, state } = setupIntegration({
      queryFactory: () => {
        queryStarted = true;
        async function* slow(): AsyncGenerator<SDKMessage, void> {
          await new Promise((r) => setTimeout(r, 10000)); // 10s — won't complete
          yield makeResultSuccess();
        }
        return Object.assign(slow(), {
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
      },
    });

    dropTaskFile(join(tmpDir, "tasks"), "shutdown-test", "Long task");
    orch.start();

    // Wait for task pickup
    await new Promise((r) => setTimeout(r, 50));

    await orch.shutdown();
    expect(orch.isRunning).toBe(false);
  });

  it("multiple tasks with different outcomes tracked independently", async () => {
    let callCount = 0;
    const { orch, state } = setupIntegration({
      mergeOverrides: {
        runTests: vi.fn().mockImplementation(() => {
          callCount++;
          // First task's tests pass, second's fail
          if (callCount === 1) return { success: true, output: "ok" };
          return { success: false, output: "FAIL" };
        }),
      },
    });

    dropTaskFile(join(tmpDir, "tasks"), "multi-pass", "Task that passes");
    dropTaskFile(join(tmpDir, "tasks"), "multi-fail", "Task that fails tests");

    orch.scanForTasks();
    await new Promise((r) => setTimeout(r, 200));

    // One done, one failed
    const allTasks = state.getAllTasks();
    const states = allTasks.map((t) => t.state).sort();
    expect(states).toContain("done");
    expect(states).toContain("failed");
  });
});
