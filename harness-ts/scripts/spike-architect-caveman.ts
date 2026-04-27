/**
 * Spike 6 — Caveman × Architect compaction-summary verbatim contract.
 *
 * The Architect §9 compaction-summary contract states:
 *   "description and nonGoals must be the ORIGINAL operator text, verbatim.
 *    The orchestrator validates this — drift kills the project."
 *
 * Spike 4 (spike-caveman-json) showed the Executor under caveman drops "filler"
 * fields like commitSha. The same plugin loads on the Architect by default
 * (ARCHITECT_DEFAULTS.plugins["caveman@caveman"]: true). If caveman compresses
 * the operator description before the Architect emits the summary, the verbatim
 * contract breaks.
 *
 * Design: 3 runs. Each run declares a project with a deliberately article-heavy
 * + filler-rich operator description and two rich nonGoals. After spawn,
 * trigger `requestSummary` directly (bypasses the cost-threshold gate). Read
 * `.harness/architect-summary.json` from the architect worktree. Score:
 *   - 9 top-level fields present
 *   - description byte-equal to operator input
 *   - nonGoals byte-equal to operator input
 *
 * Threshold for keep-caveman-on-Architect: ALL 3 runs MUST have byte-equal
 * description AND byte-equal nonGoals (the contract is binary, not statistical).
 * If any run drifts → DROP caveman from Architect defaults.
 *
 * Budget: Architect uses claude-opus-4-7. Per-run cost ~$0.30-0.80. Cap
 * `maxBudgetUsd: 1.0` per run × 3 = $3 cap. Actual ~$1-1.50 expected based
 * on Architect spawn + 1 summary turn (no decompose phase to keep cheap).
 *
 * Usage: npx tsx scripts/spike-architect-caveman.ts
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { SDKClient } from "../src/session/sdk.js";
import { StateManager } from "../src/lib/state.js";
import { ProjectStore } from "../src/lib/project.js";
import { ArchitectManager } from "../src/session/architect.js";
import { realGitOps } from "../src/session/manager.js";
import { initScratchRepo } from "./lib/scratch-repo.js";
import type { HarnessConfig } from "../src/lib/config.js";

const TOP_FIELDS = [
  "projectId",
  "name",
  "description",
  "nonGoals",
  "priorVerdicts",
  "completedPhases",
  "currentPhaseContext",
  "compactedAt",
  "compactionGeneration",
] as const;

interface TrialSpec {
  id: string;
  name: string;
  description: string;
  nonGoals: string[];
}

// Filler-heavy operator text — articles, adjectives, redundant phrasing.
// If caveman compresses this on the Architect side before §9 emission, the
// summary will drift from byte-equal.
const TRIALS: TrialSpec[] = [
  {
    id: "alpha",
    name: "alpha-utility",
    description:
      "Build a small utility that reads a list of integers from stdin, computes the running sum across the entire input, and prints the final total to stdout. Use plain Node.js with no external dependencies.",
    nonGoals: [
      "Do not add command-line argument parsing — only stdin is in scope.",
      "Do not handle floating-point numbers; integers only.",
    ],
  },
  {
    id: "beta",
    name: "beta-converter",
    description:
      "Create a simple temperature converter that takes a Celsius value as a numeric argument and prints the equivalent Fahrenheit value to stdout, formatted to two decimal places.",
    nonGoals: [
      "Do not support reverse conversion (Fahrenheit to Celsius) in this scope.",
      "Do not include any rounding modes other than the default.",
    ],
  },
];

function buildConfig(root: string): HarnessConfig {
  return {
    project: {
      name: "spike6",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "worktrees"),
      session_dir: join(root, "sessions"),
    },
    architect: {
      prompt_path: join(root, "config", "harness", "architect-prompt.md"),
    },
    pipeline: {
      poll_interval: 3,
      test_command: "true",
      max_retries: 1,
      test_timeout: 120,
      escalation_timeout: 600,
      retry_delay_ms: 5_000,
      max_session_retries: 1,
      max_budget_usd: 1.0,
      auto_escalate_on_max_retries: false,
      max_tier1_escalations: 1,
    },
    discord: {
      bot_token_env: "UNUSED",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: { orchestrator: { name: "Harness", avatar_url: "" } },
    },
  };
}

interface RunResult {
  id: string;
  spawnedOk: boolean;
  summaryWritten: boolean;
  topFieldsPresent: number;
  descriptionVerbatim: boolean;
  nonGoalsVerbatim: boolean;
  costUsd: number;
  wallMs: number;
  raw: unknown;
}

async function runOne(
  architect: ArchitectManager,
  projectStore: ProjectStore,
  trial: TrialSpec,
): Promise<RunResult> {
  // Register project so architect.spawn() finds it.
  const project = projectStore.createProject(trial.name, trial.description, trial.nonGoals);
  const startMs = Date.now();
  let spawnedOk = false;
  try {
    const spawnResult = await architect.spawn(
      project.id,
      project.name,
      project.description,
      project.nonGoals,
    );
    spawnedOk = spawnResult.status === "success";
  } catch (e) {
    console.error(`[spike6:${trial.id}] spawn threw:`, e);
  }

  let raw: unknown = null;
  let summaryWritten = false;
  if (spawnedOk) {
    try {
      // Decompose first so Architect has phase context when emitting §9
      // (priorVerdicts / completedPhases / currentPhaseContext require it).
      await architect.decompose(project.id);
    } catch (e) {
      console.error(`[spike6:${trial.id}] decompose threw:`, e);
    }
    try {
      // Trigger §9 directly (bypasses cost-threshold gate via requestSummary).
      await architect.requestSummary(project.id);
    } catch (e) {
      console.error(`[spike6:${trial.id}] requestSummary threw:`, e);
    }
    const summaryPath = join(project.architectWorktreePath, ".harness", "architect-summary.json");
    if (existsSync(summaryPath)) {
      summaryWritten = true;
      try {
        raw = JSON.parse(readFileSync(summaryPath, "utf-8"));
      } catch (e) {
        console.error(`[spike6:${trial.id}] summary parse failed:`, e);
      }
    }
  }

  const wallMs = Date.now() - startMs;

  let topFieldsPresent = 0;
  let descriptionVerbatim = false;
  let nonGoalsVerbatim = false;
  if (raw && typeof raw === "object") {
    const obj = raw as Record<string, unknown>;
    for (const f of TOP_FIELDS) {
      if (obj[f] !== undefined && obj[f] !== null) topFieldsPresent += 1;
    }
    descriptionVerbatim = obj.description === trial.description;
    if (Array.isArray(obj.nonGoals) && obj.nonGoals.length === trial.nonGoals.length) {
      nonGoalsVerbatim = trial.nonGoals.every(
        (g, i) => (obj.nonGoals as unknown[])[i] === g,
      );
    }
  }

  const costUsd = projectStore.getProject(project.id)?.totalCostUsd ?? 0;

  return {
    id: trial.id,
    spawnedOk,
    summaryWritten,
    topFieldsPresent,
    descriptionVerbatim,
    nonGoalsVerbatim,
    costUsd,
    wallMs,
    raw,
  };
}

async function main(): Promise<void> {
  const root = initScratchRepo({
    prefix: "harness-spike6",
    gitEmail: "spike6@harness.test",
    gitName: "spike6",
    promptFiles: ["architect-prompt.md"],
  });
  console.log(`[spike6] scratch root: ${root}`);
  const config = buildConfig(root);
  const sdk = new SDKClient(query);
  const state = new StateManager(config.project.state_file);
  const projectStore = new ProjectStore(
    join(root, "project-store.json"),
    config.project.worktree_base,
  );
  const architect = new ArchitectManager({
    sdk,
    projectStore,
    stateManager: state,
    gitOps: realGitOps,
    config,
  });

  const results: RunResult[] = [];
  for (const trial of TRIALS) {
    console.log(`[spike6] launching ${trial.id} — ${trial.name}`);
    const r = await runOne(architect, projectStore, trial);
    console.log(
      `[spike6] ${r.id} done: spawn=${r.spawnedOk} summary=${r.summaryWritten} ` +
        `top=${r.topFieldsPresent}/${TOP_FIELDS.length} ` +
        `descVerbatim=${r.descriptionVerbatim} ngVerbatim=${r.nonGoalsVerbatim} ` +
        `cost=$${r.costUsd.toFixed(3)} wall=${r.wallMs}ms`,
    );
    results.push(r);
  }

  await architect.shutdownAll();

  // --- Report ---
  console.log("\n===== SPIKE 6 — CAVEMAN × ARCHITECT VERBATIM RESULT =====");
  const totalTopSlots = TOP_FIELDS.length * results.length;
  const topPresent = results.reduce((n, r) => n + r.topFieldsPresent, 0);
  const descPass = results.filter((r) => r.descriptionVerbatim).length;
  const ngPass = results.filter((r) => r.nonGoalsVerbatim).length;
  const totalCost = results.reduce((n, r) => n + r.costUsd, 0);

  console.log(`runs: ${results.length}`);
  console.log(
    `top-field presence: ${topPresent}/${totalTopSlots} (${((topPresent / totalTopSlots) * 100).toFixed(1)}%)`,
  );
  console.log(`description verbatim: ${descPass}/${results.length}`);
  console.log(`nonGoals verbatim: ${ngPass}/${results.length}`);
  console.log(`total cost: $${totalCost.toFixed(3)}`);

  console.log("\nper-run breakdown:");
  for (const r of results) {
    const obj = (r.raw ?? {}) as Record<string, unknown>;
    const missingTop = TOP_FIELDS.filter((f) => obj[f] === undefined || obj[f] === null);
    let descDiff = "-";
    if (!r.descriptionVerbatim && typeof obj.description === "string") {
      const trial = TRIALS.find((t) => t.id === r.id)!;
      descDiff = `expected="${trial.description.slice(0, 60)}..." got="${(obj.description as string).slice(0, 60)}..."`;
    }
    console.log(
      `  ${r.id}: missing top=${missingTop.join(",") || "-"} ` +
        `desc=${r.descriptionVerbatim ? "OK" : "DRIFT"} ng=${r.nonGoalsVerbatim ? "OK" : "DRIFT"} ` +
        (descDiff !== "-" ? `\n    desc-diff: ${descDiff}` : ""),
    );
  }

  // Verbatim contract is binary — any drift = FAIL.
  const pass = descPass === results.length && ngPass === results.length;
  console.log(`\nTHRESHOLD: ALL runs must have byte-equal description AND nonGoals (verbatim contract)`);
  console.log(
    `RESULT: ${pass ? "PASS" : "FAIL"} — ${pass ? "keep caveman in Architect defaults" : "DROP caveman from Architect defaults"}`,
  );
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((e) => {
  console.error("[spike6] fatal:", e);
  process.exit(2);
});
