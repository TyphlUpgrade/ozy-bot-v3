import { describe, it, expect } from "vitest";
import { evaluateResponseLevel } from "../../src/lib/response.js";
import type { CompletionSignal } from "../../src/session/manager.js";
import type { SessionResult } from "../../src/session/sdk.js";

function makeCompletion(overrides?: Partial<CompletionSignal>): CompletionSignal {
  return {
    status: "success",
    commitSha: "abc123",
    summary: "Done",
    filesChanged: ["src/a.ts"],
    ...overrides,
  };
}

function makeSessionResult(overrides?: Partial<SessionResult>): SessionResult {
  return {
    success: true,
    sessionId: "s-1",
    totalCostUsd: 0.05,
    errors: [],
    ...overrides,
  };
}

describe("evaluateResponseLevel", () => {
  it("all clear -> level 0 (direct)", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "clear",
        designCertainty: "obvious",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(0);
    expect(result.name).toBe("direct");
  });

  it("clear with assumptions -> level 1 (enriched)", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "clear",
        designCertainty: "obvious",
        assumptions: [{ description: "API stable", impact: "low", reversible: true }],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(1);
    expect(result.name).toBe("enriched");
  });

  it("partial scope clarity -> level 2 (reviewed)", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "partial",
        designCertainty: "obvious",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(2);
    expect(result.name).toBe("reviewed");
  });

  it("alternatives exist in design -> level 2", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "clear",
        designCertainty: "alternatives_exist",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(2);
    expect(result.name).toBe("reviewed");
  });

  it("high cost -> level 2", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "clear",
        designCertainty: "obvious",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult({ totalCostUsd: 0.60 }));
    expect(result.level).toBe(2);
    expect(result.reasons.some((r) => r.includes("Cost"))).toBe(true);
  });

  it("many files changed -> level 2", () => {
    const completion = makeCompletion({
      filesChanged: Array.from({ length: 15 }, (_, i) => `src/file${i}.ts`),
      confidence: {
        scopeClarity: "clear",
        designCertainty: "obvious",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(2);
    expect(result.reasons.some((r) => r.includes("files changed"))).toBe(true);
  });

  it("unclear scope -> level 3 (dialogue)", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "unclear",
        designCertainty: "obvious",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(3);
    expect(result.name).toBe("dialogue");
  });

  it("guessing design -> level 3", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "clear",
        designCertainty: "guessing",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(3);
    expect(result.name).toBe("dialogue");
  });

  it("open questions present -> level 3", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "clear",
        designCertainty: "obvious",
        assumptions: [],
        openQuestions: ["What about edge case X?"],
        testCoverage: "verifiable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(3);
    expect(result.reasons.some((r) => r.includes("open question"))).toBe(true);
  });

  it("multiple unclear + high-impact irreversible -> level 4 (planned)", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "unclear",
        designCertainty: "guessing",
        assumptions: [{ description: "Schema migration", impact: "high", reversible: false }],
        openQuestions: [],
        testCoverage: "untestable",
      },
    });
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(4);
    expect(result.name).toBe("planned");
  });

  it("no confidence assessment -> level 1 (default)", () => {
    const completion = makeCompletion(); // no confidence field
    const result = evaluateResponseLevel(completion, makeSessionResult());
    expect(result.level).toBe(1);
    expect(result.name).toBe("enriched");
    expect(result.reasons).toContain("No confidence assessment provided");
  });

  it("custom thresholds override defaults", () => {
    const completion = makeCompletion({
      confidence: {
        scopeClarity: "clear",
        designCertainty: "obvious",
        assumptions: [],
        openQuestions: [],
        testCoverage: "verifiable",
      },
    });
    // Cost $0.05 is above custom direct threshold of $0.01
    const result = evaluateResponseLevel(
      completion,
      makeSessionResult({ totalCostUsd: 0.05 }),
      { maxDirectCostUsd: 0.01 },
    );
    expect(result.level).toBe(1);
  });
});
