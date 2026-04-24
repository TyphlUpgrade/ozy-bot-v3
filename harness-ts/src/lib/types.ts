/**
 * Shared assessment types used across Phase 2A modules.
 * All modules (escalation, checkpoint, response, manager) import from here.
 */

export type ScopeClarity = "clear" | "partial" | "unclear";
export type DesignCertainty = "obvious" | "alternatives_exist" | "guessing";
export type TestCoverage = "verifiable" | "partial" | "untestable";

export interface Assumption {
  description: string;
  impact: "high" | "low";
  reversible: boolean;
}

export interface ConfidenceAssessment {
  scopeClarity: ScopeClarity;
  designCertainty: DesignCertainty;
  assumptions: Assumption[];
  openQuestions: string[];
  testCoverage: TestCoverage;
}
