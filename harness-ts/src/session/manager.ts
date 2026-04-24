/**
 * Session manager — git worktree lifecycle, task spawn, completion detection.
 * Delegates SDK interaction to SDKClient.
 */

import { existsSync, readFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { execSync } from "node:child_process";
import type { Query, SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { SDKClient, type SessionConfig, type SessionResult } from "./sdk.js";
import { StateManager, type TaskRecord } from "../lib/state.js";
import {
  DEFAULT_EXECUTOR_SYSTEM_PROMPT,
  PERSISTENT_SESSION_WARN_THRESHOLD_DEFAULT,
  type HarnessConfig,
} from "../lib/config.js";
import type { ConfidenceAssessment } from "../lib/types.js";

// --- Completion signal schema ---

export interface CompletionSignal {
  status: "success" | "failure";
  commitSha: string;
  summary: string;
  filesChanged: string[];
  // Phase 2A enrichment (optional — backward compatible)
  understanding?: string;
  assumptions?: string[];
  nonGoals?: string[];
  confidence?: ConfidenceAssessment;
}

const VALID_SCOPE_CLARITY = new Set(["clear", "partial", "unclear"]);
const VALID_DESIGN_CERTAINTY = new Set(["obvious", "alternatives_exist", "guessing"]);
const VALID_TEST_COVERAGE = new Set(["verifiable", "partial", "untestable"]);

/** Validate a ConfidenceAssessment object. Returns null if malformed. */
function validateConfidence(raw: unknown): ConfidenceAssessment | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  if (!VALID_SCOPE_CLARITY.has(obj.scopeClarity as string)) return null;
  if (!VALID_DESIGN_CERTAINTY.has(obj.designCertainty as string)) return null;
  if (!VALID_TEST_COVERAGE.has(obj.testCoverage as string)) return null;
  if (!Array.isArray(obj.assumptions)) return null;
  if (!Array.isArray(obj.openQuestions)) return null;
  // Validate each assumption has required shape
  for (const a of obj.assumptions) {
    if (!a || typeof a !== "object") return null;
    if (typeof a.description !== "string") return null;
    if (a.impact !== "high" && a.impact !== "low") return null;
    if (typeof a.reversible !== "boolean") return null;
  }
  return obj as unknown as ConfidenceAssessment;
}

function validateCompletion(raw: unknown): CompletionSignal | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  // Required fields
  if (typeof obj.status !== "string" || !["success", "failure"].includes(obj.status)) return null;
  if (typeof obj.commitSha !== "string" || obj.commitSha.length === 0) return null;
  if (typeof obj.summary !== "string") return null;
  if (!Array.isArray(obj.filesChanged)) return null;

  const signal: CompletionSignal = {
    status: obj.status as "success" | "failure",
    commitSha: obj.commitSha as string,
    summary: obj.summary as string,
    filesChanged: obj.filesChanged as string[],
  };

  // Optional enrichment fields — strip malformed, keep valid (B7 pattern)
  if (typeof obj.understanding === "string") signal.understanding = obj.understanding;
  if (Array.isArray(obj.assumptions)) {
    const valid = obj.assumptions.filter((a) => typeof a === "string");
    if (valid.length > 0) signal.assumptions = valid;
  }
  if (Array.isArray(obj.nonGoals)) {
    const valid = obj.nonGoals.filter((g) => typeof g === "string");
    if (valid.length > 0) signal.nonGoals = valid;
  }
  if (obj.confidence !== undefined) {
    const validated = validateConfidence(obj.confidence);
    if (validated) signal.confidence = validated;
    // Malformed confidence silently stripped (B7)
  }

  return signal;
}

// --- Git helpers (injectable for testing) ---

export interface GitOps {
  createWorktree(basePath: string, branchName: string, worktreePath: string): void;
  removeWorktree(repoPath: string, worktreePath: string): void;
  branchExists(repoPath: string, branchName: string): boolean;
  deleteBranch(repoPath: string, branchName: string): void;
}

export const realGitOps: GitOps = {
  createWorktree(basePath: string, branchName: string, worktreePath: string): void {
    mkdirSync(worktreePath, { recursive: true });
    execSync(`git worktree add -b ${branchName} ${worktreePath}`, {
      cwd: basePath,
      stdio: "pipe",
    });
  },

  removeWorktree(repoPath: string, worktreePath: string): void {
    execSync(`git worktree remove --force ${worktreePath}`, { cwd: repoPath, stdio: "pipe" });
  },

  branchExists(repoPath: string, branchName: string): boolean {
    try {
      execSync(`git rev-parse --verify ${branchName}`, { cwd: repoPath, stdio: "pipe" });
      return true;
    } catch {
      return false;
    }
  },

  deleteBranch(repoPath: string, branchName: string): void {
    execSync(`git branch -D ${branchName}`, { cwd: repoPath, stdio: "pipe" });
  },
};

// --- Tmux helpers (Wave 1 Item 4 — injectable for testing) ---

export interface TmuxOps {
  /** Kill all tmux sessions whose names contain the given substring. Silent on failure. */
  killSessionsByPattern(pattern: string): void;
}

export const realTmuxOps: TmuxOps = {
  killSessionsByPattern(pattern: string): void {
    let output: string;
    try {
      output = execSync(`tmux list-sessions -F "#{session_name}"`, {
        stdio: ["pipe", "pipe", "pipe"],
        encoding: "utf-8",
      });
    } catch {
      return; // No tmux server or no sessions
    }
    const names = output.trim().split("\n").filter(Boolean);
    for (const name of names) {
      if (!name.includes(pattern)) continue;
      try {
        execSync(`tmux kill-session -t "${name}"`, { stdio: "pipe" });
      } catch {
        // Session already gone — non-fatal
      }
    }
  },
};

// --- Defaults (Wave 1 Items 1 + 3) ---

/** Lifecycle-escaping tools blocked by default for every session. */
const DEFAULT_DISALLOWED_TOOLS: readonly string[] = [
  "CronCreate",
  "CronDelete",
  "CronList",
  "RemoteTrigger",
  "ScheduleWakeup",
];

/** Plugins loaded by default for every session. Operator overrides via config.pipeline.plugins.
 * Key format: `plugin-name@marketplace` (Claude Code plugin registry convention).
 * `oh-my-claudecode@omc` loads OMC agents/skills; `caveman@caveman` applies cost-compression.
 */
const DEFAULT_PLUGINS: Readonly<Record<string, boolean>> = {
  "oh-my-claudecode@omc": true,
  "caveman@caveman": true,
};

// --- Active session tracking ---

export interface ActiveSession {
  taskId: string;
  sessionId?: string;
  query: Query;
  abortController: AbortController;
  worktreePath: string;
  branchName: string;
  startedAt: string;
  timeoutHandle?: ReturnType<typeof setTimeout>;
}

// --- Session Manager ---

export class SessionManager {
  private readonly sdk: SDKClient;
  private readonly state: StateManager;
  private readonly config: HarnessConfig;
  private readonly gitOps: GitOps;
  private readonly tmuxOps: TmuxOps;
  private readonly activeSessions: Map<string, ActiveSession> = new Map();
  private cumulativeSessionSpawns = 0;

  constructor(
    sdk: SDKClient,
    state: StateManager,
    config: HarnessConfig,
    gitOps?: GitOps,
    tmuxOps?: TmuxOps,
  ) {
    this.sdk = sdk;
    this.state = state;
    this.config = config;
    this.gitOps = gitOps ?? realGitOps;
    this.tmuxOps = tmuxOps ?? realTmuxOps;
  }

  /** Create a worktree for a task */
  createWorktree(taskId: string): { worktreePath: string; branchName: string } {
    const branchName = `harness/task-${taskId}`;
    const worktreePath = join(this.config.project.worktree_base, `task-${taskId}`);
    this.gitOps.createWorktree(this.config.project.root, branchName, worktreePath);
    return { worktreePath, branchName };
  }

  /** Clean up worktree and branch for a task. Also sweeps any tmux sessions spawned by /team or omc-teams (Wave 1 Item 4). */
  cleanupWorktree(taskId: string): void {
    const session = this.activeSessions.get(taskId);
    const worktreePath = session?.worktreePath
      ?? join(this.config.project.worktree_base, `task-${taskId}`);
    const branchName = session?.branchName ?? `harness/task-${taskId}`;

    try {
      this.gitOps.removeWorktree(this.config.project.root, worktreePath);
    } catch {
      // Already removed
    }
    try {
      if (this.gitOps.branchExists(this.config.project.root, branchName)) {
        this.gitOps.deleteBranch(this.config.project.root, branchName);
      }
    } catch {
      // Already deleted
    }
    // Wave 1 Item 4: kill tmux sessions matching this task (from /team or omc-teams spawns)
    try {
      this.tmuxOps.killSessionsByPattern(`task-${taskId}`);
    } catch {
      // Tmux cleanup failure is non-fatal
    }
    // TODO Wave 1.5b: when TaskRecord.projectId lands, also sweep `architect-{projectId}`
    // pattern IF no other active phases on that project (guarded via projectStore.hasActivePhases()).
    // See plan Section F Wave 1 Item 4 extension.
  }

  /**
   * Clean up all resources for a completed/failed/aborted project (Wave 1 Item 4 extension).
   * Removes the Architect worktree + branch and sweeps any tmux sessions matching the project.
   * Safe to call when no Architect worktree exists (silent no-op on missing paths).
   *
   * NOTE: the `projectStore.hasActivePhases()` guard is not yet wired — projectStore lands in
   * Wave 1.5b. Until then this method is unconditional: callers must ensure the project is
   * terminal before invoking. See plan Section C.1 + Section F Wave 1 Item 4 extension.
   */
  async cleanupProject(projectId: string): Promise<void> {
    const branchName = `harness/architect-${projectId}`;
    const worktreePath = join(this.config.project.worktree_base, `architect-${projectId}`);

    try {
      this.gitOps.removeWorktree(this.config.project.root, worktreePath);
    } catch {
      // Already removed or never created
    }
    try {
      if (this.gitOps.branchExists(this.config.project.root, branchName)) {
        this.gitOps.deleteBranch(this.config.project.root, branchName);
      }
    } catch {
      // Already deleted
    }
    try {
      this.tmuxOps.killSessionsByPattern(`architect-${projectId}`);
    } catch {
      // Tmux cleanup failure is non-fatal
    }
  }

  /**
   * Spawn a task: create worktree, start SDK session, track it.
   * Returns the SessionResult when the session completes.
   */
  async spawnTask(
    task: TaskRecord,
    onMessage?: (msg: SDKMessage) => void,
    timeoutMs?: number,
  ): Promise<{ result: SessionResult; completion: CompletionSignal | null }> {
    // Create worktree
    const { worktreePath, branchName } = this.createWorktree(task.id);

    // Update task state
    this.state.updateTask(task.id, { worktreePath, branchName });
    this.state.transition(task.id, "active");

    // Wave 1 Item 3: merge default + config-specified disallowed tools (config extends, never reduces)
    const configDisallowed = this.config.pipeline.disallowed_tools ?? [];
    const disallowedTools = Array.from(
      new Set([...DEFAULT_DISALLOWED_TOOLS, ...configDisallowed]),
    );

    // Wave 1 Item 1: merge default + config-specified plugins (config overrides per-entry)
    const configPlugins = this.config.pipeline.plugins ?? {};
    const enabledPlugins = { ...DEFAULT_PLUGINS, ...configPlugins };

    // Spawn SDK session
    const sessionConfig: SessionConfig = {
      prompt: task.prompt,
      cwd: worktreePath,
      settingSources: ["project"],
      permissionMode: "bypassPermissions",
      persistSession: true, // Wave 1 Item 1 sub-fix: was false, broke resume for escalation/dialogue
      disallowedTools,
      enabledPlugins,
      hooks: {}, // Wave 1 Item 2: explicit empty to prevent filesystem-discovered hooks
      ...(this.config.pipeline.max_budget_usd ? { maxBudgetUsd: this.config.pipeline.max_budget_usd } : {}),
      systemPrompt: this.config.systemPrompt ?? DEFAULT_EXECUTOR_SYSTEM_PROMPT,
    };

    const { query, abortController } = this.sdk.spawnSession(sessionConfig);

    // Every spawn allocates a persistSession:true SDK record; spawn count
    // is therefore a direct proxy for disk-accumulated session records.
    this.cumulativeSessionSpawns += 1;
    const threshold =
      this.config.pipeline.persistent_session_warn_threshold
        ?? PERSISTENT_SESSION_WARN_THRESHOLD_DEFAULT;
    if (this.cumulativeSessionSpawns > threshold) {
      console.warn(
        `WARN SessionManager persistent-session count ${this.cumulativeSessionSpawns} ` +
          `exceeds threshold ${threshold}. Session records accumulate on disk under ` +
          `${this.config.project.session_dir}; consider periodic cleanup.`,
      );
    }

    // Track active session
    const activeSession: ActiveSession = {
      taskId: task.id,
      query,
      abortController,
      worktreePath,
      branchName,
      startedAt: new Date().toISOString(),
    };

    // Set timeout if configured
    const timeout = timeoutMs ?? 600_000; // default 10min
    activeSession.timeoutHandle = setTimeout(() => {
      abortController.abort();
    }, timeout);

    this.activeSessions.set(task.id, activeSession);
    this.sdk.registerController(task.id, abortController);

    // Consume stream
    const result = await this.sdk.consumeStream(query, onMessage);

    // Clear timeout
    if (activeSession.timeoutHandle) {
      clearTimeout(activeSession.timeoutHandle);
    }

    // Update task with session info
    this.state.updateTask(task.id, {
      sessionId: result.sessionId,
      totalCostUsd: result.totalCostUsd,
    });

    // Check for completion signal
    const completion = this.readCompletion(worktreePath);

    // Clean up tracking
    this.activeSessions.delete(task.id);
    this.sdk.unregisterController(task.id);

    return { result, completion };
  }

  /** Read completion signal from worktree */
  readCompletion(worktreePath: string): CompletionSignal | null {
    const completionPath = join(worktreePath, ".harness", "completion.json");
    if (!existsSync(completionPath)) return null;

    try {
      const raw = JSON.parse(readFileSync(completionPath, "utf-8"));
      return validateCompletion(raw);
    } catch {
      return null;
    }
  }

  /** Abort a specific task's session */
  abortTask(taskId: string): void {
    const session = this.activeSessions.get(taskId);
    if (session) {
      if (session.timeoutHandle) clearTimeout(session.timeoutHandle);
      session.abortController.abort();
      this.activeSessions.delete(taskId);
      this.sdk.unregisterController(taskId);
    }
  }

  /** Abort all active sessions (for graceful shutdown). Sweeps tmux sessions spawned by agents (Wave 1 Item 4). */
  abortAll(): void {
    for (const [taskId, session] of this.activeSessions) {
      if (session.timeoutHandle) clearTimeout(session.timeoutHandle);
      session.abortController.abort();
      this.sdk.unregisterController(taskId);
    }
    this.activeSessions.clear();
    // Wave 1 Item 4: sweep all harness-spawned tmux sessions on shutdown
    try {
      this.tmuxOps.killSessionsByPattern("harness-");
    } catch {
      // Tmux cleanup failure is non-fatal
    }
  }

  /** Get active session for a task */
  getActiveSession(taskId: string): ActiveSession | undefined {
    return this.activeSessions.get(taskId);
  }

  /** Number of active sessions */
  get activeCount(): number {
    return this.activeSessions.size;
  }

  /** Cumulative spawn count since construction. Proxy for persistSession records on disk. */
  get persistentSessionCount(): number {
    return this.cumulativeSessionSpawns;
  }
}
