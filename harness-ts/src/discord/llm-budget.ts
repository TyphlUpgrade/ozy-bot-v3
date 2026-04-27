/**
 * Wave E-γ — LLM budget + circuit-breaker runtime guards.
 *
 * Two co-located runtime guards used by `OutboundResponseGenerator`:
 *
 * - `LlmBudgetTracker` — daily UTC LLM spend cap, persisted to
 *   `<rootDir>/.harness/llm-budget.json`. Atomic temp+rename per
 *   `state.ts:180-188`. Roll-over on UTC date change.
 * - `PerRoleCircuitBreaker` — in-memory consecutive-failure counter per
 *   outbound role. After 3 strikes, the breaker opens and stays open until
 *   process restart (Wave E.4 spec: "in-memory state, resets on restart").
 *
 * Failure semantics for both: never throw out of public methods. File parse
 * failure on the budget tracker → start fresh for current UTC day (R5
 * mitigation: refusing to spend on a corrupt file would deadlock the
 * pipeline).
 */

import { readFileSync, writeFileSync, renameSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";

import type { OutboundRole } from "./outbound-response-generator.js";

// --- Constants ---

const DEFAULT_DAILY_CAP_USD = 5.0;
const FAILURE_THRESHOLD = 3;
const BUDGET_FILENAME = "llm-budget.json";
const BUDGET_DIRNAME = ".harness";
const SCHEMA_VERSION = 1;

// --- Budget tracker types ---

interface BudgetFile {
  schemaVersion: number;
  currentUtcDate: string; // YYYY-MM-DD
  spentUsd: number;
  dailyCapUsd: number;
  lastUpdatedAt: string; // ISO8601
}

export interface LlmBudgetTrackerOpts {
  rootDir: string;
  dailyCapUsd?: number;
}

// --- Helpers ---

/** Returns current UTC date as YYYY-MM-DD. Pure — easy to mock in tests. */
function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

function nowIso(): string {
  return new Date().toISOString();
}

// --- LlmBudgetTracker ---

export class LlmBudgetTracker {
  private readonly filePath: string;
  private readonly dailyCapUsd: number;
  private state: BudgetFile;
  private parseFailureWarned = false;

  constructor(opts: LlmBudgetTrackerOpts) {
    const dir = join(opts.rootDir, BUDGET_DIRNAME);
    this.filePath = join(dir, BUDGET_FILENAME);
    this.dailyCapUsd = opts.dailyCapUsd ?? DEFAULT_DAILY_CAP_USD;
    this.state = this.loadOrInit();
  }

  /** Returns true if a $cost charge would NOT exceed today's cap. */
  canAfford(estimatedCostUsd: number): boolean {
    // Snapshot today's date once so that a UTC midnight crossing mid-call
    // can't make the rollover and the cap-check disagree.
    const today = todayUtc();
    this.rolloverIfNeeded(today);
    return this.state.spentUsd + estimatedCostUsd <= this.state.dailyCapUsd;
  }

  /** Atomically increments today's spent. Never refunds. */
  charge(actualCostUsd: number): void {
    if (actualCostUsd <= 0) return;
    // Same date snapshot as canAfford so a long-running daemon's spend can't
    // get applied to the wrong day if it straddles UTC midnight.
    const today = todayUtc();
    this.rolloverIfNeeded(today);
    this.state.spentUsd += actualCostUsd;
    this.state.lastUpdatedAt = nowIso();
    this.persist();
  }

  /** Current UTC-day spent. */
  todaySpentUsd(): number {
    const today = todayUtc();
    this.rolloverIfNeeded(today);
    return this.state.spentUsd;
  }

  // --- internals ---

  private loadOrInit(): BudgetFile {
    if (!existsSync(this.filePath)) {
      const fresh: BudgetFile = {
        schemaVersion: SCHEMA_VERSION,
        currentUtcDate: todayUtc(),
        spentUsd: 0,
        dailyCapUsd: this.dailyCapUsd,
        lastUpdatedAt: nowIso(),
      };
      this.state = fresh;
      this.persist();
      return fresh;
    }
    try {
      const raw = readFileSync(this.filePath, "utf-8");
      const parsed = JSON.parse(raw) as Partial<BudgetFile>;
      if (
        typeof parsed.currentUtcDate !== "string" ||
        typeof parsed.spentUsd !== "number" ||
        typeof parsed.dailyCapUsd !== "number"
      ) {
        return this.warnAndFreshen();
      }
      return {
        schemaVersion: SCHEMA_VERSION,
        currentUtcDate: parsed.currentUtcDate,
        spentUsd: parsed.spentUsd,
        dailyCapUsd: this.dailyCapUsd, // constructor wins; honor latest config
        lastUpdatedAt: typeof parsed.lastUpdatedAt === "string" ? parsed.lastUpdatedAt : nowIso(),
      };
    } catch {
      return this.warnAndFreshen();
    }
  }

  private warnAndFreshen(): BudgetFile {
    if (!this.parseFailureWarned) {
      this.parseFailureWarned = true;
      // eslint-disable-next-line no-console
      console.warn(
        `[LlmBudgetTracker] file at ${this.filePath} unparseable — starting fresh for current UTC day (R5)`,
      );
    }
    return {
      schemaVersion: SCHEMA_VERSION,
      currentUtcDate: todayUtc(),
      spentUsd: 0,
      dailyCapUsd: this.dailyCapUsd,
      lastUpdatedAt: nowIso(),
    };
  }

  private rolloverIfNeeded(today: string): void {
    if (this.state.currentUtcDate === today) return;
    // Surface the previous-day spend so accounting trails are inspectable —
    // without this, a long-running daemon's prior-day total disappears
    // silently when the date advances.
    const previousDate = this.state.currentUtcDate;
    const previousSpend = this.state.spentUsd;
    // eslint-disable-next-line no-console
    console.warn(
      `[LlmBudgetTracker] UTC rollover: ${previousDate} → ${today}; previous-day spent $${previousSpend.toFixed(4)}`,
    );
    this.state = {
      schemaVersion: SCHEMA_VERSION,
      currentUtcDate: today,
      spentUsd: 0,
      dailyCapUsd: this.dailyCapUsd,
      lastUpdatedAt: nowIso(),
    };
    this.persist();
  }

  /** Atomic temp+rename, mirroring `state.ts:180-188`. */
  private persist(): void {
    const dir = dirname(this.filePath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    const tmpPath = join(dir, `.llm-budget-${randomUUID()}.tmp`);
    writeFileSync(tmpPath, JSON.stringify(this.state, null, 2), "utf-8");
    renameSync(tmpPath, this.filePath);
  }
}

// --- PerRoleCircuitBreaker ---

/**
 * In-memory per-role circuit breaker. NO file persistence — Wave E.4 spec is
 * explicit: "in-memory state, resets on restart". Operator must restart the
 * orchestrator to clear an open breaker after the underlying issue is fixed.
 */
export class PerRoleCircuitBreaker {
  private readonly failures: Map<OutboundRole, number> = new Map();
  private readonly opened: Set<OutboundRole> = new Set();

  /** Returns true if the breaker for `role` is closed (next attempt allowed). */
  isClosed(role: OutboundRole): boolean {
    return !this.opened.has(role);
  }

  /** Increment per-role consecutive-failure counter; open at FAILURE_THRESHOLD. */
  recordFailure(role: OutboundRole): void {
    if (this.opened.has(role)) return; // already open — no further bookkeeping
    const next = (this.failures.get(role) ?? 0) + 1;
    this.failures.set(role, next);
    if (next >= FAILURE_THRESHOLD) {
      this.opened.add(role);
    }
  }

  /** Reset the consecutive-failure counter for `role`. No-op if breaker is open. */
  recordSuccess(role: OutboundRole): void {
    if (this.opened.has(role)) return;
    this.failures.set(role, 0);
  }
}
