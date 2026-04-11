/**
 * Merge gate with exclusive FIFO queue.
 * Auto-commit (O7), rebase, test with timeout (O8), merge --no-ff or revert.
 * Conflict -> shelve + auto-retry (max 3 then escalate).
 */

import { execSync, type ExecSyncOptions } from "node:child_process";
import type { PipelineConfig } from "../lib/config.js";

// --- Types ---

export type MergeResult =
  | { status: "merged"; commitSha: string }
  | { status: "test_failed"; error: string }
  | { status: "test_timeout" }
  | { status: "rebase_conflict"; conflictFiles: string[] }
  | { status: "error"; error: string };

export interface MergeRequest {
  taskId: string;
  worktreePath: string;
  branchName: string;
  resolve: (result: MergeResult) => void;
}

// --- Git operations (injectable for testing) ---

export interface MergeGitOps {
  /** Check for uncommitted changes in worktree */
  hasUncommittedChanges(cwd: string): boolean;
  /** Auto-commit all changes excluding .omc/ */
  autoCommit(cwd: string): string;
  /** Get current branch HEAD sha */
  getHeadSha(cwd: string): string;
  /** Rebase worktree branch onto trunk */
  rebase(cwd: string, trunk: string): { success: boolean; conflictFiles: string[] };
  /** Abort an in-progress rebase */
  rebaseAbort(cwd: string): void;
  /** Merge branch into trunk with --no-ff */
  mergeNoFf(trunkCwd: string, branchName: string): string;
  /** Revert the last merge commit on trunk */
  revertLastMerge(trunkCwd: string): void;
  /** Run test command with timeout, returns success */
  runTests(trunkCwd: string, command: string, timeoutMs: number): { success: boolean; output: string };
  /** Get the trunk branch name */
  getTrunkBranch(trunkCwd: string): string;
}

const execOpts = (cwd: string): ExecSyncOptions => ({ cwd, stdio: "pipe", encoding: "utf-8" });

export const realMergeGitOps: MergeGitOps = {
  hasUncommittedChanges(cwd: string): boolean {
    const status = execSync("git status --porcelain", execOpts(cwd)) as unknown as string;
    // Filter out .omc/ and .harness/ lines
    const significant = status
      .split("\n")
      .filter((l) => l.trim() && !l.includes(".omc/") && !l.includes(".harness/"));
    return significant.length > 0;
  },

  autoCommit(cwd: string): string {
    // Stage everything except .omc/ and .harness/ — pathspec exclude handles nested paths
    execSync("git add --all -- ':!.omc' ':!.harness'", execOpts(cwd));
    execSync('git commit -m "harness: auto-commit agent work"', execOpts(cwd));
    return (execSync("git rev-parse HEAD", execOpts(cwd)) as unknown as string).trim();
  },

  getHeadSha(cwd: string): string {
    return (execSync("git rev-parse HEAD", execOpts(cwd)) as unknown as string).trim();
  },

  rebase(cwd: string, trunk: string): { success: boolean; conflictFiles: string[] } {
    try {
      execSync(`git rebase ${trunk}`, execOpts(cwd));
      return { success: true, conflictFiles: [] };
    } catch (err) {
      // Parse conflict files from git status
      try {
        const status = execSync("git status --porcelain", execOpts(cwd)) as unknown as string;
        const conflicts = status
          .split("\n")
          .filter((l) => l.startsWith("UU") || l.startsWith("AA") || l.startsWith("DD"))
          .map((l) => l.slice(3).trim());
        return { success: false, conflictFiles: conflicts };
      } catch {
        return { success: false, conflictFiles: [] };
      }
    }
  },

  rebaseAbort(cwd: string): void {
    try {
      execSync("git rebase --abort", execOpts(cwd));
    } catch {
      // No rebase in progress
    }
  },

  mergeNoFf(trunkCwd: string, branchName: string): string {
    execSync(`git merge --no-ff ${branchName} -m "harness: merge ${branchName}"`, execOpts(trunkCwd));
    return (execSync("git rev-parse HEAD", execOpts(trunkCwd)) as unknown as string).trim();
  },

  revertLastMerge(trunkCwd: string): void {
    execSync("git revert --no-commit -m 1 HEAD && git commit -m 'harness: revert failed merge'", {
      ...execOpts(trunkCwd),
      shell: "/bin/bash",
    });
  },

  runTests(trunkCwd: string, command: string, timeoutMs: number): { success: boolean; output: string } {
    try {
      const output = execSync(command, {
        ...execOpts(trunkCwd),
        timeout: timeoutMs,
      }) as unknown as string;
      return { success: true, output: output ?? "" };
    } catch (err) {
      const e = err as { killed?: boolean; signal?: string; stdout?: string; stderr?: string; message?: string };
      if (e.killed || e.signal === "SIGTERM") {
        return { success: false, output: "TIMEOUT" };
      }
      return { success: false, output: e.stdout ?? e.stderr ?? e.message ?? "test failed" };
    }
  },

  getTrunkBranch(trunkCwd: string): string {
    // Try to determine the main branch
    try {
      const ref = execSync(
        "git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null || echo refs/heads/master",
        { ...execOpts(trunkCwd), shell: "/bin/bash" },
      ) as unknown as string;
      return ref.trim().replace("refs/remotes/origin/", "").replace("refs/heads/", "");
    } catch {
      return "master";
    }
  },
};

// --- Merge Gate ---

export class MergeGate {
  private readonly queue: MergeRequest[] = [];
  private processing = false;
  private readonly config: PipelineConfig;
  private readonly trunkCwd: string;
  private readonly gitOps: MergeGitOps;

  constructor(config: PipelineConfig, trunkCwd: string, gitOps?: MergeGitOps) {
    this.config = config;
    this.trunkCwd = trunkCwd;
    this.gitOps = gitOps ?? realMergeGitOps;
  }

  /** Enqueue a merge request. Returns promise that resolves with the result. */
  enqueue(taskId: string, worktreePath: string, branchName: string): Promise<MergeResult> {
    return new Promise<MergeResult>((resolve) => {
      this.queue.push({ taskId, worktreePath, branchName, resolve });
      this.processNext();
    });
  }

  /** Process the next item in the queue (FIFO, exclusive) */
  private async processNext(): Promise<void> {
    if (this.processing || this.queue.length === 0) return;
    this.processing = true;

    const request = this.queue.shift()!;
    try {
      const result = await this.processMerge(request);
      request.resolve(result);
    } catch (err) {
      request.resolve({
        status: "error",
        error: (err as Error).message,
      });
    } finally {
      this.processing = false;
      // Process next in queue
      if (this.queue.length > 0) {
        this.processNext();
      }
    }
  }

  /** Execute the merge pipeline for a single request */
  private async processMerge(req: MergeRequest): Promise<MergeResult> {
    const { worktreePath, branchName } = req;
    const trunk = this.gitOps.getTrunkBranch(this.trunkCwd);

    // Step 1: Auto-commit uncommitted changes (O7)
    if (this.gitOps.hasUncommittedChanges(worktreePath)) {
      this.gitOps.autoCommit(worktreePath);
    }

    // Step 2: Rebase onto trunk
    const rebaseResult = this.gitOps.rebase(worktreePath, trunk);
    if (!rebaseResult.success) {
      this.gitOps.rebaseAbort(worktreePath);
      return {
        status: "rebase_conflict",
        conflictFiles: rebaseResult.conflictFiles,
      };
    }

    // Step 3: Merge --no-ff into trunk
    let mergeSha: string;
    try {
      mergeSha = this.gitOps.mergeNoFf(this.trunkCwd, branchName);
    } catch (err) {
      return { status: "error", error: `Merge failed: ${(err as Error).message}` };
    }

    // Step 4: Run tests with timeout (O8)
    const timeoutMs = (this.config.test_timeout ?? 180) * 1000;
    const testResult = this.gitOps.runTests(this.trunkCwd, this.config.test_command, timeoutMs);

    if (!testResult.success) {
      // Revert the merge
      try {
        this.gitOps.revertLastMerge(this.trunkCwd);
      } catch {
        // If revert fails, we're in a bad state — but the merge was the problem
      }

      if (testResult.output === "TIMEOUT") {
        return { status: "test_timeout" };
      }
      return { status: "test_failed", error: testResult.output };
    }

    // Step 5: Success
    return { status: "merged", commitSha: mergeSha };
  }

  /** Current queue depth */
  get queueDepth(): number {
    return this.queue.length;
  }

  /** Whether a merge is currently processing */
  get isProcessing(): boolean {
    return this.processing;
  }
}
