import { describe, it, expect } from "vitest";
import { StubReviewGate } from "../../scripts/lib/stub-review-gate.js";
import type { TaskRecord } from "../../src/lib/state.js";
import type { CompletionSignal } from "../../src/session/manager.js";

const EMPTY_TASK = {} as TaskRecord;
const EMPTY_COMPLETION = {} as CompletionSignal;

describe("StubReviewGate", () => {
  it("consumes verdict queue in order", async () => {
    const gate = new StubReviewGate({
      queue: ["reject", "approve"],
      silent: true,
    });
    const r1 = await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    const r2 = await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    expect(r1.verdict).toBe("reject");
    expect(r2.verdict).toBe("approve");
    expect(gate.callsMade).toBe(2);
  });

  it("falls back to defaultVerdict after queue is exhausted", async () => {
    const gate = new StubReviewGate({ queue: ["reject"], defaultVerdict: "approve", silent: true });
    await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    const r2 = await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    const r3 = await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    expect(r2.verdict).toBe("approve");
    expect(r3.verdict).toBe("approve");
  });

  it("exposes configurable arbitrationThreshold with default 1", async () => {
    const def = new StubReviewGate({ queue: [], silent: true });
    expect(def.arbitrationThreshold).toBe(1);
    const custom = new StubReviewGate({ queue: [], arbitrationThreshold: 3, silent: true });
    expect(custom.arbitrationThreshold).toBe(3);
  });

  it("overrides summary + findings per call index", async () => {
    const gate = new StubReviewGate({
      queue: ["reject", "reject"],
      rejectSummaryByCall: { 1: "CALL-1-SUM", 2: "CALL-2-SUM" },
      rejectFindingsByCall: {
        1: [{ severity: "low", dimension: "integration", description: "c1" }],
      },
      silent: true,
    });
    const r1 = await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    const r2 = await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    expect(r1.summary).toBe("CALL-1-SUM");
    expect(r2.summary).toBe("CALL-2-SUM");
    expect(r1.findings[0].description).toBe("c1");
    // Call 2 had no findings override → default findings
    expect(r2.findings.length).toBeGreaterThan(0);
  });

  it("approve verdicts carry zero findings and low weighted risk", async () => {
    const gate = new StubReviewGate({ queue: ["approve"], silent: true });
    const r = await gate.runReview(EMPTY_TASK, "", EMPTY_COMPLETION);
    expect(r.findings).toHaveLength(0);
    expect(r.riskScore.weighted).toBeLessThan(0.2);
  });
});
