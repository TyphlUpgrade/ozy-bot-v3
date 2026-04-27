/**
 * Wave E-γ — OutboundResponseGenerator unit tests.
 *
 * Covers the failure-semantics matrix from plan D1 plus per-call
 * instrumentation event emission (post-refactor to @anthropic-ai/sdk
 * Messages API):
 *   - Whitelist miss → returns deterministicBody, no API call, fallback_whitelist event
 *   - Circuit breaker open → returns deterministicBody, no API call, logs once, fallback_breaker event
 *   - Budget rejects → returns deterministicBody, no API call, logs once/UTC day, fallback_budget event
 *   - API throws → returns deterministicBody, breaker increments, fallback_api_error event
 *   - API timeout (abort) → returns deterministicBody, breaker increments, fallback_timeout event
 *   - Empty assistant text → returns deterministicBody, breaker increments, fallback_validation event
 *   - Schema validation fail (merge_result with sha but LLM omits hex) → fallback_validation event
 *   - Overspend (cost > maxBudgetUsd) → fallback_overspend event
 *   - Success → returns LLM output, breaker reset, budget charged, success event
 *   - Cost computation: per-model rates from MODEL_PRICING_PER_MTOK + fallback rates
 *
 * No live API / Discord traffic.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import type Anthropic from "@anthropic-ai/sdk";

import {
  OutboundResponseGenerator,
  computeCostUsd,
  type OutboundRole,
  type OutboundGenEvent,
} from "../../src/discord/outbound-response-generator.js";
import { LlmBudgetTracker, PerRoleCircuitBreaker } from "../../src/discord/llm-budget.js";
import { OUTBOUND_LLM_WHITELIST } from "../../src/discord/outbound-whitelist.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";

// --- Helpers ---

const VALID_PROMPT =
  "# voice\n\nFenced as <operator_input>...</operator_input> and <event_payload>...</event_payload>\n";

interface MockScenario {
  /** LLM-returned text (concatenated across content blocks). */
  text?: string;
  /** Multiple content blocks; if present, overrides `text`. */
  contentBlocks?: Array<{ type: string; text?: string }>;
  /** Token usage; defaults to 100/100. */
  usage?: { input_tokens: number; output_tokens: number };
  /** Throw a synchronous error from messages.create. */
  throwError?: Error;
  /**
   * Wait this many ms before resolving. Combined with timeoutMs, lets us
   * simulate AbortController-driven cancellation.
   */
  delayMs?: number;
}

interface MockTrace {
  callCount: number;
  calls: Array<{
    model: string;
    max_tokens: number;
    system: string;
    userPrompt: string;
  }>;
}

function makeMockAnthropic(scenarios: MockScenario[]): {
  anthropic: Anthropic;
  trace: MockTrace;
} {
  const trace: MockTrace = { callCount: 0, calls: [] };
  let i = 0;
  const create = vi.fn(async (params: {
    model: string;
    max_tokens: number;
    system: string;
    messages: Array<{ role: string; content: string }>;
  }, options?: { signal?: AbortSignal }) => {
    const s = scenarios[i] ?? scenarios[scenarios.length - 1];
    i += 1;
    trace.callCount += 1;
    trace.calls.push({
      model: params.model,
      max_tokens: params.max_tokens,
      system: params.system,
      userPrompt: params.messages[0]?.content ?? "",
    });
    if (s.delayMs && s.delayMs > 0) {
      await new Promise<void>((resolve, reject) => {
        const t = setTimeout(resolve, s.delayMs);
        options?.signal?.addEventListener("abort", () => {
          clearTimeout(t);
          const e = new Error("Request was aborted");
          e.name = "AbortError";
          reject(e);
        });
      });
    }
    if (s.throwError) {
      throw s.throwError;
    }
    return {
      id: "msg_mock",
      type: "message",
      role: "assistant",
      model: params.model,
      stop_reason: "end_turn",
      stop_sequence: null,
      content: s.contentBlocks ?? [{ type: "text", text: s.text ?? "default mocked outbound prose" }],
      usage: s.usage ?? { input_tokens: 100, output_tokens: 100 },
    };
  });
  const anthropic = { messages: { create } } as unknown as Anthropic;
  return { anthropic, trace };
}

let workDir: string;

function freshDir(prefix: string): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

function writePromptFiles(dir: string): Record<OutboundRole, string> {
  const promptDir = join(dir, "prompts");
  mkdirSync(promptDir, { recursive: true });
  const roles: OutboundRole[] = ["architect", "reviewer", "executor", "orchestrator"];
  const out: Partial<Record<OutboundRole, string>> = {};
  for (const role of roles) {
    const p = join(promptDir, `v1-${role}.md`);
    writeFileSync(p, VALID_PROMPT, "utf-8");
    out[role] = p;
  }
  return out as Record<OutboundRole, string>;
}

function makeFreshGen(opts: {
  anthropic: Anthropic;
  budget?: LlmBudgetTracker;
  breaker?: PerRoleCircuitBreaker;
  whitelist?: ReadonlySet<string>;
  timeoutMs?: number;
  maxBudgetUsd?: number;
  onEvent?: (e: OutboundGenEvent) => void;
  model?: string;
}): OutboundResponseGenerator {
  const promptPaths = writePromptFiles(workDir);
  return new OutboundResponseGenerator({
    anthropic: opts.anthropic,
    promptPaths,
    whitelist: opts.whitelist ?? OUTBOUND_LLM_WHITELIST,
    budget: opts.budget ?? new LlmBudgetTracker({ rootDir: workDir, dailyCapUsd: 5.0 }),
    circuitBreaker: opts.breaker ?? new PerRoleCircuitBreaker(),
    timeoutMs: opts.timeoutMs ?? 8_000,
    maxBudgetUsd: opts.maxBudgetUsd ?? 0.02,
    onEvent: opts.onEvent,
    model: opts.model,
  });
}

const SAMPLE_SESSION_COMPLETE: OrchestratorEvent = {
  type: "session_complete",
  taskId: "task-A",
  success: true,
  errors: [],
};

const DETERMINISTIC = "**Status:** success — built 3 files [normal_completion]";

function captureEvents(): { onEvent: (e: OutboundGenEvent) => void; events: OutboundGenEvent[] } {
  const events: OutboundGenEvent[] = [];
  return { onEvent: (e) => events.push(e), events };
}

// --- Cost computation tests ---

describe("computeCostUsd (per-model rates)", () => {
  it("uses Haiku 4.5 rates ($1/M input, $5/M output)", () => {
    expect(
      computeCostUsd("claude-haiku-4-5-20251001", { input_tokens: 1_000_000, output_tokens: 0 }),
    ).toBeCloseTo(1.0, 6);
    expect(
      computeCostUsd("claude-haiku-4-5-20251001", { input_tokens: 0, output_tokens: 1_000_000 }),
    ).toBeCloseTo(5.0, 6);
  });

  it("uses Sonnet 4.6 rates ($3/M input, $15/M output)", () => {
    expect(
      computeCostUsd("claude-sonnet-4-6", { input_tokens: 1_000_000, output_tokens: 0 }),
    ).toBeCloseTo(3.0, 6);
    expect(
      computeCostUsd("claude-sonnet-4-6", { input_tokens: 0, output_tokens: 1_000_000 }),
    ).toBeCloseTo(15.0, 6);
  });

  it("uses Opus 4.7 rates ($15/M input, $75/M output)", () => {
    expect(
      computeCostUsd("claude-opus-4-7", { input_tokens: 1_000_000, output_tokens: 0 }),
    ).toBeCloseTo(15.0, 6);
    expect(
      computeCostUsd("claude-opus-4-7", { input_tokens: 0, output_tokens: 1_000_000 }),
    ).toBeCloseTo(75.0, 6);
  });

  it("falls through to Haiku rates for an unknown model", () => {
    expect(
      computeCostUsd("model-not-in-table", { input_tokens: 1_000_000, output_tokens: 0 }),
    ).toBeCloseTo(1.0, 6);
    expect(
      computeCostUsd("model-not-in-table", { input_tokens: 0, output_tokens: 1_000_000 }),
    ).toBeCloseTo(5.0, 6);
  });
});

// --- Constructor tests ---

describe("OutboundResponseGenerator constructor", () => {
  beforeEach(() => {
    workDir = freshDir("orgen-ctor-");
  });
  afterEach(() => {
    rmSync(workDir, { recursive: true, force: true });
  });

  it("throws when a prompt file is missing on disk", () => {
    const promptPaths: Record<OutboundRole, string> = {
      architect: join(workDir, "missing-arch.md"),
      reviewer: join(workDir, "missing-rev.md"),
      executor: join(workDir, "missing-exec.md"),
      orchestrator: join(workDir, "missing-orch.md"),
    };
    const { anthropic } = makeMockAnthropic([{}]);
    expect(
      () =>
        new OutboundResponseGenerator({
          anthropic,
          promptPaths,
          whitelist: OUTBOUND_LLM_WHITELIST,
          budget: new LlmBudgetTracker({ rootDir: workDir }),
          circuitBreaker: new PerRoleCircuitBreaker(),
        }),
    ).toThrow();
  });

  it("throws when a prompt file exists but is empty", () => {
    const promptDir = join(workDir, "p");
    mkdirSync(promptDir, { recursive: true });
    const promptPaths: Record<OutboundRole, string> = {
      architect: join(promptDir, "a.md"),
      reviewer: join(promptDir, "r.md"),
      executor: join(promptDir, "e.md"),
      orchestrator: join(promptDir, "o.md"),
    };
    for (const path of Object.values(promptPaths)) {
      writeFileSync(path, "   \n  \n", "utf-8");
    }
    const { anthropic } = makeMockAnthropic([{}]);
    expect(
      () =>
        new OutboundResponseGenerator({
          anthropic,
          promptPaths,
          whitelist: OUTBOUND_LLM_WHITELIST,
          budget: new LlmBudgetTracker({ rootDir: workDir }),
          circuitBreaker: new PerRoleCircuitBreaker(),
        }),
    ).toThrow(/empty/);
  });
});

// --- Failure-path tests ---

describe("OutboundResponseGenerator.generate failure paths", () => {
  beforeEach(() => {
    workDir = freshDir("orgen-fail-");
  });
  afterEach(() => {
    rmSync(workDir, { recursive: true, force: true });
    vi.restoreAllMocks();
  });

  it("whitelist miss → returns deterministicBody, no API call, fallback_whitelist event", async () => {
    const { anthropic, trace } = makeMockAnthropic([{ text: "should not appear" }]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, onEvent });
    const out = await gen.generate({
      // poll_tick is NOT in the whitelist for any role.
      event: { type: "poll_tick" },
      role: "orchestrator",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    expect(trace.callCount).toBe(0);
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({
      kind: "fallback_whitelist",
      role: "orchestrator",
      eventType: "poll_tick",
    });
  });

  it("circuit breaker open → returns deterministicBody, no API call, logs once, fallback_breaker event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    breaker.recordFailure("executor");
    breaker.recordFailure("executor");
    breaker.recordFailure("executor");
    const { anthropic, trace } = makeMockAnthropic([{ text: "no-go" }]);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out1 = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    const out2 = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out1).toBe(DETERMINISTIC);
    expect(out2).toBe(DETERMINISTIC);
    expect(trace.callCount).toBe(0);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0]?.[0]).toMatch(/circuit breaker OPEN.*role=executor/);
    // Both calls fire breaker events (no log debouncing on event emission).
    expect(events.filter((e) => e.kind === "fallback_breaker")).toHaveLength(2);
  });

  it("budget rejects → returns deterministicBody, no API call, logs once per UTC day, fallback_budget event", async () => {
    const budget = new LlmBudgetTracker({ rootDir: workDir, dailyCapUsd: 0.01 });
    budget.charge(0.009); // leave only $0.001 — under the per-call $0.02 cap
    const { anthropic, trace } = makeMockAnthropic([{ text: "irrelevant" }]);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, budget, onEvent });
    const out1 = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    const out2 = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out1).toBe(DETERMINISTIC);
    expect(out2).toBe(DETERMINISTIC);
    expect(trace.callCount).toBe(0);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0]?.[0]).toMatch(/daily LLM budget exhausted/);
    expect(events.filter((e) => e.kind === "fallback_budget")).toHaveLength(2);
  });

  it("API throws → returns deterministicBody, breaker increments, fallback_api_error event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const boom = new Error("connection reset");
    const { anthropic } = makeMockAnthropic([
      { throwError: boom },
      { throwError: boom },
      { throwError: boom },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    for (let i = 0; i < 3; i++) {
      const out = await gen.generate({
        event: SAMPLE_SESSION_COMPLETE,
        role: "executor",
        deterministicBody: DETERMINISTIC,
      });
      expect(out).toBe(DETERMINISTIC);
    }
    expect(breaker.isClosed("executor")).toBe(false);
    const apiErrors = events.filter((e) => e.kind === "fallback_api_error");
    expect(apiErrors).toHaveLength(3);
    expect(apiErrors[0]).toMatchObject({
      kind: "fallback_api_error",
      role: "executor",
      eventType: "session_complete",
      err: "connection reset",
    });
  });

  it("API timeout (abort) → returns deterministicBody, breaker increments, fallback_timeout event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const { anthropic } = makeMockAnthropic([{ delayMs: 1000 }]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent, timeoutMs: 5 });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    // One failure recorded; breaker still closed (only 1 of 3 strikes).
    expect(breaker.isClosed("executor")).toBe(true);
    expect(events.some((e) => e.kind === "fallback_timeout")).toBe(true);
  });

  it("empty assistant text → returns deterministicBody, breaker increments, fallback_validation event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const { anthropic } = makeMockAnthropic([
      { text: "   \n  ", usage: { input_tokens: 10, output_tokens: 5 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    expect(breaker.isClosed("executor")).toBe(true); // still closed (1 strike)
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("schema validation fail (merge_result with sha; LLM omits hex) → fallback + breaker++ + fallback_validation event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event: OrchestratorEvent = {
      type: "merge_result",
      taskId: "task-M",
      result: { commitSha: "abc1234def5678" } as unknown as OrchestratorEvent["result" & keyof OrchestratorEvent],
    } as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      {
        text: "I merged the change cleanly — but I will not name the sha.",
        usage: { input_tokens: 50, output_tokens: 20 },
      },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({
      event,
      role: "orchestrator",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("overspend (cost > maxBudgetUsd) → fallback + fallback_overspend event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    // 100M tokens × Haiku output rate ($5/M) = $500 → way over the $0.02 cap.
    const { anthropic } = makeMockAnthropic([
      {
        text: "but the overspend hits before validation",
        usage: { input_tokens: 0, output_tokens: 100_000_000 },
      },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    const overspend = events.find((e) => e.kind === "fallback_overspend");
    expect(overspend).toBeDefined();
    if (overspend && overspend.kind === "fallback_overspend") {
      expect(overspend.role).toBe("executor");
      expect(overspend.costUsd).toBeGreaterThan(0.02);
    }
  });
});

// --- Success-path tests ---

describe("OutboundResponseGenerator.generate success path", () => {
  beforeEach(() => {
    workDir = freshDir("orgen-success-");
  });
  afterEach(() => {
    rmSync(workDir, { recursive: true, force: true });
  });

  it("returns LLM output (truncated), breaker reset, budget charged, success event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    breaker.recordFailure("executor"); // pre-existing 1 strike
    const budget = new LlmBudgetTracker({ rootDir: workDir, dailyCapUsd: 5.0 });
    const llmOutput = "I built task-A across 3 files; handing off to the reviewer.";
    // 1000 input + 500 output Haiku → (1000*1 + 500*5) / 1e6 = 0.0035 USD
    const { anthropic, trace } = makeMockAnthropic([
      { text: llmOutput, usage: { input_tokens: 1000, output_tokens: 500 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, budget, onEvent });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(llmOutput);
    expect(budget.todaySpentUsd()).toBeCloseTo(0.0035, 6);
    // After success, a fresh strike should NOT immediately open the breaker
    // (counter was reset on success).
    breaker.recordFailure("executor");
    breaker.recordFailure("executor");
    expect(breaker.isClosed("executor")).toBe(true);
    breaker.recordFailure("executor");
    expect(breaker.isClosed("executor")).toBe(false);

    // messages.create called once with the right shape.
    expect(trace.callCount).toBe(1);
    const call = trace.calls[0];
    expect(call.model).toBe("claude-haiku-4-5-20251001");
    expect(call.max_tokens).toBe(800);
    expect(call.system).toContain("voice"); // VALID_PROMPT marker
    expect(call.userPrompt).toContain("<event_payload>");
    expect(call.userPrompt).toContain("<operator_input>");
    // UTC time injection (so v2 prompts can render `HH:MM UTC` headers
    // verbatim instead of fabricating placeholders).
    expect(call.userPrompt).toMatch(/Current UTC time: \d{2}:\d{2} UTC/);

    // Instrumentation: spawned + success.
    const spawned = events.find((e) => e.kind === "spawned");
    expect(spawned).toEqual({ kind: "spawned", role: "executor", eventType: "session_complete" });
    const success = events.find((e) => e.kind === "success");
    expect(success).toBeDefined();
    if (success && success.kind === "success") {
      expect(success.role).toBe("executor");
      expect(success.eventType).toBe("session_complete");
      expect(success.costUsd).toBeCloseTo(0.0035, 6);
      expect(success.outputChars).toBe(llmOutput.length);
    }
  });

  it("merge_result success path: LLM output containing sha7 prefix passes validation", async () => {
    const event = {
      type: "merge_result",
      taskId: "task-M",
      result: { commitSha: "abc1234def5678" },
    } as unknown as OrchestratorEvent;
    const llmOutput = "Merged at sha abc1234 cleanly — proceeding with the next phase.";
    const { anthropic } = makeMockAnthropic([
      { text: llmOutput, usage: { input_tokens: 100, output_tokens: 50 } },
    ]);
    const gen = makeFreshGen({ anthropic });
    const out = await gen.generate({
      event,
      role: "orchestrator",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(llmOutput);
  });

  it("truncates LLM output above Discord 1900-char cap", async () => {
    // Prefix carries the required taskId substring so validation passes;
    // the rest is filler to exceed the cap.
    const long = "task-A " + "x".repeat(2500);
    const { anthropic } = makeMockAnthropic([
      { text: long, usage: { input_tokens: 100, output_tokens: 100 } },
    ]);
    const gen = makeFreshGen({ anthropic });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out.length).toBeLessThanOrEqual(1900);
    expect(out.endsWith("…")).toBe(true);
  });

  it("concatenates multiple text blocks before validation", async () => {
    const { anthropic } = makeMockAnthropic([
      {
        contentBlocks: [
          { type: "text", text: "first chunk for task-A " },
          { type: "text", text: "second chunk." },
        ],
        usage: { input_tokens: 10, output_tokens: 10 },
      },
    ]);
    const gen = makeFreshGen({ anthropic });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe("first chunk for task-A second chunk.");
  });

  // --- Per-event validation rules (MEDIUM-1 tightening) ---

  it("task_done validation: LLM omits taskId → fallback + fallback_validation event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event: OrchestratorEvent = { type: "task_done", taskId: "task-OMIT" };
    const { anthropic } = makeMockAnthropic([
      { text: "I finished the work but forgot to name the id.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "executor", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("session_complete validation: LLM omits taskId → fallback + fallback_validation event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event: OrchestratorEvent = {
      type: "session_complete",
      taskId: "task-SESS",
      success: true,
      errors: [],
    };
    const { anthropic } = makeMockAnthropic([
      { text: "Session is complete; ready for review.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "executor", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("review_mandatory validation: LLM omits taskId → fallback + fallback_validation event", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event: OrchestratorEvent = {
      type: "review_mandatory",
      taskId: "task-REV",
      projectId: "P-rev",
    } as unknown as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      { text: "Review pending without an id.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "reviewer", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("review_arbitration_entered validation: LLM omits taskId → fallback", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event = {
      type: "review_arbitration_entered",
      taskId: "task-ARB",
      projectId: "P-arb",
      reviewerRejectionCount: 2,
    } as unknown as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      { text: "Arbitration entered without naming the task id.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "reviewer", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("architect_arbitration_fired validation: LLM omits taskId → fallback", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event = {
      type: "architect_arbitration_fired",
      taskId: "task-AAF",
      projectId: "P-aaf",
      cause: "no-progress",
    } as unknown as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      { text: "Architect arbitration without an id reference.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "architect", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("escalation_needed validation: LLM omits taskId → fallback", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event = {
      type: "escalation_needed",
      taskId: "task-ESC",
      escalationType: "scope_unclear",
      reason: "missing context",
    } as unknown as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      { text: "Escalation needed but I withhold the id.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "orchestrator", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("arbitration_verdict validation: LLM omits taskId → fallback", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event = {
      type: "arbitration_verdict",
      taskId: "task-AV",
      projectId: "P-av",
      verdict: "approve",
      rationale: "looks good",
    } as unknown as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      { text: "Verdict approve but no id mention.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "architect", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("arbitration_verdict validation: LLM has taskId but omits verdict literal → fallback", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event = {
      type: "arbitration_verdict",
      taskId: "task-AV2",
      projectId: "P-av2",
      verdict: "reject",
      rationale: "issues found",
    } as unknown as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      { text: "Decision on task-AV2 has been recorded.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "architect", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("project_decomposed validation: LLM omits projectId → fallback", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event = {
      type: "project_decomposed",
      projectId: "P-DEC",
      phaseCount: 4,
    } as unknown as OrchestratorEvent;
    const { anthropic } = makeMockAnthropic([
      { text: "Decomposed the project into 4 phases.", usage: { input_tokens: 50, output_tokens: 20 } },
    ]);
    const { onEvent, events } = captureEvents();
    const gen = makeFreshGen({ anthropic, breaker, onEvent });
    const out = await gen.generate({ event, role: "architect", deterministicBody: DETERMINISTIC });
    expect(out).toBe(DETERMINISTIC);
    expect(events.some((e) => e.kind === "fallback_validation")).toBe(true);
  });

  it("arbitration_verdict success: LLM includes both taskId and verdict literal", async () => {
    const event = {
      type: "arbitration_verdict",
      taskId: "task-OK",
      projectId: "P-ok",
      verdict: "approve",
      rationale: "ok",
    } as unknown as OrchestratorEvent;
    const llmOutput = "On task-OK my verdict is approve — moving on.";
    const { anthropic } = makeMockAnthropic([
      { text: llmOutput, usage: { input_tokens: 100, output_tokens: 50 } },
    ]);
    const gen = makeFreshGen({ anthropic });
    const out = await gen.generate({ event, role: "architect", deterministicBody: DETERMINISTIC });
    expect(out).toBe(llmOutput);
  });

  it("project_decomposed success: LLM includes projectId verbatim", async () => {
    const event = {
      type: "project_decomposed",
      projectId: "P-OK-DEC",
      phaseCount: 2,
    } as unknown as OrchestratorEvent;
    const llmOutput = "I have decomposed P-OK-DEC into two phases.";
    const { anthropic } = makeMockAnthropic([
      { text: llmOutput, usage: { input_tokens: 100, output_tokens: 50 } },
    ]);
    const gen = makeFreshGen({ anthropic });
    const out = await gen.generate({ event, role: "architect", deterministicBody: DETERMINISTIC });
    expect(out).toBe(llmOutput);
  });

  it("injects a `Current UTC time: HH:MM UTC` line into the user prompt", async () => {
    const { anthropic, trace } = makeMockAnthropic([
      { text: "ok task-A", usage: { input_tokens: 10, output_tokens: 5 } },
    ]);
    const gen = makeFreshGen({ anthropic });
    await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(trace.callCount).toBe(1);
    const userPrompt = trace.calls[0].userPrompt;
    // The injected line must match the documented contract format exactly so
    // v2 prompts can substring-extract `HH:MM UTC` for their section header.
    const lines = userPrompt.split("\n");
    const utcLine = lines.find((l) => l.startsWith("Current UTC time: "));
    expect(utcLine).toBeDefined();
    expect(utcLine).toMatch(/^Current UTC time: \d{2}:\d{2} UTC$/);
  });
});
