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
import { readEscalation, type EscalationSignal } from "./lib/escalation.js";
import { readCheckpoints, type CheckpointSignal } from "./lib/checkpoint.js";
import { evaluateResponseLevel, type ResponseLevel } from "./lib/response.js";

// --- Task file schema ---

export type TaskFileMode = "dialogue" | "reviewed";

export interface TaskFile {
  id?: string;
  prompt: string;
  priority?: number;
  mode?: TaskFileMode;    // Phase 2B-3: "dialogue" (pre-pipeline) or "reviewed" (force review gate)
  projectId?: string;     // Three-tier (Wave 1.5b): if present, task is a project phase
  phaseId?: string;       // Three-tier (Wave 1.5b): phase identifier within project; defaults to task.id
}

/** Thrown by parseTaskFile on business-rule validation (e.g. projectId+mode:dialogue). */
export class TaskFileValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TaskFileValidationError";
  }
}

// O4: Path traversal validation on task IDs (untrusted Discord/file input)
const SAFE_TASK_ID = /^[a-zA-Z0-9_-]+$/;

function sanitizeTaskId(raw: string): string | null {
  if (!SAFE_TASK_ID.test(raw)) return null;
  if (raw.length > 128) return null;
  return raw;
}

/**
 * Parse a task file from disk.
 *
 * Returns `null` for malformed JSON or structural failure (missing prompt,
 * wrong types on required fields) — these are "just ignore the file" cases.
 *
 * Throws `TaskFileValidationError` for business-rule violations that the
 * operator needs to see. Currently the only business rule is the Section C.2
 * mutual-exclusion: `projectId` and `mode: "dialogue"` cannot coexist —
 * project-scoped dialogue happens through the Architect, not the standalone
 * dialogue session (Wave 6-split).
 */
function parseTaskFile(path: string): TaskFile | null {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, "utf-8"));
  } catch {
    return null;
  }
  if (typeof raw !== "object" || !raw) return null;
  const obj = raw as Record<string, unknown>;
  if (typeof obj.prompt !== "string" || obj.prompt.length === 0) return null;

  // Optional field type checks — silently drop malformed optional fields (B7 pattern)
  const result: TaskFile = { prompt: obj.prompt };
  if (typeof obj.id === "string") result.id = obj.id;
  if (typeof obj.priority === "number") result.priority = obj.priority;
  if (obj.mode === "dialogue" || obj.mode === "reviewed") result.mode = obj.mode;
  if (typeof obj.projectId === "string") result.projectId = obj.projectId;
  if (typeof obj.phaseId === "string") result.phaseId = obj.phaseId;

  // Section C.2 routing conflict: projectId + mode:"dialogue" is rejected at ingest.
  if (result.projectId && result.mode === "dialogue") {
    throw new TaskFileValidationError(
      "projectId and mode:dialogue are mutually exclusive (project dialogue routes through the Architect)",
    );
  }

  return result;
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
  | { type: "shutdown" }
  // Phase 2A events
  | { type: "escalation_needed"; taskId: string; escalation: EscalationSignal }
  | { type: "checkpoint_detected"; taskId: string; checkpoints: CheckpointSignal[] }
  | { type: "response_level"; taskId: string; level: ResponseLevel; name: string; reasons: string[] }
  | { type: "completion_compliance"; taskId: string; hasConfidence: boolean; hasUnderstanding: boolean; hasAssumptions: boolean; hasNonGoals: boolean; complianceScore: number }
  | { type: "retry_scheduled"; taskId: string; attempt: number; maxRetries: number }
  | { type: "budget_exhausted"; taskId: string; totalCostUsd: number };

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
      let taskFile: TaskFile | null;
      try {
        taskFile = parseTaskFile(filePath);
      } catch (err) {
        if (err instanceof TaskFileValidationError) {
          // Business-rule rejection — log for operator visibility, drop the file.
          console.warn(`[orchestrator] task file rejected at ingest (${file}): ${err.message}`);
        }
        try { unlinkSync(filePath); } catch { /* ignore */ }
        continue;
      }
      if (!taskFile) {
        // Malformed JSON / structural failure — remove silently.
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

      // Check for checkpoints (informational — always emit if present)
      const worktreePath = this.state.getTask(task.id)?.worktreePath;
      if (worktreePath) {
        const checkpoints = readCheckpoints(worktreePath);
        if (checkpoints.length > 0) {
          this.emit({ type: "checkpoint_detected", taskId: task.id, checkpoints });
        }
      }

      // Completion compliance event (informational)
      if (completion) {
        this.emit({
          type: "completion_compliance",
          taskId: task.id,
          hasConfidence: completion.confidence !== undefined,
          hasUnderstanding: completion.understanding !== undefined,
          hasAssumptions: completion.assumptions !== undefined,
          hasNonGoals: completion.nonGoals !== undefined,
          complianceScore:
            (completion.confidence !== undefined ? 1 : 0) +
            (completion.understanding !== undefined ? 1 : 0) +
            (completion.assumptions !== undefined ? 1 : 0) +
            (completion.nonGoals !== undefined ? 1 : 0),
        });
      }

      // Check for escalation signal — takes priority over completion
      if (worktreePath) {
        const escalation = readEscalation(worktreePath);
        if (escalation) {
          this.state.transition(task.id, "escalation_wait");
          this.emit({ type: "escalation_needed", taskId: task.id, escalation });
          return;
        }
      }

      // Session failed — route through retry/escalation logic
      if (!result.success || !completion || completion.status !== "success") {
        const reason = !result.success
          ? result.errors.join("; ")
          : !completion
            ? "No completion signal"
            : `Agent reported failure: ${completion.summary}`;

        // Clean up worktree before any retry/escalation decision
        this.sessions.cleanupWorktree(task.id);

        // Budget exhaustion — permanent failure, never retry (would burn more money)
        if (result.terminalReason === "error_max_budget_usd") {
          this.state.transition(task.id, "failed");
          this.state.updateTask(task.id, { lastError: `Budget exhausted ($${result.totalCostUsd.toFixed(2)})` });
          this.emit({ type: "budget_exhausted", taskId: task.id, totalCostUsd: result.totalCostUsd });
          this.emit({ type: "task_failed", taskId: task.id, reason: "Budget exhausted" });
          return;
        }

        const retryCount = this.state.incrementRetry(task.id);
        const maxSessionRetries = this.config.pipeline.max_session_retries ?? 3;

        if (retryCount < maxSessionRetries) {
          // Retry: active -> failed -> pending -> processTask
          this.state.transition(task.id, "failed");
          this.state.updateTask(task.id, { lastError: reason });
          this.state.transition(task.id, "pending");
          this.emit({ type: "retry_scheduled", taskId: task.id, attempt: retryCount + 1, maxRetries: maxSessionRetries });
          const updated = this.state.getTask(task.id)!;
          await this.processTask(updated);
          return;
        }

        // Max retries exhausted — escalate or fail
        const autoEscalate = this.config.pipeline.auto_escalate_on_max_retries ?? true;
        const maxEscalations = this.config.pipeline.max_tier1_escalations ?? 2;
        const current = this.state.getTask(task.id)!;

        if (autoEscalate && current.tier1EscalationCount < maxEscalations) {
          // Auto-escalate: active -> escalation_wait
          this.state.transition(task.id, "escalation_wait");
          this.state.updateTask(task.id, {
            lastError: reason,
            tier1EscalationCount: current.tier1EscalationCount + 1,
          });
          this.emit({
            type: "escalation_needed",
            taskId: task.id,
            escalation: { type: "persistent_failure", question: `Task failed ${maxSessionRetries} times: ${reason}` },
          });
          return;
        }

        // Circuit breaker: permanent failure
        this.state.transition(task.id, "failed");
        this.state.updateTask(task.id, {
          lastError: autoEscalate
            ? `Circuit breaker: exhausted ${maxSessionRetries} retries × ${maxEscalations} escalation cycles`
            : reason,
        });
        this.emit({ type: "task_failed", taskId: task.id, reason });
        return;
      }

      // Graduated response routing (informational in Phase 2A — all levels proceed to merge)
      const responseResult = evaluateResponseLevel(completion, result);
      this.emit({
        type: "response_level",
        taskId: task.id,
        level: responseResult.level,
        name: responseResult.name,
        reasons: responseResult.reasons,
      });

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
          this.sessions.cleanupWorktree(task.id);
          break;
      }
    } catch (err) {
      // Unexpected error — fail the task and clean up worktree
      const reason = (err as Error).message;
      try {
        this.sessions.cleanupWorktree(task.id);
      } catch {
        // Cleanup may fail if worktree was never created
      }
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
      // Failed tasks with orphaned worktrees — clean up (safety net)
      if (task.state === "failed" && task.worktreePath) {
        this.sessions.cleanupWorktree(task.id);
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
