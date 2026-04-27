/**
 * Wave E-γ — LlmBudgetTracker tests.
 *
 * Covers:
 *   - canAfford true under cap, false at/over cap
 *   - charge increments persist
 *   - UTC date rollover resets spentUsd to 0
 *   - Atomic write (temp file used during persist)
 *   - File parse failure → fresh state, console.warn once
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { LlmBudgetTracker } from "../../src/discord/llm-budget.js";

let rootDir: string;

function freshRoot(prefix: string): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

function readBudgetFile(rootDir: string): unknown {
  const path = join(rootDir, ".harness", "llm-budget.json");
  return JSON.parse(readFileSync(path, "utf-8"));
}

describe("LlmBudgetTracker", () => {
  beforeEach(() => {
    rootDir = freshRoot("llm-budget-");
  });
  afterEach(() => {
    rmSync(rootDir, { recursive: true, force: true });
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("canAfford returns true under cap, false when total would exceed", () => {
    const t = new LlmBudgetTracker({ rootDir, dailyCapUsd: 1.0 });
    expect(t.canAfford(0.5)).toBe(true);
    expect(t.canAfford(1.0)).toBe(true);
    expect(t.canAfford(1.001)).toBe(false);
  });

  it("charge increments todaySpentUsd and persists to disk", () => {
    const t = new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    t.charge(0.10);
    t.charge(0.05);
    expect(t.todaySpentUsd()).toBeCloseTo(0.15, 6);
    const onDisk = readBudgetFile(rootDir) as { spentUsd: number };
    expect(onDisk.spentUsd).toBeCloseTo(0.15, 6);
  });

  it("canAfford rejects after cumulative spend reaches cap", () => {
    const t = new LlmBudgetTracker({ rootDir, dailyCapUsd: 1.0 });
    t.charge(0.99);
    expect(t.canAfford(0.005)).toBe(true);
    expect(t.canAfford(0.02)).toBe(false);
  });

  it("UTC rollover resets spentUsd to 0 when the date advances", () => {
    // Day 1: write a $4.00 spend on a fixed UTC date.
    const day1 = new Date("2026-04-27T12:00:00.000Z");
    vi.useFakeTimers();
    vi.setSystemTime(day1);
    const t1 = new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    t1.charge(4.0);
    expect(t1.todaySpentUsd()).toBeCloseTo(4.0, 6);

    // Day 2: a fresh tracker reading the same file; calling any public method
    // must roll the date over and reset spent to 0.
    const day2 = new Date("2026-04-28T03:00:00.000Z");
    vi.setSystemTime(day2);
    const t2 = new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    expect(t2.todaySpentUsd()).toBe(0);
    expect(t2.canAfford(4.99)).toBe(true);
    const onDisk = readBudgetFile(rootDir) as { currentUtcDate: string; spentUsd: number };
    expect(onDisk.currentUtcDate).toBe("2026-04-28");
    expect(onDisk.spentUsd).toBe(0);
  });

  it("rollover triggered by canAfford in the same process when date changes mid-life", () => {
    const day1 = new Date("2026-04-27T23:59:00.000Z");
    vi.useFakeTimers();
    vi.setSystemTime(day1);
    const t = new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    t.charge(4.5);
    expect(t.canAfford(0.4)).toBe(true);
    // Advance one minute → next UTC day.
    vi.setSystemTime(new Date("2026-04-28T00:00:30.000Z"));
    expect(t.todaySpentUsd()).toBe(0);
    expect(t.canAfford(4.99)).toBe(true);
  });

  it("file parse failure → starts fresh + emits console.warn exactly once", () => {
    const harnessDir = join(rootDir, ".harness");
    mkdirSync(harnessDir, { recursive: true });
    writeFileSync(join(harnessDir, "llm-budget.json"), "{not valid json", "utf-8");
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const t = new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    expect(t.todaySpentUsd()).toBe(0);
    expect(t.canAfford(4.0)).toBe(true);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0]?.[0]).toMatch(/unparseable/);
  });

  it("uses default dailyCapUsd=5.0 when not specified", () => {
    const t = new LlmBudgetTracker({ rootDir });
    expect(t.canAfford(5.0)).toBe(true);
    expect(t.canAfford(5.001)).toBe(false);
  });

  it("charge(0) and charge(negative) are no-ops", () => {
    const t = new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    t.charge(0);
    t.charge(-1.0);
    expect(t.todaySpentUsd()).toBe(0);
  });

  it("creates .harness directory on construction if missing", () => {
    expect(existsSync(join(rootDir, ".harness"))).toBe(false);
    new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    expect(existsSync(join(rootDir, ".harness", "llm-budget.json"))).toBe(true);
  });

  it("atomic write: never leaves a stale .tmp file after a charge", () => {
    const t = new LlmBudgetTracker({ rootDir, dailyCapUsd: 5.0 });
    t.charge(0.10);
    const harnessDir = join(rootDir, ".harness");
    const fs = readFileSync(join(harnessDir, "llm-budget.json"), "utf-8");
    expect(fs).toContain("\"spentUsd\": 0.1");
    // No leftover .llm-budget-*.tmp files (renamed atomically into place).
    const entries = readdirSync(harnessDir);
    const stragglers = entries.filter((e: string) => e.endsWith(".tmp"));
    expect(stragglers).toEqual([]);
  });
});
