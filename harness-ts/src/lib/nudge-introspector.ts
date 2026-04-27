/**
 * Wave E-δ — NudgeIntrospector.
 *
 * Periodic project-state introspector. On each `tick()`, snapshots all
 * projects + tasks once, derives a `status` (stagnant | progressing | blocked)
 * and a `sourceAgent` (architect | reviewer | executor | orchestrator) per
 * project, and emits a `nudge_check` OrchestratorEvent through the injected
 * `emit` callback.
 *
 * Design rules (per E-δ plan §N1-N4):
 *   - I-1: lib-only — never imports from `src/discord/*` or spawns sessions.
 *   - I-3: emits a single additive OrchestratorEvent variant.
 *   - Snapshot atomicity: each `tick()` calls `getAllProjects()` /
 *     `getAllTasks()` once and iterates the snapshot only.
 *   - Stall suppression (§N3.5): if a session_stalled fired for a project's
 *     task within `intervalMs * stallSuppressionMultiplier`, the next tick
 *     skips that project (suppression window naturally ages out).
 *   - Status precedence: blocked > progressing > stagnant (single rule wins).
 *
 * Commit 1 ships the class and pure helpers. Commit 2a wires the consumer in
 * the orchestrator (no production code instantiates this yet).
 */

import type { ProjectRecord } from "./project.js";
import type { ProjectStore } from "./project.js";
import type { StateManager, TaskRecord } from "./state.js";
import type { OrchestratorEvent } from "../orchestrator.js";

// --- Public types ---

export type SourceAgent =
  | "architect"
  | "reviewer"
  | "executor"
  | "orchestrator";

export type NudgeStatus = "stagnant" | "progressing" | "blocked";

export interface NudgeIntrospectorOpts {
  /** State source — strict subset; introspector reads, never writes. */
  state: Pick<StateManager, "getTask" | "getAllTasks">;
  /** Project source — strict subset; introspector reads, never writes. */
  projectStore: Pick<ProjectStore, "getAllProjects" | "getProject">;
  /** Emitter for nudge_check events. Wired to orchestrator.emit in commit 2. */
  emit: (event: OrchestratorEvent) => void;
  /** Periodic timer interval in ms. Default 600_000 (10 min). */
  intervalMs?: number;
  /** Stall suppression window — multiplier for intervalMs. Default 2 (= 20 min). */
  stallSuppressionMultiplier?: number;
  /** Wall-clock now() — injectable for tests. Defaults to Date.now. */
  now?: () => number;
}

// --- Defaults ---

const DEFAULT_INTERVAL_MS = 600_000;
const DEFAULT_STALL_SUPPRESSION_MULTIPLIER = 2;

// --- Pure helpers (exported for testing) ---

/**
 * Derive the source agent for a project's nudge per §N2.
 *
 * Project-state pre-filter applies first — when the project itself is in
 * `decomposing` or `arbitrating`, the architect is the active speaker. Then
 * we pick the most-recently-attached active phase (highest taskId
 * lexicographically — `ProjectPhase` lacks a `startedAt` field per
 * `src/lib/project.ts:26-33`) and map its TaskState to the owning role.
 *
 * Falls back to `orchestrator` when no active phase / no resolvable task.
 */
export function deriveSourceAgent(
  project: ProjectRecord,
  tasksMap: ReadonlyMap<string, TaskRecord>,
): SourceAgent {
  // Project-state pre-filter. `arbitrating` is not in the current
  // ProjectState union (`decomposing` | `executing` | `completed` | `failed` |
  // `aborted` per src/lib/project.ts:23) but the plan defensively lists it for
  // forward-compatibility — string compare is safe.
  const projState = (project as { state?: string }).state;
  if (projState === "decomposing" || projState === "arbitrating") {
    return "architect";
  }

  // Most-recently-attached active phase via stable lexicographic taskId sort.
  const activePhases = project.phases.filter(
    (p) => p.state !== "done" && p.state !== "failed",
  );
  const activePhase = activePhases
    .slice()
    .sort((a, b) => (b.taskId ?? "").localeCompare(a.taskId ?? ""))[0];
  if (!activePhase || !activePhase.taskId) return "orchestrator";

  const task = tasksMap.get(activePhase.taskId);
  if (!task) return "orchestrator";

  switch (task.state) {
    case "active":
    case "pending":
    case "shelved":
    case "escalation_wait":
      return "executor";
    case "reviewing":
    case "review_arbitration":
      return "reviewer";
    case "merging":
    case "done":
    case "failed":
    case "paused":
      return "orchestrator";
    default:
      return "orchestrator";
  }
}

/**
 * Derive nudge status for a project per §N3 (precedence: blocked >
 * progressing > stagnant).
 *
 * - blocked: any project task in escalation_wait | review_arbitration |
 *   paused | shelved.
 * - progressing: any project task in merging | done | reviewing AND
 *   `updatedAt` newer than `now - intervalMs`.
 * - stagnant: default fallback when neither prior matches.
 */
export function deriveStatus(
  project: ProjectRecord,
  tasksMap: ReadonlyMap<string, TaskRecord>,
  intervalMs: number,
  now: number,
): NudgeStatus {
  const projectTaskIds = new Set(
    project.phases.map((p) => p.taskId).filter((id): id is string => !!id),
  );
  const projectTasks = Array.from(tasksMap.values()).filter((t) =>
    projectTaskIds.has(t.id),
  );

  const blockedStates = new Set([
    "escalation_wait",
    "review_arbitration",
    "paused",
    "shelved",
  ]);
  if (projectTasks.some((t) => blockedStates.has(t.state))) return "blocked";

  const recentThreshold = now - intervalMs;
  const progressingStates = new Set(["merging", "done", "reviewing"]);
  if (
    projectTasks.some(
      (t) =>
        progressingStates.has(t.state) &&
        Date.parse(t.updatedAt) > recentThreshold,
    )
  ) {
    return "progressing";
  }

  return "stagnant";
}

/**
 * Build observations[] per §N4 / §L2.
 *
 * - stagnant: `["no events in {duration}"]`
 * - progressing: `["last task {id} done {duration} ago", "{N} phases remaining"]`
 * - blocked: `["stuck in {state}"]`
 */
export function buildObservations(
  status: NudgeStatus,
  project: ProjectRecord,
  tasksMap: ReadonlyMap<string, TaskRecord>,
  now: number,
): string[] {
  const projectTaskIds = new Set(
    project.phases.map((p) => p.taskId).filter((id): id is string => !!id),
  );
  const projectTasks = Array.from(tasksMap.values()).filter((t) =>
    projectTaskIds.has(t.id),
  );

  if (status === "stagnant") {
    const updatedAtMs = projectTasks
      .map((t) => Date.parse(t.updatedAt))
      .filter((n) => Number.isFinite(n));
    const oldestActivity = updatedAtMs.length > 0
      ? Math.min(...updatedAtMs)
      : now;
    const durationMs = now - oldestActivity;
    return [`no events in ${formatDuration(durationMs)}`];
  }
  if (status === "progressing") {
    const recentTask = projectTasks
      .filter((t) => t.state === "merging" || t.state === "done")
      .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))[0];
    const observations: string[] = [];
    if (recentTask) {
      observations.push(
        `last task ${recentTask.id} done ${formatDuration(now - Date.parse(recentTask.updatedAt))} ago`,
      );
    }
    const remainingPhases = project.phases.filter(
      (p) => p.state !== "done" && p.state !== "failed",
    ).length;
    observations.push(`${remainingPhases} phases remaining`);
    return observations;
  }
  if (status === "blocked") {
    const blockedStates = new Set([
      "escalation_wait",
      "review_arbitration",
      "paused",
      "shelved",
    ]);
    const blockedTask = projectTasks.find((t) => blockedStates.has(t.state));
    return blockedTask ? [`stuck in ${blockedTask.state}`] : ["stuck"];
  }
  return [];
}

function formatDuration(ms: number): string {
  if (ms < 0) return "0s";
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)} min`;
  return `${(ms / 3_600_000).toFixed(1)} hours`;
}

// --- NudgeIntrospector class ---

export class NudgeIntrospector {
  private readonly state: Pick<StateManager, "getTask" | "getAllTasks">;
  private readonly projectStore: Pick<
    ProjectStore,
    "getAllProjects" | "getProject"
  >;
  private readonly emit: (event: OrchestratorEvent) => void;
  private readonly intervalMs: number;
  private readonly stallSuppressionMultiplier: number;
  private readonly now: () => number;
  /** Per-projectId timestamp of the most recent forwarded session_stalled. */
  private readonly lastStalledAt = new Map<string, number>();
  private timer?: ReturnType<typeof setInterval>;

  constructor(opts: NudgeIntrospectorOpts) {
    this.state = opts.state;
    this.projectStore = opts.projectStore;
    this.emit = opts.emit;
    this.intervalMs = opts.intervalMs ?? DEFAULT_INTERVAL_MS;
    this.stallSuppressionMultiplier =
      opts.stallSuppressionMultiplier ?? DEFAULT_STALL_SUPPRESSION_MULTIPLIER;
    this.now = opts.now ?? Date.now;
  }

  /**
   * Start the periodic timer. Idempotent — second start() is a no-op.
   * First tick fires after `intervalMs` (NOT immediately) to avoid startup
   * spam.
   */
  start(): void {
    if (this.timer !== undefined) return;
    this.timer = setInterval(() => this.tick(), this.intervalMs);
  }

  /**
   * Stop the timer and clear suppression map. Idempotent — double-stop safe.
   */
  stop(): void {
    if (this.timer !== undefined) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
    this.lastStalledAt.clear();
  }

  /**
   * Single introspection pass. Snapshots projects + tasks once; emits one
   * nudge_check per non-suppressed project.
   */
  tick(): void {
    const projects = this.projectStore.getAllProjects();
    const tasks = this.state.getAllTasks();
    const tasksMap = new Map<string, TaskRecord>();
    for (const t of tasks) tasksMap.set(t.id, t);
    const nowMs = this.now();
    const suppressionWindow = this.intervalMs * this.stallSuppressionMultiplier;

    for (const project of projects) {
      const lastStall = this.lastStalledAt.get(project.id);
      if (lastStall !== undefined && nowMs - lastStall < suppressionWindow) {
        continue;
      }

      const status = deriveStatus(project, tasksMap, this.intervalMs, nowMs);
      const sourceAgent = deriveSourceAgent(project, tasksMap);
      const observations = buildObservations(
        status,
        project,
        tasksMap,
        nowMs,
      );

      this.emit({
        type: "nudge_check",
        projectId: project.id,
        sourceAgent,
        status,
        observations,
        nextAction: undefined,
      });
    }
  }

  /**
   * Record a session_stalled timestamp for a project. Subsequent ticks within
   * `intervalMs * stallSuppressionMultiplier` skip emission for that project
   * (the stall event itself carries the operator-attention signal).
   */
  noteStall(projectId: string): void {
    this.lastStalledAt.set(projectId, this.now());
  }
}
