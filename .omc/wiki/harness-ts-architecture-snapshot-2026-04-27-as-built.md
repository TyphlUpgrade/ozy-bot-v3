---
title: "Harness-TS Architecture Snapshot — 2026-04-27 (As-Built)"
tags: ["harness-ts", "architecture", "snapshot", "as-built", "wave-e-alpha", "post-spike", "comprehensive"]
created: 2026-04-27T10:44:58.129Z
updated: 2026-04-27T10:44:58.129Z
sources: []
links: ["harness-ts-architecture.md", "harness-ts-phase-roadmap.md", "phase-e-agent-perspective-discord-rendering-intended-features.md", "harness-ts-types-reference-source-of-truth.md", "harness-ts-core-invariants.md", "harness-ts-common-mistakes.md", "harness-ts-live-setup.md", "harness-ts-plan-index.md", "harness-ts-wave-c-backlog.md", "ralplan-procedure-failure-modes-and-recommended-mitigations.md", "session-log-wave-e-completion-2026-04-27.md"]
category: architecture
confidence: medium
schemaVersion: 1
---

# Harness-TS Architecture Snapshot — 2026-04-27 (As-Built)

# Harness-TS Architecture Snapshot — 2026-04-27 (As-Built)

**Companion page** to [[harness-ts-architecture]] (vision/high-level) and [[harness-ts-phase-roadmap]] (delivery history). This page is the **comprehensive as-built map** of the codebase as it stands after Wave E-α + lead-engineer spike fixes (commits `25ae1ca` / `4b425d5` / `bc98868` / `dc1bf81`).

**Scope:** every file under `harness-ts/src/`. Module-by-module API + invariants + integration points + complexity flags. Cite `file:line` for every load-bearing claim. Use this page to onboard a new agent or reason about cross-module changes.

**Total:** 8,770 LOC across 5 layers (discord, session, gates, lib, orchestrator). Empty placeholder dirs `src/events/` and `src/orchestrator/` exist but contain no code yet.

---

## Macro architecture

```
                                ┌────────────────────────┐
              file: completion → │  src/orchestrator.ts    │ ← daemon entrypoint
              file: escalation → │  (1084 LOC, sole bus)   │
              file: checkpoint → │                         │
                                └─────────┬──────────────┘
                                          │
       ┌─────────────────┬───────────────┼──────────────┬─────────────────┐
       ▼                 ▼               ▼              ▼                 ▼
src/session/       src/gates/      src/discord/      src/lib/        ProjectStore
(Executor +       (MergeGate +     (notifier +      (state, config,  (multi-phase
 Architect tier)   ReviewGate)      sender + ...)    escalation,      coordination)
                                                     budget, ...)
```

**Star topology (I-1 invariant).** The orchestrator is the SOLE owner of:
- Discord client (no agent session sees Discord)
- Audit-trail emission (OrchestratorEvent bus)
- State persistence (StateManager + ProjectStore)
- Merge gate exclusivity

Agent sessions (Architect, Executor, Reviewer) are content producers behind the SDK boundary — never endpoints. Inbound Discord routes through `dispatcher.ts` → `relayOperatorInput(projectId, plainText)` only; no Discord ids/channels/mentions/embeds reach the agent layer.

---

## Layer 1 — `src/lib/` (cross-cutting primitives, 1762 LOC, 11 files)

| File | LOC | Purpose | Key exports |
|------|----:|---------|-------------|
| `config.ts` | 386 | TOML loader (smol-toml), typed `HarnessConfig` | `loadConfig`, `DEFAULT_EXECUTOR_SYSTEM_PROMPT`, `DISCORD_AGENT_DEFAULTS`, `PERSISTENT_SESSION_WARN_THRESHOLD_DEFAULT=100` |
| `state.ts` | 367 | 9-state TaskRecord machine, atomic JSON, event log | `TaskRecord`, `StateManager`, `markPhaseSuccess`, `KNOWN_KEYS`, `VALID_TRANSITIONS` |
| `project.ts` | 329 | Multi-phase project store | `ProjectRecord`, `ProjectStore`, `ArchitectCompactionSummary`, `architectWorktreePath` derivation |
| `escalation.ts` | 69 | 5-tier classification | `EscalationType` (`clarification_needed` / `design_decision` / `blocked` / `scope_unclear` / `persistent_failure`), `readEscalation` |
| `budget.ts` | 43 | Threshold notifications | `BudgetTracker.update`, `DEFAULT_THRESHOLDS` (50%, 80%) |
| `checkpoint.ts` | 74 | Decision-point signal reader | `CheckpointReason` (4 variants), `readCheckpoints` |
| `response.ts` | 126 | Response level routing 0–4 | `evaluateResponseLevel` (level 0 direct → 4 planned) |
| `review-format.ts` | 20 | Shared formatter | `formatFindingForOps` (extracted to avoid `discord/` → `gates/` import) |
| `text.ts` | 70 | Sanitization + ID validation | `sanitizeTaskId` (O4), `redactSecrets`, `sanitize`, `truncateRationale` |
| `types.ts` | 22 | Shared assessment types | `ScopeClarity`, `DesignCertainty`, `TestCoverage`, `ConfidenceAssessment` |

### Load-bearing patterns in `lib/`
- **Atomic writes (O3)** — `state.ts:180-188`, `project.ts:172-174`. Pattern: `writeFileSync(tmp); renameSync(tmp, target)` with UUID suffix.
- **Unknown-key drop (B7)** — `state.ts:156-160`, `project.ts:90-108`. Whitelist load lets future fields land without migration.
- **9-state FSM** — `state.ts:13-48`. States: `pending`, `active`, `reviewing`, `merging`, `done`, `failed`, `shelved`, `escalation_wait`, `paused`, `review_arbitration`. `VALID_TRANSITIONS` const enforces.
- **markPhaseSuccess collapse (Wave E-α)** — `state.ts:351-361`. Precondition: task in `merging`. Single re-read at orchestrator caller (I-9), reused for both `cascadePhaseOutcome` and `emit`.
- **Path-traversal defense (O4)** — `text.ts:8`. `sanitizeTaskId` regex `^[a-zA-Z0-9_-]+$`, max 128 chars.

---

## Layer 2 — `src/session/` (agent lifecycle, 1706 LOC, 3 files)

### `sdk.ts` (245 LOC)
Thin wrapper around `@anthropic-ai/claude-agent-sdk`. Manages spawn, resume, stream consumption, abort.

- **`SDKClient.spawnSession(SessionConfig)`** — returns `{ query, abortController }`. Sets `persistSession: true` default (sdk.ts:134), explicit `hooks: {}` to block filesystem-discovered hooks (Wave 1 Item 2, sdk.ts:162), `enabledPlugins` via `Options.settings.enabledPlugins` cast (Wave 1 Item 1, sdk.ts:156).
- **`SessionResult`** — captures `modelName` from `system_init` message (sdk.ts:189-191); used by orchestrator for `Model: <name>` commit trailer (WA-5).
- **`resumeSession(sessionId, config)`** — wraps `spawnSession({ ...config, resume: sessionId })`. Per Context7 SDK docs, `resume` ONLY restores conversation history; tool restrictions, plugins, `settingSources`, `permissionMode`, hooks must be re-supplied per call.

### `manager.ts` (547 LOC) — Executor tier
Worktree lifecycle, task spawn, completion parsing, persistent-session disk-growth mitigation.

- **Worktree pattern** — `harness/task-{id}` branch, `{worktree_base}/task-{id}` dir. `createWorktree` at line 247; `cleanupWorktree` at line 254 also kills tmux sessions matching pattern (Wave 1 Item 4).
- **`DEFAULT_DISALLOWED_TOOLS`** (manager.ts:183-189) — Cron* / RemoteTrigger / ScheduleWakeup. Lifecycle-escaping tools.
- **`DEFAULT_PLUGINS`** (manager.ts:202-205) — `caveman@caveman: false`, `oh-my-claudecode@omc: false`. Both flipped OFF in **commit `4b425d5`** based on spike findings:
  - U1 (spike-caveman-json): caveman dropped `commitSha` 5/5 runs (87.5% top-field preservation, below 95% threshold)
  - U2 (spike-omc-overhead): OMC adds 25% wall-time without specialist invocation on single-mode Executor
- **`persistSession: true`** (manager.ts:407) — required for resume on escalation/dialogue (regression fix).
- **`hooks: {}`** explicit (manager.ts:410) — Wave 1 Item 2 to block `persistent-mode.cjs`.
- **`validateCompletion`** (manager.ts:64-100) — lenient parser; strips malformed enrichment via B7 pattern.
- **`listMissingEnrichment`** + **`readCompletion` warn** (manager.ts:104-111, 490-498) — added in commit `4b425d5` for U3 observability. Surfaces silent Executor omission of `commitSha` / `understanding` / `assumptions` / `nonGoals` / `confidence`.
- **`pruneSessionDir({ maxAgeMs?, maxRecords? })`** (manager.ts:328-366) — added in commit `4b425d5` for U4 unbounded-growth mitigation. Default 7-day cutoff; composes both filters.
- **`cumulativeSessionSpawns` threshold warn** (manager.ts:419-429) — early-warning when persistent-session count exceeds `PERSISTENT_SESSION_WARN_THRESHOLD_DEFAULT` (100).

### `architect.ts` (914 LOC) — Architect tier
Persistent session per project for decomposition, arbitration, crash recovery, compaction.

- **Worktree pattern** — `harness/architect-{projectId}` branch, `{worktree_base}/architect-{projectId}` dir. Cleanup pending Wave 1.5b `projectStore.hasActivePhases()` guard.
- **`ARCHITECT_DISALLOWED_TOOLS`** (architect.ts:46-54) — superset of executor: also blocks `WebFetch`, `WebSearch`, `TeamCreate`, `TeamDelete`. Network reach + team lifecycle blocked.
- **`ARCHITECT_DEFAULTS.plugins`** (architect.ts:172-179) — `oh-my-claudecode@omc: true`, `caveman@caveman: true`. Confirmed via spike-architect-caveman v3+v4 (commit `dc1bf81`): with proper architect-prompt.md loaded, caveman ON preserved 100% verbatim description+nonGoals across 2 runs. Architect needs OMC for decomposer subagents.
- **Four `resumeSession` sites** at lines 316 / 402 / 503 / 596 (decompose / relayOperatorInput / runArbitration / requestSummary). All previously missing security config; **commit `25ae1ca`** added **`buildResumeConfig` helper** (architect.ts:716-731) ensuring consistent `disallowedTools`, `enabledPlugins`, `settingSources`, `permissionMode`, `hooks` across all four sites. Per Context7 SDK docs, this is the only safe pattern.
- **`ArchitectVerdict` union** (architect.ts:120-123) — three types: `retry_with_directive`, `plan_amendment`, `escalate_operator`. No `executor_correct` (architect doesn't have authority to vouch for executor).
- **`fenceEscape`** (architect.ts:42-44) — neutralize ≥3-backtick runs in `<untrusted:*>` embeds (operator name/description/nonGoals + Executor-authored completion).
- **`validateArchitectCompactionSummary`** (architect.ts:63-115) — full schema validation. **commit `bc98868`** fixed line 109-113 to accept `lastDirective: null` (Architect emits null on fresh compaction; rejecting forced every real compaction into projectStore-derived fallback).
- **`requestSummary`** (architect.ts:574-640) — emits `.harness/architect-summary.json` per §9 contract. Falls back to projectStore-derived summary on schema failure (preserves verbatim nonGoals invariant either way).
- **`compact`** (architect.ts:557-568) — fires when `session.totalCostUsd >= compactionThresholdPct × project.budgetCeilingUsd` (default 0.60). Aborts current session, calls `respawn(reason: "compaction", summary)`, increments `compactionGeneration`.
- **`persistSession: true`** across spawn (line 751) and respawn (line 725) — context survives phases.

---

## Layer 3 — `src/gates/` (merge + review, 760 LOC, 2 files)

### `merge.ts` (402 LOC)
Exclusive FIFO merge queue; propose-then-commit, rebase, test with timeout, atomic merge or revert.

- **FIFO exclusivity** — `processing` flag at merge.ts:219, 291-310. One merge at a time.
- **Propose-then-commit (WA-4)** — orchestrator authors commit message; Executor's local commits are amended/scrubbed depending on case (a/b/c at merge.ts:325-350).
- **Rebase-before-merge** — merge.ts:354-361. Conflict → `rebase_conflict` status; caller (orchestrator) implements retry loop.
- **Test-and-revert** — merge.ts:371-387. Runs `test_command` with timeout; on failure `git reset --hard ORIG_HEAD` to preserve trunk.
- **Test timeout distinction** — merge.ts:143-146. SIGTERM kill returns `{ success: false, output: "TIMEOUT" }` distinct from `test_failed`.
- **MergeResult statuses** — `merged | test_failed | test_timeout | rebase_conflict | error`.

### `review.ts` (358 LOC)
Mandatory-for-project review gate; ephemeral Reviewer session; verdict-or-default-reject.

- **Ephemeral Reviewer** (review.ts:210, plan M.13.4 LOCKED) — `persistSession: false`, NO plugins, read-only tools `[Read, Grep, Glob, LS]` (review.ts:211-220). M2 security: blocks Edit/Write/Bash, WebFetch/WebSearch, Task/Agent, Cron*/RemoteTrigger/ScheduleWakeup.
- **Stale-file defense (security H2)** — pre-spawn `unlinkSync(.harness/review.json)` (review.ts:193); post-spawn freshness check `mtime ≥ startMs - 2s` (review.ts:258-261).
- **Fenced untrusted input** (review.ts:279-282) — Executor completion + task prompt capped + triple-backtick-escaped before embed.
- **Fail-safe default reject** (review.ts:333-357) — all riskScore weights 1.0 (maximum uncertainty), critical finding with reason. Never approves on ambiguous output.
- **Mandatory-for-project ratchet** — `arbitrationThreshold` (default 2) tracks max rejections before escalation to Architect.
- **`REVIEWER_DEFAULTS`** — model `claude-sonnet-4-6`, max_budget $1.0, reject_threshold 0.55, timeout 180s.

---

## Layer 4 — `src/discord/` (Discord I/O, 3504 LOC, 17 files)

### Outbound flow
```
OrchestratorEvent → DiscordNotifier.handleEvent (notifier.ts:378 LOC)
  → resolveProjectId / NOTIFIER_MAP lookup → renderEpistle (epistle-templates.ts:196 LOC)
  → resolveIdentity (identity.ts:64 LOC) → DiscordSender (per-channel map)
  → WebhookSender (sender.ts:155 LOC) | BotSender (bot-sender.ts:208 LOC)
```

### Inbound flow
```
Discord WS → RawWsBotGateway (bot-gateway.ts:296 LOC)
  → filter chain (self-id, allowed-channels, webhook-id self-suppression)
  → InboundDispatcher (dispatcher.ts:555 LOC)
  → precedence: rule 1 (mention) > rule 2-4 (reply-routing) > rule 5 (command/NL)
  → architectManager.relayOperatorInput | commandRouter.handleCommand | handleNaturalLanguage
```

### Module-by-module

| File | LOC | Purpose |
|------|----:|---------|
| `types.ts` | 137 | `DiscordSender`, `BotGateway`, `AgentIdentity`, `AllowedMentions` interfaces (pure types) |
| `identity.ts` | 64 | `resolveIdentity(event)` — exhaustive switch over OrchestratorEvent → IdentityRole (executor / reviewer / architect / orchestrator) |
| `identity-map.ts` | 55 | `buildIdentityMap(config)` — case-insensitive username → agent-key lookup; throws on duplicates |
| `message-context.ts` | 64 | LRU cache: Discord messageId → projectId (CW-3); reply-routing source |
| `channel-context.ts` | 95 | Per-channel ring buffer (CW-4.5); affinity hints for project resolution |
| `accumulator.ts` | 104 | Debounce rapid NL messages (default 2s); commands bypass |
| `client.ts` | 33 | `ReactionClient` interface + `NoopReactionClient` stub (CW-5) |
| `sender.ts` | 155 | `WebhookSender` — webhook-based sender, token bucket rate limit (2s spacing) |
| `bot-sender.ts` | 208 | `BotSender` — bot-token REST API; 429 retry-after handling (Phase 4 H1) |
| `sender-factory.ts` | 159 | `buildSendersForChannels` — webhook URL SSRF defense via anchored regex (Phase 4 M1) |
| `bot-gateway.ts` | 296 | `RawWsBotGateway` — Discord WS v10; IDENTIFY/HEARTBEAT/RESUME; exponential reconnect backoff (Phase 4 M3) + zombie detection (M4) |
| `epistle-templates.ts` | 196 | `renderEpistle(event, identity, ctx)` — multi-paragraph templates for 7 epistle-eligible event types (Wave E-α D3) |
| `notifier.ts` | 378 | `DiscordNotifier` — `NOTIFIER_MAP` table-driven event → (channel, identity, formatter) routing |
| `commands.ts` | 467 | `CommandRouter` — parses `!cmd` and NL; `CommandIntent` union (9 variants); two-stage NL (regex → LLM fallback) |
| `intent-classifier.ts` | 452 | `LlmIntentClassifier` — single-shot Haiku, cost-bounded $0.05, security fence-escape, allowedTools=[] |
| `response-generator.ts` | 285 | `LlmResponseGenerator` — operator-visible reply prose; $0.02 cap, 8s timeout, fallback to `StaticResponseGenerator` |
| `dispatcher.ts` | 555 | `InboundDispatcher` — multi-stage routing precedence (mention → reply → command/NL); CW-5 reactions; CW-4.5 mention extraction |

### Discord-specific invariants
- **`truncateBody(1900)` discipline** — every Discord-bound text capped (notifier, epistle-templates, response-generator). 1900 = Discord 2000-char limit minus headroom.
- **`allowedMentions: { parse: [] }`** — defense-in-depth against `@everyone`/`@here` injection (every outgoing payload).
- **Webhook URL anchoring** — `extractWebhookIdFrom` (sender-factory.ts) regex anchored to `discord(app)?.com` only. Phase 4 M1 prevents SSRF / open-redirect via `evil.com?next=...`.
- **Resolve-on-drop** — token bucket queue overflow drops oldest message + resolves promise (never rejects). Discord hiccups never crash pipeline.
- **Fence-escape in LLM paths** — `stripFenceTokens` (intent-classifier, response-generator) neutralizes `<operator_message>`, `<fields>`, `<kind>`, `<system>` markers in interpolated user data.
- **CW-5 reactions stub** — `NoopReactionClient` placeholder until bot-login lane lands (reactions need authenticated REST, not webhook).

### Wave E-α latest landed (commits `66801b0` / `5bec3dc` / `72a3ea0`)
- Identity diversification: `session_complete` + `task_done` → Executor identity (was Harness); `review_*` → Reviewer; `architect_*` → Architect; system events stay Orchestrator
- `OrchestratorEvent.task_done` extended with optional `summary?` + `filesChanged?`
- `markPhaseSuccess` collapse pattern at orchestrator (single re-read)
- `formatFindingForOps` extracted to `lib/review-format.ts` (back-compat re-export from `gates/review.ts`)
- Architecture invariant guard `tests/lib/no-discord-leak.test.ts` enforces I-1 at CI

---

## Layer 5 — `src/orchestrator.ts` (1084 LOC, sole event bus)

The orchestrator is the daemon: polls task_dir, spawns sessions, routes through gates, handles crash recovery + arbitration.

### Routing precedence (orchestrator.ts:290-310)
1. `projectId` set → `routeByProject` (Wave B stub, returns false today)
2. `mode === "dialogue" && !projectId` → `DialogueSession` (not implemented)
3. otherwise → standard Executor pipeline

**Conflict rule:** `projectId + mode:dialogue` REJECTED at ingest (`TaskFileValidationError` at orchestrator.ts:77-82).

### Task lifecycle
```
scanForTasks → createTask (sanitizeTaskId O4)
  → processTask
  → spawnTask (SessionManager.spawnTask — wo+rktree, SDK session)
  → completion read → escalation check (priority over completion)
  → routeByResponseLevel (level ≥ 2 → review)
  → routeReview (approve/reject/request_changes)
    → on reject: project → arbitration threshold check → routeArbitration
  → enqueueProposed (MergeGate)
  → handleMergeResult → markPhaseSuccess + cascadePhaseOutcome + emit task_done
```

### `OrchestratorEvent` union (orchestrator.ts:107-136) — 27 verbatim event types

Categorized:
- **Session/task** (6): `task_picked_up`, `session_complete`, `merge_result`, `task_shelved`, `task_failed`, `task_done`
- **Phase 2A** (6): `escalation_needed`, `checkpoint_detected`, `response_level`, `completion_compliance`, `retry_scheduled`, `budget_exhausted`
- **Wave 2 project** (5): `project_declared`, `project_decomposed`, `project_completed`, `project_failed`, `project_aborted`
- **Architect** (8): `architect_spawned`, `architect_respawned`, `architect_arbitration_fired`, `arbitration_verdict`, `review_arbitration_entered`, `review_mandatory`, `budget_ceiling_reached`, `compaction_fired`
- **System** (2): `poll_tick`, `shutdown`

### Crash recovery (WA-6 Fresh-2, orchestrator.ts:983-1071)
- `active`/`reviewing` → cleanup + fail + back to `pending` (re-run from scratch)
- `review_arbitration` → fail (escalate to operator; restart required)
- `shelved` → `scheduleRetry`
- `merging` → branch-commits-ahead-of-trunk check:
  - (a) no commits → cleanup + re-run
  - (b) has commits → `enqueueProposed { alreadyCommitted: true }`
- Bounded by `MAX_RECOVERY_ATTEMPTS = 3` (state.ts `recoveryAttempts` counter)

### Architect interaction
- `declareProject(name, desc, nonGoals)` → `projectStore.createProject` → `architect.spawn` → `architect.decompose` → phase files dropped → poll loop picks up
- `processArchitectVerdict` (Architect arbitration result):
  - `retry_with_directive` → unshelve task, store `lastDirective` for next prompt
  - `plan_amendment` → `projectStore.updatePhaseSpec`, retry
  - `escalate_operator` → fail task, emit `arbitration_verdict` to ops_channel
- `cascadePhaseOutcome` → mark phase done/failed → `hasActivePhases` check → finalize project

### Daemon shutdown
- Graceful: drains MergeGate, aborts active sessions, calls `architectManager.shutdownAll`, persists state
- Hooks: SIGTERM/SIGINT handlers; ESC key in TTY mode

---

## Cross-cutting themes

### 1. Disallowed-tools hierarchy
| Layer | Set | What it blocks |
|-------|-----|----------------|
| Default Executor | `DEFAULT_DISALLOWED_TOOLS` (manager.ts:183) | Cron* / RemoteTrigger / ScheduleWakeup |
| Architect | `ARCHITECT_DISALLOWED_TOOLS` (architect.ts:46) | DEFAULT + WebFetch / WebSearch / TeamCreate / TeamDelete |
| Reviewer | `allowedTools: [Read, Grep, Glob, LS]` (review.ts:211) | strict allowlist; everything else implicitly blocked |
| Intent classifier | `allowedTools: []` + explicit disallow (intent-classifier.ts) | all tool use forbidden; single-shot LLM only |

### 2. Plugin discipline
| Tier | OMC | caveman | Rationale |
|------|-----|---------|-----------|
| Executor | OFF | OFF | Spike U1 (caveman drops `commitSha` 5/5), U2 (OMC adds 25% wall-time, no specialist invocation) |
| Architect | ON | ON | Architect needs OMC subagents for decomposition. Spike-architect-caveman v4 confirmed caveman-on doesn't break verbatim contract when systemPrompt is properly loaded. |
| Reviewer | none | none | Locked at gate (review.ts:222) — no plugins, ephemeral session |

### 3. SDK resume contract
Per Context7 docs for `@anthropic-ai/claude-agent-sdk`: `resume` ONLY restores conversation history. Tool restrictions, plugins, settingSources, permissionMode, and hooks must be re-supplied PER QUERY. Architect's `buildResumeConfig` helper (commit `25ae1ca`) is load-bearing for I-1; missing it would silently restore WebFetch/WebSearch on resumed turns.

### 4. Atomic file-write pattern (O3)
Used in StateManager + ProjectStore + (will be used in) LlmBudgetTracker. Pattern: `writeFileSync(tmp, json); renameSync(tmp, target)` with UUID temp suffix. Crash-safe; partial writes never observable.

### 5. Stale-file defenses
| Site | Pattern | Purpose |
|------|---------|---------|
| ReviewGate | unlink pre-spawn (review.ts:193) + freshness mtime check (review.ts:258) | Reviewer's own output, not stale prior verdict |
| ArchitectManager | unlink pre-spawn for verdict file (architect.ts:526) | Same |
| SessionManager.readCompletion | NO pre-spawn unlink | **GAP** — could read stale completion.json from prior task with same id |

### 6. Three-tier persistence
- **`TaskRecord`** (StateManager) — single task execution state
- **`ProjectRecord`** (ProjectStore) — multi-phase coordination
- **`ArchitectSession`** (ArchitectManager, in-memory) — session continuity across phases + compaction

### 7. B7 forward-compat
`KNOWN_KEYS` whitelists at deserialize (state.ts:98-111, project.ts:90-108). Optional fields can be added without migration. Removed/renamed fields require explicit migration step.

### 8. I-1 enforcement
Architecture invariant guard `tests/lib/no-discord-leak.test.ts` (Wave E-α D8) reads every file under `src/session/*` and rejects any non-type import from `src/discord/`. Type-only imports allowed via negative-lookahead regex (erased at compile, no runtime coupling).

### 9. Observability layered into runtime
- **`listMissingEnrichment` warn** (manager.ts:104, commit `4b425d5`) — surface silent Executor U3 drops
- **`cumulativeSessionSpawns` threshold warn** (manager.ts:419) — early alert for U4 disk growth
- **`pruneSessionDir` helper** (manager.ts:328, commit `4b425d5`) — operator-callable cleanup; default 7-day cutoff
- **OrchestratorEvent bus** — every notable lifecycle hits Discord ops_channel; operator sees state changes in real time

---

## Recent commits (this session 2026-04-27)
| Hash | Layer | Subject |
|------|-------|---------|
| `25ae1ca` | session/architect.ts | Re-supply disallowedTools+plugins on resumeSession via `buildResumeConfig` helper |
| `4b425d5` | session/manager.ts + tests | Drop caveman+OMC from Executor defaults; add `listMissingEnrichment` warn + `pruneSessionDir` helper |
| `bc98868` | session/architect.ts + tests | Validator accepts `lastDirective: null` (Architect emits null on fresh compaction) |
| `dc1bf81` | scripts/spike-architect-caveman.ts | Spike 6 — verifies Architect verbatim contract under caveman ON; 4 iter total |

Test count: 755 → 763 (+8 new). Lint clean. `audit:epistle-pins` clean.

---

## Open issues / risks (not yet addressed)

- **MergeGate rebase-conflict retry loop missing** — caller (orchestrator) currently has `MAX_RECOVERY_ATTEMPTS = 3` for recovery but no explicit retry-on-conflict counter. Pending Wave C.
- **`SessionManager.readCompletion` stale-file gap** — no pre-spawn unlink of `.harness/completion.json`. Could theoretically read stale data if task id reused. Lower-priority; current ID generation is unique.
- **`cleanupProject` no `hasActivePhases` guard yet** — manager.ts:269-261 TODO Wave 1.5b. Today the cleanup is unconditional; caller must verify project terminal state.
- **`compaction_fired` stale check** — Architect compaction triggers when cost crosses threshold but doesn't snapshot pre-compaction state for diff comparison. Manual operator inspection if compaction misbehaves.
- **`BUG/FIXME/TODO` comments in code** — sdk.ts:153 (SDK type opacity), manager.ts:259 (Wave 1.5b projectId-based tmux sweep). Low severity; documented.
- **Empty placeholder dirs** — `src/events/` and `src/orchestrator/` exist but contain no code. Unclear intent; either remove or document Wave plan.
- **Wave E-β/γ/δ not started** — see [[phase-e-agent-perspective-discord-rendering-intended-features]] for intended scope. E-γ plan written today at `.omc/plans/2026-04-27-discord-wave-e-gamma.md`.

---

## Cross-references

- [[harness-ts-architecture]] — vision + high-level overview
- [[harness-ts-phase-roadmap]] — delivery history (Phase 0 → 4 pending)
- [[harness-ts-types-reference-source-of-truth]] — verbatim type signatures + 27-event allow-list
- [[harness-ts-core-invariants]] — 10 architectural rules (I-1..I-10)
- [[harness-ts-common-mistakes]] — repeated mistakes catalog (M-1..M-12)
- [[harness-ts-live-setup]] — `live-*.ts` script recipes
- [[harness-ts-plan-index]] — index of `.omc/plans/*.md` files
- [[harness-ts-wave-c-backlog]] — deferred items + P1/P2 follow-ups
- [[phase-e-agent-perspective-discord-rendering-intended-features]] — Phase E intended scope
- [[ralplan-procedure-failure-modes-and-recommended-mitigations]] — RALPLAN consensus loop postmortem
- [[session-log-wave-e-completion-2026-04-27]] — Wave E-α completion log
- `.omc/plans/2026-04-27-discord-wave-e-gamma.md` — Wave E-γ plan (manual-write)

