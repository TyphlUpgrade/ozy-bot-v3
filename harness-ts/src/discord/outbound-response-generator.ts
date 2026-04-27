/**
 * Wave E-γ — outbound LLM voice generator.
 *
 * Per-role first-person prose rewrites of deterministic event bodies.
 * Calls the Anthropic Messages API directly via `@anthropic-ai/sdk` — a
 * stateless one-shot operation, no Claude Agent SDK session, no
 * harness-ts/CLAUDE.md / settings.json / MCP / plugins overhead.
 *
 * Differences vs `LlmResponseGenerator` (`src/discord/response-generator.ts`):
 *   - Per-role system prompts (Architect / Reviewer / Executor / Orchestrator)
 *     loaded eagerly at construction; throws on missing or empty file (D9).
 *   - Whitelist guard: non-eligible (event.type, role) tuples short-circuit
 *     to the deterministic body without calling the API (D5).
 *   - Daily budget tracker + per-role circuit breaker as runtime guards (D3, D4).
 *   - Replacement-with-fallback semantic: on ANY failure path, returns
 *     `deterministicBody` verbatim. NEVER throws.
 *   - Optional per-call instrumentation via `onEvent` callback (silent by
 *     default; bootstraps wire to console for visibility).
 *
 * Security posture:
 *   - Deterministic body fenced inside `<operator_input>` tags. Event payload
 *     fenced inside `<event_payload>` tags. The system prompt classifies fenced
 *     content as DATA, not instruction.
 *   - No tools available — Messages API has no tool concept on this call.
 *   - Bounded `max_tokens` cap on output (default 800 ≈ 1500 chars).
 *   - Wall-clock bounded via AbortController.
 *
 * Failure semantics inside `generate()`:
 *   - whitelist miss → return deterministicBody (no API call, no log, no breaker)
 *   - circuit breaker open → return deterministicBody; log ONCE per process per role
 *   - budget rejects → return deterministicBody; log ONCE per UTC day to console.warn
 *   - API throws / abort / non-text → increment breaker; return deterministicBody
 *   - schema validation fail → increment breaker; return deterministicBody
 *   - actual cost > maxBudgetUsd → charge actual + increment breaker; return deterministicBody
 *   - any uncaught throw → swallow, return deterministicBody
 */

import { readFileSync } from "node:fs";

import type Anthropic from "@anthropic-ai/sdk";

import type { OrchestratorEvent } from "../orchestrator.js";

import { LlmBudgetTracker, PerRoleCircuitBreaker } from "./llm-budget.js";

// --- Public types ---

export type OutboundRole = "architect" | "reviewer" | "executor" | "orchestrator";

/**
 * Per-call instrumentation event. Emitted exactly once per `generate()` call
 * (after guards short-circuit OR after the API call resolves). `undefined`
 * `onEvent` opt → silent (preserves prior behavior). Bootstraps wire to
 * `console.log` so operators can grep for `fallback_*` variants to understand
 * why the LLM didn't fire.
 */
export type OutboundGenEvent =
  | { kind: "spawned"; role: OutboundRole; eventType: string }
  | { kind: "fallback_whitelist"; role: OutboundRole; eventType: string }
  | { kind: "fallback_breaker"; role: OutboundRole }
  | { kind: "fallback_budget"; spentUsd: number }
  | { kind: "fallback_timeout"; role: OutboundRole; eventType: string }
  | { kind: "fallback_api_error"; role: OutboundRole; eventType: string; err: string }
  | { kind: "fallback_validation"; role: OutboundRole; eventType: string }
  | { kind: "fallback_overspend"; role: OutboundRole; costUsd: number }
  | { kind: "success"; role: OutboundRole; eventType: string; costUsd: number; outputChars: number };

export interface OutboundResponseGeneratorOpts {
  anthropic: Anthropic;
  promptPaths: Record<OutboundRole, string>;
  whitelist: ReadonlySet<string>;
  budget: LlmBudgetTracker;
  circuitBreaker: PerRoleCircuitBreaker;
  /** Default "claude-haiku-4-5-20251001". */
  model?: string;
  /** Default 0.02 USD per call — strict ceiling, breach falls back. */
  maxBudgetUsd?: number;
  /** Default 8000ms wall-clock timeout. */
  timeoutMs?: number;
  /**
   * Default 800 tokens (~1500 chars). Bounds cost; truncated server-side if
   * the model would generate more. Output is further truncated client-side
   * to Discord's 1900-char headroom cap.
   */
  maxOutputTokens?: number;
  /** Optional per-call instrumentation. Default `undefined` = silent. */
  onEvent?: (e: OutboundGenEvent) => void;
}

export interface OutboundGenerateInput {
  event: OrchestratorEvent;
  role: OutboundRole;
  deterministicBody: string;
}

// --- Constants ---

export const DEFAULT_OUTBOUND_MODEL = "claude-haiku-4-5-20251001";
export const DEFAULT_OUTBOUND_MAX_BUDGET_USD = 0.02;
export const DEFAULT_OUTBOUND_TIMEOUT_MS = 8_000;
export const DEFAULT_OUTBOUND_MAX_OUTPUT_TOKENS = 800;

const ALL_ROLES: readonly OutboundRole[] = [
  "architect",
  "reviewer",
  "executor",
  "orchestrator",
];

const DISCORD_BODY_MAX_CHARS = 1900;

/**
 * Per-model token pricing in USD per 1M tokens. Fall through to
 * `FALLBACK_PRICING` (Haiku rates) if the model isn't listed — keeps cost
 * accounting conservative-ish without crashing on a new/unknown model.
 *
 * To add a model: add one row here.
 */
const MODEL_PRICING_PER_MTOK: Record<string, { input: number; output: number }> = {
  "claude-haiku-4-5-20251001": { input: 1.0, output: 5.0 },
  "claude-sonnet-4-6": { input: 3.0, output: 15.0 },
  "claude-opus-4-7": { input: 15.0, output: 75.0 },
};
const FALLBACK_PRICING = { input: 1.0, output: 5.0 };

// --- Helpers ---

/** Discord hard cap is 2000 chars; truncate to 1900 to leave headroom. Mirrors notifier.ts. */
function truncateBody(body: string, max = DISCORD_BODY_MAX_CHARS): string {
  if (body.length <= max) return body;
  return body.slice(0, max - 1) + "…";
}

/** Strip envelope tokens an operator could inject to break out of the fences. */
function stripFenceTokens(s: string): string {
  return s.replace(/<\/?(?:operator_input|event_payload|system)>/gi, "");
}

function buildUserPrompt(input: OutboundGenerateInput): string {
  // Serialize the event payload as JSON (sanitized of fence tokens) so the LLM
  // can extract structured fields verbatim. Operator-derived prose lives in
  // the deterministic body, fenced separately.
  let payloadJson: string;
  try {
    payloadJson = JSON.stringify(input.event, null, 2);
  } catch {
    payloadJson = JSON.stringify({ type: input.event.type });
  }
  const safePayload = stripFenceTokens(payloadJson);
  const safeBody = stripFenceTokens(input.deterministicBody);

  // Inject current UTC time so v2 prompts can render the section-header
  // pattern verbatim instead of fabricating placeholders. Format HH:MM
  // (model has no realtime clock without tool access).
  const utcHHMM = new Date().toISOString().slice(11, 16);

  const parts: string[] = [];
  parts.push(`<event_payload>\n${safePayload}\n</event_payload>`);
  parts.push(`<operator_input>\n${safeBody}\n</operator_input>`);
  parts.push(`Current UTC time: ${utcHHMM} UTC`);
  parts.push("Rewrite the deterministic body in your first-person voice. Plain prose only.");
  return parts.join("\n\n");
}

/**
 * Validate LLM output. For events with an obvious structured field
 * (e.g. `merge_result.result.commitSha` if present), the output must contain
 * the verbatim hex string. For other events, validation = "non-empty assistant
 * text after trim".
 */
function validateOutput(out: string, event: OrchestratorEvent): boolean {
  const trimmed = out.trim();
  if (trimmed.length === 0) return false;

  // Structured-field check: merge_result with a hex commitSha must contain it.
  if (event.type === "merge_result") {
    const result = event.result as { commitSha?: unknown } | undefined;
    const sha = result?.commitSha;
    if (typeof sha === "string" && sha.length > 0 && /^[0-9a-f]+$/i.test(sha)) {
      // Accept either the full sha or a short prefix (>=7 chars) — many
      // deterministic bodies render only sha7. Be lenient: require any 7+
      // hex prefix substring of the full sha to appear in the output.
      const prefix = sha.slice(0, 7);
      if (!trimmed.toLowerCase().includes(prefix.toLowerCase())) return false;
    }
  }
  return true;
}

/**
 * Compute USD cost from token usage and model. Pulls from
 * `MODEL_PRICING_PER_MTOK`; falls through to Haiku rates for unknown models
 * so accounting never crashes on a config typo.
 */
export function computeCostUsd(
  model: string,
  usage: { input_tokens: number; output_tokens: number },
): number {
  const rates = MODEL_PRICING_PER_MTOK[model] ?? FALLBACK_PRICING;
  return (usage.input_tokens * rates.input + usage.output_tokens * rates.output) / 1_000_000;
}

// --- Generator ---

export class OutboundResponseGenerator {
  private readonly anthropic: Anthropic;
  private readonly prompts: Record<OutboundRole, string>;
  private readonly whitelist: ReadonlySet<string>;
  private readonly budget: LlmBudgetTracker;
  private readonly breaker: PerRoleCircuitBreaker;
  private readonly model: string;
  private readonly maxBudgetUsd: number;
  private readonly timeoutMs: number;
  private readonly maxOutputTokens: number;
  private readonly onEvent: ((e: OutboundGenEvent) => void) | undefined;

  // One-shot logging guards.
  private readonly breakerWarnedRoles: Set<OutboundRole> = new Set();
  private budgetWarnedUtcDate: string | null = null;

  constructor(opts: OutboundResponseGeneratorOpts) {
    const prompts: Partial<Record<OutboundRole, string>> = {};
    for (const role of ALL_ROLES) {
      const path = opts.promptPaths[role];
      if (!path || path.trim().length === 0) {
        throw new Error(
          `OutboundResponseGenerator: promptPaths.${role} is missing or empty`,
        );
      }
      const text = readFileSync(path, "utf-8");
      if (text.trim().length === 0) {
        throw new Error(
          `OutboundResponseGenerator: prompt file ${path} (role=${role}) is empty`,
        );
      }
      prompts[role] = text;
    }
    this.prompts = prompts as Record<OutboundRole, string>;
    this.anthropic = opts.anthropic;
    this.whitelist = opts.whitelist;
    this.budget = opts.budget;
    this.breaker = opts.circuitBreaker;
    this.model = opts.model ?? DEFAULT_OUTBOUND_MODEL;
    this.maxBudgetUsd = opts.maxBudgetUsd ?? DEFAULT_OUTBOUND_MAX_BUDGET_USD;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_OUTBOUND_TIMEOUT_MS;
    this.maxOutputTokens = opts.maxOutputTokens ?? DEFAULT_OUTBOUND_MAX_OUTPUT_TOKENS;
    this.onEvent = opts.onEvent;
  }

  /**
   * Generate a per-role rewrite of `deterministicBody`. NEVER throws — every
   * failure path returns the deterministic body verbatim.
   */
  async generate(input: OutboundGenerateInput): Promise<string> {
    try {
      return await this.generateInner(input);
    } catch {
      // Defensive catch-all: any uncaught throw still yields the deterministic
      // body. The notifier never sees a rejected promise from this method.
      return input.deterministicBody;
    }
  }

  // --- internals ---

  private generateInner(input: OutboundGenerateInput): Promise<string> {
    const { event, role, deterministicBody } = input;

    // 1. Whitelist guard — silent short-circuit, no log, no breaker, no API call.
    const key = `${event.type}::${role}`;
    if (!this.whitelist.has(key)) {
      this.onEvent?.({ kind: "fallback_whitelist", role, eventType: event.type });
      return Promise.resolve(deterministicBody);
    }

    // 2. Circuit breaker guard — log once per process per role.
    if (!this.breaker.isClosed(role)) {
      this.warnBreakerOnce(role);
      this.onEvent?.({ kind: "fallback_breaker", role });
      return Promise.resolve(deterministicBody);
    }

    // 3. Budget guard — log once per UTC day.
    if (!this.budget.canAfford(this.maxBudgetUsd)) {
      this.warnBudgetOnce();
      this.onEvent?.({ kind: "fallback_budget", spentUsd: this.budget.todaySpentUsd() });
      return Promise.resolve(deterministicBody);
    }

    return this.spawnAndCollect(input);
  }

  private async spawnAndCollect(input: OutboundGenerateInput): Promise<string> {
    const { event, role, deterministicBody } = input;
    const userPrompt = buildUserPrompt(input);
    const ac = new AbortController();
    const timeoutHandle = setTimeout(() => ac.abort(), this.timeoutMs);

    this.onEvent?.({ kind: "spawned", role, eventType: event.type });

    let response: Awaited<ReturnType<Anthropic["messages"]["create"]>>;
    try {
      response = await this.anthropic.messages.create(
        {
          model: this.model,
          max_tokens: this.maxOutputTokens,
          system: this.prompts[role],
          messages: [{ role: "user", content: userPrompt }],
        },
        { signal: ac.signal },
      );
    } catch (err) {
      clearTimeout(timeoutHandle);
      if (ac.signal.aborted) {
        this.breaker.recordFailure(role);
        this.onEvent?.({ kind: "fallback_timeout", role, eventType: event.type });
        return deterministicBody;
      }
      this.breaker.recordFailure(role);
      this.onEvent?.({
        kind: "fallback_api_error",
        role,
        eventType: event.type,
        err: err instanceof Error ? err.message : String(err),
      });
      return deterministicBody;
    } finally {
      clearTimeout(timeoutHandle);
    }

    // The streaming variant returns a stream; the non-streaming `create()` we
    // use here returns a `Message` whose `content` is `ContentBlock[]`.
    // Concatenate any text blocks. Cast through `unknown` because the SDK's
    // create() return is a generic union of streaming/non-streaming.
    const message = response as unknown as {
      content: Array<{ type: string; text?: string }>;
      usage: { input_tokens: number; output_tokens: number };
    };
    const text = message.content
      .filter((b): b is { type: "text"; text: string } => b.type === "text" && typeof b.text === "string")
      .map((b) => b.text)
      .join("")
      .trim();

    const costUsd = computeCostUsd(this.model, message.usage);

    if (costUsd > this.maxBudgetUsd) {
      // Breach; charge the actual cost (never refund) and treat as failure.
      this.budget.charge(costUsd);
      this.breaker.recordFailure(role);
      this.onEvent?.({ kind: "fallback_overspend", role, costUsd });
      return deterministicBody;
    }

    if (!validateOutput(text, event)) {
      this.budget.charge(costUsd);
      this.breaker.recordFailure(role);
      this.onEvent?.({ kind: "fallback_validation", role, eventType: event.type });
      return deterministicBody;
    }

    // Success path: charge actual cost, reset breaker counter, return truncated.
    this.budget.charge(costUsd);
    this.breaker.recordSuccess(role);
    const finalBody = truncateBody(text);
    this.onEvent?.({
      kind: "success",
      role,
      eventType: event.type,
      costUsd,
      outputChars: finalBody.length,
    });
    return finalBody;
  }

  private warnBreakerOnce(role: OutboundRole): void {
    if (this.breakerWarnedRoles.has(role)) return;
    this.breakerWarnedRoles.add(role);
    // eslint-disable-next-line no-console
    console.warn(
      `[OutboundResponseGenerator] circuit breaker OPEN for role=${role} — falling back to deterministic body for the rest of this process`,
    );
  }

  private warnBudgetOnce(): void {
    const today = new Date().toISOString().slice(0, 10);
    if (this.budgetWarnedUtcDate === today) return;
    this.budgetWarnedUtcDate = today;
    // eslint-disable-next-line no-console
    console.warn(
      `[OutboundResponseGenerator] daily LLM budget exhausted (spent=$${this.budget.todaySpentUsd().toFixed(4)}) — falling back to deterministic bodies until UTC date rollover`,
    );
  }
}
