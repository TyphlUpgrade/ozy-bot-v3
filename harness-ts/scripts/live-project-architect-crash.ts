/**
 * Architect crash-recovery live test.
 *
 * Validates `checkArchitectHealth` end-to-end: simulates an Architect that
 * died mid-decomposition by spawning + shutting it down before phase files
 * are written, then asserts the orchestrator's poll loop detects the dead
 * session and respawns with `reason=crash_recovery`. The respawned architect
 * (driven by `buildRecoveryPrompt`) completes decomposition, the orchestrator
 * ingests the phases, and a 1-phase project runs to completion.
 *
 * Real SDK: Architect (both spawn + respawn). Reviewer is a stub that
 * approves on first call so the test stays deterministic and cheap.
 *
 * Usage: npx tsx scripts/live-project-architect-crash.ts
 * Budget: ~$0.50.
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
import { ArchitectManager } from "../src/session/architect.js";
import type { ReviewGate } from "../src/gates/review.js";
import type { HarnessConfig } from "../src/lib/config.js";
import {
  initScratchRepo,
  buildBaseConfig,
  installSigintHandler,
  isProjectTerminal,
  DEFAULT_POLL_LOOP_MS,
  DEFAULT_RUN_TIMEOUT_MS,
} from "./lib/scratch-repo.js";
import { StubReviewGate } from "./lib/stub-review-gate.js";

function buildConfig(root: string): HarnessConfig {
  return buildBaseConfig({
    root,
    projectName: "architect-crash-demo",
    pipelineOverrides: { retry_delay_ms: 3_000 },
    reviewer: { max_budget_usd: 1.0, timeout_ms: 180_000, arbitration_threshold: 1 },
    architect: {
      max_budget_usd: 4.0,
      compaction_threshold_pct: 0.9,
      arbitration_timeout_ms: 180_000,
      prompt_path: join(root, "config", "harness", "architect-prompt.md"),
    },
  });
}

async function main(): Promise<void> {
  const root = initScratchRepo({
    prefix: "harness-architect-crash",
    gitEmail: "crash@harness.test",
    gitName: "crash",
    promptFiles: ["architect-prompt.md", "review-prompt.md"],
  });
  console.log(`[crash] scratch root: ${root}`);

  const config = buildConfig(root);
  const sdk = new SDKClient(query);
  const state = new StateManager(config.project.state_file);
  const projectStore = new ProjectStore(
    join(root, "projects.json"),
    config.project.worktree_base,
  );
  const sessions = new SessionManager(sdk, state, config);
  const mergeGate = new MergeGate(config.pipeline, root);
  const reviewGate = new StubReviewGate({
    queue: ["approve"],
    arbitrationThreshold: 1,
  });
  const { realGitOps } = await import("../src/session/manager.js");
  const architectManager = new ArchitectManager({
    sdk,
    projectStore,
    stateManager: state,
    gitOps: realGitOps,
    config,
  });

  const orch = new Orchestrator({
    sessionManager: sessions,
    mergeGate,
    stateManager: state,
    config,
    reviewGate: reviewGate as unknown as ReviewGate,
    architectManager,
    projectStore,
  });

  installSigintHandler([
    { shutdown: () => orch.shutdown() },
    { shutdown: () => architectManager.shutdownAll() },
  ]);

  const events: OrchestratorEvent[] = [];
  orch.on((ev: OrchestratorEvent) => {
    events.push(ev);
    const t = new Date().toISOString().slice(11, 23);
    console.log(`[${t}] ${ev.type} ${JSON.stringify(ev).slice(0, 200)}`);
  });

  // --- Crash simulation ---
  // Step 1: createProject puts the project into `decomposing` state. Step 2:
  // spawn the Architect session. Step 3: kill it before decomposition runs.
  // This is faithful to "process died after spawn, before decompose returned"
  // — disk state shows project=decomposing with an aborted Architect session.
  console.log(`[crash] phase 1 — pre-crash setup`);
  const startedAt = Date.now();

  const project = projectStore.createProject(
    "architect-crash-demo",
    [
      "Create EXACTLY ONE file at `src/marker.ts` containing the single line:",
      "",
      "`export const MARKER = 'crash-recovered';`",
      "",
      "Nothing else. One phase is sufficient.",
    ].join("\n"),
    ["no tests", "no README modifications", "no extra files beyond src/marker.ts"],
  );
  console.log(`[crash] project ${project.id} created (state=${project.state})`);

  const initialSpawn = await architectManager.spawn(
    project.id,
    project.name,
    project.description,
    project.nonGoals,
  );
  if (initialSpawn.status !== "success" || !initialSpawn.sessionId) {
    console.error(`[crash] initial spawn failed: ${initialSpawn.error}`);
    process.exit(1);
  }
  console.log(`[crash] architect spawned: ${initialSpawn.sessionId}`);

  // Manually emit architect_spawned so the test report sees it (declareProject
  // would've emitted this, but we bypassed declareProject).
  events.push({
    type: "architect_spawned",
    projectId: project.id,
    sessionId: initialSpawn.sessionId,
  });

  await architectManager.shutdown(project.id);
  console.log(`[crash] architect shutdown — simulated mid-decomposition crash`);
  console.log(`[crash] project state after crash: ${projectStore.getProject(project.id)?.state}`);

  // --- Recovery via orchestrator poll loop ---
  console.log(`[crash] phase 2 — start orchestrator (checkArchitectHealth should respawn)`);
  orch.start();

  const isDone = (): boolean =>
    isProjectTerminal(projectStore.getProject(project.id)?.state);

  while (!isDone() && Date.now() - startedAt < DEFAULT_RUN_TIMEOUT_MS) {
    await new Promise((r) => setTimeout(r, DEFAULT_POLL_LOOP_MS));
  }

  await orch.shutdown();
  await architectManager.shutdownAll();

  // --- Report ---
  console.log("\n===== ARCHITECT CRASH RECOVERY RESULT =====");
  console.log(`elapsed: ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
  console.log(`events: ${events.length}`);

  const tasks = state.getAllTasks();
  console.log(`\ntasks (${tasks.length}):`);
  for (const t of tasks) {
    console.log(
      `  ${t.id} state=${t.state} retries=${t.retryCount} cost=$${t.totalCostUsd.toFixed(3)}`,
    );
  }

  const finalProject = projectStore.getProject(project.id);
  console.log(`\nproject state: ${finalProject?.state}`);
  console.log(`project cost: $${finalProject?.totalCostUsd.toFixed(3)}`);

  const trunkLog = execSync("git log --oneline -10", { cwd: root, encoding: "utf-8" });
  console.log(`\ntrunk commits:\n${trunkLog}`);

  const markerPath = join(root, "src", "marker.ts");
  if (existsSync(markerPath)) {
    console.log(`marker.ts on trunk:\n${readFileSync(markerPath, "utf-8")}`);
  } else {
    console.log(`marker.ts NOT on trunk`);
  }

  // PASS criteria: architect_respawned event with reason=crash_recovery
  // must fire, project must complete, marker file must land on trunk.
  const architectSpawned = events.some((e) => e.type === "architect_spawned");
  const respawned = events.find(
    (e) => e.type === "architect_respawned" && e.reason === "crash_recovery",
  );
  const decomposed = events.some((e) => e.type === "project_decomposed");
  const taskDoneCount = events.filter((e) => e.type === "task_done").length;
  const projectCompleted = events.some((e) => e.type === "project_completed");
  const markerOk = existsSync(markerPath);

  console.log(`\nchecks:`);
  console.log(`  architect_spawned (initial):           ${architectSpawned}`);
  console.log(`  architect_respawned crash_recovery:    ${!!respawned}`);
  console.log(`  project_decomposed:                    ${decomposed}`);
  console.log(`  task_done ≥ 1:                         ${taskDoneCount >= 1} (actual: ${taskDoneCount})`);
  console.log(`  project_completed event:               ${projectCompleted}`);
  console.log(`  marker.ts on trunk:                    ${markerOk}`);

  const pass =
    architectSpawned &&
    !!respawned &&
    decomposed &&
    taskDoneCount >= 1 &&
    projectCompleted &&
    markerOk;

  console.log(`\nRESULT: ${pass ? "PASS" : "FAIL"}`);
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((err) => {
  console.error("[crash] FATAL", err);
  process.exit(2);
});
