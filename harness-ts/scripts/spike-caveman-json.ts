/**
 * Spike 4 — Caveman × structured JSON on Executor.
 *
 * Risk (per plan M.11.5): caveman mode drops articles / filler. Applied to an
 * Executor that must emit a strict 8-field completion.json + 5-field
 * confidence sub-block, it could silently skip fields the model views as
 * filler. One clean live run is not enough signal; this runs 5 independent
 * Executor phases against the U3 enriched completion contract with caveman
 * enabled (production default) and measures top-level + confidence-subfield
 * preservation.
 *
 * Threshold for Wave C design lock: ≥ 95% field preservation across
 * (5 runs) × (8 top-level + 5 confidence subfields) = 65 field-slots.
 * Below 95% → drop caveman from Executor defaults.
 *
 * Budget: $0.25 per run × 5 = $1.25 cap. Actual usage expected lower based
 * on prior live runs (~$0.10 per phase).
 *
 * Usage: npx tsx scripts/spike-caveman-json.ts
 */

import { mkdirSync, writeFileSync, existsSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { SDKClient } from "../src/session/sdk.js";
import { SessionManager } from "../src/session/manager.js";
import { StateManager } from "../src/lib/state.js";
import type { HarnessConfig } from "../src/lib/config.js";

const __filename = fileURLToPath(import.meta.url);
const HARNESS_ROOT = dirname(dirname(__filename));

const TOP_FIELDS = [
  "status",
  "commitSha",
  "summary",
  "filesChanged",
  "understanding",
  "assumptions",
  "nonGoals",
  "confidence",
] as const;

const CONF_SUBFIELDS = [
  "scopeClarity",
  "designCertainty",
  "testCoverage",
  "assumptions",
  "openQuestions",
] as const;

// Distinct trivial tasks to defeat prompt caching across runs.
const TASKS: Array<{ id: string; filename: string; contents: string }> = [
  { id: "run1", filename: "alpha.ts",  contents: "export const ALPHA = 'a';\n" },
  { id: "run2", filename: "beta.ts",   contents: "export const BETA = 'b';\n" },
  { id: "run3", filename: "gamma.ts",  contents: "export const GAMMA = 'g';\n" },
  { id: "run4", filename: "delta.ts",  contents: "export const DELTA = 'd';\n" },
  { id: "run5", filename: "epsilon.ts", contents: "export const EPSILON = 'e';\n" },
];

function initScratchRepo(): string {
  const root = join(tmpdir(), `harness-spike4-${Date.now()}`);
  mkdirSync(root, { recursive: true });
  mkdirSync(join(root, "tasks"), { recursive: true });
  mkdirSync(join(root, "worktrees"), { recursive: true });
  mkdirSync(join(root, "sessions"), { recursive: true });
  execSync("git init -b main", { cwd: root, stdio: "ignore" });
  execSync("git config user.email spike4@harness.test", { cwd: root, stdio: "ignore" });
  execSync("git config user.name spike4", { cwd: root, stdio: "ignore" });
  writeFileSync(join(root, "README.md"), "# scratch spike4\n");
  writeFileSync(join(root, ".gitignore"), "tasks/\nworktrees/\nsessions/\nstate.json\nstate.log.jsonl\n");
  execSync("git add -A && git commit -m init", { cwd: root, stdio: "ignore" });
  return root;
}

function buildConfig(root: string): HarnessConfig {
  return {
    project: {
      name: "spike4",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "worktrees"),
      session_dir: join(root, "sessions"),
    },
    pipeline: {
      poll_interval: 3,
      test_command: "true",
      max_retries: 1,
      test_timeout: 120,
      escalation_timeout: 600,
      retry_delay_ms: 5_000,
      max_session_retries: 1,
      max_budget_usd: 0.25,
      auto_escalate_on_max_retries: false,
      max_tier1_escalations: 1,
    },
    discord: {
      bot_token_env: "UNUSED",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {
        orchestrator: { name: "Harness", avatar_url: "" },
      },
    },
    // systemPrompt intentionally unset → SessionManager uses U3 default.
  };
}

interface RunResult {
  id: string;
  spawnedOk: boolean;
  completionWritten: boolean;
  topFieldsPresent: number;    // 0-8
  confFieldsPresent: number;   // 0-5
  costUsd: number;
  wallMs: number;
  completionRaw: unknown;
}

async function runOne(
  sessions: SessionManager,
  state: StateManager,
  task: { id: string; filename: string; contents: string },
  worktreeBase: string,
): Promise<RunResult> {
  const prompt = [
    `Create a file at path \`${task.filename}\` at the repo root with these exact contents:`,
    "",
    "```",
    task.contents,
    "```",
    "",
    "One file, one commit. Then write the required completion.json.",
  ].join("\n");

  const taskRec = state.createTask(prompt, `spike4-${task.id}`);
  const startMs = Date.now();
  let spawnedOk = false;
  try {
    const { result } = await sessions.spawnTask(taskRec);
    spawnedOk = result.success;
  } catch (e) {
    console.error(`[spike4:${task.id}] spawn threw:`, e);
  }
  const wallMs = Date.now() - startMs;

  const wtPath = join(worktreeBase, `task-${taskRec.id}`);
  const completionPath = join(wtPath, ".harness", "completion.json");
  let completionRaw: unknown = null;
  let completionWritten = false;
  let topFieldsPresent = 0;
  let confFieldsPresent = 0;

  if (existsSync(completionPath)) {
    completionWritten = true;
    try {
      completionRaw = JSON.parse(readFileSync(completionPath, "utf-8"));
      if (completionRaw && typeof completionRaw === "object") {
        const obj = completionRaw as Record<string, unknown>;
        for (const f of TOP_FIELDS) {
          if (obj[f] !== undefined && obj[f] !== null) topFieldsPresent += 1;
        }
        if (obj.confidence && typeof obj.confidence === "object") {
          const c = obj.confidence as Record<string, unknown>;
          for (const s of CONF_SUBFIELDS) {
            if (c[s] !== undefined && c[s] !== null) confFieldsPresent += 1;
          }
        }
      }
    } catch (e) {
      console.error(`[spike4:${task.id}] completion.json parse failed:`, e);
    }
  }

  const costUsd = state.getTask(taskRec.id)?.totalCostUsd ?? 0;

  return {
    id: task.id,
    spawnedOk,
    completionWritten,
    topFieldsPresent,
    confFieldsPresent,
    costUsd,
    wallMs,
    completionRaw,
  };
}

async function main(): Promise<void> {
  const root = initScratchRepo();
  console.log(`[spike4] scratch root: ${root}`);
  const config = buildConfig(root);
  const sdk = new SDKClient(query);
  const state = new StateManager(config.project.state_file);
  const sessions = new SessionManager(sdk, state, config);

  const results: RunResult[] = [];
  for (const task of TASKS) {
    console.log(`[spike4] launching ${task.id} — ${task.filename}`);
    const r = await runOne(sessions, state, task, config.project.worktree_base);
    console.log(
      `[spike4] ${r.id} done: spawn=${r.spawnedOk} completion=${r.completionWritten} ` +
        `top=${r.topFieldsPresent}/${TOP_FIELDS.length} conf=${r.confFieldsPresent}/${CONF_SUBFIELDS.length} ` +
        `cost=$${r.costUsd.toFixed(3)} wall=${r.wallMs}ms`,
    );
    results.push(r);
  }

  await sessions.abortAll();

  // --- Report ---
  console.log("\n===== SPIKE 4 — CAVEMAN × JSON RESULT =====");
  const totalTopSlots = TOP_FIELDS.length * results.length;
  const totalConfSlots = CONF_SUBFIELDS.length * results.length;
  const topPresent = results.reduce((n, r) => n + r.topFieldsPresent, 0);
  const confPresent = results.reduce((n, r) => n + r.confFieldsPresent, 0);
  const totalSlots = totalTopSlots + totalConfSlots;
  const totalPresent = topPresent + confPresent;
  const preservationPct = (totalPresent / totalSlots) * 100;
  const totalCost = results.reduce((n, r) => n + r.costUsd, 0);

  console.log(`runs: ${results.length}`);
  console.log(`top-field preservation: ${topPresent}/${totalTopSlots} (${((topPresent / totalTopSlots) * 100).toFixed(1)}%)`);
  console.log(`conf-subfield preservation: ${confPresent}/${totalConfSlots} (${((confPresent / totalConfSlots) * 100).toFixed(1)}%)`);
  console.log(`overall preservation: ${totalPresent}/${totalSlots} (${preservationPct.toFixed(1)}%)`);
  console.log(`total cost: $${totalCost.toFixed(3)}`);

  // Per-run field-by-field breakdown
  console.log(`\nper-run breakdown:`);
  for (const r of results) {
    const obj = (r.completionRaw ?? {}) as Record<string, unknown>;
    const missingTop = TOP_FIELDS.filter((f) => obj[f] === undefined || obj[f] === null);
    const missingConf = obj.confidence && typeof obj.confidence === "object"
      ? CONF_SUBFIELDS.filter((s) => (obj.confidence as Record<string, unknown>)[s] === undefined)
      : [...CONF_SUBFIELDS];
    console.log(`  ${r.id}: missing top=${missingTop.join(",") || "-"} missing conf=${missingConf.join(",") || "-"}`);
  }

  const pass = preservationPct >= 95.0;
  console.log(`\nTHRESHOLD: ≥95% preservation`);
  console.log(`RESULT: ${pass ? "PASS" : "FAIL"} — ${pass ? "keep caveman in Executor defaults" : "DROP caveman from Executor defaults"}`);
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((err) => {
  console.error("[spike4] FATAL", err);
  process.exit(2);
});
