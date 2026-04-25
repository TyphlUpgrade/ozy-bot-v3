/**
 * P3 — Mass-phase live stress test (7 phases).
 *
 * Builds on live-project-3phase.ts but forces ≥ 7 phases. Exposes:
 *   - state.json contention under parallel transitions
 *   - merge-gate FIFO under burst arrival
 *   - branch-name pileup (harness/task-project-{id}-phase-NN)
 *   - ProjectStore persist ordering vs hasActivePhases
 *   - Reviewer mandatory-per-project-phase × 7
 *
 * Budget: Architect $6 + Executor $1 × 7 + Reviewer $1 × 7. Realistic ≤ $5.
 *
 * Usage: npx tsx scripts/live-project-mass-phase.ts
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
     "filesChanged": ["path1"]
   }

Keep work tight: one file per phase. No tests, no README changes.
`;

const EXPECTED_FILES = [
  "src/math/add.ts",
  "src/math/sub.ts",
  "src/math/mul.ts",
  "src/math/div.ts",
  "src/math/mod.ts",
  "src/math/pow.ts",
  "src/math/abs.ts",
] as const;

function buildConfig(root: string): HarnessConfig {
  return buildBaseConfig({
    root,
    projectName: "mass-phase-stress",
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
    prefix: "harness-mass-phase",
    gitEmail: "mass@harness.test",
    gitName: "mass",
    promptFiles: ["architect-prompt.md", "review-prompt.md"],
  });
  console.log(`[mass-phase] scratch root: ${root}`);

  const config = buildConfig(root);
  const sdk = new SDKClient(query);
  const state = new StateManager(config.project.state_file);
  const projectStore = new ProjectStore(
    join(root, "projects.json"),
    config.project.worktree_base,
  );
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
      console.log(`  [discord:${channel}] ${content.slice(0, 140)}`);
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
    if (ev.type !== "poll_tick") {
      console.log(`[${t}] ${ev.type} ${JSON.stringify(ev).slice(0, 180)}`);
    }
  });
  orch.on((ev) => notifier.handleEvent(ev));

  installSigintHandler([
    { shutdown: () => orch.shutdown() },
    { shutdown: () => architectManager.shutdownAll() },
  ]);

  const startedAt = Date.now();
  console.log(`[mass-phase] declaring project...`);
  const result = await orch.declareProject(
    "mass-phase-stress",
    [
      "Build seven tiny arithmetic utility files. EACH file must be its OWN phase.",
      "Decompose this project into EXACTLY SEVEN phases (one per file):",
      "",
      "1. `src/math/add.ts`:      `export function add(a: number, b: number): number { return a + b; }`",
      "2. `src/math/sub.ts`:      `export function sub(a: number, b: number): number { return a - b; }`",
      "3. `src/math/mul.ts`:      `export function mul(a: number, b: number): number { return a * b; }`",
      "4. `src/math/div.ts`:      `export function div(a: number, b: number): number { return a / b; }`",
      "5. `src/math/mod.ts`:      `export function mod(a: number, b: number): number { return a % b; }`",
      "6. `src/math/pow.ts`:      `export function pow(a: number, b: number): number { return a ** b; }`",
      "7. `src/math/abs.ts`:      `export function abs(a: number): number { return Math.abs(a); }`",
      "",
      "Each phase creates exactly one file at the exact path and content shown.",
      "Phases are INDEPENDENT — no phase depends on another. Do NOT merge them.",
    ].join("\n"),
    [
      "no tests",
      "no README modifications",
      "no index file or barrel export",
      "no merging phases together",
    ],
  );

  if ("error" in result) {
    console.error(`[mass-phase] declareProject FAILED: ${result.error}`);
    process.exit(1);
  }
  console.log(`[mass-phase] project ${result.projectId} declared`);

  orch.start();

  while (
    !isProjectTerminal(projectStore.getProject(result.projectId)?.state)
    && Date.now() - startedAt < DEFAULT_RUN_TIMEOUT_MS
  ) {
    await new Promise((r) => setTimeout(r, DEFAULT_POLL_LOOP_MS));
  }

  await orch.shutdown();
  await architectManager.shutdownAll();

  // --- Report ---
  const elapsedSec = (Date.now() - startedAt) / 1000;
  console.log("\n===== P3 MASS-PHASE RESULT =====");
  console.log(`elapsed: ${elapsedSec.toFixed(1)}s`);
  console.log(`events: ${events.length} (non-poll_tick: ${events.filter((e) => e.type !== "poll_tick").length})`);
  console.log(`discord messages: ${sent.length}`);

  const tasks = state.getAllTasks();
  console.log(`\ntasks (${tasks.length}):`);
  for (const t of tasks) {
    console.log(
      `  ${t.id} state=${t.state} retries=${t.retryCount} cost=$${t.totalCostUsd.toFixed(3)} ` +
        `phase=${t.phaseId ?? "?"} summary=${(t.summary ?? "").slice(0, 40)}`,
    );
  }

  const project = projectStore.getProject(result.projectId);
  console.log(`\nproject state: ${project?.state}`);
  console.log(`project phases (${project?.phases.length}):`);
  for (const p of project?.phases ?? []) {
    console.log(`  ${p.id} state=${p.state} task=${p.taskId ?? "?"}`);
  }
  console.log(`project cost: $${project?.totalCostUsd.toFixed(3) ?? "?"}`);

  const trunkLog = execSync("git log --oneline -20", { cwd: root, encoding: "utf-8" });
  console.log(`\ntrunk commits:\n${trunkLog}`);

  const filesOnTrunk = EXPECTED_FILES.map((p) => ({ p, exists: existsSync(join(root, p)) }));
  console.log(`\nfiles on trunk:`);
  for (const { p, exists } of filesOnTrunk) {
    console.log(`  ${exists ? "✓" : "✗"} ${p}`);
  }

  // PASS criteria
  const architectSpawned = events.some((e) => e.type === "architect_spawned");
  const decomposed = events.find((e) => e.type === "project_decomposed");
  const phaseCount = decomposed && decomposed.type === "project_decomposed" ? decomposed.phaseCount : 0;
  const pickedUp = events.filter((e) => e.type === "task_picked_up").length;
  const sessionCompletes = events.filter(
    (e) => e.type === "session_complete" && e.type === "session_complete" && e.success,
  ).length;
  const taskFailed = events.filter((e) => e.type === "task_failed").length;
  const taskDone = events.filter((e) => e.type === "task_done").length;
  const projectCompleted = events.filter((e) => e.type === "project_completed").length;
  const projectTerminal = project?.state === "completed";
  const allFilesPresent = filesOnTrunk.every((f) => f.exists);
  const mergeCommits = (trunkLog.match(/harness: merge/g) ?? []).length;

  console.log(`\nchecks:`);
  console.log(`  1.  architect_spawned:                    ${architectSpawned}`);
  console.log(`  2.  project_decomposed phaseCount ≥ 7:    ${phaseCount >= 7} (actual: ${phaseCount})`);
  console.log(`  3.  task_picked_up ≥ 7:                   ${pickedUp >= 7} (actual: ${pickedUp})`);
  console.log(`  4.  session_complete success ≥ 7:         ${sessionCompletes >= 7} (actual: ${sessionCompletes})`);
  console.log(`  5.  task_failed == 0:                     ${taskFailed === 0} (actual: ${taskFailed})`);
  console.log(`  6.  task_done ≥ 7:                        ${taskDone >= 7} (actual: ${taskDone})`);
  console.log(`  7.  project_completed == 1:               ${projectCompleted === 1} (actual: ${projectCompleted})`);
  console.log(`  8.  project.state == completed:           ${projectTerminal}`);
  console.log(`  9.  all 7 files on trunk:                 ${allFilesPresent}`);
  console.log(`  10. ≥ 7 merge commits on trunk:           ${mergeCommits >= 7} (actual: ${mergeCommits})`);
  console.log(`  11. wall < 30 min:                        ${elapsedSec < 30 * 60}`);

  const pass =
    architectSpawned &&
    phaseCount >= 7 &&
    pickedUp >= 7 &&
    sessionCompletes >= 7 &&
    taskFailed === 0 &&
    taskDone >= 7 &&
    projectCompleted === 1 &&
    projectTerminal &&
    allFilesPresent &&
    mergeCommits >= 7 &&
    elapsedSec < 30 * 60;

  console.log(`\nRESULT: ${pass ? "PASS" : "FAIL"}`);
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((err) => {
  console.error("[mass-phase] FATAL", err);
  process.exit(2);
});
