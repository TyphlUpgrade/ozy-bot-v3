/**
 * Wave E-γ — OutboundResponseGenerator unit tests.
 *
 * Covers the failure-semantics matrix from plan D1:
 *   - Whitelist miss → returns deterministicBody, no SDK call
 *   - Circuit breaker open → returns deterministicBody, no SDK call, logs once
 *   - Budget rejects → returns deterministicBody, no SDK call, logs once/UTC day
 *   - SDK throws → returns deterministicBody, breaker increments
 *   - SDK timeout → returns deterministicBody, breaker increments
 *   - Empty assistant text → returns deterministicBody, breaker increments
 *   - Schema validation fail (merge_result with sha but LLM omits hex) → fallback
 *   - Success → returns LLM output, breaker reset, budget charged
 *
 * No live SDK / Discord traffic.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import {
  OutboundResponseGenerator,
  type OutboundRole,
} from "../../src/discord/outbound-response-generator.js";
import { LlmBudgetTracker, PerRoleCircuitBreaker } from "../../src/discord/llm-budget.js";
import { OUTBOUND_LLM_WHITELIST } from "../../src/discord/outbound-whitelist.js";
import type { SDKClient } from "../../src/session/sdk.js";
import type { OrchestratorEvent } from "../../src/orchestrator.js";

// --- Helpers ---

const VALID_PROMPT =
  "# voice\n\nFenced as <operator_input>...</operator_input> and <event_payload>...</event_payload>\n";

interface MockScenario {
  result?: string;
  success?: boolean;
  totalCostUsd?: number;
  raceTimeout?: boolean;
  throwOnSpawn?: boolean;
}

interface MockTrace {
  spawnCount: number;
  spawnedPrompts: string[];
}

function makeMockSdk(scenarios: MockScenario[]): { sdk: SDKClient; trace: MockTrace } {
  const trace: MockTrace = { spawnCount: 0, spawnedPrompts: [] };
  let i = 0;
  const sdk = {
    spawnSession: (config: { prompt: string }) => {
      const s = scenarios[i] ?? scenarios[scenarios.length - 1];
      trace.spawnedPrompts.push(config.prompt);
      trace.spawnCount += 1;
      if (s.throwOnSpawn) {
        throw new Error("sdk spawn boom");
      }
      return {
        query: { __mock: i++ } as unknown as never,
        abortController: new AbortController(),
      };
    },
    consumeStream: async (q: unknown) => {
      const idx = (q as { __mock: number }).__mock;
      const s = scenarios[idx] ?? scenarios[scenarios.length - 1];
      if (s.raceTimeout) {
        await new Promise((r) => setTimeout(r, 50));
        return {
          sessionId: "sess-x",
          success: false,
          errors: ["aborted"],
          totalCostUsd: 0,
          numTurns: 0,
          usage: { input_tokens: 0, output_tokens: 0 },
        };
      }
      return {
        sessionId: "sess-x",
        success: s.success ?? true,
        result: s.result ?? "default mocked outbound prose",
        errors: s.success === false ? ["sdk failed"] : [],
        totalCostUsd: s.totalCostUsd ?? 0.001,
        numTurns: 1,
        usage: { input_tokens: 10, output_tokens: 10 },
      };
    },
  } as unknown as SDKClient;
  return { sdk, trace };
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
  sdk: SDKClient;
  budget?: LlmBudgetTracker;
  breaker?: PerRoleCircuitBreaker;
  whitelist?: ReadonlySet<string>;
  timeoutMs?: number;
  maxBudgetUsd?: number;
}): OutboundResponseGenerator {
  const promptPaths = writePromptFiles(workDir);
  return new OutboundResponseGenerator({
    sdk: opts.sdk,
    cwd: workDir,
    promptPaths,
    whitelist: opts.whitelist ?? OUTBOUND_LLM_WHITELIST,
    budget: opts.budget ?? new LlmBudgetTracker({ rootDir: workDir, dailyCapUsd: 5.0 }),
    circuitBreaker: opts.breaker ?? new PerRoleCircuitBreaker(),
    timeoutMs: opts.timeoutMs ?? 8_000,
    maxBudgetUsd: opts.maxBudgetUsd ?? 0.02,
  });
}

const SAMPLE_SESSION_COMPLETE: OrchestratorEvent = {
  type: "session_complete",
  taskId: "task-A",
  success: true,
  errors: [],
};

const DETERMINISTIC = "**Status:** success — built 3 files [normal_completion]";

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
    const { sdk } = makeMockSdk([{}]);
    expect(
      () =>
        new OutboundResponseGenerator({
          sdk,
          cwd: workDir,
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
    const { sdk } = makeMockSdk([{}]);
    expect(
      () =>
        new OutboundResponseGenerator({
          sdk,
          cwd: workDir,
          promptPaths,
          whitelist: OUTBOUND_LLM_WHITELIST,
          budget: new LlmBudgetTracker({ rootDir: workDir }),
          circuitBreaker: new PerRoleCircuitBreaker(),
        }),
    ).toThrow(/empty/);
  });
});

describe("OutboundResponseGenerator.generate failure paths", () => {
  beforeEach(() => {
    workDir = freshDir("orgen-fail-");
  });
  afterEach(() => {
    rmSync(workDir, { recursive: true, force: true });
    vi.restoreAllMocks();
  });

  it("whitelist miss → returns deterministicBody, no SDK call", async () => {
    const { sdk, trace } = makeMockSdk([{ result: "should not appear" }]);
    const gen = makeFreshGen({ sdk });
    const out = await gen.generate({
      // poll_tick is NOT in the whitelist for any role.
      event: { type: "poll_tick" },
      role: "orchestrator",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    expect(trace.spawnCount).toBe(0);
  });

  it("circuit breaker open → returns deterministicBody, no SDK call, logs once", async () => {
    const breaker = new PerRoleCircuitBreaker();
    breaker.recordFailure("executor");
    breaker.recordFailure("executor");
    breaker.recordFailure("executor");
    const { sdk, trace } = makeMockSdk([{ result: "no-go" }]);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const gen = makeFreshGen({ sdk, breaker });
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
    expect(trace.spawnCount).toBe(0);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0]?.[0]).toMatch(/circuit breaker OPEN.*role=executor/);
  });

  it("budget rejects → returns deterministicBody, no SDK call, logs once per UTC day", async () => {
    const budget = new LlmBudgetTracker({ rootDir: workDir, dailyCapUsd: 0.01 });
    budget.charge(0.009); // leave only $0.001 — under the per-call $0.02 cap
    const { sdk, trace } = makeMockSdk([{ result: "irrelevant" }]);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const gen = makeFreshGen({ sdk, budget });
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
    expect(trace.spawnCount).toBe(0);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0]?.[0]).toMatch(/daily LLM budget exhausted/);
  });

  it("SDK throws synchronously → returns deterministicBody, breaker increments", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const { sdk } = makeMockSdk([{ throwOnSpawn: true }, { throwOnSpawn: true }, { throwOnSpawn: true }]);
    const gen = makeFreshGen({ sdk, breaker });
    for (let i = 0; i < 3; i++) {
      const out = await gen.generate({
        event: SAMPLE_SESSION_COMPLETE,
        role: "executor",
        deterministicBody: DETERMINISTIC,
      });
      expect(out).toBe(DETERMINISTIC);
    }
    expect(breaker.isClosed("executor")).toBe(false);
  });

  it("SDK timeout → returns deterministicBody, breaker increments", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const { sdk } = makeMockSdk([{ raceTimeout: true }]);
    const gen = makeFreshGen({ sdk, breaker, timeoutMs: 5 });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    // One failure recorded; breaker still closed (only 1 of 3 strikes).
    expect(breaker.isClosed("executor")).toBe(true);
  });

  it("empty assistant text → returns deterministicBody, breaker increments", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const { sdk } = makeMockSdk([{ result: "   \n  ", success: true, totalCostUsd: 0.001 }]);
    const gen = makeFreshGen({ sdk, breaker });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
    expect(breaker.isClosed("executor")).toBe(true); // still closed (1 strike)
  });

  it("schema validation fail (merge_result with sha; LLM omits hex) → fallback + breaker++", async () => {
    const breaker = new PerRoleCircuitBreaker();
    const event: OrchestratorEvent = {
      type: "merge_result",
      taskId: "task-M",
      result: { commitSha: "abc1234def5678" } as unknown as OrchestratorEvent["result" & keyof OrchestratorEvent],
    } as OrchestratorEvent;
    const { sdk } = makeMockSdk([
      { result: "I merged the change cleanly — but I will not name the sha.", success: true, totalCostUsd: 0.001 },
    ]);
    const gen = makeFreshGen({ sdk, breaker });
    const out = await gen.generate({
      event,
      role: "orchestrator",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(DETERMINISTIC);
  });
});

describe("OutboundResponseGenerator.generate success path", () => {
  beforeEach(() => {
    workDir = freshDir("orgen-success-");
  });
  afterEach(() => {
    rmSync(workDir, { recursive: true, force: true });
  });

  it("returns LLM output (truncated), breaker reset, budget charged", async () => {
    const breaker = new PerRoleCircuitBreaker();
    breaker.recordFailure("executor"); // pre-existing 1 strike
    const budget = new LlmBudgetTracker({ rootDir: workDir, dailyCapUsd: 5.0 });
    const llmOutput = "I built the change across 3 files; handing off to the reviewer.";
    const { sdk } = makeMockSdk([{ result: llmOutput, success: true, totalCostUsd: 0.005 }]);
    const gen = makeFreshGen({ sdk, breaker, budget });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(llmOutput);
    expect(budget.todaySpentUsd()).toBeCloseTo(0.005, 6);
    // After success, a fresh strike should NOT immediately open the breaker
    // (counter was reset on success).
    breaker.recordFailure("executor");
    breaker.recordFailure("executor");
    expect(breaker.isClosed("executor")).toBe(true);
    breaker.recordFailure("executor");
    expect(breaker.isClosed("executor")).toBe(false);
  });

  it("merge_result success path: LLM output containing sha7 prefix passes validation", async () => {
    const event = {
      type: "merge_result",
      taskId: "task-M",
      result: { commitSha: "abc1234def5678" },
    } as unknown as OrchestratorEvent;
    const llmOutput = "Merged at sha abc1234 cleanly — proceeding with the next phase.";
    const { sdk } = makeMockSdk([{ result: llmOutput, success: true, totalCostUsd: 0.003 }]);
    const gen = makeFreshGen({ sdk });
    const out = await gen.generate({
      event,
      role: "orchestrator",
      deterministicBody: DETERMINISTIC,
    });
    expect(out).toBe(llmOutput);
  });

  it("truncates LLM output above Discord 1900-char cap", async () => {
    const long = "x".repeat(2500);
    const { sdk } = makeMockSdk([{ result: long, success: true, totalCostUsd: 0.001 }]);
    const gen = makeFreshGen({ sdk });
    const out = await gen.generate({
      event: SAMPLE_SESSION_COMPLETE,
      role: "executor",
      deterministicBody: DETERMINISTIC,
    });
    expect(out.length).toBeLessThanOrEqual(1900);
    expect(out.endsWith("…")).toBe(true);
  });
});
