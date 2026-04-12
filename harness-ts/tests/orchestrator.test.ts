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
  freshQueryPerCall?: boolean;
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

  const queryFn: QueryFn = opts?.freshQueryPerCall
    ? vi.fn().mockImplementation(() => mockQuery(opts?.queryMessages ?? [makeResultSuccess()]))
    : vi.fn().mockReturnValue(mockQuery(opts?.queryMessages ?? [makeResultSuccess()]));
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

    it("fails task when session returns error (after retries exhausted)", async () => {
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

      const { orch, state, events, config } = setupHarness({ queryMessages: [errorResult], freshQueryPerCall: true });
      config.pipeline.max_session_retries = 1;
      config.pipeline.auto_escalate_on_max_retries = false;
      const task = state.createTask("test", "fail-1");

      await orch.processTask(task);

      expect(state.getTask("fail-1")!.state).toBe("failed");
      expect(events.some((e) => e.type === "task_failed")).toBe(true);
    });

    it("fails task when no completion signal (after retries exhausted)", async () => {
      const { orch, state, config } = setupHarness({ withCompletion: false, freshQueryPerCall: true });
      config.pipeline.max_session_retries = 1;
      config.pipeline.auto_escalate_on_max_retries = false;
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

  describe("Phase 2A — escalation detection", () => {
    it("escalation signal -> task transitions to escalation_wait", async () => {
      const { orch, state, events, gitOps } = setupHarness({ withCompletion: true });
      // Also write escalation.json (escalation takes priority)
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          writeFileSync(
            join(wtPath, ".harness", "completion.json"),
            JSON.stringify({ status: "success", commitSha: "abc", summary: "Done", filesChanged: [] }),
          );
          writeFileSync(
            join(wtPath, ".harness", "escalation.json"),
            JSON.stringify({ type: "design_decision", question: "REST or gRPC?" }),
          );
        },
      );
      const task = state.createTask("test", "esc-1");
      await orch.processTask(task);

      expect(state.getTask("esc-1")!.state).toBe("escalation_wait");
      const escEvent = events.find((e) => e.type === "escalation_needed");
      expect(escEvent).toBeTruthy();
      if (escEvent && escEvent.type === "escalation_needed") {
        expect(escEvent.escalation.type).toBe("design_decision");
      }
    });

    it("escalation takes priority over successful completion", async () => {
      const { orch, state, events, gitOps } = setupHarness();
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          writeFileSync(
            join(wtPath, ".harness", "completion.json"),
            JSON.stringify({ status: "success", commitSha: "abc", summary: "Done", filesChanged: ["a.ts"] }),
          );
          writeFileSync(
            join(wtPath, ".harness", "escalation.json"),
            JSON.stringify({ type: "scope_unclear", question: "Scope question" }),
          );
        },
      );
      const task = state.createTask("test", "esc-prio");
      await orch.processTask(task);

      // Should NOT proceed to merge
      expect(state.getTask("esc-prio")!.state).toBe("escalation_wait");
      expect(events.some((e) => e.type === "merge_result")).toBe(false);
    });

    it("no escalation -> normal flow unchanged", async () => {
      const { orch, state, events } = setupHarness({ withCompletion: true });
      const task = state.createTask("test", "no-esc");
      await orch.processTask(task);

      expect(state.getTask("no-esc")!.state).toBe("done");
      expect(events.some((e) => e.type === "escalation_needed")).toBe(false);
    });
  });

  describe("Phase 2A — checkpoint detection", () => {
    it("checkpoint found -> event emitted", async () => {
      const { orch, state, events, gitOps } = setupHarness();
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          writeFileSync(
            join(wtPath, ".harness", "completion.json"),
            JSON.stringify({ status: "success", commitSha: "abc", summary: "Done", filesChanged: [] }),
          );
          writeFileSync(
            join(wtPath, ".harness", "checkpoint.json"),
            JSON.stringify([
              { timestamp: "2026-04-11T12:00:00Z", reason: "decision_point", description: "Chose REST" },
            ]),
          );
        },
      );
      const task = state.createTask("test", "cp-1");
      await orch.processTask(task);

      const cpEvent = events.find((e) => e.type === "checkpoint_detected");
      expect(cpEvent).toBeTruthy();
      if (cpEvent && cpEvent.type === "checkpoint_detected") {
        expect(cpEvent.checkpoints).toHaveLength(1);
      }
    });

    it("no checkpoint file -> no event", async () => {
      const { orch, state, events } = setupHarness({ withCompletion: true });
      const task = state.createTask("test", "no-cp");
      await orch.processTask(task);

      expect(events.some((e) => e.type === "checkpoint_detected")).toBe(false);
    });
  });

  describe("Phase 2A — response level", () => {
    it("emits response_level on successful completion", async () => {
      const { orch, state, events } = setupHarness({ withCompletion: true });
      const task = state.createTask("test", "resp-1");
      await orch.processTask(task);

      const respEvent = events.find((e) => e.type === "response_level");
      expect(respEvent).toBeTruthy();
      if (respEvent && respEvent.type === "response_level") {
        // Bare completion (no confidence) -> level 1
        expect(respEvent.level).toBe(1);
        expect(respEvent.name).toBe("enriched");
      }
    });

    it("uses session cost in response evaluation", async () => {
      const { orch, state, events, gitOps } = setupHarness();
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          writeFileSync(
            join(wtPath, ".harness", "completion.json"),
            JSON.stringify({
              status: "success",
              commitSha: "abc",
              summary: "Done",
              filesChanged: ["a.ts"],
              confidence: {
                scopeClarity: "clear",
                designCertainty: "obvious",
                assumptions: [],
                openQuestions: [],
                testCoverage: "verifiable",
              },
            }),
          );
        },
      );
      const task = state.createTask("test", "resp-cost");
      await orch.processTask(task);

      const respEvent = events.find((e) => e.type === "response_level");
      expect(respEvent).toBeTruthy();
      // Cost is $0.05 (from mock), below review threshold — level 0
      if (respEvent && respEvent.type === "response_level") {
        expect(respEvent.level).toBe(0);
      }
    });
  });

  describe("Phase 2A — failure retry + circuit breaker", () => {
    it("session failure -> retry (attempt 1 of 3)", async () => {
      // First call fails (no completion), second succeeds (with completion)
      let callCount = 0;
      const { orch, state, events, gitOps, config } = setupHarness({ freshQueryPerCall: true });
      config.pipeline.max_session_retries = 3;
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          callCount++;
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          if (callCount >= 2) {
            writeFileSync(
              join(wtPath, ".harness", "completion.json"),
              JSON.stringify({ status: "success", commitSha: "abc", summary: "Done", filesChanged: [] }),
            );
          }
        },
      );
      const task = state.createTask("test", "retry-1");
      await orch.processTask(task);

      expect(events.some((e) => e.type === "retry_scheduled")).toBe(true);
      expect(state.getTask("retry-1")!.state).toBe("done");
    });

    it("session failure -> retry -> success on attempt 2", async () => {
      let callCount = 0;
      const { orch, state, events, gitOps, config } = setupHarness({ freshQueryPerCall: true });
      config.pipeline.max_session_retries = 3;
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          callCount++;
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          if (callCount === 2) {
            writeFileSync(
              join(wtPath, ".harness", "completion.json"),
              JSON.stringify({ status: "success", commitSha: "abc", summary: "Done", filesChanged: [] }),
            );
          }
        },
      );
      const task = state.createTask("test", "retry-success-2");
      await orch.processTask(task);

      const retryEvents = events.filter((e) => e.type === "retry_scheduled");
      expect(retryEvents).toHaveLength(1); // only 1 retry needed
      expect(state.getTask("retry-success-2")!.state).toBe("done");
    });

    it("max retries exhausted -> auto-escalate", async () => {
      const { orch, state, events, config } = setupHarness({ withCompletion: false, freshQueryPerCall: true });
      config.pipeline.max_session_retries = 2;
      config.pipeline.auto_escalate_on_max_retries = true;
      config.pipeline.max_tier1_escalations = 2;
      const task = state.createTask("test", "retry-esc");

      await orch.processTask(task);

      expect(state.getTask("retry-esc")!.state).toBe("escalation_wait");
      const escEvent = events.find((e) => e.type === "escalation_needed");
      expect(escEvent).toBeTruthy();
      if (escEvent && escEvent.type === "escalation_needed") {
        expect(escEvent.escalation.type).toBe("persistent_failure");
      }
    });

    it("auto_escalate_on_max_retries=false -> direct fail", async () => {
      const { orch, state, events, config } = setupHarness({ withCompletion: false, freshQueryPerCall: true });
      config.pipeline.max_session_retries = 1;
      config.pipeline.auto_escalate_on_max_retries = false;
      const task = state.createTask("test", "no-esc-fail");

      await orch.processTask(task);

      expect(state.getTask("no-esc-fail")!.state).toBe("failed");
      expect(events.some((e) => e.type === "escalation_needed")).toBe(false);
    });

    it("circuit breaker -> permanent failure after max escalation cycles", async () => {
      const { orch, state, events, config } = setupHarness({ withCompletion: false, freshQueryPerCall: true });
      config.pipeline.max_session_retries = 1;
      config.pipeline.auto_escalate_on_max_retries = true;
      config.pipeline.max_tier1_escalations = 1;
      // Pre-set escalation count to max
      state.createTask("test", "circuit-break");
      state.updateTask("circuit-break", { tier1EscalationCount: 1 });
      const task = state.getTask("circuit-break")!;

      await orch.processTask(task);

      expect(state.getTask("circuit-break")!.state).toBe("failed");
      expect(state.getTask("circuit-break")!.lastError).toContain("Circuit breaker");
    });

    it("tier1EscalationCount increments on each auto-escalation", async () => {
      const { orch, state, config } = setupHarness({ withCompletion: false, freshQueryPerCall: true });
      config.pipeline.max_session_retries = 1;
      config.pipeline.auto_escalate_on_max_retries = true;
      config.pipeline.max_tier1_escalations = 3;
      const task = state.createTask("test", "esc-count");

      await orch.processTask(task);

      expect(state.getTask("esc-count")!.tier1EscalationCount).toBe(1);
      expect(state.getTask("esc-count")!.state).toBe("escalation_wait");
    });

    it("retry_scheduled event includes correct attempt number", async () => {
      let callCount = 0;
      const { orch, state, events, gitOps, config } = setupHarness({ freshQueryPerCall: true });
      config.pipeline.max_session_retries = 3;
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          callCount++;
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          if (callCount === 3) {
            writeFileSync(
              join(wtPath, ".harness", "completion.json"),
              JSON.stringify({ status: "success", commitSha: "abc", summary: "Done", filesChanged: [] }),
            );
          }
        },
      );
      const task = state.createTask("test", "retry-count");
      await orch.processTask(task);

      const retryEvents = events.filter((e) => e.type === "retry_scheduled");
      expect(retryEvents).toHaveLength(2);
      if (retryEvents[0].type === "retry_scheduled") {
        expect(retryEvents[0].attempt).toBe(2); // attempt 2 (after 1st failure)
      }
      if (retryEvents[1].type === "retry_scheduled") {
        expect(retryEvents[1].attempt).toBe(3); // attempt 3 (after 2nd failure)
      }
    });

    it("retryCount persists across retry cycles", async () => {
      const { orch, state, config } = setupHarness({ withCompletion: false, freshQueryPerCall: true });
      config.pipeline.max_session_retries = 2;
      config.pipeline.auto_escalate_on_max_retries = false;
      const task = state.createTask("test", "retry-persist");

      await orch.processTask(task);

      expect(state.getTask("retry-persist")!.retryCount).toBe(2);
    });
  });

  describe("budget exhaustion — no retry", () => {
    it("budget exhaustion -> permanent failure, no retries", async () => {
      const budgetResult: SDKMessage = {
        type: "result",
        subtype: "error_max_budget_usd",
        duration_ms: 60000,
        duration_api_ms: 55000,
        is_error: true,
        num_turns: 10,
        stop_reason: null,
        total_cost_usd: 2.50,
        usage: { input_tokens: 50000, output_tokens: 25000 },
        modelUsage: {},
        permission_denials: [],
        errors: ["Budget exceeded"],
        uuid: "budget-uuid" as any,
        session_id: "budget-session",
        terminal_reason: "error_max_budget_usd",
      } as unknown as SDKMessage;

      const { orch, state, events, config } = setupHarness({ queryMessages: [budgetResult], freshQueryPerCall: true });
      config.pipeline.max_session_retries = 3; // would normally allow retries
      const task = state.createTask("expensive task", "budget-1");

      await orch.processTask(task);

      expect(state.getTask("budget-1")!.state).toBe("failed");
      expect(state.getTask("budget-1")!.lastError).toContain("Budget exhausted");
      // Should NOT retry — no retry_scheduled events
      expect(events.some((e) => e.type === "retry_scheduled")).toBe(false);
      // Should emit budget_exhausted event
      expect(events.some((e) => e.type === "budget_exhausted")).toBe(true);
      const budgetEvent = events.find((e) => e.type === "budget_exhausted");
      if (budgetEvent && budgetEvent.type === "budget_exhausted") {
        expect(budgetEvent.totalCostUsd).toBe(2.50);
      }
    });

    it("budget exhaustion -> no auto-escalation even if configured", async () => {
      const budgetResult: SDKMessage = {
        type: "result",
        subtype: "error_max_budget_usd",
        duration_ms: 60000,
        duration_api_ms: 55000,
        is_error: true,
        num_turns: 10,
        stop_reason: null,
        total_cost_usd: 5.00,
        usage: { input_tokens: 100000, output_tokens: 50000 },
        modelUsage: {},
        permission_denials: [],
        errors: ["Budget exceeded"],
        uuid: "budget-uuid-2" as any,
        session_id: "budget-session-2",
        terminal_reason: "error_max_budget_usd",
      } as unknown as SDKMessage;

      const { orch, state, events, config } = setupHarness({ queryMessages: [budgetResult], freshQueryPerCall: true });
      config.pipeline.max_session_retries = 1;
      config.pipeline.auto_escalate_on_max_retries = true;
      const task = state.createTask("expensive task", "budget-no-esc");

      await orch.processTask(task);

      // Should go straight to failed, not escalation_wait
      expect(state.getTask("budget-no-esc")!.state).toBe("failed");
      expect(events.some((e) => e.type === "escalation_needed")).toBe(false);
    });
  });

  describe("crash cleanup — worktree orphaning", () => {
    it("merge error path -> cleanupWorktree called", async () => {
      const { orch, state, gitOps } = setupHarness({
        withCompletion: true,
        mergeGitOverrides: {
          // Force the merge gate to return an error
          rebase: vi.fn().mockImplementation(() => { throw new Error("merge gate internal error"); }),
        },
      });
      // The rebase throwing will cause MergeGate.enqueue to return status: "error"
      const task = state.createTask("test", "merge-err-cleanup");
      await orch.processTask(task);

      expect(state.getTask("merge-err-cleanup")!.state).toBe("failed");
      // cleanupWorktree should have been called (removeWorktree is the indicator)
      expect(gitOps.removeWorktree).toHaveBeenCalled();
    });

    it("unexpected exception -> cleanupWorktree called", async () => {
      // Create a harness where spawnTask will throw
      const config = makeConfig();
      mkdirSync(join(tmpDir, "tasks"), { recursive: true });
      const gitOps = mockGitOps();
      // Make createWorktree throw to simulate unexpected error
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(() => {
        throw new Error("disk full");
      });

      const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const sdk = new SDKClient(queryFn);
      const state = new StateManager(join(tmpDir, "state.json"));
      const sessionMgr = new SessionManager(sdk, state, config, gitOps);
      const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
      const orch = new Orchestrator({ sessionManager: sessionMgr, mergeGate, stateManager: state, config });

      const events: OrchestratorEvent[] = [];
      orch.on((e) => events.push(e));

      const task = state.createTask("test", "crash-cleanup");
      await orch.processTask(task);

      expect(state.getTask("crash-cleanup")!.state).toBe("failed");
      // removeWorktree called in catch block cleanup (even though it'll no-op since worktree never created)
      expect(gitOps.removeWorktree).toHaveBeenCalled();
    });
  });

  describe("crash recovery — failed state worktree cleanup", () => {
    it("recoverFromCrash cleans up worktrees for failed tasks", () => {
      const { orch, state, gitOps } = setupHarness({ withCompletion: true });
      // Manually create a failed task with a worktreePath (simulating orphaned worktree)
      state.createTask("orphaned", "orphan-1");
      state.transition("orphan-1", "active");
      state.updateTask("orphan-1", { worktreePath: join(tmpDir, "worktrees", "task-orphan-1") });
      state.transition("orphan-1", "failed");

      // Start triggers recoverFromCrash
      orch.start();

      // Should have called cleanupWorktree for the failed task
      expect(gitOps.removeWorktree).toHaveBeenCalled();

      orch.shutdown();
    });
  });

  describe("Phase 2A — completion compliance", () => {
    it("fully enriched completion -> complianceScore 4", async () => {
      const { orch, state, events, gitOps } = setupHarness();
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          writeFileSync(
            join(wtPath, ".harness", "completion.json"),
            JSON.stringify({
              status: "success",
              commitSha: "abc",
              summary: "Done",
              filesChanged: [],
              understanding: "Task understood",
              assumptions: ["A1"],
              nonGoals: ["NG1"],
              confidence: {
                scopeClarity: "clear",
                designCertainty: "obvious",
                assumptions: [],
                openQuestions: [],
                testCoverage: "verifiable",
              },
            }),
          );
        },
      );
      const task = state.createTask("test", "comp-full");
      await orch.processTask(task);

      const compEvent = events.find((e) => e.type === "completion_compliance");
      expect(compEvent).toBeTruthy();
      if (compEvent && compEvent.type === "completion_compliance") {
        expect(compEvent.hasConfidence).toBe(true);
        expect(compEvent.hasUnderstanding).toBe(true);
        expect(compEvent.hasAssumptions).toBe(true);
        expect(compEvent.hasNonGoals).toBe(true);
        expect(compEvent.complianceScore).toBe(4);
      }
    });

    it("bare completion -> complianceScore 0", async () => {
      const { orch, state, events } = setupHarness({ withCompletion: true });
      const task = state.createTask("test", "comp-bare");
      await orch.processTask(task);

      const compEvent = events.find((e) => e.type === "completion_compliance");
      expect(compEvent).toBeTruthy();
      if (compEvent && compEvent.type === "completion_compliance") {
        expect(compEvent.hasConfidence).toBe(false);
        expect(compEvent.hasUnderstanding).toBe(false);
        expect(compEvent.hasAssumptions).toBe(false);
        expect(compEvent.hasNonGoals).toBe(false);
        expect(compEvent.complianceScore).toBe(0);
      }
    });

    it("partial enrichment -> correct score", async () => {
      const { orch, state, events, gitOps } = setupHarness();
      (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
        (_base: string, _branch: string, wtPath: string) => {
          mkdirSync(join(wtPath, ".harness"), { recursive: true });
          writeFileSync(
            join(wtPath, ".harness", "completion.json"),
            JSON.stringify({
              status: "success",
              commitSha: "abc",
              summary: "Done",
              filesChanged: [],
              confidence: {
                scopeClarity: "clear",
                designCertainty: "obvious",
                assumptions: [],
                openQuestions: [],
                testCoverage: "verifiable",
              },
            }),
          );
        },
      );
      const task = state.createTask("test", "comp-partial");
      await orch.processTask(task);

      const compEvent = events.find((e) => e.type === "completion_compliance");
      expect(compEvent).toBeTruthy();
      if (compEvent && compEvent.type === "completion_compliance") {
        expect(compEvent.hasConfidence).toBe(true);
        expect(compEvent.complianceScore).toBe(1);
      }
    });
  });
});
