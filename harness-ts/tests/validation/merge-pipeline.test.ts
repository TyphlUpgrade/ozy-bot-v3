/**
 * Validation test: Full merge pipeline with real git.
 * Tests MergeGate with realMergeGitOps against actual git repositories.
 * Verifies: rebase + merge, sequential merges, conflict handling, test failure + revert.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { execSync } from "node:child_process";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { MergeGate, realMergeGitOps } from "../../src/gates/merge.js";
import type { PipelineConfig } from "../../src/lib/config.js";

// --- Helpers ---

function createRepo(dir: string): void {
  execSync("git init", { cwd: dir, stdio: "pipe" });
  execSync("git config user.email 'test@test.com'", { cwd: dir, stdio: "pipe" });
  execSync("git config user.name 'Test'", { cwd: dir, stdio: "pipe" });
  writeFileSync(join(dir, "README.md"), "# Test Repo\n");
  execSync("git add -A && git commit -m 'initial commit'", {
    cwd: dir,
    stdio: "pipe",
    shell: "/bin/bash",
  });
}

function createWorktree(mainDir: string, branchName: string): string {
  const wtDir = join(tmpdir(), `harness-test-wt-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  execSync(`git worktree add -b ${branchName} ${wtDir}`, {
    cwd: mainDir,
    stdio: "pipe",
  });
  return wtDir;
}

function cleanupWorktree(mainDir: string, wtDir: string, branchName: string): void {
  try { execSync(`git worktree remove --force ${wtDir}`, { cwd: mainDir, stdio: "pipe" }); } catch { /* ok */ }
  try { rmSync(wtDir, { recursive: true, force: true }); } catch { /* ok */ }
  try { execSync(`git branch -D ${branchName}`, { cwd: mainDir, stdio: "pipe" }); } catch { /* ok */ }
}

function getHeadSha(cwd: string): string {
  return execSync("git rev-parse HEAD", { cwd, stdio: "pipe", encoding: "utf-8" }).trim();
}

// --- Config ---

const pipelineConfig: PipelineConfig = {
  poll_interval: 1,
  test_command: "echo 'tests pass'",
  max_retries: 3,
  test_timeout: 10,
  escalation_timeout: 3600,
  retry_delay_ms: 100,
};

// --- Tests ---

describe("Merge Pipeline — Real Git", () => {
  let mainDir: string;
  const worktrees: { dir: string; branch: string }[] = [];

  beforeEach(() => {
    mainDir = mkdtempSync(join(tmpdir(), "harness-merge-pipeline-"));
    createRepo(mainDir);
  });

  afterEach(() => {
    for (const wt of worktrees) {
      cleanupWorktree(mainDir, wt.dir, wt.branch);
    }
    worktrees.length = 0;
    rmSync(mainDir, { recursive: true, force: true });
  });

  it("clean worktree with committed changes rebases and merges onto trunk", async () => {
    const branch = "harness/task-merge-clean";
    const wtDir = createWorktree(mainDir, branch);
    worktrees.push({ dir: wtDir, branch });

    // Make a change in the worktree and commit
    writeFileSync(join(wtDir, "feature.ts"), "export const x = 1;\n");
    execSync("git add -A && git commit -m 'add feature'", {
      cwd: wtDir, stdio: "pipe", shell: "/bin/bash",
    });

    const preMergeSha = getHeadSha(mainDir);
    const gate = new MergeGate(pipelineConfig, mainDir, realMergeGitOps);
    const result = await gate.enqueue("task-merge-clean", wtDir, branch);

    expect(result.status).toBe("merged");
    if (result.status === "merged") {
      expect(result.commitSha).toBeTruthy();
      // Trunk should have advanced past pre-merge
      expect(result.commitSha).not.toBe(preMergeSha);
    }

    // Verify the merged file exists on trunk
    const trunkFiles = execSync("ls", { cwd: mainDir, encoding: "utf-8", stdio: "pipe" });
    expect(trunkFiles).toContain("feature.ts");
  });

  it("two sequential merges both succeed — second rebases onto first", async () => {
    // Create two worktrees with non-conflicting changes
    const branch1 = "harness/task-seq-1";
    const wt1 = createWorktree(mainDir, branch1);
    worktrees.push({ dir: wt1, branch: branch1 });

    writeFileSync(join(wt1, "file1.ts"), "export const a = 1;\n");
    execSync("git add -A && git commit -m 'add file1'", {
      cwd: wt1, stdio: "pipe", shell: "/bin/bash",
    });

    const branch2 = "harness/task-seq-2";
    const wt2 = createWorktree(mainDir, branch2);
    worktrees.push({ dir: wt2, branch: branch2 });

    writeFileSync(join(wt2, "file2.ts"), "export const b = 2;\n");
    execSync("git add -A && git commit -m 'add file2'", {
      cwd: wt2, stdio: "pipe", shell: "/bin/bash",
    });

    const gate = new MergeGate(pipelineConfig, mainDir, realMergeGitOps);

    // Merge first
    const result1 = await gate.enqueue("task-seq-1", wt1, branch1);
    expect(result1.status).toBe("merged");

    // Merge second — must rebase onto first's changes
    const result2 = await gate.enqueue("task-seq-2", wt2, branch2);
    expect(result2.status).toBe("merged");

    // Both files should exist on trunk
    const files = execSync("ls", { cwd: mainDir, encoding: "utf-8", stdio: "pipe" });
    expect(files).toContain("file1.ts");
    expect(files).toContain("file2.ts");
  });

  it("conflicting changes trigger rebase_conflict with file list", async () => {
    // Advance main with a conflicting change
    writeFileSync(join(mainDir, "shared.ts"), "// main version\nexport const x = 'main';\n");
    execSync("git add -A && git commit -m 'main change'", {
      cwd: mainDir, stdio: "pipe", shell: "/bin/bash",
    });

    // Create worktree from BEFORE main's change (branch off the initial commit)
    const branch = "harness/task-conflict";
    // We need to create the worktree from a point before the conflicting commit
    const initialSha = execSync("git rev-list --max-parents=0 HEAD", {
      cwd: mainDir, encoding: "utf-8", stdio: "pipe",
    }).trim();
    const wtDir = join(tmpdir(), `harness-test-conflict-${Date.now()}`);
    execSync(`git worktree add -b ${branch} ${wtDir} ${initialSha}`, {
      cwd: mainDir, stdio: "pipe",
    });
    worktrees.push({ dir: wtDir, branch });

    // Make a conflicting change in worktree
    writeFileSync(join(wtDir, "shared.ts"), "// branch version\nexport const x = 'branch';\n");
    execSync("git add -A && git commit -m 'branch change'", {
      cwd: wtDir, stdio: "pipe", shell: "/bin/bash",
    });

    const gate = new MergeGate(pipelineConfig, mainDir, realMergeGitOps);
    const result = await gate.enqueue("task-conflict", wtDir, branch);

    expect(result.status).toBe("rebase_conflict");
    if (result.status === "rebase_conflict") {
      expect(result.conflictFiles.length).toBeGreaterThan(0);
    }
  });

  it("failing test command triggers test_failed and merge is reverted", async () => {
    const failConfig: PipelineConfig = {
      ...pipelineConfig,
      test_command: "exit 1",
    };

    const branch = "harness/task-testfail";
    const wtDir = createWorktree(mainDir, branch);
    worktrees.push({ dir: wtDir, branch });

    writeFileSync(join(wtDir, "bad-feature.ts"), "export const broken = true;\n");
    execSync("git add -A && git commit -m 'add bad feature'", {
      cwd: wtDir, stdio: "pipe", shell: "/bin/bash",
    });

    const preMergeSha = getHeadSha(mainDir);
    const gate = new MergeGate(failConfig, mainDir, realMergeGitOps);
    const result = await gate.enqueue("task-testfail", wtDir, branch);

    expect(result.status).toBe("test_failed");

    // Trunk should have the revert commit — but the net content should match pre-merge
    // The merge commit exists but is reverted, so files from the branch should NOT be on trunk
    const files = execSync("ls", { cwd: mainDir, encoding: "utf-8", stdio: "pipe" });
    expect(files).not.toContain("bad-feature.ts");
  });

  it("auto-commits uncommitted worktree changes before merge", async () => {
    const branch = "harness/task-autocommit";
    const wtDir = createWorktree(mainDir, branch);
    worktrees.push({ dir: wtDir, branch });

    // Leave changes UNCOMMITTED in worktree
    writeFileSync(join(wtDir, "uncommitted.ts"), "export const y = 42;\n");

    // Also add an .omc file that should be excluded from auto-commit
    mkdirSync(join(wtDir, ".omc"), { recursive: true });
    writeFileSync(join(wtDir, ".omc", "state.json"), '{"test": true}');

    const gate = new MergeGate(pipelineConfig, mainDir, realMergeGitOps);
    const result = await gate.enqueue("task-autocommit", wtDir, branch);

    expect(result.status).toBe("merged");

    // uncommitted.ts should be on trunk (auto-committed)
    const files = execSync("ls", { cwd: mainDir, encoding: "utf-8", stdio: "pipe" });
    expect(files).toContain("uncommitted.ts");

    // .omc/ should NOT be on trunk
    const allFiles = execSync("git ls-files", { cwd: mainDir, encoding: "utf-8", stdio: "pipe" });
    expect(allFiles).not.toContain(".omc/");
  });
});
