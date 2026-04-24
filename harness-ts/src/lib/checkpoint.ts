/**
 * Checkpoint signal reader — detects .harness/checkpoint.json in worktrees.
 * Agent writes checkpoints at decision points and budget thresholds.
 * Informational in Phase 2A — gating in Phase 3.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { ConfidenceAssessment } from "./types.js";

export type CheckpointReason =
  | "decision_point"
  | "budget_threshold"
  | "complexity_spike"
  | "scope_change";

const VALID_REASONS = new Set<string>([
  "decision_point",
  "budget_threshold",
  "complexity_spike",
  "scope_change",
]);

export interface CheckpointSignal {
  timestamp: string;
  reason: CheckpointReason;
  description: string;
  assessment?: ConfidenceAssessment;
  budgetConsumedPct?: number;
}

/** Validate a single checkpoint entry. Returns null if malformed. */
export function validateCheckpoint(raw: unknown): CheckpointSignal | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  if (typeof obj.timestamp !== "string") return null;
  if (typeof obj.reason !== "string" || !VALID_REASONS.has(obj.reason)) return null;
  if (typeof obj.description !== "string") return null;

  const signal: CheckpointSignal = {
    timestamp: obj.timestamp,
    reason: obj.reason as CheckpointReason,
    description: obj.description,
  };

  if (obj.assessment && typeof obj.assessment === "object") {
    signal.assessment = obj.assessment as ConfidenceAssessment;
  }
  if (typeof obj.budgetConsumedPct === "number") {
    signal.budgetConsumedPct = obj.budgetConsumedPct;
  }

  return signal;
}

/** Read checkpoint array from worktree. Returns empty array if missing or invalid. */
export function readCheckpoints(worktreePath: string): CheckpointSignal[] {
  const cpPath = join(worktreePath, ".harness", "checkpoint.json");
  if (!existsSync(cpPath)) return [];

  try {
    const raw = JSON.parse(readFileSync(cpPath, "utf-8"));
    if (!Array.isArray(raw)) return [];
    // Strip invalid entries (partial parse)
    const valid: CheckpointSignal[] = [];
    for (const entry of raw) {
      const cp = validateCheckpoint(entry);
      if (cp) valid.push(cp);
    }
    return valid;
  } catch {
    return [];
  }
}
