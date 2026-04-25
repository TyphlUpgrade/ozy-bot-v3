/**
 * Live end-to-end project lifecycle run — real SDK, real git, real Architect,
 * real Reviewer, real Executor. Uses a scratch /tmp trunk repo so we never
 * touch the harness-ts source tree.
 *
 * Usage: npx tsx scripts/live-project.ts
 *
 * Flow:
 *   1. Initialize scratch trunk repo + copy harness prompts
 *   2. Wire all Wave 1/1.5/2/3/A/B components (SDKClient, SessionManager,
 *      MergeGate, StateManager, ProjectStore, ReviewGate, ArchitectManager,
 *      Orchestrator, DiscordNotifier with stdout sender, CommandRouter)
 *   3. Declare a trivial project
 *   4. Orchestrator polls → Architect spawns → decomposes → Executor picks
 *      up phase → Reviewer runs → merge → task_done
 *   5. Watch + report pass/fail per milestone
 *
 * Budget caps: $0.50 Architect + $0.30 Executor + $0.30 Reviewer. Worst case
 * 1-phase project total ≈ $1.10. Orchestrator-level budget kill terminates
 * any session that overruns.
 */

import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { execSync } from "node:child_process";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { Orchestrator, type OrchestratorEvent } from "../src/orchestrator.js";
import { SessionManager } from "../src/session/manager.js";
import { SDKClient } from "../src/session/sdk.js";
import { MergeGate } from "../src/gates/merge.js";
import { StateManager } from "../src/lib/state.js";
import { ProjectStore } from "../src/lib/project.js";
import { ReviewGate } from "../src/gates/review.js";
import { ArchitectManager } from "../src/session/architect.js";
import { DiscordNotifier } from "../src/discord/notifier.js";
import type { DiscordSender, AgentIdentity } from "../src/discord/types.js";
import type { HarnessConfig } from "../src/lib/config.js";
import {
  initScratchRepo,
  buildBaseConfig,
  installSigintHandler,
  DEFAULT_POLL_LOOP_MS,
  DEFAULT_RUN_TIMEOUT_MS,
} from "./lib/scratch-repo.js";

function buildConfig(root: string): HarnessConfig {
  return buildBaseConfig({
    root,
    projectName: "live-project",
    reviewer: { max_budget_usd: 1.0, timeout_ms: 180_000, arbitration_threshold: 2 },
    architect: {
      max_budget_usd: 4.0, // Opus + 200-line system prompt + OMC/caveman plugins is heavy
      compaction_threshold_pct: 0.90, // avoid compaction during this run
      arbitration_timeout_ms: 120_000,
      prompt_path: join(root, "config", "harness", "architect-prompt.md"),
    },
    systemPrompt: EXECUTOR_PROMPT,
  });
}

const EXECUTOR_PROMPT = `You are an Executor in a harness-managed git worktree.

When you finish your task, you MUST:
1. Write your code changes into the worktree. DO NOT run \`git add\`. DO NOT run \`git commit\`. The orchestrator will stage and commit your work after the Reviewer approves it.
2. Create directory \`.harness/\` if missing.
3. Write \`.harness/completion.json\` (commitSha is no longer required — omit it):
   {
     "status": "success" | "failure",
     "summary": "<one sentence — used as orchestrator commit message>",
     "filesChanged": ["path1", "path2"]
   }

The completion file is how the orchestrator knows you are done. If you do not write it the task will be marked failed.
`;

// ---------- Runner ----------

async function main(): Promise<void> {
  const root = initScratchRepo({
    prefix: "harness-live-proj",
    gitEmail: "live-proj@harness.test",
    gitName: "live-proj",
    promptFiles: ["architect-prompt.md", "review-prompt.md"],
  });
  console.log(`[live-project] scratch root: ${root}`);

  const config = buildConfig(root);
  const sdk = new SDKClient(query);
  const state = new StateManager(config.project.state_file);
  const projectStore = new ProjectStore(join(root, "projects.json"), config.project.worktree_base);
  const sessions = new SessionManager(sdk, state, config);
  const mergeGate = new MergeGate(config.pipeline, root);
  const reviewGate = new ReviewGate({
    sdk,
    config,
    promptPath: join(root, "config", "harness", "review-prompt.md"),
  });
  // realGitOps (default) — SessionManager uses it internally; ArchitectManager
  // also needs it. Import it via SessionManager's realGitOps for consistency.
  const { realGitOps } = await import("../src/session/manager.js");
  const architectManager = new ArchitectManager({
    sdk,
    projectStore,
    stateManager: state,
    gitOps: realGitOps,
    config,
  });

  const sent: Array<{ channel: string; content: string; identity?: AgentIdentity }> = [];
  const sender: DiscordSender = {
    async sendToChannel(channel, content, identity) {
      sent.push({ channel, content, identity });
      console.log(`  [discord:${channel}] ${content.slice(0, 160)}`);
    },
    async addReaction() { /* noop */ },
  };
  const notifier = new DiscordNotifier(sender, config.discord);

  const orch = new Orchestrator({
    sessionManager: sessions,
    mergeGate,
    stateManager: state,
    config,
    reviewGate,
    architectManager,
    projectStore,
  });

  const events: OrchestratorEvent[] = [];
  orch.on((ev: OrchestratorEvent) => {
    events.push(ev);
    const t = new Date().toISOString().slice(11, 23);
    console.log(`[${t}] ${ev.type} ${JSON.stringify(ev).slice(0, 180)}`);
  });
  orch.on((ev) => notifier.handleEvent(ev));

  installSigintHandler([
    { shutdown: () => orch.shutdown() },
    { shutdown: () => architectManager.shutdownAll() },
  ]);

  const startedAt = Date.now();
  console.log(`[live-project] declaring project...`);
  const result = await orch.declareProject(
    "live-hello",
    "Add a file named `hello.ts` at the repo root that exports `export const MESSAGE = 'hi';`. One phase is sufficient. Keep it minimal.",
    ["no tests", "no README modifications", "no extra files"],
  );

  if ("error" in result) {
    console.error(`[live-project] declareProject FAILED: ${result.error}`);
    process.exit(1);
  }
  console.log(`[live-project] project ${result.projectId} declared; architect session ${result.sessionId.slice(0, 12)}`);

  // Start orchestrator poll loop so it picks up decomposed phase files.
  orch.start();

  // Wait for all phase tasks to reach terminal (done or failed) OR global timeout.
  const isDone = (): boolean => {
    const tasks = state.getAllTasks();
    if (tasks.length === 0) return false; // haven't picked up any phase yet
    return tasks.every((t) => t.state === "done" || t.state === "failed");
  };

  while (!isDone() && Date.now() - startedAt < DEFAULT_RUN_TIMEOUT_MS) {
    await new Promise((r) => setTimeout(r, DEFAULT_POLL_LOOP_MS));
  }

  await orch.shutdown();
  await architectManager.shutdownAll();

  // --- Report ---
  console.log("\n===== LIVE PROJECT RESULT =====");
  console.log(`elapsed: ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
  console.log(`events: ${events.length}`);
  console.log(`discord messages: ${sent.length}`);

  const allTasks = state.getAllTasks();
  console.log(`\ntasks (${allTasks.length}):`);
  for (const t of allTasks) {
    console.log(`  ${t.id} state=${t.state} retries=${t.retryCount} cost=$${t.totalCostUsd.toFixed(2)} summary=${t.summary ?? ""}`);
  }

  const project = projectStore.getProject(result.projectId);
  console.log(`\nproject: ${project?.state ?? "?"}`);
  console.log(`project cost: $${project?.totalCostUsd.toFixed(2) ?? "?"}`);

  const trunkLog = execSync("git log --oneline -10", { cwd: root, encoding: "utf-8" });
  console.log(`\ntrunk commits:\n${trunkLog}`);

  const helloPath = join(root, "hello.ts");
  if (existsSync(helloPath)) {
    console.log(`hello.ts content:\n${readFileSync(helloPath, "utf-8")}`);
  } else {
    console.log(`hello.ts NOT on trunk`);
  }

  // PASS criteria
  const decomposed = events.some((e) => e.type === "project_decomposed");
  const architectSpawned = events.some((e) => e.type === "architect_spawned");
  const taskDone = events.some((e) => e.type === "task_done");
  const allTasksDone = allTasks.length > 0 && allTasks.every((t) => t.state === "done");
  const helloExists = existsSync(helloPath);

  console.log(`\nchecks:`);
  console.log(`  architect_spawned:     ${architectSpawned}`);
  console.log(`  project_decomposed:    ${decomposed}`);
  console.log(`  ≥1 task_done:          ${taskDone}`);
  console.log(`  all phase tasks done:  ${allTasksDone}`);
  console.log(`  hello.ts on trunk:     ${helloExists}`);

  const pass = architectSpawned && decomposed && taskDone && allTasksDone && helloExists;
  console.log(`\nRESULT: ${pass ? "PASS" : "FAIL"}`);
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((err) => {
  console.error("[live-project] FATAL", err);
  process.exit(2);
});
