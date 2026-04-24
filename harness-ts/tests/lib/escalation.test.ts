import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { readEscalation, validateEscalation } from "../../src/lib/escalation.js";

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-esc-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

describe("validateEscalation", () => {
  it("validates a complete escalation signal", () => {
    const result = validateEscalation({
      type: "clarification_needed",
      question: "Should we use RS256 or HS256?",
      context: "Auth module design",
      options: ["RS256", "HS256"],
    });
    expect(result).not.toBeNull();
    expect(result!.type).toBe("clarification_needed");
    expect(result!.question).toBe("Should we use RS256 or HS256?");
    expect(result!.context).toBe("Auth module design");
    expect(result!.options).toEqual(["RS256", "HS256"]);
  });

  it("validates all escalation types", () => {
    const types = [
      "clarification_needed",
      "design_decision",
      "blocked",
      "scope_unclear",
      "persistent_failure",
    ];
    for (const t of types) {
      const result = validateEscalation({ type: t, question: "q" });
      expect(result).not.toBeNull();
      expect(result!.type).toBe(t);
    }
  });

  it("rejects unknown escalation type", () => {
    expect(validateEscalation({ type: "unknown_type", question: "q" })).toBeNull();
  });

  it("rejects missing required fields", () => {
    expect(validateEscalation({ type: "blocked" })).toBeNull(); // no question
    expect(validateEscalation({ question: "q" })).toBeNull(); // no type
  });

  it("rejects empty question string", () => {
    expect(validateEscalation({ type: "blocked", question: "" })).toBeNull();
  });

  it("accepts optional assessment field", () => {
    const result = validateEscalation({
      type: "design_decision",
      question: "Which pattern?",
      assessment: {
        scopeClarity: "partial",
        designCertainty: "alternatives_exist",
        assumptions: [],
        openQuestions: ["Which pattern is better?"],
        testCoverage: "verifiable",
      },
    });
    expect(result).not.toBeNull();
    expect(result!.assessment).toBeDefined();
    expect(result!.assessment!.scopeClarity).toBe("partial");
  });

  it("returns null for non-object input", () => {
    expect(validateEscalation(null)).toBeNull();
    expect(validateEscalation("string")).toBeNull();
    expect(validateEscalation(42)).toBeNull();
  });

  it("strips non-string entries from options", () => {
    const result = validateEscalation({
      type: "blocked",
      question: "Which?",
      options: ["valid", 42, null, "also valid"],
    });
    expect(result).not.toBeNull();
    expect(result!.options).toEqual(["valid", "also valid"]);
  });
});

describe("readEscalation", () => {
  beforeEach(() => { tmpDir = makeTmpDir(); });
  afterEach(() => { rmSync(tmpDir, { recursive: true, force: true }); });

  it("reads valid escalation file", () => {
    mkdirSync(join(tmpDir, ".harness"), { recursive: true });
    writeFileSync(
      join(tmpDir, ".harness", "escalation.json"),
      JSON.stringify({ type: "blocked", question: "Missing API key" }),
    );
    const result = readEscalation(tmpDir);
    expect(result).not.toBeNull();
    expect(result!.type).toBe("blocked");
  });

  it("returns null for missing file", () => {
    expect(readEscalation(tmpDir)).toBeNull();
  });

  it("returns null for malformed JSON", () => {
    mkdirSync(join(tmpDir, ".harness"), { recursive: true });
    writeFileSync(join(tmpDir, ".harness", "escalation.json"), "not json{{{");
    expect(readEscalation(tmpDir)).toBeNull();
  });
});
