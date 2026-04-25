/**
 * Task state machine with atomic persistence.
 * Lessons: B1 (sync mutations), B3 (shelve clock reset), B5 (resume at executor),
 * B6 (escalation tier reset), B7 (unknown key drop), O3 (atomic writes), O9 (write-only log).
 */

import { readFileSync, writeFileSync, renameSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";

// --- Task States ---

export const TASK_STATES = [
  "pending",
  "active",
  "reviewing",
  "merging",
  "done",
  "failed",
  "shelved",
  "escalation_wait",
  "paused",
  "review_arbitration", // Three-tier (Wave 1.5b) — Reviewer rejected, awaiting Architect arbitration
] as const;

export type TaskState = (typeof TASK_STATES)[number];

// Valid transitions: from -> allowed destinations
const VALID_TRANSITIONS: Record<TaskState, readonly TaskState[]> = {
  pending: ["active", "failed"],
  active: ["reviewing", "merging", "done", "failed", "shelved", "escalation_wait", "paused"],
  // Wave 1.5b: reviewing can route to review_arbitration on Reviewer reject (Wave A wires the state,
  // Wave C wires the Architect listener that consumes it).
  reviewing: ["active", "merging", "done", "failed", "escalation_wait", "review_arbitration", "shelved"],
  merging: ["done", "failed", "shelved"],
  done: [],
  failed: ["pending"], // can retry
  shelved: ["pending", "active", "failed"],
  // P1 Architect verdict path: escalation_wait → shelved lets scheduleRetry
  // unshelve and re-process the task after the Architect issues a verdict.
  escalation_wait: ["active", "failed", "shelved"],
  paused: ["active", "failed"],
  // review_arbitration exit edges mirror reviewing's non-terminal destinations:
  // back to active (retry_with_directive via shelved queue), merging (arbitration
  // override — gated by C.3), failed (plan_amendment cancels current task),
  // escalation_wait (cap reached), or shelved (P1 retry path).
  review_arbitration: ["active", "merging", "failed", "escalation_wait", "shelved"],
};

// --- Task Record ---

export interface DialogueMessage {
  role: "operator" | "agent";
  content: string;
  timestamp: string;
}

export interface ReviewResult {
  verdict: string;
  weightedRisk: number;
  findingCount: number;
}

export interface TaskRecord {
  id: string;
  state: TaskState;
  prompt: string;
  sessionId?: string;
  worktreePath?: string;
  branchName?: string;
  createdAt: string;      // ISO timestamp
  updatedAt: string;
  completedAt?: string;
  totalCostUsd: number;
  retryCount: number;
  escalationTier: number; // 1=normal, 2=complex, 3=operator
  shelvedAt?: string;     // ISO — B3: shelve time tracked separately
  rebaseAttempts: number;
  tier1EscalationCount: number; // circuit breaker counter for auto-escalation cycles
  lastError?: string;
  summary?: string;
  filesChanged?: string[];
  // Phase 2B-3 additions (dialogue + review)
  dialogueMessages?: DialogueMessage[];
  dialoguePendingConfirmation?: boolean;
  reviewResult?: ReviewResult;
  // Three-tier additions (Wave 1.5b)
  projectId?: string;              // if present, task is a project phase
  phaseId?: string;                // phase identifier within project; defaults to task.id
  arbitrationCount?: number;       // per-task Architect arbitrations on THIS phase
  reviewerRejectionCount?: number; // per-task Reviewer rejections on THIS phase
  lastDirective?: string;          // most recent Architect retry_with_directive verdict text
  recoveryAttempts?: number;       // WA-6 / Fresh-2: recoverFromCrash depth bound (max 3)
}

// Known keys for defensive deserialization (B7). Unknown keys are silently dropped on load.
const KNOWN_KEYS: ReadonlySet<string> = new Set([
  // Phase 2A
  "id", "state", "prompt", "sessionId", "worktreePath", "branchName",
  "createdAt", "updatedAt", "completedAt", "totalCostUsd", "retryCount",
  "escalationTier", "shelvedAt", "rebaseAttempts", "tier1EscalationCount",
  "lastError", "summary", "filesChanged",
  // Phase 2B-3
  "dialogueMessages", "dialoguePendingConfirmation", "reviewResult",
  // Three-tier (Wave 1.5b)
  "projectId", "phaseId", "arbitrationCount", "reviewerRejectionCount", "lastDirective",
  "recoveryAttempts",
]);

// --- Event Log (O9: write-only) ---

export interface StateEvent {
  timestamp: string;
  taskId: string;
  event: string;
  from?: TaskState;
  to?: TaskState;
  detail?: string;
}

// --- State Store ---

export interface TaskStore {
  tasks: Record<string, TaskRecord>;
  version: number;
}

export class StateManager {
  private store: TaskStore;
  private readonly statePath: string;
  private readonly logPath: string;

  constructor(statePath: string) {
    this.statePath = statePath;
    this.logPath = statePath.replace(/\.json$/, ".log.jsonl");
    this.store = this.load();
  }

  /** Load state from disk with defensive deserialization (B7) */
  private load(): TaskStore {
    if (!existsSync(this.statePath)) {
      return { tasks: {}, version: 1 };
    }
    try {
      const raw = readFileSync(this.statePath, "utf-8");
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      const tasks: Record<string, TaskRecord> = {};

      const rawTasks = (parsed.tasks ?? {}) as Record<string, Record<string, unknown>>;
      for (const [id, rawTask] of Object.entries(rawTasks)) {
        // B7: Drop unknown keys
        const cleaned: Record<string, unknown> = {};
        for (const [key, value] of Object.entries(rawTask)) {
          if (KNOWN_KEYS.has(key)) {
            cleaned[key] = value;
          }
          // Unknown keys silently dropped
        }

        // Validate state is known
        const state = cleaned.state as string;
        if (!TASK_STATES.includes(state as TaskState)) {
          cleaned.state = "failed";
        }

        tasks[id] = cleaned as unknown as TaskRecord;
      }

      return { tasks, version: (parsed.version as number) ?? 1 };
    } catch {
      // Corrupt file — start fresh
      return { tasks: {}, version: 1 };
    }
  }

  /** Atomic write: temp file + rename (O3, B1: synchronous) */
  private persist(): void {
    const dir = dirname(this.statePath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    const tmpPath = join(dir, `.state-${randomUUID()}.tmp`);
    writeFileSync(tmpPath, JSON.stringify(this.store, null, 2), "utf-8");
    renameSync(tmpPath, this.statePath);
  }

  /** Append event to write-only log (O9) */
  private logEvent(event: StateEvent): void {
    const dir = dirname(this.logPath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    // Append-only, never read by control flow
    writeFileSync(this.logPath, JSON.stringify(event) + "\n", { flag: "a" });
  }

  /** Create a new task in pending state */
  createTask(prompt: string, id?: string): TaskRecord {
    const taskId = id ?? randomUUID();
    const now = new Date().toISOString();
    const task: TaskRecord = {
      id: taskId,
      state: "pending",
      prompt,
      createdAt: now,
      updatedAt: now,
      totalCostUsd: 0,
      retryCount: 0,
      escalationTier: 1,
      rebaseAttempts: 0,
      tier1EscalationCount: 0,
    };
    this.store.tasks[taskId] = task;
    this.persist();
    this.logEvent({ timestamp: now, taskId, event: "created" });
    return task;
  }

  /** Get a task by ID */
  getTask(taskId: string): TaskRecord | undefined {
    return this.store.tasks[taskId];
  }

  /** Get all tasks */
  getAllTasks(): TaskRecord[] {
    return Object.values(this.store.tasks);
  }

  /** Get tasks by state */
  getTasksByState(state: TaskState): TaskRecord[] {
    return this.getAllTasks().filter((t) => t.state === state);
  }

  /**
   * Transition a task to a new state.
   * Validates transition is legal. Applies business logic:
   * - B3: shelve resets escalation clock
   * - B5/B6: escalation tier reset on auto-escalation
   */
  transition(taskId: string, to: TaskState): TaskRecord {
    const task = this.store.tasks[taskId];
    if (!task) {
      throw new Error(`Task not found: ${taskId}`);
    }

    const from = task.state;
    const allowed = VALID_TRANSITIONS[from];
    if (!allowed.includes(to)) {
      throw new Error(`Invalid transition: ${from} -> ${to} for task ${taskId}`);
    }

    const now = new Date().toISOString();
    task.state = to;
    task.updatedAt = now;

    // B3: Shelve resets escalation clock
    if (to === "shelved") {
      task.shelvedAt = now;
    }

    // Unshelve: clear shelvedAt
    if (from === "shelved" && (to === "pending" || to === "active")) {
      task.shelvedAt = undefined;
    }

    // Terminal states
    if (to === "done" || to === "failed") {
      task.completedAt = now;
    }

    this.persist();
    this.logEvent({ timestamp: now, taskId, event: "transition", from, to });
    return task;
  }

  /**
   * Escalate task tier. B5: resume at executor (not reviewer). B6: reset retry count.
   */
  escalate(taskId: string, newTier: number): TaskRecord {
    const task = this.store.tasks[taskId];
    if (!task) {
      throw new Error(`Task not found: ${taskId}`);
    }
    if (newTier <= task.escalationTier) {
      throw new Error(`Cannot de-escalate: ${task.escalationTier} -> ${newTier}`);
    }

    const now = new Date().toISOString();
    const fromTier = task.escalationTier;
    task.escalationTier = newTier;
    task.retryCount = 0; // B6: reset retry count on escalation
    task.updatedAt = now;

    this.persist();
    this.logEvent({
      timestamp: now,
      taskId,
      event: "escalated",
      detail: `tier ${fromTier} -> ${newTier}`,
    });
    return task;
  }

  /** Increment retry count, return updated count */
  incrementRetry(taskId: string): number {
    const task = this.store.tasks[taskId];
    if (!task) throw new Error(`Task not found: ${taskId}`);
    task.retryCount += 1;
    task.updatedAt = new Date().toISOString();
    this.persist();
    return task.retryCount;
  }

  /** Increment rebase attempts, return updated count */
  incrementRebaseAttempts(taskId: string): number {
    const task = this.store.tasks[taskId];
    if (!task) throw new Error(`Task not found: ${taskId}`);
    task.rebaseAttempts += 1;
    task.updatedAt = new Date().toISOString();
    this.persist();
    return task.rebaseAttempts;
  }

  /** Update task fields (partial update, then persist) */
  updateTask(taskId: string, updates: Partial<Omit<TaskRecord, "id" | "state">>): TaskRecord {
    const task = this.store.tasks[taskId];
    if (!task) throw new Error(`Task not found: ${taskId}`);
    Object.assign(task, updates, { updatedAt: new Date().toISOString() });
    this.persist();
    return task;
  }

  /** WA-6 Fresh-2: record crash-recovery attempt count. Throws if task missing. */
  setRecoveryAttempts(taskId: string, n: number): void {
    const task = this.store.tasks[taskId];
    if (!task) throw new Error(`Task not found: ${taskId}`);
    task.recoveryAttempts = n;
    task.updatedAt = new Date().toISOString();
    this.persist();
  }

  /** Reload from disk (for crash recovery) */
  reload(): void {
    this.store = this.load();
  }
}
