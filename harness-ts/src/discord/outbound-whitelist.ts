/**
 * Wave E-γ — outbound LLM whitelist.
 *
 * 9 (event.type, role) tuples eligible for LLM voice transformation. Strict
 * subset of the 27-event allow-list × 4 roles. Per harness-ts I-4 (verbatim
 * source-of-truth), keys are taken verbatim from the OrchestratorEvent union
 * at `src/orchestrator.ts:107-137`. The plan document mentioned
 * `architect_decomposed`, but no such event exists — the verbatim name is
 * `project_decomposed` (with `architect` as the speaking role).
 */

import type { OrchestratorEvent } from "../orchestrator.js";
import type { OutboundRole } from "./outbound-response-generator.js";

export const OUTBOUND_LLM_WHITELIST: ReadonlySet<string> = new Set([
  // Executor identity
  "session_complete::executor",
  "task_done::executor",
  // Reviewer identity
  "review_mandatory::reviewer",
  "review_arbitration_entered::reviewer",
  // Architect identity (project_decomposed is the verbatim event name; the
  // E-γ plan referenced "architect_decomposed" — same drift caught in E-β)
  "project_decomposed::architect",
  "architect_arbitration_fired::architect",
  "arbitration_verdict::architect",
  // Orchestrator identity
  "escalation_needed::orchestrator",
  "merge_result::orchestrator",
]);

export function isOutboundLlmEligible(
  eventType: OrchestratorEvent["type"],
  role: OutboundRole,
): boolean {
  return OUTBOUND_LLM_WHITELIST.has(`${eventType}::${role}`);
}
