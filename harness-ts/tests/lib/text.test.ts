import { describe, it, expect } from "vitest";
import { truncateRationale, sanitize, redactSecrets, sanitizeTaskId } from "../../src/lib/text.js";

describe("truncateRationale", () => {
  it("passes short strings through unchanged", () => {
    expect(truncateRationale("short reason")).toBe("short reason");
  });

  it("caps at default 1024 chars + single-character ellipsis suffix", () => {
    const long = "x".repeat(2000);
    const out = truncateRationale(long);
    expect(out.length).toBeLessThanOrEqual(1024 + 2); // tolerant of ellipsis-char width drift
    expect(out.startsWith("x".repeat(1024))).toBe(true);
    expect(out.endsWith("…")).toBe(true);
  });

  it("strips C1 controls (8-bit CSI)", () => {
    expect(truncateRationale("aXb")).toBe("aXb");
  });

  it("strips BIDI override characters", () => {
    // ‮ = Right-to-Left Override; commonly used for filename spoofing.
    expect(truncateRationale("hi‮there")).toBe("hithere");
  });

  it("strips OSC escape sequence (ESC ] ... BEL)", () => {
    // OSC 0;title sets terminal title; malicious content would survive a
    // naive ANSI-only regex that only handles CSI-final-byte.
    expect(truncateRationale("safe]0;pwndtext")).toBe("safetext");
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
