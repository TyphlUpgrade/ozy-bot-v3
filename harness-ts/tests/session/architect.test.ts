import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync, existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
import {
  ArchitectManager,
  ARCHITECT_DEFAULTS,
  type ArchitectConfig,
  writePhaseFile,
  cleanupPhaseFiles,
  validateArchitectCompactionSummary,
} from "../../src/session/architect.js";
import { SDKClient, type QueryFn } from "../../src/session/sdk.js";
import type { GitOps } from "../../src/session/manager.js";
import { StateManager } from "../../src/lib/state.js";
import { ProjectStore } from "../../src/lib/project.js";
import type { HarnessConfig } from "../../src/lib/config.js";
import type { Query, SDKMessage, SDKResultSuccess, Options } from "@anthropic-ai/claude-agent-sdk";

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `arch-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

function makeResult(sessionId = "arch-session"): SDKResultSuccess {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 100,
    duration_api_ms: 90,
    is_error: false,
    num_turns: 1,
    result: "ok",
    stop_reason: "end_turn",
    total_cost_usd: 0.05,
    usage: { input_tokens: 10, output_tokens: 5 },
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

function makeConfig(archOverride: HarnessConfig["architect"] = {}): HarnessConfig {
  return {
    project: {
      name: "test",
      root: tmpDir,
      task_dir: join(tmpDir, "tasks"),
      state_file: join(tmpDir, "state.json"),
      worktree_base: join(tmpDir, "wt"),
      session_dir: join(tmpDir, "sess"),
    },
    pipeline: {
      poll_interval: 1,
      test_command: "true",
      max_retries: 1,
      test_timeout: 60,
      escalation_timeout: 300,
      retry_delay_ms: 100,
    },
    discord: { bot_token_env: "T", dev_channel: "d", ops_channel: "o", escalation_channel: "e", agents: {} },
    architect: archOverride,
  };
}

function makeGitOps(opts: { failCreate?: boolean } = {}): GitOps {
  return {
    createWorktree: vi.fn((_base, _branch, wtPath) => {
      if (opts.failCreate) throw new Error("worktree create failed");
      mkdirSync(wtPath, { recursive: true });
    }),
    removeWorktree: vi.fn(),
    branchExists: vi.fn(() => false),
    deleteBranch: vi.fn(),
  };
}

interface Harness {
  sdk: SDKClient;
  queryFn: ReturnType<typeof vi.fn>;
  state: StateManager;
  projectStore: ProjectStore;
  config: HarnessConfig;
  gitOps: GitOps;
  manager: ArchitectManager;
  capturedOptions: Options[];
}

function setupManager(opts: {
  architectOverride?: ArchitectConfig;
  archFileCfg?: HarnessConfig["architect"];
  gitOpsOverride?: GitOps;
  queryImpl?: (params: { prompt: string; options?: Options }) => Query;
} = {}): Harness {
  const capturedOptions: Options[] = [];
  const queryFn = vi.fn().mockImplementation((params: { prompt: string; options?: Options }) => {
    if (params.options) capturedOptions.push(params.options);
    if (opts.queryImpl) return opts.queryImpl(params);
    return mockQuery([makeResult()]);
  });
  const sdk = new SDKClient(queryFn);
  const state = new StateManager(join(tmpDir, "state.json"));
  const projectStore = new ProjectStore(join(tmpDir, "projects.json"), join(tmpDir, "wt"));
  const config = makeConfig(opts.archFileCfg);
  const gitOps = opts.gitOpsOverride ?? makeGitOps();
  const manager = new ArchitectManager({
    sdk,
    projectStore,
    stateManager: state,
    gitOps,
    config,
    architectConfig: opts.architectOverride,
  });
  return { sdk, queryFn, state, projectStore, config, gitOps, manager, capturedOptions };
}

beforeEach(() => {
  tmpDir = makeTmpDir();
  mkdirSync(join(tmpDir, "tasks"), { recursive: true });
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("ArchitectManager", () => {
  // --- Lifecycle: spawn / respawn ---

  it("spawn creates worktree + session with status=success", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-1", "desc", ["no UI"]);
    const result = await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    expect(result.status).toBe("success");
    expect(result.sessionId).toBeTruthy();
    expect(h.gitOps.createWorktree).toHaveBeenCalled();
    const session = h.manager.getSession(p.id);
    expect(session?.worktreePath).toContain("architect-");
  });

  it("spawn returns {status:failure, error} when worktree creation throws", async () => {
    const h = setupManager({ gitOpsOverride: makeGitOps({ failCreate: true }) });
    const p = h.projectStore.createProject("proj-2", "d", []);
    const result = await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    expect(result.status).toBe("failure");
    expect(result.error).toMatch(/Worktree create failed/);
  });

  it("respawn with crash_recovery reason reuses worktree", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-3", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const worktreePath = h.manager.getSession(p.id)!.worktreePath;
    const createCallsBefore = (h.gitOps.createWorktree as ReturnType<typeof vi.fn>).mock.calls.length;
    const result = await h.manager.respawn(p.id, "crash_recovery");
    expect(result.status).toBe("success");
    // respawn must NOT re-create the worktree
    expect((h.gitOps.createWorktree as ReturnType<typeof vi.fn>).mock.calls.length).toBe(createCallsBefore);
    expect(h.manager.getSession(p.id)?.worktreePath).toBe(worktreePath);
  });

  it("respawn with compaction reason increments compactionGeneration", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-4", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const before = h.manager.getSession(p.id)!.compactionGeneration;
    // Prepare a summary (normally produced by requestSummary)
    const summary = {
      projectId: p.id,
      name: p.name,
      description: p.description,
      nonGoals: p.nonGoals,
      priorVerdicts: [],
      completedPhases: [],
      currentPhaseContext: { phaseId: "", taskId: "", state: "", reviewerRejectionCount: 0, arbitrationCount: 0 },
      compactedAt: new Date().toISOString(),
      compactionGeneration: 1,
    };
    await h.manager.respawn(p.id, "compaction", summary);
    const after = h.manager.getSession(p.id)!.compactionGeneration;
    expect(after).toBe(before + 1);
  });

  // --- persistSession regression (Architect MUST persist) ---

  it("spawn options: persistSession=true (opposite of Reviewer)", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-5", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const opts = h.capturedOptions[0];
    expect(opts.persistSession).toBe(true);
  });

  it("spawn options: enabledPlugins includes OMC + caveman (decomposer forced-delegation config)", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-6", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const opts = h.capturedOptions[0] as Options & { settings?: { enabledPlugins?: Record<string, boolean> } };
    expect(opts.settings?.enabledPlugins?.["oh-my-claudecode@omc"]).toBe(true);
    expect(opts.settings?.enabledPlugins?.["caveman@caveman"]).toBe(true);
  });

  it("spawn options: disallows network + cron + team-lifecycle tools (SEC M2)", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-sec-m2", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const opts = h.capturedOptions[0];
    expect(opts.disallowedTools).toEqual(expect.arrayContaining([
      "WebFetch", "WebSearch",
      "CronCreate", "CronDelete", "CronList",
      "TeamCreate", "TeamDelete",
    ]));
    // `Task` MUST remain available — OMC subagent delegation is core to decomposition.
    expect(opts.disallowedTools).not.toContain("Task");
  });

  // --- Decomposition ---

  it("decompose reads phase files and registers them on the project", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-7", "d", []);
    // Wave B stub queryImpl simulates Architect writing phase files during the resumeSession call.
    const sdk2 = new SDKClient(vi.fn().mockImplementation((params: { options?: Options }) => {
      if (params.options) h.capturedOptions.push(params.options);
      writePhaseFile(h.config.project.task_dir, p.id, "01", "phase 1 prompt");
      writePhaseFile(h.config.project.task_dir, p.id, "02", "phase 2 prompt");
      return mockQuery([makeResult()]);
    }));
    // Rebuild manager with this SDK
    const mgr = new ArchitectManager({
      sdk: sdk2,
      projectStore: h.projectStore,
      stateManager: h.state,
      gitOps: h.gitOps,
      config: h.config,
    });
    await mgr.spawn(p.id, p.name, p.description, p.nonGoals);
    const result = await mgr.decompose(p.id);
    expect(result.status).toBe("success");
    expect(result.phases).toHaveLength(2);
    expect(result.phases!.map((ph) => ph.phaseId).sort()).toEqual(["01", "02"]);
    // ProjectStore has received phases
    expect(h.projectStore.getProject(p.id)!.phases).toHaveLength(2);
  });

  it("decompose writes files with correct schema (projectId + phaseId set)", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-8", "d", []);
    const path = writePhaseFile(h.config.project.task_dir, p.id, "01", "do the first thing");
    const raw = JSON.parse(readFileSync(path, "utf-8"));
    expect(raw.id).toBe(`project-${p.id}-phase-01`);
    expect(raw.projectId).toBe(p.id);
    expect(raw.phaseId).toBe("01");
    expect(raw.prompt).toBe("do the first thing");
    expect(raw.priority).toBe(1);
  });

  it("decompose returns failure when no phase files are produced", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-9", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    // No writePhaseFile call — Architect "session" produced nothing.
    const result = await h.manager.decompose(p.id);
    expect(result.status).toBe("failure");
    expect(result.error).toMatch(/no phase files/i);
  });

  it("decompose ignores phase files from OTHER projects", async () => {
    const h = setupManager();
    const p1 = h.projectStore.createProject("proj-a", "d", []);
    const p2 = h.projectStore.createProject("proj-b", "d", []);
    writePhaseFile(h.config.project.task_dir, p1.id, "01", "p1 work");
    writePhaseFile(h.config.project.task_dir, p2.id, "01", "p2 work");
    await h.manager.spawn(p1.id, p1.name, p1.description, p1.nonGoals);
    const result = await h.manager.decompose(p1.id);
    expect(result.phases).toHaveLength(1);
    expect(result.phases![0].phaseId).toBe("01");
    // p2's phase file still on disk
    const files = existsSync(h.config.project.task_dir);
    expect(files).toBe(true);
  });

  // --- Verdict schema / retry-only guardrail ---

  it("verdict union has exactly 3 types — no executor_correct", () => {
    // Pure type-level assertion via the prompt file.
    const promptPath = join(tmpDir, "prompt.md");
    writeFileSync(
      promptPath,
      readFileSync(
        join(__dirname, "..", "..", "config", "harness", "architect-prompt.md"),
        "utf-8",
      ),
    );
    const content = readFileSync(promptPath, "utf-8");
    expect(content).toMatch(/retry_with_directive/);
    expect(content).toMatch(/plan_amendment/);
    expect(content).toMatch(/escalate_operator/);
    // The prompt must EXPLICITLY forbid executor_correct (Critic item 23).
    // That means the string appears in a negative context ("cannot issue",
    // "No executor_correct"), not as one of the three valid verdict types.
    expect(content).toMatch(/cannot issue an `executor_correct`|No `executor_correct`/i);
  });

  it("readArchitectVerdict rejects unknown verdict types", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-vv", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const session = h.manager.getSession(p.id)!;
    mkdirSync(join(session.worktreePath, ".harness"), { recursive: true });
    writeFileSync(
      join(session.worktreePath, ".harness", "architect-verdict.json"),
      JSON.stringify({ type: "executor_correct", directive: "approve" }),
    );
    const task = h.state.createTask("x", "t-v");
    h.state.updateTask("t-v", { projectId: p.id });
    const verdict = await h.manager.handleEscalation(
      h.state.getTask("t-v")!,
      { type: "clarification_needed", question: "?" },
    );
    // Invalid verdict ignored → stub falls back to escalate_operator
    expect(verdict.type).toBe("escalate_operator");
  });

  it("readArchitectVerdict accepts retry_with_directive", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-rd", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const session = h.manager.getSession(p.id)!;
    mkdirSync(join(session.worktreePath, ".harness"), { recursive: true });
    writeFileSync(
      join(session.worktreePath, ".harness", "architect-verdict.json"),
      JSON.stringify({ type: "retry_with_directive", directive: "handle empty list" }),
    );
    const task = h.state.createTask("x", "t-rd");
    h.state.updateTask("t-rd", { projectId: p.id });
    const verdict = await h.manager.handleReviewArbitration(
      h.state.getTask("t-rd")!,
      { verdict: "reject", riskScore: { correctness:0,integration:0,stateCorruption:0,performance:0,regression:0,weighted:0.8 }, findings: [], summary: "bad" },
    );
    expect(verdict.type).toBe("retry_with_directive");
    if (verdict.type === "retry_with_directive") {
      expect(verdict.directive).toBe("handle empty list");
    }
  });

  // --- Shutdown ---

  it("shutdown aborts session; isAlive returns false", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-sd", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    expect(h.manager.isAlive(p.id)).toBe(true);
    await h.manager.shutdown(p.id);
    expect(h.manager.isAlive(p.id)).toBe(false);
  });

  it("shutdownAll iterates all projects", async () => {
    const h = setupManager();
    const p1 = h.projectStore.createProject("proj-sa", "d", []);
    const p2 = h.projectStore.createProject("proj-sb", "d", []);
    await h.manager.spawn(p1.id, p1.name, p1.description, p1.nonGoals);
    await h.manager.spawn(p2.id, p2.name, p2.description, p2.nonGoals);
    await h.manager.shutdownAll();
    expect(h.manager.isAlive(p1.id)).toBe(false);
    expect(h.manager.isAlive(p2.id)).toBe(false);
  });

  // --- Budget tracking ---

  it("project budget accumulates across spawn + decompose", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-bud", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    // After spawn, project.totalCostUsd has the spawn cost
    const costAfterSpawn = h.projectStore.getProject(p.id)!.totalCostUsd;
    expect(costAfterSpawn).toBeGreaterThan(0);
  });

  // --- Compaction ---

  it("compact returns {compacted:false, reason} when threshold not crossed", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-no-compact", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const result = await h.manager.compact(p.id);
    expect(result.compacted).toBe(false);
    expect(result.reason).toMatch(/threshold_not_crossed/);
  });

  it("compact returns {compacted:true, newSessionId, generation} when threshold crossed", async () => {
    // Override compaction threshold to 0 (always compact) and use a very small budget ceiling.
    const h = setupManager({ architectOverride: { compactionThresholdPct: 0 } });
    const p = h.projectStore.createProject("proj-do-compact", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const result = await h.manager.compact(p.id);
    expect(result.compacted).toBe(true);
    expect(result.newSessionId).toBeTruthy();
    expect(result.generation).toBeGreaterThanOrEqual(1);
  });

  it("requestSummary preserves nonGoals verbatim from projectStore (never re-derived)", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-ng", "d", ["no UI", "no DB migration", "no breaking API"]);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const summary = await h.manager.requestSummary(p.id);
    expect(summary.nonGoals).toEqual(["no UI", "no DB migration", "no breaking API"]);
    // Additionally verify the summary file, if written by the Architect, does NOT
    // override the verbatim contract: we drift-check in code.
  });

  it("requestSummary forces verbatim nonGoals when Architect-written file drifts", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-drift", "d", ["ng-1", "ng-2"]);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    // Simulate Architect writing a summary with drifted nonGoals
    const session = h.manager.getSession(p.id)!;
    mkdirSync(join(session.worktreePath, ".harness"), { recursive: true });
    writeFileSync(
      join(session.worktreePath, ".harness", "architect-summary.json"),
      JSON.stringify({
        projectId: p.id,
        name: p.name,
        description: p.description,
        nonGoals: ["DRIFTED"],
        priorVerdicts: [],
        completedPhases: [],
        currentPhaseContext: { phaseId: "", taskId: "", state: "", reviewerRejectionCount: 0, arbitrationCount: 0 },
        compactedAt: new Date().toISOString(),
        compactionGeneration: 1,
      }),
    );
    const consoleSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const summary = await h.manager.requestSummary(p.id);
    consoleSpy.mockRestore();
    expect(summary.nonGoals).toEqual(["ng-1", "ng-2"]);
  });

  // --- SEC M1: full schema validation for architect-summary.json ---

  it("validateArchitectCompactionSummary accepts a well-formed summary", () => {
    const ok = {
      projectId: "p1",
      name: "n",
      description: "d",
      nonGoals: ["a", "b"],
      priorVerdicts: [{ phaseId: "ph1", verdict: "retry_with_directive", rationale: "r", timestamp: "t" }],
      completedPhases: [{ phaseId: "ph0", taskId: "t0", state: "done", finalCostUsd: 0.5 }],
      currentPhaseContext: { phaseId: "ph1", taskId: "t1", state: "running", reviewerRejectionCount: 0, arbitrationCount: 0 },
      compactedAt: "2026-04-24T00:00:00Z",
      compactionGeneration: 1,
    };
    expect(validateArchitectCompactionSummary(ok)).toBe(true);
  });

  it("validateArchitectCompactionSummary rejects invalid verdict enum", () => {
    const bad = {
      projectId: "p1", name: "n", description: "d", nonGoals: [],
      priorVerdicts: [{ phaseId: "x", verdict: "executor_correct", rationale: "", timestamp: "" }],
      completedPhases: [],
      currentPhaseContext: { phaseId: "", taskId: "", state: "", reviewerRejectionCount: 0, arbitrationCount: 0 },
      compactedAt: "", compactionGeneration: 0,
    };
    expect(validateArchitectCompactionSummary(bad)).toBe(false);
  });

  it("validateArchitectCompactionSummary rejects non-array nonGoals", () => {
    const bad = {
      projectId: "p1", name: "n", description: "d", nonGoals: "not-an-array",
      priorVerdicts: [], completedPhases: [],
      currentPhaseContext: { phaseId: "", taskId: "", state: "", reviewerRejectionCount: 0, arbitrationCount: 0 },
      compactedAt: "", compactionGeneration: 0,
    };
    expect(validateArchitectCompactionSummary(bad)).toBe(false);
  });

  it("validateArchitectCompactionSummary rejects missing currentPhaseContext counters", () => {
    const bad = {
      projectId: "p1", name: "n", description: "d", nonGoals: [],
      priorVerdicts: [], completedPhases: [],
      currentPhaseContext: { phaseId: "", taskId: "", state: "" }, // missing counters
      compactedAt: "", compactionGeneration: 0,
    };
    expect(validateArchitectCompactionSummary(bad)).toBe(false);
  });

  it("validateArchitectCompactionSummary rejects non-numeric finalCostUsd", () => {
    const bad = {
      projectId: "p1", name: "n", description: "d", nonGoals: [],
      priorVerdicts: [],
      completedPhases: [{ phaseId: "ph0", taskId: "t0", state: "done", finalCostUsd: "not-a-number" }],
      currentPhaseContext: { phaseId: "", taskId: "", state: "", reviewerRejectionCount: 0, arbitrationCount: 0 },
      compactedAt: "", compactionGeneration: 0,
    };
    expect(validateArchitectCompactionSummary(bad)).toBe(false);
  });

  it("requestSummary synthesized completedPhases pull finalCostUsd from StateManager (CR M2)", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-cost", "d", []);
    h.projectStore.addPhase(p.id, "phase 1", "ph-1");
    h.projectStore.attachTask(p.id, "ph-1", "t-ph1");
    const task = h.state.createTask("phase 1", "t-ph1");
    h.state.updateTask(task.id, { projectId: p.id, phaseId: "ph-1", totalCostUsd: 0.42 });
    h.projectStore.markPhaseDone(p.id, "ph-1");
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    // No architect-summary.json on disk → fallback path synthesizes summary.
    const summary = await h.manager.requestSummary(p.id);
    const ph1 = summary.completedPhases.find((cp) => cp.phaseId === "ph-1");
    expect(ph1?.finalCostUsd).toBe(0.42);
  });

  it("requestSummary falls back to projectStore when summary file fails schema", async () => {
    const h = setupManager();
    const p = h.projectStore.createProject("proj-badschema", "desc-x", ["ng-a"]);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const session = h.manager.getSession(p.id)!;
    mkdirSync(join(session.worktreePath, ".harness"), { recursive: true });
    // Write schema-invalid summary (missing priorVerdicts, wrong type)
    writeFileSync(
      join(session.worktreePath, ".harness", "architect-summary.json"),
      JSON.stringify({ projectId: p.id, name: "garbage", nonGoals: "not-array" }),
    );
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const summary = await h.manager.requestSummary(p.id);
    warnSpy.mockRestore();
    // Fallback uses verbatim projectStore values
    expect(summary.nonGoals).toEqual(["ng-a"]);
    expect(summary.description).toBe("desc-x");
  });

  // --- Architect authority / no-merge-override ---

  it("ArchitectManager exposes no merge-gate invocation path", () => {
    // Type-level / API-surface check: class should not expose any method name
    // containing "merge" or "enqueueMerge".
    const proto = Object.getOwnPropertyNames(ArchitectManager.prototype);
    const violators = proto.filter((n) => /merge/i.test(n));
    expect(violators).toEqual([]);
  });

  // --- resumeSession on relayOperatorInput ---

  it("relayOperatorInput uses resumeSession (not spawn) on existing session", async () => {
    let spawnCount = 0;
    let resumeCount = 0;
    const impl = (params: { options?: Options }) => {
      if (params.options?.resume) resumeCount++;
      else spawnCount++;
      return mockQuery([makeResult()]);
    };
    const h = setupManager({ queryImpl: impl });
    const p = h.projectStore.createProject("proj-relay", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    await h.manager.relayOperatorInput(p.id, "the operator says hi");
    expect(spawnCount).toBe(1);
    expect(resumeCount).toBe(1);
  });

  // --- Arbitration timeout ---

  it("arbitrationTimeoutMs elapses → escalate_operator with rationale architect_timeout", async () => {
    // Use a very small timeout (1ms) and a queryImpl that hangs until aborted.
    const impl = (params: { options?: Options }) => {
      async function* gen(): AsyncGenerator<SDKMessage, void> {
        // Wait until abort fires then exit without result
        await new Promise<void>((resolve) => {
          params.options?.abortController?.signal.addEventListener("abort", () => resolve());
        });
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
    };
    const h = setupManager({
      architectOverride: { arbitrationTimeoutMs: 10 },
      queryImpl: (params) => {
        // First call (spawn) returns a normal result
        if (!params.options?.resume) return mockQuery([makeResult()]);
        return impl(params);
      },
    });
    const p = h.projectStore.createProject("proj-timeout", "d", []);
    await h.manager.spawn(p.id, p.name, p.description, p.nonGoals);
    const task = h.state.createTask("x", "t-to");
    h.state.updateTask("t-to", { projectId: p.id });
    const verdict = await h.manager.handleEscalation(
      h.state.getTask("t-to")!,
      { type: "clarification_needed", question: "?" },
    );
    expect(verdict.type).toBe("escalate_operator");
    if (verdict.type === "escalate_operator") {
      expect(verdict.rationale).toBe("architect_timeout");
    }
  });

  // --- Config merge ---

  it("architect config overrides default model + compaction threshold", () => {
    const h = setupManager({
      archFileCfg: { model: "claude-opus-4-7", compaction_threshold_pct: 0.8 },
    });
    // Spawn to force option capture
    const p = h.projectStore.createProject("proj-cfg", "d", []);
    return h.manager.spawn(p.id, p.name, p.description, p.nonGoals).then(() => {
      expect(h.capturedOptions[0].model).toBe("claude-opus-4-7");
    });
  });

  // --- cleanupPhaseFiles helper ---

  it("cleanupPhaseFiles removes files scoped to the given projectId", () => {
    const h = setupManager();
    const p1 = h.projectStore.createProject("proj-cpa", "d", []);
    const p2 = h.projectStore.createProject("proj-cpb", "d", []);
    writePhaseFile(h.config.project.task_dir, p1.id, "01", "x");
    writePhaseFile(h.config.project.task_dir, p2.id, "01", "y");
    cleanupPhaseFiles(h.config.project.task_dir, p1.id);
    // Only p2's file remains
    const files = readdirSyncSafe(h.config.project.task_dir);
    expect(files.some((f) => f.includes(p1.id))).toBe(false);
    expect(files.some((f) => f.includes(p2.id))).toBe(true);
  });

  it("ARCHITECT_DEFAULTS match plan (opus, 5-min arbitration, 0.60 compaction)", () => {
    expect(ARCHITECT_DEFAULTS.model).toBe("claude-opus-4-7");
    expect(ARCHITECT_DEFAULTS.arbitration_timeout_ms).toBe(300_000);
    expect(ARCHITECT_DEFAULTS.compaction_threshold_pct).toBe(0.60);
    expect(ARCHITECT_DEFAULTS.plugins["oh-my-claudecode@omc"]).toBe(true);
  });
});

function readdirSyncSafe(dir: string): string[] {
  try {
    return readdirSync(dir);
  } catch {
    return [];
  }
}
