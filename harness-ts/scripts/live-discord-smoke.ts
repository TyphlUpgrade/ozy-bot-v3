/**
 * Discord live-delivery smoke test.
 *
 * Sends one message per agent identity (Architect / Reviewer / Executor /
 * Operator) to the dev channel and one routed event through DiscordNotifier.
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
  // Fake event: project_declared maps to dev channel by default.
  const notifier = new DiscordNotifier(senders, config);
  notifier.handleEvent({
    type: "project_declared",
    projectId: "smoke-test-fake-id",
    name: "discord-smoke-test",
  });

  // Drain queue: wait long enough for the sender's rate limit to flush all
  // 5 messages (4 direct + 1 from notifier). Default 2 s spacing × 5 = 10 s.
  await new Promise((r) => setTimeout(r, 12_000));

  console.log("[discord-smoke] done — check Discord for 5 messages in #dev");
  process.exit(0);
}

main().catch((err) => {
  console.error("[discord-smoke] FATAL", err);
  process.exit(2);
});
