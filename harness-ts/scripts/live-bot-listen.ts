/**
 * CW-3 — live conversational Discord bootstrap.
 *
 * Wires `RawWsBotGateway` → `InboundDispatcher` → `CommandRouter`/Architect
 * relay so an operator can `start project ...` in #dev and reply to agent
 * messages to feed UNTRUSTED operator input back into the live Architect
 * session.
 *
 * Pre-flight (cross-w 5): every configured channel MUST have a webhook
 * URL — `extractWebhookIdFrom` returning null is a fatal config error
 * because without registered self-webhook ids the gateway loops on its own
 * outbound messages.
 *
 * Startup notice (cross-w 6): operator-visible warning to ops channel
 * because conversational state (`MessageContext`) is in-memory and lost on
 * restart.
 *
 * Usage:
 *   set -a && source ../.env && set +a
 *   DISCORD_WEBHOOK_DEV=... DISCORD_WEBHOOK_OPS=... DISCORD_WEBHOOK_ESCALATION=... \
 *     npx tsx scripts/live-bot-listen.ts
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "@anthropic-ai/claude-agent-sdk";

import { Orchestrator } from "../src/orchestrator.js";
import { SDKClient } from "../src/session/sdk.js";
import { SessionManager, realGitOps } from "../src/session/manager.js";
import { MergeGate } from "../src/gates/merge.js";
import { ReviewGate } from "../src/gates/review.js";
import { StateManager } from "../src/lib/state.js";
import { ProjectStore } from "../src/lib/project.js";
import { ArchitectManager } from "../src/session/architect.js";
import { loadConfig, DEFAULT_TRUNK_BRANCH } from "../src/lib/config.js";
import { DiscordNotifier } from "../src/discord/notifier.js";
import {
  buildSendersForChannels,
  extractWebhookIdFrom,
} from "../src/discord/sender-factory.js";
import { buildIdentityMap } from "../src/discord/identity-map.js";
import { InMemoryMessageContext } from "../src/discord/message-context.js";
import { InboundDispatcher } from "../src/discord/dispatcher.js";
import { RawWsBotGateway } from "../src/discord/bot-gateway.js";
import { ChannelContextBuffer } from "../src/discord/channel-context.js";
import type { InboundMessage } from "../src/discord/types.js";
import type { MessageContext } from "../src/discord/message-context.js";
import {
  CommandRouter,
  FileTaskSink,
  type AbortHook,
} from "../src/discord/commands.js";
import { LlmIntentClassifier } from "../src/discord/intent-classifier.js";
import { LlmResponseGenerator } from "../src/discord/response-generator.js";
import { BotSender } from "../src/discord/bot-sender.js";
import { installSigintHandler } from "./lib/scratch-repo.js";

function loadDotEnv(path: string): void {
  if (!existsSync(path)) return;
  for (const line of readFileSync(path, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (!process.env[key]) process.env[key] = value;
  }
}

function fatal(msg: string, exitCode = 2): never {
  console.error(`[live-bot-listen] FATAL: ${msg}`);
  process.exit(exitCode);
}

/**
 * CW-4.5 — compute the optional `projectIdHint` for a buffer append. Hint
 * fires only on operator messages that REPLY to a recorded agent message
 * (conservative). The dispatcher consumes this in `resolveProjectForChannel`
 * to disambiguate multi-active-project channels.
 */
function computeProjectIdHint(msg: InboundMessage, messageContext: MessageContext): string | null {
  if (msg.repliedToMessageId) {
    const pid = messageContext.resolveProjectIdForMessage(msg.repliedToMessageId);
    if (pid) return pid;
  }
  return null;
}

async function main(): Promise<void> {
  const harnessRoot = dirname(dirname(fileURLToPath(import.meta.url)));
  loadDotEnv(join(harnessRoot, "..", ".env"));

  const configPath =
    process.env.HARNESS_CONFIG_PATH ?? join(harnessRoot, "config", "harness", "project.toml");
  if (!existsSync(configPath)) {
    fatal(
      `config not found at ${configPath} — set HARNESS_CONFIG_PATH or create config/harness/project.toml`,
    );
  }
  const config = loadConfig(configPath);
  const token = process.env[config.discord.bot_token_env] ?? process.env.DISCORD_BOT_TOKEN;
  if (!token) {
    fatal(
      `Discord bot token missing — set ${config.discord.bot_token_env} or DISCORD_BOT_TOKEN in env`,
    );
  }

  // --- Cross-w 5: per-channel webhook ID validation, FATAL on miss ---

  const channels: Array<{ name: string; channelId: string; envKey: string }> = [
    { name: "dev_channel", channelId: config.discord.dev_channel, envKey: "DISCORD_WEBHOOK_DEV" },
    { name: "ops_channel", channelId: config.discord.ops_channel, envKey: "DISCORD_WEBHOOK_OPS" },
    {
      name: "escalation_channel",
      channelId: config.discord.escalation_channel,
      envKey: "DISCORD_WEBHOOK_ESCALATION",
    },
  ];

  // Carry the env-derived webhook URL into the parsed DiscordConfig so
  // buildSendersForChannels picks WebhookSender for every channel.
  config.discord.webhooks = {
    dev: process.env.DISCORD_WEBHOOK_DEV,
    ops: process.env.DISCORD_WEBHOOK_OPS,
    escalation: process.env.DISCORD_WEBHOOK_ESCALATION,
  };

  const webhookIdsByChannel: Record<string, string> = {};
  for (const ch of channels) {
    const url = process.env[ch.envKey];
    const id = extractWebhookIdFrom(url ?? "");
    if (!id) {
      console.error(
        `[live-bot-listen] Channel ${ch.name} (${ch.channelId}) missing webhook URL via ${ch.envKey}.\n` +
          `  Run: npx tsx scripts/provision-webhooks.ts`,
      );
      process.exit(2);
    }
    webhookIdsByChannel[ch.channelId] = id;
  }

  // --- Core construction (mirrors live-project-arbitration.ts) ---

  const harnessRepoRoot = config.project.root;
  const sdk = new SDKClient(query);
  const state = new StateManager(config.project.state_file);
  const projectStore = new ProjectStore(
    join(harnessRepoRoot, "projects.json"),
    config.project.worktree_base,
  );
  const sessions = new SessionManager(sdk, state, config);
  const mergeGate = new MergeGate(config.pipeline, harnessRepoRoot);
  const reviewGate = new ReviewGate({
    sdk,
    config,
    getTrunkBranch: () => DEFAULT_TRUNK_BRANCH,
  });
  const architectManager = new ArchitectManager({
    sdk,
    projectStore,
    stateManager: state,
    gitOps: realGitOps,
    config,
  });

  // --- Discord wiring ---

  const senders = buildSendersForChannels(config.discord, token);
  const messageContext = new InMemoryMessageContext({ maxEntries: 1000 });
  const identityMap = buildIdentityMap(config.discord);
  const notifier = new DiscordNotifier(senders, config.discord, {
    messageContext,
    stateManager: state,
  });

  const orch = new Orchestrator({
    sessionManager: sessions,
    mergeGate,
    stateManager: state,
    config,
    reviewGate,
    architectManager,
    projectStore,
  });
  orch.on((ev) => notifier.handleEvent(ev));

  // --- CommandRouter wiring ---

  const noopAbort: AbortHook = {
    abortTask(_taskId: string) {
      // Live conversational v0 — abort routing arrives via `!abort` REST path
      // through CommandRouter; this hook is a no-op until the gateway grows
      // a SIGTERM-equivalent for in-flight sessions.
    },
  };
  const taskSink = new FileTaskSink(config.project.task_dir);
  // CW-4 — LLM-backed intent classifier (regex→LLM cascade final stage).
  const classifier = new LlmIntentClassifier({
    sdk,
    systemPromptPath: join(harnessRoot, "config", "harness", "intent-classifier-prompt.md"),
    cwd: harnessRepoRoot,
  });
  // CW-4.5 — single shared per-channel ring buffer feeding both the LLM
  // classifier (via `recentMessagesProvider`) and the dispatcher's mention
  // resolution (via `channelBuffer`).
  //
  // Security MED Q4 — buffer memory bound is implicitly capped by the gateway
  // channel allowlist: the gateway filters inbound messages to
  // `allowedChannelIds`, so only messages from those channels ever reach
  // `channelBuffer.append`. As long as `maxChannels >= allowedChannelIds.size`,
  // no LRU eviction can occur due to operator traffic alone — the buffer's
  // total memory ceiling is `maxChannels × perChannelCap × ~500B ≈ 250KB`.
  // We assert the invariant at construction so a future config change that
  // adds channels without bumping `maxChannels` fails fast and loud.
  const allowedChannelIds = new Set<string>([
    config.discord.dev_channel,
    config.discord.ops_channel,
    config.discord.escalation_channel,
  ]);
  const CHANNEL_BUFFER_MAX_CHANNELS = 50;
  if (CHANNEL_BUFFER_MAX_CHANNELS < allowedChannelIds.size) {
    fatal(
      `ChannelContextBuffer.maxChannels (${CHANNEL_BUFFER_MAX_CHANNELS}) < allowedChannelIds.size ` +
        `(${allowedChannelIds.size}) — bump maxChannels so the implicit memory bound holds.`,
    );
  }
  const channelBuffer = new ChannelContextBuffer({
    perChannelCap: 10,
    maxChannels: CHANNEL_BUFFER_MAX_CHANNELS,
  });

  const commandRouter = new CommandRouter({
    state,
    config,
    classifier,
    abort: noopAbort,
    taskSink,
    projectStore,
    emit: (ev) => notifier.handleEvent(ev),
    orchestrator: orch,
    // CW-4.5 — strip projectIdHint from the classifier view (it's a
    // dispatcher-private signal, not classifier-relevant data).
    recentMessagesProvider: (channelId) =>
      channelBuffer.recent(channelId, 5).map((m) => ({
        author: m.author,
        content: m.content,
        timestamp: m.timestamp,
      })),
  });

  // --- Gateway + dispatcher ---

  const gateway = new RawWsBotGateway({
    token,
    allowedChannelIds: Array.from(allowedChannelIds),
  });
  gateway.registerSelfWebhookIds(Object.values(webhookIdsByChannel));

  // Critic 13 — surface MESSAGE_CONTENT-disabled state to the operator.
  gateway.onMessageContentMissing(() => {
    const opsSender = senders[config.discord.ops_channel];
    void opsSender?.sendToChannel(
      config.discord.ops_channel,
      "**Warning:** MESSAGE_CONTENT intent appears disabled — enable in Discord Developer Portal, then restart.",
    );
  });

  // CW-5 — reaction acknowledgments need a bot REST client (webhooks can't
  // react). Construct a dedicated BotSender separate from the per-channel
  // content senders so reactions work even on webhook-routed channels.
  const reactionClient = new BotSender(token);

  // CW-5 — LlmResponseGenerator turns dispatcher signals into conversational
  // prose; falls back to StaticResponseGenerator on SDK / budget / timeout.
  const responseGenerator = new LlmResponseGenerator({
    sdk,
    systemPromptPath: join(harnessRoot, "config", "harness", "response-generator-prompt.md"),
    cwd: harnessRepoRoot,
  });

  const dispatcher = new InboundDispatcher({
    commandRouter,
    architectManager,
    identityMap,
    senders,
    config: config.discord,
    messageContext,
    // CW-4.5 v2 — narrow seams for mention rule 1.
    projectStore,
    channelBuffer,
    getBotUsername: () => gateway.getBotUsername(),
    // CW-5 — UX polish: reactions + conversational responses.
    reactionClient,
    responseGenerator,
  });
  // CW-4.5 §8 — explicit single-handler swap. The wrapper appends to the
  // shared ChannelContextBuffer BEFORE invoking the dispatcher so the message
  // that triggered classification is included in its own recentMessages.
  gateway.on((msg) => {
    channelBuffer.append(msg.channelId, {
      author: msg.authorUsername,
      content: msg.content,
      timestamp: msg.timestamp,
      projectIdHint: computeProjectIdHint(msg, messageContext) ?? undefined,
    });
    void dispatcher.dispatch(msg);
  });

  // --- Startup ops-channel notice (cross-w 6) ---

  const opsSender = senders[config.discord.ops_channel];
  void opsSender?.sendToChannel(
    config.discord.ops_channel,
    "harness-ts started. Conversational state lost across restarts; please re-issue commands as `!project ...` or by replying to a fresh agent message that lands after this notice.",
  );

  installSigintHandler([
    { shutdown: () => gateway.stop() },
    { shutdown: () => orch.shutdown() },
    { shutdown: () => architectManager.shutdownAll() },
  ]);

  await gateway.start();
  orch.start();

  console.log("[live-bot-listen] gateway + orchestrator running. Ctrl+C to exit.");

  // Park forever — SIGINT handler exits the process.
  await new Promise<void>(() => undefined);
}

main().catch((err) => {
  console.error("[live-bot-listen] FATAL", err);
  process.exit(2);
});
