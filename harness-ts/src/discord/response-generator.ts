/**
 * CW-5 — operator-visible response generator.
 *
 * Two-tier UX: hand-crafted friendly templates (`StaticResponseGenerator`,
 * default) vs. fully-conversational LLM-backed prose (`LlmResponseGenerator`,
 * opt-in via the live bootstrap).
 *
 * The dispatcher emits a structured `ResponseInput` describing what just
 * happened (intent kind + operator's original message + optional fields). The
 * generator turns that into a single string suitable for posting back to the
 * channel.
 *
 * Security posture mirrors the LLM intent classifier:
 * - Operator content is fenced inside `<operator_message>` tags (DATA, not
 *   instruction). The system prompt explicitly classifies fenced content as
 *   data and refuses embedded directives.
 * - `allowedTools: []` and `disallowedTools` block any tool use. `maxTurns: 1`
 *   keeps the call single-shot.
 * - Budget + wall-clock bounded; SDK / parse / timeout failures fall back to
 *   the static templates so the operator always sees a helpful reply.
 */

import { readFileSync } from "node:fs";
import type { SDKClient } from "../session/sdk.js";

// --- Public types ---

export type ResponseKind =
  | "no_active_project"
  | "multiple_mentions"
  | "no_session"
  | "session_terminated"
  | "queue_full"
  | "relay_generic_error"
  | "no_record_of_message"
  | "unknown_intent"
  | "ambiguous_resolution"
  // Wave E-δ MR3 / H3 fix — distinct from `no_session` (architect-relay
  // failure). Used by reviewer/executor mention branches where there is no
  // long-running session to relay to in the first place.
  | "no_active_role";

export interface ResponseInput {
  /** What kind of action just took place (intent type, error reason, etc). */
  kind: ResponseKind;
  /** Original operator message, used for context-aware response. */
  operatorMessage: string;
  /** Optional structured fields per kind (e.g., projectId, agentName). */
  fields?: Record<string, string>;
}

export interface ResponseGenerator {
  generate(input: ResponseInput): Promise<string>;
}

// --- Static (hand-crafted) templates ---

/**
 * Hand-crafted friendlier prose for each `ResponseKind`. These are the DEFAULT
 * operator-visible replies and the fallback used when the LLM generator fails.
 */
export function renderStaticTemplate(input: ResponseInput): string {
  const f = input.fields ?? {};
  switch (input.kind) {
    case "no_active_project":
      // "No active project or dialogue" substring kept for back-compat.
      return (
        "Hmm, looks like there's nothing going on right now — No active " +
        "project or dialogue. Want to start one? Try something like " +
        "*'start a project to add hello.ts, no tests'* and I'll set it up " +
        "— or use `!task`, `!project`, or `!dialogue` directly."
      );
    case "multiple_mentions": {
      const first = f.firstMention ?? "the first agent";
      // "Multiple agent mentions detected" substring pinned by dispatcher.test.ts.
      return (
        `Multiple agent mentions detected — I'm only routing to \`${first}\` ` +
        `for now. If the others should also see this, send separate messages.`
      );
    }
    case "no_record_of_message": {
      const username = f.agentName ?? "that agent";
      // "no record of that message" substring pinned by dispatcher.test.ts.
      // "${username}" substring (e.g., "Architect") also pinned.
      return (
        `I recognized this as a reply to **${username}**, but I have no record ` +
        `of that message in my context — probably from before I restarted. ` +
        `Just type the request fresh and I'll route it to the right agent.`
      );
    }
    case "no_session": {
      const pid = f.projectId ?? "<unknown>";
      // "no live Architect session" substring is pinned by dispatcher.test.ts.
      return (
        `The Architect for \`${pid}\` isn't running anymore — there's no live ` +
        `Architect session, so it may have completed or been aborted. Want me ` +
        `to start a fresh one? You can also check with \`!project ${pid} status\`.`
      );
    }
    case "session_terminated": {
      const pid = f.projectId ?? "<unknown>";
      return (
        `Looks like the Architect session for \`${pid}\` was terminated. ` +
        `Re-issue the request via \`!project <name>\` and I'll spin up a fresh ` +
        `one for you.`
      );
    }
    case "queue_full":
      return (
        "Discord send queue is full right now — your reply was dropped on the " +
        "floor. Give it about 30 seconds and try again."
      );
    case "relay_generic_error": {
      const pid = f.projectId ?? "<unknown>";
      const raw = (f.rawError ?? "").slice(0, 200);
      return (
        `Reply to \`${pid}\` failed: ${raw}. Mind giving it another shot? If ` +
        `it keeps failing, check \`!project ${pid} status\`.`
      );
    }
    case "unknown_intent":
      return (
        "I couldn't quite tell what you wanted there. Try `!task <prompt>` for " +
        "a one-off task, `!project <name>` to declare a project, or `!status` " +
        "to see what's going on."
      );
    case "ambiguous_resolution":
      return (
        "Multiple/no active projects — reply to a specific agent's message or " +
        "use `!project` commands to disambiguate."
      );
    case "no_active_role": {
      const pid = f.projectId ?? "<unknown>";
      const agentName = f.agentName ?? "agent";
      // Wave E-δ — distinct from `no_session` (Architect-relay-failure).
      // Used by reviewer/executor mention branches where no long-running
      // session exists to relay to in the first place.
      return (
        `No active ${agentName} session for project \`${pid}\` — operator ` +
        `input dropped.`
      );
    }
  }
}

/**
 * Default non-LLM generator. Returns the friendlier hand-crafted templates
 * directly. Used as the dispatcher default and as the LLM fallback.
 */
export class StaticResponseGenerator implements ResponseGenerator {
  /**
   * IMPORTANT: The exact phrases produced here are pinned by dispatcher tests
   * (`tests/discord/dispatcher.test.ts`) so the static fallback chain remains
   * predictable. The LLM path (`LlmResponseGenerator.generate`) DOES NOT
   * preserve these substrings — model output is free-form prose. Tests that
   * assert specific phrases run against the static generator only.
   */
  async generate(input: ResponseInput): Promise<string> {
    return renderStaticTemplate(input);
  }
}

// --- LLM-backed generator ---

const DEFAULT_MODEL = "claude-haiku-4-5-20251001";
const DEFAULT_MAX_BUDGET_USD = 0.02;
const DEFAULT_TIMEOUT_MS = 8_000;

export interface LlmResponseGeneratorOpts {
  sdk: SDKClient;
  /** Absolute path to the system prompt markdown file. Loaded eagerly; throws on missing/empty. */
  systemPromptPath: string;
  /** Required cwd for the SDK session. */
  cwd: string;
  /** Default "claude-haiku-4-5-20251001". */
  model?: string;
  /** Default 0.02 USD per call — strict ceiling, breach falls back to static template. */
  maxBudgetUsd?: number;
  /** Default 8000ms wall-clock timeout. */
  timeoutMs?: number;
  /** Optional fallback generator. Defaults to `StaticResponseGenerator`. */
  fallback?: ResponseGenerator;
}

/**
 * Strip envelope tokens an operator could inject to break out of the
 * `<operator_message>` fence and inject system-level instructions.
 */
function stripFenceTokens(s: string): string {
  return s.replace(/<\/?(?:operator_message|fields|kind|system)>/gi, "");
}

function buildUserPrompt(input: ResponseInput): string {
  const parts: string[] = [];
  parts.push(`<kind>${stripFenceTokens(input.kind)}</kind>`);
  if (input.fields && Object.keys(input.fields).length > 0) {
    const lines = Object.entries(input.fields)
      .map(([k, v]) => `${stripFenceTokens(k)}: ${stripFenceTokens(v)}`)
      .join("\n");
    parts.push(`<fields>\n${lines}\n</fields>`);
  }
  parts.push(`<operator_message>\n${stripFenceTokens(input.operatorMessage)}\n</operator_message>`);
  parts.push("Respond with plain prose only.");
  return parts.join("\n\n");
}

export class LlmResponseGenerator implements ResponseGenerator {
  private readonly sdk: SDKClient;
  private readonly systemPrompt: string;
  private readonly cwd: string;
  private readonly model: string;
  private readonly maxBudgetUsd: number;
  private readonly timeoutMs: number;
  private readonly fallback: ResponseGenerator;

  constructor(opts: LlmResponseGeneratorOpts) {
    const promptText = readFileSync(opts.systemPromptPath, "utf-8");
    if (promptText.trim().length === 0) {
      throw new Error(
        `LlmResponseGenerator: system prompt at ${opts.systemPromptPath} is empty`,
      );
    }
    if (!promptText.includes("<operator_message>")) {
      throw new Error(
        `LlmResponseGenerator: system prompt at ${opts.systemPromptPath} is missing required '<operator_message>' fence reference`,
      );
    }
    this.systemPrompt = promptText;
    this.sdk = opts.sdk;
    this.cwd = opts.cwd;
    this.model = opts.model ?? DEFAULT_MODEL;
    this.maxBudgetUsd = opts.maxBudgetUsd ?? DEFAULT_MAX_BUDGET_USD;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fallback = opts.fallback ?? new StaticResponseGenerator();
  }

  async generate(input: ResponseInput): Promise<string> {
    const userPrompt = buildUserPrompt(input);
    const ac = new AbortController();
    const timeoutHandle = setTimeout(() => ac.abort(), this.timeoutMs);

    type TimeoutResolver = (v: "timeout" | "settled") => void;
    const timeoutResolverHolder: { resolve: TimeoutResolver | null } = { resolve: null };
    const onAbort = (): void => {
      timeoutResolverHolder.resolve?.("timeout");
    };
    ac.signal.addEventListener("abort", onAbort, { once: true });

    let sessionResult: Awaited<ReturnType<SDKClient["consumeStream"]>> | null = null;
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

      const timeoutPromise = new Promise<"timeout" | "settled">((resolve) => {
        timeoutResolverHolder.resolve = resolve;
      });
      const consumePromise = this.sdk.consumeStream(query);
      const raced = await Promise.race([consumePromise, timeoutPromise]);
      if (raced === "timeout") {
        return this.fallback.generate(input);
      }
      sessionResult = raced as Awaited<ReturnType<SDKClient["consumeStream"]>>;
    } catch {
      return this.fallback.generate(input);
    } finally {
      clearTimeout(timeoutHandle);
      ac.signal.removeEventListener("abort", onAbort);
      timeoutResolverHolder.resolve?.("settled");
    }

    if (!sessionResult || !sessionResult.success) {
      return this.fallback.generate(input);
    }
    if (sessionResult.totalCostUsd > this.maxBudgetUsd) {
      return this.fallback.generate(input);
    }
    const out = (sessionResult.result ?? "").trim();
    if (out.length === 0) return this.fallback.generate(input);
    return out;
  }
}
