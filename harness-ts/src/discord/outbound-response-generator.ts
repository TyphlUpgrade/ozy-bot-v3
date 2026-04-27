/**
 * Wave E-γ — outbound LLM voice generator.
 *
 * Mirrors `LlmResponseGenerator` (`src/discord/response-generator.ts:192`) but
 * for the outbound notifier path: per-role first-person prose rewrites of
 * deterministic event bodies. Differences:
 *   - Per-role system prompts (Architect / Reviewer / Executor / Orchestrator)
 *     loaded eagerly at construction; throws on missing or empty file (D9).
 *   - Whitelist guard: non-eligible (event.type, role) tuples short-circuit
 *     to the deterministic body without spawning the SDK (D5).
 *   - Daily budget tracker + per-role circuit breaker as runtime guards (D3, D4).
 *   - Replacement-with-fallback semantic: on ANY failure path, returns
 *     `deterministicBody` verbatim. NEVER throws.
 *
 * Security posture mirrors `LlmResponseGenerator`:
 *   - Deterministic body fenced inside `<operator_input>` tags. Event payload
 *     fenced inside `<event_payload>` tags. The system prompt classifies fenced
 *     content as DATA, not instruction.
 *   - `allowedTools: []` and explicit `disallowedTools` block any tool use.
 *   - `maxTurns: 1` keeps the call single-shot.
 *   - Budget + wall-clock bounded. SDK / parse / timeout failures fall back.
 *
 * Failure semantics inside `generate()`:
 *   - whitelist miss → return deterministicBody (no spawn, no log, no breaker)
 *   - circuit breaker open → return deterministicBody; log ONCE per process
 *     per role
 *   - budget rejects → return deterministicBody; log ONCE per UTC day to
 *     console.warn
 *   - SDK throws / abort / non-text → increment breaker; return deterministicBody
 *   - schema validation fail → increment breaker; return deterministicBody
 *   - any uncaught throw → swallow, return deterministicBody
 */

import { readFileSync } from "node:fs";

import type { OrchestratorEvent } from "../orchestrator.js";
import type { SDKClient } from "../session/sdk.js";

import { LlmBudgetTracker, PerRoleCircuitBreaker } from "./llm-budget.js";

// --- Public types ---

export type OutboundRole = "architect" | "reviewer" | "executor" | "orchestrator";

export interface OutboundResponseGeneratorOpts {
  sdk: SDKClient;
  cwd: string;
  promptPaths: Record<OutboundRole, string>;
  whitelist: ReadonlySet<string>;
  budget: LlmBudgetTracker;
  circuitBreaker: PerRoleCircuitBreaker;
  model?: string;
  maxBudgetUsd?: number;
  timeoutMs?: number;
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

const ALL_ROLES: readonly OutboundRole[] = [
  "architect",
  "reviewer",
  "executor",
  "orchestrator",
];

const DISCORD_BODY_MAX_CHARS = 1900;

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

  const parts: string[] = [];
  parts.push(`<event_payload>\n${safePayload}\n</event_payload>`);
  parts.push(`<operator_input>\n${safeBody}\n</operator_input>`);
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

// --- Generator ---

export class OutboundResponseGenerator {
  private readonly sdk: SDKClient;
  private readonly cwd: string;
  private readonly prompts: Record<OutboundRole, string>;
  private readonly whitelist: ReadonlySet<string>;
  private readonly budget: LlmBudgetTracker;
  private readonly breaker: PerRoleCircuitBreaker;
  private readonly model: string;
  private readonly maxBudgetUsd: number;
  private readonly timeoutMs: number;

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
    this.sdk = opts.sdk;
    this.cwd = opts.cwd;
    this.whitelist = opts.whitelist;
    this.budget = opts.budget;
    this.breaker = opts.circuitBreaker;
    this.model = opts.model ?? DEFAULT_OUTBOUND_MODEL;
    this.maxBudgetUsd = opts.maxBudgetUsd ?? DEFAULT_OUTBOUND_MAX_BUDGET_USD;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_OUTBOUND_TIMEOUT_MS;
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

    // 1. Whitelist guard — silent short-circuit, no log, no breaker, no spawn.
    const key = `${event.type}::${role}`;
    if (!this.whitelist.has(key)) {
      return Promise.resolve(deterministicBody);
    }

    // 2. Circuit breaker guard — log once per process per role.
    if (!this.breaker.isClosed(role)) {
      this.warnBreakerOnce(role);
      return Promise.resolve(deterministicBody);
    }

    // 3. Budget guard — log once per UTC day.
    if (!this.budget.canAfford(this.maxBudgetUsd)) {
      this.warnBudgetOnce();
      return Promise.resolve(deterministicBody);
    }

    return this.spawnAndCollect(input);
  }

  private async spawnAndCollect(input: OutboundGenerateInput): Promise<string> {
    const { event, role, deterministicBody } = input;
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
        systemPrompt: this.prompts[role],
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
        this.breaker.recordFailure(role);
        return deterministicBody;
      }
      sessionResult = raced as Awaited<ReturnType<SDKClient["consumeStream"]>>;
    } catch {
      this.breaker.recordFailure(role);
      return deterministicBody;
    } finally {
      clearTimeout(timeoutHandle);
      ac.signal.removeEventListener("abort", onAbort);
      timeoutResolverHolder.resolve?.("settled");
    }

    if (!sessionResult || !sessionResult.success) {
      this.breaker.recordFailure(role);
      return deterministicBody;
    }
    if (sessionResult.totalCostUsd > this.maxBudgetUsd) {
      // Breach; charge the actual cost (never refund) and treat as failure.
      this.budget.charge(sessionResult.totalCostUsd);
      this.breaker.recordFailure(role);
      return deterministicBody;
    }

    const out = (sessionResult.result ?? "").trim();
    if (!validateOutput(out, event)) {
      this.budget.charge(sessionResult.totalCostUsd);
      this.breaker.recordFailure(role);
      return deterministicBody;
    }

    // Success path: charge actual cost, reset breaker counter, return truncated.
    this.budget.charge(sessionResult.totalCostUsd);
    this.breaker.recordSuccess(role);
    return truncateBody(out);
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
