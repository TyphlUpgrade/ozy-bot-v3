/**
 * Deterministic identity resolver — maps every OrchestratorEvent variant to one
 * of four operator-visible roles.  The exhaustive switch is the single source of
 * truth; TypeScript will emit a compile error if a new OrchestratorEvent variant
 * is added without updating this switch.
 *
 * Direction: identity.ts → OrchestratorEvent (orchestrator.ts) only.
 * orchestrator.ts MUST NOT import identity.ts (would create a cycle).
 */

import type { OrchestratorEvent } from "../orchestrator.js";

export type IdentityRole = "executor" | "reviewer" | "architect" | "orchestrator";

/**
 * Map an OrchestratorEvent to the IdentityRole that produced it.
 *
 * - executor    — built / ran the work (session_complete, task_done)
 * - reviewer    — gates review verdict (review_mandatory, review_arbitration_entered)
 * - architect   — project / phase / arbitration lifecycle (10 events)
 * - orchestrator — system / lifecycle coordination (13 events, default)
 */
export function resolveIdentity(event: OrchestratorEvent): IdentityRole {
  switch (event.type) {
    // Executor — built/ran the work
    case "session_complete":
    case "task_done":
      return "executor";

    // Reviewer — gates review verdict
    case "review_mandatory":
    case "review_arbitration_entered":
      return "reviewer";

    // Architect — project/phase/arbitration lifecycle
    case "architect_spawned":
    case "architect_respawned":
    case "architect_arbitration_fired":
    case "arbitration_verdict":
    case "project_declared":
    case "project_decomposed":
    case "project_completed":
    case "project_failed":
    case "project_aborted":
    case "compaction_fired":
      return "architect";

    // Orchestrator — system/lifecycle events
    case "task_picked_up":
    case "merge_result":
    case "task_shelved":
    case "task_failed":
    case "poll_tick":
    case "shutdown":
    case "escalation_needed":
    case "checkpoint_detected":
    case "response_level":
    case "completion_compliance":
    case "retry_scheduled":
    case "budget_exhausted":
    case "budget_ceiling_reached":
    case "session_stalled":
      return "orchestrator";

    // Wave E-δ commit 2a: identity is derived from `event.sourceAgent`. The
    // sourceAgent literal union (`architect | reviewer | executor |
    // orchestrator`) is co-extensive with `IdentityRole`, so the assignment is
    // direct; no per-value mapping needed.
    case "nudge_check":
      return event.sourceAgent;
  }
}
