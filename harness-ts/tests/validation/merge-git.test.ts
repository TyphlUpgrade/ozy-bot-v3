import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { execSync } from "node:child_process";
import { realMergeGitOps } from "../../src/gates/merge.js";

// --- Helpers ---

function initRepo(dir: string): void {
  const opts = { cwd: dir, stdio: "pipe" as const };
  execSync("git init", opts);
  execSync("git config user.email 'test@harness.local'", opts);
  execSync("git config user.name 'Harness Test'", opts);
  execSync("git config commit.gpgsign false", opts);
  // Initial commit so HEAD exists and branching works
  writeFileSync(join(dir, "README.md"), "initial\n");
  execSync("git add README.md", opts);
  execSync("git commit -m 'initial commit'", opts);
}

function git(dir: string, cmd: string): string {
  return execSync(cmd, { cwd: dir, stdio: "pipe", encoding: "utf-8" }).trim();
}

// --- Tests ---

describe("realMergeGitOps — validation against real git", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "harness-merge-git-"));
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  // -------------------------------------------------------------------------
  // hasUncommittedChanges
  // -------------------------------------------------------------------------

  describe("hasUncommittedChanges", () => {
    it("returns false on a clean repo", () => {
      initRepo(tmpDir);
      expect(realMergeGitOps.hasUncommittedChanges(tmpDir)).toBe(false);
    });

    it("returns true when a tracked file is modified", () => {
      initRepo(tmpDir);
      writeFileSync(join(tmpDir, "README.md"), "modified content\n");
      expect(realMergeGitOps.hasUncommittedChanges(tmpDir)).toBe(true);
    });

    it("ignores changes inside .omc/ directory", () => {
      initRepo(tmpDir);
      mkdirSync(join(tmpDir, ".omc"), { recursive: true });
      writeFileSync(join(tmpDir, ".omc", "notes.md"), "some notes\n");
      expect(realMergeGitOps.hasUncommittedChanges(tmpDir)).toBe(false);
    });

    it("ignores changes inside .harness/ directory", () => {
      initRepo(tmpDir);
      mkdirSync(join(tmpDir, ".harness"), { recursive: true });
      writeFileSync(join(tmpDir, ".harness", "state.json"), '{"active":true}\n');
      expect(realMergeGitOps.hasUncommittedChanges(tmpDir)).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // autoCommit
  // -------------------------------------------------------------------------

  describe("autoCommit", () => {
    it("commits all changes and returns the new HEAD SHA", () => {
      initRepo(tmpDir);
      writeFileSync(join(tmpDir, "src.ts"), "export const x = 1;\n");

      const sha = realMergeGitOps.autoCommit(tmpDir);

      expect(sha).toMatch(/^[0-9a-f]{40}$/);
      expect(sha).toBe(git(tmpDir, "git rev-parse HEAD"));
      // The new file must be part of the commit
      const show = git(tmpDir, "git show --name-only --format='' HEAD");
      expect(show).toContain("src.ts");
    });

    it("does NOT commit files inside .omc/", () => {
      initRepo(tmpDir);
      writeFileSync(join(tmpDir, "work.ts"), "const y = 2;\n");
      mkdirSync(join(tmpDir, ".omc"), { recursive: true });
      writeFileSync(join(tmpDir, ".omc", "secret.md"), "private\n");

      realMergeGitOps.autoCommit(tmpDir);

      const show = git(tmpDir, "git show --name-only --format='' HEAD");
      expect(show).toContain("work.ts");
      expect(show).not.toContain(".omc");
    });
  });

  // -------------------------------------------------------------------------
  // getHeadSha
  // -------------------------------------------------------------------------

  describe("getHeadSha", () => {
    it("returns current HEAD SHA matching git rev-parse HEAD", () => {
      initRepo(tmpDir);
      const expected = git(tmpDir, "git rev-parse HEAD");
      expect(realMergeGitOps.getHeadSha(tmpDir)).toBe(expected);
    });
  });

  // -------------------------------------------------------------------------
  // rebase
  // -------------------------------------------------------------------------

  describe("rebase", () => {
    it("succeeds with no conflicts when branch diverges cleanly from main", () => {
      initRepo(tmpDir);

      // Create a feature branch touching a different file
      git(tmpDir, "git checkout -b feature");
      writeFileSync(join(tmpDir, "feature.ts"), "export const f = 1;\n");
      git(tmpDir, "git add feature.ts");
      git(tmpDir, "git commit -m 'feature work'");

      // Add a commit on main that does not touch feature.ts
      git(tmpDir, "git checkout master");
      writeFileSync(join(tmpDir, "main-work.ts"), "export const m = 1;\n");
      git(tmpDir, "git add main-work.ts");
      git(tmpDir, "git commit -m 'main work'");

      // Rebase feature onto master
      git(tmpDir, "git checkout feature");
      const result = realMergeGitOps.rebase(tmpDir, "master");

      expect(result.success).toBe(true);
      expect(result.conflictFiles).toEqual([]);
    });

    it("returns success:false and conflicting file names when rebase conflicts", () => {
      initRepo(tmpDir);

      // Commit A is the initial commit (already done by initRepo)
      // Both main and feature will modify the same line of file.txt

      // Commit B on main: modify file.txt
      writeFileSync(join(tmpDir, "file.txt"), "main change\n");
      git(tmpDir, "git add file.txt");
      git(tmpDir, "git commit -m 'commit B - main changes file.txt'");

      // Create branch from commit A (initial commit)
      const initialSha = git(tmpDir, "git rev-parse HEAD~1");
      git(tmpDir, `git checkout -b feature ${initialSha}`);

      // Commit C on feature: modify the same file differently
      writeFileSync(join(tmpDir, "file.txt"), "branch change\n");
      git(tmpDir, "git add file.txt");
      git(tmpDir, "git commit -m 'commit C - branch changes file.txt'");

      // Rebase feature onto master — must conflict on file.txt
      const result = realMergeGitOps.rebase(tmpDir, "master");

      expect(result.success).toBe(false);
      expect(result.conflictFiles).toContain("file.txt");
    });
  });

  // -------------------------------------------------------------------------
  // rebaseAbort
  // -------------------------------------------------------------------------

  describe("rebaseAbort", () => {
    it("cleans up an in-progress rebase so the working tree is usable again", () => {
      initRepo(tmpDir);

      // Reproduce the conflict setup from the rebase conflict test
      writeFileSync(join(tmpDir, "file.txt"), "main change\n");
      git(tmpDir, "git add file.txt");
      git(tmpDir, "git commit -m 'main change'");

      const initialSha = git(tmpDir, "git rev-parse HEAD~1");
      git(tmpDir, `git checkout -b feature ${initialSha}`);
      writeFileSync(join(tmpDir, "file.txt"), "branch change\n");
      git(tmpDir, "git add file.txt");
      git(tmpDir, "git commit -m 'branch change'");

      // Trigger the conflict
      realMergeGitOps.rebase(tmpDir, "master");

      // Abort should succeed without throwing
      expect(() => realMergeGitOps.rebaseAbort(tmpDir)).not.toThrow();

      // After abort, git status should be clean (no rebase in progress)
      const status = git(tmpDir, "git status --porcelain");
      expect(status).toBe("");
    });
  });

  // -------------------------------------------------------------------------
  // mergeNoFf
  // -------------------------------------------------------------------------

  describe("mergeNoFf", () => {
    it("creates a merge commit (non-fast-forward) and returns its SHA", () => {
      initRepo(tmpDir);

      // Create a feature branch with one commit
      git(tmpDir, "git checkout -b feature");
      writeFileSync(join(tmpDir, "feature.ts"), "export const f = 1;\n");
      git(tmpDir, "git add feature.ts");
      git(tmpDir, "git commit -m 'feature commit'");

      git(tmpDir, "git checkout master");

      const mergeSha = realMergeGitOps.mergeNoFf(tmpDir, "feature");

      expect(mergeSha).toMatch(/^[0-9a-f]{40}$/);
      expect(mergeSha).toBe(git(tmpDir, "git rev-parse HEAD"));

      // Confirm it is a merge commit (has two parents)
      const parents = git(tmpDir, "git log --pretty=%P -1 HEAD");
      expect(parents.trim().split(/\s+/).length).toBe(2);
    });
  });

  // -------------------------------------------------------------------------
  // revertLastMerge
  // -------------------------------------------------------------------------

  describe("revertLastMerge", () => {
    it("reverts the merge so trunk content matches pre-merge state", () => {
      initRepo(tmpDir);

      // Record file state before merge
      const preMergeContent = readFileSync(join(tmpDir, "README.md"), "utf-8");

      // Feature branch adds a new file
      git(tmpDir, "git checkout -b feature");
      writeFileSync(join(tmpDir, "added.ts"), "export const added = true;\n");
      git(tmpDir, "git add added.ts");
      git(tmpDir, "git commit -m 'feature adds file'");

      git(tmpDir, "git checkout master");
      const preMergeSha = git(tmpDir, "git rev-parse HEAD");

      // Merge the feature branch
      realMergeGitOps.mergeNoFf(tmpDir, "feature");

      // Revert the merge
      realMergeGitOps.revertLastMerge(tmpDir);

      // The revert commit is now HEAD — trunk should be back to pre-merge content
      const currentReadme = readFileSync(join(tmpDir, "README.md"), "utf-8");
      expect(currentReadme).toBe(preMergeContent);

      // The file added by the feature branch should no longer be in tree
      const lsFiles = git(tmpDir, "git ls-files");
      expect(lsFiles).not.toContain("added.ts");

      // HEAD is a new commit (revert), not the original pre-merge SHA
      const currentSha = git(tmpDir, "git rev-parse HEAD");
      expect(currentSha).not.toBe(preMergeSha);
    });
  });

  // -------------------------------------------------------------------------
  // runTests
  // -------------------------------------------------------------------------

  describe("runTests", () => {
    it("returns success:true for a command that exits 0", () => {
      initRepo(tmpDir);
      const result = realMergeGitOps.runTests(tmpDir, "echo ok", 5000);
      expect(result.success).toBe(true);
      expect(result.output).toContain("ok");
    });

    it("returns success:false for a command that exits non-zero", () => {
      initRepo(tmpDir);
      const result = realMergeGitOps.runTests(tmpDir, "exit 1", 5000);
      expect(result.success).toBe(false);
      expect(result.output).not.toBe("TIMEOUT");
    });

    it("returns success:false with TIMEOUT output when command exceeds timeoutMs", () => {
      initRepo(tmpDir);
      const result = realMergeGitOps.runTests(tmpDir, "sleep 10", 100);
      expect(result.success).toBe(false);
      expect(result.output).toBe("TIMEOUT");
    });
  });

  // -------------------------------------------------------------------------
  // getTrunkBranch
  // -------------------------------------------------------------------------

  describe("getTrunkBranch", () => {
    it("returns the current branch name for a local-only repo", () => {
      initRepo(tmpDir);
      // No remote configured — falls back to detecting the default branch name
      const branch = realMergeGitOps.getTrunkBranch(tmpDir);
      // Should be a non-empty string (typically 'master' or 'main')
      expect(typeof branch).toBe("string");
      expect(branch.length).toBeGreaterThan(0);
    });
  });
});
