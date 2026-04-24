/**
 * Orchestrator — daemon main loop.
 * Watches task_dir for new task JSON files, spawns agent sessions,
 * routes completions to merge gate. Handles shutdown and crash recovery.
 */

import { readdirSync, readFileSync, unlinkSync, existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { SessionManager, type CompletionSignal } from "./session/manager.js";
import { MergeGate, type MergeResult } from "./gates/merge.js";
import type { SessionResult } from "./session/sdk.js";
import { StateManager, type TaskRecord } from "./lib/state.js";
import type { HarnessConfig } from "./lib/config.js";
import { readEscalation, type EscalationSignal } from "./lib/escalation.js";
import { readCheckpoints, type CheckpointSignal } from "./lib/checkpoint.js";
import { evaluateResponseLevel, type ResponseLevel } from "./lib/response.js";
import { sanitizeTaskId } from "./lib/text.js";
import type { ReviewGate, ReviewResult } from "./gates/review.js";
import type { ArchitectManager } from "./session/architect.js";
import type { ProjectStore } from "./lib/project.js";

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

// O4: Path-traversal-safe task IDs live in src/lib/text.ts (`sanitizeTaskId`).
// Imported above; no local copy to keep one source of truth.

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
  /** Wave A: optional ReviewGate. When present, project tasks always go through review;
   *  standalone tasks go through review only when `shouldReview` returns true. */
  reviewGate?: ReviewGate;
  /** Wave B: optional ArchitectManager. Required for project declaration + crash recovery. */
  architectManager?: ArchitectManager;
  /** Wave B: optional ProjectStore. Required alongside architectManager for declareProject. */
  projectStore?: ProjectStore;
}

export type ArbitrationVerdict = "retry_with_directive" | "plan_amendment" | "escalate_operator";
export type ArchitectRespawnReason = "compaction" | "crash_recovery";
export type ArbitrationCause = "escalation" | "review_disagreement";

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
  | { type: "budget_exhausted"; taskId: string; totalCostUsd: number }
  // Wave 2 three-tier events — all project-related events carry projectId (Critic item 10).
  | { type: "project_declared"; projectId: string; name: string }
  | { type: "project_decomposed"; projectId: string; phaseCount: number }
  | { type: "project_completed"; projectId: string; phaseCount: number; totalCostUsd: number }
  | { type: "project_failed"; projectId: string; reason: string }
  | { type: "project_aborted"; projectId: string; operatorId: string }
  | { type: "architect_spawned"; projectId: string; sessionId: string }
  | { type: "architect_respawned"; projectId: string; sessionId: string; reason: ArchitectRespawnReason }
  | { type: "architect_arbitration_fired"; taskId: string; projectId: string; cause: ArbitrationCause }
  | { type: "arbitration_verdict"; taskId: string; projectId: string; verdict: ArbitrationVerdict; rationale: string }
  | { type: "review_arbitration_entered"; taskId: string; projectId: string; reviewerRejectionCount: number }
  | { type: "review_mandatory"; taskId: string; projectId: string }
  | { type: "budget_ceiling_reached"; projectId: string; currentCostUsd: number; ceilingUsd: number }
  | { type: "compaction_fired"; projectId: string; generation: number };

export class Orchestrator {
  private readonly sessions: SessionManager;
  private readonly mergeGate: MergeGate;
  private readonly state: StateManager;
  private readonly config: HarnessConfig;
  private readonly reviewGate?: ReviewGate;
  private readonly architectManager?: ArchitectManager;
  private readonly projectStore?: ProjectStore;
  private running = false;
  private pollTimer?: ReturnType<typeof setTimeout>;
  private readonly eventListeners: ((event: OrchestratorEvent) => void)[] = [];
  /** Tracks task ids that have already emitted the interim Wave A→C "listener not wired" warning,
   *  so the log fires exactly once per task per Section C.6. */
  private readonly arbitrationWarnedTasks: Set<string> = new Set();

  constructor(deps: OrchestratorDeps) {
    this.sessions = deps.sessionManager;
    this.mergeGate = deps.mergeGate;
    this.state = deps.stateManager;
    this.config = deps.config;
    this.reviewGate = deps.reviewGate;
    this.architectManager = deps.architectManager;
    this.projectStore = deps.projectStore;
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
      if (taskFile.projectId) {
        this.state.updateTask(task.id, {
          projectId: taskFile.projectId,
          phaseId: taskFile.phaseId ?? task.id,
        });
      }

      // Remove the file — we've ingested it
      try { unlinkSync(filePath); } catch { /* ignore */ }

      this.emit({ type: "task_picked_up", taskId: task.id, prompt: task.prompt });

      // Spawn session (fire and forget — lifecycle handled in processTask)
      this.processTask(task);
    }
  }

  /**
   * Routing precedence (evaluated in order):
   *
   *   1. task.projectId !== undefined
   *        → routeByProject (Wave B); mode/response_level subordinate.
   *        → All project behavior (mandatory review, Architect arbitration,
   *          project-channel dialogue) gates on this single boolean.
   *
   *   2. TaskFile.mode === "dialogue" && task.projectId === undefined
   *        → pre-pipeline dialogue (Wave 6-split); DialogueSession + proposal.json.
   *
   *   3. Otherwise
   *        → standard pipeline: Executor → shouldReview(task) → (review?) → merge.
   *        → responseLevel routes merge / review / escalation as per Phase 2A.
   *
   * Conflict: projectId + mode:"dialogue" combined → REJECTED at task ingest
   * (throws TaskFileValidationError). Project dialogue happens through the
   * Architect, not through the standalone dialogue session. See Section C.2.
   *
   * Full task lifecycle: spawn session -> check completion -> merge or fail.
   */
  async processTask(task: TaskRecord): Promise<void> {
    try {
      // Routing precedence rule 1: project-scoped tasks go through project dispatch.
      // Wave 1.5a stub — returns false so standard pipeline handles today's tasks;
      // Wave B wires cost-ceiling precheck, phase attachment, and Architect routing.
      if (await this.routeByProject(task)) return;

      const { result, completion } = await this.sessions.spawnTask(task);

      this.emit({
        type: "session_complete",
        taskId: task.id,
        success: result.success,
      });

      this.emitInformationalEvents(task, completion);

      // Check for escalation signal — takes priority over completion.
      const worktreePath = this.state.getTask(task.id)?.worktreePath;
      if (worktreePath) {
        const escalation = readEscalation(worktreePath);
        if (escalation) {
          this.state.transition(task.id, "escalation_wait");
          this.emit({ type: "escalation_needed", taskId: task.id, escalation });
          return;
        }
      }

      if (!result.success || !completion || completion.status !== "success") {
        await this.handleSessionFailure(task, result, completion);
        return;
      }

      await this.routeByResponseLevel(task, completion, result);
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

  /**
   * Project-aware dispatch entry point. Wave 1.5a stub.
   *
   * Returns true iff the task was fully handled by the project path (caller
   * should short-circuit). Returns false to fall through to the standard
   * Executor pipeline.
   *
   * Today this is a no-op pass-through: even project-scoped tasks continue
   * through the standard pipeline because Wave B has not wired the project
   * cost-ceiling precheck, phase attachment, or Architect routing yet.
   *
   * Wave B will flesh this out to:
   *   - consult projectStore for the current project + phase
   *   - enforce budgetCeilingUsd via Section C.4 precheck
   *   - call projectStore.attachTask(projectId, phaseId, task.id)
   *   - on Architect-owned work, spawn the Architect session instead
   */
  private async routeByProject(_task: TaskRecord): Promise<boolean> {
    // Wave B hook — kept intentionally minimal so Wave 1.5a lands with zero
    // behavior change. Any project-aware decision belongs here, not inline.
    return false;
  }

  /** Emit checkpoint + completion-compliance events (informational, never blocks flow). */
  private emitInformationalEvents(task: TaskRecord, completion: CompletionSignal | null): void {
    const worktreePath = this.state.getTask(task.id)?.worktreePath;
    if (worktreePath) {
      const checkpoints = readCheckpoints(worktreePath);
      if (checkpoints.length > 0) {
        this.emit({ type: "checkpoint_detected", taskId: task.id, checkpoints });
      }
    }
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
  }

  /** Handle session failure: budget exhaustion > retry > auto-escalation > circuit breaker. */
  private async handleSessionFailure(
    task: TaskRecord,
    result: SessionResult,
    completion: CompletionSignal | null,
  ): Promise<void> {
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
  }

  /**
   * Decide whether the review gate should fire for a standalone task.
   *
   * Project tasks (`task.projectId !== undefined`) ALWAYS fire review — that
   * decision is made upstream in `routeByResponseLevel` and this method is
   * not consulted for them.
   *
   * Wave A returns `true` when response level ≥ 2 (reviewed / dialogue).
   * TaskFile.mode === "reviewed" is a future extension (Wave A.1) once the
   * ingest path plumbs it through to TaskRecord.
   */
  private shouldReview(
    _task: TaskRecord,
    _completion: CompletionSignal,
    _result: SessionResult,
    responseLevel: ResponseLevel,
  ): boolean {
    return responseLevel >= 2;
  }

  /** Route task based on response level: review (when gate present + triggered) or direct merge. */
  private async routeByResponseLevel(
    task: TaskRecord,
    completion: CompletionSignal,
    result: SessionResult,
  ): Promise<void> {
    const responseResult = evaluateResponseLevel(completion, result);
    this.emit({
      type: "response_level",
      taskId: task.id,
      level: responseResult.level,
      name: responseResult.name,
      reasons: responseResult.reasons,
    });

    const projectTask = task.projectId !== undefined;
    const standaloneTriggered = this.shouldReview(task, completion, result, responseResult.level);
    if (this.reviewGate && (projectTask || standaloneTriggered)) {
      await this.routeReview(task, completion);
      return;
    }

    await this.routeDirectMerge(task, completion);
  }

  /** Direct merge path — no review gate. Extracted so routeReview can share it on approve. */
  private async routeDirectMerge(task: TaskRecord, completion: CompletionSignal): Promise<void> {
    this.state.transition(task.id, "merging");
    const updatedTask = this.state.getTask(task.id)!;
    const mergeResult = await this.mergeGate.enqueue(
      task.id,
      updatedTask.worktreePath!,
      updatedTask.branchName!,
    );

    this.emit({ type: "merge_result", taskId: task.id, result: mergeResult });
    this.handleMergeResult(task, mergeResult, completion);
  }

  /** Route through the Reviewer gate. On approve → merge; on reject → retry or review_arbitration. */
  private async routeReview(task: TaskRecord, completion: CompletionSignal): Promise<void> {
    if (!this.reviewGate) return; // defensive — routeByResponseLevel already gated

    this.state.transition(task.id, "reviewing");
    if (task.projectId) {
      this.emit({ type: "review_mandatory", taskId: task.id, projectId: task.projectId });
    }

    const worktreePath = this.state.getTask(task.id)!.worktreePath!;
    const review = await this.reviewGate.runReview(task, worktreePath, completion);

    this.state.updateTask(task.id, {
      reviewResult: {
        verdict: review.verdict,
        weightedRisk: review.riskScore.weighted,
        findingCount: review.findings.length,
      },
    });

    if (review.verdict === "approve") {
      this.state.transition(task.id, "merging");
      const updatedTask = this.state.getTask(task.id)!;
      const mergeResult = await this.mergeGate.enqueue(
        task.id,
        updatedTask.worktreePath!,
        updatedTask.branchName!,
      );

      this.emit({ type: "merge_result", taskId: task.id, result: mergeResult });
      this.handleMergeResult(task, mergeResult, completion);
      return;
    }

    await this.handleReviewReject(task, review);
  }

  /** Handle reject / request_changes verdict. Standalone → failed; project → retry or arbitration. */
  private async handleReviewReject(task: TaskRecord, review: ReviewResult): Promise<void> {
    if (!task.projectId) {
      // Standalone reject → permanent failure for this wave.
      this.state.transition(task.id, "failed");
      this.state.updateTask(task.id, {
        lastError: `Review ${review.verdict}: ${review.summary}`,
      });
      this.emit({
        type: "task_failed",
        taskId: task.id,
        reason: `Review ${review.verdict}`,
      });
      this.sessions.cleanupWorktree(task.id);
      return;
    }

    // Project path: increment rejection counter + check arbitration threshold.
    const threshold = this.reviewGate!.arbitrationThreshold;
    const current = this.state.getTask(task.id)!;
    const newCount = (current.reviewerRejectionCount ?? 0) + 1;
    this.state.updateTask(task.id, { reviewerRejectionCount: newCount });

    if (newCount >= threshold) {
      this.state.transition(task.id, "review_arbitration");
      this.emit({
        type: "review_arbitration_entered",
        taskId: task.id,
        projectId: task.projectId,
        reviewerRejectionCount: newCount,
      });
      if (!this.arbitrationWarnedTasks.has(task.id)) {
        // Section C.6 interim warning — removed in Wave C when the Architect listener wires in.
        console.warn(
          `WARN task=${task.id} in review_arbitration but architect listener not yet wired (Wave A/B/C window); merge blocked`,
        );
        this.arbitrationWarnedTasks.add(task.id);
      }
      // Merge blocked — no further action until Wave C.
      return;
    }

    // Below threshold — retry: transition reviewing → active.
    // Wave B wires the re-spawn-with-directive flow; Wave A only moves state.
    this.state.transition(task.id, "active");
    this.emit({
      type: "retry_scheduled",
      taskId: task.id,
      attempt: newCount,
      maxRetries: threshold,
    });
  }

  /** Branch on merge gate result: merged / rebase_conflict / test_failed / test_timeout / error. */
  private handleMergeResult(
    task: TaskRecord,
    mergeResult: MergeResult,
    completion: CompletionSignal,
  ): void {
    switch (mergeResult.status) {
      case "merged":
        this.state.transition(task.id, "done");
        this.state.updateTask(task.id, {
          summary: completion.summary,
          filesChanged: completion.filesChanged,
        });
        this.emit({ type: "task_done", taskId: task.id });
        this.sessions.cleanupWorktree(task.id);
        if (task.projectId && task.phaseId && this.projectStore) {
          this.projectStore.markPhaseDone(task.projectId, task.phaseId);
          if (!this.projectStore.hasActivePhases(task.projectId)) {
            const project = this.projectStore.getProject(task.projectId);
            if (project) {
              this.projectStore.completeProject(task.projectId);
              this.emit({
                type: "project_completed",
                projectId: task.projectId,
                phaseCount: project.phases.length,
                totalCostUsd: project.totalCostUsd,
              });
            }
          }
        }
        break;

      case "rebase_conflict": {
        const attempts = this.state.incrementRebaseAttempts(task.id);
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
          this.scheduleRetry(task.id, this.config.pipeline.retry_delay_ms);
        }
        break;
      }

      case "test_failed":
        this.state.transition(task.id, "failed");
        this.state.updateTask(task.id, {
          lastError: `Tests failed: ${mergeResult.error}`,
        });
        this.emit({ type: "task_failed", taskId: task.id, reason: `Tests failed` });
        this.sessions.cleanupWorktree(task.id);
        break;

      case "test_timeout":
        this.state.transition(task.id, "failed");
        this.state.updateTask(task.id, { lastError: "Test timeout" });
        this.emit({ type: "task_failed", taskId: task.id, reason: "Test timeout" });
        this.sessions.cleanupWorktree(task.id);
        break;

      case "error":
        this.state.transition(task.id, "failed");
        this.state.updateTask(task.id, { lastError: mergeResult.error });
        this.emit({ type: "task_failed", taskId: task.id, reason: mergeResult.error });
        this.sessions.cleanupWorktree(task.id);
        break;
    }
  }

  /** Schedule a retry for a shelved task */
  /**
   * Wave B: declare a new project. Creates the project record, spawns the
   * Architect session, and fires decomposition. On any failure emits
   * `project_failed` and marks the project as failed. Caller gets a structured
   * result rather than an exception.
   */
  async declareProject(
    name: string,
    description: string,
    nonGoals: string[],
  ): Promise<{ projectId: string; sessionId: string } | { error: string }> {
    if (!this.architectManager || !this.projectStore) {
      return { error: "ArchitectManager/ProjectStore not configured" };
    }

    const project = this.projectStore.createProject(name, description, nonGoals);
    this.emit({ type: "project_declared", projectId: project.id, name: project.name });

    const spawn = await this.architectManager.spawn(project.id, name, description, nonGoals);
    if (spawn.status !== "success" || !spawn.sessionId) {
      this.projectStore.failProject(project.id, spawn.error ?? "architect spawn failed");
      this.emit({ type: "project_failed", projectId: project.id, reason: spawn.error ?? "architect spawn failed" });
      return { error: spawn.error ?? "architect spawn failed" };
    }
    this.emit({ type: "architect_spawned", projectId: project.id, sessionId: spawn.sessionId });

    const decomp = await this.architectManager.decompose(project.id);
    if (decomp.status !== "success" || !decomp.phases) {
      this.projectStore.failProject(project.id, decomp.error ?? "decompose failed");
      this.emit({ type: "project_failed", projectId: project.id, reason: decomp.error ?? "decompose failed" });
      return { error: decomp.error ?? "decompose failed" };
    }
    this.emit({ type: "project_decomposed", projectId: project.id, phaseCount: decomp.phases.length });

    return { projectId: project.id, sessionId: spawn.sessionId };
  }

  /**
   * Wave B: check each executing project's Architect liveness. If dead,
   * respawn with `crash_recovery` reason + emit `architect_respawned`.
   * Orchestrator calls this on every poll tick.
   */
  async checkArchitectHealth(): Promise<void> {
    if (!this.architectManager || !this.projectStore) return;
    for (const project of this.projectStore.getAllProjects()) {
      if (project.state !== "decomposing" && project.state !== "executing") continue;
      if (this.architectManager.isAlive(project.id)) continue;
      const respawn = await this.architectManager.respawn(project.id, "crash_recovery");
      if (respawn.status === "success" && respawn.sessionId) {
        this.emit({
          type: "architect_respawned",
          projectId: project.id,
          sessionId: respawn.sessionId,
          reason: "crash_recovery",
        });
      }
    }
  }

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
      // Section C.6: review_arbitration persists across restarts. The Architect
      // listener (Wave C) consumes this state; preserve it + worktree so the
      // listener has the diff to arbitrate over. Re-emit the interim warning
      // so post-restart ops see the stuck task.
      if (task.state === "review_arbitration") {
        console.warn(
          `WARN task=${task.id} in review_arbitration but architect listener not yet wired (Wave A/B/C window); merge blocked`,
        );
        this.arbitrationWarnedTasks.add(task.id);
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
