import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  CommandRouter,
  UnknownIntentClassifier,
  type CommandIntent,
  type IntentClassifier,
  type ClassifyContext,
  type TaskSink,
  type AbortHook,
} from "../../src/discord/commands.js";
import { StateManager } from "../../src/lib/state.js";
import { ProjectStore } from "../../src/lib/project.js";
import type { HarnessConfig } from "../../src/lib/config.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";

// --- Fakes + fixtures ---

let tmpDir: string;
let config: HarnessConfig;
let state: StateManager;
let projectStore: ProjectStore;
let aborted: string[];
let emitted: OrchestratorEvent[];
let createdTasks: Array<{ id: string; prompt: string }>;

function buildConfig(root: string): HarnessConfig {
  return {
    project: {
      name: "test",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "wt"),
      session_dir: join(root, "sessions"),
    },
    pipeline: {
      poll_interval: 1,
      test_command: "true",
      max_retries: 1,
      test_timeout: 60,
      escalation_timeout: 300,
      retry_delay_ms: 100,
    },
    discord: {
      bot_token_env: "T",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {
        orchestrator: { name: "Harness", avatar_url: "" },
        architect: { name: "Architect", avatar_url: "" },
        reviewer: { name: "Reviewer", avatar_url: "" },
      },
    },
  };
}

function makeRouter(classifier: IntentClassifier = new UnknownIntentClassifier()): CommandRouter {
  const taskSink: TaskSink = {
    createTask: (prompt: string): string => {
      const id = `task-fake-${createdTasks.length + 1}`;
      createdTasks.push({ id, prompt });
      return id;
    },
  };
  const abort: AbortHook = { abortTask: (id: string) => aborted.push(id) };
  return new CommandRouter({
    state,
    config,
    classifier,
    projectStore,
    abort,
    taskSink,
    emit: (e) => emitted.push(e),
  });
}

beforeEach(() => {
  tmpDir = join(tmpdir(), `cmd-test-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  mkdirSync(tmpDir, { recursive: true });
  mkdirSync(join(tmpDir, "tasks"), { recursive: true });
  config = buildConfig(tmpDir);
  state = new StateManager(config.project.state_file);
  projectStore = new ProjectStore(join(tmpDir, "projects.json"), config.project.worktree_base);
  aborted = [];
  emitted = [];
  createdTasks = [];
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("CommandRouter — structured commands", () => {
  it("!task routes through TaskSink (no direct fs write)", async () => {
    const r = makeRouter();
    const reply = await r.handleCommand("task", "add input validation", "dev");
    expect(reply).toMatch(/Task `task-fake-1` created/);
    expect(createdTasks).toEqual([{ id: "task-fake-1", prompt: "add input validation" }]);
  });

  it("!task with no args returns usage hint", async () => {
    const r = makeRouter();
    const reply = await r.handleCommand("task", "", "dev");
    expect(reply).toMatch(/Usage/);
  });

  it("!status with no args and no tasks returns default message", async () => {
    const r = makeRouter();
    const reply = await r.handleCommand("status", "", "dev");
    expect(reply).toMatch(/No projects or tasks/);
  });

  it("!status <taskId> returns task state", async () => {
    state.createTask("do a thing", "task-abc");
    const r = makeRouter();
    const reply = await r.handleCommand("status", "task-abc", "dev");
    expect(reply).toMatch(/Task `task-abc`: pending/);
  });

  it("!abort dispatches to AbortHook", async () => {
    state.createTask("test", "task-kill");
    const r = makeRouter();
    const reply = await r.handleCommand("abort", "task-kill", "dev");
    expect(reply).toMatch(/aborted/);
    expect(aborted).toEqual(["task-kill"]);
  });

  it("!abort unknown task returns not-found", async () => {
    const r = makeRouter();
    const reply = await r.handleCommand("abort", "task-ghost", "dev");
    expect(reply).toMatch(/No task/);
  });

  it("!retry re-queues a failed task", async () => {
    const task = state.createTask("test", "task-redo");
    state.transition(task.id, "active");
    state.transition(task.id, "failed");
    const r = makeRouter();
    const reply = await r.handleCommand("retry", "task-redo", "dev");
    expect(reply).toMatch(/re-queued/);
    expect(state.getTask("task-redo")!.state).toBe("pending");
  });

  it("!retry refuses non-failed task", async () => {
    state.createTask("test", "task-active");
    const r = makeRouter();
    const reply = await r.handleCommand("retry", "task-active", "dev");
    expect(reply).toMatch(/not in failed state/);
  });

  it("!reply gates on escalation_wait state", async () => {
    const task = state.createTask("active work", "task-chat");
    // Task is pending by default — reply should be rejected.
    const r = makeRouter();
    const pendingReply = await r.handleCommand("reply", "task-chat hello", "dev");
    expect(pendingReply).toMatch(/not awaiting a response/);

    // Transition to escalation_wait — now reply is accepted.
    state.transition(task.id, "active");
    state.transition(task.id, "escalation_wait");
    const acceptedReply = await r.handleCommand("reply", "task-chat this is my answer", "dev");
    expect(acceptedReply).toMatch(/Response sent/);
    const msgs = state.getTask("task-chat")!.dialogueMessages!;
    expect(msgs).toHaveLength(1);
    expect(msgs[0].role).toBe("operator");
    expect(msgs[0].content).toBe("this is my answer");
  });

  it("unknown command returns error", async () => {
    const r = makeRouter();
    const reply = await r.handleCommand("frobnicate", "", "dev");
    expect(reply).toMatch(/Unknown command/);
  });

  it("command from non-configured channel is ignored", async () => {
    const r = makeRouter();
    const reply = await r.handleCommand("task", "x", "random-channel");
    expect(reply).toMatch(/not configured/);
  });
});

describe("CommandRouter — NL classification (deterministic patterns)", () => {
  it("'create ...' routes to new_task via keyword match", async () => {
    const r = makeRouter();
    const reply = await r.handleNaturalLanguage("create a user profile page", "dev", "user1");
    expect(reply).toMatch(/created/);
  });

  it("'status of project <id>' routes to project_status", async () => {
    const p = projectStore.createProject("my-proj", "desc", ["no auth"]);
    const r = makeRouter();
    const reply = await r.handleNaturalLanguage(`status of project ${p.id}`, "dev", "user1");
    expect(reply).toMatch(/Project `.+` — \*\*my-proj\*\*/);
  });

  it("'reply to <taskId> <msg>' routes to escalation_response (when task is awaiting)", async () => {
    const task = state.createTask("blocked", "task-esc");
    state.transition(task.id, "active");
    state.transition(task.id, "escalation_wait");
    const r = makeRouter();
    const reply = await r.handleNaturalLanguage("reply to task-esc here is my answer", "dev", "user1");
    expect(reply).toMatch(/Response sent/);
    expect(state.getTask("task-esc")!.dialogueMessages).toHaveLength(1);
  });

  it("LLM classifier failure returns unknown intent (safe default)", async () => {
    const failing: IntentClassifier = { classify: async () => { throw new Error("down"); } };
    const r = makeRouter(failing);
    const reply = await r.handleNaturalLanguage("ambiguous text nobody can parse", "dev", "user1");
    expect(reply).toMatch(/Could not understand|No active project/);
  });

  it("LLM classifier returns a parsed CommandIntent when keyword match misses", async () => {
    state.createTask("blocked", "task-llm");
    const classifier: IntentClassifier = {
      async classify(_text: string, _ctx: ClassifyContext): Promise<CommandIntent> {
        return { type: "status_query", target: "task-llm" };
      },
    };
    const r = makeRouter(classifier);
    const reply = await r.handleNaturalLanguage("where are we at with that one thing", "dev", "user1");
    expect(reply).toMatch(/Task `task-llm`/);
  });

  it("NL pattern precedence — 'status of project X' picks project_status, not status_query", async () => {
    // This test pins the ordering of NL_PATTERNS. If a future contributor
    // moves status_query above project_status, this test fails.
    const p = projectStore.createProject("pinned", "desc", []);
    const r = makeRouter();
    const reply = await r.handleNaturalLanguage(`status of project ${p.id}`, "dev", "user1");
    expect(reply).toMatch(/State: decomposing/);
    expect(reply).not.toMatch(/No task or project/);
  });
});

describe("CommandRouter — three-tier project commands", () => {
  it("!project <name>\\n<desc>\\nNON-GOALS creates a project + emits project_declared", async () => {
    const r = makeRouter();
    const args = "auth-rewrite\nReplace legacy auth with JWT\nNON-GOALS:\n- no UI changes\n- no DB migration";
    const reply = await r.handleCommand("project", args, "dev");
    expect(reply).toMatch(/declared/);
    const projects = projectStore.getAllProjects();
    expect(projects).toHaveLength(1);
    expect(projects[0].name).toBe("auth-rewrite");
    expect(projects[0].nonGoals).toEqual(["no UI changes", "no DB migration"]);
    expect(emitted.some((e) => e.type === "project_declared")).toBe(true);
  });

  it("!project refuses declaration without NON-GOALS section", async () => {
    const r = makeRouter();
    const args = "bad-proj\njust a description";
    const reply = await r.handleCommand("project", args, "dev");
    expect(reply).toMatch(/Missing required `NON-GOALS:` section/);
    expect(projectStore.getAllProjects()).toHaveLength(0);
  });

  it("!project rejects project name exceeding length cap", async () => {
    const r = makeRouter();
    const longName = "x".repeat(200);
    const args = `${longName}\ndesc\nNON-GOALS:\n- x`;
    const reply = await r.handleCommand("project", args, "dev");
    expect(reply).toMatch(/exceeds.*limit/);
  });

  it("!project <id> status returns formatted project status", async () => {
    const p = projectStore.createProject("my-proj", "desc", ["ng"]);
    projectStore.addPhase(p.id, "spec-1");
    const r = makeRouter();
    const reply = await r.handleCommand("project", `${p.id} status`, "dev");
    expect(reply).toMatch(/State: decomposing/);
    expect(reply).toMatch(/Phases \(0\/1\)/);
    expect(reply).toMatch(/Cost: \$0\.00/);
  });

  it("!project <id> abort first call returns confirmation prompt", async () => {
    const p = projectStore.createProject("my-proj", "desc", []);
    const r = makeRouter();
    const reply = await r.handleCommand("project", `${p.id} abort`, "dev");
    expect(reply).toMatch(/Confirm with/);
    expect(projectStore.getProject(p.id)!.state).toBe("decomposing");
    expect(emitted.filter((e) => e.type === "project_aborted")).toHaveLength(0);
  });

  it("!project <id> abort confirm actually aborts + emits project_aborted", async () => {
    const p = projectStore.createProject("my-proj", "desc", []);
    const r = makeRouter();
    const reply = await r.handleCommand("project", `${p.id} abort confirm`, "dev");
    expect(reply).toMatch(/aborted/);
    expect(projectStore.getProject(p.id)!.state).toBe("aborted");
    expect(emitted.some((e) => e.type === "project_aborted")).toBe(true);
  });

  it("'start a new project' NL hands off to cmdProjectDeclare (no NON-GOALS → instructive error)", async () => {
    const r = makeRouter();
    const reply = await r.handleNaturalLanguage("start a new project to rewrite auth", "dev", "user1");
    // NL sentence has no newline+NON-GOALS block, so cmdProjectDeclare returns
    // the usage hint — same error the user would get for a malformed !project.
    expect(reply).toMatch(/Usage: `!project|Missing required `NON-GOALS:`/);
  });

  it("'abort project <id>' NL routes to project_abort (first-call confirmation)", async () => {
    const p = projectStore.createProject("my-proj", "desc", []);
    const r = makeRouter();
    const reply = await r.handleNaturalLanguage(`abort project ${p.id}`, "dev", "user1");
    expect(reply).toMatch(/Confirm with/);
  });

  it("!status with no args lists projects and tasks", async () => {
    projectStore.createProject("proj-x", "desc", []);
    state.createTask("task prompt", "task-1");
    const r = makeRouter();
    const reply = await r.handleCommand("status", "", "dev");
    expect(reply).toMatch(/Projects:/);
    expect(reply).toMatch(/proj-x/);
    expect(reply).toMatch(/Tasks:/);
    expect(reply).toMatch(/task-1/);
  });

  it("!status <projectId> returns project status (falls back from task lookup)", async () => {
    const p = projectStore.createProject("proj-y", "desc", []);
    const r = makeRouter();
    const reply = await r.handleCommand("status", p.id, "dev");
    expect(reply).toMatch(/proj-y/);
    expect(reply).toMatch(/State:/);
  });

  it("dialogue channel empty-state prompts operator when no project + no dialogue active", async () => {
    const r = makeRouter();
    const reply = await r.handleNaturalLanguage("hey what's up", "dev", "user1");
    expect(reply).toMatch(/No active project or dialogue/);
  });
});
