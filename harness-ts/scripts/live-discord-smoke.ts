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
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import Anthropic from "@anthropic-ai/sdk";
import { DiscordNotifier } from "../src/discord/notifier.js";
import { buildSendersForChannels } from "../src/discord/sender-factory.js";
import type { DiscordConfig } from "../src/lib/config.js";
import { OutboundResponseGenerator } from "../src/discord/outbound-response-generator.js";
import { OUTBOUND_LLM_WHITELIST } from "../src/discord/outbound-whitelist.js";
import { LlmBudgetTracker, PerRoleCircuitBreaker } from "../src/discord/llm-budget.js";
import { OUTBOUND_EPISTLE_DEFAULTS } from "../src/lib/config.js";
import { InMemoryMessageContext } from "../src/discord/message-context.js";
import { StateManager } from "../src/lib/state.js";

/**
 * Channel-collapse plumbing (2026-04-27) — extract the bare snowflake from
 * a Discord-style mention `<@123>` or `<@!123>`. Returns undefined for
 * unrecognized shapes so the caller falls back to defaults / undefined.
 */
function parseOperatorUserId(mention: string | undefined): string | undefined {
  if (!mention) return undefined;
  const match = mention.match(/^<@!?(\d+)>$/);
  return match ? match[1] : undefined;
}

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

// Phase B.2 + Wave E-α fixture matrix — exported for audit-epistle-pins.ts (AC1 §2, R-IT5-4).
// To add Wave E-α fixtures (F1-F6), append entries here; audit script picks them up automatically.
export const SMOKE_FIXTURES: Parameters<typeof DiscordNotifier.prototype.handleEvent>[0][] = [
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
  // Wave E-α audit coverage: escalation_needed with "background details" context pin (notifier.test.ts:351)
  { type: "escalation_needed", taskId: "task-smoke-esc2", escalation: { type: "scope_unclear", question: "what scope", options: ["a", "b"], context: "background details" } },
  // Wave E-α audit coverage: task_failed with backtick in reason → sanitize escapes to \` (notifier.test.ts:436)
  { type: "task_failed", taskId: "task-smoke-fail2", reason: "error: `unclosed backtick injection", attempt: 1 },
  // --- Wave E-γ smoke fixtures (D7) — one per OUTBOUND_LLM_WHITELIST tuple ---
  // Operator screenshots dev/ops/escalation channels with --llm to compare LLM
  // voice against the reference target for each (event.type, role) pair.
  // Project-scoped events only (LLM transform requires resolvable projectId).
  // project_decomposed::architect
  { type: "project_decomposed", projectId: "proj-eg-1", phaseCount: 3 },
  // architect_arbitration_fired::architect
  { type: "architect_arbitration_fired", taskId: "task-eg-arb", projectId: "proj-eg-2", cause: "review_disagreement" },
  // arbitration_verdict::architect
  { type: "arbitration_verdict", taskId: "task-eg-arb", projectId: "proj-eg-3", verdict: "retry_with_directive", rationale: "Reviewer's concern is well-founded; the missing test case must land before merge." },
  // session_complete::executor
  { type: "session_complete", taskId: "task-eg-sess", success: true, errors: [] },
  // task_done::executor
  { type: "task_done", taskId: "task-eg-done", responseLevelName: "reviewed" },
  // merge_result::orchestrator
  { type: "merge_result", taskId: "task-eg-merge", result: { status: "merged", commitSha: "f00ba12cafe34567" } },
  // review_mandatory::reviewer
  { type: "review_mandatory", taskId: "task-eg-rev", projectId: "proj-eg-4" },
  // review_arbitration_entered::reviewer
  { type: "review_arbitration_entered", taskId: "task-eg-revarb", projectId: "proj-eg-5", reviewerRejectionCount: 2 },
  // escalation_needed::orchestrator
  { type: "escalation_needed", taskId: "task-eg-esc", escalation: { type: "scope_unclear", question: "Should this parser handle file:// URLs?", options: ["yes — extend grammar", "no — out of scope"], context: "Found file:// URL in test fixture but spec doesn't mention it." } },
];

async function main(): Promise<void> {
  const harnessRoot = dirname(dirname(fileURLToPath(import.meta.url)));
  loadDotEnv(join(harnessRoot, "..", ".env"));

  // Wave E-γ — `--llm` flag opt-in. Without it the smoke runs E-α/β
  // deterministic only (operator screenshots #ops to baseline). With it,
  // the 9 whitelisted-tuple fixtures route through OutboundResponseGenerator
  // for first-person LLM voice (operator screenshots; compares voice to
  // reference target). Live Discord delivery requires DISCORD_BOT_TOKEN +
  // DEV_CHANNEL; without those the script log-only's the intent and exits.
  const llmMode = process.argv.includes("--llm");
  // `--dry-run` forces log-only path regardless of env. Use when verifying
  // fixture wiring without burning LLM cost or posting to a live channel.
  const dryRunFlag = process.argv.includes("--dry-run");

  const token = process.env.DISCORD_BOT_TOKEN;
  const dev = process.env.DEV_CHANNEL;
  const ops = process.env.AGENT_CHANNEL ?? dev;
  const escalation = process.env.ALERTS_CHANNEL ?? dev;

  if (dryRunFlag || !token || !dev) {
    const reason = dryRunFlag
      ? "--dry-run flag set"
      : "DISCORD_BOT_TOKEN or DEV_CHANNEL missing";
    console.error(
      `[discord-smoke] dry-run (${reason}) — would dispatch ` +
        `${SMOKE_FIXTURES.length} fixtures (llmMode=${llmMode})`,
    );
    for (const fx of SMOKE_FIXTURES) {
      const projectKey =
        "projectId" in fx && typeof fx.projectId === "string"
          ? `project=${fx.projectId}`
          : "taskId" in fx && typeof fx.taskId === "string"
            ? `task=${fx.taskId}`
            : "(no project/task)";
      console.error(`  - ${fx.type} (${projectKey})`);
    }
    process.exit(0);
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
    // Wave E-γ — `--llm` flag flips the feature flag for the smoke run.
    // Default off so non-LLM smoke matches Wave E-α/β behavior byte-equal.
    outbound_epistle_enabled: llmMode,
    // Channel-collapse plumbing (2026-04-27) — accept either OPERATOR_USER_ID
    // (bare snowflake) or OPERATOR_MENTION (`<@123>` shape from .env).
    operator_user_id:
      process.env.OPERATOR_USER_ID ?? parseOperatorUserId(process.env.OPERATOR_MENTION),
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
    { username: "Architect", avatarURL: "https://api.dicebear.com/9.x/bottts-neutral/svg?seed=harness-architect" },
    { username: "Reviewer", avatarURL: "https://api.dicebear.com/9.x/bottts-neutral/svg?seed=harness-reviewer" },
    { username: "Executor", avatarURL: "https://api.dicebear.com/9.x/bottts-neutral/svg?seed=harness-executor" },
    { username: "Operator", avatarURL: "https://api.dicebear.com/9.x/bottts-neutral/svg?seed=harness-operator" },
  ];

  for (const id of identities) {
    await devSender.sendToChannel(
      dev,
      `harness-ts smoke test — ${id.username} reporting in (${new Date().toISOString()})`,
      id,
    );
  }

  // Wave E-γ — generator construction is opt-in via --llm flag. When off,
  // the notifier is byte-equal to E-α/β (no LLM, no prompt-file read, no
  // budget tracker instantiation). When on, all 9 whitelisted tuples go
  // through OutboundResponseGenerator. Project-scoped fixtures only
  // activate the LLM path (notifier projectId guard).
  const messageContext = new InMemoryMessageContext({ maxEntries: 1000 });
  const stateManager = new StateManager(join(harnessRoot, ".harness", "smoke-state.json"));
  const outboundGenerator = llmMode
    ? new OutboundResponseGenerator({
        // ANTHROPIC_API_KEY is read from env automatically by the SDK constructor.
        anthropic: new Anthropic(),
        promptPaths: {
          // Wave E-γ R1 mitigation — smoke validates v2 voice quality before
          // production bootstrap (live-bot-listen.ts) flips from v1 to v2.
          architect:    resolve(harnessRoot, "config/prompts/outbound-response/v2-architect.md"),
          reviewer:     resolve(harnessRoot, "config/prompts/outbound-response/v2-reviewer.md"),
          executor:     resolve(harnessRoot, "config/prompts/outbound-response/v2-executor.md"),
          orchestrator: resolve(harnessRoot, "config/prompts/outbound-response/v2-orchestrator.md"),
        },
        whitelist: OUTBOUND_LLM_WHITELIST,
        budget: new LlmBudgetTracker({
          rootDir: harnessRoot,
          dailyCapUsd: OUTBOUND_EPISTLE_DEFAULTS.llm_daily_cap_usd,
        }),
        circuitBreaker: new PerRoleCircuitBreaker(),
        // Per-call instrumentation surfaces immediately during live smoke so
        // operator can see why the LLM didn't fire on each fixture (whitelist
        // miss / breaker / budget / api error / validation / overspend).
        onEvent: (e) => {
          // eslint-disable-next-line no-console
          console.error(`[outbound] ${JSON.stringify(e)}`);
        },
      })
    : undefined;

  // Wire DiscordNotifier so we know the routing layer also works end-to-end.
  // messageContext + stateManager required so the LLM transform's projectId
  // guard fires correctly for task-keyed events that look up projectId via state.
  const notifier = new DiscordNotifier(senders, config, {
    messageContext,
    stateManager,
    outboundGenerator,
  });

  // Wave E-γ — seed stateManager so taskId-only fixtures resolve a projectId.
  // Notifier's recording-path (and therefore LLM voice transform) only fires
  // when resolveProjectId(event) returns non-null. For events whose union type
  // doesn't carry projectId (session_complete, task_done, merge_result,
  // escalation_needed) the notifier looks up via stateManager.getTask(taskId).
  // Without seeding, those events bypass the LLM path entirely.
  for (const fx of SMOKE_FIXTURES) {
    const hasProjectId = "projectId" in fx && typeof fx.projectId === "string";
    const hasTaskId = "taskId" in fx && typeof fx.taskId === "string";
    if (!hasProjectId && hasTaskId) {
      const taskId = (fx as { taskId: string }).taskId;
      // Only seed D7 / E-γ fixtures (taskId starts with "task-eg-"); leave the
      // legacy Phase B fixtures (task-smoke-*) unseeded so they exercise the
      // plain sendToChannel fallback path.
      if (taskId.startsWith("task-eg-") && !stateManager.getTask(taskId)) {
        const task = stateManager.createTask("smoke seed prompt", taskId);
        stateManager.updateTask(task.id, { projectId: `proj-${taskId}` });
      }
    }
  }

  console.log(
    `[discord-smoke] dispatching ${SMOKE_FIXTURES.length} fixtures via notifier (llmMode=${llmMode})`,
  );
  for (const fx of SMOKE_FIXTURES) {
    notifier.handleEvent(fx);
    // Small gap so the rate-limit queue can drain between sends.
    await new Promise((r) => setTimeout(r, 100));
  }

  // Drain queue: wait long enough for the sender's rate limit to flush all
  // messages (4 direct + N from notifier). Default 2 s spacing × 30 = 60 s.
  await new Promise((r) => setTimeout(r, 60_000));

  console.log(`[discord-smoke] done — check Discord for ${SMOKE_FIXTURES.length} notifier messages across #dev / #ops / #esc`);
  process.exit(0);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error("[discord-smoke] FATAL", err);
    process.exit(2);
  });
}
