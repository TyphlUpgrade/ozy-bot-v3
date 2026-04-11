/**
 * Orchestrator — daemon main loop.
 * Watches task_dir for new task JSON files, spawns agent sessions,
 * routes completions to merge gate. Handles shutdown and crash recovery.
 */

import { readdirSync, readFileSync, unlinkSync, existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { SessionManager } from "./session/manager.js";
import { MergeGate, type MergeResult } from "./gates/merge.js";
import { StateManager, type TaskRecord } from "./lib/state.js";
import type { HarnessConfig } from "./lib/config.js";

// --- Task file schema ---

export interface TaskFile {
  id?: string;
  prompt: string;
  priority?: number;
}

// O4: Path traversal validation on task IDs (untrusted Discord/file input)
const SAFE_TASK_ID = /^[a-zA-Z0-9_-]+$/;

function sanitizeTaskId(raw: string): string | null {
  if (!SAFE_TASK_ID.test(raw)) return null;
  if (raw.length > 128) return null;
  return raw;
}

function parseTaskFile(path: string): TaskFile | null {
  try {
    const raw = JSON.parse(readFileSync(path, "utf-8"));
    if (typeof raw !== "object" || !raw) return null;
    if (typeof raw.prompt !== "string" || raw.prompt.length === 0) return null;
    return raw as TaskFile;
  } catch {
    return null;
  }
}

// --- Orchestrator ---

export interface OrchestratorDeps {
  sessionManager: SessionManager;
  mergeGate: MergeGate;
  stateManager: StateManager;
  config: HarnessConfig;
}

export type OrchestratorEvent =
  | { type: "task_picked_up"; taskId: string; prompt: string }
  | { type: "session_complete"; taskId: string; success: boolean }
  | { type: "merge_result"; taskId: string; result: MergeResult }
  | { type: "task_shelved"; taskId: string; reason: string }
  | { type: "task_failed"; taskId: string; reason: string }
  | { type: "task_done"; taskId: string }
  | { type: "poll_tick" }
  | { type: "shutdown" };

export class Orchestrator {
  private readonly sessions: SessionManager;
  private readonly mergeGate: MergeGate;
  private readonly state: StateManager;
  private readonly config: HarnessConfig;
  private running = false;
  private pollTimer?: ReturnType<typeof setTimeout>;
  private readonly eventListeners: ((event: OrchestratorEvent) => void)[] = [];

  constructor(deps: OrchestratorDeps) {
    this.sessions = deps.sessionManager;
    this.mergeGate = deps.mergeGate;
    this.state = deps.stateManager;
    this.config = deps.config;
  }

  /** Register event listener */
  on(listener: (event: OrchestratorEvent) => void): void {
    this.eventListeners.push(listener);
  }

  private emit(event: OrchestratorEvent): void {
    for (const listener of this.eventListeners) {
      listener(event);
    }
  }

  /** Start the daemon loop */
  start(): void {
    if (this.running) return;
    this.running = true;

    // Ensure task_dir exists
    const taskDir = this.resolveTaskDir();
    if (!existsSync(taskDir)) {
      mkdirSync(taskDir, { recursive: true });
    }

    // Resume any incomplete tasks from persisted state
    this.recoverFromCrash();

    // Start polling
    this.poll();
  }

  /** Stop the daemon loop gracefully */
  async shutdown(): Promise<void> {
    this.running = false;
    if (this.pollTimer) {
      clearTimeout(this.pollTimer);
      this.pollTimer = undefined;
    }

    // Abort all active sessions
    this.sessions.abortAll();
    this.emit({ type: "shutdown" });
  }

  /** Single poll iteration — scan for new tasks, check completions */
  async poll(): Promise<void> {
    if (!this.running) return;

    this.emit({ type: "poll_tick" });

    // Scan for new task files
    this.scanForTasks();

    // Schedule next poll
    if (this.running) {
      this.pollTimer = setTimeout(
        () => this.poll(),
        this.config.pipeline.poll_interval * 1000,
      );
    }
  }

  /** Scan task_dir for new .json files and ingest them */
  scanForTasks(): void {
    const taskDir = this.resolveTaskDir();
    if (!existsSync(taskDir)) return;

    let files: string[];
    try {
      files = readdirSync(taskDir).filter((f) => f.endsWith(".json"));
    } catch {
      return;
    }

    for (const file of files) {
      const filePath = join(taskDir, file);
      const taskFile = parseTaskFile(filePath);
      if (!taskFile) {
        // Invalid file — remove it
        try { unlinkSync(filePath); } catch { /* ignore */ }
        continue;
      }

      // Create task in state — O4: validate task ID against path traversal
      const rawId = taskFile.id ?? file.replace(/\.json$/, "");
      const taskId = sanitizeTaskId(rawId);
      if (!taskId) {
        // Invalid task ID — reject file
        try { unlinkSync(filePath); } catch { /* ignore */ }
        continue;
      }
      const existing = this.state.getTask(taskId);
      if (existing) {
        // Already tracked — skip
        try { unlinkSync(filePath); } catch { /* ignore */ }
        continue;
      }

      const task = this.state.createTask(taskFile.prompt, taskId);

      // Remove the file — we've ingested it
      try { unlinkSync(filePath); } catch { /* ignore */ }

      this.emit({ type: "task_picked_up", taskId: task.id, prompt: task.prompt });

      // Spawn session (fire and forget — lifecycle handled in processTask)
      this.processTask(task);
    }
  }

  /** Full task lifecycle: spawn session -> check completion -> merge or fail */
  async processTask(task: TaskRecord): Promise<void> {
    try {
      // Spawn agent session
      const { result, completion } = await this.sessions.spawnTask(task);

      this.emit({
        type: "session_complete",
        taskId: task.id,
        success: result.success,
      });

      // Session failed without completion signal
      if (!result.success || !completion || completion.status !== "success") {
        const reason = !result.success
          ? result.errors.join("; ")
          : !completion
            ? "No completion signal"
            : `Agent reported failure: ${completion.summary}`;

        this.state.transition(task.id, "failed");
        this.state.updateTask(task.id, { lastError: reason });
        this.emit({ type: "task_failed", taskId: task.id, reason });
        return;
      }

      // Route to merge gate
      this.state.transition(task.id, "merging");
      const updatedTask = this.state.getTask(task.id)!;
      const mergeResult = await this.mergeGate.enqueue(
        task.id,
        updatedTask.worktreePath!,
        updatedTask.branchName!,
      );

      this.emit({ type: "merge_result", taskId: task.id, result: mergeResult });

      switch (mergeResult.status) {
        case "merged":
          this.state.transition(task.id, "done");
          this.state.updateTask(task.id, {
            summary: completion.summary,
            filesChanged: completion.filesChanged,
          });
          this.emit({ type: "task_done", taskId: task.id });
          // Cleanup worktree
          this.sessions.cleanupWorktree(task.id);
          break;

        case "rebase_conflict": {
          const attempts = this.state.incrementRebaseAttempts(task.id);
          // Clean up worktree so retry can re-create it
          this.sessions.cleanupWorktree(task.id);
          if (attempts >= this.config.pipeline.max_retries) {
            this.state.transition(task.id, "failed");
            this.state.updateTask(task.id, {
              lastError: `Rebase conflict after ${attempts} attempts`,
            });
            this.emit({
              type: "task_failed",
              taskId: task.id,
              reason: `Rebase conflict persists (${attempts} attempts)`,
            });
          } else {
            this.state.transition(task.id, "shelved");
            this.emit({
              type: "task_shelved",
              taskId: task.id,
              reason: `Rebase conflict (attempt ${attempts}/${this.config.pipeline.max_retries})`,
            });
            // Schedule auto-retry — delay from config
            this.scheduleRetry(task.id, this.config.pipeline.retry_delay_ms);
          }
          break;
        }

        case "test_failed":
          this.state.transition(task.id, "failed");
          this.state.updateTask(task.id, {
            lastError: `Tests failed: ${mergeResult.error}`,
          });
          this.emit({
            type: "task_failed",
            taskId: task.id,
            reason: `Tests failed`,
          });
          this.sessions.cleanupWorktree(task.id);
          break;

        case "test_timeout":
          this.state.transition(task.id, "failed");
          this.state.updateTask(task.id, { lastError: "Test timeout" });
          this.emit({
            type: "task_failed",
            taskId: task.id,
            reason: "Test timeout",
          });
          this.sessions.cleanupWorktree(task.id);
          break;

        case "error":
          this.state.transition(task.id, "failed");
          this.state.updateTask(task.id, { lastError: mergeResult.error });
          this.emit({
            type: "task_failed",
            taskId: task.id,
            reason: mergeResult.error,
          });
          break;
      }
    } catch (err) {
      // Unexpected error — fail the task
      const reason = (err as Error).message;
      try {
        this.state.transition(task.id, "failed");
        this.state.updateTask(task.id, { lastError: reason });
      } catch {
        // State transition may also fail if task is in incompatible state
      }
      this.emit({ type: "task_failed", taskId: task.id, reason });
    }
  }

  /** Schedule a retry for a shelved task */
  private scheduleRetry(taskId: string, delayMs: number): void {
    if (!this.running) return;
    setTimeout(() => {
      if (!this.running) return;
      const task = this.state.getTask(taskId);
      if (!task || task.state !== "shelved") return;

      // Unshelve and re-process
      this.state.transition(taskId, "pending");
      const updated = this.state.getTask(taskId)!;
      this.processTask(updated);
    }, delayMs);
  }

  /** Recover incomplete tasks after crash/restart */
  private recoverFromCrash(): void {
    const tasks = this.state.getAllTasks();
    for (const task of tasks) {
      // Active or reviewing tasks were interrupted — clean up old worktree, re-queue
      if (task.state === "active" || task.state === "reviewing") {
        this.sessions.cleanupWorktree(task.id);
        this.state.transition(task.id, "failed");
        this.state.transition(task.id, "pending");
        this.processTask(this.state.getTask(task.id)!);
      }
      // Shelved tasks get retried (worktree already cleaned on shelve path)
      if (task.state === "shelved") {
        this.scheduleRetry(task.id, this.config.pipeline.retry_delay_ms);
      }
    }
  }

  /** Resolve task_dir to absolute path */
  private resolveTaskDir(): string {
    const dir = this.config.project.task_dir;
    if (dir.startsWith("/")) return dir;
    return join(this.config.project.root, dir);
  }

  /** Is the orchestrator running? */
  get isRunning(): boolean {
    return this.running;
  }
}
