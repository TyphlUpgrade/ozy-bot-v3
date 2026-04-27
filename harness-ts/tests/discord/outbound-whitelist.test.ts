/**
 * Wave E-γ + E-δ — outbound-whitelist tests.
 *
 * AC3 hard assertion: exactly 13 tuples (9 baseline + 4 nudge_check::*).
 * Verified that no tuple references `architect_decomposed` (a non-existent
 * event type — the verbatim source-of-truth is `project_decomposed`).
 */

import { describe, it, expect } from "vitest";

import {
  OUTBOUND_LLM_WHITELIST,
  isOutboundLlmEligible,
} from "../../src/discord/outbound-whitelist.js";
import type { OutboundRole } from "../../src/discord/outbound-response-generator.js";

const ALL_ROLES: readonly OutboundRole[] = [
  "architect",
  "reviewer",
  "executor",
  "orchestrator",
];

// Verbatim event types from src/orchestrator.ts:107-140 — used for shape checks.
const KNOWN_EVENT_TYPES = new Set<string>([
  "task_picked_up",
  "session_complete",
  "merge_result",
  "task_shelved",
  "task_failed",
  "task_done",
  "poll_tick",
  "shutdown",
  "session_stalled",
  "escalation_needed",
  "checkpoint_detected",
  "response_level",
  "completion_compliance",
  "retry_scheduled",
  "budget_exhausted",
  "project_declared",
  "project_decomposed",
  "project_completed",
  "project_failed",
  "project_aborted",
  "architect_spawned",
  "architect_respawned",
  "architect_arbitration_fired",
  "arbitration_verdict",
  "review_arbitration_entered",
  "review_mandatory",
  "budget_ceiling_reached",
  "compaction_fired",
  "nudge_check",
]);

describe("OUTBOUND_LLM_WHITELIST", () => {
  it("Wave E-δ assertion: contains exactly 13 tuples (9 + 4 nudge_check)", () => {
    expect(OUTBOUND_LLM_WHITELIST.size).toBe(13);
  });

  it("every key is `<eventType>::<role>` with both halves verbatim", () => {
    for (const key of OUTBOUND_LLM_WHITELIST) {
      const [eventType, role] = key.split("::");
      expect(eventType).toBeDefined();
      expect(role).toBeDefined();
      expect(KNOWN_EVENT_TYPES.has(eventType!)).toBe(true);
      expect((ALL_ROLES as readonly string[]).includes(role!)).toBe(true);
    }
  });

  it("does NOT include `architect_decomposed` (the plan drift caught in E-β)", () => {
    for (const key of OUTBOUND_LLM_WHITELIST) {
      expect(key.startsWith("architect_decomposed")).toBe(false);
    }
  });

  it("includes `project_decomposed::architect` (verbatim source-of-truth replacement)", () => {
    expect(OUTBOUND_LLM_WHITELIST.has("project_decomposed::architect")).toBe(true);
  });

  it("includes the 13 documented tuples per plan D5 + E-δ N5 (project_decomposed correction)", () => {
    const expected = [
      "session_complete::executor",
      "task_done::executor",
      "review_mandatory::reviewer",
      "review_arbitration_entered::reviewer",
      "project_decomposed::architect",
      "architect_arbitration_fired::architect",
      "arbitration_verdict::architect",
      "escalation_needed::orchestrator",
      "merge_result::orchestrator",
      // Wave E-δ N5 — periodic nudge_check, one per sourceAgent value
      "nudge_check::architect",
      "nudge_check::reviewer",
      "nudge_check::executor",
      "nudge_check::orchestrator",
    ];
    for (const tuple of expected) {
      expect(OUTBOUND_LLM_WHITELIST.has(tuple)).toBe(true);
    }
  });

  it("Wave E-δ N5: includes one nudge_check tuple per sourceAgent value", () => {
    expect(OUTBOUND_LLM_WHITELIST.has("nudge_check::architect")).toBe(true);
    expect(OUTBOUND_LLM_WHITELIST.has("nudge_check::reviewer")).toBe(true);
    expect(OUTBOUND_LLM_WHITELIST.has("nudge_check::executor")).toBe(true);
    expect(OUTBOUND_LLM_WHITELIST.has("nudge_check::orchestrator")).toBe(true);
  });
});

describe("isOutboundLlmEligible", () => {
  it("returns true for whitelisted (eventType, role) pairs", () => {
    expect(isOutboundLlmEligible("session_complete", "executor")).toBe(true);
    expect(isOutboundLlmEligible("review_mandatory", "reviewer")).toBe(true);
    expect(isOutboundLlmEligible("project_decomposed", "architect")).toBe(true);
    expect(isOutboundLlmEligible("merge_result", "orchestrator")).toBe(true);
  });

  it("returns false for non-whitelisted (eventType, role) pairs", () => {
    // Right event, wrong role:
    expect(isOutboundLlmEligible("session_complete", "architect")).toBe(false);
    expect(isOutboundLlmEligible("merge_result", "executor")).toBe(false);
    // Right role, wrong event:
    expect(isOutboundLlmEligible("poll_tick", "orchestrator")).toBe(false);
    expect(isOutboundLlmEligible("task_picked_up", "orchestrator")).toBe(false);
    // Both off-list:
    expect(isOutboundLlmEligible("shutdown", "executor")).toBe(false);
  });
});
