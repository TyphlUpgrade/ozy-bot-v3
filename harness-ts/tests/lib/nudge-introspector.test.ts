/**
 * Wave E-δ — NudgeIntrospector unit tests.
 *
 * Coverage:
 *   - deriveSourceAgent: TaskState mapping + project-state pre-filter
 *   - deriveStatus: blocked > progressing > stagnant precedence
 *   - buildObservations: per-status content
 *   - tick(): emits one nudge_check per project (mocked state + projectStore)
 *   - tick(): skips project after recent noteStall (suppression window)
 *   - start() / stop() idempotent timer hygiene
 *   - it.todo placeholder for orchestrator integration (commit 2a)
 */

import { describe, it, expect, vi, afterEach } from "vitest";

import {
  NudgeIntrospector,
  deriveSourceAgent,
  deriveStatus,
  buildObservations,
  type NudgeStatus,
  type SourceAgent,
} from "../../src/lib/nudge-introspector.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";
import type { ProjectRecord, ProjectPhase } from "../../src/lib/project.js";
import type { TaskRecord, TaskState } from "../../src/lib/state.js";

// --- Helpers ---

function makeProject(
  id: string,
  phases: Array<Partial<ProjectPhase> & { id: string; state: ProjectPhase["state"] }>,
  overrides: Partial<ProjectRecord> = {},
): ProjectRecord {
  return {
    id,
    name: id,
    description: "",
    nonGoals: [],
    state: "executing",
    architectWorktreePath: `/tmp/${id}`,
    compactionGeneration: 0,
    phases: phases.map((p) => ({
      id: p.id,
      taskId: p.taskId,
      state: p.state,
      spec: p.spec ?? "spec",
      reviewerRejectionCount: p.reviewerRejectionCount ?? 0,
      arbitrationCount: p.arbitrationCount ?? 0,
    })),
    totalCostUsd: 0,
    budgetCeilingUsd: 10,
    totalTier1EscalationCount: 0,
    createdAt: "2026-04-27T00:00:00.000Z",
    updatedAt: "2026-04-27T00:00:00.000Z",
    ...overrides,
  };
}

function makeTask(
  id: string,
  state: TaskState,
  updatedAtMs: number,
  overrides: Partial<TaskRecord> = {},
): TaskRecord {
  return {
    id,
    state,
    prompt: "p",
    createdAt: new Date(updatedAtMs).toISOString(),
    updatedAt: new Date(updatedAtMs).toISOString(),
    totalCostUsd: 0,
    retryCount: 0,
    escalationTier: 1,
    rebaseAttempts: 0,
    tier1EscalationCount: 0,
    ...overrides,
  };
}

function tasksToMap(tasks: TaskRecord[]): ReadonlyMap<string, TaskRecord> {
  const m = new Map<string, TaskRecord>();
  for (const t of tasks) m.set(t.id, t);
  return m;
}

// --- deriveSourceAgent ---

describe("deriveSourceAgent", () => {
  const FIXED_NOW = 1_000_000_000_000;

  it("returns 'architect' when project state is decomposing", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }], { state: "decomposing" });
    const tasks = tasksToMap([makeTask("t1", "active", FIXED_NOW)]);
    expect(deriveSourceAgent(project, tasks)).toBe("architect");
  });

  it("returns 'orchestrator' when no active phase exists", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "done", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", "done", FIXED_NOW)]);
    expect(deriveSourceAgent(project, tasks)).toBe("orchestrator");
  });

  it("returns 'orchestrator' when active phase has no taskId", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "pending" }]);
    const tasks = tasksToMap([]);
    expect(deriveSourceAgent(project, tasks)).toBe("orchestrator");
  });

  it("returns 'orchestrator' when task is missing from map", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t-missing" }]);
    const tasks = tasksToMap([]);
    expect(deriveSourceAgent(project, tasks)).toBe("orchestrator");
  });

  const EXEC_STATES: TaskState[] = ["active", "pending", "shelved", "escalation_wait"];
  it.each(EXEC_STATES)("maps TaskState=%s → 'executor'", (state) => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", state, FIXED_NOW)]);
    expect(deriveSourceAgent(project, tasks)).toBe("executor");
  });

  const REVIEW_STATES: TaskState[] = ["reviewing", "review_arbitration"];
  it.each(REVIEW_STATES)("maps TaskState=%s → 'reviewer'", (state) => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", state, FIXED_NOW)]);
    expect(deriveSourceAgent(project, tasks)).toBe("reviewer");
  });

  const ORCH_STATES: TaskState[] = ["merging", "done", "failed", "paused"];
  it.each(ORCH_STATES)("maps TaskState=%s → 'orchestrator'", (state) => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", state, FIXED_NOW)]);
    expect(deriveSourceAgent(project, tasks)).toBe("orchestrator");
  });

  it("picks the highest taskId among active phases (lexicographic stable proxy)", () => {
    const project = makeProject("P1", [
      { id: "ph-1", state: "active", taskId: "t-001" },
      { id: "ph-2", state: "active", taskId: "t-999" }, // should win
    ]);
    const tasks = tasksToMap([
      makeTask("t-001", "reviewing", FIXED_NOW),
      makeTask("t-999", "active", FIXED_NOW),
    ]);
    expect(deriveSourceAgent(project, tasks)).toBe("executor");
  });
});

// --- deriveStatus ---

describe("deriveStatus", () => {
  const FIXED_NOW = 1_000_000_000_000;
  const INTERVAL = 600_000;

  it("returns 'blocked' when any project task is escalation_wait", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", "escalation_wait", FIXED_NOW)]);
    expect(deriveStatus(project, tasks, INTERVAL, FIXED_NOW)).toBe("blocked");
  });

  it("returns 'blocked' when any project task is paused (precedence over progressing)", () => {
    const project = makeProject("P1", [
      { id: "ph-1", state: "active", taskId: "t1" },
      { id: "ph-2", state: "active", taskId: "t2" },
    ]);
    const tasks = tasksToMap([
      makeTask("t1", "merging", FIXED_NOW), // would be progressing
      makeTask("t2", "paused", FIXED_NOW),
    ]);
    expect(deriveStatus(project, tasks, INTERVAL, FIXED_NOW)).toBe("blocked");
  });

  it("returns 'blocked' for shelved + review_arbitration", () => {
    const project = makeProject("P1", [
      { id: "ph-1", state: "active", taskId: "t1" },
      { id: "ph-2", state: "active", taskId: "t2" },
    ]);
    const tasks = tasksToMap([
      makeTask("t1", "review_arbitration", FIXED_NOW),
      makeTask("t2", "shelved", FIXED_NOW),
    ]);
    expect(deriveStatus(project, tasks, INTERVAL, FIXED_NOW)).toBe("blocked");
  });

  it("returns 'progressing' when a recent merging/done/reviewing task exists", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", "done", FIXED_NOW - 60_000)]);
    expect(deriveStatus(project, tasks, INTERVAL, FIXED_NOW)).toBe("progressing");
  });

  it("returns 'stagnant' when only stale activity exists", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", "done", FIXED_NOW - INTERVAL - 1_000)]);
    expect(deriveStatus(project, tasks, INTERVAL, FIXED_NOW)).toBe("stagnant");
  });

  it("returns 'stagnant' when project has no tasks", () => {
    const project = makeProject("P1", []);
    const tasks = tasksToMap([]);
    expect(deriveStatus(project, tasks, INTERVAL, FIXED_NOW)).toBe("stagnant");
  });
});

// --- buildObservations ---

describe("buildObservations", () => {
  const FIXED_NOW = 1_000_000_000_000;

  it("stagnant: emits a single 'no events in {duration}' string", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", "active", FIXED_NOW - 720_000)]);
    const obs = buildObservations("stagnant", project, tasks, FIXED_NOW);
    expect(obs).toHaveLength(1);
    expect(obs[0]).toMatch(/^no events in /);
  });

  it("progressing: emits 'last task <id> done <duration> ago' + remaining-phases count", () => {
    const project = makeProject("P1", [
      { id: "ph-1", state: "done", taskId: "t1" },
      { id: "ph-2", state: "pending", taskId: "t2" },
      { id: "ph-3", state: "pending", taskId: "t3" },
    ]);
    const tasks = tasksToMap([
      makeTask("t1", "done", FIXED_NOW - 30_000),
      makeTask("t2", "pending", FIXED_NOW - 10_000),
      makeTask("t3", "pending", FIXED_NOW - 10_000),
    ]);
    const obs = buildObservations("progressing", project, tasks, FIXED_NOW);
    expect(obs[0]).toContain("last task t1 done");
    expect(obs[0]).toContain("ago");
    expect(obs[1]).toBe("2 phases remaining");
  });

  it("blocked: emits 'stuck in <state>' for the first blocking task", () => {
    const project = makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]);
    const tasks = tasksToMap([makeTask("t1", "escalation_wait", FIXED_NOW)]);
    const obs = buildObservations("blocked", project, tasks, FIXED_NOW);
    expect(obs).toEqual(["stuck in escalation_wait"]);
  });

  it("blocked: emits generic 'stuck' fallback when no blocking task identified", () => {
    const project = makeProject("P1", []);
    const tasks = tasksToMap([]);
    const obs = buildObservations("blocked", project, tasks, FIXED_NOW);
    expect(obs).toEqual(["stuck"]);
  });
});

// --- NudgeIntrospector class ---

describe("NudgeIntrospector", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function setup(opts: {
    projects?: ProjectRecord[];
    tasks?: TaskRecord[];
    intervalMs?: number;
    suppressionMultiplier?: number;
    nowMs?: number;
  } = {}) {
    const projects = opts.projects ?? [];
    const tasks = opts.tasks ?? [];
    const emitted: OrchestratorEvent[] = [];
    const introspector = new NudgeIntrospector({
      state: {
        getTask: (id: string) => tasks.find((t) => t.id === id),
        getAllTasks: () => tasks.slice(),
      },
      projectStore: {
        getAllProjects: () => projects.slice(),
        getProject: (id: string) => projects.find((p) => p.id === id),
      },
      emit: (e) => {
        emitted.push(e);
      },
      intervalMs: opts.intervalMs ?? 600_000,
      stallSuppressionMultiplier: opts.suppressionMultiplier ?? 2,
      now: () => opts.nowMs ?? 1_000_000_000_000,
    });
    return { introspector, emitted };
  }

  describe("tick()", () => {
    it("emits one nudge_check per project (no suppression)", () => {
      const FIXED_NOW = 1_000_000_000_000;
      const { introspector, emitted } = setup({
        projects: [
          makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]),
          makeProject("P2", [{ id: "ph-2", state: "active", taskId: "t2" }]),
        ],
        tasks: [
          makeTask("t1", "active", FIXED_NOW),
          makeTask("t2", "reviewing", FIXED_NOW),
        ],
        nowMs: FIXED_NOW,
      });
      introspector.tick();
      expect(emitted).toHaveLength(2);
      expect(emitted[0]).toMatchObject({ type: "nudge_check", projectId: "P1", sourceAgent: "executor" });
      expect(emitted[1]).toMatchObject({ type: "nudge_check", projectId: "P2", sourceAgent: "reviewer" });
    });

    it("emits no events when project list is empty", () => {
      const { introspector, emitted } = setup({});
      introspector.tick();
      expect(emitted).toHaveLength(0);
    });

    it("populates observations per derived status", () => {
      const FIXED_NOW = 1_000_000_000_000;
      const { introspector, emitted } = setup({
        projects: [makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }])],
        tasks: [makeTask("t1", "escalation_wait", FIXED_NOW)],
        nowMs: FIXED_NOW,
      });
      introspector.tick();
      expect(emitted[0]).toMatchObject({
        type: "nudge_check",
        projectId: "P1",
        status: "blocked",
        observations: ["stuck in escalation_wait"],
        nextAction: undefined,
      });
    });

    it("skips project when noteStall fired within suppression window", () => {
      const FIXED_NOW = 1_000_000_000_000;
      const { introspector, emitted } = setup({
        projects: [makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }])],
        tasks: [makeTask("t1", "active", FIXED_NOW)],
        intervalMs: 600_000,
        suppressionMultiplier: 2,
        nowMs: FIXED_NOW,
      });
      introspector.noteStall("P1");
      introspector.tick();
      expect(emitted).toHaveLength(0);
    });

    it("emits again for a project after suppression window ages out", () => {
      const FIXED_NOW = 1_000_000_000_000;
      let nowMs = FIXED_NOW;
      const projects = [makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }])];
      const tasks = [makeTask("t1", "active", FIXED_NOW)];
      const emitted: OrchestratorEvent[] = [];
      const introspector = new NudgeIntrospector({
        state: {
          getTask: (id) => tasks.find((t) => t.id === id),
          getAllTasks: () => tasks.slice(),
        },
        projectStore: {
          getAllProjects: () => projects.slice(),
          getProject: (id) => projects.find((p) => p.id === id),
        },
        emit: (e) => {
          emitted.push(e);
        },
        intervalMs: 600_000,
        stallSuppressionMultiplier: 2,
        now: () => nowMs,
      });
      introspector.noteStall("P1");
      introspector.tick();
      expect(emitted).toHaveLength(0);
      // Advance past suppression window (2 × 600_000 = 1_200_000).
      nowMs = FIXED_NOW + 1_200_001;
      introspector.tick();
      expect(emitted).toHaveLength(1);
      expect(emitted[0]).toMatchObject({ type: "nudge_check", projectId: "P1" });
    });

    it("snapshots tasks via getAllTasks() once per pass", () => {
      const FIXED_NOW = 1_000_000_000_000;
      const tasks = [makeTask("t1", "active", FIXED_NOW)];
      const projects = [makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }])];
      let getAllCalls = 0;
      const emitted: OrchestratorEvent[] = [];
      const introspector = new NudgeIntrospector({
        state: {
          getTask: (id) => tasks.find((t) => t.id === id),
          getAllTasks: () => {
            getAllCalls += 1;
            return tasks.slice();
          },
        },
        projectStore: {
          getAllProjects: () => projects.slice(),
          getProject: (id) => projects.find((p) => p.id === id),
        },
        emit: (e) => {
          emitted.push(e);
        },
        now: () => FIXED_NOW,
      });
      introspector.tick();
      expect(getAllCalls).toBe(1);
    });
  });

  describe("start() / stop()", () => {
    it("start() schedules an interval; stop() clears it", () => {
      vi.useFakeTimers();
      const { introspector, emitted } = setup({
        projects: [makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }])],
        tasks: [makeTask("t1", "active", 1_000_000_000_000)],
        intervalMs: 1_000,
        nowMs: 1_000_000_000_000,
      });
      introspector.start();
      expect(emitted).toHaveLength(0); // first tick fires AFTER intervalMs
      vi.advanceTimersByTime(1_000);
      expect(emitted).toHaveLength(1);
      introspector.stop();
      vi.advanceTimersByTime(10_000);
      expect(emitted).toHaveLength(1); // no further ticks
    });

    it("start() is idempotent — second call does not schedule a duplicate timer", () => {
      vi.useFakeTimers();
      const { introspector, emitted } = setup({
        projects: [makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }])],
        tasks: [makeTask("t1", "active", 1_000_000_000_000)],
        intervalMs: 1_000,
        nowMs: 1_000_000_000_000,
      });
      introspector.start();
      introspector.start();
      vi.advanceTimersByTime(1_000);
      expect(emitted).toHaveLength(1);
      introspector.stop();
    });

    it("stop() is idempotent — double stop is safe", () => {
      const { introspector } = setup({});
      introspector.start();
      introspector.stop();
      expect(() => introspector.stop()).not.toThrow();
    });

    it("stop() clears the suppression map", () => {
      const FIXED_NOW = 1_000_000_000_000;
      const { introspector, emitted } = setup({
        projects: [makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }])],
        tasks: [makeTask("t1", "active", FIXED_NOW)],
        nowMs: FIXED_NOW,
      });
      introspector.noteStall("P1");
      introspector.tick();
      expect(emitted).toHaveLength(0); // suppressed
      introspector.stop();
      introspector.tick();
      expect(emitted).toHaveLength(1); // suppression map cleared, emits again
    });
  });

  // Type-only sanity: the SourceAgent / NudgeStatus types stay in sync with the
  // OrchestratorEvent union literal types.
  it("types: SourceAgent and NudgeStatus are accepted by an OrchestratorEvent literal", () => {
    const sa: SourceAgent = "architect";
    const st: NudgeStatus = "stagnant";
    const e: OrchestratorEvent = {
      type: "nudge_check",
      projectId: "P",
      sourceAgent: sa,
      status: st,
      observations: [],
    };
    expect(e.type).toBe("nudge_check");
  });

  // Wave E-δ commit 2a — integration: NudgeIntrospector → DiscordNotifier.
  // Confirms the routing chain works end-to-end via a stub notifier (the
  // introspector itself never imports discord — I-1 preserved).
  it("integrates with notifier via emit callback (NudgeIntrospector → notifier.handleEvent)", async () => {
    const FIXED_NOW = 1_000_000_000_000;
    const dispatched: OrchestratorEvent[] = [];
    // Stub notifier: matches the orchestrator wiring contract — introspector
    // emits to a callback the orchestrator hands to it; the orchestrator
    // forwards to notifier.handleEvent in production.
    const stubNotifier = {
      handleEvent(event: OrchestratorEvent) {
        dispatched.push(event);
      },
    };
    const projects = [
      makeProject("P1", [{ id: "ph-1", state: "active", taskId: "t1" }]),
    ];
    const tasks = [makeTask("t1", "active", FIXED_NOW)];
    const introspector = new NudgeIntrospector({
      state: {
        getTask: (id) => tasks.find((t) => t.id === id),
        getAllTasks: () => tasks.slice(),
      },
      projectStore: {
        getAllProjects: () => projects.slice(),
        getProject: (id) => projects.find((p) => p.id === id),
      },
      emit: (e) => stubNotifier.handleEvent(e),
      now: () => FIXED_NOW,
    });
    introspector.tick();
    expect(dispatched).toHaveLength(1);
    expect(dispatched[0]).toMatchObject({
      type: "nudge_check",
      projectId: "P1",
      sourceAgent: "executor",
    });
  });
});
