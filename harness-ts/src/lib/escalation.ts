/**
 * Escalation signal reader — detects .harness/escalation.json in worktrees.
 * Agent writes this when stuck. Orchestrator transitions task to escalation_wait.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { ConfidenceAssessment } from "./types.js";

export type EscalationType =
  | "clarification_needed"
  | "design_decision"
  | "blocked"
  | "scope_unclear"
  | "persistent_failure";

const VALID_TYPES = new Set<string>([
  "clarification_needed",
  "design_decision",
  "blocked",
  "scope_unclear",
  "persistent_failure",
]);

export interface EscalationSignal {
  type: EscalationType;
  question: string;
  context?: string;
  options?: string[];
  assessment?: ConfidenceAssessment;
}

/** Validate raw JSON into EscalationSignal. Returns null if malformed. */
export function validateEscalation(raw: unknown): EscalationSignal | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  if (typeof obj.type !== "string" || !VALID_TYPES.has(obj.type)) return null;
  if (typeof obj.question !== "string" || obj.question.length === 0) return null;

  const signal: EscalationSignal = {
    type: obj.type as EscalationType,
    question: obj.question,
  };

  if (typeof obj.context === "string") signal.context = obj.context;
  if (Array.isArray(obj.options)) {
    const valid = obj.options.filter((o) => typeof o === "string");
    if (valid.length > 0) signal.options = valid;
  }
  // assessment validated loosely — presence is enough for escalation
  if (obj.assessment && typeof obj.assessment === "object") {
    signal.assessment = obj.assessment as ConfidenceAssessment;
  }

  return signal;
}

/** Read escalation signal from worktree. Returns null if missing or invalid. */
export function readEscalation(worktreePath: string): EscalationSignal | null {
  const escalationPath = join(worktreePath, ".harness", "escalation.json");
  if (!existsSync(escalationPath)) return null;

  try {
    const raw = JSON.parse(readFileSync(escalationPath, "utf-8"));
    return validateEscalation(raw);
  } catch {
    return null;
  }
}
