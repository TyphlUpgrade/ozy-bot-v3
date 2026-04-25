/**
 * CW-1 — sender factory: returns a per-channel `DiscordSender` map.
 *
 * Channels listed in `DiscordConfig.webhooks` get a `WebhookSender` (per-agent
 * username + avatar render natively); channels without a webhook fall back to
 * `BotSender` (uses the bot token and renders identity as a `**[Name]**`
 * prefix). Live scripts call `buildSendersForChannels(config, token)` and
 * inject the result into `DiscordNotifier`, which routes by channel id.
 *
 * Webhook URLs are runtime-only and never embedded in source — the factory
 * reads them from the already-parsed `DiscordConfig`.
 *
 * **No discord.js dependency.** WebhookSender accepts a minimal `WebhookClient`
 * (see types.ts); the factory wires a fetch-based client that POSTs to the
 * webhook URL with `?wait=true` so the response carries the message id.
 */

import { BotSender } from "./bot-sender.js";
import { WebhookSender } from "./sender.js";
import type { DiscordSender, WebhookClient } from "./types.js";
import type { DiscordConfig } from "../lib/config.js";

/**
 * Discord webhook URL regex — matches `/api/webhooks/<id>/<token>` AND
 * `/api/v10/webhooks/<id>/<token>` (v\d+ optional segment). Capture group 1
 * is the webhook id (snowflake digits).
 */
const WEBHOOK_URL_RE = /\/api\/(?:v\d+\/)?webhooks\/(\d+)\//;

/**
 * Extract the webhook id from a Discord webhook URL. Returns `null` if the
 * URL is malformed or doesn't match the expected shape.
 */
export function extractWebhookIdFrom(url: string): string | null {
  if (typeof url !== "string" || url.length === 0) return null;
  const match = WEBHOOK_URL_RE.exec(url);
  return match ? match[1] : null;
}

/** Map channel id → (channelKey, webhookUrl?). Internal helper. */
type ChannelPlan = Array<{ channelId: string; webhookUrl?: string }>;

function planChannels(config: DiscordConfig): ChannelPlan {
  // CW-1 — three production channels; lookup table so adding a fourth is a
  // single row + new optional `webhooks.<key>` field.
  return [
    { channelId: config.dev_channel, webhookUrl: config.webhooks?.dev },
    { channelId: config.ops_channel, webhookUrl: config.webhooks?.ops },
    { channelId: config.escalation_channel, webhookUrl: config.webhooks?.escalation },
  ];
}

export interface SenderFactoryOptions {
  /** Test seam: override WebhookClient construction. Defaults to fetch-based client. */
  webhookClientFactory?: (url: string) => WebhookClient;
  /** Test seam: override fetch (used by both BotSender and the default webhook client). */
  fetch?: typeof globalThis.fetch;
}

/**
 * Default WebhookClient — fetch-based POST to the webhook URL with
 * `?wait=true`. On success returns the JSON body so WebhookSender can extract
 * `.id`. Errors propagate so WebhookSender's swallow-and-log wrapper logs them.
 */
function fetchWebhookClient(
  url: string,
  fetchImpl: typeof globalThis.fetch,
): WebhookClient {
  return {
    async send(options) {
      const target = `${url}${url.includes("?") ? "&" : "?"}wait=true`;
      const res = await fetchImpl(target, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: options.content,
          username: options.username,
          avatar_url: options.avatarURL,
          allowed_mentions: options.allowedMentions ?? { parse: [] },
        }),
      });
      if (!res.ok) {
        throw new Error(`webhook ${res.status} ${res.statusText}`);
      }
      try {
        return (await res.json()) as unknown;
      } catch {
        return undefined;
      }
    },
  };
}

/**
 * Build a `Record<channelId, DiscordSender>` for the configured production
 * channels. Channels with a webhook URL get a `WebhookSender`; others share a
 * single `BotSender` instance keyed off the bot token.
 *
 * Distinct channelIds always get distinct entries; channels that map to the
 * same id (e.g., dev_channel == ops_channel in a test config) collapse to a
 * single entry — last write wins, but per-call routing is identical so this
 * is safe.
 */
export function buildSendersForChannels(
  config: DiscordConfig,
  token: string,
  options: SenderFactoryOptions = {},
): Record<string, DiscordSender> {
  const fetchImpl = options.fetch ?? globalThis.fetch;
  const factory =
    options.webhookClientFactory ?? ((url: string) => fetchWebhookClient(url, fetchImpl));
  const senders: Record<string, DiscordSender> = {};
  let lazyBot: BotSender | null = null;
  const getBot = (): BotSender => {
    if (lazyBot) return lazyBot;
    lazyBot = new BotSender(token, options.fetch ? { fetch: options.fetch } : {});
    return lazyBot;
  };
  for (const { channelId, webhookUrl } of planChannels(config)) {
    if (!channelId) continue;
    if (webhookUrl && webhookUrl.length > 0) {
      const client = factory(webhookUrl);
      senders[channelId] = new WebhookSender(client);
    } else {
      senders[channelId] = getBot();
    }
  }
  return senders;
}
