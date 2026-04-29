/**
 * End-to-end project run with operator-in-loop dialogue.
 *
 * Mirrors `live-project.ts` but uses a deliberately ambiguous project goal so
 * the Architect should fire `escalation_needed` (scope_unclear). Instead of
 * running the real Discord dispatcher, the script polls a file
 * `/tmp/operator-input-{taskId}.txt` for the operator's reply. When the file
 * appears, its contents are forwarded to `architectManager.relayOperatorInput`
 * and Architect resumes.
 *
 * Usage:
 *   npx tsx scripts/live-project-operator-loop.ts
 *
 *   # In another shell, when escalation prints:
 *   echo "Use TypeScript. Add a divide() helper that throws on zero." \
 *     > /tmp/operator-input-<taskId>.txt
 */

import { readFileSync, existsSync, unlinkSync } from "node:fs";
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
import { type DiscordSender, type AgentIdentity, sendToChannelAndReturnIdDefault } from "../src/discord/types.js";
import type { HarnessConfig } from "../src/lib/config.js";
import {
  initScratchRepo,
  buildBaseConfig,
  installSigintHandler,
  DEFAULT_POLL_LOOP_MS,
  DEFAULT_RUN_TIMEOUT_MS,
} from "./lib/scratch-repo.js";

const EXECUTOR_PROMPT = `You are an Executor in a harness-managed git worktree.

When you finish your task, you MUST:
1. Write your code changes into the worktree. DO NOT run \`git add\`. DO NOT run \`git commit\`. The orchestrator will stage and commit your work after the Reviewer approves it.
2. Create directory \`.harness/\` if missing.
3. Write \`.harness/completion.json\`:
   {
     "status": "success" | "failure",
     "summary": "<one sentence>",
     "filesChanged": ["path1"]
   }
`;

function buildConfig(root: string): HarnessConfig {
  return buildBaseConfig({
    root,
    projectName: "live-project-operator-loop",
    reviewer: { max_budget_usd: 1.0, timeout_ms: 180_000, arbitration_threshold: 2 },
    architect: {
      max_budget_usd: 4.0,
      compaction_threshold_pct: 0.90,
      arbitration_timeout_ms: 120_000,
      prompt_path: join(root, "config", "harness", "architect-prompt.md"),
    },
    systemPrompt: EXECUTOR_PROMPT,
  });
}

async function main(): Promise<void> {
  const root = initScratchRepo({
    prefix: "harness-live-proj-op",
    gitEmail: "live-proj-op@harness.test",
    gitName: "live-proj-op",
    promptFiles: ["architect-prompt.md", "review-prompt.md"],
  });
  console.log(`[op-loop] scratch root: ${root}`);

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

  const sent: Array<{ channel: string; content: string; identity?: AgentIdentity }> = [];
  const stdoutFake: DiscordSender = {
    async sendToChannel(channel, content, identity) {
      sent.push({ channel, content, identity });
      console.log(`  [discord:${channel}] ${content.slice(0, 200)}`);
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
  let escalationsHandled = 0;
  let pendingProjectId: string | null = null;

  // Operator-in-loop: when escalation_needed fires, write the question to
  // stdout and start polling the input file. When the file appears, forward
  // contents to relayOperatorInput and unlink.
  orch.on((ev: OrchestratorEvent) => {
    events.push(ev);
    const t = new Date().toISOString().slice(11, 23);
    console.log(`[${t}] ${ev.type} ${JSON.stringify(ev).slice(0, 200)}`);
  });
  orch.on((ev) => notifier.handleEvent(ev));
  orch.on(async (ev: OrchestratorEvent) => {
    if (ev.type !== "escalation_needed") return;
    if (!pendingProjectId) {
      console.warn(`[op-loop] escalation_needed received but no projectId pinned; skipping`);
      return;
    }
    const inputPath = `/tmp/operator-input-${ev.taskId}.txt`;
    console.log(`\n[op-loop] >>>>> OPERATOR INPUT REQUIRED <<<<<`);
    console.log(`[op-loop] task: ${ev.taskId}`);
    console.log(`[op-loop] type: ${ev.escalation.type}`);
    console.log(`[op-loop] question: ${ev.escalation.question}`);
    if (ev.escalation.options) {
      console.log(`[op-loop] options: ${ev.escalation.options.join(" | ")}`);
    }
    if (ev.escalation.context) {
      console.log(`[op-loop] context: ${ev.escalation.context}`);
    }
    console.log(`[op-loop] Reply by writing to: ${inputPath}`);
    console.log(`[op-loop] Polling for response (timeout 5 min)...\n`);

    const deadline = Date.now() + 5 * 60_000;
    while (Date.now() < deadline) {
      if (existsSync(inputPath)) {
        const reply = readFileSync(inputPath, "utf-8").trim();
        try { unlinkSync(inputPath); } catch { /* best-effort */ }
        console.log(`[op-loop] operator replied (${reply.length} chars): ${reply.slice(0, 120)}`);
        try {
          await architectManager.relayOperatorInput(pendingProjectId, reply);
          escalationsHandled += 1;
          console.log(`[op-loop] relayed to architect (handled=${escalationsHandled})`);
        } catch (err) {
          console.error(`[op-loop] relayOperatorInput threw: ${err instanceof Error ? err.message : err}`);
        }
        return;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
    console.error(`[op-loop] operator response timed out after 5 min`);
  });

  installSigintHandler([
    { shutdown: () => orch.shutdown() },
    { shutdown: () => architectManager.shutdownAll() },
  ]);

  const startedAt = Date.now();
  console.log(`[op-loop] declaring deliberately-vague project...`);
  // Vague-on-purpose: language unspecified, signature unspecified, error
  // handling unspecified. Architect should ask before decomposing.
  const result = await orch.declareProject(
    "vague-math",
    "Add a small math helper. It should support division. Make it good.",
    [],
  );

  if ("error" in result) {
    console.error(`[op-loop] declareProject FAILED: ${result.error}`);
    process.exit(1);
  }
  pendingProjectId = result.projectId;
  console.log(`[op-loop] project ${result.projectId} declared`);

  orch.start();

  const isDone = (): boolean => {
    // Wave R2 — phases ingest serially, so checking only state.getAllTasks()
    // returns true after phase 01 finishes (phases 02+ are still on disk and
    // missing from state). Anchor terminal-detection on the project's own
    // state machine: completed/failed/aborted are the only terminal values.
    const project = projectStore.getProject(result.projectId);
    if (!project) return false;
    return project.state === "completed" || project.state === "failed" || project.state === "aborted";
  };

  while (!isDone() && Date.now() - startedAt < DEFAULT_RUN_TIMEOUT_MS * 2) {
    await new Promise((r) => setTimeout(r, DEFAULT_POLL_LOOP_MS));
  }

  await orch.shutdown();
  await architectManager.shutdownAll();

  console.log("\n===== OPERATOR-LOOP RESULT =====");
  console.log(`elapsed: ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
  console.log(`events: ${events.length}`);
  console.log(`escalations handled: ${escalationsHandled}`);
  console.log(`stdout-fake messages: ${sent.length}`);

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

  const escFired = events.some((e) => e.type === "escalation_needed");
  const completed = project?.state === "completed";
  console.log(`\nchecks:`);
  console.log(`  escalation_needed fired: ${escFired}`);
  console.log(`  ≥1 escalation handled:   ${escalationsHandled >= 1}`);
  console.log(`  project completed:       ${completed}`);

  console.log(`\nscratch preserved at: ${root}`);
  // Exit gate: project must complete cleanly. Operator dialogue is
  // best-effort — `escalation_needed` only fires on Architect arbitration
  // (Reviewer rejection cascade), and a vague-but-bounded prompt may not
  // trigger it. PRD US-3 allows the no-escalation case; PASS = project
  // completed without rebase_conflict / terminal phase failures.
  process.exit(completed ? 0 : 1);
}

main().catch((err) => {
  console.error("[op-loop] FATAL", err);
  process.exit(2);
});
