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
import type { HarnessConfig } from "../lib/config.js";

// --- Completion signal schema ---

export interface CompletionSignal {
  status: "success" | "failure";
  commitSha: string;
  summary: string;
  filesChanged: string[];
}

function validateCompletion(raw: unknown): CompletionSignal | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  if (typeof obj.status !== "string" || !["success", "failure"].includes(obj.status)) return null;
  if (typeof obj.commitSha !== "string" || obj.commitSha.length === 0) return null;
  if (typeof obj.summary !== "string") return null;
  if (!Array.isArray(obj.filesChanged)) return null;
  return obj as unknown as CompletionSignal;
}

// --- Git helpers (injectable for testing) ---

export interface GitOps {
  createWorktree(basePath: string, branchName: string, worktreePath: string): void;
  removeWorktree(worktreePath: string): void;
  branchExists(branchName: string): boolean;
  deleteBranch(branchName: string): void;
}

export const realGitOps: GitOps = {
  createWorktree(basePath: string, branchName: string, worktreePath: string): void {
    mkdirSync(worktreePath, { recursive: true });
    execSync(`git worktree add -b ${branchName} ${worktreePath}`, {
      cwd: basePath,
      stdio: "pipe",
    });
  },

  removeWorktree(worktreePath: string): void {
    execSync(`git worktree remove --force ${worktreePath}`, { stdio: "pipe" });
  },

  branchExists(branchName: string): boolean {
    try {
      execSync(`git rev-parse --verify ${branchName}`, { stdio: "pipe" });
      return true;
    } catch {
      return false;
    }
  },

  deleteBranch(branchName: string): void {
    execSync(`git branch -D ${branchName}`, { stdio: "pipe" });
  },
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
  private readonly activeSessions: Map<string, ActiveSession> = new Map();

  constructor(
    sdk: SDKClient,
    state: StateManager,
    config: HarnessConfig,
    gitOps?: GitOps,
  ) {
    this.sdk = sdk;
    this.state = state;
    this.config = config;
    this.gitOps = gitOps ?? realGitOps;
  }

  /** Create a worktree for a task */
  createWorktree(taskId: string): { worktreePath: string; branchName: string } {
    const branchName = `harness/task-${taskId}`;
    const worktreePath = join(this.config.project.worktree_base, `task-${taskId}`);
    this.gitOps.createWorktree(this.config.project.root, branchName, worktreePath);
    return { worktreePath, branchName };
  }

  /** Clean up worktree and branch for a task */
  cleanupWorktree(taskId: string): void {
    const session = this.activeSessions.get(taskId);
    const worktreePath = session?.worktreePath
      ?? join(this.config.project.worktree_base, `task-${taskId}`);
    const branchName = session?.branchName ?? `harness/task-${taskId}`;

    try {
      this.gitOps.removeWorktree(worktreePath);
    } catch {
      // Already removed
    }
    try {
      if (this.gitOps.branchExists(branchName)) {
        this.gitOps.deleteBranch(branchName);
      }
    } catch {
      // Already deleted
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

    // Spawn SDK session
    const sessionConfig: SessionConfig = {
      prompt: task.prompt,
      cwd: worktreePath,
      settingSources: ["project"],
      permissionMode: "bypassPermissions",
      persistSession: false,
    };

    const { query, abortController } = this.sdk.spawnSession(sessionConfig);

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

  /** Abort all active sessions (for graceful shutdown) */
  abortAll(): void {
    for (const [taskId, session] of this.activeSessions) {
      if (session.timeoutHandle) clearTimeout(session.timeoutHandle);
      session.abortController.abort();
      this.sdk.unregisterController(taskId);
    }
    this.activeSessions.clear();
  }

  /** Get active session for a task */
  getActiveSession(taskId: string): ActiveSession | undefined {
    return this.activeSessions.get(taskId);
  }

  /** Number of active sessions */
  get activeCount(): number {
    return this.activeSessions.size;
  }
}
