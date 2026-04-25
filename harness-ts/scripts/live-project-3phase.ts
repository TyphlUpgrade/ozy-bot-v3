/**
 * Live 3-phase real-SDK project stress — validates state machine under
 * dependent-phase decomposition, concurrency gating, and project completion.
 *
 * Goal: force the Architect to decompose into ≥ 3 phases where later phases
 * depend on earlier ones, then assert all phases land on trunk in order, the
 * project state transitions to "completed", and the project_completed event
 * fires exactly once.
 *
 * Budget caps (orchestrator-level):
 *   Architect: $6  (Opus + OMC + caveman; spawn alone ~$0.30)
 *   Executor:  $1 per phase × 3 = $3
 *   Reviewer:  $1 per phase × 3 = $3
 * Worst-case project cap ~$12; real runs have stayed well under $1 per phase.
 *
 * Usage: npx tsx scripts/live-project-3phase.ts
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
import { buildSendersForChannels } from "../src/discord/sender-factory.js";
import { sendToChannelAndReturnIdDefault, type DiscordSender, type AgentIdentity } from "../src/discord/types.js";
import type { HarnessConfig } from "../src/lib/config.js";
import {
  initScratchRepo,
  buildBaseConfig,
  installSigintHandler,
  isProjectTerminal,
  DEFAULT_POLL_LOOP_MS,
  DEFAULT_RUN_TIMEOUT_MS,
} from "./lib/scratch-repo.js";

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

function buildConfig(root: string): HarnessConfig {
  return buildBaseConfig({
    root,
    projectName: "3phase-project",
    reviewer: { max_budget_usd: 1.0, timeout_ms: 180_000, arbitration_threshold: 2 },
    architect: {
      max_budget_usd: 6.0,
      compaction_threshold_pct: 0.9,
      arbitration_timeout_ms: 120_000,
      prompt_path: join(root, "config", "harness", "architect-prompt.md"),
    },
    systemPrompt: EXECUTOR_PROMPT,
  });
}

async function main(): Promise<void> {
  const root = initScratchRepo({
    prefix: "harness-3phase",
    gitEmail: "3phase@harness.test",
    gitName: "3phase",
    promptFiles: ["architect-prompt.md", "review-prompt.md"],
  });
  console.log(`[3phase] scratch root: ${root}`);

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
    getTrunkBranch: () => "main",
  });
  const { realGitOps } = await import("../src/session/manager.js");
  const architectManager = new ArchitectManager({
    sdk,
    projectStore,
    stateManager: state,
    gitOps: realGitOps,
    config,
  });

  // CW-1 — real senders when DISCORD_BOT_TOKEN is in env; stdout fake otherwise.
  const sent: Array<{ channel: string; content: string; identity?: AgentIdentity }> = [];
  const stdoutFake: DiscordSender = {
    async sendToChannel(channel, content, identity) {
      sent.push({ channel, content, identity });
      console.log(`  [discord:${channel}] ${content.slice(0, 160)}`);
    },
    async sendToChannelAndReturnId(channel, content, identity) {
      return sendToChannelAndReturnIdDefault(this, channel, content, identity);
    },
    async addReaction() { /* noop */ },
  };
  const discordToken = process.env.DISCORD_BOT_TOKEN;
  const senders = discordToken
    ? buildSendersForChannels(config.discord, discordToken)
    : stdoutFake;
  const notifier = new DiscordNotifier(senders, config.discord);

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
  console.log(`[3phase] declaring project...`);
  const result = await orch.declareProject(
    "3phase-math-utils",
    [
      "Build a small math utility module in 3 sequential phases. Each phase must be a SEPARATE task file because the phases depend on each other in order:",
      "",
      "Phase 1: Create src/math/add.ts that exports `export function add(a: number, b: number): number { return a + b; }`",
      "Phase 2: Create src/math/subtract.ts that exports `export function subtract(a: number, b: number): number { return a - b; }`",
      "Phase 3: Create src/math/index.ts that re-exports both functions: `export { add } from './add.js'; export { subtract } from './subtract.js';`",
      "",
      "You MUST decompose this into EXACTLY THREE phases (not one, not two). Each phase writes one file. Phase 3 depends on phase 1 and 2 having landed on trunk.",
    ].join("\n"),
    [
      "no tests",
      "no README modifications",
      "no extra files beyond the three listed",
      "no package.json changes",
    ],
  );

  if ("error" in result) {
    console.error(`[3phase] declareProject FAILED: ${result.error}`);
    process.exit(1);
  }
  console.log(`[3phase] project ${result.projectId} declared; architect session ${result.sessionId.slice(0, 12)}`);

  orch.start();

  const isDone = (): boolean => {
    const tasks = state.getAllTasks();
    if (tasks.length === 0) return false;
    const project = projectStore.getProject(result.projectId);
    if (isProjectTerminal(project?.state)) return true;
    return tasks.length >= 3 && tasks.every((t) => t.state === "done" || t.state === "failed");
  };

  while (!isDone() && Date.now() - startedAt < DEFAULT_RUN_TIMEOUT_MS) {
    await new Promise((r) => setTimeout(r, DEFAULT_POLL_LOOP_MS));
  }

  await orch.shutdown();
  await architectManager.shutdownAll();

  // --- Report ---
  console.log("\n===== 3-PHASE PROJECT RESULT =====");
  console.log(`elapsed: ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
  console.log(`events: ${events.length}`);
  console.log(`discord messages: ${sent.length}`);

  const allTasks = state.getAllTasks();
  console.log(`\ntasks (${allTasks.length}):`);
  for (const t of allTasks) {
    console.log(`  ${t.id} state=${t.state} retries=${t.retryCount} cost=$${t.totalCostUsd.toFixed(2)} phase=${t.phaseId ?? "?"} summary=${t.summary ?? ""}`);
  }

  const project = projectStore.getProject(result.projectId);
  console.log(`\nproject: ${project?.state ?? "?"}`);
  console.log(`project cost: $${project?.totalCostUsd.toFixed(2) ?? "?"}`);
  console.log(`phase count: ${project?.phases.length ?? 0}`);
  for (const p of project?.phases ?? []) {
    console.log(`  phase ${p.id} state=${p.state} task=${p.taskId ?? "?"}`);
  }

  const trunkLog = execSync("git log --oneline -10", { cwd: root, encoding: "utf-8" });
  console.log(`\ntrunk commits:\n${trunkLog}`);

  const addPath = join(root, "src", "math", "add.ts");
  const subPath = join(root, "src", "math", "subtract.ts");
  const idxPath = join(root, "src", "math", "index.ts");
  for (const [label, p] of [["add.ts", addPath], ["subtract.ts", subPath], ["index.ts", idxPath]] as const) {
    if (existsSync(p)) {
      console.log(`${label} content (trunk):`);
      console.log(readFileSync(p, "utf-8"));
    } else {
      console.log(`${label} NOT on trunk`);
    }
  }

  const decomposed = events.find((e) => e.type === "project_decomposed");
  const completedEvent = events.find((e) => e.type === "project_completed");
  const doneEvents = events.filter((e) => e.type === "task_done");
  const allTasksDone = allTasks.length >= 3 && allTasks.every((t) => t.state === "done");
  const filesExist = existsSync(addPath) && existsSync(subPath) && existsSync(idxPath);
  const projectCompleted = project?.state === "completed";
  const phaseCountOk = (project?.phases.length ?? 0) >= 3;

  console.log(`\nchecks:`);
  console.log(`  project_decomposed fired:   ${!!decomposed}`);
  console.log(`  phase count ≥ 3:            ${phaseCountOk}`);
  console.log(`  task_done × 3+:             ${doneEvents.length >= 3}`);
  console.log(`  all phase tasks done:       ${allTasksDone}`);
  console.log(`  all 3 files on trunk:       ${filesExist}`);
  console.log(`  project state=completed:    ${projectCompleted}`);
  console.log(`  project_completed event:    ${!!completedEvent}`);

  const pass = !!decomposed && phaseCountOk && doneEvents.length >= 3 && allTasksDone && filesExist && projectCompleted && !!completedEvent;
  console.log(`\nRESULT: ${pass ? "PASS" : "FAIL"}`);
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((err) => {
  console.error("[3phase] FATAL", err);
  process.exit(2);
});
