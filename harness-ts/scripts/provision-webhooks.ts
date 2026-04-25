/**
 * CW-1 — One-shot webhook provisioning CLI.
 *
 * For each configured channel (dev/ops/escalation), GET existing webhooks
 * via the bot token; if a webhook with the conventional name already exists
 * reuse it (idempotent), otherwise POST a new one. Print env-var-format
 * URLs + webhook ids to stdout for operator capture.
 *
 * Required env:
 *   DISCORD_BOT_TOKEN     — bot token with MANAGE_WEBHOOKS in each channel
 *   DEV_CHANNEL           — channel id (snowflake)
 *   AGENT_CHANNEL         — channel id; defaults to DEV_CHANNEL
 *   ALERTS_CHANNEL        — channel id; defaults to DEV_CHANNEL
 *
 * Usage:
 *   set -a && source ../.env && set +a
 *   npx tsx scripts/provision-webhooks.ts
 *
 * Output (stdout, ready to paste into env):
 *   DISCORD_WEBHOOK_DEV=https://discord.com/api/webhooks/<id>/<token>
 *   DISCORD_WEBHOOK_DEV_ID=<id>
 *   DISCORD_WEBHOOK_OPS=...
 *   DISCORD_WEBHOOK_OPS_ID=...
 *   DISCORD_WEBHOOK_ESCALATION=...
 *   DISCORD_WEBHOOK_ESCALATION_ID=...
 */

const DISCORD_API_BASE = "https://discord.com/api/v10";

interface WebhookRecord {
  id: string;
  name: string;
  url?: string;
  token?: string;
  channel_id: string;
}

function authHeaders(token: string): Record<string, string> {
  return {
    "Authorization": `Bot ${token}`,
    "Content-Type": "application/json",
    "User-Agent": "harness-ts (https://github.com/anthropics/claude-code, 0.1)",
  };
}

async function listWebhooks(token: string, channelId: string): Promise<WebhookRecord[]> {
  const res = await fetch(`${DISCORD_API_BASE}/channels/${channelId}/webhooks`, {
    headers: authHeaders(token),
  });
  if (!res.ok) {
    throw new Error(`GET /channels/${channelId}/webhooks -> ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as WebhookRecord[];
}

async function createWebhook(token: string, channelId: string, name: string): Promise<WebhookRecord> {
  const res = await fetch(`${DISCORD_API_BASE}/channels/${channelId}/webhooks`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    throw new Error(`POST /channels/${channelId}/webhooks -> ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as WebhookRecord;
}

function buildUrl(record: WebhookRecord): string {
  if (record.url) return record.url;
  if (record.token) return `${DISCORD_API_BASE}/webhooks/${record.id}/${record.token}`;
  // Token only present on POST response or when bot owns the webhook; if missing
  // we cannot reconstruct the URL — surface a clear error.
  throw new Error(`webhook ${record.id} returned without token; bot may not own it`);
}

interface Channel {
  envVar: string;        // e.g., "DISCORD_WEBHOOK_DEV"
  webhookName: string;   // conventional name used for GET-then-reuse
  channelId: string;
}

async function provision(channel: Channel, token: string): Promise<{ url: string; id: string }> {
  const existing = await listWebhooks(token, channel.channelId);
  const match = existing.find((w) => w.name === channel.webhookName);
  if (match) {
    return { url: buildUrl(match), id: match.id };
  }
  const created = await createWebhook(token, channel.channelId, channel.webhookName);
  return { url: buildUrl(created), id: created.id };
}

async function main(): Promise<void> {
  const token = process.env.DISCORD_BOT_TOKEN;
  const dev = process.env.DEV_CHANNEL;
  const ops = process.env.AGENT_CHANNEL ?? dev;
  const escalation = process.env.ALERTS_CHANNEL ?? dev;

  if (!token) {
    console.error("[provision-webhooks] DISCORD_BOT_TOKEN missing — populate .env or export it");
    process.exit(2);
  }
  if (!dev) {
    console.error("[provision-webhooks] DEV_CHANNEL missing — populate .env or export it");
    process.exit(2);
  }

  const channels: Channel[] = [
    { envVar: "DISCORD_WEBHOOK_DEV", webhookName: "harness-dev", channelId: dev },
    { envVar: "DISCORD_WEBHOOK_OPS", webhookName: "harness-ops", channelId: ops! },
    { envVar: "DISCORD_WEBHOOK_ESCALATION", webhookName: "harness-escalation", channelId: escalation! },
  ];

  // Phase 4 H1 (CR) — deduplicate channels that map to the same id so we don't
  // make redundant API calls or emit redundant webhook env-vars. Provision
  // once per unique channel id (using the first label's webhookName), then
  // emit env-vars for every label that maps to that same id, all pointing at
  // the single provisioned webhook URL.
  const uniqueByChannelId = new Map<string, Channel>();
  for (const c of channels) {
    if (!uniqueByChannelId.has(c.channelId)) uniqueByChannelId.set(c.channelId, c);
  }

  const provisioned = new Map<string, { url: string; id: string }>();
  for (const channel of uniqueByChannelId.values()) {
    try {
      const result = await provision(channel, token);
      provisioned.set(channel.channelId, result);
    } catch (err) {
      console.error(
        `[provision-webhooks] ${channel.envVar} (channel=${channel.channelId}) failed: ${
          (err as Error).message
        }`,
      );
      process.exit(1);
    }
  }

  // Emit env-vars for every label, reusing the single provisioned webhook
  // when multiple labels collapsed to the same channel id.
  for (const channel of channels) {
    const result = provisioned.get(channel.channelId);
    if (!result) continue;
    console.log(`${channel.envVar}=${result.url}`);
    console.log(`${channel.envVar}_ID=${result.id}`);
  }
}

main().catch((err) => {
  console.error("[provision-webhooks] FATAL", err);
  process.exit(2);
});
