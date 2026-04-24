/**
 * P2 — Live arbitration flow test.
 *
 * Validates P1 end-to-end: reviewer rejection → arbitration fires → real
 * Architect writes a verdict → orchestrator parses + applies → Executor
 * retries → reviewer approves → merge → project completed.
 *
 * Reviewer is a stub (reject first call, approve second) so the test is
 * deterministic. Architect + Executor hit the real SDK.
 *
 * Usage: npx tsx scripts/live-project-arbitration.ts
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
    projectName: "arbitration-demo",
    pipelineOverrides: { retry_delay_ms: 3_000 },
    reviewer: { max_budget_usd: 1.0, timeout_ms: 180_000, arbitration_threshold: 1 },
    architect: {
      max_budget_usd: 6.0,
      compaction_threshold_pct: 0.9,
      arbitration_timeout_ms: 180_000,
      prompt_path: join(root, "config", "harness", "architect-prompt.md"),
    },
  });
}

// --- Runner ---

async function main(): Promise<void> {
  const root = initScratchRepo({
    prefix: "harness-arbitration",
    gitEmail: "arb@harness.test",
    gitName: "arb",
    promptFiles: ["architect-prompt.md", "review-prompt.md"],
  });
  console.log(`[arbitration] scratch root: ${root}`);

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
    queue: ["reject", "approve"],
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

  const startedAt = Date.now();
  console.log(`[arbitration] declaring project...`);
  const result = await orch.declareProject(
    "arbitration-demo",
    [
      "Create EXACTLY ONE file at `src/greet.ts`. The file MUST contain:",
      "",
      "1. The exact export statement: `export const GREETING = 'hello-v2';`",
      "2. A trailing single-line comment on the LAST line with EXACTLY this text: `// HELLO-V2`",
      "",
      "Both the export and the `// HELLO-V2` trailer are MANDATORY. The file must",
      "not contain anything else. One phase is sufficient.",
    ].join("\n"),
    ["no tests", "no README modifications", "no extra files beyond src/greet.ts"],
  );

  if ("error" in result) {
    console.error(`[arbitration] declareProject FAILED: ${result.error}`);
    process.exit(1);
  }
  console.log(`[arbitration] project ${result.projectId} declared`);

  orch.start();

  const isDone = (): boolean =>
    isProjectTerminal(projectStore.getProject(result.projectId)?.state);

  while (!isDone() && Date.now() - startedAt < DEFAULT_RUN_TIMEOUT_MS) {
    await new Promise((r) => setTimeout(r, DEFAULT_POLL_LOOP_MS));
  }

  await orch.shutdown();
  await architectManager.shutdownAll();

  // --- Report ---
  console.log("\n===== P2 ARBITRATION RESULT =====");
  console.log(`elapsed: ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
  console.log(`events: ${events.length}`);

  const tasks = state.getAllTasks();
  console.log(`\ntasks (${tasks.length}):`);
  for (const t of tasks) {
    console.log(
      `  ${t.id} state=${t.state} retries=${t.retryCount} cost=$${t.totalCostUsd.toFixed(3)} ` +
        `rejections=${t.reviewerRejectionCount ?? 0} lastDirective=${(t.lastDirective ?? "").slice(0, 60)}`,
    );
  }

  const project = projectStore.getProject(result.projectId);
  console.log(`\nproject state: ${project?.state}`);
  console.log(`project cost: $${project?.totalCostUsd.toFixed(3)}`);

  const trunkLog = execSync("git log --oneline -10", { cwd: root, encoding: "utf-8" });
  console.log(`\ntrunk commits:\n${trunkLog}`);

  const greetPath = join(root, "src", "greet.ts");
  if (existsSync(greetPath)) {
    console.log(`greet.ts on trunk:\n${readFileSync(greetPath, "utf-8")}`);
  } else {
    console.log(`greet.ts NOT on trunk`);
  }

  // PASS criteria. The script is configured so the stub Reviewer's concern
  // IS grounded in the task prompt, so escalate_operator here indicates a
  // regression (Architect should retry or amend). We accept retry_with_directive
  // OR plan_amendment but forbid escalate_operator.
  const architectSpawned = events.some((e) => e.type === "architect_spawned");
  const decomposed = events.some((e) => e.type === "project_decomposed");
  const arbEntered = events.filter((e) => e.type === "review_arbitration_entered").length;
  const arbFired = events.find((e) => e.type === "architect_arbitration_fired");
  const arbVerdict = events.find((e) => e.type === "arbitration_verdict");
  const verdictType = arbVerdict && arbVerdict.type === "arbitration_verdict" ? arbVerdict.verdict : null;
  const verdictOk = verdictType === "retry_with_directive" || verdictType === "plan_amendment";
  const sessionCompletes = events.filter((e) => e.type === "session_complete").length;
  const taskDoneCount = events.filter((e) => e.type === "task_done").length;
  const projectCompleted = events.some((e) => e.type === "project_completed");
  const greetOk = existsSync(greetPath);

  console.log(`\nchecks:`);
  console.log(`  architect_spawned:              ${architectSpawned}`);
  console.log(`  project_decomposed:             ${decomposed}`);
  console.log(`  review_arbitration_entered=1:   ${arbEntered === 1} (actual: ${arbEntered})`);
  console.log(`  architect_arbitration_fired:    ${!!arbFired} cause=${arbFired && arbFired.type === "architect_arbitration_fired" ? arbFired.cause : "-"}`);
  console.log(`  arbitration_verdict non-escalate: ${verdictOk} verdict=${verdictType ?? "-"}`);
  console.log(`  session_complete ≥ 2 (retry ran): ${sessionCompletes >= 2} (actual: ${sessionCompletes})`);
  console.log(`  task_done ≥ 1:                  ${taskDoneCount >= 1} (actual: ${taskDoneCount})`);
  console.log(`  project_completed event:        ${projectCompleted}`);
  console.log(`  greet.ts on trunk:              ${greetOk}`);

  const pass =
    architectSpawned &&
    decomposed &&
    arbEntered === 1 &&
    !!arbFired &&
    verdictOk &&
    sessionCompletes >= 2 &&
    taskDoneCount >= 1 &&
    projectCompleted &&
    greetOk;

  console.log(`\nRESULT: ${pass ? "PASS" : "FAIL"}`);
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((err) => {
  console.error("[arbitration] FATAL", err);
  process.exit(2);
});
