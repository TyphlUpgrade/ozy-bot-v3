import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync, existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { ProjectStore, type ArchitectCompactionSummary } from "../../src/lib/project.js";

let tmpDir: string;
let statePath: string;
let worktreeBase: string;

function freshStore(): ProjectStore {
  return new ProjectStore(statePath, worktreeBase);
}

describe("ProjectStore", () => {
  beforeEach(() => {
    tmpDir = join(tmpdir(), `harness-project-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    mkdirSync(tmpDir, { recursive: true });
    statePath = join(tmpDir, "projects.json");
    worktreeBase = join(tmpDir, "worktrees");
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  // --- Creation ---

  it("creates a project with nonGoals array", () => {
    const store = freshStore();
    const proj = store.createProject("auth-rewrite", "Replace legacy auth", ["no UI changes", "no DB migration"]);
    expect(proj.id).toBeTruthy();
    expect(proj.name).toBe("auth-rewrite");
    expect(proj.nonGoals).toEqual(["no UI changes", "no DB migration"]);
    expect(proj.state).toBe("decomposing");
    expect(proj.compactionGeneration).toBe(0);
    expect(proj.phases).toEqual([]);
    expect(proj.totalCostUsd).toBe(0);
    expect(proj.architectWorktreePath).toBe(join(worktreeBase, `architect-${proj.id}`));
  });

  it("permits empty nonGoals array", () => {
    const store = freshStore();
    const proj = store.createProject("p", "desc", []);
    expect(proj.nonGoals).toEqual([]);
  });

  it("rejects non-array nonGoals (e.g. undefined)", () => {
    const store = freshStore();
    expect(() =>
      store.createProject("p", "desc", undefined as unknown as string[]),
    ).toThrow(/nonGoals must be an array/);
  });

  // --- Phases ---

  it("addPhase appends to phases[] in pending state", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    const phaseId = store.addPhase(proj.id, "spec-1");
    expect(phaseId).toBeTruthy();
    const reloaded = store.getProject(proj.id)!;
    expect(reloaded.phases).toHaveLength(1);
    expect(reloaded.phases[0].id).toBe(phaseId);
    expect(reloaded.phases[0].state).toBe("pending");
    expect(reloaded.phases[0].spec).toBe("spec-1");
    expect(reloaded.phases[0].reviewerRejectionCount).toBe(0);
    expect(reloaded.phases[0].arbitrationCount).toBe(0);
  });

  it("attachTask binds taskId and transitions phase to active", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    const phaseId = store.addPhase(proj.id, "spec");
    store.attachTask(proj.id, phaseId, "task-xyz");
    const phase = store.getProject(proj.id)!.phases[0];
    expect(phase.taskId).toBe("task-xyz");
    expect(phase.state).toBe("active");
  });

  it("updatePhaseSpec replaces phase.spec (P1-C)", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    const phaseId = store.addPhase(proj.id, "original spec");
    store.updatePhaseSpec(proj.id, phaseId, "amended spec — handle empty list");
    expect(store.getProject(proj.id)!.phases[0].spec).toBe("amended spec — handle empty list");
  });

  it("updatePhaseSpec throws on unknown project", () => {
    const store = freshStore();
    expect(() => store.updatePhaseSpec("missing", "ph", "x")).toThrow();
  });

  it("updatePhaseSpec throws on unknown phase", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    expect(() => store.updatePhaseSpec(proj.id, "missing", "x")).toThrow();
  });

  it("markPhaseDone / markPhaseFailed update phase state", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    const a = store.addPhase(proj.id, "a");
    const b = store.addPhase(proj.id, "b");
    store.markPhaseDone(proj.id, a);
    store.markPhaseFailed(proj.id, b, "explicit reason");
    const phases = store.getProject(proj.id)!.phases;
    expect(phases.find((p) => p.id === a)!.state).toBe("done");
    expect(phases.find((p) => p.id === b)!.state).toBe("failed");
  });

  // --- Cost + escalation counters ---

  it("incrementCost accumulates totalCostUsd", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    store.incrementCost(proj.id, 0.5);
    store.incrementCost(proj.id, 0.25);
    expect(store.getProject(proj.id)!.totalCostUsd).toBeCloseTo(0.75);
  });

  it("cost ceiling boundary check (caller-side) — totalCostUsd approaches budgetCeilingUsd", () => {
    // ProjectStore does not enforce ceiling (orchestrator precheck per Section C.4 does).
    // This test verifies the fields the precheck depends on are correctly persisted/read.
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    expect(proj.budgetCeilingUsd).toBe(10); // default
    store.incrementCost(proj.id, 9.99);
    const reloaded = store.getProject(proj.id)!;
    expect(reloaded.totalCostUsd).toBeCloseTo(9.99);
    expect(reloaded.totalCostUsd < reloaded.budgetCeilingUsd).toBe(true);
  });

  it("incrementTier1Escalation increments and returns new total", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    expect(store.incrementTier1Escalation(proj.id)).toBe(1);
    expect(store.incrementTier1Escalation(proj.id)).toBe(2);
    expect(store.getProject(proj.id)!.totalTier1EscalationCount).toBe(2);
  });

  // --- hasActivePhases ---

  it("hasActivePhases reflects pending and active phases only", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    expect(store.hasActivePhases(proj.id)).toBe(false); // no phases yet
    const a = store.addPhase(proj.id, "a"); // pending → active by criteria
    expect(store.hasActivePhases(proj.id)).toBe(true);
    store.attachTask(proj.id, a, "task-1"); // active
    expect(store.hasActivePhases(proj.id)).toBe(true);
    store.markPhaseDone(proj.id, a); // done → not active
    expect(store.hasActivePhases(proj.id)).toBe(false);
  });

  it("hasActivePhases returns false for unknown project id", () => {
    const store = freshStore();
    expect(store.hasActivePhases("nonexistent")).toBe(false);
  });

  // --- Terminal state transitions ---

  it("completeProject marks state=completed and sets completedAt", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    store.completeProject(proj.id);
    const reloaded = store.getProject(proj.id)!;
    expect(reloaded.state).toBe("completed");
    expect(reloaded.completedAt).toBeTruthy();
  });

  it("failProject marks state=failed and sets completedAt", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    store.failProject(proj.id, "budget exceeded");
    expect(store.getProject(proj.id)!.state).toBe("failed");
    expect(store.getProject(proj.id)!.completedAt).toBeTruthy();
  });

  it("abortProject marks state=aborted and sets completedAt", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    store.abortProject(proj.id);
    expect(store.getProject(proj.id)!.state).toBe("aborted");
    expect(store.getProject(proj.id)!.completedAt).toBeTruthy();
  });

  // --- Persistence + corruption ---

  it("persists to disk and round-trips via a fresh instance", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", ["ng"]);
    const phaseId = store.addPhase(proj.id, "spec");
    store.attachTask(proj.id, phaseId, "task-a");
    store.incrementCost(proj.id, 1.23);
    store.incrementTier1Escalation(proj.id);

    const store2 = freshStore();
    const reloaded = store2.getProject(proj.id)!;
    expect(reloaded.id).toBe(proj.id);
    expect(reloaded.nonGoals).toEqual(["ng"]);
    expect(reloaded.phases).toHaveLength(1);
    expect(reloaded.phases[0].taskId).toBe("task-a");
    expect(reloaded.totalCostUsd).toBeCloseTo(1.23);
    expect(reloaded.totalTier1EscalationCount).toBe(1);
  });

  it("corruption recovery — corrupt file yields empty store", () => {
    writeFileSync(statePath, "{ not valid json");
    const store = freshStore();
    expect(store.getAllProjects()).toEqual([]);
  });

  it("B7 drops unknown keys but keeps known fields", () => {
    writeFileSync(
      statePath,
      JSON.stringify({
        version: 1,
        projects: {
          p1: {
            id: "p1",
            name: "n",
            description: "d",
            nonGoals: [],
            state: "executing",
            architectWorktreePath: "/tmp/arch-p1",
            compactionGeneration: 0,
            phases: [
              {
                id: "ph1",
                state: "pending",
                spec: "s",
                reviewerRejectionCount: 0,
                arbitrationCount: 0,
                futureField: "drop-me", // unknown phase key
              },
            ],
            totalCostUsd: 0,
            budgetCeilingUsd: 10,
            totalTier1EscalationCount: 0,
            createdAt: "2026-04-24T00:00:00Z",
            updatedAt: "2026-04-24T00:00:00Z",
            bogusKeyFromFuture: "also-drop",
          },
        },
      }),
    );
    const store = freshStore();
    const proj = store.getProject("p1")!;
    expect(proj.state).toBe("executing");
    expect(proj.phases).toHaveLength(1);
    expect((proj as unknown as Record<string, unknown>).bogusKeyFromFuture).toBeUndefined();
    expect(
      (proj.phases[0] as unknown as Record<string, unknown>).futureField,
    ).toBeUndefined();
  });

  // --- Multi-project isolation ---

  it("multi-project isolation — cost and escalation counts don't leak", () => {
    const store = freshStore();
    const a = store.createProject("a", "desc-a", []);
    const b = store.createProject("b", "desc-b", []);
    store.incrementCost(a.id, 1.0);
    store.incrementCost(b.id, 2.5);
    store.incrementTier1Escalation(a.id);
    expect(store.getProject(a.id)!.totalCostUsd).toBeCloseTo(1.0);
    expect(store.getProject(b.id)!.totalCostUsd).toBeCloseTo(2.5);
    expect(store.getProject(a.id)!.totalTier1EscalationCount).toBe(1);
    expect(store.getProject(b.id)!.totalTier1EscalationCount).toBe(0);
  });

  it("getAllProjects returns every tracked project", () => {
    const store = freshStore();
    store.createProject("a", "da", []);
    store.createProject("b", "db", []);
    store.createProject("c", "dc", []);
    expect(store.getAllProjects()).toHaveLength(3);
  });

  // --- Architect compaction summary ---

  it("setArchitectSummary stores summary and increments compactionGeneration", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", ["ng"]);
    expect(proj.compactionGeneration).toBe(0);

    const summary: ArchitectCompactionSummary = {
      projectId: proj.id,
      name: proj.name,
      description: proj.description,
      nonGoals: proj.nonGoals,
      priorVerdicts: [],
      completedPhases: [],
      currentPhaseContext: {
        phaseId: "ph-1",
        taskId: "t-1",
        state: "active",
        reviewerRejectionCount: 0,
        arbitrationCount: 0,
      },
      compactedAt: new Date().toISOString(),
      compactionGeneration: 1,
    };
    store.setArchitectSummary(proj.id, summary);
    const reloaded = store.getProject(proj.id)!;
    expect(reloaded.architectSummary?.currentPhaseContext.phaseId).toBe("ph-1");
    expect(reloaded.compactionGeneration).toBe(1);

    // Second compaction increments again
    store.setArchitectSummary(proj.id, summary);
    expect(store.getProject(proj.id)!.compactionGeneration).toBe(2);
  });

  // --- Guards ---

  it("addPhase/attachTask/markPhase... throw on unknown project id", () => {
    const store = freshStore();
    expect(() => store.addPhase("nope", "s")).toThrow(/not found/);
    expect(() => store.attachTask("nope", "ph", "t")).toThrow(/not found/);
    expect(() => store.incrementCost("nope", 1)).toThrow(/not found/);
  });

  it("attachTask throws on unknown phase id within a known project", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    expect(() => store.attachTask(proj.id, "unknown-phase", "t")).toThrow(/Phase not found/);
  });

  // --- Atomic persistence ---

  it("atomic write — leaves no stale .tmp files after a round of mutations", () => {
    const store = freshStore();
    const proj = store.createProject("p", "d", []);
    store.addPhase(proj.id, "s");
    store.incrementCost(proj.id, 0.1);

    // final state.json should exist; no stragglers matching .projects-*.tmp
    expect(existsSync(statePath)).toBe(true);
    const stragglers = readdirSync(tmpDir).filter((f) => f.startsWith(".projects-") && f.endsWith(".tmp"));
    expect(stragglers).toHaveLength(0);
  });
});
