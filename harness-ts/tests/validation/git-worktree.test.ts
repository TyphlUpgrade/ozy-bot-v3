/**
 * Validation tests for realGitOps — exercises the real git worktree implementation
 * against actual git repositories in temp directories.
 *
 * These are NOT unit tests (no mocks). They test the real git command execution.
 *
 * KNOWN COSMETIC ISSUE:
 * realGitOps.createWorktree calls mkdirSync(worktreePath, { recursive: true }) BEFORE
 * running `git worktree add -b <branch> <path>`. Git expects the target path to either
 * not exist OR be an empty directory. In practice, git worktree add does accept a
 * pre-existing empty directory, so this does not cause a failure — but the mkdir is
 * redundant and could mask errors if git fails for a different reason.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { execSync } from "node:child_process";
import { realGitOps } from "../../src/session/manager.js";

// ---------------------------------------------------------------------------
// Test repo setup helpers
// ---------------------------------------------------------------------------

function makeTestRepo(): string {
  const repoDir = mkdtempSync(join(tmpdir(), "harness-git-validation-"));

  // Configure git identity for this repo (avoids "user.email" failures in CI)
  execSync("git init", { cwd: repoDir, stdio: "pipe" });
  execSync('git config user.email "test@harness.local"', { cwd: repoDir, stdio: "pipe" });
  execSync('git config user.name "Harness Test"', { cwd: repoDir, stdio: "pipe" });

  // Create an initial commit — git worktree add requires at least one commit
  writeFileSync(join(repoDir, "README.md"), "# test repo\n");
  execSync("git add README.md", { cwd: repoDir, stdio: "pipe" });
  execSync('git commit -m "initial commit"', { cwd: repoDir, stdio: "pipe" });

  return repoDir;
}

/** Generate a unique branch name to avoid collisions between tests. */
function uniqueBranch(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

// ---------------------------------------------------------------------------
// Test state
// ---------------------------------------------------------------------------

let repoDir: string;
let worktreeDir: string;

beforeEach(() => {
  repoDir = makeTestRepo();
  // worktreeDir is a sibling of repoDir (outside the repo) so git doesn't complain
  // about nested repos
  worktreeDir = mkdtempSync(join(tmpdir(), "harness-git-wt-"));
  // Remove the dir immediately — createWorktree will recreate it (and git wants
  // either no path or an empty dir; we test both cases)
  rmSync(worktreeDir, { recursive: true, force: true });
});

afterEach(() => {
  rmSync(repoDir, { recursive: true, force: true });
  rmSync(worktreeDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// createWorktree
// ---------------------------------------------------------------------------

describe("realGitOps.createWorktree", () => {
  it("creates the worktree directory on disk", () => {
    const branch = uniqueBranch("test-create-dir");

    realGitOps.createWorktree(repoDir, branch, worktreeDir);

    expect(existsSync(worktreeDir)).toBe(true);
  });

  it("creates the named branch in the repo", () => {
    const branch = uniqueBranch("test-create-branch");

    realGitOps.createWorktree(repoDir, branch, worktreeDir);

    // Verify git knows about the branch
    const result = execSync(`git branch --list ${branch}`, {
      cwd: repoDir,
      stdio: "pipe",
    }).toString().trim();
    expect(result).toContain(branch);
  });

  it("files from main repo are accessible in the worktree", () => {
    const branch = uniqueBranch("test-files-accessible");
    // Add a file to main repo before creating worktree
    writeFileSync(join(repoDir, "shared.txt"), "shared content");
    execSync("git add shared.txt", { cwd: repoDir, stdio: "pipe" });
    execSync('git commit -m "add shared file"', { cwd: repoDir, stdio: "pipe" });

    realGitOps.createWorktree(repoDir, branch, worktreeDir);

    const worktreeFile = join(worktreeDir, "shared.txt");
    expect(existsSync(worktreeFile)).toBe(true);
    expect(readFileSync(worktreeFile, "utf-8")).toBe("shared content");
  });

  it("commits made in worktree are on the new branch, not main", () => {
    const branch = uniqueBranch("test-commit-isolation");

    realGitOps.createWorktree(repoDir, branch, worktreeDir);

    // Commit a new file inside the worktree
    writeFileSync(join(worktreeDir, "worktree-only.txt"), "worktree content");
    execSync("git add worktree-only.txt", { cwd: worktreeDir, stdio: "pipe" });
    execSync('git commit -m "worktree commit"', { cwd: worktreeDir, stdio: "pipe" });

    // The new file must NOT appear in the main repo working tree
    expect(existsSync(join(repoDir, "worktree-only.txt"))).toBe(false);

    // But it must be on the worktree branch
    const logOnBranch = execSync(`git log ${branch} --oneline`, {
      cwd: repoDir,
      stdio: "pipe",
    }).toString();
    expect(logOnBranch).toContain("worktree commit");

    // And it must NOT appear in the log of the main branch
    const mainBranch = execSync("git symbolic-ref --short HEAD", {
      cwd: repoDir,
      stdio: "pipe",
    }).toString().trim();
    const logOnMain = execSync(`git log ${mainBranch} --oneline`, {
      cwd: repoDir,
      stdio: "pipe",
    }).toString();
    expect(logOnMain).not.toContain("worktree commit");
  });

  it("works even when worktree path pre-exists as an empty directory (mkdirSync side-effect)", () => {
    // This tests the actual behaviour of realGitOps.createWorktree: it calls
    // mkdirSync before git worktree add. We verify git tolerates the pre-made dir.
    const branch = uniqueBranch("test-prexisting-dir");
    mkdirSync(worktreeDir, { recursive: true }); // simulate the internal mkdirSync

    // Should not throw
    expect(() => realGitOps.createWorktree(repoDir, branch, worktreeDir)).not.toThrow();
    expect(existsSync(worktreeDir)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// removeWorktree
// ---------------------------------------------------------------------------

describe("realGitOps.removeWorktree", () => {
  it("removes the worktree directory from disk", () => {
    const branch = uniqueBranch("test-remove");
    realGitOps.createWorktree(repoDir, branch, worktreeDir);
    expect(existsSync(worktreeDir)).toBe(true);

    realGitOps.removeWorktree(repoDir, worktreeDir);

    expect(existsSync(worktreeDir)).toBe(false);
  });

  it("removes the worktree registration from git", () => {
    const branch = uniqueBranch("test-remove-registration");
    realGitOps.createWorktree(repoDir, branch, worktreeDir);

    realGitOps.removeWorktree(repoDir, worktreeDir);

    // `git worktree list` should only show the main worktree
    const list = execSync("git worktree list", {
      cwd: repoDir,
      stdio: "pipe",
    }).toString();
    expect(list).not.toContain(worktreeDir);
  });
});

// ---------------------------------------------------------------------------
// branchExists
// ---------------------------------------------------------------------------

describe("realGitOps.branchExists", () => {
  it("returns true for a branch that exists", () => {
    const branch = uniqueBranch("test-branch-exists-true");
    execSync(`git branch ${branch}`, { cwd: repoDir, stdio: "pipe" });

    expect(realGitOps.branchExists(repoDir, branch)).toBe(true);
  });

  it("returns false for a branch that does not exist", () => {
    const branch = uniqueBranch("test-branch-exists-false-nonexistent");

    expect(realGitOps.branchExists(repoDir, branch)).toBe(false);
  });

  it("returns true for a branch created via createWorktree", () => {
    const branch = uniqueBranch("test-branch-exists-via-wt");
    realGitOps.createWorktree(repoDir, branch, worktreeDir);

    expect(realGitOps.branchExists(repoDir, branch)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// deleteBranch
// ---------------------------------------------------------------------------

describe("realGitOps.deleteBranch", () => {
  it("removes an existing branch", () => {
    const branch = uniqueBranch("test-delete-branch");
    execSync(`git branch ${branch}`, { cwd: repoDir, stdio: "pipe" });

    realGitOps.deleteBranch(repoDir, branch);

    const list = execSync("git branch --list", { cwd: repoDir, stdio: "pipe" }).toString();
    expect(list).not.toContain(branch);
  });

  it("throws when deleting a branch that does not exist", () => {
    const branch = uniqueBranch("test-delete-nonexistent");

    expect(() => realGitOps.deleteBranch(repoDir, branch)).toThrow();
  });

  it("requires worktree removal before branch deletion (git constraint)", () => {
    const branch = uniqueBranch("test-delete-checked-out");
    realGitOps.createWorktree(repoDir, branch, worktreeDir);

    expect(() => realGitOps.deleteBranch(repoDir, branch)).toThrow();
  });
});

// ---------------------------------------------------------------------------
// Full lifecycle
// ---------------------------------------------------------------------------

describe("realGitOps full lifecycle", () => {
  it("create worktree → modify file → commit → remove worktree → delete branch", () => {
    const branch = uniqueBranch("test-full-lifecycle");

    // 1. Create worktree
    realGitOps.createWorktree(repoDir, branch, worktreeDir);
    expect(existsSync(worktreeDir)).toBe(true);

    // 2. Modify a file inside the worktree and commit
    writeFileSync(join(worktreeDir, "lifecycle.txt"), "lifecycle change");
    execSync("git add lifecycle.txt", { cwd: worktreeDir, stdio: "pipe" });
    execSync('git commit -m "lifecycle commit"', { cwd: worktreeDir, stdio: "pipe" });

    // Verify commit landed on the branch
    const log = execSync(`git log ${branch} --oneline`, {
      cwd: repoDir,
      stdio: "pipe",
    }).toString();
    expect(log).toContain("lifecycle commit");

    // 3. Remove the worktree
    realGitOps.removeWorktree(repoDir, worktreeDir);
    expect(existsSync(worktreeDir)).toBe(false);

    // 4. branchExists returns true after worktree removal (branch still present)
    expect(realGitOps.branchExists(repoDir, branch)).toBe(true);

    // 5. Delete the branch
    realGitOps.deleteBranch(repoDir, branch);

    // 6. Branch is gone
    expect(realGitOps.branchExists(repoDir, branch)).toBe(false);
  });
});
