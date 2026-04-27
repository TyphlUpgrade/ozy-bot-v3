/**
 * Orchestrator stall watchdog (commit 2/2) — integration tests.
 *
 * Verifies the orchestrator-side interval timer that scans
 * SessionManager / ArchitectManager / ReviewGate `getActiveSessions()`,
 * aborts stalled sessions, and emits `session_stalled` events.
 *
 * Uses fake timers + stub managers so the watchdog logic is exercised in
 * isolation from real SDK + git surface area.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { Orchestrator, type OrchestratorEvent } from "../../src/orchestrator.js";
import { SessionManager, type GitOps, type ActiveSessionInfo } from "../../src/session/manager.js";
import { SDKClient } from "../../src/session/sdk.js";
import { MergeGate, type MergeGitOps } from "../../src/gates/merge.js";
import { StateManager } from "../../src/lib/state.js";
import type { HarnessConfig, StallWatchdogConfig } from "../../src/lib/config.js";
import type { ArchitectManager, ActiveArchitectSessionInfo } from "../../src/session/architect.js";
import type { ReviewGate, ActiveReviewerSessionInfo } from "../../src/gates/review.js";

// --- Fixtures ---

let tmpDir: string;

function makeTmpDir(): string {
  const d = join(tmpdir(), `harness-watchdog-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(d, { recursive: true });
  return d;
}

function baseHarnessConfig(root: string, watchdog?: StallWatchdogConfig): HarnessConfig {
  const cfg: HarnessConfig = {
    project: {
      name: "test",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "worktrees"),
      session_dir: join(root, "sessions"),
    },
    pipeline: {
      poll_interval: 60,
      test_command: "echo ok",
      max_retries: 3,
      test_timeout: 180,
      escalation_timeout: 14400,
      retry_delay_ms: 1000,
    },
    discord: {
      bot_token_env: "TOKEN",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {},
    },
  };
  if (watchdog) cfg.stall_watchdog = watchdog;
  return cfg;
}

function stubGitOps(): GitOps {
  return {
    createWorktree: () => { /* no-op */ },
    removeWorktree: () => { /* no-op */ },
    branchExists: () => false,
    deleteBranch: () => { /* no-op */ },
  };
}

function stubMergeGitOps(): MergeGitOps {
  return {
    hasUncommittedChanges: () => false,
    autoCommit: () => "sha",
    getHeadSha: () => "sha",
    rebase: () => ({ success: true, conflictFiles: [] }),
    rebaseAbort: () => { /* no-op */ },
    mergeNoFf: () => "merge-sha",
    revertLastMerge: () => { /* no-op */ },
    runTests: () => ({ success: true, output: "ok" }),
    getTrunkBranch: () => "master",
    branchHasCommitsAheadOfTrunk: () => false,
    diffNameOnly: () => [],
    scrubHarnessFromHead: () => false,
    getUserEmail: () => "test@example",
  };
}

interface Harness {
  orch: Orchestrator;
  events: OrchestratorEvent[];
  setExecutorSessions: (sessions: ActiveSessionInfo[]) => void;
  setArchitectSessions: (sessions: ActiveArchitectSessionInfo[]) => void;
  setReviewerSessions: (sessions: ActiveReviewerSessionInfo[]) => void;
  abortSpies: Map<string, ReturnType<typeof vi.fn>>;
}

function setupHarness(opts: {
  watchdog?: StallWatchdogConfig;
  withArchitect?: boolean;
  withReviewer?: boolean;
}): Harness {
  const root = join(tmpDir, `case-${Math.random().toString(36).slice(2)}`);
  mkdirSync(join(root, "tasks"), { recursive: true });
  const config = baseHarnessConfig(root, opts.watchdog);

  const sdk = new SDKClient(() => { throw new Error("query not used"); });
  const state = new StateManager(join(root, "state.json"));
  const sessionMgr = new SessionManager(sdk, state, config, stubGitOps());
  const mergeGate = new MergeGate(config.pipeline, root, stubMergeGitOps());

  let executorSessions: ActiveSessionInfo[] = [];
  let architectSessions: ActiveArchitectSessionInfo[] = [];
  let reviewerSessions: ActiveReviewerSessionInfo[] = [];
  const abortSpies = new Map<string, ReturnType<typeof vi.fn>>();

  // Override getActiveSessions on the real SessionManager so we don't need a
  // live SDK stream. Per I-10 the orchestrator only sees the plain shape.
  vi.spyOn(sessionMgr, "getActiveSessions").mockImplementation(() => executorSessions);

  let architectStub: ArchitectManager | undefined;
  if (opts.withArchitect) {
    architectStub = {
      getActiveSessions: () => architectSessions,
      isAlive: () => true,
    } as unknown as ArchitectManager;
  }

  let reviewerStub: ReviewGate | undefined;
  if (opts.withReviewer) {
    reviewerStub = {
      getActiveSessions: () => reviewerSessions,
      get arbitrationThreshold(): number { return 2; },
    } as unknown as ReviewGate;
  }

  const orch = new Orchestrator({
    sessionManager: sessionMgr,
    mergeGate,
    stateManager: state,
    config,
    architectManager: architectStub,
    reviewGate: reviewerStub,
  });
  const events: OrchestratorEvent[] = [];
  orch.on((e) => events.push(e));

  return {
    orch,
    events,
    setExecutorSessions: (s) => { executorSessions = s; },
    setArchitectSessions: (s) => { architectSessions = s; },
    setReviewerSessions: (s) => { reviewerSessions = s; },
    abortSpies,
  };
}

function makeSession<T extends { taskId: string; lastActivityAt: number; abort: () => void }>(
  taskId: string,
  ageMs: number,
  spies: Map<string, ReturnType<typeof vi.fn>>,
  tier?: T["tier" & keyof T],
): T {
  const abort = vi.fn();
  spies.set(taskId, abort);
  // taskId / lastActivityAt / abort fields match all three Active*SessionInfo shapes.
  // tier is set per-call by the caller using spread.
  return {
    taskId,
    lastActivityAt: Date.now() - ageMs,
    abort,
    ...(tier !== undefined ? { tier } : {}),
  } as unknown as T;
}

beforeEach(() => {
  tmpDir = makeTmpDir();
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("Orchestrator stall watchdog (commit 2/2)", () => {
  it("disabled by default → no session_stalled emitted even if a session looks stalled", async () => {
    vi.useFakeTimers();
    try {
      const h = setupHarness({}); // watchdog absent
      h.setExecutorSessions([
        makeSession<ActiveSessionInfo>("task-1", 999_999_999, h.abortSpies, "executor"),
      ]);
      h.orch.start();
      // Advance well past any plausible interval.
      vi.advanceTimersByTime(10 * 60 * 1000);
      const stalled = h.events.filter((e) => e.type === "session_stalled");
      expect(stalled.length).toBe(0);
      await h.orch.shutdown();
    } finally {
      vi.useRealTimers();
    }
  });

  it("enabled + executor session past threshold → emits session_stalled and calls abort", async () => {
    vi.useFakeTimers();
    try {
      const h = setupHarness({
        watchdog: {
          enabled: true,
          check_interval_ms: 1_000,
          executor_threshold_ms: 60_000,
        },
      });
      // Pre-populate a session whose lastActivityAt is older than threshold.
      const sessions = [makeSession<ActiveSessionInfo>("task-stale", 60_001, h.abortSpies, "executor")];
      h.setExecutorSessions(sessions);
      h.orch.start();

      // Fire one watchdog tick.
      vi.advanceTimersByTime(1_000);

      const stalled = h.events.filter((e): e is Extract<OrchestratorEvent, { type: "session_stalled" }> =>
        e.type === "session_stalled",
      );
      expect(stalled.length).toBe(1);
      expect(stalled[0].taskId).toBe("task-stale");
      expect(stalled[0].tier).toBe("executor");
      expect(stalled[0].stalledForMs).toBeGreaterThanOrEqual(60_000);
      expect(stalled[0].aborted).toBe(true);
      expect(h.abortSpies.get("task-stale")).toHaveBeenCalledOnce();

      await h.orch.shutdown();
    } finally {
      vi.useRealTimers();
    }
  });

  it("session active within threshold window → no event emitted", async () => {
    vi.useFakeTimers();
    try {
      const h = setupHarness({
        watchdog: {
          enabled: true,
          check_interval_ms: 1_000,
          executor_threshold_ms: 60_000,
        },
      });
      h.setExecutorSessions([
        // 30s old → well under 60s threshold.
        makeSession<ActiveSessionInfo>("task-fresh", 30_000, h.abortSpies, "executor"),
      ]);
      h.orch.start();
      vi.advanceTimersByTime(5_000);

      const stalled = h.events.filter((e) => e.type === "session_stalled");
      expect(stalled.length).toBe(0);
      expect(h.abortSpies.get("task-fresh")).not.toHaveBeenCalled();

      await h.orch.shutdown();
    } finally {
      vi.useRealTimers();
    }
  });

  it("multiple stalled sessions across tiers → emits one event per stalled session with correct tier", async () => {
    vi.useFakeTimers();
    try {
      const h = setupHarness({
        watchdog: {
          enabled: true,
          check_interval_ms: 1_000,
          executor_threshold_ms: 10_000,
          architect_threshold_ms: 20_000,
          reviewer_threshold_ms: 5_000,
        },
        withArchitect: true,
        withReviewer: true,
      });
      h.setExecutorSessions([
        makeSession<ActiveSessionInfo>("exec-a", 11_000, h.abortSpies, "executor"),
        makeSession<ActiveSessionInfo>("exec-b", 12_000, h.abortSpies, "executor"),
      ]);
      h.setArchitectSessions([
        makeSession<ActiveArchitectSessionInfo>("proj-1", 21_000, h.abortSpies, "architect"),
      ]);
      h.setReviewerSessions([
        makeSession<ActiveReviewerSessionInfo>("rev-x", 6_000, h.abortSpies, "reviewer"),
      ]);
      h.orch.start();
      vi.advanceTimersByTime(1_000);

      const stalled = h.events.filter((e): e is Extract<OrchestratorEvent, { type: "session_stalled" }> =>
        e.type === "session_stalled",
      );
      expect(stalled.length).toBe(4);

      const byId = new Map(stalled.map((e) => [e.taskId, e]));
      expect(byId.get("exec-a")?.tier).toBe("executor");
      expect(byId.get("exec-b")?.tier).toBe("executor");
      expect(byId.get("proj-1")?.tier).toBe("architect");
      expect(byId.get("rev-x")?.tier).toBe("reviewer");

      // All four abort callbacks fired.
      for (const id of ["exec-a", "exec-b", "proj-1", "rev-x"]) {
        expect(h.abortSpies.get(id)).toHaveBeenCalledOnce();
      }

      await h.orch.shutdown();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shutdown() clears the watchdog interval", async () => {
    vi.useFakeTimers();
    try {
      const h = setupHarness({
        watchdog: { enabled: true, check_interval_ms: 1_000, executor_threshold_ms: 60_000 },
      });
      const baseTimers = vi.getTimerCount();
      h.orch.start();
      // start synchronously registers only the watchdog interval (the poll
      // self-schedule happens after an await).
      expect(vi.getTimerCount() - baseTimers).toBe(1);
      await h.orch.shutdown();
      // Watchdog cleared on shutdown.
      expect(vi.getTimerCount()).toBe(baseTimers);
    } finally {
      vi.useRealTimers();
    }
  });

  it("aborted=false when abort callback throws", async () => {
    vi.useFakeTimers();
    try {
      const h = setupHarness({
        watchdog: { enabled: true, check_interval_ms: 1_000, executor_threshold_ms: 10_000 },
      });
      const throwingAbort = vi.fn(() => { throw new Error("boom"); });
      h.abortSpies.set("task-bad", throwingAbort);
      h.setExecutorSessions([
        {
          taskId: "task-bad",
          tier: "executor",
          lastActivityAt: Date.now() - 11_000,
          abort: throwingAbort,
        },
      ]);
      h.orch.start();
      vi.advanceTimersByTime(1_000);

      const stalled = h.events.filter((e): e is Extract<OrchestratorEvent, { type: "session_stalled" }> =>
        e.type === "session_stalled",
      );
      expect(stalled.length).toBe(1);
      expect(stalled[0].aborted).toBe(false);

      await h.orch.shutdown();
    } finally {
      vi.useRealTimers();
    }
  });
});
