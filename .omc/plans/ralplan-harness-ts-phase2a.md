# Phase 2A: Pipeline Hardening — Implementation Plan

**Date:** 2026-04-11
**Status:** APPROVED — Consensus reached (iteration 2, 2026-04-11)
**Depends:** Phase 1.5 (validation) COMPLETE
**Goal:** Agent communicates structured information back to orchestrator; orchestrator routes based on signals. Internal pipeline only — no Discord dependency.

---

## RALPLAN-DR Summary

### Principles (5)

1. **Signal-driven, not rule-driven.** The system defaults to the cheapest effective posture and escalates based on observed signals (completion assessment dimensions, cost thresholds, retry counts), not fixed rules or static phase ladders.

2. **Prompt engineering carries the weight.** Most ambiguity protection is in the systemPrompt, not code. The orchestrator enforces gates (can't merge without signal, can't proceed with degraded confidence) but the quality comes from what the agent is told to do.

3. **Additive extension of existing contracts.** All new fields on `CompletionSignal` are optional. All new event types extend the existing `OrchestratorEvent` union. Existing tests must not break. Zero breaking changes to the Phase 0+1 foundation.

4. **Informational before gating.** Budget alarms, checkpoints, and graduated response levels are informational in Phase 2A (emit events, log entries). Gating behavior (pause pipeline, require operator input) is deferred to Phase 2B/3. This lets us ship and observe before adding hard gates.

5. **Testability through injection.** Every new behavior (escalation file reading, checkpoint detection, retry logic, budget tracking) goes through injectable interfaces or reads from the filesystem — same pattern as `GitOps`, `MergeGitOps`, `QueryFn`. No hidden side effects.

### Decision Drivers (Top 3)

1. **Backward compatibility.** 202 existing tests, 11 test files. Phase 2A must not break any of them. The `CompletionSignal` interface is consumed by `SessionManager.readCompletion()`, `Orchestrator.processTask()`, and all tests that mock completion signals. Adding required fields would break everything.

2. **No Discord dependency.** Phase 2A must work without Discord. Escalation pauses the task with a log entry and event emission. Budget warnings are events. Operator feedback comes in Phase 2B. This means escalation is "pause and wait" not "pause and notify."

3. **Single-session model constraints.** Unlike the Python pipeline (classify->architect->executor->reviewer), the TS harness has one agent session per task. Retry means spawning a new session entirely. Circuit breaker counts retries across session respawns, not within a session.

### Viable Options

#### Option A: Monolithic orchestrator extension (REJECTED)

Add all Phase 2A logic directly into `orchestrator.ts` — retry loops, circuit breaker state, graduated response evaluation, escalation detection, budget tracking.

**Pros:** Single file to understand, no new modules.
**Cons:** `orchestrator.ts` grows from 352 lines to ~600+. Violates existing pattern where orchestrator delegates to specialized modules (SessionManager, MergeGate, StateManager). Testing becomes harder — every test needs full orchestrator setup to test one behavior.

**Invalidation rationale:** The existing architecture already established the pattern of delegating to specialized modules. Adding 250+ lines of mixed concerns (retry logic, confidence evaluation, file reading, budget math) to the orchestrator contradicts the proven design. The orchestrator should remain a thin routing layer.

#### Option B: Extracted modules with orchestrator routing (SELECTED)

Extract new concerns into focused modules:
- `src/lib/types.ts` — shared assessment types (ConfidenceAssessment, etc.)
- `src/lib/escalation.ts` — escalation signal types + file reader
- `src/lib/checkpoint.ts` — checkpoint signal types + file reader  
- `src/lib/response.ts` — graduated response level evaluator
- `src/lib/budget.ts` — budget tracking + alarm emission

System prompt loader (~15 lines) goes directly into `src/lib/config.ts` — too small for its own module.

Orchestrator gains ~60 lines of routing logic that calls into these modules — same pattern as it calls `SessionManager.spawnTask()` and `MergeGate.enqueue()`.

**Pros:** Each module is independently testable. Follows established architecture pattern. Orchestrator stays thin. New tests go in dedicated test files, not the already-large orchestrator test.
**Cons:** More files (5 new src files, 5 new test files). Slightly more indirection.

**Why chosen:** Matches the project's established modularity philosophy. Each new concern (escalation, checkpoints, budget, response routing) can be tested in isolation with simple mocked inputs. The orchestrator test file stays focused on routing/lifecycle, not evaluation logic.

### ADR

**Decision:** Extract Phase 2A concerns into 5 focused modules + shared types file + 1 prompt file. Prompt loader in `config.ts`. Orchestrator routes based on signals from these modules.

**Drivers:** Backward compatibility, testability, established module pattern, Phase 2B readiness.

**Alternatives considered:** Monolithic orchestrator extension (Option A).

**Why chosen:** Option B follows the existing `SessionManager`/`MergeGate`/`StateManager` delegation pattern. Testing is cleaner — each new behavior gets its own test file with simple inputs/outputs rather than requiring full orchestrator harness setup.

**Consequences:** 6 new source files (including `types.ts`) + 4 new test files + 1 prompt file. Orchestrator gains ~80 lines. Total Phase 2A code: ~380 lines source, ~500 lines test, ~200 lines prompt.

**Follow-ups:** Phase 2B (Discord) will listen to the events emitted here. Phase 3 (review gate, dialogue agent) will use the graduated response levels to decide when to spawn review sessions.

---

## Implementation Plan

### Item 1: systemPrompt Content

**What:** Single markdown prompt file loaded by the config module, appended to every agent session via `systemPrompt` in `SDKClient.spawnSession()`. Ports institutional knowledge from Python `config/harness/agents/*.md` into a single unified prompt for the supervised single-session model.

**Files:**
- CREATE `config/harness/system-prompt.md` (~200 lines)
- MODIFY `src/lib/config.ts` — add `loadSystemPrompt(configDir: string): string` function (~15 lines, no separate `prompt.ts` — too small to justify its own module)
- MODIFY `src/session/manager.ts` — pass loaded prompt as `systemPrompt` to `SessionConfig` (~5 lines)

**Types/Interfaces:**
- Add `systemPromptPath?: string` to `ProjectConfig` interface
- Add `systemPrompt?: string` to `HarnessConfig` (loaded at startup, cached)

**Prompt content (ported from Python agents):**
- Intent classification gate (from `architect.md` Layer 2): classify task before starting
- Decision boundaries directive (from `architect.md` Layer 3): declare what you decided vs what needs operator
- Simplifier pressure test (from `executor.md`): "Can we get 80% with less code?"
- Completion contract: MUST write `.harness/completion.json` with assessment dimensions
- Escalation contract: MUST write `.harness/escalation.json` when stuck
- Checkpoint contract: SHOULD write `.harness/checkpoint.json` at decision points and budget thresholds
- Anti-clustering directive: "Do NOT default to middle ratings"
- Budget reassessment triggers: reassess at 25% and 50% budget consumption

**Implementation approach:** The prompt is a markdown file, not code. The config loader reads it at startup with `readFileSync()`. `SessionManager.spawnTask()` passes it through to `SDKClient.spawnSession()` which already supports `systemPrompt` via the `preset: "claude_code", append: ...` pattern (see `sdk.ts:134-139`).

**Dependencies:** None. This is foundational — all other items depend on the agent following these prompt directives.

**Test strategy:**
- `tests/lib/config.test.ts`: add 3 tests — loads prompt file, returns empty string if missing, caches across calls
- `tests/session/manager.test.ts`: add 1 test — verify `spawnTask` passes systemPrompt to SDK

**Estimated tests:** 4 new

---

### Item 2: Shared Types + Completion Signal Enrichment

**What:** Create `src/lib/types.ts` with shared assessment types used across Items 2-7. Extend `CompletionSignal` with optional assessment fields. Add structured confidence as 5 assessment dimensions. Update validation to accept (but not require) new fields.

**Files:**
- CREATE `src/lib/types.ts` — shared assessment types (~30 lines)
- MODIFY `src/session/manager.ts` — extend `CompletionSignal` interface, import from types.ts, update `validateCompletion()` (~25 lines net)

**Types/Interfaces:**
```typescript
// src/lib/types.ts — shared assessment types
// All modules (escalation, checkpoint, response) import from here, never cross-import from manager.ts.

export type ScopeClarity = "clear" | "partial" | "unclear";
export type DesignCertainty = "obvious" | "alternatives_exist" | "guessing";
export type TestCoverage = "verifiable" | "partial" | "untestable";

export interface Assumption {
  description: string;
  impact: "high" | "low";
  reversible: boolean;
}

export interface ConfidenceAssessment {
  scopeClarity: ScopeClarity;
  designCertainty: DesignCertainty;
  assumptions: Assumption[];
  openQuestions: string[];
  testCoverage: TestCoverage;
}
```

```typescript
// Additions to CompletionSignal in manager.ts (imports from types.ts)

export interface CompletionSignal {
  status: "success" | "failure";
  commitSha: string;
  summary: string;
  filesChanged: string[];
  // Phase 2A enrichment (all optional for backward compat)
  understanding?: string;
  assumptions?: string[];
  nonGoals?: string[];
  confidence?: ConfidenceAssessment;
}
```

**Implementation approach:** `validateCompletion()` currently checks the 4 required fields. Extend it to also parse (but not require) the new fields. Use type narrowing — if `confidence` is present, validate its shape; if malformed, strip it (don't reject the whole signal). This follows the B7 pattern (unknown key drop) from state.ts.

**Dependencies:** None. Item 1 (systemPrompt) tells the agent to write these fields, but the code accepts them regardless.

**Test strategy:**
- `tests/session/manager.test.ts`: add 5 tests:
  - Accepts completion with all new optional fields
  - Accepts completion with some optional fields (partial enrichment)
  - Accepts completion with no optional fields (backward compat — existing tests cover this)
  - Strips malformed confidence object (keeps rest of signal)
  - Validates confidence assessment dimensions when present

**Estimated tests:** 5 new

---

### Item 3: Escalation Protocol

**What:** Agent writes `.harness/escalation.json` when stuck. Orchestrator detects it, transitions task to `escalation_wait`, emits event. Works without Discord — just pauses the task with a log entry.

**Files:**
- CREATE `src/lib/escalation.ts` (~50 lines)
- MODIFY `src/orchestrator.ts` — add escalation detection in `processTask()` after session completes (~20 lines)
- MODIFY `src/orchestrator.ts` — add `escalation_needed` to `OrchestratorEvent` union (~3 lines)

**Types/Interfaces:**
```typescript
// src/lib/escalation.ts

export type EscalationType =
  | "clarification_needed"
  | "design_decision"
  | "blocked"
  | "scope_unclear"
  | "persistent_failure";

export interface EscalationSignal {
  type: EscalationType;
  question: string;
  context?: string;
  options?: string[];
  assessment?: ConfidenceAssessment;  // from types.ts
}

export function readEscalation(worktreePath: string): EscalationSignal | null;
export function validateEscalation(raw: unknown): EscalationSignal | null;
```

**New event type:**
```typescript
| { type: "escalation_needed"; taskId: string; escalation: EscalationSignal }
```

**Implementation approach:** After `sessions.spawnTask()` returns but before routing to merge gate, orchestrator checks for `.harness/escalation.json` in the worktree. If found and valid, transition to `escalation_wait` and emit event. The task stays paused until Phase 2B provides a mechanism for operator response (or manual state manipulation).

Escalation takes priority over completion — if both files exist, escalation wins. Rationale: the agent wrote both, but the escalation means "I finished but I'm uncertain" which should be reviewed before merging.

**Dependencies:** Item 2 (`ConfidenceAssessment` type from `src/lib/types.ts`).

**Test strategy:**
- `tests/lib/escalation.test.ts` (new file): 8 tests:
  - Reads valid escalation file
  - Returns null for missing file
  - Returns null for malformed JSON
  - Validates all escalation types
  - Rejects unknown escalation type
  - Accepts optional fields (context, options, assessment)
  - Rejects missing required fields (type, question)
  - Handles empty question string
- `tests/orchestrator.test.ts`: 3 tests:
  - Escalation detected -> task transitions to escalation_wait
  - Escalation takes priority over completion signal
  - No escalation -> normal flow unchanged

**Estimated tests:** 11 new

---

### Item 4: Failure Retry + Circuit Breaker

**What:** When agent session completes with `status: "failure"` (or no completion signal), orchestrator retries by spawning a new session (up to `max_session_retries`). After N total failures, auto-escalate (emit `escalation_needed` with `persistent_failure` type) instead of silently dropping the task. Circuit breaker caps total retry+escalation cycles before permanent failure.

**Files:**
- MODIFY `src/orchestrator.ts` — replace direct `failed` transition with retry logic (~40 lines)
- MODIFY `src/lib/state.ts` — add `tier1EscalationCount` to `TaskRecord` + `KNOWN_KEYS` (~5 lines)
- MODIFY `src/lib/config.ts` — add `max_session_retries`, `auto_escalate_on_max_retries`, `max_tier1_escalations` to `PipelineConfig` (~8 lines)

**Types/Interfaces:**
```typescript
// Additions to PipelineConfig
max_session_retries?: number;              // default 3 — session-failure retries (separate from max_retries which caps rebase retries)
auto_escalate_on_max_retries?: boolean;    // default true
max_tier1_escalations?: number;            // default 2

// Addition to TaskRecord
tier1EscalationCount: number;  // circuit breaker counter
```

**Naming clarification:** `max_retries` (existing) caps *rebase* retries in the merge gate path. `max_session_retries` (new) caps *session failure* retries — agent crash, no completion signal, agent-reported failure. These are independent counters tracking different failure modes.

**New event type:**
```typescript
| { type: "retry_scheduled"; taskId: string; attempt: number; maxRetries: number }
```

**Exact processTask failure branch replacement:**

Current code (orchestrator.ts lines 198-209):
```typescript
// Session failed without completion signal
if (!result.success || !completion || completion.status !== "success") {
  const reason = !result.success
    ? result.errors.join("; ")
    : !completion
      ? "No completion signal"
      : `Agent reported failure: ${completion.summary}`;

  this.state.transition(task.id, "failed");
  this.state.updateTask(task.id, { lastError: reason });
  this.emit({ type: "task_failed", taskId: task.id, reason });
  return;
}
```

Replaced with:
```typescript
// Session failed — route through retry/escalation logic
if (!result.success || !completion || completion.status !== "success") {
  const reason = !result.success
    ? result.errors.join("; ")
    : !completion
      ? "No completion signal"
      : `Agent reported failure: ${completion.summary}`;

  // Clean up worktree before any retry/escalation decision
  this.sessions.cleanupWorktree(task.id);
  const retryCount = this.state.incrementRetry(task.id);
  const maxSessionRetries = this.config.pipeline.max_session_retries ?? 3;

  if (retryCount < maxSessionRetries) {
    // Retry: active -> failed -> pending -> processTask
    this.state.transition(task.id, "failed");
    this.state.updateTask(task.id, { lastError: reason });
    this.state.transition(task.id, "pending");
    this.emit({ type: "retry_scheduled", taskId: task.id, attempt: retryCount + 1, maxRetries: maxSessionRetries });
    const updated = this.state.getTask(task.id)!;
    this.processTask(updated);
    return;
  }

  // Max retries exhausted — escalate or fail
  const autoEscalate = this.config.pipeline.auto_escalate_on_max_retries ?? true;
  const maxEscalations = this.config.pipeline.max_tier1_escalations ?? 2;
  const current = this.state.getTask(task.id)!;

  if (autoEscalate && current.tier1EscalationCount < maxEscalations) {
    // Auto-escalate: active -> escalation_wait (valid transition, see state.ts line 30)
    this.state.transition(task.id, "escalation_wait");
    this.state.updateTask(task.id, {
      lastError: reason,
      tier1EscalationCount: current.tier1EscalationCount + 1,
    });
    this.emit({
      type: "escalation_needed",
      taskId: task.id,
      escalation: { type: "persistent_failure", question: `Task failed ${maxSessionRetries} times: ${reason}` },
    });
    return;
  }

  // Circuit breaker: permanent failure
  this.state.transition(task.id, "failed");
  this.state.updateTask(task.id, {
    lastError: autoEscalate
      ? `Circuit breaker: exhausted ${maxSessionRetries} retries × ${maxEscalations} escalation cycles`
      : reason,
  });
  this.emit({ type: "task_failed", taskId: task.id, reason });
  return;
}
```

**Retry sequencing invariant:** `processTask` is async but NOT awaited by `scanForTasks`. Multiple tasks can be in-flight simultaneously. However, retry spawns are synchronous within a single task's `processTask` call — the retry calls `this.processTask(updated)` directly (tail-call style), so a single task never has two concurrent sessions. The merge gate's FIFO queue already serializes cross-task merge attempts.

**Key design decision:** `tier1EscalationCount` resets on task creation and `done`, but NOT on retry or escalation resume. This matches the Python harness's circuit breaker design (see `v5-harness-auto-escalate-proposal.md` lines 146-153).

**State transition path clarification:**
- Retry path: `active -> failed -> pending -> active` (uses existing transitions)
- Escalation path: `active -> escalation_wait` (already valid, state.ts line 30)
- Circuit breaker path: `active -> failed` (terminal)
- No new state transitions needed — all paths use existing valid transitions.

**Dependencies:** Item 3 (escalation event type). The `escalation_needed` event from Item 3 is reused here for `persistent_failure` type.

**Test strategy:**
- `tests/orchestrator.test.ts`: 8 tests:
  - Session failure -> retry (attempt 1 of 3)
  - Session failure -> retry -> success on attempt 2
  - Session failure x3 -> auto-escalate to escalation_wait
  - Circuit breaker: 3 failures -> escalate -> resume -> 3 failures -> escalate -> resume -> 3 failures -> circuit breaker fires -> failed
  - auto_escalate_on_max_retries=false -> silent fail (legacy)
  - Retry preserves original prompt
  - Retry cleans up worktree before respawn
  - tier1EscalationCount resets on task creation, not on retry
- `tests/lib/state.test.ts`: 3 tests:
  - tier1EscalationCount initialized to 0
  - tier1EscalationCount in KNOWN_KEYS (survives B7 deserialization)
  - tier1EscalationCount preserved through serialization cycle

**Estimated tests:** 11 new

---

### Item 5: Budget Alarm Events

**What:** After session completes, evaluate cumulative cost against `max_budget_usd` from `PipelineConfig` and emit `budget_report` events at 50% and 80% thresholds. Informational only (O6) — does not pause pipeline.

**Files:**
- CREATE `src/lib/budget.ts` (~30 lines)
- MODIFY `src/orchestrator.ts` — add `budget_report` to event union, wire budget tracker after session result (~10 lines)
- MODIFY `src/lib/config.ts` — add `max_budget_usd` to `PipelineConfig` (~2 lines)

**Types/Interfaces:**
```typescript
// src/lib/budget.ts

export interface BudgetThreshold {
  percent: number;  // 0.50, 0.80
  label: string;    // "50%", "80%"
}

export const DEFAULT_THRESHOLDS: BudgetThreshold[] = [
  { percent: 0.50, label: "50%" },
  { percent: 0.80, label: "80%" },
];

export class BudgetTracker {
  private triggered: Set<number> = new Set();
  constructor(
    private maxBudgetUsd: number,
    private thresholds: BudgetThreshold[] = DEFAULT_THRESHOLDS,
  ) {}

  /** Call with cumulative cost. Returns newly-crossed thresholds (each fires at most once). */
  update(cumulativeCostUsd: number): BudgetThreshold[];
}
```

```typescript
// Addition to PipelineConfig in config.ts
max_budget_usd?: number;  // default undefined — no budget tracking when absent
```

**`maxBudgetUsd` source:** New optional field `max_budget_usd` in `[pipeline]` section of `config/harness/project.toml`. Parsed via existing `optionalNumber()` helper in `config.ts`. When absent/undefined, budget tracking is skipped entirely (no `BudgetTracker` instantiated, no events emitted). This avoids requiring a config migration for existing setups.

**New event type:**
```typescript
| { type: "budget_report"; taskId: string; threshold: string; currentCost: number; maxBudget: number }
```

**Naming rationale:** `budget_report` rather than `budget_warning` — these are informational observations in Phase 2A (O6 principle), not warnings that imply action is needed. Phase 3 can promote specific thresholds to warnings when gating behavior is added.

**Implementation approach:** `BudgetTracker` is a stateless evaluator — given cumulative cost and max budget, it returns which thresholds were crossed (deduplicating so each threshold fires at most once). The orchestrator creates one `BudgetTracker` per task (in `processTask`, before `spawnTask`). After `spawnTask` returns with `result.totalCostUsd`, call `tracker.update(result.totalCostUsd)` and emit events for any crossed thresholds.

**Simplification for Phase 2A:** Only emit budget reports from the final result message's `total_cost_usd`. This means reports fire after the session ends, not mid-stream. Mid-stream tracking deferred to Phase 4 (session metrics) when we have better token-to-cost mapping. No modifications to `sdk.ts` needed for Phase 2A.

**Dependencies:** None. Independent of all other items.

**Test strategy:**
- `tests/lib/budget.test.ts` (new file): 7 tests:
  - No threshold crossed at 0% cost
  - 50% threshold crossed
  - 80% threshold crossed
  - Both thresholds crossed in one update
  - Threshold fires only once (dedup)
  - Custom thresholds
  - Zero maxBudget handled (no division by zero)
- `tests/orchestrator.test.ts`: 2 tests:
  - Budget report event emitted when session cost exceeds 50% of configured max
  - No budget report when no max_budget_usd configured

**Estimated tests:** 9 new

---

### Item 6: Mid-Task Checkpoints

**What:** Agent writes `.harness/checkpoint.json` at decision points and budget thresholds. Orchestrator reads and logs but does not pause (informational in Phase 2A, gating in Phase 3).

**Files:**
- CREATE `src/lib/checkpoint.ts` (~35 lines)
- MODIFY `src/orchestrator.ts` — add checkpoint detection after session completes (~15 lines)
- MODIFY `src/orchestrator.ts` — add `checkpoint_detected` to event union (~3 lines)

**Types/Interfaces:**
```typescript
// src/lib/checkpoint.ts

export interface CheckpointSignal {
  timestamp: string;
  reason: "decision_point" | "budget_threshold" | "complexity_spike" | "scope_change";
  description: string;
  assessment?: ConfidenceAssessment;  // from types.ts
  budgetConsumedPct?: number;
}

export function readCheckpoints(worktreePath: string): CheckpointSignal[];
export function validateCheckpoint(raw: unknown): CheckpointSignal | null;
```

**Design decision:** Checkpoints are an array, not a single file. Agent may write multiple checkpoints during a session. Store as `.harness/checkpoint.json` (JSON array) or `.harness/checkpoints/*.json` (directory of files). **Chosen: single array file** — simpler for the agent to append to, simpler for the orchestrator to read. Agent writes `checkpoint.json` as a JSON array; each new checkpoint appends to the array.

**New event type:**
```typescript
| { type: "checkpoint_detected"; taskId: string; checkpoints: CheckpointSignal[] }
```

**Implementation approach:** After session completes (alongside completion and escalation reads), read `.harness/checkpoint.json`. If present and valid, emit `checkpoint_detected` event and log entries. In Phase 2A, this is purely informational — the event is available for Discord notifications in Phase 2B and for gating logic in Phase 3.

**Dependencies:** Item 2 (ConfidenceAssessment type). Same import pattern as Item 3.

**Test strategy:**
- `tests/lib/checkpoint.test.ts` (new file): 7 tests:
  - Reads valid checkpoint array
  - Returns empty array for missing file
  - Returns empty array for malformed JSON
  - Validates checkpoint reasons (enum)
  - Rejects unknown reason
  - Handles single-element array
  - Strips invalid entries from array (partial parse)
- `tests/orchestrator.test.ts`: 2 tests:
  - Checkpoint detected -> event emitted
  - No checkpoint file -> no event

**Estimated tests:** 9 new

---

### Item 7: Graduated Response Routing

**What:** Evaluate completion signal assessment dimensions to select escalation level (0-4). Routes to merge directly (level 0-1), flags for external review (level 2), or pauses (level 3-4). In Phase 2A, levels 2+ emit events but don't actually block merge — gating deferred to Phase 3.

**Files:**
- CREATE `src/lib/response.ts` (~50 lines)
- MODIFY `src/orchestrator.ts` — add response level evaluation before merge routing (~20 lines)
- MODIFY `src/orchestrator.ts` — add `response_level` to event union (~3 lines)

**Types/Interfaces:**
```typescript
// src/lib/response.ts

export type ResponseLevel = 0 | 1 | 2 | 3 | 4;

export interface ResponseLevelResult {
  level: ResponseLevel;
  name: "direct" | "enriched" | "reviewed" | "dialogue" | "planned";
  reasons: string[];  // why this level was selected
}

export function evaluateResponseLevel(
  completion: CompletionSignal,
  sessionResult: SessionResult,
  thresholds?: ResponseThresholds,
): ResponseLevelResult;

export interface ResponseThresholds {
  reviewCostUsd?: number;       // default 0.50 — above this, level >= 2
  reviewFileCount?: number;     // default 10 — above this, level >= 2
  maxDirectCostUsd?: number;    // default 0.20 — above this, level >= 1
}
```

**Evaluation logic (from graduated-response.md):**
```
Level 0 (direct): All dimensions clear/obvious, no high-impact assumptions, cost < maxDirectCostUsd
Level 1 (enriched): All clear/obvious but has assumptions, or cost > maxDirectCostUsd
Level 2 (reviewed): Any "partial" or "alternatives_exist", or cost > reviewCostUsd, or filesChanged > reviewFileCount
Level 3 (dialogue): Any "unclear"/"guessing" or open questions present
Level 4 (planned): Multiple unclear dimensions + high-impact irreversible assumptions
```

If no `confidence` assessment in completion signal, default to level 1 (enriched) — the agent completed without structured assessment, assume it's fine but note the gap.

**New event type:**
```typescript
| { type: "response_level"; taskId: string; level: ResponseLevel; name: string; reasons: string[] }
```

**Implementation approach:** Pure function. Takes `CompletionSignal` + `SessionResult`, returns `ResponseLevelResult`. Orchestrator calls this after reading completion signal but before routing to merge. In Phase 2A, the result is emitted as an event and logged. All levels proceed to merge. In Phase 3, levels 2+ will gate the merge.

**Dependencies:** Item 2 (`CompletionSignal` enrichment, `ConfidenceAssessment` from `src/lib/types.ts`). The function must handle completions with and without the `confidence` field.

**Test strategy:**
- `tests/lib/response.test.ts` (new file): 12 tests:
  - All clear -> level 0
  - Clear with assumptions -> level 1
  - Partial scope clarity -> level 2
  - Alternatives exist in design -> level 2
  - High cost -> level 2
  - Many files changed -> level 2
  - Unclear scope -> level 3
  - Guessing design -> level 3
  - Open questions present -> level 3
  - Multiple unclear + high-impact irreversible assumptions -> level 4
  - No confidence assessment -> level 1 (default)
  - Custom thresholds override defaults
- `tests/orchestrator.test.ts`: 2 tests:
  - Response level event emitted on successful completion
  - Response level evaluation uses session cost from SessionResult

**Estimated tests:** 14 new

---

### Item 8: Completion Compliance Event

**What:** After reading the completion signal, emit a `completion_compliance` event that reports whether the agent followed the systemPrompt contract — did it include the enriched fields (confidence assessment, understanding, assumptions, nonGoals)? Informational only — never blocks merge. Gives observability into how well the prompt engineering is working before we add hard gates in Phase 3.

**Files:**
- MODIFY `src/orchestrator.ts` — emit `completion_compliance` event after reading completion signal (~20 lines)

**Types/Interfaces:**
```typescript
// New event type
| {
    type: "completion_compliance";
    taskId: string;
    hasConfidence: boolean;
    hasUnderstanding: boolean;
    hasAssumptions: boolean;
    hasNonGoals: boolean;
    complianceScore: number;  // 0-4 count of present optional fields
  }
```

**Implementation approach:** After `readCompletion()` succeeds, check which optional enrichment fields are present. Emit the event with boolean flags and a simple count (0-4). This is ~20 lines in `processTask()`, placed right after the existing `session_complete` event emission and before escalation/checkpoint detection.

**Dependencies:** Item 2 (enriched CompletionSignal — the fields being checked).

**Test strategy:**
- `tests/orchestrator.test.ts`: 3 tests:
  - Fully enriched completion -> complianceScore 4, all flags true
  - Bare completion (no optional fields) -> complianceScore 0, all flags false
  - Partial enrichment (only confidence) -> complianceScore 1

**Estimated tests:** 3 new

---

## Ordering

### Dependency Graph

```
Item 1 (systemPrompt)     -- no code deps, foundational for agent behavior
    |
    v
Item 2 (shared types + completion enrichment) -- defines ConfidenceAssessment in types.ts
    |
    +---> Item 3 (escalation protocol)     -- imports from types.ts
    |         |
    |         v
    |     Item 4 (retry + circuit breaker) -- reuses escalation_needed event
    |
    +---> Item 6 (checkpoints)             -- imports from types.ts
    |
    +---> Item 7 (graduated response)      -- depends on enriched CompletionSignal
    |
    +---> Item 8 (completion compliance)   -- checks enriched CompletionSignal fields
    
Item 5 (budget alarms) -- fully independent
```

### Recommended Implementation Order

**Wave 1 (parallel):**
- Item 1: systemPrompt content (prompt file + config loader, no separate prompt.ts)
- Item 5: Budget alarm events

These two are completely independent. Item 1 is prompt-only + config. Item 5 is a self-contained module.

**Wave 2 (sequential):**
- Item 2: Shared types (`src/lib/types.ts`) + Completion signal enrichment

Must come before Items 3, 6, 7, 8 because they all import `ConfidenceAssessment` from `types.ts`.

**Wave 3 (parallel):**
- Item 3: Escalation protocol
- Item 6: Mid-task checkpoints
- Item 7: Graduated response routing
- Item 8: Completion compliance event

Items 3, 6, 7 depend on Item 2 (types) but are independent of each other. Item 8 depends on Item 2 (enriched CompletionSignal) but is independent of 3, 6, 7.

**Wave 4 (sequential):**
- Item 4: Failure retry + circuit breaker

Depends on Item 3's `escalation_needed` event type and `EscalationSignal` for the `persistent_failure` auto-escalation.

### Summary

```
Wave 1: [Item 1, Item 5]              -- parallel
Wave 2: [Item 2]                       -- sequential
Wave 3: [Item 3, Item 6, Item 7, Item 8] -- parallel
Wave 4: [Item 4]                       -- sequential
```

Total: 4 waves, 8 items. With parallelism, the critical path is: Item 1 | Item 2 | Item 3 | Item 4 (4 sequential steps).

---

## File Manifest

### New Files (6 source + 4 test + 1 prompt)

| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `config/harness/system-prompt.md` | ~200 | Agent protocol prompt |
| `src/lib/types.ts` | ~30 | Shared assessment types (ConfidenceAssessment, ScopeClarity, etc.) |
| `src/lib/escalation.ts` | ~50 | Escalation signal types + reader |
| `src/lib/checkpoint.ts` | ~35 | Checkpoint signal types + reader |
| `src/lib/response.ts` | ~50 | Graduated response level evaluator |
| `src/lib/budget.ts` | ~30 | Budget tracking + alarm emission |
| `tests/lib/escalation.test.ts` | ~80 | Escalation module tests |
| `tests/lib/checkpoint.test.ts` | ~70 | Checkpoint module tests |
| `tests/lib/response.test.ts` | ~120 | Response level evaluator tests |
| `tests/lib/budget.test.ts` | ~70 | Budget tracker tests |

Note: `src/lib/prompt.ts` collapsed into `src/lib/config.ts` (15 lines doesn't justify a separate module). Prompt loader tests go in `tests/lib/config.test.ts`.

### Modified Files (4)

| File | Lines changed (est.) | What changes |
|------|---------------------|-------------|
| `src/session/manager.ts` | +15 | CompletionSignal enrichment (imports ConfidenceAssessment from types.ts), validateCompletion() extension |
| `src/orchestrator.ts` | +80 | Escalation/checkpoint/compliance detection, retry logic, budget wiring, response routing, 6 new event types |
| `src/lib/config.ts` | +20 | System prompt loading (`loadSystemPrompt()`), new PipelineConfig fields (`max_session_retries`, `max_budget_usd`, `auto_escalate_on_max_retries`, `max_tier1_escalations`) |
| `src/lib/state.ts` | +5 | tier1EscalationCount on TaskRecord + KNOWN_KEYS |

### Modified Test Files (3)

| File | Tests added (est.) | What's tested |
|------|-------------------|--------------|
| `tests/session/manager.test.ts` | +6 | Completion enrichment validation, systemPrompt pass-through |
| `tests/orchestrator.test.ts` | +20 | Escalation detection, retry/circuit breaker, budget events, checkpoint events, response levels, completion compliance |
| `tests/lib/state.test.ts` | +3 | tier1EscalationCount serialization |

---

## Acceptance Criteria

### Per-Item

**Item 1 — systemPrompt content:**
- [ ] `config/harness/system-prompt.md` exists with intent classification, decision boundaries, simplifier pressure test, completion/escalation/checkpoint contracts, anti-clustering directive
- [ ] `loadConfig()` or separate `loadSystemPrompt()` reads and returns prompt content
- [ ] `SessionManager.spawnTask()` passes prompt as `systemPrompt` to SDK session
- [ ] Existing 202 tests still pass (no regressions)

**Item 2 — Completion signal enrichment:**
- [ ] `CompletionSignal` interface includes `understanding`, `assumptions`, `nonGoals`, `confidence` (all optional)
- [ ] `ConfidenceAssessment` type has 5 dimensions: scopeClarity, designCertainty, assumptions, openQuestions, testCoverage
- [ ] `validateCompletion()` accepts signals with and without new fields
- [ ] Malformed confidence object is stripped, not rejected (signal still valid)

**Item 3 — Escalation protocol:**
- [ ] `readEscalation(worktreePath)` reads and validates `.harness/escalation.json`
- [ ] Orchestrator detects escalation after session completes
- [ ] Task transitions to `escalation_wait` when escalation found
- [ ] `escalation_needed` event emitted with full EscalationSignal
- [ ] Escalation takes priority over completion signal

**Item 4 — Failure retry + circuit breaker:**
- [ ] Failed session triggers retry (up to `max_session_retries`)
- [ ] After `max_session_retries` failures, auto-escalate with `persistent_failure` type
- [ ] Circuit breaker: after `max_tier1_escalations` escalation cycles, task fails permanently
- [ ] `auto_escalate_on_max_retries=false` preserves legacy silent-fail behavior
- [ ] `tier1EscalationCount` persists through serialization, resets on new task/done
- [ ] `retry_scheduled` event emitted on each retry

**Item 5 — Budget alarm events:**
- [ ] `BudgetTracker` fires at 50% and 80% thresholds (configurable)
- [ ] Each threshold fires at most once per task
- [ ] `budget_report` event emitted
- [ ] No crash when `maxBudgetUsd` is 0 or not configured

**Item 6 — Mid-task checkpoints:**
- [ ] `readCheckpoints(worktreePath)` reads `.harness/checkpoint.json` as array
- [ ] Invalid entries in array are stripped (partial parse)
- [ ] `checkpoint_detected` event emitted when checkpoints found
- [ ] Missing checkpoint file produces no event (not an error)

**Item 7 — Graduated response routing:**
- [ ] `evaluateResponseLevel()` maps assessment dimensions to levels 0-4
- [ ] Missing confidence assessment defaults to level 1
- [ ] Cost and file-count thresholds configurable
- [ ] `response_level` event emitted with level, name, and reasons
- [ ] All levels proceed to merge in Phase 2A (gating deferred to Phase 3)

**Item 8 — Completion compliance event:**
- [ ] `completion_compliance` event emitted after every successful completion read
- [ ] Reports boolean flags for each optional enrichment field (confidence, understanding, assumptions, nonGoals)
- [ ] Reports `complianceScore` (0-4 count of present fields)
- [ ] Informational only — never blocks merge

### Overall Phase 2A

- [ ] All existing 202 tests pass unchanged
- [ ] ~66 new tests added (4 + 5 + 11 + 11 + 9 + 9 + 14 + 3 = 66)
- [ ] Total test count: ~268
- [ ] No new runtime dependencies (all using Node.js built-ins + existing smol-toml)
- [ ] 6 new `OrchestratorEvent` types: `escalation_needed`, `retry_scheduled`, `budget_report`, `checkpoint_detected`, `response_level`, `completion_compliance`
- [ ] `orchestrator.ts` stays under 450 lines (currently 352, adding ~80)
- [ ] Shared assessment types in `src/lib/types.ts` — no cross-module imports from `manager.ts`
- [ ] Every new module has its own test file with isolated, independently-runnable tests
- [ ] No Discord imports or dependencies anywhere in Phase 2A code

---

## Risks and Mitigations

### Risk 1: Agent ignores systemPrompt directives

**Likelihood:** Medium. The agent may not write assessment dimensions, may write partial assessments, or may cluster ratings.

**Mitigation:** All orchestrator logic handles missing/partial data gracefully. Missing confidence -> level 1 default. Missing escalation -> normal flow. The anti-clustering directive and specific assessment questions in the prompt reduce clustering. Observation during Phase 2A (informational mode) lets us tune the prompt before adding hard gates in Phase 3.

### Risk 2: Retry storms

**Likelihood:** Low. A task that fails deterministically will retry `max_session_retries` times, then escalate `max_tier1_escalations` times, burning sessions and budget.

**Mitigation:** Circuit breaker caps total attempts. Default worst case: 3 retries x 2 escalation cycles = 6 sessions + 1 final failure = 7 sessions. Each session has its own maxBudgetUsd cap. Budget warning events provide visibility. Future: add per-task total budget cap.

### Risk 3: ConfidenceAssessment type coupling

**Likelihood:** Low. Items 3, 6, and 7 all import `ConfidenceAssessment` from `manager.ts`.

**Mitigation:** Extract shared types (`ConfidenceAssessment`, `ScopeClarity`, `DesignCertainty`, `TestCoverage`, `Assumption`) to `src/lib/types.ts` upfront. All modules import from `types.ts`, never cross-import from `manager.ts`. This eliminates coupling from day one.

### Risk 4: Completion signal schema drift

**Likelihood:** Medium. The prompt tells the agent one schema, the validator accepts another.

**Mitigation:** The prompt includes the exact JSON schema the agent should write. The validator is permissive (optional fields). Version the prompt (include a `version` field in the schema) so future changes are detectable.

### Explicitly Deferred to Phase 2B/3

- **Discord notifications for escalation, budget_report, checkpoint, compliance events** -> Phase 2B
- **Operator response to escalation** (resume from escalation_wait) -> Phase 2B
- **Merge gating by response level** (levels 2+ block merge) -> Phase 3
- **External review gate** (separate read-only sonnet session) -> Phase 3
- **Dialogue agent pattern** (proposal.json, operator review before implementation) -> Phase 3
- **Mid-stream budget tracking** (per-turn cost estimation) -> Phase 4
- **Checkpoint gating** (orchestrator pauses at checkpoints) -> Phase 3
- **Escalation queue** (multiple escalations per task) -> future
- **Earned autonomy** (trust score modulates response thresholds) -> Phase 4+
