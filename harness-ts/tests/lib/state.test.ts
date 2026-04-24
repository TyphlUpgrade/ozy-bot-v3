import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { StateManager, TASK_STATES } from "../../src/lib/state.js";
import type { TaskState } from "../../src/lib/state.js";

let tmpDir: string;
let statePath: string;

function freshManager(): StateManager {
  return new StateManager(statePath);
}

describe("StateManager", () => {
  beforeEach(() => {
    tmpDir = join(tmpdir(), `harness-state-test-${Date.now()}`);
    mkdirSync(tmpDir, { recursive: true });
    statePath = join(tmpDir, "state.json");
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  // --- Creation ---

  it("creates tasks in pending state", () => {
    const mgr = freshManager();
    const task = mgr.createTask("fix the auth bug");
    expect(task.state).toBe("pending");
    expect(task.prompt).toBe("fix the auth bug");
    expect(task.retryCount).toBe(0);
    expect(task.escalationTier).toBe(1);
    expect(task.rebaseAttempts).toBe(0);
    expect(task.totalCostUsd).toBe(0);
  });

  it("creates tasks with custom ID", () => {
    const mgr = freshManager();
    const task = mgr.createTask("task", "custom-id-123");
    expect(task.id).toBe("custom-id-123");
  });

  // --- 10 States (Phase 2A 9 + Wave 1.5b review_arbitration) ---

  it("has exactly 10 states", () => {
    expect(TASK_STATES).toHaveLength(10);
    expect(TASK_STATES).toContain("pending");
    expect(TASK_STATES).toContain("active");
    expect(TASK_STATES).toContain("reviewing");
    expect(TASK_STATES).toContain("merging");
    expect(TASK_STATES).toContain("done");
    expect(TASK_STATES).toContain("failed");
    expect(TASK_STATES).toContain("shelved");
    expect(TASK_STATES).toContain("escalation_wait");
    expect(TASK_STATES).toContain("paused");
    expect(TASK_STATES).toContain("review_arbitration");
  });

  // --- Valid Transitions ---

  it("transitions pending -> active", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    const updated = mgr.transition(task.id, "active");
    expect(updated.state).toBe("active");
  });

  it("transitions active -> merging (skip review for simple tasks)", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    const updated = mgr.transition(task.id, "merging");
    expect(updated.state).toBe("merging");
  });

  it("transitions active -> reviewing -> merging -> done", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "reviewing");
    mgr.transition(task.id, "merging");
    mgr.transition(task.id, "done");
    expect(mgr.getTask(task.id)!.state).toBe("done");
    expect(mgr.getTask(task.id)!.completedAt).toBeTruthy();
  });

  it("transitions active -> shelved", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "shelved");
    expect(mgr.getTask(task.id)!.state).toBe("shelved");
  });

  it("transitions merging -> failed on test failure", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "merging");
    mgr.transition(task.id, "failed");
    expect(mgr.getTask(task.id)!.state).toBe("failed");
  });

  it("transitions failed -> pending for retry", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "failed");
    mgr.transition(task.id, "pending");
    expect(mgr.getTask(task.id)!.state).toBe("pending");
  });

  it("transitions active -> escalation_wait", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "escalation_wait");
    expect(mgr.getTask(task.id)!.state).toBe("escalation_wait");
  });

  it("transitions escalation_wait -> active on operator response", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "escalation_wait");
    mgr.transition(task.id, "active");
    expect(mgr.getTask(task.id)!.state).toBe("active");
  });

  it("transitions active -> paused -> active", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "paused");
    expect(mgr.getTask(task.id)!.state).toBe("paused");
    mgr.transition(task.id, "active");
    expect(mgr.getTask(task.id)!.state).toBe("active");
  });

  // --- Invalid Transitions ---

  it("rejects invalid transition: pending -> done", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    expect(() => mgr.transition(task.id, "done")).toThrow("Invalid transition: pending -> done");
  });

  it("rejects invalid transition: done -> anything", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "done");
    expect(() => mgr.transition(task.id, "pending")).toThrow("Invalid transition: done -> pending");
  });

  it("rejects transition for nonexistent task", () => {
    const mgr = freshManager();
    expect(() => mgr.transition("nonexistent", "active")).toThrow("Task not found");
  });

  // --- Atomic Persistence (O3, B1) ---

  it("persists state atomically — survives reload", () => {
    const mgr = freshManager();
    const task = mgr.createTask("persistent task");
    mgr.transition(task.id, "active");

    // Fresh manager reads from disk
    const mgr2 = freshManager();
    const loaded = mgr2.getTask(task.id);
    expect(loaded).toBeTruthy();
    expect(loaded!.state).toBe("active");
    expect(loaded!.prompt).toBe("persistent task");
  });

  it("state file is valid JSON after write", () => {
    const mgr = freshManager();
    mgr.createTask("test");
    const raw = readFileSync(statePath, "utf-8");
    expect(() => JSON.parse(raw)).not.toThrow();
  });

  // --- Defensive Deserialization (B7) ---

  it("drops unknown keys on load without crash", () => {
    // Write state with extra keys
    const state = {
      tasks: {
        "task-1": {
          id: "task-1",
          state: "active",
          prompt: "test",
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
          totalCostUsd: 0,
          retryCount: 0,
          escalationTier: 1,
          rebaseAttempts: 0,
          unknownField: "should be dropped",
          anotherUnknown: { nested: true },
        },
      },
      version: 1,
    };
    writeFileSync(statePath, JSON.stringify(state), "utf-8");

    const mgr = freshManager();
    const task = mgr.getTask("task-1");
    expect(task).toBeTruthy();
    expect(task!.state).toBe("active");
    expect((task as Record<string, unknown>).unknownField).toBeUndefined();
    expect((task as Record<string, unknown>).anotherUnknown).toBeUndefined();
  });

  it("recovers from unknown state value by defaulting to failed", () => {
    const state = {
      tasks: {
        "task-1": {
          id: "task-1",
          state: "nonexistent_state",
          prompt: "test",
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
          totalCostUsd: 0,
          retryCount: 0,
          escalationTier: 1,
          rebaseAttempts: 0,
        },
      },
      version: 1,
    };
    writeFileSync(statePath, JSON.stringify(state), "utf-8");

    const mgr = freshManager();
    const task = mgr.getTask("task-1");
    expect(task!.state).toBe("failed");
  });

  it("handles corrupt JSON file gracefully", () => {
    writeFileSync(statePath, "not json at all {{{", "utf-8");
    const mgr = freshManager();
    expect(mgr.getAllTasks()).toHaveLength(0);
  });

  // --- Shelve Clock Reset (B3) ---

  it("records shelvedAt when transitioning to shelved", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "shelved");
    expect(mgr.getTask(task.id)!.shelvedAt).toBeTruthy();
  });

  it("clears shelvedAt when unshelving", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "shelved");
    expect(mgr.getTask(task.id)!.shelvedAt).toBeTruthy();
    mgr.transition(task.id, "active");
    expect(mgr.getTask(task.id)!.shelvedAt).toBeUndefined();
  });

  // --- Escalation (B5, B6) ---

  it("escalate() increases tier and resets retry count (B6)", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.incrementRetry(task.id); // retryCount = 1
    mgr.incrementRetry(task.id); // retryCount = 2

    mgr.escalate(task.id, 2);
    const updated = mgr.getTask(task.id)!;
    expect(updated.escalationTier).toBe(2);
    expect(updated.retryCount).toBe(0); // B6: reset
  });

  it("escalate() rejects de-escalation", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.escalate(task.id, 2);
    expect(() => mgr.escalate(task.id, 1)).toThrow("Cannot de-escalate");
    expect(() => mgr.escalate(task.id, 2)).toThrow("Cannot de-escalate");
  });

  // --- Retry Count ---

  it("incrementRetry increases count", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    expect(mgr.incrementRetry(task.id)).toBe(1);
    expect(mgr.incrementRetry(task.id)).toBe(2);
    expect(mgr.incrementRetry(task.id)).toBe(3);
  });

  // --- Rebase Attempts ---

  it("incrementRebaseAttempts increases count", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    expect(mgr.incrementRebaseAttempts(task.id)).toBe(1);
    expect(mgr.incrementRebaseAttempts(task.id)).toBe(2);
  });

  // --- Query ---

  it("getTasksByState filters correctly", () => {
    const mgr = freshManager();
    mgr.createTask("a");
    mgr.createTask("b");
    const c = mgr.createTask("c");
    mgr.transition(c.id, "active");

    expect(mgr.getTasksByState("pending")).toHaveLength(2);
    expect(mgr.getTasksByState("active")).toHaveLength(1);
    expect(mgr.getTasksByState("done")).toHaveLength(0);
  });

  // --- Event Log (O9) ---

  it("writes event log entries as JSONL", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");

    const logPath = statePath.replace(/\.json$/, ".log.jsonl");
    expect(existsSync(logPath)).toBe(true);
    const lines = readFileSync(logPath, "utf-8").trim().split("\n");
    expect(lines.length).toBeGreaterThanOrEqual(2); // created + transition
    const last = JSON.parse(lines[lines.length - 1]);
    expect(last.event).toBe("transition");
    expect(last.from).toBe("pending");
    expect(last.to).toBe("active");
  });

  // --- Reload ---

  it("reload() refreshes from disk", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");

    // External modification
    const mgr2 = freshManager();
    mgr2.transition(task.id, "active");

    // mgr still sees pending
    expect(mgr.getTask(task.id)!.state).toBe("pending");
    mgr.reload();
    expect(mgr.getTask(task.id)!.state).toBe("active");
  });

  // --- Wave 1.5b: review_arbitration state + three-tier field round-trip ---

  it("transitions reviewing -> review_arbitration", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "reviewing");
    const updated = mgr.transition(task.id, "review_arbitration");
    expect(updated.state).toBe("review_arbitration");
  });

  it("review_arbitration exits to active (retry_with_directive)", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "reviewing");
    mgr.transition(task.id, "review_arbitration");
    const updated = mgr.transition(task.id, "active");
    expect(updated.state).toBe("active");
  });

  it("review_arbitration rejects illegal transition to done", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.transition(task.id, "active");
    mgr.transition(task.id, "reviewing");
    mgr.transition(task.id, "review_arbitration");
    expect(() => mgr.transition(task.id, "done")).toThrow(/Invalid transition/);
  });

  it("persists and reloads three-tier fields (projectId, phaseId, arbitrationCount, reviewerRejectionCount)", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.updateTask(task.id, {
      projectId: "proj-abc",
      phaseId: "phase-1",
      arbitrationCount: 2,
      reviewerRejectionCount: 1,
    });
    const mgr2 = freshManager();
    const reloaded = mgr2.getTask(task.id)!;
    expect(reloaded.projectId).toBe("proj-abc");
    expect(reloaded.phaseId).toBe("phase-1");
    expect(reloaded.arbitrationCount).toBe(2);
    expect(reloaded.reviewerRejectionCount).toBe(1);
  });

  it("persists and reloads dialogue + reviewResult fields", () => {
    const mgr = freshManager();
    const task = mgr.createTask("test");
    mgr.updateTask(task.id, {
      dialogueMessages: [
        { role: "operator", content: "clarify scope", timestamp: "2026-04-24T00:00:00Z" },
        { role: "agent", content: "scope: X", timestamp: "2026-04-24T00:00:05Z" },
      ],
      dialoguePendingConfirmation: true,
      reviewResult: { verdict: "request_changes", weightedRisk: 0.42, findingCount: 3 },
    });
    const mgr2 = freshManager();
    const reloaded = mgr2.getTask(task.id)!;
    expect(reloaded.dialogueMessages).toHaveLength(2);
    expect(reloaded.dialoguePendingConfirmation).toBe(true);
    expect(reloaded.reviewResult?.verdict).toBe("request_changes");
    expect(reloaded.reviewResult?.weightedRisk).toBeCloseTo(0.42);
  });

  it("B7 drops unknown keys on load but keeps Wave 1.5b known keys", () => {
    // Hand-craft a state.json with both unknown and known-new keys
    const now = new Date().toISOString();
    writeFileSync(
      statePath,
      JSON.stringify({
        version: 1,
        tasks: {
          t1: {
            id: "t1",
            state: "pending",
            prompt: "x",
            createdAt: now,
            updatedAt: now,
            totalCostUsd: 0,
            retryCount: 0,
            escalationTier: 1,
            rebaseAttempts: 0,
            tier1EscalationCount: 0,
            projectId: "proj-1",
            arbitrationCount: 5,
            bogusKeyFromFuture: "drop-me", // unknown — must be dropped
          },
        },
      }),
    );
    const mgr = freshManager();
    const t = mgr.getTask("t1")!;
    expect(t.projectId).toBe("proj-1");
    expect(t.arbitrationCount).toBe(5);
    expect((t as unknown as Record<string, unknown>).bogusKeyFromFuture).toBeUndefined();
  });
});
