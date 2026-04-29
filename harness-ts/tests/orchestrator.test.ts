import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Orchestrator, type OrchestratorEvent, type OrchestratorDeps } from "../src/orchestrator.js";
import { SessionManager, type GitOps, type CompletionSignal } from "../src/session/manager.js";
import { SDKClient, type QueryFn } from "../src/session/sdk.js";
import { MergeGate, type MergeGitOps } from "../src/gates/merge.js";
import { StateManager, type TaskRecord } from "../src/lib/state.js";
import type { HarnessConfig } from "../src/lib/config.js";
import type { ReviewGate, ReviewResult, ReviewVerdict } from "../src/gates/review.js";
import type { ArchitectManager, ArchitectVerdict } from "../src/session/architect.js";
import { ProjectStore } from "../src/lib/project.js";
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
    autoCommit: vi.fn((_cwd, _msg, _opts) => "sha1"),
    getHeadSha: vi.fn().mockReturnValue("sha1"),
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
}

interface TestHarness {
  orch: Orchestrator;
  state: StateManager;
  config: HarnessConfig;
  events: OrchestratorEvent[];
  gitOps: GitOps;
  mergeGitOps: MergeGitOps;
  queryFn: QueryFn;
  projectStore?: ProjectStore;
}

function setupHarness(opts?: {
  queryMessages?: SDKMessage[];
  withCompletion?: boolean;
  mergeGitOverrides?: Partial<MergeGitOps>;
  freshQueryPerCall?: boolean;
  withProjectStore?: boolean;
  /** Wave R3 — set project.final_test_command for smoke-gate tests. */
  finalTestCommand?: string;
}): TestHarness {
  const config = makeConfig();
  if (opts?.finalTestCommand !== undefined) {
    config.project.final_test_command = opts.finalTestCommand;
  }
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

  const projectStore = opts?.withProjectStore
    ? new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"))
    : undefined;

  const orch = new Orchestrator({
    sessionManager: sessionMgr,
    mergeGate,
    stateManager: state,
    config,
    projectStore,
  });

  const events: OrchestratorEvent[] = [];
  orch.on((e) => events.push(e));

  return { orch, state, config, events, gitOps, mergeGitOps: mergeGitOps_, queryFn, projectStore };
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

    // --- Wave 1.5b: TaskFile three-tier + mode extensions ---

    it("ingests task file with projectId and phaseId", () => {
      const { orch, state } = setupHarness({ withCompletion: true });
      writeFileSync(
        join(tmpDir, "tasks", "proj-task.json"),
        JSON.stringify({
          id: "proj-task",
          prompt: "phase 1: add logger",
          projectId: "proj-abc",
          phaseId: "phase-1",
        }),
      );

      orch.scanForTasks();

      const task = state.getTask("proj-task");
      expect(task).toBeTruthy();
      expect(task!.prompt).toBe("phase 1: add logger");
      expect(task!.projectId).toBe("proj-abc");
      expect(task!.phaseId).toBe("phase-1");
    });

    it("defaults phaseId to task.id when TaskFile omits it", () => {
      const { orch, state } = setupHarness({ withCompletion: true });
      writeFileSync(
        join(tmpDir, "tasks", "phase-only-proj.json"),
        JSON.stringify({
          id: "phase-only-proj",
          prompt: "single-phase project",
          projectId: "proj-solo",
        }),
      );

      orch.scanForTasks();

      const task = state.getTask("phase-only-proj");
      expect(task!.projectId).toBe("proj-solo");
      expect(task!.phaseId).toBe("phase-only-proj");
    });

    it("rejects task file with projectId + mode:dialogue (Section C.2)", () => {
      const { orch, state } = setupHarness({ withCompletion: true });
      const badPath = join(tmpDir, "tasks", "conflict.json");
      writeFileSync(
        badPath,
        JSON.stringify({
          id: "conflict",
          prompt: "ambiguous",
          projectId: "proj-xyz",
          mode: "dialogue",
        }),
      );

      orch.scanForTasks();

      // No task created; file removed.
      expect(state.getAllTasks()).toHaveLength(0);
      expect(existsSync(badPath)).toBe(false);
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
      const failedEvent = events.find((e) => e.type === "task_failed");
      expect(failedEvent).toBeTruthy();
      // M7 fix: retry-exhaustion path must mark terminal: true so notifier
      // pings the operator regardless of operator-overridden max_session_retries.
      if (failedEvent && failedEvent.type === "task_failed") {
        expect(failedEvent.terminal).toBe(true);
      }
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

    it("emits project_completed when final phase merges (Bug 2: auto-completion)", async () => {
      const { orch, state, events, projectStore } = setupHarness({ withCompletion: true, withProjectStore: true });
      const project = projectStore!.createProject("p-auto", "desc", []);
      projectStore!.addPhase(project.id, "only phase", "phase-a");
      projectStore!.attachTask(project.id, "phase-a", "t-phase-a");
      const task = state.createTask("only phase", "t-phase-a");
      state.updateTask(task.id, { projectId: project.id, phaseId: "phase-a" });
      const refreshed = state.getTask("t-phase-a")!;

      await orch.processTask(refreshed);

      expect(state.getTask("t-phase-a")!.state).toBe("done");
      const completed = events.find((e) => e.type === "project_completed");
      expect(completed).toBeTruthy();
      if (completed && completed.type === "project_completed") {
        expect(completed.projectId).toBe(project.id);
        expect(completed.phaseCount).toBe(1);
      }
      expect(projectStore!.getProject(project.id)!.state).toBe("completed");
    });

    it("does not emit project_completed while active phases remain", async () => {
      const { orch, state, events, projectStore } = setupHarness({ withCompletion: true, withProjectStore: true });
      const project = projectStore!.createProject("p-multi", "d", []);
      projectStore!.addPhase(project.id, "phase 1", "phase-1");
      projectStore!.addPhase(project.id, "phase 2", "phase-2");
      projectStore!.attachTask(project.id, "phase-1", "t-p1");
      const task = state.createTask("phase 1", "t-p1");
      state.updateTask(task.id, { projectId: project.id, phaseId: "phase-1" });
      const refreshed = state.getTask("t-p1")!;

      await orch.processTask(refreshed);

      expect(state.getTask("t-p1")!.state).toBe("done");
      expect(events.some((e) => e.type === "project_completed")).toBe(false);
      expect(projectStore!.getProject(project.id)!.state).toBe("decomposing");
    });

    // Wave R3 — final_test_command smoke gate.
    it("emits project_completed when final_test_command exits 0", async () => {
      const { orch, state, events, projectStore } = setupHarness({
        withCompletion: true,
        withProjectStore: true,
        finalTestCommand: "true",
      });
      const project = projectStore!.createProject("p-smoke-pass", "desc", []);
      projectStore!.addPhase(project.id, "only phase", "phase-a");
      projectStore!.attachTask(project.id, "phase-a", "t-smoke-pass");
      const task = state.createTask("only phase", "t-smoke-pass");
      state.updateTask(task.id, { projectId: project.id, phaseId: "phase-a" });

      await orch.processTask(state.getTask("t-smoke-pass")!);

      expect(events.some((e) => e.type === "project_completed")).toBe(true);
      expect(events.some((e) => e.type === "project_failed")).toBe(false);
      expect(projectStore!.getProject(project.id)!.state).toBe("completed");
    });

    it("emits project_failed when final_test_command exits non-zero", async () => {
      const { orch, state, events, projectStore } = setupHarness({
        withCompletion: true,
        withProjectStore: true,
        finalTestCommand: "echo broken-build && exit 1",
      });
      const project = projectStore!.createProject("p-smoke-fail", "desc", []);
      projectStore!.addPhase(project.id, "only phase", "phase-a");
      projectStore!.attachTask(project.id, "phase-a", "t-smoke-fail");
      const task = state.createTask("only phase", "t-smoke-fail");
      state.updateTask(task.id, { projectId: project.id, phaseId: "phase-a" });

      await orch.processTask(state.getTask("t-smoke-fail")!);

      expect(events.some((e) => e.type === "project_completed")).toBe(false);
      const failed = events.find((e) => e.type === "project_failed");
      expect(failed).toBeTruthy();
      if (failed && failed.type === "project_failed") {
        expect(failed.reason).toContain("Project smoke test failed");
        expect(failed.reason).toContain("broken-build");
      }
      expect(projectStore!.getProject(project.id)!.state).toBe("failed");
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

    // Wave E-δ N8 / H4 — shutdown ordering: nudgeIntrospector.stop() must run
    // BEFORE sessions.abortAll() AND BEFORE the shutdown emit so a final
    // periodic tick cannot race with teardown.
    it("E-δ: calls nudgeIntrospector.stop() before sessions.abortAll() and before emit({type:shutdown})", async () => {
      const callOrder: string[] = [];
      const { orch, events } = setupHarness();
      // Inject a stub introspector via deps mutation through reflection —
      // setupHarness doesn't expose the dep, so build a fresh orchestrator
      // instance with the same deps + nudgeIntrospector.
      const stubIntrospector = {
        start() { /* no-op */ },
        stop() { callOrder.push("nudge.stop"); },
        tick() { /* no-op */ },
        noteStall() { /* no-op */ },
      };
      // Spy on sessions.abortAll
      const sessions = (orch as unknown as { sessions: { abortAll: () => void } }).sessions;
      const origAbort = sessions.abortAll.bind(sessions);
      sessions.abortAll = () => {
        callOrder.push("sessions.abortAll");
        origAbort();
      };
      // Inject the introspector via the private field (test-only).
      (orch as unknown as { nudgeIntrospector: typeof stubIntrospector }).nudgeIntrospector = stubIntrospector;
      // Capture emit order via a listener.
      orch.on((e) => {
        if (e.type === "shutdown") callOrder.push("emit.shutdown");
      });

      orch.start();
      await orch.shutdown();

      // Order: nudge.stop → sessions.abortAll → emit.shutdown
      expect(callOrder).toEqual(["nudge.stop", "sessions.abortAll", "emit.shutdown"]);
      // shutdown event still fires (back-compat).
      expect(events.some((e) => e.type === "shutdown")).toBe(true);
    });

    it("E-δ: shutdown is a no-op for nudgeIntrospector when dep is not provided", async () => {
      // Just confirms no crash when introspector is undefined (the default).
      const { orch } = setupHarness();
      orch.start();
      await expect(orch.shutdown()).resolves.toBeUndefined();
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

  describe("WA-6 crash recovery — merging state", () => {
    function seedMerging(state: StateManager, id: string, worktreePath: string): void {
      state.createTask("work", id);
      state.transition(id, "active");
      state.updateTask(id, { worktreePath, branchName: `harness/task-${id}` });
      state.transition(id, "reviewing");
      state.transition(id, "merging");
    }

    it("transitions merging task to failed when worktreePath is undefined", () => {
      const { orch, state, events } = setupHarness({ withCompletion: false });
      state.createTask("work", "m-noWt");
      state.transition("m-noWt", "active");
      state.transition("m-noWt", "reviewing");
      state.transition("m-noWt", "merging");
      orch.start();
      expect(state.getTask("m-noWt")!.state).toBe("failed");
      expect(state.getTask("m-noWt")!.lastError).toBe("merging_recovery_worktree_missing");
      expect(events.some((e) => e.type === "task_failed" && e.reason === "merging_recovery_worktree_missing")).toBe(true);
      orch.shutdown();
    });

    it("transitions merging task to failed when worktreePath does not exist on disk", () => {
      const { orch, state, events } = setupHarness({ withCompletion: false });
      seedMerging(state, "m-missing", join(tmpDir, "gone", "nope"));
      orch.start();
      expect(state.getTask("m-missing")!.state).toBe("failed");
      expect(state.getTask("m-missing")!.lastError).toBe("merging_recovery_worktree_missing");
      orch.shutdown();
    });

    it("bounds recovery to MAX_RECOVERY_ATTEMPTS (Fresh-2)", () => {
      const { orch, state, events } = setupHarness({ withCompletion: false });
      const wt = join(tmpDir, "worktrees", "task-m-bound");
      mkdirSync(wt, { recursive: true });
      seedMerging(state, "m-bound", wt);
      state.updateTask("m-bound", { recoveryAttempts: 3 });
      orch.start();
      // Increments 3 → 4 (over MAX=3), then marks failed.
      expect(state.getTask("m-bound")!.recoveryAttempts).toBe(4);
      expect(state.getTask("m-bound")!.state).toBe("failed");
      expect(state.getTask("m-bound")!.lastError).toBe("max_recovery_attempts_exceeded");
      expect(
        events.some((e) => e.type === "task_failed" && e.reason === "max_recovery_attempts_exceeded"),
      ).toBe(true);
      orch.shutdown();
    });

    it("increments recoveryAttempts on each merging-recovery pass", () => {
      const { orch, state } = setupHarness({ withCompletion: false });
      const wt = join(tmpDir, "worktrees", "task-m-incr");
      mkdirSync(wt, { recursive: true });
      seedMerging(state, "m-incr", wt);
      orch.start();
      orch.shutdown();
      expect(state.getTask("m-incr")!.recoveryAttempts).toBe(1);
    });

    it("sub-case (b): re-enqueues merging task with alreadyCommitted=true when branch has commits", () => {
      const { orch, state, mergeGitOps } = setupHarness({
        withCompletion: false,
        mergeGitOverrides: {
          branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(true),
        },
      });
      const wt = join(tmpDir, "worktrees", "task-m-b");
      mkdirSync(wt, { recursive: true });
      seedMerging(state, "m-b", wt);
      orch.start();
      // Recovered path re-enqueues; rebase fires, cleanup does not.
      const rebase = mergeGitOps.rebase as ReturnType<typeof vi.fn>;
      expect(rebase).toHaveBeenCalled();
      orch.shutdown();
    });

    it("sub-case (a): re-runs merging task from scratch when branch is empty", () => {
      const { orch, state, gitOps } = setupHarness({
        withCompletion: false,
        mergeGitOverrides: {
          branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(false),
        },
      });
      const wt = join(tmpDir, "worktrees", "task-m-a");
      mkdirSync(wt, { recursive: true });
      seedMerging(state, "m-a", wt);
      orch.start();
      expect(gitOps.removeWorktree).toHaveBeenCalled();
      orch.shutdown();
    });
  });

  describe("WA-6 Fresh-2 / Iteration 4 — recoveryAttempts persistence", () => {
    it("recoveryAttempts round-trips through state file save/load", () => {
      const stateFile = join(tmpDir, "state-persist.json");
      const a = new StateManager(stateFile);
      a.createTask("x", "persist-1");
      a.transition("persist-1", "active");
      a.updateTask("persist-1", { recoveryAttempts: 2 });
      const b = new StateManager(stateFile);
      expect(b.getTask("persist-1")!.recoveryAttempts).toBe(2);
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

// --- Wave A: Review gate + mandatory-for-project + arbitration state wiring ---

/** Fake ReviewGate that returns a pre-configured verdict without spawning SDK. */
function makeFakeReviewGate(
  verdict: ReviewVerdict,
  opts: { arbitrationThreshold?: number; weightedRisk?: number } = {},
): ReviewGate {
  const result: ReviewResult = {
    verdict,
    riskScore: {
      correctness: 0,
      integration: 0,
      stateCorruption: 0,
      performance: 0,
      regression: 0,
      weighted: opts.weightedRisk ?? (verdict === "approve" ? 0.1 : 0.7),
    },
    findings: verdict === "approve" ? [] : [{ severity: "high", file: "f", description: "d" }],
    summary: `fake ${verdict}`,
  };
  return {
    arbitrationThreshold: opts.arbitrationThreshold ?? 2,
    runReview: vi.fn().mockResolvedValue(result),
  } as unknown as ReviewGate;
}

function setupWithReview(opts: {
  reviewGate: ReviewGate;
  withCompletion?: boolean;
  architectManager?: ArchitectManager;
  projectStore?: ProjectStore;
}): TestHarness {
  const config = makeConfig();
  mkdirSync(join(tmpDir, "tasks"), { recursive: true });

  const gitOps = mockGitOps();
  if (opts.withCompletion) {
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

  const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
  const sdk = new SDKClient(queryFn);
  const state = new StateManager(join(tmpDir, "state.json"));
  const sessionMgr = new SessionManager(sdk, state, config, gitOps);
  const mergeGitOps_ = mockMergeGitOps();
  const mergeGate = new MergeGate(config.pipeline, tmpDir, mergeGitOps_);

  const orch = new Orchestrator({
    sessionManager: sessionMgr,
    mergeGate,
    stateManager: state,
    config,
    reviewGate: opts.reviewGate,
    architectManager: opts.architectManager,
    projectStore: opts.projectStore,
  } as OrchestratorDeps);

  const events: OrchestratorEvent[] = [];
  orch.on((e) => events.push(e));

  return { orch, state, config, events, gitOps, mergeGitOps: mergeGitOps_, queryFn };
}

describe("Orchestrator — Wave A review gate + arbitration wiring", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("standalone task with no review trigger goes directly to merge (Phase 2A preserved)", async () => {
    const gate = makeFakeReviewGate("approve");
    const { orch, state, events } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t1");
    await orch.processTask(task);
    // shouldReview default is responseLevel >= 2; minimal completion has no confidence → level 1 → no review
    expect((gate.runReview as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled();
    expect(events.some((e) => e.type === "task_done")).toBe(true);
  });

  it("project task ALWAYS triggers review + emits review_mandatory", async () => {
    const gate = makeFakeReviewGate("approve");
    const { orch, state, events } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-proj");
    state.updateTask("t-proj", { projectId: "proj-abc", phaseId: "phase-1" });
    await orch.processTask(state.getTask("t-proj")!);
    expect((gate.runReview as ReturnType<typeof vi.fn>)).toHaveBeenCalledOnce();
    const reviewMandatory = events.find((e) => e.type === "review_mandatory");
    expect(reviewMandatory).toBeTruthy();
    if (reviewMandatory && reviewMandatory.type === "review_mandatory") {
      expect(reviewMandatory.projectId).toBe("proj-abc");
    }
  });

  it("review approve → merging path + task_done", async () => {
    const gate = makeFakeReviewGate("approve");
    const { orch, state, events } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-ok");
    state.updateTask("t-ok", { projectId: "proj", phaseId: "p1" });
    await orch.processTask(state.getTask("t-ok")!);
    expect(state.getTask("t-ok")!.state).toBe("done");
    expect(events.some((e) => e.type === "task_done")).toBe(true);
  });

  it("review reject + standalone → failed (no retry)", async () => {
    // Rich completion triggers responseLevel ≥ 2 (dialogue) for a standalone task,
    // which fires the review gate. Reject verdict → transition to failed.
    const gate = makeFakeReviewGate("reject");
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const gitOps = mockGitOps();
    (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
      (_b: string, _br: string, wtPath: string) => {
        mkdirSync(join(wtPath, ".harness"), { recursive: true });
        writeFileSync(
          join(wtPath, ".harness", "completion.json"),
          JSON.stringify({
            status: "success",
            commitSha: "abc",
            summary: "ambiguous work",
            filesChanged: ["x.ts"],
            // Confidence with unclear scope + open questions → response level 3 (dialogue).
            confidence: {
              scopeClarity: "unclear",
              designCertainty: "guessing",
              testCoverage: "untestable",
              assumptions: [],
              openQuestions: ["what should this really do?", "is this in scope?"],
            },
          }),
        );
      },
    );
    const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
    const sdk = new SDKClient(queryFn);
    const state = new StateManager(join(tmpDir, "state.json"));
    const sessionMgr = new SessionManager(sdk, state, config, gitOps);
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const orch = new Orchestrator({
      sessionManager: sessionMgr, mergeGate, stateManager: state, config, reviewGate: gate,
    });
    const events: OrchestratorEvent[] = [];
    orch.on((e) => events.push(e));

    const task = state.createTask("ambiguous work", "t-reject-standalone");
    // No projectId — pure standalone.
    await orch.processTask(state.getTask("t-reject-standalone")!);

    expect((gate.runReview as ReturnType<typeof vi.fn>)).toHaveBeenCalledOnce();
    const updated = state.getTask("t-reject-standalone")!;
    expect(updated.state).toBe("failed");
    expect(events.some((e) => e.type === "task_failed" && e.type === "task_failed" && /Review reject/i.test(e.reason))).toBe(true);
    // No retry for standalone
    expect(events.some((e) => e.type === "retry_scheduled")).toBe(false);
  });

  it("review reject + project + count 0 → retry (reviewerRejectionCount becomes 1, shelved for scheduleRetry)", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 2 });
    const { orch, state, events } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-retry");
    state.updateTask("t-retry", { projectId: "proj" });
    await orch.processTask(state.getTask("t-retry")!);
    const updated = state.getTask("t-retry")!;
    expect(updated.reviewerRejectionCount).toBe(1);
    expect(updated.state).toBe("shelved");
    expect(events.some((e) => e.type === "retry_scheduled")).toBe(true);
    // Did NOT enter review_arbitration yet
    expect(events.some((e) => e.type === "review_arbitration_entered")).toBe(false);
  });

  it("review reject + project + count already 1 → threshold crossed → review_arbitration_entered fires (P1-B)", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 2 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "retry_with_directive", directive: "retry cleanly" },
    });
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
    });
    const task = state.createTask("test", "t-arb");
    state.updateTask("t-arb", { projectId: "proj", reviewerRejectionCount: 1 });
    await orch.processTask(state.getTask("t-arb")!);
    const entered = events.find((e) => e.type === "review_arbitration_entered");
    expect(entered).toBeTruthy();
    if (entered && entered.type === "review_arbitration_entered") {
      expect(entered.reviewerRejectionCount).toBe(2);
      expect(entered.projectId).toBe("proj");
    }
    // Architect was consulted.
    expect(architectManager.handleReviewArbitration).toHaveBeenCalledOnce();
  });

  it("arbitration without architectManager → task_failed with explicit reason (P1-B fallback)", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 2 });
    const { orch, state, events } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-warn");
    state.updateTask("t-warn", { projectId: "proj", reviewerRejectionCount: 1 });
    await orch.processTask(state.getTask("t-warn")!);
    expect(state.getTask("t-warn")!.state).toBe("failed");
    const failed = events.find((e) => e.type === "task_failed");
    expect(failed).toBeTruthy();
    if (failed && failed.type === "task_failed") {
      expect(failed.reason).toContain("arbitration_fired_without_architectManager");
    }
  });

  it("request_changes verdict treated same as reject for retry path", async () => {
    const gate = makeFakeReviewGate("request_changes", { arbitrationThreshold: 2 });
    const { orch, state, events } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-rc");
    state.updateTask("t-rc", { projectId: "proj" });
    await orch.processTask(state.getTask("t-rc")!);
    expect(state.getTask("t-rc")!.reviewerRejectionCount).toBe(1);
    expect(state.getTask("t-rc")!.state).toBe("shelved");
    expect(events.some((e) => e.type === "retry_scheduled")).toBe(true);
  });

  it("configurable arbitration_threshold honored (threshold=1 triggers arbitration on first reject)", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "retry_with_directive", directive: "try again" },
    });
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
    });
    const task = state.createTask("test", "t-1");
    state.updateTask("t-1", { projectId: "proj" });
    await orch.processTask(state.getTask("t-1")!);
    expect(events.some((e) => e.type === "review_arbitration_entered")).toBe(true);
    expect(architectManager.handleReviewArbitration).toHaveBeenCalledOnce();
  });

  it("review result persists into task.reviewResult on any verdict", async () => {
    const gate = makeFakeReviewGate("approve", { weightedRisk: 0.15 });
    const { orch, state } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-res");
    state.updateTask("t-res", { projectId: "proj" });
    await orch.processTask(state.getTask("t-res")!);
    const rr = state.getTask("t-res")!.reviewResult;
    expect(rr).toBeTruthy();
    expect(rr!.verdict).toBe("approve");
    expect(rr!.weightedRisk).toBeCloseTo(0.15);
    expect(rr!.findingCount).toBe(0);
  });

  it("reviewerRejectionCount round-trips via state persistence", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 3 });
    const { orch, state } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-rt");
    state.updateTask("t-rt", { projectId: "proj" });
    await orch.processTask(state.getTask("t-rt")!);
    const count = state.getTask("t-rt")!.reviewerRejectionCount;
    const mgr2 = new StateManager(join(tmpDir, "state.json"));
    expect(mgr2.getTask("t-rt")!.reviewerRejectionCount).toBe(count);
  });

  it("orchestrator with NO reviewGate skips review path entirely (backward-compat)", async () => {
    // Construct orchestrator WITHOUT reviewGate — same as Phase 2A.
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const gitOps = mockGitOps();
    (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
      (_b: string, _br: string, wtPath: string) => {
        mkdirSync(join(wtPath, ".harness"), { recursive: true });
        writeFileSync(
          join(wtPath, ".harness", "completion.json"),
          JSON.stringify({
            status: "success",
            commitSha: "x",
            summary: "done",
            filesChanged: ["a.ts"],
          }),
        );
      },
    );
    const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
    const sdk = new SDKClient(queryFn);
    const state = new StateManager(join(tmpDir, "state.json"));
    const sessionMgr = new SessionManager(sdk, state, config, gitOps);
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const orch = new Orchestrator({ sessionManager: sessionMgr, mergeGate, stateManager: state, config });
    const events: OrchestratorEvent[] = [];
    orch.on((e) => events.push(e));
    const task = state.createTask("test", "t-nogate");
    state.updateTask("t-nogate", { projectId: "proj" }); // even project → no review without gate
    await orch.processTask(state.getTask("t-nogate")!);
    expect(events.some((e) => e.type === "task_done")).toBe(true);
    expect(events.some((e) => e.type === "review_mandatory")).toBe(false);
  });

  it("reviewGate.runReview receives correct task + worktreePath + completion", async () => {
    const gate = makeFakeReviewGate("approve");
    const { orch, state } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("my prompt", "t-args");
    state.updateTask("t-args", { projectId: "proj" });
    await orch.processTask(state.getTask("t-args")!);
    const call = (gate.runReview as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[0].id).toBe("t-args");
    expect(call[1]).toContain("task-t-args"); // worktreePath
    expect(call[2].summary).toBe("Fixed it");
  });

  it("reviewGate threshold exposed to handleReviewReject logic matches config", async () => {
    // threshold 5; task starts at count 4; expect exactly one arbitration fire after count=5.
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 5 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "retry_with_directive", directive: "retry once" },
    });
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
    });
    const task = state.createTask("test", "t-thresh");
    state.updateTask("t-thresh", { projectId: "proj", reviewerRejectionCount: 4 });
    await orch.processTask(state.getTask("t-thresh")!);
    const entered = events.find((e) => e.type === "review_arbitration_entered");
    expect(entered).toBeTruthy();
    if (entered && entered.type === "review_arbitration_entered") {
      expect(entered.reviewerRejectionCount).toBe(5);
    }
    expect(architectManager.handleReviewArbitration).toHaveBeenCalledOnce();
  });

  it("approve verdict with zero findings proceeds cleanly without any retry signal", async () => {
    const gate = makeFakeReviewGate("approve");
    const { orch, state, events } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-clean");
    state.updateTask("t-clean", { projectId: "proj" });
    await orch.processTask(state.getTask("t-clean")!);
    expect(events.some((e) => e.type === "retry_scheduled")).toBe(false);
    expect(state.getTask("t-clean")!.state).toBe("done");
  });

  it("review_mandatory emitted BEFORE review runs (ordering)", async () => {
    let runReviewCalledAt = -1;
    const gate = {
      arbitrationThreshold: 2,
      runReview: vi.fn().mockImplementation(async () => {
        runReviewCalledAt = eventsRef.length;
        return {
          verdict: "approve" as const,
          riskScore: { correctness: 0, integration: 0, stateCorruption: 0, performance: 0, regression: 0, weighted: 0.1 },
          findings: [],
          summary: "ok",
        };
      }),
    } as unknown as ReviewGate;
    const eventsRef: OrchestratorEvent[] = [];
    // Can't use setupWithReview since we need eventsRef captured before runReview
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const gitOps = mockGitOps();
    (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
      (_b: string, _br: string, wtPath: string) => {
        mkdirSync(join(wtPath, ".harness"), { recursive: true });
        writeFileSync(
          join(wtPath, ".harness", "completion.json"),
          JSON.stringify({ status: "success", commitSha: "x", summary: "done", filesChanged: [] }),
        );
      },
    );
    const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
    const sdk = new SDKClient(queryFn);
    const state = new StateManager(join(tmpDir, "state.json"));
    const sessionMgr = new SessionManager(sdk, state, config, gitOps);
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const orch = new Orchestrator({ sessionManager: sessionMgr, mergeGate, stateManager: state, config, reviewGate: gate });
    orch.on((e) => eventsRef.push(e));
    const task = state.createTask("test", "t-order");
    state.updateTask("t-order", { projectId: "proj" });
    await orch.processTask(state.getTask("t-order")!);
    // review_mandatory fires BEFORE runReview is invoked
    const mandatoryIdx = eventsRef.findIndex((e) => e.type === "review_mandatory");
    expect(mandatoryIdx).toBeGreaterThanOrEqual(0);
    expect(runReviewCalledAt).toBeGreaterThan(mandatoryIdx);
  });

  it("reject + project logs warning ONLY at threshold, not at count < threshold", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 3 });
    const { orch, state } = setupWithReview({ reviewGate: gate, withCompletion: true });
    const task = state.createTask("test", "t-warn-no");
    state.updateTask("t-warn-no", { projectId: "proj" }); // count 0 → becomes 1, below threshold 3
    const consoleSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    await orch.processTask(state.getTask("t-warn-no")!);
    // Filter out unrelated enrichment-missing warnings from readCompletion (test
    // fixtures intentionally omit Phase 2A enrichment); assert no arbitration warn.
    const arbitrationCalls = consoleSpy.mock.calls.filter((c) =>
      typeof c[0] === "string" && /arbitration/i.test(c[0]),
    );
    expect(arbitrationCalls).toHaveLength(0);
    consoleSpy.mockRestore();
  });
});

// --- Wave B: declareProject + Architect crash recovery ---

function makeFakeArchitectManager(opts: {
  spawnStatus?: "success" | "failure";
  decomposeStatus?: "success" | "failure";
  alive?: boolean;
  respawnStatus?: "success" | "failure";
  reviewVerdict?: ArchitectVerdict;
  escalationVerdict?: ArchitectVerdict;
  reviewThrow?: Error;
} = {}): ArchitectManager {
  const reviewVerdict: ArchitectVerdict =
    opts.reviewVerdict ?? { type: "escalate_operator", rationale: "fake_default" };
  const escalationVerdict: ArchitectVerdict =
    opts.escalationVerdict ?? { type: "escalate_operator", rationale: "fake_default" };
  return {
    spawn: vi.fn().mockResolvedValue(
      opts.spawnStatus === "failure"
        ? { status: "failure", error: "spawn failed" }
        : { status: "success", sessionId: "arch-sess-1" },
    ),
    respawn: vi.fn().mockResolvedValue(
      opts.respawnStatus === "failure"
        ? { status: "failure", error: "respawn failed" }
        : { status: "success", sessionId: "arch-sess-2" },
    ),
    decompose: vi.fn().mockResolvedValue(
      opts.decomposeStatus === "failure"
        ? { status: "failure", error: "no phase files" }
        : {
            status: "success",
            phases: [{ phaseId: "01", taskFilePath: "/tmp/x.json" }, { phaseId: "02", taskFilePath: "/tmp/y.json" }],
          },
    ),
    isAlive: vi.fn().mockReturnValue(opts.alive ?? true),
    getSession: vi.fn().mockReturnValue(undefined),
    shutdown: vi.fn().mockResolvedValue(undefined),
    shutdownAll: vi.fn().mockResolvedValue(undefined),
    handleEscalation: vi.fn().mockImplementation(() => {
      if (opts.reviewThrow) return Promise.reject(opts.reviewThrow);
      return Promise.resolve(escalationVerdict);
    }),
    handleReviewArbitration: vi.fn().mockImplementation(() => {
      if (opts.reviewThrow) return Promise.reject(opts.reviewThrow);
      return Promise.resolve(reviewVerdict);
    }),
    compact: vi.fn().mockResolvedValue({ compacted: false, reason: "stub" }),
    requestSummary: vi.fn().mockResolvedValue({}),
    relayOperatorInput: vi.fn().mockResolvedValue(undefined),
    shouldCompact: vi.fn().mockReturnValue(false),
    persistDecomposedPhases: vi.fn().mockReturnValue([]),
  } as unknown as ArchitectManager;
}

describe("Orchestrator — P1-B arbitration verdict routing", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("retry_with_directive: stores directive, resets reviewerRejectionCount, shelves for retry", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "retry_with_directive", directive: "wrap iteration in null-check" },
    });
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
    });
    state.createTask("original spec", "t-rd");
    state.updateTask("t-rd", { projectId: "proj", phaseId: "ph" });
    await orch.processTask(state.getTask("t-rd")!);
    const updated = state.getTask("t-rd")!;
    expect(updated.lastDirective).toBe("wrap iteration in null-check");
    expect(updated.reviewerRejectionCount).toBe(0);
    // Task is shelved waiting for scheduleRetry tick → pending → processTask.
    expect(updated.state).toBe("shelved");
    // arbitration_verdict event carries the directive as rationale
    const verdictEv = events.find((e) => e.type === "arbitration_verdict");
    expect(verdictEv).toBeTruthy();
    if (verdictEv && verdictEv.type === "arbitration_verdict") {
      expect(verdictEv.verdict).toBe("retry_with_directive");
      expect(verdictEv.rationale).toBe("wrap iteration in null-check");
    }
    // architect_arbitration_fired also emitted, with cause=review_disagreement
    const fired = events.find((e) => e.type === "architect_arbitration_fired");
    expect(fired).toBeTruthy();
    if (fired && fired.type === "architect_arbitration_fired") {
      expect(fired.cause).toBe("review_disagreement");
    }
  });

  it("plan_amendment: updates phase spec via ProjectStore, rewrites prompt, resets count, transitions active", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: {
        type: "plan_amendment",
        updatedPhaseSpec: "NEW SPEC: handle both list and single input",
        rationale: "phase spec ambiguous on input shape",
      },
    });
    // Need a real ProjectStore with the phase pre-registered so updatePhaseSpec can run.
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const proj = projectStore.createProject("p-amend", "desc", []);
    projectStore.addPhase(proj.id, "original spec", "ph-1");
    projectStore.attachTask(proj.id, "ph-1", "t-pa");

    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager, projectStore,
    });
    state.createTask("original spec", "t-pa");
    state.updateTask("t-pa", { projectId: proj.id, phaseId: "ph-1" });

    await orch.processTask(state.getTask("t-pa")!);

    expect(projectStore.getProject(proj.id)!.phases[0].spec).toBe("NEW SPEC: handle both list and single input");
    const updated = state.getTask("t-pa")!;
    expect(updated.prompt).toBe("NEW SPEC: handle both list and single input");
    expect(updated.reviewerRejectionCount).toBe(0);
    expect(updated.state).toBe("shelved");
    const verdictEv = events.find((e) => e.type === "arbitration_verdict");
    expect(verdictEv && verdictEv.type === "arbitration_verdict" && verdictEv.verdict).toBe("plan_amendment");
  });

  it("escalate_operator cascades to projectStore.failProject when no active phases remain", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "escalate_operator", rationale: "unresolvable" },
    });
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const proj = projectStore.createProject("p-single", "d", []);
    projectStore.addPhase(proj.id, "only phase", "ph-only");
    projectStore.attachTask(proj.id, "ph-only", "t-single");
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager, projectStore,
    });
    state.createTask("x", "t-single");
    state.updateTask("t-single", { projectId: proj.id, phaseId: "ph-only" });
    await orch.processTask(state.getTask("t-single")!);

    expect(projectStore.getProject(proj.id)!.state).toBe("failed");
    const pf = events.find((e) => e.type === "project_failed");
    expect(pf).toBeTruthy();
    if (pf && pf.type === "project_failed") {
      expect(pf.reason).toContain("unresolvable");
    }
  });

  it("escalate_operator leaves project open when other phases are still active", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "escalate_operator", rationale: "just this one" },
    });
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const proj = projectStore.createProject("p-multi", "d", []);
    projectStore.addPhase(proj.id, "failing phase", "ph-fail");
    projectStore.addPhase(proj.id, "still pending phase", "ph-pending");
    projectStore.attachTask(proj.id, "ph-fail", "t-multi-fail");
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager, projectStore,
    });
    state.createTask("x", "t-multi-fail");
    state.updateTask("t-multi-fail", { projectId: proj.id, phaseId: "ph-fail" });
    await orch.processTask(state.getTask("t-multi-fail")!);

    expect(projectStore.getProject(proj.id)!.state).toBe("decomposing");
    expect(events.some((e) => e.type === "project_failed")).toBe(false);
    // Phase itself marked failed.
    const ph = projectStore.getProject(proj.id)!.phases.find((p) => p.id === "ph-fail");
    expect(ph?.state).toBe("failed");
  });

  it("escalate_operator no-ops cascade when projectStore is absent", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "escalate_operator", rationale: "no store" },
    });
    // No projectStore injected — legacy orchestrator mode.
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
    });
    state.createTask("x", "t-nostore");
    state.updateTask("t-nostore", { projectId: "proj", phaseId: "ph" });
    await orch.processTask(state.getTask("t-nostore")!);
    expect(state.getTask("t-nostore")!.state).toBe("failed");
    expect(events.some((e) => e.type === "project_failed")).toBe(false);
  });

  it("escalate_operator no-ops cascade for standalone task (no projectId)", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "escalate_operator", rationale: "standalone" },
    });
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager, projectStore,
    });
    state.createTask("x", "t-standalone");
    // No projectId/phaseId — standalone task. Note: routeArbitration also
    // guards standalone above; we still want to confirm cascade is a no-op.
    await orch.processTask(state.getTask("t-standalone")!);
    expect(events.some((e) => e.type === "project_failed")).toBe(false);
  });

  it("escalate_operator with sibling phase in 'active' state leaves project open (hasActivePhases respects active)", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: { type: "escalate_operator", rationale: "active sibling" },
    });
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const proj = projectStore.createProject("p-active-sibling", "d", []);
    projectStore.addPhase(proj.id, "failing phase", "ph-fail");
    projectStore.addPhase(proj.id, "active sibling", "ph-act");
    projectStore.attachTask(proj.id, "ph-fail", "t-fail");
    projectStore.attachTask(proj.id, "ph-act", "t-active"); // sibling already active
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager, projectStore,
    });
    state.createTask("x", "t-fail");
    state.updateTask("t-fail", { projectId: proj.id, phaseId: "ph-fail" });
    await orch.processTask(state.getTask("t-fail")!);
    expect(projectStore.getProject(proj.id)!.state).toBe("decomposing");
    expect(events.some((e) => e.type === "project_failed")).toBe(false);
    const activePhase = projectStore.getProject(proj.id)!.phases.find((p) => p.id === "ph-act");
    expect(activePhase?.state).toBe("active"); // untouched
  });

  it("escalate_operator: transitions failed, emits task_failed with architect rationale", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: {
        type: "escalate_operator",
        rationale: "external API contract ambiguity",
      },
    });
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
    });
    state.createTask("ambiguous spec", "t-eo");
    state.updateTask("t-eo", { projectId: "proj", phaseId: "ph" });
    await orch.processTask(state.getTask("t-eo")!);
    expect(state.getTask("t-eo")!.state).toBe("failed");
    const failed = events.find((e) => e.type === "task_failed");
    expect(failed).toBeTruthy();
    if (failed && failed.type === "task_failed") {
      expect(failed.reason).toContain("external API contract ambiguity");
    }
    // lastError captures the escalation context for operator triage
    expect(state.getTask("t-eo")!.lastError).toContain("architect_escalate_operator");
  });

  it("architectManager.handleReviewArbitration throws → escalate_operator synthesized", async () => {
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewThrow: new Error("network blip"),
    });
    const { orch, state, events } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
    });
    state.createTask("x", "t-throw");
    state.updateTask("t-throw", { projectId: "proj", phaseId: "ph" });
    await orch.processTask(state.getTask("t-throw")!);
    expect(state.getTask("t-throw")!.state).toBe("failed");
    const verdictEv = events.find((e) => e.type === "arbitration_verdict");
    expect(verdictEv).toBeTruthy();
    if (verdictEv && verdictEv.type === "arbitration_verdict") {
      expect(verdictEv.verdict).toBe("escalate_operator");
      expect(verdictEv.rationale).toMatch(/architect_manager_threw.*network blip/);
    }
  });

  it("escalation-source arbitration: routes to Architect when task has projectId (P1-B)", async () => {
    const gate = makeFakeReviewGate("approve"); // review irrelevant — we exit via escalation
    const architectManager = makeFakeArchitectManager({
      escalationVerdict: { type: "retry_with_directive", directive: "re-read the prompt" },
    });
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const gitOps = mockGitOps();
    (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
      (_b: string, _br: string, wtPath: string) => {
        mkdirSync(join(wtPath, ".harness"), { recursive: true });
        writeFileSync(
          join(wtPath, ".harness", "completion.json"),
          JSON.stringify({ status: "success", commitSha: "abc", summary: "S", filesChanged: ["f.ts"] }),
        );
        // Escalation signal takes priority over completion.
        writeFileSync(
          join(wtPath, ".harness", "escalation.json"),
          JSON.stringify({ type: "blocked", question: "which file?" }),
        );
      },
    );
    const queryFn: QueryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
    const sdk = new SDKClient(queryFn);
    const state = new StateManager(join(tmpDir, "state.json"));
    const sessionMgr = new SessionManager(sdk, state, config, gitOps);
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const orch = new Orchestrator({
      sessionManager: sessionMgr, mergeGate, stateManager: state, config,
      reviewGate: gate, architectManager,
    });
    const events: OrchestratorEvent[] = [];
    orch.on((e) => events.push(e));
    state.createTask("x", "t-esc");
    state.updateTask("t-esc", { projectId: "proj", phaseId: "ph" });
    await orch.processTask(state.getTask("t-esc")!);
    expect(architectManager.handleEscalation).toHaveBeenCalledOnce();
    const verdictEv = events.find((e) => e.type === "arbitration_verdict");
    expect(verdictEv && verdictEv.type === "arbitration_verdict" && verdictEv.verdict).toBe("retry_with_directive");
    const fired = events.find((e) => e.type === "architect_arbitration_fired");
    expect(fired && fired.type === "architect_arbitration_fired" && fired.cause).toBe("escalation");
  });

  it("plan_amendment: no-ops ProjectStore call when store is unavailable (still updates task)", async () => {
    // Unusual but legal: plan_amendment verdict returned but projectStore not
    // injected. Task prompt should still update; phase spec update silently skipped.
    const gate = makeFakeReviewGate("reject", { arbitrationThreshold: 1 });
    const architectManager = makeFakeArchitectManager({
      reviewVerdict: {
        type: "plan_amendment",
        updatedPhaseSpec: "alt spec",
        rationale: "fix bad spec",
      },
    });
    const { orch, state } = setupWithReview({
      reviewGate: gate, withCompletion: true, architectManager,
      // no projectStore
    });
    state.createTask("x", "t-nops");
    state.updateTask("t-nops", { projectId: "proj", phaseId: "ph" });
    await orch.processTask(state.getTask("t-nops")!);
    expect(state.getTask("t-nops")!.prompt).toBe("alt spec");
    expect(state.getTask("t-nops")!.state).toBe("shelved");
  });
});

describe("Orchestrator — Wave B declareProject + Architect crash recovery", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("declareProject happy path: project_declared + architect_spawned + project_decomposed", async () => {
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const state = new StateManager(join(tmpDir, "state.json"));
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const sessionMgr = new SessionManager(new SDKClient(vi.fn()), state, config, mockGitOps());
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const architectManager = makeFakeArchitectManager();
    const orch = new Orchestrator({
      sessionManager: sessionMgr, mergeGate, stateManager: state, config, architectManager, projectStore,
    });
    const events: OrchestratorEvent[] = [];
    orch.on((e) => events.push(e));

    const result = await orch.declareProject("test-proj", "desc", ["no ui"]);
    expect("projectId" in result).toBe(true);
    expect(events.some((e) => e.type === "project_declared")).toBe(true);
    expect(events.some((e) => e.type === "architect_spawned")).toBe(true);
    const decomposed = events.find((e) => e.type === "project_decomposed");
    expect(decomposed).toBeTruthy();
    if (decomposed && decomposed.type === "project_decomposed") {
      expect(decomposed.phaseCount).toBe(2);
    }
  });

  it("declareProject returns {error} when architectManager is not configured", async () => {
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const state = new StateManager(join(tmpDir, "state.json"));
    const sessionMgr = new SessionManager(new SDKClient(vi.fn()), state, config, mockGitOps());
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const orch = new Orchestrator({ sessionManager: sessionMgr, mergeGate, stateManager: state, config });
    const result = await orch.declareProject("x", "y", []);
    expect("error" in result).toBe(true);
  });

  it("declareProject emits project_failed when spawn fails", async () => {
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const state = new StateManager(join(tmpDir, "state.json"));
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const sessionMgr = new SessionManager(new SDKClient(vi.fn()), state, config, mockGitOps());
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const architectManager = makeFakeArchitectManager({ spawnStatus: "failure" });
    const orch = new Orchestrator({
      sessionManager: sessionMgr, mergeGate, stateManager: state, config, architectManager, projectStore,
    });
    const events: OrchestratorEvent[] = [];
    orch.on((e) => events.push(e));
    const result = await orch.declareProject("proj", "d", []);
    expect("error" in result).toBe(true);
    expect(events.some((e) => e.type === "project_failed")).toBe(true);
  });

  it("declareProject emits project_failed when decompose fails", async () => {
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const state = new StateManager(join(tmpDir, "state.json"));
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    const sessionMgr = new SessionManager(new SDKClient(vi.fn()), state, config, mockGitOps());
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const architectManager = makeFakeArchitectManager({ decomposeStatus: "failure" });
    const orch = new Orchestrator({
      sessionManager: sessionMgr, mergeGate, stateManager: state, config, architectManager, projectStore,
    });
    const events: OrchestratorEvent[] = [];
    orch.on((e) => events.push(e));
    const result = await orch.declareProject("proj", "d", []);
    expect("error" in result).toBe(true);
    const failed = events.find((e) => e.type === "project_failed");
    expect(failed).toBeTruthy();
    if (failed && failed.type === "project_failed") expect(failed.reason).toMatch(/no phase files|decompose/i);
  });

  it("checkArchitectHealth respawns dead Architect + emits architect_respawned", async () => {
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const state = new StateManager(join(tmpDir, "state.json"));
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    projectStore.createProject("stuck", "d", []);
    const sessionMgr = new SessionManager(new SDKClient(vi.fn()), state, config, mockGitOps());
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    // Architect isAlive returns false → triggers respawn
    const architectManager = makeFakeArchitectManager({ alive: false });
    const orch = new Orchestrator({
      sessionManager: sessionMgr, mergeGate, stateManager: state, config, architectManager, projectStore,
    });
    const events: OrchestratorEvent[] = [];
    orch.on((e) => events.push(e));
    await orch.checkArchitectHealth();
    expect((architectManager.respawn as ReturnType<typeof vi.fn>)).toHaveBeenCalledWith(
      expect.any(String),
      "crash_recovery",
    );
    const respawned = events.find((e) => e.type === "architect_respawned");
    expect(respawned).toBeTruthy();
    if (respawned && respawned.type === "architect_respawned") {
      expect(respawned.reason).toBe("crash_recovery");
    }
  });

  it("checkArchitectHealth skips healthy Architect", async () => {
    const config = makeConfig();
    mkdirSync(join(tmpDir, "tasks"), { recursive: true });
    const state = new StateManager(join(tmpDir, "state.json"));
    const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
    projectStore.createProject("healthy", "d", []);
    const sessionMgr = new SessionManager(new SDKClient(vi.fn()), state, config, mockGitOps());
    const mergeGate = new MergeGate(config.pipeline, tmpDir, mockMergeGitOps());
    const architectManager = makeFakeArchitectManager({ alive: true });
    const orch = new Orchestrator({
      sessionManager: sessionMgr, mergeGate, stateManager: state, config, architectManager, projectStore,
    });
    await orch.checkArchitectHealth();
    expect((architectManager.respawn as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled();
  });
});

describe("Orchestrator — WA-5 formatCommitMessage + enqueueProposed wiring", () => {
  beforeEach(() => {
    tmpDir = makeTmpDir();
  });
  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("routeDirectMerge passes formatted commit message with Model/Session/Phase trailers to enqueueProposed", async () => {
    const { orch, state, mergeGitOps } = setupHarness({
      withCompletion: true,
      mergeGitOverrides: { hasUncommittedChanges: vi.fn().mockReturnValue(true) },
    });
    const task = state.createTask("fix the thing", "t-fmt-1");
    await orch.processTask(task);
    const autoCommit = mergeGitOps.autoCommit as ReturnType<typeof vi.fn>;
    expect(autoCommit).toHaveBeenCalled();
    const [, message] = autoCommit.mock.calls[0];
    expect(message).toMatch(/^harness: t-fmt-1 — /);
    expect(message).toMatch(/\nSession: /);
    expect(message).toMatch(/\nPhase: standalone/);
    expect(message).toMatch(/\nModel: /);
  });

  it("formatCommitMessage subject stays under 100 chars even with long summary", async () => {
    const longSummary = "x".repeat(300);
    const { orch, state, mergeGitOps, gitOps } = setupHarness({
      withCompletion: false,
      mergeGitOverrides: { hasUncommittedChanges: vi.fn().mockReturnValue(true) },
    });
    (gitOps.createWorktree as ReturnType<typeof vi.fn>).mockImplementation(
      (_base: string, _branch: string, wtPath: string) => {
        mkdirSync(join(wtPath, ".harness"), { recursive: true });
        writeFileSync(
          join(wtPath, ".harness", "completion.json"),
          JSON.stringify({
            status: "success",
            commitSha: "abc",
            summary: longSummary,
            filesChanged: ["x.ts"],
          }),
        );
      },
    );
    const task = state.createTask("do", "t-long");
    await orch.processTask(task);
    const autoCommit = mergeGitOps.autoCommit as ReturnType<typeof vi.fn>;
    const [, message] = autoCommit.mock.calls[0];
    const subject = (message as string).split("\n")[0];
    expect(subject.length).toBeLessThanOrEqual(100);
  });

  it("records phaseId in the Phase trailer when task is a project phase", async () => {
    const { orch, state, mergeGitOps } = setupHarness({
      withCompletion: true,
      mergeGitOverrides: { hasUncommittedChanges: vi.fn().mockReturnValue(true) },
    });
    state.createTask("phase work", "t-phase");
    state.updateTask("t-phase", { projectId: "p-1", phaseId: "ph-42" });
    await orch.processTask(state.getTask("t-phase")!);
    const autoCommit = mergeGitOps.autoCommit as ReturnType<typeof vi.fn>;
    const [, message] = autoCommit.mock.calls[0];
    expect(message).toMatch(/\nPhase: ph-42/);
  });
});
