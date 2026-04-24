/**
 * Project store — persistent record of multi-phase projects owned by the
 * Architect tier (three-tier model, Wave 1.5b scaffolding).
 *
 * Each ProjectRecord tracks a single operator-declared project: its name,
 * description, verbatim non-goals, the Architect worktree bound to it, and
 * the sequence of phases it decomposed into. Each phase references the
 * TaskRecord the Executor ran for it (if any).
 *
 * This file is pure state. It does NOT spawn sessions, create worktrees, or
 * call out to the SDK — those side effects live in ArchitectManager (Wave B).
 *
 * Persistence mirrors StateManager: atomic write (temp + rename), B7 unknown-
 * key drop on load, corrupt file → start fresh.
 */

import { readFileSync, writeFileSync, renameSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";

// --- Types ---

export type ProjectState = "decomposing" | "executing" | "completed" | "failed" | "aborted";
export type PhaseState = "pending" | "active" | "done" | "failed";

export interface ProjectPhase {
  id: string;
  taskId?: string;
  state: PhaseState;
  spec: string;
  reviewerRejectionCount: number;
  arbitrationCount: number;
}

export interface ArchitectCompactionSummary {
  projectId: string;
  name: string;
  description: string;
  nonGoals: string[];
  priorVerdicts: Array<{
    phaseId: string;
    verdict: "retry_with_directive" | "plan_amendment" | "escalate_operator";
    rationale: string;
    timestamp: string;
  }>;
  completedPhases: Array<{
    phaseId: string;
    taskId: string;
    state: "done" | "failed";
    finalCostUsd: number;
    finalVerdict?: string;
  }>;
  currentPhaseContext: {
    phaseId: string;
    taskId: string;
    state: string;
    reviewerRejectionCount: number;
    arbitrationCount: number;
    lastDirective?: string;
  };
  compactedAt: string;
  compactionGeneration: number;
}

export interface ProjectRecord {
  id: string;
  name: string;
  description: string;
  nonGoals: string[];                           // verbatim from operator declaration
  state: ProjectState;
  architectSessionId?: string;
  architectWorktreePath: string;                // derived from projectId + worktreeBase
  architectSummary?: ArchitectCompactionSummary;
  compactionGeneration: number;                 // 0 until first compaction
  phases: ProjectPhase[];
  totalCostUsd: number;
  budgetCeilingUsd: number;                     // default: 10 * pipeline.max_budget_usd
  totalTier1EscalationCount: number;            // per-project aggregate
  createdAt: string;
  updatedAt: string;
  completedAt?: string;
}

interface ProjectStoreData {
  projects: Record<string, ProjectRecord>;
  version: number;
}

// Defensive B7 key set — mirrors StateManager pattern so forward-compat is bounded.
const KNOWN_PROJECT_KEYS: ReadonlySet<string> = new Set([
  "id", "name", "description", "nonGoals", "state",
  "architectSessionId", "architectWorktreePath", "architectSummary",
  "compactionGeneration", "phases",
  "totalCostUsd", "budgetCeilingUsd", "totalTier1EscalationCount",
  "createdAt", "updatedAt", "completedAt",
]);

const KNOWN_PHASE_KEYS: ReadonlySet<string> = new Set([
  "id", "taskId", "state", "spec", "reviewerRejectionCount", "arbitrationCount",
]);

const VALID_PROJECT_STATES: ReadonlySet<string> = new Set([
  "decomposing", "executing", "completed", "failed", "aborted",
]);

const VALID_PHASE_STATES: ReadonlySet<string> = new Set([
  "pending", "active", "done", "failed",
]);

// --- Store ---

export interface ProjectStoreOptions {
  /** Default budgetCeilingUsd applied to new projects. Plan C.4: 10 * max_budget_usd. */
  defaultBudgetCeilingUsd?: number;
}

export class ProjectStore {
  private data: ProjectStoreData;
  private readonly statePath: string;
  private readonly worktreeBase: string;
  private readonly defaultBudgetCeilingUsd: number;

  constructor(statePath: string, worktreeBase: string, options: ProjectStoreOptions = {}) {
    this.statePath = statePath;
    this.worktreeBase = worktreeBase;
    this.defaultBudgetCeilingUsd = options.defaultBudgetCeilingUsd ?? 10;
    this.data = this.load();
  }

  private load(): ProjectStoreData {
    if (!existsSync(this.statePath)) {
      return { projects: {}, version: 1 };
    }
    try {
      const raw = readFileSync(this.statePath, "utf-8");
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      const projects: Record<string, ProjectRecord> = {};
      const rawProjects = (parsed.projects ?? {}) as Record<string, Record<string, unknown>>;
      for (const [id, rawProj] of Object.entries(rawProjects)) {
        const cleaned: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(rawProj)) {
          if (KNOWN_PROJECT_KEYS.has(k)) cleaned[k] = v;
        }
        // Validate state
        if (!VALID_PROJECT_STATES.has(cleaned.state as string)) {
          cleaned.state = "failed";
        }
        // Clean phases
        if (Array.isArray(cleaned.phases)) {
          cleaned.phases = (cleaned.phases as Record<string, unknown>[]).map((p) => {
            const cp: Record<string, unknown> = {};
            for (const [pk, pv] of Object.entries(p)) {
              if (KNOWN_PHASE_KEYS.has(pk)) cp[pk] = pv;
            }
            if (!VALID_PHASE_STATES.has(cp.state as string)) cp.state = "failed";
            return cp;
          });
        } else {
          cleaned.phases = [];
        }
        projects[id] = cleaned as unknown as ProjectRecord;
      }
      return { projects, version: (parsed.version as number) ?? 1 };
    } catch {
      return { projects: {}, version: 1 };
    }
  }

  private persist(): void {
    const dir = dirname(this.statePath);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    const tmp = join(dir, `.projects-${randomUUID()}.tmp`);
    writeFileSync(tmp, JSON.stringify(this.data, null, 2), "utf-8");
    renameSync(tmp, this.statePath);
  }

  /** Create a project. nonGoals must be an array (empty permitted; undefined rejected). */
  createProject(name: string, description: string, nonGoals: string[]): ProjectRecord {
    if (!Array.isArray(nonGoals)) {
      throw new Error("createProject: nonGoals must be an array (empty [] permitted, undefined rejected)");
    }
    const id = randomUUID();
    const now = new Date().toISOString();
    const project: ProjectRecord = {
      id,
      name,
      description,
      nonGoals: [...nonGoals], // defensive copy
      state: "decomposing",
      architectWorktreePath: join(this.worktreeBase, `architect-${id}`),
      compactionGeneration: 0,
      phases: [],
      totalCostUsd: 0,
      budgetCeilingUsd: this.defaultBudgetCeilingUsd,
      totalTier1EscalationCount: 0,
      createdAt: now,
      updatedAt: now,
    };
    this.data.projects[id] = project;
    this.persist();
    return project;
  }

  getProject(projectId: string): ProjectRecord | undefined {
    return this.data.projects[projectId];
  }

  getAllProjects(): ProjectRecord[] {
    return Object.values(this.data.projects);
  }

  /** Append a phase to the project. Returns the phase id. */
  addPhase(projectId: string, spec: string, phaseId?: string): string {
    const project = this.requireProject(projectId);
    const id = phaseId ?? randomUUID();
    project.phases.push({
      id,
      state: "pending",
      spec,
      reviewerRejectionCount: 0,
      arbitrationCount: 0,
    });
    this.touch(project);
    return id;
  }

  /** Bind a task id to a phase, transitioning it to active. */
  attachTask(projectId: string, phaseId: string, taskId: string): void {
    const project = this.requireProject(projectId);
    const phase = this.requirePhase(project, phaseId);
    phase.taskId = taskId;
    phase.state = "active";
    this.touch(project);
  }

  incrementCost(projectId: string, costUsd: number): void {
    const project = this.requireProject(projectId);
    project.totalCostUsd += costUsd;
    this.touch(project);
  }

  /** Increment the project's tier-1 escalation aggregate; return the new total. */
  incrementTier1Escalation(projectId: string): number {
    const project = this.requireProject(projectId);
    project.totalTier1EscalationCount += 1;
    this.touch(project);
    return project.totalTier1EscalationCount;
  }

  markPhaseDone(projectId: string, phaseId: string): void {
    const project = this.requireProject(projectId);
    const phase = this.requirePhase(project, phaseId);
    phase.state = "done";
    this.touch(project);
  }

  markPhaseFailed(projectId: string, phaseId: string, _reason: string): void {
    const project = this.requireProject(projectId);
    const phase = this.requirePhase(project, phaseId);
    phase.state = "failed";
    // Reason is logged by the orchestrator; ProjectStore stays side-effect-free.
    this.touch(project);
  }

  hasActivePhases(projectId: string): boolean {
    const project = this.data.projects[projectId];
    if (!project) return false;
    return project.phases.some((p) => p.state === "active" || p.state === "pending");
  }

  completeProject(projectId: string): void {
    const project = this.requireProject(projectId);
    project.state = "completed";
    project.completedAt = new Date().toISOString();
    this.touch(project);
  }

  failProject(projectId: string, _reason: string): void {
    const project = this.requireProject(projectId);
    project.state = "failed";
    project.completedAt = new Date().toISOString();
    this.touch(project);
  }

  abortProject(projectId: string): void {
    const project = this.requireProject(projectId);
    project.state = "aborted";
    project.completedAt = new Date().toISOString();
    this.touch(project);
  }

  setArchitectSummary(projectId: string, summary: ArchitectCompactionSummary): void {
    const project = this.requireProject(projectId);
    project.architectSummary = summary;
    project.compactionGeneration += 1;
    this.touch(project);
  }

  // --- Internals ---

  private requireProject(projectId: string): ProjectRecord {
    const project = this.data.projects[projectId];
    if (!project) throw new Error(`Project not found: ${projectId}`);
    return project;
  }

  private requirePhase(project: ProjectRecord, phaseId: string): ProjectPhase {
    const phase = project.phases.find((p) => p.id === phaseId);
    if (!phase) throw new Error(`Phase not found: ${phaseId} in project ${project.id}`);
    return phase;
  }

  private touch(project: ProjectRecord): void {
    project.updatedAt = new Date().toISOString();
    this.persist();
  }
}
