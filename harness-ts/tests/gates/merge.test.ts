import { describe, it, expect, vi, beforeEach } from "vitest";
import { MergeGate, type MergeGitOps, type MergeResult } from "../../src/gates/merge.js";
import type { PipelineConfig } from "../../src/lib/config.js";

// --- Mock helpers ---

function makeConfig(overrides?: Partial<PipelineConfig>): PipelineConfig {
  return {
    poll_interval: 1,
    test_command: "npm test",
    max_retries: 3,
    test_timeout: 180,
    escalation_timeout: 14400,
    retry_delay_ms: 100,
    ...overrides,
  };
}

function mockGitOps(overrides?: Partial<MergeGitOps>): MergeGitOps {
  return {
    hasUncommittedChanges: vi.fn().mockReturnValue(false),
    autoCommit: vi.fn((_cwd: string, _msg: string, _opts?: { amend?: boolean }) => "abc123"),
    getHeadSha: vi.fn().mockReturnValue("abc123"),
    rebase: vi.fn().mockReturnValue({ success: true, conflictFiles: [] }),
    rebaseAbort: vi.fn(),
    mergeNoFf: vi.fn().mockReturnValue("merge-sha-123"),
    revertLastMerge: vi.fn(),
    runTests: vi.fn().mockReturnValue({ success: true, output: "all tests passed" }),
    getTrunkBranch: vi.fn().mockReturnValue("master"),
    branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(false),
    diffNameOnly: vi.fn().mockReturnValue(["src/x.ts"]),
    scrubHarnessFromHead: vi.fn().mockReturnValue(false),
    getUserEmail: vi.fn().mockReturnValue("test@example"),
    ...overrides,
  };
}

// --- Tests ---

describe("MergeGate", () => {
  let config: PipelineConfig;

  beforeEach(() => {
    config = makeConfig();
  });

  describe("happy path", () => {
    it("merges successfully: no uncommitted -> rebase clean -> merge -> tests pass", async () => {
      const git = mockGitOps();
      const gate = new MergeGate(config, "/repo", git);

      const result = await gate.enqueue("task-1", "/worktree/task-1", "harness/task-1");

      expect(result.status).toBe("merged");
      if (result.status === "merged") {
        expect(result.commitSha).toBe("merge-sha-123");
      }
      expect(git.hasUncommittedChanges).toHaveBeenCalledWith("/worktree/task-1");
      expect(git.autoCommit).not.toHaveBeenCalled();
      expect(git.rebase).toHaveBeenCalledWith("/worktree/task-1", "master");
      expect(git.mergeNoFf).toHaveBeenCalledWith("/repo", "harness/task-1");
      expect(git.runTests).toHaveBeenCalled();
    });
  });

  describe("auto-commit (O7)", () => {
    it("auto-commits uncommitted changes before rebase", async () => {
      const git = mockGitOps({
        hasUncommittedChanges: vi.fn().mockReturnValue(true),
      });
      const gate = new MergeGate(config, "/repo", git);

      const result = await gate.enqueue("task-1", "/worktree", "branch-1");

      expect(result.status).toBe("merged");
      expect(git.autoCommit).toHaveBeenCalledWith("/worktree", "harness: auto-commit agent work");
    });

    it("skips auto-commit when worktree is clean", async () => {
      const git = mockGitOps({
        hasUncommittedChanges: vi.fn().mockReturnValue(false),
      });
      const gate = new MergeGate(config, "/repo", git);

      await gate.enqueue("task-1", "/worktree", "branch-1");
      expect(git.autoCommit).not.toHaveBeenCalled();
    });
  });

  describe("rebase conflict", () => {
    it("returns rebase_conflict with file list", async () => {
      const git = mockGitOps({
        rebase: vi.fn().mockReturnValue({
          success: false,
          conflictFiles: ["src/auth.ts", "src/config.ts"],
        }),
      });
      const gate = new MergeGate(config, "/repo", git);

      const result = await gate.enqueue("task-1", "/worktree", "branch-1");

      expect(result.status).toBe("rebase_conflict");
      if (result.status === "rebase_conflict") {
        expect(result.conflictFiles).toEqual(["src/auth.ts", "src/config.ts"]);
      }
      expect(git.rebaseAbort).toHaveBeenCalled();
      // Should NOT proceed to merge or test
      expect(git.mergeNoFf).not.toHaveBeenCalled();
      expect(git.runTests).not.toHaveBeenCalled();
    });
  });

  describe("test failure", () => {
    it("reverts merge on test failure", async () => {
      const git = mockGitOps({
        runTests: vi.fn().mockReturnValue({ success: false, output: "FAIL: auth.test.ts" }),
      });
      const gate = new MergeGate(config, "/repo", git);

      const result = await gate.enqueue("task-1", "/worktree", "branch-1");

      expect(result.status).toBe("test_failed");
      if (result.status === "test_failed") {
        expect(result.error).toContain("FAIL: auth.test.ts");
      }
      expect(git.revertLastMerge).toHaveBeenCalledWith("/repo");
    });
  });

  describe("test timeout (O8)", () => {
    it("reverts merge on test timeout", async () => {
      const git = mockGitOps({
        runTests: vi.fn().mockReturnValue({ success: false, output: "TIMEOUT" }),
      });
      const gate = new MergeGate(config, "/repo", git);

      const result = await gate.enqueue("task-1", "/worktree", "branch-1");

      expect(result.status).toBe("test_timeout");
      expect(git.revertLastMerge).toHaveBeenCalledWith("/repo");
    });

    it("uses configured test_timeout", async () => {
      const git = mockGitOps();
      const gate = new MergeGate(makeConfig({ test_timeout: 300 }), "/repo", git);

      await gate.enqueue("task-1", "/worktree", "branch-1");

      // test_timeout is 300s = 300000ms
      expect(git.runTests).toHaveBeenCalledWith("/repo", "npm test", 300000);
    });
  });

  describe("FIFO queue serialization", () => {
    it("processes two requests in FIFO order", async () => {
      const order: string[] = [];
      const git = mockGitOps({
        mergeNoFf: vi.fn().mockImplementation((_cwd: string, branch: string) => {
          order.push(branch);
          return `sha-${branch}`;
        }),
      });
      const gate = new MergeGate(config, "/repo", git);

      // Enqueue two requests simultaneously
      const p1 = gate.enqueue("task-1", "/wt/1", "branch-1");
      const p2 = gate.enqueue("task-2", "/wt/2", "branch-2");

      const [r1, r2] = await Promise.all([p1, p2]);

      expect(r1.status).toBe("merged");
      expect(r2.status).toBe("merged");
      // FIFO: branch-1 first, then branch-2
      expect(order).toEqual(["branch-1", "branch-2"]);
    });

    it("second request waits while first processes", async () => {
      let firstStarted = false;
      let firstDone = false;
      let secondStartedBeforeFirstDone = false;

      const git = mockGitOps({
        mergeNoFf: vi.fn().mockImplementation((_cwd: string, branch: string) => {
          if (branch === "branch-1") {
            firstStarted = true;
          }
          if (branch === "branch-2" && !firstDone) {
            secondStartedBeforeFirstDone = true;
          }
          return `sha-${branch}`;
        }),
        runTests: vi.fn().mockImplementation(() => {
          if (firstStarted && !firstDone) {
            firstDone = true;
          }
          return { success: true, output: "" };
        }),
      });

      const gate = new MergeGate(config, "/repo", git);

      const p1 = gate.enqueue("t1", "/wt/1", "branch-1");
      const p2 = gate.enqueue("t2", "/wt/2", "branch-2");

      await Promise.all([p1, p2]);

      // Second should NOT have started merging before first completed
      expect(secondStartedBeforeFirstDone).toBe(false);
    });

    it("three concurrent requests serialize correctly", async () => {
      const order: string[] = [];
      const git = mockGitOps({
        mergeNoFf: vi.fn().mockImplementation((_cwd: string, branch: string) => {
          order.push(branch);
          return `sha-${branch}`;
        }),
      });
      const gate = new MergeGate(config, "/repo", git);

      const p1 = gate.enqueue("t1", "/wt/1", "b1");
      const p2 = gate.enqueue("t2", "/wt/2", "b2");
      const p3 = gate.enqueue("t3", "/wt/3", "b3");

      await Promise.all([p1, p2, p3]);
      expect(order).toEqual(["b1", "b2", "b3"]);
    });
  });

  describe("queue state", () => {
    it("reports queue depth", () => {
      const git = mockGitOps();
      const gate = new MergeGate(config, "/repo", git);

      expect(gate.queueDepth).toBe(0);
      // Can't easily test mid-processing depth without async complexity,
      // but structure is correct
    });
  });

  describe("error handling", () => {
    it("returns error if merge --no-ff throws", async () => {
      const git = mockGitOps({
        mergeNoFf: vi.fn().mockImplementation(() => {
          throw new Error("merge: not a fast-forward");
        }),
      });
      const gate = new MergeGate(config, "/repo", git);

      const result = await gate.enqueue("task-1", "/wt", "branch-1");
      expect(result.status).toBe("error");
      if (result.status === "error") {
        expect(result.error).toContain("merge: not a fast-forward");
      }
    });

    it("continues processing queue after error", async () => {
      let callCount = 0;
      const git = mockGitOps({
        mergeNoFf: vi.fn().mockImplementation((_cwd: string, branch: string) => {
          callCount++;
          if (callCount === 1) throw new Error("first merge fails");
          return `sha-${branch}`;
        }),
      });
      const gate = new MergeGate(config, "/repo", git);

      const p1 = gate.enqueue("t1", "/wt/1", "b1");
      const p2 = gate.enqueue("t2", "/wt/2", "b2");

      const [r1, r2] = await Promise.all([p1, p2]);
      expect(r1.status).toBe("error");
      expect(r2.status).toBe("merged");
    });
  });

  // --- WA-4 propose-then-commit ---

  describe("WA-4 enqueueProposed", () => {
    it("stages + commits with the supplied message then merges (canonical path)", async () => {
      const git = mockGitOps({ hasUncommittedChanges: vi.fn().mockReturnValue(true) });
      const gate = new MergeGate(config, "/repo", git);
      const r = await gate.enqueueProposed("t-cp", "/wt", "br-cp", "harness: t-cp — add foo");
      expect(r.status).toBe("merged");
      expect(git.autoCommit).toHaveBeenCalledWith("/wt", "harness: t-cp — add foo");
    });

    it("returns empty_executor_commit when branch and working tree are empty", async () => {
      const git = mockGitOps({
        hasUncommittedChanges: vi.fn().mockReturnValue(false),
        branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(false),
        diffNameOnly: vi.fn().mockReturnValue([]),
      });
      const gate = new MergeGate(config, "/repo", git);
      const r = await gate.enqueueProposed("t-empty", "/wt", "br", "msg");
      expect(r.status).toBe("error");
      if (r.status === "error") expect(r.error).toBe("empty_executor_commit");
    });

    it("sub-case (b): already-committed-clean branch skips stage and scrubs .harness/", async () => {
      const git = mockGitOps({
        hasUncommittedChanges: vi.fn().mockReturnValue(false),
        branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(true),
      });
      const gate = new MergeGate(config, "/repo", git);
      const r = await gate.enqueueProposed("t-legacy", "/wt", "br-legacy", "msg");
      expect(r.status).toBe("merged");
      expect(git.autoCommit).not.toHaveBeenCalled();
      expect(git.scrubHarnessFromHead).toHaveBeenCalledWith("/wt");
    });

    it("sub-case (a): legacy commit + uncommitted changes amends the existing commit", async () => {
      const git = mockGitOps({
        hasUncommittedChanges: vi.fn().mockReturnValue(true),
        branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(true),
      });
      const gate = new MergeGate(config, "/repo", git);
      const r = await gate.enqueueProposed("t-mix", "/wt", "br-mix", "msg-mix");
      expect(r.status).toBe("merged");
      expect(git.autoCommit).toHaveBeenCalledWith("/wt", "msg-mix", { amend: true });
    });

    it("alreadyCommitted=true skips detection and proceeds to rebase", async () => {
      const git = mockGitOps({
        hasUncommittedChanges: vi.fn().mockReturnValue(false),
        branchHasCommitsAheadOfTrunk: vi.fn().mockReturnValue(true),
      });
      const gate = new MergeGate(config, "/repo", git);
      const r = await gate.enqueueProposed("t-rec", "/wt", "br-rec", "msg", { alreadyCommitted: true });
      expect(r.status).toBe("merged");
      expect(git.autoCommit).not.toHaveBeenCalled();
      expect(git.scrubHarnessFromHead).not.toHaveBeenCalled();
    });

    it("legacy enqueue wrapper still works with default commit message", async () => {
      const git = mockGitOps({ hasUncommittedChanges: vi.fn().mockReturnValue(true) });
      const gate = new MergeGate(config, "/repo", git);
      const r = await gate.enqueue("t-leg", "/wt", "br-leg");
      expect(r.status).toBe("merged");
      expect(git.autoCommit).toHaveBeenCalledWith("/wt", "harness: auto-commit agent work");
    });

    it("passes shell-metachars through to autoCommit verbatim (argv form)", async () => {
      const git = mockGitOps({ hasUncommittedChanges: vi.fn().mockReturnValue(true) });
      const gate = new MergeGate(config, "/repo", git);
      const danger = "harness: t — `rm -rf /`; $(echo pwned)";
      await gate.enqueueProposed("t-sh", "/wt", "br-sh", danger);
      expect(git.autoCommit).toHaveBeenCalledWith("/wt", danger);
    });

    it("lazy probe (M3): first call fails when getUserEmail returns undefined", async () => {
      const email = vi.fn()
        .mockReturnValueOnce(undefined)
        .mockReturnValueOnce("test@example");
      const git = mockGitOps({ getUserEmail: email });
      const gate = new MergeGate(config, "/repo", git);
      const r1 = await gate.enqueueProposed("t-probe1", "/wt", "br", "msg");
      expect(r1.status).toBe("error");
      if (r1.status === "error") expect(r1.error).toMatch(/user\.email/);
      // Probe re-runs until first success, then caches.
      const r2 = await gate.enqueueProposed("t-probe2", "/wt", "br", "msg");
      expect(r2.status).toBe("merged");
      await gate.enqueueProposed("t-probe3", "/wt", "br", "msg");
      expect(email).toHaveBeenCalledTimes(2);
    });
  });
});
