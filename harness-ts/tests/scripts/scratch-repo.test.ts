import { describe, it, expect, afterEach } from "vitest";
import { existsSync, rmSync, mkdtempSync, writeFileSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
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
    // Wave R3+R6 — detection-at-eval-time bash snippet covering both
    // languages. Python branch: pyproject.toml [build-system] gate + pytest
    // run. TS branch: package.json + npm test (auto-install if needed).
    expect(cfg.pipeline.test_command).toContain("pyproject.toml");
    expect(cfg.pipeline.test_command).toContain("pytest");
    expect(cfg.pipeline.test_command).toContain("package.json");
    expect(cfg.pipeline.test_command).toContain("npm test");
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
    expect(cfg.pipeline.test_command).toContain("pyproject.toml");
    expect(cfg.pipeline.test_command).toContain("package.json");
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

  // Wave R6 — TS-branch test_command actually runs npm test on a fake
  // package.json worktree. Skip if `npm` is not installed (CI runner sans
  // Node).
  it("test_command TS branch executes npm test against a fake worktree", () => {
    const npmAvailable = spawnSync("which", ["npm"], { stdio: "pipe" }).status === 0;
    if (!npmAvailable) return; // graceful skip
    const fake = mkdtempSync(join(tmpdir(), "scratch-ts-test-"));
    CREATED_DIRS.push(fake);
    writeFileSync(
      join(fake, "package.json"),
      JSON.stringify({ name: "fake", private: true, scripts: { test: "true" } }),
    );

    const cfg = buildBaseConfig({ root: fake, projectName: "fake" });
    // Strip the `bash -c '...'` outer wrapper so we can run the inner snippet
    // directly without nested-quote escaping.
    const cmdMatch = cfg.pipeline.test_command.match(/^bash -c '(.*)'$/s);
    expect(cmdMatch).toBeTruthy();
    const inner = cmdMatch![1];

    const okRun = spawnSync("bash", ["-c", inner], {
      cwd: fake,
      encoding: "utf-8",
      timeout: 60_000,
    });
    expect(okRun.status).toBe(0);

    // Failing-test arm — flip the script to `false`. Reuse the same dir;
    // npm install has populated node_modules already.
    writeFileSync(
      join(fake, "package.json"),
      JSON.stringify({ name: "fake", private: true, scripts: { test: "false" } }),
    );
    const failRun = spawnSync("bash", ["-c", inner], {
      cwd: fake,
      encoding: "utf-8",
      timeout: 60_000,
    });
    expect(failRun.status).not.toBe(0);
  }, 90_000);
});
