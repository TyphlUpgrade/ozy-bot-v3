import { describe, it, expect } from "vitest";
import { renderEpistle } from "../../src/discord/epistle-templates.js";
import { resolveIdentity } from "../../src/discord/identity.js";
import { frozenCtx } from "./fixtures/epistle-timestamp.js";

describe("Wave E-α epistle fixtures (un-skipped in commit 2)", () => {
  it("F1 session_complete success → executor identity", () => {
    const event = {
      type: "session_complete" as const,
      taskId: "t1",
      success: true,
      errors: [] as string[],
    };
    const identity = resolveIdentity(event);
    expect(identity).toBe("executor");
    const out = renderEpistle(event, identity, frozenCtx());
    expect(out).toContain("success");
  });

  it("F2 task_done with summary+filesChanged+responseLevelName → executor identity", () => {
    const event = {
      type: "task_done" as const,
      taskId: "t1",
      responseLevelName: "reviewed",
      summary: "Fixed the auth bug",
      filesChanged: ["src/auth.ts", "tests/auth.test.ts"],
    };
    const identity = resolveIdentity(event);
    expect(identity).toBe("executor");
    const out = renderEpistle(event, identity, frozenCtx());
    // Structured rendering — summary + filesChanged present
    expect(out).toContain("Fixed the auth bug");
    expect(out).toContain("src/auth.ts");
    expect(out).toContain("response level: reviewed");
  });

  it("F3 review_mandatory with reviewSummary+reviewFindings → reviewer identity", () => {
    const event = {
      type: "review_mandatory" as const,
      taskId: "t1",
      projectId: "proj-1",
      reviewSummary: "Two critical issues found",
      reviewFindings: [
        { severity: "critical" as const, file: "src/foo.ts", line: 42, description: "SQL injection" },
        { severity: "high" as const, file: "src/bar.ts", description: "Missing auth check" },
      ],
    } as Parameters<typeof renderEpistle>[0] & {
      reviewSummary?: string;
      reviewFindings?: { severity: string; file: string; line?: number; description: string }[];
    };
    const identity = resolveIdentity(event as Parameters<typeof resolveIdentity>[0]);
    expect(identity).toBe("reviewer");
    const out = renderEpistle(event as Parameters<typeof renderEpistle>[0], identity, frozenCtx());
    expect(out).toContain("Review Required");
    expect(out).toContain("Two critical issues found");
    expect(out).toContain("SQL injection");
  });

  it("F4 review_arbitration_entered → reviewer identity", () => {
    const event = {
      type: "review_arbitration_entered" as const,
      taskId: "t1",
      projectId: "proj-1",
      reviewerRejectionCount: 2,
    };
    const identity = resolveIdentity(event);
    expect(identity).toBe("reviewer");
    const out = renderEpistle(event, identity, frozenCtx());
    // Falls through to compact fallback (not an epistle-eligible event)
    expect(typeof out).toBe("string");
  });

  it("F5 arbitration_verdict → architect identity (UNCHANGED)", () => {
    const event = {
      type: "arbitration_verdict" as const,
      taskId: "t1",
      projectId: "proj-1",
      verdict: "retry_with_directive" as const,
      rationale: "add integration test",
    };
    const identity = resolveIdentity(event);
    expect(identity).toBe("architect");
    const out = renderEpistle(event, identity, frozenCtx());
    expect(typeof out).toBe("string");
  });

  it("F6 session_complete failure preserves Phase A pin :309 byte-equality (em-dash + bracketed terminalReason)", () => {
    const event = {
      type: "session_complete" as const,
      taskId: "t1",
      success: false,
      errors: ["boom1", "boom2"],
      terminalReason: "budget_exceeded",
    };
    const identity = resolveIdentity(event);
    expect(identity).toBe("executor");
    const out = renderEpistle(event, identity, frozenCtx());
    // AC8 byte-equality: em-dash U+2014 + space + semicolon-space joining + brackets
    expect(out).toContain("failure — boom1; boom2 [budget_exceeded]");
  });
});
