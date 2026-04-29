---
title: "Harness-TS Types Reference (Source-of-Truth)"
tags: ["harness-ts", "reference", "types", "source-of-truth", "anti-fabrication"]
created: 2026-04-27T08:52:37.665Z
updated: 2026-04-27T08:52:37.665Z
sources: []
links: ["harness-ts-architecture.md", "harness-ts-core-invariants.md", "harness-ts-common-mistakes.md", "phase-e-agent-perspective-discord-rendering-intended-features.md"]
category: reference
confidence: medium
schemaVersion: 1
---

# Harness-TS Types Reference (Source-of-Truth)

# Harness-TS Types Reference (Source-of-Truth)

**Updated:** 2026-04-27 (post Wave E-α). Verified against actual source files.

**Purpose:** authoritative copy of harness-ts type signatures to PREVENT fabrication. Whenever planning, citing, or generating code that references these types, READ THIS PAGE FIRST. Do NOT invent event names or field names.

**Update protocol:** when source changes, update this page in same commit. Cite `filename:line` for every type below.

---

## OrchestratorEvent union — 27 variants (`src/orchestrator.ts:107-136`)

```ts
export type OrchestratorEvent =
  | { type: "task_picked_up"; taskId: string; prompt: string }
  | { type: "session_complete"; taskId: string; success: boolean; errors: string[]; terminalReason?: string }
  | { type: "merge_result"; taskId: string; result: MergeResult }
  | { type: "task_shelved"; taskId: string; reason: string }
  | { type: "task_failed"; taskId: string; reason: string; attempt: number }
  | { type: "task_done"; taskId: string; responseLevelName?: string; summary?: string; filesChanged?: string[] }
  | { type: "poll_tick" }
  | { type: "shutdown" }
  // Phase 2A
  | { type: "escalation_needed"; taskId: string; escalation: EscalationSignal }
  | { type: "checkpoint_detected"; taskId: string; checkpoints: CheckpointSignal[] }
  | { type: "response_level"; taskId: string; level: ResponseLevel; name: string; reasons: string[] }
  | { type: "completion_compliance"; taskId: string; hasConfidence: boolean; hasUnderstanding: boolean; hasAssumptions: boolean; hasNonGoals: boolean; complianceScore: number }
  | { type: "retry_scheduled"; taskId: string; attempt: number; maxRetries: number }
  | { type: "budget_exhausted"; taskId: string; totalCostUsd: number }
  // Wave 2 three-tier
  | { type: "project_declared"; projectId: string; name: string }
  | { type: "project_decomposed"; projectId: string; phaseCount: number }
  | { type: "project_completed"; projectId: string; phaseCount: number; totalCostUsd: number }
  | { type: "project_failed"; projectId: string; reason: string; failedPhase?: string }
  | { type: "project_aborted"; projectId: string; operatorId: string }
  | { type: "architect_spawned"; projectId: string; sessionId: string }
  | { type: "architect_respawned"; projectId: string; sessionId: string; reason: ArchitectRespawnReason }
  | { type: "architect_arbitration_fired"; taskId: string; projectId: string; cause: ArbitrationCause }
  | { type: "arbitration_verdict"; taskId: string; projectId: string; verdict: ArbitrationVerdict; rationale: string }
  | { type: "review_arbitration_entered"; taskId: string; projectId: string; reviewerRejectionCount: number }
  | { type: "review_mandatory"; taskId: string; projectId: string; reviewSummary?: string; reviewFindings?: ReviewFinding[] }
  | { type: "budget_ceiling_reached"; projectId: string; currentCostUsd: number; ceilingUsd: number }
  | { type: "compaction_fired"; projectId: string; generation: number };
```

**27 EVENT TYPE NAMES (verbatim allow-list):**

```
task_picked_up, session_complete, merge_result, task_shelved,
task_failed, task_done, poll_tick, shutdown,
escalation_needed, checkpoint_detected, response_level,
completion_compliance, retry_scheduled, budget_exhausted,
project_declared, project_decomposed, project_completed,
project_failed, project_aborted, architect_spawned,
architect_respawned, architect_arbitration_fired,
arbitration_verdict, review_arbitration_entered,
review_mandatory, budget_ceiling_reached, compaction_fired
```

**FABRICATIONS to refuse** (these DO NOT exist; past planner hallucinations):
- `phase_started`, `phase_succeeded`, `phase_failed` — phases use `project_completed`/`project_failed` events; phase outcomes flow through `cascadePhaseOutcome` to those events
- `architect_phase_start`, `architect_phase_end` — no such events; use `architect_spawned`/`project_decomposed` for lifecycle
- `review_arbitration_resolved` — no such event; only `review_arbitration_entered` exists; resolution flows through `arbitration_verdict`

---

## TaskRecord (`src/lib/state.ts:64-94`)

```ts
export interface TaskRecord {
  id: string;
  state: TaskState;
  prompt: string;
  sessionId?: string;
  worktreePath?: string;
  branchName?: string;
  createdAt: string;
  updatedAt: string;
  completedAt?: string;
  totalCostUsd: number;
  retryCount: number;
  escalationTier: number;
  shelvedAt?: string;
  rebaseAttempts: number;
  tier1EscalationCount: number;
  lastError?: string;
  summary?: string;                 // written ONLY at merge-success via markPhaseSuccess (Wave E-α D1)
  filesChanged?: string[];          // same merge-success contract
  dialogueMessages?: DialogueMessage[];
  dialoguePendingConfirmation?: boolean;
  reviewResult?: ReviewResult;
  // Three-tier (Wave 1.5b)
  projectId?: string;
  phaseId?: string;
  arbitrationCount?: number;
  reviewerRejectionCount?: number;
  lastDirective?: string;
  recoveryAttempts?: number;
  lastResponseLevelName?: string;   // most recent ResponseLevel.name; persisted at response_level emit (Phase A)
}
```

**KNOWN_KEYS** at `src/lib/state.ts:97` — defensive deserialization filter (B7); unknown keys silently dropped on load.

---

## TaskState + transitions (`src/lib/state.ts:30-82`)

10 states:
```
pending, active, reviewing, merging, done, failed,
shelved, escalation_wait, paused, review_arbitration
```

**Transitions** (`from → allowed destinations`):
```
pending          → active, failed
active           → reviewing, merging, done, failed, shelved, escalation_wait, paused
reviewing        → active, merging, done, failed, escalation_wait, review_arbitration, shelved
merging          → done, failed, shelved
done             → (terminal)
failed           → pending (can retry)
shelved          → pending, active, failed
escalation_wait  → active, failed, shelved
paused           → active, failed
review_arbitration → active, merging, failed, escalation_wait, shelved
```

**Where `markPhaseSuccess` operates:** task in `merging` → transitions to `done` internally + writes summary/filesChanged (Wave E-α D1).

---

## CompletionSignal (`src/session/manager.ts:21-39`)

Written by Executor to `.harness/completion.json`:

```ts
export interface CompletionSignal {
  status: "success" | "failure";
  commitSha?: string;            // optional post-WA-1 propose-then-commit
  summary: string;
  filesChanged: string[];
  // Phase 2A enrichment
  understanding?: string;
  assumptions?: string[];
  nonGoals?: string[];
  confidence?: ConfidenceAssessment;
}
```

`confidence.openQuestions` is `string[]`; `firstOpenQuestion` derived from `confidence.openQuestions?.[0]` (manager.ts:53 validation).

---

## SessionResult (`src/session/sdk.ts:38-53`)

Returned by SDKClient.consumeStream:

```ts
export interface SessionResult {
  sessionId: string;
  success: boolean;
  result?: string;
  errors: string[];
  totalCostUsd: number;
  numTurns: number;
  usage: { input_tokens: number; output_tokens: number };
  terminalReason?: string;
  modelName?: string;
}
```

**Common confusion:** SessionResult does NOT have `summary`, `filesChanged`, `confidence`, or `firstOpenQuestion`. Those live on `CompletionSignal` (which is parsed from `.harness/completion.json`, distinct from the SDK transport result).

---

## MergeResult (`src/gates/merge.ts:12-17`)

```ts
export type MergeResult =
  | { status: "merged"; commitSha: string }
  | { status: "test_failed"; error: string }
  | { status: "test_timeout" }
  | { status: "rebase_conflict"; conflictFiles: string[] }
  | { status: "error"; error: string };
```

---

## ReviewResult + ReviewFinding (`src/gates/review.ts:30-52`)

```ts
export type ReviewVerdict = "approve" | "reject" | "request_changes";
export type FindingSeverity = "critical" | "high" | "medium" | "low";

export interface ReviewFinding {
  severity: FindingSeverity;
  file: string;
  line?: number;
  description: string;
  suggestion?: string;
}

export interface ReviewResult {
  verdict: ReviewVerdict;
  riskScore: RiskScore;
  findings: ReviewFinding[];
  summary: string;
}
```

`formatFindingForOps(f)` from `src/lib/review-format.ts` renders `[${severity}] ${file}:${line ?? "?"} — ${description}`.

---

## EscalationSignal (`src/lib/escalation.ts:25-31`)

```ts
export interface EscalationSignal {
  type: EscalationType;       // "clarification_needed" | "design_decision" | "blocked" | "scope_unclear" | "persistent_failure"
  question: string;
  context?: string;
  options?: string[];
  assessment?: ConfidenceAssessment;
}
```

Valid `type` values are restricted by `VALID_TYPES` Set at `escalation.ts:18`.

---

## DiscordConfig (`src/lib/config.ts:57-65`)

```ts
export interface DiscordConfig {
  bot_token_env: string;
  dev_channel: string;
  ops_channel: string;
  escalation_channel: string;
  webhook_url?: string;
  webhooks?: DiscordWebhookUrls;     // { dev?, ops?, escalation? }
  agents: Record<string, DiscordAgentIdentity>;  // { name, avatar_url }
}
```

`DISCORD_AGENT_DEFAULTS` at `config.ts:209-213` — pre-populated for orchestrator/architect/reviewer/executor/operator with empty `avatar_url`.

---

## IdentityRole (Wave E-α — `src/discord/identity.ts`)

Role attribution for outbound Discord identity:

```ts
export type IdentityRole = "executor" | "reviewer" | "architect" | "orchestrator";
```

`resolveIdentity(event: OrchestratorEvent): IdentityRole` — exhaustive switch over all 27 OrchestratorEvent variants. Default: orchestrator.

**Mapping (Wave E-α):**
- executor: `session_complete`, `task_done`
- reviewer: `review_mandatory`, `review_arbitration_entered`
- architect: `architect_spawned`, `architect_respawned`, `architect_arbitration_fired`, `arbitration_verdict`, `project_declared`, `project_decomposed`, `project_completed`, `project_failed`, `project_aborted`, `compaction_fired`
- orchestrator: 13 remaining (task_*, poll_*, escalation_*, etc.)

---

## Cross-refs

- [[harness-ts-architecture]] — full architecture
- [[harness-ts-core-invariants]] — load-bearing invariants
- [[harness-ts-common-mistakes]] — repeated mistake patterns + how to avoid
- [[phase-e-agent-perspective-discord-rendering-intended-features]] — Phase E sub-phase mapping

