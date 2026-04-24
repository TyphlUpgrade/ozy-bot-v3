import { describe, it, expect } from "vitest";
import { truncateRationale, sanitize, redactSecrets, sanitizeTaskId } from "../../src/lib/text.js";

describe("truncateRationale", () => {
  it("passes short strings through unchanged", () => {
    expect(truncateRationale("short reason")).toBe("short reason");
  });

  it("caps at default 1024 chars with ellipsis", () => {
    const long = "x".repeat(2000);
    const out = truncateRationale(long);
    expect(out.length).toBe(1025); // 1024 + ellipsis char
    expect(out.endsWith("…")).toBe(true);
  });

  it("strips ANSI escape sequences", () => {
    const s = "hello \x1b[31mred\x1b[0m world";
    expect(truncateRationale(s)).toBe("hello red world");
  });

  it("strips control chars but keeps newlines and tabs", () => {
    const s = "line1\nline2\tcol\x00\x07end";
    expect(truncateRationale(s)).toBe("line1\nline2\tcolend");
  });

  it("accepts custom maxLen", () => {
    expect(truncateRationale("abcdef", 3)).toBe("abc…");
  });
});

describe("sanitize + redactSecrets (regression)", () => {
  it("sanitize neutralizes @everyone", () => {
    expect(sanitize("hi @everyone")).toContain("@​everyone");
  });

  it("redactSecrets strips API keys", () => {
    const raw = "sk-abcdefghijklmnopqrst1234";
    expect(redactSecrets(raw)).toBe("[REDACTED]");
  });

  it("sanitizeTaskId rejects slashes", () => {
    expect(sanitizeTaskId("../evil")).toBe(null);
  });
});
