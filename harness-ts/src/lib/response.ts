/**
 * Graduated response routing — maps completion assessment to escalation level 0-4.
 * Phase 2A: informational only, all levels proceed to merge.
 * Phase 3: levels 2+ will gate the merge.
 */

import type { CompletionSignal } from "../session/manager.js";
import type { SessionResult } from "../session/sdk.js";

export type ResponseLevel = 0 | 1 | 2 | 3 | 4;

export interface ResponseLevelResult {
  level: ResponseLevel;
  name: "direct" | "enriched" | "reviewed" | "dialogue" | "planned";
  reasons: string[];
}

export interface ResponseThresholds {
  reviewCostUsd?: number;     // default 0.50
  reviewFileCount?: number;   // default 10
  maxDirectCostUsd?: number;  // default 0.20
}

const LEVEL_NAMES: Record<ResponseLevel, ResponseLevelResult["name"]> = {
  0: "direct",
  1: "enriched",
  2: "reviewed",
  3: "dialogue",
  4: "planned",
};

const DEFAULT_THRESHOLDS: Required<ResponseThresholds> = {
  reviewCostUsd: 0.50,
  reviewFileCount: 10,
  maxDirectCostUsd: 0.20,
};

export function evaluateResponseLevel(
  completion: CompletionSignal,
  sessionResult: SessionResult,
  thresholds?: ResponseThresholds,
): ResponseLevelResult {
  const t = { ...DEFAULT_THRESHOLDS, ...thresholds };
  const reasons: string[] = [];
  let level: ResponseLevel = 0;

  const confidence = completion.confidence;

  // No confidence assessment → default level 1
  if (!confidence) {
    return {
      level: 1,
      name: "enriched",
      reasons: ["No confidence assessment provided"],
    };
  }

  // Level 4: multiple unclear + high-impact irreversible assumptions
  const unclearCount =
    (confidence.scopeClarity === "unclear" ? 1 : 0) +
    (confidence.designCertainty === "guessing" ? 1 : 0) +
    (confidence.testCoverage === "untestable" ? 1 : 0);
  const hasHighImpactIrreversible = confidence.assumptions.some(
    (a) => a.impact === "high" && !a.reversible,
  );

  if (unclearCount >= 2 && hasHighImpactIrreversible) {
    reasons.push(`${unclearCount} unclear dimensions + high-impact irreversible assumptions`);
    level = 4;
  }

  // Level 3: any unclear/guessing or open questions
  if (level < 3) {
    if (confidence.scopeClarity === "unclear") {
      reasons.push("Scope clarity: unclear");
      level = Math.max(level, 3) as ResponseLevel;
    }
    if (confidence.designCertainty === "guessing") {
      reasons.push("Design certainty: guessing");
      level = Math.max(level, 3) as ResponseLevel;
    }
    if (confidence.openQuestions.length > 0) {
      reasons.push(`${confidence.openQuestions.length} open question(s)`);
      level = Math.max(level, 3) as ResponseLevel;
    }
  }

  // Level 2: partial/alternatives_exist, high cost, many files
  if (level < 2) {
    if (confidence.scopeClarity === "partial") {
      reasons.push("Scope clarity: partial");
      level = Math.max(level, 2) as ResponseLevel;
    }
    if (confidence.designCertainty === "alternatives_exist") {
      reasons.push("Design certainty: alternatives exist");
      level = Math.max(level, 2) as ResponseLevel;
    }
    if (sessionResult.totalCostUsd > t.reviewCostUsd) {
      reasons.push(`Cost $${sessionResult.totalCostUsd.toFixed(2)} > review threshold $${t.reviewCostUsd.toFixed(2)}`);
      level = Math.max(level, 2) as ResponseLevel;
    }
    if (completion.filesChanged.length > t.reviewFileCount) {
      reasons.push(`${completion.filesChanged.length} files changed > threshold ${t.reviewFileCount}`);
      level = Math.max(level, 2) as ResponseLevel;
    }
  }

  // Level 1: has assumptions or cost above direct threshold
  if (level < 1) {
    if (confidence.assumptions.length > 0) {
      reasons.push(`${confidence.assumptions.length} assumption(s)`);
      level = 1;
    }
    if (sessionResult.totalCostUsd > t.maxDirectCostUsd) {
      reasons.push(`Cost $${sessionResult.totalCostUsd.toFixed(2)} > direct threshold $${t.maxDirectCostUsd.toFixed(2)}`);
      level = 1;
    }
  }

  // Level 0: all clear
  if (level === 0) {
    reasons.push("All dimensions clear, low cost");
  }

  return { level, name: LEVEL_NAMES[level], reasons };
}
