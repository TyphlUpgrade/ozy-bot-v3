/**
 * Command router — parses Discord input (both `!` commands and natural
 * language) into typed `CommandIntent` values and dispatches them against
 * the harness state (StateManager, optional ProjectStore).
 *
 * NL classification is two-stage:
 *   1. Deterministic regex keyword match (no API call)
 *   2. LLM fallback via an injected `IntentClassifier` (Wave 4 wires a real
 *      SDK-backed classifier; Wave 3 only declares the interface)
 *
 * LLM failure returns `{ type: "unknown" }` — safe default, never crashes.
 *
 * The router does not touch Discord directly; call sites format the returned
 * string and send via `DiscordSender`. Event emissions (`project_declared`,
 * `project_aborted`) are delivered via an injected `emit` callback so the
 * orchestrator — not the router — owns the event stream. Task file writes
 * go through an injected `TaskSink` so tests don't need a tmp dir.
 */

import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { StateManager } from "../lib/state.js";
import type { ProjectStore } from "../lib/project.js";
import type { HarnessConfig } from "../lib/config.js";
import type { Orchestrator, OrchestratorEvent } from "../orchestrator.js";
import { sanitize, sanitizeTaskId } from "../lib/text.js";

// --- Intent types ---

export type CommandIntent =
  | { type: "new_task"; prompt: string }
  | { type: "status_query"; target?: string }
  | { type: "escalation_response"; taskId: string; message: string }
  | { type: "abort_task"; taskId: string }
  | { type: "retry_task"; taskId: string }
  | { type: "declare_project"; message: string }
  | { type: "project_status"; projectId: string }
  | { type: "project_abort"; projectId: string; confirmed: boolean }
  | { type: "unknown" };

export interface ClassifyContext {
  channel: string;
  activeTaskIds: string[];
  escalatedTaskIds: string[];
  activeProjectIds: string[];
  /**
   * CW-4: optional structural recent-messages array for LLM-backed
   * classifiers. Declared here so future CW-4.5 can wire ChannelContextBuffer
   * without touching this type again. v2 always passes undefined; classifier
   * handles missing gracefully. Plain shape — does NOT import InboundMessage
   * to keep commands.ts Discord-agnostic.
   */
  recentMessages?: ReadonlyArray<{ author: string; content: string; timestamp: string }>;
}

export interface IntentClassifier {
  classify(text: string, context: ClassifyContext): Promise<CommandIntent>;
}

/** Fallback classifier — always returns unknown. Used when no LLM path is wired. */
export class UnknownIntentClassifier implements IntentClassifier {
  async classify(_text: string, _context: ClassifyContext): Promise<CommandIntent> {
    return { type: "unknown" };
  }
}

// --- Router dependencies ---

export interface AbortHook {
  abortTask(taskId: string): void;
}

/**
 * Task-file sink. Production wires this to a writer that drops a task file
 * into `config.project.task_dir` where the orchestrator's poll loop picks it
 * up. Tests inject a fake that records the prompt without touching disk.
 */
export interface TaskSink {
  createTask(prompt: string): string;
}

/** Default filesystem-backed TaskSink — writes `{id}.json` to task_dir. */
export class FileTaskSink implements TaskSink {
  constructor(private readonly taskDir: string) {}
  createTask(prompt: string): string {
    const id = `task-${randomUUID().slice(0, 8)}`;
    writeFileSync(join(this.taskDir, `${id}.json`), JSON.stringify({ id, prompt }, null, 2));
    return id;
  }
}

export interface CommandRouterDeps {
  state: StateManager;
  config: HarnessConfig;
  classifier: IntentClassifier;
  /** Required — even if you never expect an abort, inject a no-op to avoid silent success. */
  abort: AbortHook;
  /** Required — task creation is core. Inject `FileTaskSink` in production. */
  taskSink: TaskSink;
  projectStore?: ProjectStore;
  /** Optional event emit hook — if present, router fires project lifecycle events. */
  emit?: (event: OrchestratorEvent) => void;
  /**
   * Wave B: optional Orchestrator for `!project <name>` end-to-end declaration.
   * When present, cmdProjectDeclare calls `orchestrator.declareProject(...)`
   * which spawns the Architect + decomposes. When absent, falls back to the
   * projectStore-only path (Wave 3 behavior: creates record, emits event).
   */
  orchestrator?: Orchestrator;
}

// --- Validation constants ---

const PROJECT_NAME_MAX = 120;

// --- NL patterns (deterministic fast-path) ---

const NL_PATTERNS: Array<{ pattern: RegExp; build: (m: RegExpMatchArray) => CommandIntent }> = [
  // Project intents FIRST — "status of project ..." otherwise collides with status_query.
  // Require sentence-final (no trailing text) after the optional determiner sequence to avoid
  // "start a project status dashboard" eagerly declaring a project.
  {
    pattern: /^(?:start|begin|kick ?off)\s+(?:(?:a|the|new|my|our|another)\s+)*project\b(.*)$/i,
    build: (m) => ({ type: "declare_project", message: m[0].trim() }),
  },
  {
    pattern: /^(?:status|progress|state)\s+of\s+project\s+(\S+)/i,
    build: (m) => ({ type: "project_status", projectId: m[1] }),
  },
  {
    pattern: /^(?:abort|kill|cancel)\s+project\s+(\S+)/i,
    build: (m) => ({ type: "project_abort", projectId: m[1], confirmed: false }),
  },
  // Task intents.
  {
    pattern: /^(?:create|add|build|implement|fix)\b(.*)/i,
    build: (m) => ({ type: "new_task", prompt: m[0].trim() }),
  },
  {
    pattern: /^(?:status|progress|what'?s?\s+(?:happening|going))(?:\s+(\S+))?/i,
    build: (m) => ({ type: "status_query", target: m[1] }),
  },
  {
    pattern: /^(?:reply|respond)\s+(?:to\s+)?([a-zA-Z0-9_-]+)\s+(.+)/i,
    build: (m) => ({ type: "escalation_response", taskId: m[1], message: m[2] }),
  },
];

function parseNonGoalsBlock(description: string): string[] | null {
  const match = description.match(/NON-GOALS:\s*\n((?:\s*-\s+.+\n?)+)/i);
  if (!match) return null;
  const lines = match[1].split("\n").map((l) => l.trim()).filter((l) => l.startsWith("-"));
  if (lines.length === 0) return null;
  return lines.map((l) => l.replace(/^-\s*/, "").trim()).filter((s) => s.length > 0);
}

// --- Router ---

export class CommandRouter {
  private readonly state: StateManager;
  private readonly config: HarnessConfig;
  private readonly classifier: IntentClassifier;
  private readonly abort: AbortHook;
  private readonly taskSink: TaskSink;
  private readonly projectStore?: ProjectStore;
  private readonly emit?: (event: OrchestratorEvent) => void;
  private readonly orchestrator?: Orchestrator;

  constructor(deps: CommandRouterDeps) {
    this.state = deps.state;
    this.config = deps.config;
    this.classifier = deps.classifier;
    this.abort = deps.abort;
    this.taskSink = deps.taskSink;
    this.projectStore = deps.projectStore;
    this.emit = deps.emit;
    this.orchestrator = deps.orchestrator;
  }

  /** Handle a structured command (the leading `!` has already been stripped). */
  async handleCommand(command: string, args: string, channelId: string): Promise<string> {
    if (!this.channelAllowed(channelId)) return "Channel not configured.";
    switch (command) {
      case "task": return this.cmdTask(args);
      case "status": return this.cmdStatus(args);
      case "abort": return this.cmdAbort(args);
      case "retry": return this.cmdRetry(args);
      case "reply": return this.cmdReply(args);
      case "project": return this.cmdProject(args);
      default: return `Unknown command: !${sanitize(command, 40)}`;
    }
  }

  /** Handle natural language (post-accumulator). */
  async handleNaturalLanguage(text: string, channelId: string, _userId: string): Promise<string> {
    if (!this.channelAllowed(channelId)) return "Channel not configured.";

    // Step 1: deterministic keyword match (no API cost).
    for (const { pattern, build } of NL_PATTERNS) {
      const m = text.match(pattern);
      if (m) return this.dispatchIntent(build(m), channelId);
    }

    // Step 2: LLM classifier fallback.
    const ctx: ClassifyContext = {
      channel: channelId,
      activeTaskIds: this.state.getTasksByState("active").map((t) => t.id),
      escalatedTaskIds: this.state.getTasksByState("escalation_wait").map((t) => t.id),
      activeProjectIds: this.projectStore?.getAllProjects().map((p) => p.id) ?? [],
    };
    let intent: CommandIntent;
    try {
      intent = await this.classifier.classify(text, ctx);
    } catch {
      intent = { type: "unknown" };
    }
    return this.dispatchIntent(intent, channelId);
  }

  // --- Command handlers ---

  private cmdTask(args: string): string {
    const prompt = args.trim();
    if (!prompt) return "Usage: `!task <prompt>`";
    const id = this.taskSink.createTask(prompt);
    return `Task \`${id}\` created.`;
  }

  private cmdStatus(args: string): string {
    const target = args.trim();
    if (!target) {
      const projects = this.projectStore?.getAllProjects() ?? [];
      const tasks = this.state.getAllTasks();
      const out: string[] = [];
      if (projects.length > 0) {
        out.push("**Projects:**");
        out.push(...projects.map((p) => `- \`${p.id.slice(0, 8)}\` ${sanitize(p.name, 40)} (${p.state})`));
      }
      if (tasks.length > 0) {
        out.push("**Tasks:**");
        out.push(...tasks.slice(0, 20).map((t) => `- \`${t.id}\` (${t.state})`));
      }
      return out.length > 0 ? out.join("\n") : "No projects or tasks.";
    }
    const project = this.projectStore?.getProject(target);
    if (project) return this.formatProjectStatus(target);
    const task = this.state.getTask(target);
    if (task) {
      return `Task \`${task.id}\`: ${task.state} (cost $${task.totalCostUsd.toFixed(2)}, retries ${task.retryCount})`;
    }
    return `No task or project \`${sanitize(target, 64)}\`.`;
  }

  private cmdAbort(args: string): string {
    const taskId = sanitizeTaskId(args.trim());
    if (!taskId) return "Usage: `!abort <taskId>`";
    if (!this.state.getTask(taskId)) return `No task \`${taskId}\`.`;
    this.abort.abortTask(taskId);
    return `Task \`${taskId}\` aborted.`;
  }

  private cmdRetry(args: string): string {
    const taskId = sanitizeTaskId(args.trim());
    if (!taskId) return "Usage: `!retry <taskId>`";
    const task = this.state.getTask(taskId);
    if (!task) return `No task \`${taskId}\`.`;
    if (task.state !== "failed") return `Task \`${taskId}\` is not in failed state (currently ${task.state}).`;
    this.state.transition(taskId, "pending");
    return `Task \`${taskId}\` re-queued.`;
  }

  private cmdReply(args: string): string {
    const m = args.trim().match(/^(\S+)\s+(.+)$/);
    if (!m) return "Usage: `!reply <taskId> <message>`";
    const taskId = sanitizeTaskId(m[1]);
    if (!taskId) return "Invalid task id.";
    const task = this.state.getTask(taskId);
    if (!task) return `No task \`${taskId}\`.`;
    // Gate replies to tasks actually awaiting operator input.
    if (task.state !== "escalation_wait" && !task.dialoguePendingConfirmation) {
      return `Task \`${taskId}\` is not awaiting a response (state: ${task.state}).`;
    }
    this.state.updateTask(taskId, {
      dialogueMessages: [
        ...(task.dialogueMessages ?? []),
        { role: "operator", content: m[2], timestamp: new Date().toISOString() },
      ],
    });
    return `Response sent to \`${taskId}\`.`;
  }

  private async cmdProject(args: string): Promise<string> {
    const trimmed = args.trim();
    if (!trimmed) return "Usage: `!project <name>\\n<description>\\nNON-GOALS:\\n- ...`";

    // Two-arg project sub-commands: "<id> status", "<id> abort", "<id> abort confirm"
    const firstLine = trimmed.split("\n")[0];
    const subMatch = firstLine.match(/^(\S+)\s+(status|abort)(?:\s+(confirm))?\s*$/);
    if (subMatch) {
      const [, projectId, sub, confirm] = subMatch;
      if (sub === "status") return this.formatProjectStatus(projectId);
      if (sub === "abort") return this.cmdProjectAbort(projectId, confirm === "confirm");
    }

    // Otherwise treat args as project declaration: "<name>\n<description with NON-GOALS>"
    return this.cmdProjectDeclare(trimmed);
  }

  private async cmdProjectDeclare(body: string): Promise<string> {
    if (!this.projectStore) return "Project store not configured.";
    const firstNewline = body.indexOf("\n");
    if (firstNewline < 0) {
      return "Usage: `!project <name>\\n<description>\\nNON-GOALS:\\n- ...`";
    }
    const name = body.slice(0, firstNewline).trim();
    if (name.length === 0) return "Project name cannot be empty.";
    if (name.length > PROJECT_NAME_MAX) return `Project name exceeds ${PROJECT_NAME_MAX}-char limit.`;
    if (name.includes("\r")) return "Project name contains carriage return — use a single line.";

    const description = body.slice(firstNewline + 1);
    const nonGoals = parseNonGoalsBlock(description);
    if (nonGoals === null) {
      return "Missing required `NON-GOALS:` section. Add `NON-GOALS:` followed by one or more `- <item>` lines.";
    }

    // Wave B: when orchestrator is wired, route through the full declare path
    // (creates project + spawns Architect + fires decomposition). Fallback to
    // projectStore-only is kept for Wave 3 tests that don't run the Architect.
    if (this.orchestrator) {
      const result = await this.orchestrator.declareProject(name, description, nonGoals);
      if ("error" in result) {
        return `Project declaration failed: ${sanitize(result.error, 200)}`;
      }
      return `Project \`${result.projectId}\` declared: **${sanitize(name, 80)}**. Architect spawned.`;
    }

    const project = this.projectStore.createProject(name, description, nonGoals);
    this.emit?.({ type: "project_declared", projectId: project.id, name: project.name });
    return `Project \`${project.id}\` declared: **${sanitize(project.name, 80)}**.`;
  }

  private cmdProjectAbort(projectId: string, confirmed: boolean): string {
    if (!this.projectStore) return "Project store not configured.";
    const project = this.projectStore.getProject(projectId);
    if (!project) return `No project \`${sanitize(projectId, 64)}\`.`;
    if (!confirmed) return `Are you sure? Confirm with \`!project ${projectId} abort confirm\`.`;
    this.projectStore.abortProject(projectId);
    this.emit?.({ type: "project_aborted", projectId, operatorId: "operator" });
    return `Project \`${projectId}\` aborted.`;
  }

  private formatProjectStatus(projectId: string): string {
    const project = this.projectStore?.getProject(projectId);
    if (!project) return `No project \`${sanitize(projectId, 64)}\`.`;
    const done = project.phases.filter((p) => p.state === "done").length;
    const total = project.phases.length;
    const lines: string[] = [
      `Project \`${project.id}\` — **${sanitize(project.name, 80)}**`,
      `State: ${project.state}`,
      `Phases (${done}/${total}):`,
    ];
    for (const p of project.phases) {
      lines.push(
        `  - ${sanitize(p.id, 40)} (${p.state}, reviewer rejections: ${p.reviewerRejectionCount}, arbitration: ${p.arbitrationCount})`,
      );
    }
    lines.push(`Cost: $${project.totalCostUsd.toFixed(2)} / $${project.budgetCeilingUsd.toFixed(2)}`);
    lines.push(`Architect: generation ${project.compactionGeneration}`);
    return lines.join("\n");
  }

  // --- NL intent dispatch ---

  private async dispatchIntent(intent: CommandIntent, channelId: string): Promise<string> {
    switch (intent.type) {
      case "new_task":
        return this.cmdTask(intent.prompt);
      case "status_query":
        return this.cmdStatus(intent.target ?? "");
      case "escalation_response":
        return this.cmdReply(`${intent.taskId} ${intent.message}`);
      case "abort_task":
        return this.cmdAbort(intent.taskId);
      case "retry_task":
        return this.cmdRetry(intent.taskId);
      case "declare_project":
        // NL cannot carry a structured NON-GOALS block — route to cmdProjectDeclare
        // so the operator sees the same instructive error path as `!project` users.
        return this.cmdProjectDeclare(intent.message);
      case "project_status":
        return this.formatProjectStatus(intent.projectId);
      case "project_abort":
        return this.cmdProjectAbort(intent.projectId, intent.confirmed);
      case "unknown":
        if (this.isDialogueChannel(channelId) && this.hasNoActiveContext()) {
          return "No active project or dialogue. Use `!task`, `!project`, or `!dialogue` to start one.";
        }
        return "Could not understand the message. Try `!task <prompt>` or `!status`.";
    }
  }

  // --- Util ---

  private channelAllowed(channelId: string): boolean {
    const d = this.config.discord;
    return (
      channelId === d.dev_channel ||
      channelId === d.ops_channel ||
      channelId === d.escalation_channel
    );
  }

  private isDialogueChannel(channelId: string): boolean {
    // The plan anticipates a dedicated dialogue channel in Wave 6-split.
    // Wave 3 treats dev_channel as the dialogue channel until that's configured.
    return channelId === this.config.discord.dev_channel;
  }

  private hasNoActiveContext(): boolean {
    const hasProject = (this.projectStore?.getAllProjects() ?? []).some(
      (p) => p.state === "decomposing" || p.state === "executing",
    );
    const hasDialogue = this.state.getAllTasks().some((t) => !!t.dialoguePendingConfirmation);
    return !hasProject && !hasDialogue;
  }
}
