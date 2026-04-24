/**
 * Concurrent-session smoke test — Wave 1.75 item 9.
 *
 * Boots a single orchestrator in a scratch /tmp repo, drops TWO task files
 * simultaneously, verifies both sessions run in parallel (separate worktrees,
 * unique SDK session IDs, no state corruption, both merges succeed via the
 * FIFO merge gate).
 *
 * Usage: npx tsx scripts/live-concurrent.ts
 *
 * Validates the three Wave 1.75 item 9 checks:
 *   - no session-ID collision (two distinct SDKMessage session_ids)
 *   - no tmux collision (no /team spawn in these tasks, trivially passes; worth
 *     re-running with a /team-invoking task once Wave 2+ has one)
 *   - no state-write contention (final state.json contains BOTH tasks in "done")
 */

import { mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { execSync } from "node:child_process";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { Orchestrator, type OrchestratorEvent } from "../src/orchestrator.js";
import { SessionManager } from "../src/session/manager.js";
import { SDKClient } from "../src/session/sdk.js";
import { MergeGate } from "../src/gates/merge.js";
import { StateManager } from "../src/lib/state.js";
import type { HarnessConfig } from "../src/lib/config.js";

const SYSTEM_PROMPT = `You are working inside a harness-managed git worktree.

When you finish your task, commit your change and write .harness/completion.json:
{
  "status": "success",
  "commitSha": "<full sha>",
  "summary": "<one sentence>",
  "filesChanged": ["<paths>"]
}
`;

function initScratchRepo(): string {
  const root = join(tmpdir(), `harness-concurrent-${Date.now()}`);
  mkdirSync(root, { recursive: true });
  mkdirSync(join(root, "tasks"), { recursive: true });
  mkdirSync(join(root, "worktrees"), { recursive: true });
  mkdirSync(join(root, "sessions"), { recursive: true });
  execSync("git init -b main", { cwd: root, stdio: "ignore" });
  execSync("git config user.email concurrent@harness.test", { cwd: root, stdio: "ignore" });
  execSync("git config user.name concurrent", { cwd: root, stdio: "ignore" });
  writeFileSync(join(root, "README.md"), "# scratch\n");
  execSync("git add -A && git commit -m init", { cwd: root, stdio: "ignore" });
  return root;
}

function buildConfig(root: string): HarnessConfig {
  return {
    project: {
      name: "concurrent-run",
      root,
      task_dir: join(root, "tasks"),
      state_file: join(root, "state.json"),
      worktree_base: join(root, "worktrees"),
      session_dir: join(root, "sessions"),
    },
    pipeline: {
      poll_interval: 1,
      test_command: "true",
      max_retries: 1,
      test_timeout: 60,
      escalation_timeout: 300,
      retry_delay_ms: 1000,
      max_session_retries: 1,
      max_budget_usd: 1.5,
      auto_escalate_on_max_retries: false,
      max_tier1_escalations: 1,
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

const TASK_A_PROMPT = `Create file \`alpha.ts\` at repo root with contents:
\`\`\`
export const ALPHA = 1;
\`\`\`
Commit with message "add alpha". Write .harness/completion.json per system prompt.
filesChanged: ["alpha.ts"], summary: "Added alpha module".`;

const TASK_B_PROMPT = `Create file \`beta.ts\` at repo root with contents:
\`\`\`
export const BETA = 2;
\`\`\`
Commit with message "add beta". Write .harness/completion.json per system prompt.
filesChanged: ["beta.ts"], summary: "Added beta module".`;

async function main(): Promise<void> {
  const root = initScratchRepo();
  console.log(`[concurrent] scratch root: ${root}`);

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

  const taskIds = new Set<string>();
  const sessionIds: Record<string, string> = {};
  const doneIds = new Set<string>();
  const failedIds = new Set<string>();

  orch.on((ev: OrchestratorEvent) => {
    const t = new Date().toISOString().slice(11, 23);
    console.log(`[${t}] ${ev.type} ${JSON.stringify(ev).slice(0, 160)}`);
    if ("taskId" in ev) {
      if (ev.type === "task_picked_up") taskIds.add(ev.taskId);
      if (ev.type === "task_done") doneIds.add(ev.taskId);
      if (ev.type === "task_failed") failedIds.add(ev.taskId);
      if (ev.type === "session_complete") {
        const task = state.getTask(ev.taskId);
        if (task?.sessionId) sessionIds[ev.taskId] = task.sessionId;
      }
    }
  });

  // Drop both task files BEFORE start → first poll picks both up, fires both
  // processTask calls concurrently (fire-and-forget), both run in separate worktrees.
  const idA = `alpha-${Date.now().toString(36)}`;
  const idB = `beta-${Date.now().toString(36)}`;
  writeFileSync(
    join(config.project.task_dir, `${idA}.json`),
    JSON.stringify({ id: idA, prompt: TASK_A_PROMPT }, null, 2),
  );
  writeFileSync(
    join(config.project.task_dir, `${idB}.json`),
    JSON.stringify({ id: idB, prompt: TASK_B_PROMPT }, null, 2),
  );
  console.log(`[concurrent] dropped ${idA} + ${idB}`);

  orch.start();

  const startedAt = Date.now();
  const timeoutMs = 10 * 60 * 1000;
  while (doneIds.size + failedIds.size < 2 && Date.now() - startedAt < timeoutMs) {
    await new Promise((r) => setTimeout(r, 500));
  }

  await orch.shutdown();

  // --- Assertions ---
  console.log("\n===== CONCURRENT SMOKE RESULT =====");
  console.log(`tasks picked up: ${[...taskIds].join(", ")}`);
  console.log(`done: ${[...doneIds].join(", ") || "(none)"}`);
  console.log(`failed: ${[...failedIds].join(", ") || "(none)"}`);
  console.log(`session ids: ${JSON.stringify(sessionIds)}`);

  const bothDone = doneIds.has(idA) && doneIds.has(idB);
  const distinctSessions =
    sessionIds[idA] && sessionIds[idB] && sessionIds[idA] !== sessionIds[idB];

  // Verify trunk has both commits + both files
  const trunkLog = execSync("git log --oneline", { cwd: root, encoding: "utf-8" });
  const hasAlpha = /add alpha/.test(trunkLog);
  const hasBeta = /add beta/.test(trunkLog);
  const alphaOnTrunk = /export const ALPHA/.test(readFileSync(join(root, "alpha.ts"), "utf-8"));
  const betaOnTrunk = /export const BETA/.test(readFileSync(join(root, "beta.ts"), "utf-8"));

  // Verify state.json has both in "done"
  const finalState = JSON.parse(readFileSync(config.project.state_file, "utf-8"));
  const stateA = finalState.tasks?.[idA]?.state;
  const stateB = finalState.tasks?.[idB]?.state;
  const bothStatePersisted = stateA === "done" && stateB === "done";

  console.log("\nchecks:");
  console.log(`  both tasks done:              ${bothDone}`);
  console.log(`  distinct session ids:         ${distinctSessions}`);
  console.log(`  alpha commit on trunk:        ${hasAlpha}`);
  console.log(`  beta commit on trunk:         ${hasBeta}`);
  console.log(`  alpha.ts content correct:     ${alphaOnTrunk}`);
  console.log(`  beta.ts content correct:      ${betaOnTrunk}`);
  console.log(`  state.json persisted both:    ${bothStatePersisted} (A=${stateA}, B=${stateB})`);

  const pass = bothDone && distinctSessions && hasAlpha && hasBeta && alphaOnTrunk && betaOnTrunk && bothStatePersisted;
  console.log(`\nRESULT: ${pass ? "PASS" : "FAIL"}`);
  console.log(`scratch preserved at: ${root}`);
  process.exit(pass ? 0 : 1);
}

main().catch((err) => {
  console.error("[concurrent] FATAL", err);
  process.exit(2);
});
