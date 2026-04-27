import { describe, it, expect } from "vitest";
import { resolveIdentity, type IdentityRole } from "../../src/discord/identity.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";

// Minimal event factory — constructs the smallest valid object for each
// OrchestratorEvent variant so the exhaustive switch can be exercised.
function makeEvent(type: OrchestratorEvent["type"]): OrchestratorEvent {
  switch (type) {
    case "task_picked_up":
      return { type, taskId: "t1", prompt: "p" };
    case "session_complete":
      return { type, taskId: "t1", success: true, errors: [] };
    case "merge_result":
      return { type, taskId: "t1", result: { status: "merged" } };
    case "task_shelved":
      return { type, taskId: "t1", reason: "r" };
    case "task_failed":
      return { type, taskId: "t1", reason: "r", attempt: 1 };
    case "task_done":
      return { type, taskId: "t1" };
    case "poll_tick":
      return { type };
    case "shutdown":
      return { type };
    case "escalation_needed":
      return { type, taskId: "t1", escalation: { type: "scope_unclear" } };
    case "checkpoint_detected":
      return { type, taskId: "t1", checkpoints: [] };
    case "response_level":
      return { type, taskId: "t1", level: 0, name: "direct", reasons: [] };
    case "completion_compliance":
      return {
        type,
        taskId: "t1",
        hasConfidence: false,
        hasUnderstanding: false,
        hasAssumptions: false,
        hasNonGoals: false,
        complianceScore: 0,
      };
    case "retry_scheduled":
      return { type, taskId: "t1", attempt: 1, maxRetries: 3 };
    case "budget_exhausted":
      return { type, taskId: "t1", totalCostUsd: 1.0 };
    case "project_declared":
      return { type, projectId: "p1", name: "test" };
    case "project_decomposed":
      return { type, projectId: "p1", phaseCount: 1 };
    case "project_completed":
      return { type, projectId: "p1", phaseCount: 1, totalCostUsd: 0 };
    case "project_failed":
      return { type, projectId: "p1", reason: "r" };
    case "project_aborted":
      return { type, projectId: "p1", operatorId: "op1" };
    case "architect_spawned":
      return { type, projectId: "p1", sessionId: "s1" };
    case "architect_respawned":
      return { type, projectId: "p1", sessionId: "s1", reason: "compaction" };
    case "architect_arbitration_fired":
      return { type, taskId: "t1", projectId: "p1", cause: "escalation" };
    case "arbitration_verdict":
      return { type, taskId: "t1", projectId: "p1", verdict: "retry_with_directive", rationale: "r" };
    case "review_arbitration_entered":
      return { type, taskId: "t1", projectId: "p1", reviewerRejectionCount: 1 };
    case "review_mandatory":
      return { type, taskId: "t1", projectId: "p1" };
    case "budget_ceiling_reached":
      return { type, projectId: "p1", currentCostUsd: 9.0, ceilingUsd: 10.0 };
    case "compaction_fired":
      return { type, projectId: "p1", generation: 1 };
    case "session_stalled":
      return {
        type,
        taskId: "t1",
        tier: "executor",
        lastActivityAt: 0,
        stalledForMs: 0,
        aborted: false,
      };
    case "nudge_check":
      return {
        type,
        projectId: "p1",
        sourceAgent: "orchestrator",
        status: "stagnant",
        observations: [],
      };
  }
}

// Table: [event type, expected IdentityRole] — one row per OrchestratorEvent variant (27 total)
const CASES: Array<[OrchestratorEvent["type"], IdentityRole]> = [
  ["task_picked_up", "orchestrator"],
  ["session_complete", "executor"],
  ["merge_result", "orchestrator"],
  ["task_shelved", "orchestrator"],
  ["task_failed", "orchestrator"],
  ["task_done", "executor"],
  ["poll_tick", "orchestrator"],
  ["shutdown", "orchestrator"],
  ["escalation_needed", "orchestrator"],
  ["checkpoint_detected", "orchestrator"],
  ["response_level", "orchestrator"],
  ["completion_compliance", "orchestrator"],
  ["retry_scheduled", "orchestrator"],
  ["budget_exhausted", "orchestrator"],
  ["project_declared", "architect"],
  ["project_decomposed", "architect"],
  ["project_completed", "architect"],
  ["project_failed", "architect"],
  ["project_aborted", "architect"],
  ["architect_spawned", "architect"],
  ["architect_respawned", "architect"],
  ["architect_arbitration_fired", "architect"],
  ["arbitration_verdict", "architect"],
  ["review_arbitration_entered", "reviewer"],
  ["review_mandatory", "reviewer"],
  ["budget_ceiling_reached", "orchestrator"],
  ["compaction_fired", "architect"],
  ["session_stalled", "orchestrator"],
  // Wave E-δ commit-1 — placeholder arm returns "orchestrator". Commit-2a
  // wires `event.sourceAgent` so each of the four sourceAgent variants
  // resolves to its matching role.
  ["nudge_check", "orchestrator"],
];

describe("resolveIdentity", () => {
  it.each(CASES)("resolveIdentity(%s) = %s", (type, expected) => {
    const event = makeEvent(type);
    expect(resolveIdentity(event)).toBe(expected);
  });
});
