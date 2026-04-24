/**
 * Spike 5 — OMC plugin dead-weight on Executor.
 *
 * Per plan M.13.3, single-mode Executors don't invoke OMC specialists
 * unprompted. The OMC plugin adds init overhead on spawn. Does disabling
 * OMC on the Executor materially reduce wall-clock spawn time without
 * regressing completion compliance?
 *
 * Design: 3 runs with `oh-my-claudecode@omc: true` (production default) and
 * 3 runs with `oh-my-claudecode@omc: false`, same U3 enriched prompt, same
 * trivial tasks (one file per run). Caveman stays enabled throughout to
 * isolate the OMC variable. Measure wall-clock spawn → completion.json
 * write + per-run cost + field preservation. Compare medians.
 *
 * Threshold for Wave C design lock: median wall-clock reduction ≥ 20%
 * with zero drop in field preservation when OMC disabled → flip the
 * DEFAULT_PLUGINS entry for Executor.
 *
 * Budget: $0.25 per run × 6 = $1.50 cap (below backlog $0.50 target because
 * Executor runs are Sonnet and observed ~$0.10 each in prior live runs;
 * the $0.50 cap in the backlog was optimistic).
 *
 * Usage: npx tsx scripts/spike-omc-overhead.ts
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
const _HARNESS_ROOT = dirname(dirname(__filename));

const TOP_FIELDS = ["status", "commitSha", "summary", "filesChanged", "understanding", "assumptions", "nonGoals", "confidence"] as const;
const CONF_SUBFIELDS = ["scopeClarity", "designCertainty", "testCoverage", "assumptions", "openQuestions"] as const;

interface TrialSpec {
  id: string;
  filename: string;
  contents: string;
  omcEnabled: boolean;
}

// Interleave on/off to spread any time-of-day or cache effects evenly.
const TRIALS: TrialSpec[] = [
  { id: "omc-on-1",  filename: "one.ts",   contents: "export const ONE = 1;\n",   omcEnabled: true  },
  { id: "omc-off-1", filename: "two.ts",   contents: "export const TWO = 2;\n",   omcEnabled: false },
  { id: "omc-on-2",  filename: "three.ts", contents: "export const THREE = 3;\n", omcEnabled: true  },
  { id: "omc-off-2", filename: "four.ts",  contents: "export const FOUR = 4;\n",  omcEnabled: false },
  { id: "omc-on-3",  filename: "five.ts",  contents: "export const FIVE = 5;\n",  omcEnabled: true  },
  { id: "omc-off-3", filename: "six.ts",   contents: "export const SIX = 6;\n",   omcEnabled: false },
];

function initScratchRepo(): string {
  const root = join(tmpdir(), `harness-spike5-${Date.now()}`);
  mkdirSync(root, { recursive: true });
  mkdirSync(join(root, "tasks"), { recursive: true });
  mkdirSync(join(root, "worktrees"), { recursive: true });
  mkdirSync(join(root, "sessions"), { recursive: true });
  execSync("git init -b main", { cwd: root, stdio: "ignore" });
  execSync("git config user.email spike5@harness.test", { cwd: root, stdio: "ignore" });
  execSync("git config user.name spike5", { cwd: root, stdio: "ignore" });
  writeFileSync(join(root, "README.md"), "# scratch spike5\n");
  writeFileSync(join(root, ".gitignore"), "tasks/\nworktrees/\nsessions/\nstate.json\nstate.log.jsonl\n");
  execSync("git add -A && git commit -m init", { cwd: root, stdio: "ignore" });
  return root;
}

function buildConfig(root: string, omcEnabled: boolean): HarnessConfig {
  return {
    project: {
      name: "spike5",
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
      // Override the DEFAULT_PLUGINS entry for this trial.
      plugins: {
        "oh-my-claudecode@omc": omcEnabled,
        "caveman@caveman": true, // keep constant
      },
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
  };
}

interface TrialResult {
  id: string;
  omcEnabled: boolean;
  spawnedOk: boolean;
  completionWritten: boolean;
  topFieldsPresent: number;
  confFieldsPresent: number;
  costUsd: number;
  wallMs: number;
}

async function runOne(trial: TrialSpec, rootBase: string, stateFile: string): Promise<TrialResult> {
  // Each trial uses an independent scratch repo + state file so plugin flag
  // swaps cleanly and nothing leaks between trials.
  const root = initScratchRepo();
  const config = buildConfig(root, trial.omcEnabled);
  const sdk = new SDKClient(query);
  const state = new StateManager(config.project.state_file);
  const sessions = new SessionManager(sdk, state, config);

  const prompt = [
    `Create a file at path \`${trial.filename}\` at the repo root with these exact contents:`,
    "",
    "```",
    trial.contents,
    "```",
    "",
    "One file, one commit, then write the required completion.json.",
  ].join("\n");

  const taskRec = state.createTask(prompt, `spike5-${trial.id}`);
  const startMs = Date.now();
  let spawnedOk = false;
  try {
    const { result } = await sessions.spawnTask(taskRec);
    spawnedOk = result.success;
  } catch (e) {
    console.error(`[spike5:${trial.id}] spawn threw:`, e);
  }
  const wallMs = Date.now() - startMs;

  const wtPath = join(config.project.worktree_base, `task-${taskRec.id}`);
  const completionPath = join(wtPath, ".harness", "completion.json");
  let topFieldsPresent = 0;
  let confFieldsPresent = 0;
  let completionWritten = false;
  if (existsSync(completionPath)) {
    completionWritten = true;
    try {
      const obj = JSON.parse(readFileSync(completionPath, "utf-8")) as Record<string, unknown>;
      for (const f of TOP_FIELDS) if (obj[f] !== undefined && obj[f] !== null) topFieldsPresent += 1;
      if (obj.confidence && typeof obj.confidence === "object") {
        const c = obj.confidence as Record<string, unknown>;
        for (const s of CONF_SUBFIELDS) if (c[s] !== undefined && c[s] !== null) confFieldsPresent += 1;
      }
    } catch (e) {
      console.error(`[spike5:${trial.id}] completion parse failed:`, e);
    }
  }

  const costUsd = state.getTask(taskRec.id)?.totalCostUsd ?? 0;
  await sessions.abortAll();

  return {
    id: trial.id,
    omcEnabled: trial.omcEnabled,
    spawnedOk,
    completionWritten,
    topFieldsPresent,
    confFieldsPresent,
    costUsd,
    wallMs,
  };
}

function median(nums: number[]): number {
  if (nums.length === 0) return 0;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

async function main(): Promise<void> {
  const results: TrialResult[] = [];
  for (const trial of TRIALS) {
    console.log(`[spike5] launching ${trial.id} omc=${trial.omcEnabled}`);
    const r = await runOne(trial, "", "");
    console.log(
      `[spike5] ${r.id} spawn=${r.spawnedOk} completion=${r.completionWritten} ` +
        `top=${r.topFieldsPresent}/${TOP_FIELDS.length} conf=${r.confFieldsPresent}/${CONF_SUBFIELDS.length} ` +
        `cost=$${r.costUsd.toFixed(3)} wall=${r.wallMs}ms`,
    );
    results.push(r);
  }

  console.log("\n===== SPIKE 5 — OMC OVERHEAD RESULT =====");
  const on = results.filter((r) => r.omcEnabled);
  const off = results.filter((r) => !r.omcEnabled);
  const onMed = median(on.map((r) => r.wallMs));
  const offMed = median(off.map((r) => r.wallMs));
  const reductionPct = onMed > 0 ? ((onMed - offMed) / onMed) * 100 : 0;
  const totalSlots = TOP_FIELDS.length + CONF_SUBFIELDS.length;
  const onPreservation = on.reduce((n, r) => n + r.topFieldsPresent + r.confFieldsPresent, 0) / (totalSlots * on.length) * 100;
  const offPreservation = off.reduce((n, r) => n + r.topFieldsPresent + r.confFieldsPresent, 0) / (totalSlots * off.length) * 100;
  const totalCost = results.reduce((n, r) => n + r.costUsd, 0);

  console.log(`OMC ON  — wall medians: ${onMed}ms  | preservation: ${onPreservation.toFixed(1)}%  | runs: ${on.length}`);
  console.log(`OMC OFF — wall medians: ${offMed}ms | preservation: ${offPreservation.toFixed(1)}% | runs: ${off.length}`);
  console.log(`wall reduction (OFF vs ON): ${reductionPct.toFixed(1)}%`);
  console.log(`total cost: $${totalCost.toFixed(3)}`);

  const regression = offPreservation < onPreservation;
  const meetsThreshold = reductionPct >= 20.0 && !regression;
  console.log(`\nTHRESHOLDS: wall reduction ≥20% AND preservation-off ≥ preservation-on`);
  console.log(`RESULT: ${meetsThreshold ? "PASS" : "FAIL"} — ${meetsThreshold ? "DROP OMC from Executor defaults" : "keep OMC in Executor defaults"}`);
  process.exit(meetsThreshold ? 0 : 1);
}

main().catch((err) => {
  console.error("[spike5] FATAL", err);
  process.exit(2);
});
