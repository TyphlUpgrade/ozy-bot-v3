import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { readCheckpoints, validateCheckpoint } from "../../src/lib/checkpoint.js";

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-cp-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

describe("validateCheckpoint", () => {
  it("validates a complete checkpoint", () => {
    const result = validateCheckpoint({
      timestamp: "2026-04-11T12:00:00Z",
      reason: "decision_point",
      description: "Choosing between REST and gRPC",
    });
    expect(result).not.toBeNull();
    expect(result!.reason).toBe("decision_point");
    expect(result!.description).toBe("Choosing between REST and gRPC");
  });

  it("validates all checkpoint reasons", () => {
    const reasons = ["decision_point", "budget_threshold", "complexity_spike", "scope_change"];
    for (const r of reasons) {
      const result = validateCheckpoint({
        timestamp: "2026-04-11T12:00:00Z",
        reason: r,
        description: "test",
      });
      expect(result).not.toBeNull();
      expect(result!.reason).toBe(r);
    }
  });

  it("rejects unknown reason", () => {
    expect(validateCheckpoint({
      timestamp: "2026-04-11T12:00:00Z",
      reason: "unknown_reason",
      description: "test",
    })).toBeNull();
  });

  it("rejects missing required fields", () => {
    expect(validateCheckpoint({ reason: "decision_point", description: "test" })).toBeNull(); // no timestamp
    expect(validateCheckpoint({ timestamp: "t", description: "test" })).toBeNull(); // no reason
    expect(validateCheckpoint({ timestamp: "t", reason: "decision_point" })).toBeNull(); // no description
  });

  it("accepts optional assessment and budgetConsumedPct", () => {
    const result = validateCheckpoint({
      timestamp: "2026-04-11T12:00:00Z",
      reason: "budget_threshold",
      description: "50% budget consumed",
      budgetConsumedPct: 50,
      assessment: { scopeClarity: "clear", designCertainty: "obvious", assumptions: [], openQuestions: [], testCoverage: "verifiable" },
    });
    expect(result).not.toBeNull();
    expect(result!.budgetConsumedPct).toBe(50);
    expect(result!.assessment).toBeDefined();
  });

  it("returns null for non-object input", () => {
    expect(validateCheckpoint(null)).toBeNull();
    expect(validateCheckpoint("string")).toBeNull();
  });
});

describe("readCheckpoints", () => {
  beforeEach(() => { tmpDir = makeTmpDir(); });
  afterEach(() => { rmSync(tmpDir, { recursive: true, force: true }); });

  it("reads valid checkpoint array", () => {
    mkdirSync(join(tmpDir, ".harness"), { recursive: true });
    writeFileSync(
      join(tmpDir, ".harness", "checkpoint.json"),
      JSON.stringify([
        { timestamp: "2026-04-11T12:00:00Z", reason: "decision_point", description: "First" },
        { timestamp: "2026-04-11T12:05:00Z", reason: "budget_threshold", description: "Second" },
      ]),
    );
    const result = readCheckpoints(tmpDir);
    expect(result).toHaveLength(2);
    expect(result[0].reason).toBe("decision_point");
    expect(result[1].reason).toBe("budget_threshold");
  });

  it("returns empty array for missing file", () => {
    expect(readCheckpoints(tmpDir)).toEqual([]);
  });

  it("returns empty array for malformed JSON", () => {
    mkdirSync(join(tmpDir, ".harness"), { recursive: true });
    writeFileSync(join(tmpDir, ".harness", "checkpoint.json"), "not json{{{");
    expect(readCheckpoints(tmpDir)).toEqual([]);
  });

  it("handles single-element array", () => {
    mkdirSync(join(tmpDir, ".harness"), { recursive: true });
    writeFileSync(
      join(tmpDir, ".harness", "checkpoint.json"),
      JSON.stringify([{ timestamp: "t", reason: "scope_change", description: "only one" }]),
    );
    const result = readCheckpoints(tmpDir);
    expect(result).toHaveLength(1);
  });

  it("strips invalid entries from array (partial parse)", () => {
    mkdirSync(join(tmpDir, ".harness"), { recursive: true });
    writeFileSync(
      join(tmpDir, ".harness", "checkpoint.json"),
      JSON.stringify([
        { timestamp: "t", reason: "decision_point", description: "valid" },
        { reason: "bad" }, // missing timestamp + invalid reason value
        "not an object",
        { timestamp: "t", reason: "complexity_spike", description: "also valid" },
      ]),
    );
    const result = readCheckpoints(tmpDir);
    expect(result).toHaveLength(2);
    expect(result[0].description).toBe("valid");
    expect(result[1].description).toBe("also valid");
  });

  it("returns empty for non-array JSON", () => {
    mkdirSync(join(tmpDir, ".harness"), { recursive: true });
    writeFileSync(
      join(tmpDir, ".harness", "checkpoint.json"),
      JSON.stringify({ not: "an array" }),
    );
    expect(readCheckpoints(tmpDir)).toEqual([]);
  });
});
