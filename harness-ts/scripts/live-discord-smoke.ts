/**
 * Discord live-delivery smoke test.
 *
 * Sends one message per agent identity (Architect / Reviewer / Executor /
 * Operator) to the dev channel via the bot REST API. Confirms BotSender wiring
 * + bot-token authorization + per-message identity prefix work end-to-end.
 *
 * Reads `.env` (or already-exported env) for:
 *   - DISCORD_BOT_TOKEN — required, bot token from Discord developer portal
 *   - DEV_CHANNEL       — required, channel ID (snowflake)
 *
 * Usage:
 *   set -a && source ../.env && set +a    # if .env not auto-loaded
 *   npx tsx scripts/live-discord-smoke.ts
 */

import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { BotSender } from "../src/discord/bot-sender.js";
import { DiscordNotifier } from "../src/discord/notifier.js";
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
  const sender = new BotSender(token, { minSpacingMs: 2000 });

  const identities = [
    { username: "Architect", avatarURL: undefined },
    { username: "Reviewer", avatarURL: undefined },
    { username: "Executor", avatarURL: undefined },
    { username: "Operator", avatarURL: undefined },
  ];

  for (const id of identities) {
    await sender.sendToChannel(
      dev,
      `harness-ts smoke test — ${id.username} reporting in (${new Date().toISOString()})`,
      id,
    );
  }

  // Wire DiscordNotifier so we know the routing layer also works end-to-end.
  // Fake event: project_declared maps to dev channel by default.
  const config: DiscordConfig = {
    bot_token_env: "DISCORD_BOT_TOKEN",
    dev_channel: dev,
    ops_channel: ops!,
    escalation_channel: escalation!,
    agents: {},
  };
  const notifier = new DiscordNotifier(sender, config);
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
