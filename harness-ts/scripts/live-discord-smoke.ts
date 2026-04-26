/**
 * Discord live-delivery smoke test.
 *
 * Sends one message per agent identity (Architect / Reviewer / Executor /
 * Operator) to the dev channel, then fires 16 fixtures through DiscordNotifier
 * covering every row in the Phase B.1 renderer table.
 *
 * CW-1: routes via `buildSendersForChannels` so channels with a webhook URL
 * get per-agent username AND avatar; channels without fall back to bot REST
 * (identity rendered as `**[Name]**` prefix).
 *
 * Reads `.env` (or already-exported env) for:
 *   - DISCORD_BOT_TOKEN          — required, bot token from Discord dev portal
 *   - DEV_CHANNEL                — required, channel ID (snowflake)
 *   - AGENT_CHANNEL              — optional, defaults to DEV_CHANNEL
 *   - ALERTS_CHANNEL             — optional, defaults to DEV_CHANNEL
 *   - DISCORD_WEBHOOK_DEV        — optional; per-channel webhook for #dev
 *   - DISCORD_WEBHOOK_OPS        — optional; per-channel webhook for #ops
 *   - DISCORD_WEBHOOK_ESCALATION — optional; per-channel webhook for #escalation
 *
 * Webhook URLs upgrade the channel from BotSender to WebhookSender so per-
 * agent avatars render natively. Provision via `scripts/provision-webhooks.ts`.
 *
 * Phase B.2 fixture matrix (one per Phase B.1 renderer row):
 *   Row  1 — task_picked_up
 *   Row  2 — session_complete (success)
 *   Row  3 — session_complete (failure, errors + terminalReason)
 *   Row  9 — task_done (with responseLevelName)
 *   Row  9 — task_done (without responseLevelName)
 *   Row  4 — merge_result (merged, sha7)
 *   Row  5 — merge_result (test_failed, error)
 *   Row  6 — merge_result (test_timeout)
 *   Row  7 — merge_result (rebase_conflict, 4 files → shows first 3)
 *   Row  8 — merge_result (error)
 *   Row 10 — task_failed (with attempt)
 *   Row 11 — escalation_needed (with options + context)
 *   Row 14 — arbitration_verdict
 *   Row 13 — budget_ceiling_reached
 *   Row 12 — project_failed (with failedPhase)
 *
 * Usage:
 *   set -a && source ../.env && set +a    # if .env not auto-loaded
 *   npx tsx scripts/live-discord-smoke.ts
 */

import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { DiscordNotifier } from "../src/discord/notifier.js";
import { buildSendersForChannels } from "../src/discord/sender-factory.js";
import type { DiscordConfig } from "../src/lib/config.js";

function loadDotEnv(path: string): void {
  if (!existsSync(path)) return;
  for (const line of readFileSync(path, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!process.env[key]) process.env[key] = value;
  }
}

async function main(): Promise<void> {
  const harnessRoot = dirname(dirname(fileURLToPath(import.meta.url)));
  loadDotEnv(join(harnessRoot, "..", ".env"));

  const token = process.env.DISCORD_BOT_TOKEN;
  const dev = process.env.DEV_CHANNEL;
  const ops = process.env.AGENT_CHANNEL ?? dev;
  const escalation = process.env.ALERTS_CHANNEL ?? dev;

  if (!token) {
    console.error("[discord-smoke] DISCORD_BOT_TOKEN missing — populate .env or export it");
    process.exit(2);
  }
  if (!dev) {
    console.error("[discord-smoke] DEV_CHANNEL missing — populate .env or export it");
    process.exit(2);
  }

  console.log(`[discord-smoke] sending 4 messages to dev channel ${dev}`);
  const config: DiscordConfig = {
    bot_token_env: "DISCORD_BOT_TOKEN",
    dev_channel: dev,
    ops_channel: ops!,
    escalation_channel: escalation!,
    webhooks: {
      dev: process.env.DISCORD_WEBHOOK_DEV,
      ops: process.env.DISCORD_WEBHOOK_OPS,
      escalation: process.env.DISCORD_WEBHOOK_ESCALATION,
    },
    agents: {},
  };
  // CW-1 — per-channel sender map. Channels with webhook URL get WebhookSender
  // (native per-agent avatar); others fall back to BotSender.
  const senders = buildSendersForChannels(config, token);
  const devSender = senders[dev];
  if (!devSender) {
    console.error(`[discord-smoke] no sender constructed for channel ${dev}`);
    process.exit(2);
  }

  const identities = [
    { username: "Architect", avatarURL: "" },
    { username: "Reviewer", avatarURL: "" },
    { username: "Executor", avatarURL: "" },
    { username: "Operator", avatarURL: "" },
  ];

  for (const id of identities) {
    await devSender.sendToChannel(
      dev,
      `harness-ts smoke test — ${id.username} reporting in (${new Date().toISOString()})`,
      id,
    );
  }

  // Wire DiscordNotifier so we know the routing layer also works end-to-end.
  const notifier = new DiscordNotifier(senders, config);

  // Phase B.2 fixture matrix — one event per renderer row in Phase B.1 table.
  const fixtures: Parameters<typeof notifier.handleEvent>[0][] = [
    // Row 1 — task_picked_up
    { type: "task_picked_up", taskId: "task-smoke-1", prompt: "demo prompt for url parser" },
    // Row 2 — session_complete success
    { type: "session_complete", taskId: "task-smoke-1", success: true, errors: [] },
    // Row 3 — session_complete failure with errors + terminalReason
    { type: "session_complete", taskId: "task-smoke-1f", success: false, errors: ["build broke", "lint fail"], terminalReason: "max_iterations" },
    // Row 9 — task_done with responseLevelName
    { type: "task_done", taskId: "task-smoke-1", responseLevelName: "reviewed" },
    // Row 9 — task_done without responseLevelName
    { type: "task_done", taskId: "task-smoke-2" },
    // Row 4 — merge_result merged (sha7)
    { type: "merge_result", taskId: "task-smoke-1", result: { status: "merged", commitSha: "abc1234567890" } },
    // Row 5 — merge_result test_failed
    { type: "merge_result", taskId: "task-smoke-1", result: { status: "test_failed", error: "FAIL: src/url/parser.test.ts > parses scheme\nExpected 'http' got undefined" } },
    // Row 6 — merge_result test_timeout
    { type: "merge_result", taskId: "task-smoke-1", result: { status: "test_timeout" } },
    // Row 7 — merge_result rebase_conflict (4 files → shows first 3)
    { type: "merge_result", taskId: "task-smoke-1", result: { status: "rebase_conflict", conflictFiles: ["src/url/parser.ts", "src/url/scheme.ts", "src/url/host.ts", "tests/url.test.ts"] } },
    // Row 8 — merge_result error
    { type: "merge_result", taskId: "task-smoke-1", result: { status: "error", error: "git push rejected: non-fast-forward" } },
    // Row 10 — task_failed with attempt
    { type: "task_failed", taskId: "task-smoke-1", reason: "executor stack trace overflow", attempt: 3 },
    // Row 11 — escalation_needed with options + context
    { type: "escalation_needed", taskId: "task-smoke-1", escalation: { type: "scope_unclear", question: "Should the parser handle file:// URLs?", options: ["yes — extend grammar", "no — out of scope"], context: "Found file:// URL in test fixture but spec doesn't mention it." } },
    // Row 14 — arbitration_verdict
    { type: "arbitration_verdict", taskId: "task-smoke-1", projectId: "proj-smoke", verdict: "retry_with_directive", rationale: "Reviewer concern is valid: add the missing test." },
    // Row 13 — budget_ceiling_reached
    { type: "budget_ceiling_reached", projectId: "proj-smoke", currentCostUsd: 9.80, ceilingUsd: 10.00 },
    // Row 12 — project_failed with failedPhase
    { type: "project_failed", projectId: "proj-smoke", reason: "Architect issued escalate_operator after 3 retry cycles", failedPhase: "phase-2-implement-parser" },
    // Row 12b — project_failed without failedPhase (spawn-time)
    { type: "project_failed", projectId: "proj-smoke-2", reason: "architect spawn failed" },
  ];

  for (const fx of fixtures) {
    notifier.handleEvent(fx);
    // Small gap so the rate-limit queue can drain between sends.
    await new Promise((r) => setTimeout(r, 100));
  }

  // Drain queue: wait long enough for the sender's rate limit to flush all
  // 20 messages (4 direct + 16 from notifier). Default 2 s spacing × 20 = 40 s.
  await new Promise((r) => setTimeout(r, 40_000));

  console.log("[discord-smoke] done — check Discord for 16 notifier messages across #dev / #ops / #esc");
  process.exit(0);
}

main().catch((err) => {
  console.error("[discord-smoke] FATAL", err);
  process.exit(2);
});
