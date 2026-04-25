/**
 * Merge gate with exclusive FIFO queue.
 * Auto-commit (O7), rebase, test with timeout (O8), merge --no-ff or revert.
 * Conflict -> shelve + auto-retry (max 3 then escalate).
 */

import { execSync, execFileSync, type ExecSyncOptions } from "node:child_process";
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
  /** Message for the orchestrator-authored commit. Required under propose-then-commit. */
  commitMessage: string;
  /** Legacy compat: if true, skip staging (branch has Executor commits; proceed to rebase). */
  alreadyCommitted?: boolean;
  resolve: (result: MergeResult) => void;
}

// --- Git operations (injectable for testing) ---

export interface MergeGitOps {
  /** Check for uncommitted changes in worktree */
  hasUncommittedChanges(cwd: string): boolean;
  /**
   * Stage + commit all changes excluding .omc/ and .harness/. Uses argv-form
   * `git commit` so `message` is never shell-interpreted. `opts.amend` folds
   * into the previous commit (legacy compat sub-case a).
   */
  autoCommit(cwd: string, message: string, opts?: { amend?: boolean }): string;
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
  /** WA-4: does the branch have commits ahead of trunk? True → legacy Executor committed. */
  branchHasCommitsAheadOfTrunk(cwd: string, trunk: string): boolean;
  /** WA-4: files changed vs trunk (three-dot diff so trunk movement is handled). */
  diffNameOnly(cwd: string, trunk: string): string[];
  /** WA-4: if HEAD tracks .harness/ files, `git rm --cached -r .harness/` + amend. Returns true if scrub fired. */
  scrubHarnessFromHead(cwd: string): boolean;
  /** WA-4 (M3 lazy probe): daemon-host git config user.email. undefined/empty → fail fast. */
  getUserEmail(cwd: string): string | undefined;
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

  autoCommit(cwd: string, message: string, opts?: { amend?: boolean }): string {
    // Stage everything except .omc/ and .harness/ — pathspec exclude handles nested paths.
    // Argv form so `message` is never shell-interpreted.
    execFileSync("git", ["add", "--all", "--", ":!.omc", ":!.harness"], { cwd, stdio: "pipe" });
    const commitArgs = ["commit", "-m", message];
    if (opts?.amend) commitArgs.push("--amend", "--no-edit");
    execFileSync("git", commitArgs, { cwd, stdio: "pipe" });
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
    // Prefer origin/HEAD (remote-tracking repo), then local HEAD (local-only repo),
    // then master as last-resort default.
    try {
      const ref = execSync(
        "git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null || git symbolic-ref HEAD 2>/dev/null || echo refs/heads/master",
        { ...execOpts(trunkCwd), shell: "/bin/bash" },
      ) as unknown as string;
      return ref.trim().replace("refs/remotes/origin/", "").replace("refs/heads/", "");
    } catch {
      return "master";
    }
  },

  branchHasCommitsAheadOfTrunk(cwd: string, trunk: string): boolean {
    try {
      const out = execFileSync("git", ["rev-list", "--count", `${trunk}..HEAD`], {
        cwd, stdio: "pipe", encoding: "utf-8",
      }) as unknown as string;
      return parseInt(out.trim(), 10) > 0;
    } catch {
      return false;
    }
  },

  diffNameOnly(cwd: string, trunk: string): string[] {
    try {
      const out = execFileSync("git", ["diff", `${trunk}...HEAD`, "--name-only"], {
        cwd, stdio: "pipe", encoding: "utf-8",
      }) as unknown as string;
      return out.split("\n").map((l) => l.trim()).filter((l) => l.length > 0);
    } catch {
      return [];
    }
  },

  scrubHarnessFromHead(cwd: string): boolean {
    try {
      const tracked = execFileSync("git", ["ls-tree", "-r", "--name-only", "HEAD", ".harness"], {
        cwd, stdio: "pipe", encoding: "utf-8",
      }) as unknown as string;
      if (tracked.trim().length === 0) return false;
      execFileSync("git", ["rm", "-r", "--cached", ".harness"], { cwd, stdio: "pipe" });
      execFileSync("git", ["commit", "--amend", "--no-edit"], { cwd, stdio: "pipe" });
      return true;
    } catch {
      return false;
    }
  },

  getUserEmail(cwd: string): string | undefined {
    try {
      const out = execFileSync("git", ["config", "--get", "user.email"], {
        cwd, stdio: "pipe", encoding: "utf-8",
      }) as unknown as string;
      const trimmed = out.trim();
      return trimmed.length > 0 ? trimmed : undefined;
    } catch {
      return undefined;
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
  /** M3 lazy probe: verified once on first enqueueProposed call. */
  private userEmailProbed = false;

  constructor(config: PipelineConfig, trunkCwd: string, gitOps?: MergeGitOps) {
    this.config = config;
    this.trunkCwd = trunkCwd;
    this.gitOps = gitOps ?? realMergeGitOps;
  }

  /**
   * Propose-then-commit entry point. Stages the worktree diff on the phase
   * branch with a single orchestrator-authored commit (message supplied by
   * caller), then proceeds through rebase + test + merge. See WA-4 plan
   * for compat sub-cases.
   */
  enqueueProposed(
    taskId: string,
    worktreePath: string,
    branchName: string,
    commitMessage: string,
    opts?: { alreadyCommitted?: boolean },
  ): Promise<MergeResult> {
    if (!this.userEmailProbed) {
      const email = this.gitOps.getUserEmail(this.trunkCwd);
      if (!email) {
        return Promise.resolve({
          status: "error",
          error:
            "MergeGate: 'git config user.email' must be set on the daemon host before orchestrator-staged commits can run. Set it with 'git config --global user.email <addr>'.",
        });
      }
      this.userEmailProbed = true;
    }
    return new Promise<MergeResult>((resolve) => {
      this.queue.push({
        taskId,
        worktreePath,
        branchName,
        commitMessage,
        alreadyCommitted: opts?.alreadyCommitted,
        resolve,
      });
      this.processNext();
    });
  }

  /** Legacy compat wrapper — delegates to enqueueProposed with default message. */
  enqueue(taskId: string, worktreePath: string, branchName: string): Promise<MergeResult> {
    return this.enqueueProposed(taskId, worktreePath, branchName, "harness: auto-commit agent work");
  }

  /** Canonical trunk-branch accessor. Used by orchestrator to wire ReviewGate. */
  getTrunkBranch(): string {
    return this.gitOps.getTrunkBranch(this.trunkCwd);
  }

  /**
   * WA-6: does the worktree branch have commits ahead of trunk? Used by
   * `recoverFromCrash` to distinguish "crashed before stage" vs
   * "crashed after orchestrator commit but before merge" in the `merging`
   * recovery branch.
   */
  branchHasCommitsAheadOfTrunk(worktreePath: string, trunk?: string): boolean {
    return this.gitOps.branchHasCommitsAheadOfTrunk(worktreePath, trunk ?? this.getTrunkBranch());
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
    const { worktreePath, branchName, commitMessage } = req;
    const trunk = this.gitOps.getTrunkBranch(this.trunkCwd);

    // Step 1 — propose-then-commit + legacy compat.
    const hasCommits = this.gitOps.branchHasCommitsAheadOfTrunk(worktreePath, trunk);
    const hasUncommitted = this.gitOps.hasUncommittedChanges(worktreePath);

    if (req.alreadyCommitted) {
      // Crash-recovery path: caller asserts branch already carries the orchestrator
      // commit. Skip stage/commit entirely and proceed to rebase.
    } else if (hasCommits && hasUncommitted) {
      // Sub-case (a): legacy Executor committed AND has additional uncommitted changes.
      console.warn(
        `WARN legacy_executor_commit (sub-case a) on branch ${branchName}: ` +
          `amending existing commit to fold in uncommitted changes`,
      );
      try {
        this.gitOps.autoCommit(worktreePath, commitMessage, { amend: true });
      } catch (err) {
        return { status: "error", error: `legacy_commit_amend_failed: ${(err as Error).message}` };
      }
    } else if (hasCommits && !hasUncommitted) {
      // Sub-case (b): legacy Executor committed cleanly. Scrub any .harness/ pollution.
      console.warn(
        `WARN legacy_executor_commit (sub-case b) on branch ${branchName}: ` +
          `accepting Executor commits; scrubbing .harness/ if tracked`,
      );
      this.gitOps.scrubHarnessFromHead(worktreePath);
    } else if (hasUncommitted) {
      // Canonical propose-then-commit path: orchestrator stages + commits.
      this.gitOps.autoCommit(worktreePath, commitMessage);
    } else {
      // Sub-case (c): empty proposal. Neither commits nor uncommitted changes.
      // Guard against `git commit --allow-empty` by also diffing name-only vs trunk.
      if (this.gitOps.diffNameOnly(worktreePath, trunk).length === 0) {
        return { status: "error", error: "empty_executor_commit" };
      }
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
