import { describe, it, expect, afterEach } from "vitest";
import { existsSync, rmSync } from "node:fs";
import { execSync } from "node:child_process";
import { join } from "node:path";
import {
  initScratchRepo,
  buildBaseConfig,
  isProjectTerminal,
  DEFAULT_POLL_LOOP_MS,
  DEFAULT_RUN_TIMEOUT_MS,
} from "../../scripts/lib/scratch-repo.js";

const CREATED_DIRS: string[] = [];

afterEach(() => {
  while (CREATED_DIRS.length) {
    const d = CREATED_DIRS.pop()!;
    try { rmSync(d, { recursive: true, force: true }); } catch { /* ignore */ }
  }
});

describe("scratch-repo helper", () => {
  it("initScratchRepo creates an isolated repo with git init", () => {
    const root = initScratchRepo({ prefix: "test-init" });
    CREATED_DIRS.push(root);
    expect(existsSync(join(root, ".git"))).toBe(true);
    expect(existsSync(join(root, "tasks"))).toBe(true);
    expect(existsSync(join(root, "worktrees"))).toBe(true);
    expect(existsSync(join(root, "sessions"))).toBe(true);
    expect(existsSync(join(root, "README.md"))).toBe(true);
    // An initial commit exists.
    const log = execSync("git log --oneline", { cwd: root, encoding: "utf-8" });
    expect(log).toContain("init");
  });

  it("initScratchRepo collisions are impossible (mkdtempSync pattern)", () => {
    const a = initScratchRepo({ prefix: "test-collide" });
    CREATED_DIRS.push(a);
    const b = initScratchRepo({ prefix: "test-collide" });
    CREATED_DIRS.push(b);
    expect(a).not.toBe(b);
  });

  it("buildBaseConfig returns a valid HarnessConfig with sensible defaults", () => {
    const cfg = buildBaseConfig({ root: "/tmp/dummy", projectName: "demo" });
    expect(cfg.project.name).toBe("demo");
    expect(cfg.project.root).toBe("/tmp/dummy");
    expect(cfg.pipeline.test_command).toBe("true");
    expect(cfg.pipeline.max_budget_usd).toBe(1.0);
  });

  it("buildBaseConfig applies pipelineOverrides on top of defaults", () => {
    const cfg = buildBaseConfig({
      root: "/tmp/dummy",
      projectName: "demo",
      pipelineOverrides: { retry_delay_ms: 42, max_budget_usd: 99 },
    });
    expect(cfg.pipeline.retry_delay_ms).toBe(42);
    expect(cfg.pipeline.max_budget_usd).toBe(99);
    // Default still present for untouched keys
    expect(cfg.pipeline.test_command).toBe("true");
  });

  it("isProjectTerminal recognizes terminal states", () => {
    expect(isProjectTerminal("completed")).toBe(true);
    expect(isProjectTerminal("failed")).toBe(true);
    expect(isProjectTerminal("aborted")).toBe(true);
    expect(isProjectTerminal("decomposing")).toBe(false);
    expect(isProjectTerminal(undefined)).toBe(false);
  });

  it("exports sensible timing constants", () => {
    expect(DEFAULT_POLL_LOOP_MS).toBeGreaterThan(0);
    expect(DEFAULT_RUN_TIMEOUT_MS).toBeGreaterThanOrEqual(60_000);
  });
});
