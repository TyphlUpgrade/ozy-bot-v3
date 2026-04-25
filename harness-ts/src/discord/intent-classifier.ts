/**
 * CW-4 — LLM-backed intent classifier.
 *
 * Final fallback after the CommandRouter regex layer; only invoked when
 * `NL_PATTERNS` returns no match. Uses a single-turn Claude haiku session,
 * cost-bounded ($0.05) and wall-clock-bounded (10s). On parse failure,
 * timeout, low confidence, missing fields, or budget breach → returns
 * `{type: "unknown"}` and lets the existing instructive fallback path reply.
 *
 * Security posture (per ralplan §7.3):
 * - Operator content is fenced inside `<user_message>` tags. The system
 *   prompt explicitly classifies fenced content as DATA, not instruction.
 * - `allowedTools: []` and explicit `disallowedTools` block any tool use,
 *   so a fully-jailbroken classifier session can do nothing but emit text.
 * - `maxTurns: 1` — single shot, no agentic exploration.
 *
 * Logging spec (per ralplan §4.5): every call emits `intent_classifier_called`
 * + exactly one of `intent_classified` | `intent_classifier_unknown`. Budget
 * breaches emit a `intent_classifier_budget_exceeded` warning.
 */

import { readFileSync } from "node:fs";
import type { CommandIntent, ClassifyContext, IntentClassifier } from "./commands.js";
import type { SDKClient } from "../session/sdk.js";

// --- Config / types ---

const DEFAULT_MODEL = "claude-haiku-4-5-20251001";
const DEFAULT_MAX_BUDGET_USD = 0.05;
const DEFAULT_TIMEOUT_MS = 10_000;
const DEFAULT_MIN_CONFIDENCE = 0.7;
const BUDGET_HEADROOM_FACTOR = 1.1;

export interface LlmIntentClassifierOpts {
  sdk: SDKClient;
  /** Absolute path to intent-classifier-prompt.md. Loaded eagerly at construction; throws if missing or empty. */
  systemPromptPath: string;
  /** Default "claude-haiku-4-5-20251001". */
  model?: string;
  /** Default 0.05. Hard ceiling for per-call SDK budget. */
  maxBudgetUsd?: number;
  /** Default 10_000. Wall-clock timeout via AbortController. */
  timeoutMs?: number;
  /** Default 0.7. PLACEHOLDER — calibration follow-up tracked in ralplan §6. */
  minConfidence?: number;
  /** Required cwd for SDK session. */
  cwd: string;
  /** Optional logger seam. Default writes JSON-per-line to console.log. */
  logger?: (line: ClassifierLogLine) => void;
}

export interface ClassifierLogLine {
  event:
    | "intent_classifier_called"
    | "intent_classified"
    | "intent_classifier_unknown"
    | "intent_classifier_budget_exceeded";
  channelId?: string;
  intent?: CommandIntent["type"];
  confidence?: number;
  durationMs?: number;
  costUsd?: number;
  fellThrough?: boolean;
  contentLength?: number;
  hadRecentMessages?: boolean;
  maxBudgetUsd?: number;
  reason?:
    | "low_confidence"
    | "parse_error"
    | "timeout"
    | "budget_exceeded"
    | "missing_field"
    | "empty_nongoals"
    | "no_classifier_path";
}

type ClassifierFailReason = NonNullable<ClassifierLogLine["reason"]>;

interface ParsedClassifierOutput {
  intent: string;
  fields: Record<string, unknown>;
  confidence: number;
}

// --- Runtime guards (no `any`) ---

function isString(v: unknown): v is string {
  return typeof v === "string" && v.length > 0;
}

function isStringArray(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((x) => typeof x === "string");
}

function isPlainRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// --- Default logger ---

const defaultLogger = (line: ClassifierLogLine): void => {
  // eslint-disable-next-line no-console
  console.log(JSON.stringify(line));
};

// --- Prompt assembly (security-critical fencing) ---

/**
 * Build the user-side prompt with fenced operator content. The system prompt
 * (loaded eagerly in the constructor) classifies the fenced content as DATA,
 * not instruction. NEVER concat operator text outside `<user_message>`.
 */
function buildUserPrompt(text: string, recentMessages: ClassifyContext["recentMessages"]): string {
  const parts: string[] = [];
  if (recentMessages && recentMessages.length > 0) {
    const ctxLines = recentMessages.map((m) => `${m.author}: ${m.content}`).join("\n");
    parts.push(`<recent_context>\n${ctxLines}\n</recent_context>`);
  }
  parts.push(`<user_message>\n${text}\n</user_message>`);
  parts.push("Respond with JSON only.");
  return parts.join("\n\n");
}

// --- JSON parse: try direct, then strip ```json fences, then null ---

function tryParseClassifierOutput(raw: string): ParsedClassifierOutput | null {
  const candidates: string[] = [raw.trim()];
  // Strip ```json ... ``` or ``` ... ``` fences.
  const fenceMatch = raw.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenceMatch && fenceMatch[1]) {
    candidates.push(fenceMatch[1].trim());
  }
  for (const candidate of candidates) {
    try {
      const parsed: unknown = JSON.parse(candidate);
      if (
        isPlainRecord(parsed) &&
        typeof parsed.intent === "string" &&
        typeof parsed.confidence === "number" &&
        isPlainRecord(parsed.fields)
      ) {
        return {
          intent: parsed.intent,
          fields: parsed.fields,
          confidence: parsed.confidence,
        };
      }
    } catch {
      // try next candidate
    }
  }
  return null;
}

// --- Map parsed output to typed CommandIntent ---

type MapResult = { ok: true; intent: CommandIntent } | { ok: false; reason: ClassifierFailReason };

function mapToCommandIntent(parsed: ParsedClassifierOutput, minConfidence: number): MapResult {
  if (parsed.confidence < minConfidence) {
    return { ok: false, reason: "low_confidence" };
  }
  const f = parsed.fields;
  switch (parsed.intent) {
    case "declare_project": {
      if (!isString(f.description)) return { ok: false, reason: "missing_field" };
      const goals = f.nonGoals;
      if (!isStringArray(goals)) return { ok: false, reason: "missing_field" };
      if (goals.length === 0) return { ok: false, reason: "empty_nongoals" };
      const body = `${f.description}\nNON-GOALS:\n${goals.map((g) => `- ${g}`).join("\n")}`;
      return { ok: true, intent: { type: "declare_project", message: body } };
    }
    case "new_task": {
      if (!isString(f.prompt)) return { ok: false, reason: "missing_field" };
      return { ok: true, intent: { type: "new_task", prompt: f.prompt } };
    }
    case "project_status": {
      if (!isString(f.projectId)) return { ok: false, reason: "missing_field" };
      return { ok: true, intent: { type: "project_status", projectId: f.projectId } };
    }
    case "project_abort": {
      if (!isString(f.projectId)) return { ok: false, reason: "missing_field" };
      const confirmed = typeof f.confirmed === "boolean" ? f.confirmed : false;
      return { ok: true, intent: { type: "project_abort", projectId: f.projectId, confirmed } };
    }
    case "abort_task": {
      if (!isString(f.taskId)) return { ok: false, reason: "missing_field" };
      return { ok: true, intent: { type: "abort_task", taskId: f.taskId } };
    }
    case "retry_task": {
      if (!isString(f.taskId)) return { ok: false, reason: "missing_field" };
      return { ok: true, intent: { type: "retry_task", taskId: f.taskId } };
    }
    case "escalation_response": {
      if (!isString(f.taskId) || !isString(f.message)) {
        return { ok: false, reason: "missing_field" };
      }
      return {
        ok: true,
        intent: { type: "escalation_response", taskId: f.taskId, message: f.message },
      };
    }
    case "status_query": {
      const target = isString(f.target) ? f.target : undefined;
      return { ok: true, intent: { type: "status_query", target } };
    }
    case "unknown":
      return { ok: true, intent: { type: "unknown" } };
    default:
      return { ok: false, reason: "missing_field" };
  }
}

// --- Classifier ---

export class LlmIntentClassifier implements IntentClassifier {
  private readonly sdk: SDKClient;
  private readonly systemPrompt: string;
  private readonly model: string;
  private readonly maxBudgetUsd: number;
  private readonly timeoutMs: number;
  private readonly minConfidence: number;
  private readonly cwd: string;
  private readonly logger: (line: ClassifierLogLine) => void;

  constructor(opts: LlmIntentClassifierOpts) {
    // Architect #2 — eager load. readFileSync; throws on missing file.
    const promptText = readFileSync(opts.systemPromptPath, "utf-8");
    if (promptText.trim().length === 0) {
      throw new Error(
        `LlmIntentClassifier: system prompt at ${opts.systemPromptPath} is empty`,
      );
    }
    // Sanity check — the prompt MUST teach the model about the fenced input.
    if (!promptText.includes("<user_message>")) {
      throw new Error(
        `LlmIntentClassifier: system prompt at ${opts.systemPromptPath} is missing required '<user_message>' fence reference`,
      );
    }
    this.systemPrompt = promptText;
    this.sdk = opts.sdk;
    this.model = opts.model ?? DEFAULT_MODEL;
    this.maxBudgetUsd = opts.maxBudgetUsd ?? DEFAULT_MAX_BUDGET_USD;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.minConfidence = opts.minConfidence ?? DEFAULT_MIN_CONFIDENCE;
    this.cwd = opts.cwd;
    this.logger = opts.logger ?? defaultLogger;
  }

  async classify(text: string, ctx: ClassifyContext): Promise<CommandIntent> {
    const startedAt = Date.now();

    // 1. Empty input short-circuit — never call SDK.
    if (text.trim().length === 0) {
      this.logger({
        event: "intent_classifier_called",
        channelId: ctx.channel,
        contentLength: 0,
        hadRecentMessages: !!ctx.recentMessages && ctx.recentMessages.length > 0,
      });
      this.logger({
        event: "intent_classifier_unknown",
        channelId: ctx.channel,
        reason: "no_classifier_path",
        durationMs: 0,
        costUsd: 0,
      });
      return { type: "unknown" };
    }

    this.logger({
      event: "intent_classifier_called",
      channelId: ctx.channel,
      contentLength: text.length,
      hadRecentMessages: !!ctx.recentMessages && ctx.recentMessages.length > 0,
    });

    const userPrompt = buildUserPrompt(text, ctx.recentMessages);
    const ac = new AbortController();
    const timeoutHandle = setTimeout(() => ac.abort(), this.timeoutMs);

    let timedOut = false;
    let sessionResult:
      | Awaited<ReturnType<SDKClient["consumeStream"]>>
      | null = null;
    try {
      const { query } = this.sdk.spawnSession({
        prompt: userPrompt,
        cwd: this.cwd,
        systemPrompt: this.systemPrompt,
        model: this.model,
        maxBudgetUsd: this.maxBudgetUsd,
        maxTurns: 1,
        allowedTools: [],
        disallowedTools: [
          "Bash",
          "Edit",
          "Read",
          "Write",
          "Grep",
          "Glob",
          "WebFetch",
          "WebSearch",
        ],
        permissionMode: "default",
        abortController: ac,
      });

      const timeoutPromise = new Promise<"timeout">((resolve) => {
        ac.signal.addEventListener("abort", () => {
          timedOut = true;
          resolve("timeout");
        });
      });

      const consumePromise = this.sdk.consumeStream(query);
      const raced = await Promise.race([consumePromise, timeoutPromise]);
      if (raced === "timeout") {
        this.logger({
          event: "intent_classifier_unknown",
          channelId: ctx.channel,
          reason: "timeout",
          durationMs: Date.now() - startedAt,
          costUsd: 0,
        });
        return { type: "unknown" };
      }
      sessionResult = raced;
    } catch {
      this.logger({
        event: "intent_classifier_unknown",
        channelId: ctx.channel,
        reason: timedOut ? "timeout" : "parse_error",
        durationMs: Date.now() - startedAt,
        costUsd: 0,
      });
      return { type: "unknown" };
    } finally {
      clearTimeout(timeoutHandle);
    }

    if (!sessionResult) {
      this.logger({
        event: "intent_classifier_unknown",
        channelId: ctx.channel,
        reason: "parse_error",
        durationMs: Date.now() - startedAt,
        costUsd: 0,
      });
      return { type: "unknown" };
    }

    const costUsd = sessionResult.totalCostUsd;

    // 2. Cost-bound enforcement (Architect #1).
    if (!sessionResult.success) {
      const isBudgetBreach =
        sessionResult.errors.some((e) => /budget/i.test(e)) ||
        costUsd > this.maxBudgetUsd;
      if (isBudgetBreach) {
        this.logger({
          event: "intent_classifier_budget_exceeded",
          channelId: ctx.channel,
          costUsd,
          maxBudgetUsd: this.maxBudgetUsd,
        });
      }
      this.logger({
        event: "intent_classifier_unknown",
        channelId: ctx.channel,
        reason: isBudgetBreach ? "budget_exceeded" : "parse_error",
        durationMs: Date.now() - startedAt,
        costUsd,
      });
      return { type: "unknown" };
    }

    // 3. Headroom warning — still parse if cost is within 10% slack.
    if (costUsd > this.maxBudgetUsd * BUDGET_HEADROOM_FACTOR) {
      this.logger({
        event: "intent_classifier_budget_exceeded",
        channelId: ctx.channel,
        costUsd,
        maxBudgetUsd: this.maxBudgetUsd,
      });
    }

    // 4. Parse JSON.
    const rawText = sessionResult.result ?? "";
    const parsed = tryParseClassifierOutput(rawText);
    if (!parsed) {
      this.logger({
        event: "intent_classifier_unknown",
        channelId: ctx.channel,
        reason: "parse_error",
        durationMs: Date.now() - startedAt,
        costUsd,
      });
      return { type: "unknown" };
    }

    // 5. Map.
    const mapped = mapToCommandIntent(parsed, this.minConfidence);
    if (!mapped.ok) {
      this.logger({
        event: "intent_classifier_unknown",
        channelId: ctx.channel,
        reason: mapped.reason,
        durationMs: Date.now() - startedAt,
        costUsd,
      });
      return { type: "unknown" };
    }

    this.logger({
      event: "intent_classified",
      channelId: ctx.channel,
      intent: mapped.intent.type,
      confidence: parsed.confidence,
      durationMs: Date.now() - startedAt,
      costUsd,
      fellThrough: false,
    });
    return mapped.intent;
  }
}
