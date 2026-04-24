/**
 * Live pipeline run — boots orchestrator in scratch /tmp repo, drops one task,
 * observes events end-to-end. Uses REAL SDK + REAL git. Costs API money.
 *
 * Usage: npx tsx scripts/live-run.ts
 *
 * Validates Wave 1 pre-requisites in live runtime:
 *  - OMC plugin loading (enabledPlugins via Options.settings)
 *  - Hook defense (options.hooks = {} blocks persistent-mode.cjs)
 *  - Cron/remote trigger block (default disallowedTools)
 *  - Tmux cleanup on worktree teardown
 *
 * And Phase 2A pipeline:
 *  - Task file ingest → session spawn → completion detect → merge queue → trunk
 */

import { writeFileSync, existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { Orchestrator, type OrchestratorEvent } from "../src/orchestrator.js";
import { SessionManager } from "../src/session/manager.js";
import { SDKClient } from "../src/session/sdk.js";
import { MergeGate } from "../src/gates/merge.js";
import { StateManager } from "../src/lib/state.js";
import type { HarnessConfig } from "../src/lib/config.js";
import { initScratchRepo } from "./lib/scratch-repo.js";

function buildConfig(root: string): HarnessConfig {
  return {
    project: {
      name: "live-run",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "worktrees"),
      session_dir: join(root, "sessions"),
    },
    pipeline: {
      poll_interval: 2,
      test_command: "true",
      max_retries: 1,
      test_timeout: 60,
      escalation_timeout: 300,
      retry_delay_ms: 1000,
      max_session_retries: 1,
      max_budget_usd: 2.0,
      auto_escalate_on_max_retries: false,
      max_tier1_escalations: 1,
      // plugins + disallowed_tools default to Wave 1 defaults (OMC + caveman, cron blocks)
    },
    discord: {
      bot_token_env: "UNUSED",
      dev_channel: "dev",
      ops_channel: "ops",
      escalation_channel: "esc",
      agents: {},
    },
    systemPrompt: SYSTEM_PROMPT,
  };
}

// Mode:
//   minimal  — required fields only
//   enriched — demands confidence/understanding/assumptions/nonGoals
//   vague    — deliberately ambiguous task; observe honest uncertainty + graduated response routing
const MODE = (process.argv.find((a) => a.startsWith("--mode="))?.slice("--mode=".length) ?? "minimal") as
  | "minimal"
  | "enriched"
  | "vague";

const SYSTEM_PROMPT_MINIMAL = `You are working inside a harness-managed git worktree.

When you finish your task, you MUST:
1. Commit your changes with a short message.
2. Create directory .harness/ if missing.
3. Write .harness/completion.json with exactly this JSON shape:
   {
     "status": "success" | "failure",
     "commitSha": "<full sha of your final commit>",
     "summary": "<one sentence>",
     "filesChanged": ["path1", "path2"]
   }

The completion file is how the orchestrator knows you are done. If you do not write it the task will be marked failed.
`;

const SYSTEM_PROMPT_ENRICHED = `You are working inside a harness-managed git worktree.

When you finish your task, you MUST:
1. Commit your changes with a short message.
2. Create directory .harness/ if missing.
3. Write .harness/completion.json with this JSON shape (all fields required):
   {
     "status": "success" | "failure",
     "commitSha": "<full sha of your final commit>",
     "summary": "<one sentence>",
     "filesChanged": ["path1", "path2"],
     "understanding": "<one-paragraph restatement of the task as you interpreted it>",
     "assumptions": ["<assumption 1>", "<assumption 2>"],
     "nonGoals": ["<thing you deliberately did not do 1>", "<thing 2>"],
     "confidence": {
       "scopeClarity": "clear" | "partial" | "unclear",
       "designCertainty": "obvious" | "alternatives_exist" | "guessing",
       "testCoverage": "verifiable" | "partial" | "untestable",
       "assumptions": [
         { "description": "<same as top-level assumption>", "impact": "high" | "low", "reversible": true | false }
       ],
       "openQuestions": ["<question the operator may need to answer>"]
     }
   }

All enrichment fields are required. Be honest about uncertainty — if the scope is not fully clear, say so; if you are guessing on design, say so. Do not fabricate certainty.

The completion file is how the orchestrator knows you are done. If you do not write it the task will be marked failed.
`;

const SYSTEM_PROMPT = MODE === "minimal" ? SYSTEM_PROMPT_MINIMAL : SYSTEM_PROMPT_ENRICHED;

// ---------- Task definition ----------

const TASK_PROMPT_MINIMAL = `Create a file named \`hello.ts\` at the repository root with these exact contents (no extra whitespace, no trailing newline changes):

\`\`\`
export const MESSAGE = 'hi';
\`\`\`

Then:
1. \`git add hello.ts\`
2. \`git commit -m "add hello module"\`
3. Get the commit SHA via \`git rev-parse HEAD\`
4. Write .harness/completion.json per the system prompt with:
   - status: "success"
   - commitSha: <the SHA>
   - summary: "Added hello.ts exporting MESSAGE constant"
   - filesChanged: ["hello.ts"]

Do not run tests. Do not create any other files. Do not modify README.md.`;

const TASK_PROMPT_ENRICHED = `Create a file named \`greet.ts\` at the repository root exporting a function \`greet(name: string): string\` that returns a greeting like "Hello, <name>!". Write one concise comment above the function explaining its purpose.

Then:
1. \`git add greet.ts\`
2. \`git commit -m "add greet module"\`
3. Get the commit SHA via \`git rev-parse HEAD\`
4. Write .harness/completion.json per the system prompt, populating every required field including the enrichment block.

Guidance for the enrichment block:
- "understanding": restate what you built and why
- "assumptions": call out any choices you made that the operator did not specify (e.g. exact greeting format, whether to handle empty input, TS strict mode)
- "nonGoals": things you intentionally did not do (e.g. no tests, no README update, no input validation)
- "confidence": honest self-assessment on scopeClarity / designCertainty / testCoverage; list at least one open question if anything is genuinely unclear

Do not run tests. Do not create any other files. Do not modify README.md.`;

const TASK_PROMPT_VAGUE = `Make the repo a bit better. Add something useful and commit it.

When you are done, write .harness/completion.json per the system prompt with the enrichment block filled in honestly. The operator did not specify what "better" means — reflect that uncertainty in scopeClarity / designCertainty and in openQuestions. Do not guess a specific feature and present it as certain.

Do not run tests. Do not create more than three files.`;

const TASK_PROMPT =
  MODE === "enriched" ? TASK_PROMPT_ENRICHED
  : MODE === "vague" ? TASK_PROMPT_VAGUE
  : TASK_PROMPT_MINIMAL;

// ---------- Runner ----------

async function main(): Promise<void> {
  const root = initScratchRepo({
    prefix: "harness-live",
    gitEmail: "live-run@harness.test",
    gitName: "live-run",
  });
  console.log(`[live-run] mode: ${MODE}`);
  console.log(`[live-run] scratch root: ${root}`);

  const config = buildConfig(root);
  const state = new StateManager(config.project.state_file);
  const sdk = new SDKClient(query);
  const sessions = new SessionManager(sdk, state, config);
  const mergeGate = new MergeGate(config.pipeline, root);
  const orch = new Orchestrator({
    sessionManager: sessions,
    mergeGate,
    stateManager: state,
    config,
  });

  const events: OrchestratorEvent[] = [];
  const terminalEvents = new Set(["task_done", "task_failed", "budget_exhausted"]);
  let done = false;
  let doneTaskId: string | undefined;

  orch.on((ev: OrchestratorEvent) => {
    events.push(ev);
    const t = new Date().toISOString().slice(11, 23);
    const summary = JSON.stringify(ev).slice(0, 180);
    console.log(`[${t}] ${ev.type} ${summary}`);
    // Snapshot completion.json before merge/cleanup destroys the worktree
    if (ev.type === "session_complete" && "taskId" in ev) {
      const task = state.getTask(ev.taskId);
      if (task?.worktreePath) {
        const p = join(task.worktreePath, ".harness", "completion.json");
        if (existsSync(p)) {
          console.log(`\n[completion-signal]\n${readFileSync(p, "utf-8")}\n`);
        } else {
          console.log(`[completion-signal] (missing at ${p})`);
        }
      }
    }
    if (terminalEvents.has(ev.type)) {
      done = true;
      if ("taskId" in ev) doneTaskId = ev.taskId;
    }
  });

  // Drop task file BEFORE start so the first poll picks it up
  const taskId = `live-${Date.now().toString(36)}`;
  writeFileSync(
    join(config.project.task_dir, `${taskId}.json`),
    JSON.stringify({ id: taskId, prompt: TASK_PROMPT, priority: 1 }, null, 2),
  );
  console.log(`[live-run] dropped task ${taskId}`);

  orch.start();

  const startedAt = Date.now();
  const timeoutMs = 10 * 60 * 1000;
  while (!done && Date.now() - startedAt < timeoutMs) {
    await new Promise((r) => setTimeout(r, 1000));
  }

  if (!done) {
    console.error(`[live-run] TIMEOUT after ${timeoutMs}ms`);
  }

  await orch.shutdown();

  // Summary
  console.log("\n===== SUMMARY =====");
  console.log(`events: ${events.length}`);
  console.log(`terminal: ${done ? "yes" : "TIMEOUT"}`);
  if (doneTaskId) {
    const task = state.getTask(doneTaskId);
    console.log(`final state: ${task?.state ?? "unknown"}`);
    console.log(`retries: ${task?.retryCount ?? 0}`);
  }

  const trunkLog = execSync("git log --oneline -5", { cwd: root, encoding: "utf-8" });
  console.log(`trunk commits:\n${trunkLog}`);

  if (doneTaskId) {
    const task = state.getTask(doneTaskId);
    const files = task?.filesChanged ?? [];
    if (files.length === 0) {
      console.log("task reported no filesChanged");
    } else {
      for (const f of files) {
        const p = join(root, f);
        if (existsSync(p)) {
          console.log(`${f} on trunk:\n${readFileSync(p, "utf-8")}`);
        } else {
          console.log(`${f} NOT merged to trunk`);
        }
      }
    }
  }

  console.log(`\nscratch preserved at: ${root}`);
  process.exit(done ? 0 : 1);
}

main().catch((err) => {
  console.error("[live-run] FATAL", err);
  process.exit(2);
});
