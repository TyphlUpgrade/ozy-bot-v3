import { describe, it, expect } from "vitest";
import { formatFindingForOps } from "../../src/lib/review-format.js";
import type { ReviewFinding } from "../../src/gates/review.js";

describe("formatFindingForOps", () => {
  it("renders [severity] file:line — description (happy path)", () => {
    const f: ReviewFinding = { severity: "critical", file: "foo.ts", line: 42, description: "desc" };
    expect(formatFindingForOps(f)).toBe("[critical] foo.ts:42 — desc");
  });

  it("substitutes ? for missing line", () => {
    const f: ReviewFinding = { severity: "high", file: "bar.ts", description: "missing line" };
    expect(formatFindingForOps(f)).toBe("[high] bar.ts:? — missing line");
  });

  it("substitutes ? when line is explicitly undefined", () => {
    const f: ReviewFinding = { severity: "low", file: "baz.ts", line: undefined, description: "low risk" };
    expect(formatFindingForOps(f)).toBe("[low] baz.ts:? — low risk");
  });

  it("preserves whitespace in description", () => {
    const f: ReviewFinding = { severity: "medium", file: "src/x.ts", line: 1, description: "  leading space  " };
    expect(formatFindingForOps(f)).toBe("[medium] src/x.ts:1 —   leading space  ");
  });

  it("handles line 0 (falsy but defined)", () => {
    const f: ReviewFinding = { severity: "low", file: "index.ts", line: 0, description: "top of file" };
    expect(formatFindingForOps(f)).toBe("[low] index.ts:0 — top of file");
  });

  it("handles all severity levels", () => {
    const severities = ["critical", "high", "medium", "low"] as const;
    for (const severity of severities) {
      const f: ReviewFinding = { severity, file: "f.ts", line: 1, description: "d" };
      expect(formatFindingForOps(f)).toMatch(new RegExp(`^\\[${severity}\\]`));
    }
  });
});
