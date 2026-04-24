# Phase 2B + Phase 3: Discord Integration, Review Gate, Dialogue Agent

**Date:** 2026-04-12
**Status:** APPROVED — Consensus reached (Architect APPROVE_WITH_CONDITIONS → Critic APPROVE)
**SUPERSEDED IN PART:** `.omc/plans/ralplan-harness-ts-three-tier-architect.md` (2026-04-23) replaces Waves 5 and 6 with three-tier Waves A, B, B.5, C, 6-split, D. Waves 1, 1.5, 1.75, 2, 3, 4 remain as approved with additive extensions documented in the three-tier plan's Section B integration map.
**Depends:** Phase 2A (COMPLETE, 280 tests), Phase 1.5 (`resumeSession` verified)
**Goal:** Operator controls the pipeline via Discord. High-stakes tasks pass independent review. Ambiguous tasks go through dialogue before implementation.

---

## RALPLAN-DR Summary

### Principles (5)

1. **Discord is a view layer, not a controller.** All state mutations happen through the orchestrator and state machine. Discord modules translate between Discord events and orchestrator actions. If Discord goes down, the pipeline still runs — tasks dropped as files still work, escalations still pause tasks, merges still happen.

2. **Injectable everything.** Discord.js `Client`, `WebhookClient`, and LLM classify calls are all injectable interfaces. Tests never touch Discord or LLM APIs. Same pattern as `GitOps`, `MergeGitOps`, `QueryFn`.

3. **Pre-requisites gate Wave 1.** The 4 open critic/architect findings (OMC plugin loading, hook defense, cron/remote blocking, tmux cleanup) must land before Discord integration. They affect every agent session and failing to resolve them first means building on a broken foundation.

4. **Informational-then-gating graduation.** Phase 2A made response levels, checkpoints, and budget alarms informational. Phase 3 promotes them to hard gates: level 2+ pauses for review, checkpoints pause until reviewed, budget exhaustion already handled. This is the same graduation pattern — observe first, gate second.

5. **Single new dependency.** `discord.js` is the only new runtime dependency. The review gate and dialogue agent use the existing `SDKClient` (SDK sessions). LLM classify calls use the same SDK `query()` function. No new external services.

### Decision Drivers (Top 3)

1. **Pre-requisite safety.** OMC plugin loading (#1) and hook defense (#3) directly affect agent session behavior. If OMC plugins don't load, agents lose orchestration capabilities. If `persistent-mode.cjs` fights `abortController`, sessions hang on abort. These must be resolved before building anything on top.

2. **Testability without Discord.** Every Discord module must be testable with mocked clients. The existing test suite (280 tests) uses `vi.fn()` mocks for all external deps. Discord integration follows the same pattern. Integration tests verify event flow without real Discord connections.

3. **Review gate as merge prerequisite.** The review gate inserts between session completion and merge. It must integrate cleanly with the existing `processTask()` flow in the orchestrator. The merge gate's FIFO queue and the review gate's SDK session both return promises — they compose naturally.

### Viable Options

#### Option A: Webhook-only Discord (no bot, no message listening) — REJECTED

Use Discord webhooks for all outbound notifications. No `discord.js` Client, no inbound message handling. Task submission stays file-based only. Escalation responses via CLI or file drop.

**Pros:** Zero bot setup. No gateway connection. Simplest possible Discord integration. No message accumulator needed.
**Cons:** No operator task submission via Discord. No escalation response via Discord. No dialogue channel. Misses the core goal — operator controls pipeline via Discord. Reduces Discord to a notification sink.

**Invalidation rationale:** The architecture doc explicitly requires bidirectional Discord interaction: task submission, escalation response, and dialogue. Webhook-only delivers ~20% of the value (notifications only) while the remaining 80% (operator interaction) is the primary goal.

#### Option B: discord.js Client + Webhook hybrid (SELECTED)

`discord.js` Client for inbound (message listening, reactions). Webhook for outbound (per-agent identity, formatted notifications). Bot token for authentication. Message accumulator for NL debounce.

**Pros:** Full bidirectional interaction. Per-agent webhook identity (different username/avatar per agent role). Message accumulator prevents split-message misrouting. Established discord.js patterns.
**Cons:** Requires bot token setup. Gateway connection management. More code (~400 lines discord modules). Need to handle Discord rate limits and reconnection.

**Why chosen:** Matches the architecture doc requirements. Webhook outbound gives per-agent identity without managing webhook-per-channel. Client inbound handles all operator interaction patterns (commands, NL, reactions, escalation responses). The hybrid avoids the bot needing to send-as-different-users (which requires webhooks anyway).

### ADR

**Decision:** Implement Phase 2B as discord.js Client (inbound) + Webhook (outbound) with 4 pre-requisite fixes in Wave 1. Phase 3 adds review gate (separate SDK session) and dialogue agent (single-session-with-pause via `resumeSession()`). 6 waves total.

**Drivers:** Pre-requisite safety, testability, bidirectional Discord requirement, review-before-merge for high-stakes tasks.

**Alternatives considered:** Webhook-only (Option A) — rejected because it only delivers notifications, missing the core operator interaction goal.

**Why chosen:** Option B is the only option that delivers the full architecture doc requirements. The discord.js + webhook hybrid is a well-understood pattern with clean separation between inbound (Client) and outbound (Webhook).

**Consequences:** 1 new runtime dependency (`discord.js`, pinned to `^14.x`). ~12 new source files across `src/discord/`, `src/gates/`, `src/session/`. ~15 new test files. Orchestrator gains ~80 lines of routing for review gate and dialogue. Config gains discord-specific fields already defined in `project.toml`. `TaskFile` interface extended with optional `mode: "dialogue" | "reviewed"` field. `TaskRecord` extended with dialogue fields. `KNOWN_KEYS` and `VALID_TRANSITIONS` updated accordingly.

**Follow-ups:** Phase 4 (observability) builds on the event stream established here. Earned autonomy modulates review thresholds. Multi-task concurrency adds thread-per-task Discord UX.

---

## Wave Breakdown

### Wave 1: Pre-Requisites (4 items, no Discord dependency)

These are the 4 OPEN critic/architect findings that must be resolved first. They affect every agent session.

---

#### Item 1: OMC Plugin Loading via SDK `Options.settings`

**What:** Load OMC and caveman marketplace plugins in SDK agent sessions. Two candidate SDK paths exist — `Options.settings.enabledPlugins` (flag layer) and `Options.plugins` (local path array). **Implementation requires empirical verification** of which path works for marketplace plugins before committing.

**Why critical:** Without OMC plugins, agents lose orchestration capabilities (`/team`, skill invocation, hook system). Without caveman, agents lose cost control. Both are intentional capabilities the user wants agents to have.

**Empirical verification step (MUST run before writing production code):**
```typescript
// Test script: scripts/verify-plugin-loading.ts
// Spawn two sessions, one per path. Check which loads OMC successfully.
// Path A: Options.settings = { enabledPlugins: { "oh-my-claudecode@omc": true } }
// Path B: Options.plugins = [{ name: "oh-my-claudecode", version: "latest" }]
// Also verify: permissionMode: "plan" works for read-only review sessions (Item 12)
// Success criteria: agent session has access to OMC skills (e.g., /oh-my-claudecode:cancel)
// Run: npx tsx scripts/verify-plugin-loading.ts
```
This is a sub-hour spike — run both, pick the winner, delete the script.

**Pre-requisite fix: `persistSession` override in manager.ts (Architect finding #1):**
`manager.ts:208` explicitly passes `persistSession: false`, overriding the SDK default fix from Phase 2A. This breaks `resumeSession()` for escalation (Item 10), dialogue (Items 11, 14), and any future resume-based flow. Fix: change `manager.ts:208` to `persistSession: true`. This is a 1-line fix applied at the start of Wave 1 alongside the plugin loading work. Sessions that don't need resume incur negligible overhead from persistence (sessions are cleaned up on worktree removal anyway).

**Files:**
- CREATE `scripts/verify-plugin-loading.ts` — one-shot verification spike (~40 lines, deleted after)
- MODIFY `src/session/sdk.ts` — add plugin field to `Options` construction in `spawnSession()` (~15 lines)
- MODIFY `src/session/sdk.ts` — add plugin config to `SessionConfig` interface (~3 lines)
- MODIFY `src/session/manager.ts` — pass plugin config from `HarnessConfig` to `SessionConfig` (~5 lines)
- MODIFY `src/lib/config.ts` — add `plugins` section to config types for explicit plugin list (~10 lines)

**Interface changes (Path A — adjust if Path B wins):**
```typescript
// Addition to SessionConfig in sdk.ts
enabledPlugins?: Record<string, boolean>;

// In spawnSession(), add to Options construction:
if (config.enabledPlugins) {
  options.settings = {
    enabledPlugins: config.enabledPlugins,
  };
}
```

**Default plugins (hardcoded in SessionManager, overridable via config):**
```typescript
const DEFAULT_PLUGINS: Record<string, boolean> = {
  "oh-my-claudecode@omc": true,
  "caveman@caveman": true,
};
```

**Acceptance criteria:**
- Empirical verification script run and results documented in commit message
- SDK plugin loading uses the verified path (A or B)
- Both OMC and caveman plugins specified by default
- Config can override/extend plugin list
- `settingSources: ["project"]` remains (loads CLAUDE.md + project settings)
- Existing 280 tests pass (mocked SDK doesn't care about new options fields)

**Test strategy:**
- `tests/session/sdk.test.ts`: 3 tests — plugins passed through to Options, default plugins applied, custom plugins override
- `tests/session/manager.test.ts`: 1 test — manager passes plugin config to SDK

**Estimated effort:** ~33 lines source, 4 tests (+40 line spike script, deleted after)

---

#### Item 2: Persistent-Mode Hook Defense

**What:** When Item 1 enables OMC plugin loading, `persistent-mode.cjs` (a filesystem-discovered hook from the OMC plugin) would fight the SDK's `abortController`. The hook tries to keep sessions alive; the harness tries to abort them. Fix: pass explicit `hooks: {}` in SDK Options to prevent filesystem-discovered hooks from loading while still allowing programmatic hooks.

**Why high priority:** Without this fix, `abortTask()` and `abortAll()` would race against the persistent-mode hook trying to restart sessions. Sessions would hang on shutdown.

**Files:**
- MODIFY `src/session/sdk.ts` — add `hooks: {}` to Options construction in `spawnSession()` (~3 lines)
- MODIFY `src/session/sdk.ts` — add `hooks` to `SessionConfig` interface for future programmatic hooks (~2 lines)

**Interface changes:**
```typescript
// Addition to SessionConfig
hooks?: Partial<Record<string, unknown[]>>;

// In spawnSession(), always set hooks to prevent filesystem discovery:
options.hooks = config.hooks ?? {};
```

**Acceptance criteria:**
- `options.hooks` is always set (empty object by default)
- Filesystem-discovered hooks (including `persistent-mode.cjs`) do not load
- Future programmatic hooks can be passed via `SessionConfig.hooks`
- Existing tests pass

**Test strategy:**
- `tests/session/sdk.test.ts`: 2 tests — empty hooks by default, custom hooks passed through

**Estimated effort:** ~5 lines source, 2 tests

---

#### Item 3: Block Cron/Remote Triggers via `disallowedTools`

**What:** Block `CronCreate`, `CronDelete`, `CronList`, `RemoteTrigger`, `ScheduleWakeup` via `disallowedTools` in session config. These tools create resources (cron jobs, remote triggers) that outlive agent sessions with no cleanup path in the harness.

**Files:**
- MODIFY `src/session/manager.ts` — add default `disallowedTools` to `spawnTask()` (~8 lines)
- MODIFY `src/lib/config.ts` — add `disallowed_tools` to `PipelineConfig` for config-level overrides (~3 lines)

**Interface changes:**
```typescript
// Default blocked tools (in SessionManager)
const DEFAULT_DISALLOWED_TOOLS: string[] = [
  "CronCreate", "CronDelete", "CronList",
  "RemoteTrigger", "ScheduleWakeup",
];
```

**Acceptance criteria:**
- Default disallowed tools list blocks all 5 lifecycle-escaping tools
- Config can extend (but not reduce) the default list
- `disallowedTools` passed to SDK `Options` in `spawnSession()`
- User's desired tools (`/team`, `omc-teams`, etc.) are NOT blocked

**Test strategy:**
- `tests/session/manager.test.ts`: 2 tests — default disallowed tools applied, config extends list
- `tests/session/sdk.test.ts`: 1 test — disallowedTools passed to Options

**Estimated effort:** ~11 lines source, 3 tests

---

#### Item 4: Tmux Cleanup in Worktree Lifecycle

**What:** When agents use `/team` or `omc-teams`, tmux sessions are spawned outside the SDK lifecycle. Add tmux cleanup to `cleanupWorktree()` and a cleanup sweep to `shutdown()`/`abortAll()`.

**Files:**
- MODIFY `src/session/manager.ts` — add tmux kill to `cleanupWorktree()` (~8 lines)
- MODIFY `src/session/manager.ts` — add tmux sweep to `abortAll()` (~5 lines)
- MODIFY `src/session/manager.ts` — add `TmuxOps` injectable interface (~10 lines)

**Interface changes:**
```typescript
// New injectable interface
export interface TmuxOps {
  killSessionsByPattern(pattern: string): void;
}

export const realTmuxOps: TmuxOps = {
  killSessionsByPattern(pattern: string): void {
    try {
      // tmux -t doesn't support globs — list sessions and filter
      const output = execSync(`tmux list-sessions -F "#{session_name}" 2>/dev/null`, {
        stdio: ["pipe", "pipe", "pipe"],
        encoding: "utf-8",
      });
      for (const name of output.trim().split("\n")) {
        if (name.includes(pattern)) {
          try { execSync(`tmux kill-session -t "${name}"`, { stdio: "pipe" }); } catch { /* already dead */ }
        }
      }
    } catch { /* no tmux server or no sessions */ }
  },
};
```

**Cleanup logic:**
- `cleanupWorktree(taskId)`: after git worktree removal, call `tmuxOps.killSessionsByPattern(`task-${taskId}`)` — lists all tmux sessions, kills those containing the pattern
- `abortAll()`: after aborting all sessions, call `tmuxOps.killSessionsByPattern("harness-")` to sweep all harness-spawned tmux sessions

**Acceptance criteria:**
- Tmux sessions matching `*task-{id}*` are killed on worktree cleanup
- `abortAll()` sweeps all harness-related tmux sessions
- `TmuxOps` is injectable (tests use mock)
- Failure to kill tmux (no server, no matching sessions) is swallowed silently

**Test strategy:**
- `tests/session/manager.test.ts`: 3 tests — tmux kill called on cleanup, tmux sweep on abortAll, tmux failure swallowed

**Estimated effort:** ~23 lines source, 3 tests

---

### Wave 1.5: Orchestrator Decomposition + State Schema Update

Depends: Wave 1 complete. Required before Waves 2-6 add more routing to `processTask()`.

**Rationale (Critic Major #1):** `processTask()` is currently 224 lines handling spawn → escalation check → retry/escalation logic → response routing → merge gate → 5 merge result branches → catch block. Waves 4-6 add review gate routing, dialogue routing, and `resolveEscalation()`. Without decomposition, `processTask()` would grow to ~350+ lines with deeply nested control flow.

---

#### Item 1.5a: Extract `processTask()` Into Composable Methods

**What:** Split `processTask()` into focused methods that compose in the main flow. Each method handles one phase of the task lifecycle.

**Files:**
- MODIFY `src/orchestrator.ts` — extract 4 methods from `processTask()` (~0 net new lines, reorganization)

**Extracted methods:**
```typescript
/** Handle session failure: retry, escalate, or permanent fail */
private handleSessionFailure(task: TaskRecord, result: SessionResult, completion: CompletionSignal | null): Promise<void>;

/** Handle merge result: success, conflict, test failure, error */
private handleMergeResult(task: TaskRecord, mergeResult: MergeResult, completion: CompletionSignal): void;

/** Evaluate whether review gate should fire. Returns true if review needed. */
private shouldReview(task: TaskRecord, completion: CompletionSignal, result: SessionResult, responseLevel: ResponseLevel): boolean;

/** Route task based on response level: merge, review, or escalation_wait */
private routeByResponseLevel(task: TaskRecord, completion: CompletionSignal, result: SessionResult): Promise<void>;
```

**Resulting `processTask()` structure (~60 lines):**
```typescript
async processTask(task: TaskRecord): Promise<void> {
  try {
    const { result, completion } = await this.sessions.spawnTask(task);
    this.emit({ type: "session_complete", ... });
    this.emitInformationalEvents(task, completion, worktreePath);  // checkpoints, compliance

    if (escalation) { /* escalation_wait path — unchanged */ return; }
    if (!result.success || !completion || completion.status !== "success") {
      return this.handleSessionFailure(task, result, completion);
    }

    await this.routeByResponseLevel(task, completion, result);
  } catch (err) { /* unchanged catch block */ }
}
```

**Acceptance criteria:**
- `processTask()` body under 80 lines
- Each extracted method testable independently
- Zero behavior change — all 41 existing orchestrator tests pass unchanged
- New methods are `private` (internal decomposition, not new public API)

**Test strategy:**
- No new tests — existing 41 orchestrator tests are the behavior contract
- Run full suite to verify zero regressions

**Estimated effort:** ~0 net new lines (reorganization), 0 new tests

---

#### Item 1.5b: State Schema Updates for Dialogue + Review

**What:** Extend `TaskRecord`, `KNOWN_KEYS`, `VALID_TRANSITIONS`, and `TaskFile` for Phase 2B/3 features.

**Files:**
- MODIFY `src/lib/state.ts` — add fields to `TaskRecord`, update `KNOWN_KEYS` (~15 lines)
- MODIFY `src/orchestrator.ts` — add `mode` to `TaskFile` interface (~2 lines)

**`TaskRecord` additions:**
```typescript
// Dialogue fields (Wave 4)
dialogueMessages?: Array<{ role: "operator" | "agent"; content: string; timestamp: string }>;
dialoguePendingConfirmation?: boolean;
// Review fields (Wave 5)
reviewResult?: { verdict: string; weightedRisk: number; findingCount: number };
```

**`KNOWN_KEYS` update (B7 — defensive deserialization):**
```typescript
const KNOWN_KEYS: ReadonlySet<string> = new Set([
  // ... existing 17 keys ...
  "dialogueMessages", "dialoguePendingConfirmation", "reviewResult",
]);
```

**`VALID_TRANSITIONS` clarification:**
The `reviewing` state already exists and allows transitions to `[active, merging, done, failed, escalation_wait]`. In Phase 3:
- `reviewing` is used when the review gate is running (between session completion and merge)
- `active → reviewing`: review gate spawned
- `reviewing → merging`: review approved
- `reviewing → failed`: review rejected
- `reviewing → escalation_wait`: review requests operator input
- No changes needed to `VALID_TRANSITIONS` — the existing transitions already support the review gate flow

**`TaskFile` extension:**
```typescript
export interface TaskFile {
  id?: string;
  prompt: string;
  priority?: number;
  mode?: "dialogue" | "reviewed";  // NEW: task execution mode
}
```

**Acceptance criteria:**
- `KNOWN_KEYS` includes all new fields (dialogue + review)
- `VALID_TRANSITIONS` unchanged (existing `reviewing` state already covers review gate)
- `TaskFile.mode` parsed from task JSON files
- Existing state files with missing new fields load correctly (all new fields optional)
- 280 existing tests pass

**Test strategy:**
- `tests/lib/state.test.ts`: 3 tests — new keys survive deserialization round-trip, unknown keys still dropped, `reviewing` transitions validated
- `tests/orchestrator.test.ts`: 1 test — `TaskFile.mode` parsed correctly

**Estimated effort:** ~17 lines source, 4 tests

---

### Wave 1.75: Pipeline Smoke Test (Architect synthesis — de-risk before Discord)

Depends: Wave 1.5 complete. Not a coding wave — a validation gate.

**What:** Run 3-5 real tasks through the pipeline end-to-end (file-based, no Discord). This is 1-2 hours of manual operation, not new code.

**Verify:**
1. Plugin loading works (OMC and caveman available in agent sessions)
2. `persistSession: true` — sessions can be resumed after escalation
3. Hook defense (`hooks: {}`) — `persistent-mode.cjs` does not fight `abortController`
4. `disallowedTools` — agents cannot create cron jobs or remote triggers
5. Response levels fire correctly on real completions
6. Escalation flow completes with file-based `!reply` equivalent
7. Tmux cleanup fires on worktree removal (if agents used `/team`)

**Outcome:** Go/no-go for Wave 2. If any of the above fail, fix before building Discord on top. Document real response level distribution and failure modes — this calibrates the thresholds in Open Design Questions #2.

**Acceptance criteria:**
- 3+ tasks complete the full lifecycle (pending → active → merging → done)
- 1+ task triggers escalation and is successfully resumed
- 0 orphaned worktrees or tmux sessions after cleanup
- Plugin loading path (A or B) confirmed working

---

### Wave 2: Discord Foundation (outbound notifications + webhook identity)

Depends: Wave 1.75 pass (pipeline validated end-to-end).

---

#### Item 5: Discord Notifier (Event to Discord)

**What:** Listen to orchestrator events and post formatted messages to Discord channels. Maps event types to channels: stage transitions and completions to `dev_channel`, failures to `ops_channel`, escalations to `escalation_channel`. Uses Discord webhook for outbound with per-agent identity.

**Files:**
- CREATE `src/discord/notifier.ts` (~120 lines)
- CREATE `src/discord/types.ts` (~30 lines) — shared Discord interfaces

**Interface:**
```typescript
// src/discord/types.ts
export interface DiscordSender {
  sendToChannel(channel: string, content: string, identity?: AgentIdentity): Promise<void>;
  addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
}

export interface AgentIdentity {
  username: string;
  avatarURL: string;
}

// src/discord/notifier.ts
export class DiscordNotifier {
  constructor(
    sender: DiscordSender,
    config: DiscordConfig,
  );

  /** Register as orchestrator event listener */
  handleEvent(event: OrchestratorEvent): void;
}
```

**Event-to-channel mapping:**
| Event Type | Channel | Format |
|-----------|---------|--------|
| `task_picked_up` | dev_channel | Task `{id}` picked up: "{prompt}" |
| `session_complete` | dev_channel | Session complete for `{id}` (success/failure) |
| `merge_result` | dev_channel | Merge result for `{id}`: merged/conflict/failed |
| `task_done` | dev_channel | Task `{id}` complete |
| `task_failed` | ops_channel | Task `{id}` FAILED: {reason} |
| `escalation_needed` | escalation_channel | ESCALATION `{id}`: {question} (with options if present) |
| `budget_exhausted` | ops_channel | Budget exhausted for `{id}`: ${cost} |
| `retry_scheduled` | dev_channel | Retry {attempt}/{max} for `{id}` |
| `response_level` | dev_channel (level 2+ only) | Response level {level} for `{id}`: {reasons} |

**Agent identity:** Events use the `orchestrator` agent identity from config. Review events (Phase 3) use `reviewer` identity. Fallback: hardcoded defaults if config entry missing.

**Acceptance criteria:**
- Every orchestrator event type has a handler (even if some are no-ops like `poll_tick`)
- Correct channel routing per event type
- Per-agent webhook identity applied
- Formatting is clean markdown (bold task IDs, code blocks for errors)
- Sender failures are swallowed (never crashes the pipeline)

**Test strategy:**
- `tests/discord/notifier.test.ts` (new): 12 tests — one per event type routing + identity + error swallowing

**Estimated effort:** ~150 lines source, 12 tests

---

#### Item 6: Webhook Per-Agent Identity

**What:** Implement the `DiscordSender` interface using Discord webhooks for outbound messages. Each message carries agent-specific username and avatar_url from the `[discord.agents.*]` config. Falls back to hardcoded defaults.

**Files:**
- CREATE `src/discord/sender.ts` (~60 lines)

**Interface:**
```typescript
export interface WebhookClient {
  send(options: { content: string; username?: string; avatarURL?: string }): Promise<unknown>;
}

export class WebhookSender implements DiscordSender {
  constructor(
    webhookClient: WebhookClient,
    agentConfig: Record<string, DiscordAgentIdentity>,
  );

  async sendToChannel(channel: string, content: string, identity?: AgentIdentity): Promise<void>;
  async addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
}
```

**Rate limiting (Critic finding — moved from Open Design Questions #4):**
Discord webhook rate limit is 30 messages per 60 seconds per channel. The sender includes a simple token-bucket rate limiter:
```typescript
// Built into WebhookSender — not a separate class
private queue: Array<{ resolve: () => void; payload: WebhookPayload }> = [];
private lastSendTime = 0;
private readonly minSpacingMs = 2000; // 2s minimum between sends
```
Messages exceeding the rate are queued and drained at `minSpacingMs` intervals. Queue overflow (>50 pending) drops oldest messages with a warning.

**Webhook URL clarification:** Single webhook URL in config (`discord.webhook_url`). The webhook posts to its configured channel. Per-channel routing is achieved by channel-specific webhooks configured at the Discord level, not in the harness. If future multi-channel webhooks are needed, `webhook_url` becomes `Record<string, string>` — but that's a future change, not Wave 2.

**Acceptance criteria:**
- Messages sent via webhook with correct username/avatar
- Agent identity resolved from config, falls back to defaults
- `addReaction` is a no-op for webhook sender (reactions require bot client — implemented in Wave 3)
- Webhook errors swallowed with console.error
- Rate limiter enforces 2s minimum spacing
- Queue overflow drops oldest with warning log

**Test strategy:**
- `tests/discord/sender.test.ts` (new): 7 tests — send with identity, fallback identity, error swallowed, reaction no-op, content formatting, rate limit queuing, queue overflow drop

**Estimated effort:** ~80 lines source, 7 tests

---

### Wave 3: Discord Inbound (task submission + message accumulator)

Depends: Wave 2 (outbound working, can notify results).

---

#### Item 7: Message Accumulator

**What:** 2-second debounce window for rapid natural language messages. `!` commands bypass the accumulator and process immediately. Accumulated text is concatenated and processed as a single coherent message. Prevents split-message misrouting when operators type in multiple Discord messages.

**Files:**
- CREATE `src/discord/accumulator.ts` (~50 lines)

**Interface:**
```typescript
export class MessageAccumulator {
  constructor(
    debounceMs?: number,  // default 2000
    onFlush: (userId: string, channelId: string, text: string) => void,
  );

  /** Add a message. Commands (starting with !) bypass and flush immediately. */
  push(userId: string, channelId: string, text: string): void;

  /** Force flush all pending (for shutdown). */
  flushAll(): void;
}
```

**Behavior:**
- Messages from the same user in the same channel within `debounceMs` are concatenated
- `!` prefix messages bypass — flush any pending for that user first, then process immediately
- Timer reset on each new message from same user/channel
- `flushAll()` called on shutdown to process any pending

**Acceptance criteria:**
- Multiple rapid messages concatenated into single text
- `!` commands bypass debounce
- `!` command flushes any pending NL messages before processing command
- Different users tracked independently
- Different channels tracked independently
- `flushAll()` processes all pending

**Test strategy:**
- `tests/discord/accumulator.test.ts` (new): 8 tests — basic debounce, command bypass, multi-user isolation, channel isolation, flush pending on command, flushAll, timer reset, empty flush

**Estimated effort:** ~50 lines source, 8 tests

---

#### Item 8: Operator Task Submission (Commands + NL)

**What:** Operator creates tasks via Discord. Two paths: structured `!task <prompt>` commands and natural language. NL path uses a single LLM classify call to disambiguate intent (new task vs feedback vs status query vs noise). Deterministic routing for `!` commands, LLM only for NL ambiguity.

**Files:**
- CREATE `src/discord/commands.ts` (~100 lines)

**Interface:**
```typescript
export type CommandIntent =
  | { type: "new_task"; prompt: string }
  | { type: "status_query"; taskId?: string }
  | { type: "feedback"; taskId: string; message: string }
  | { type: "escalation_response"; taskId: string; message: string }
  | { type: "unknown" };

export interface IntentClassifier {
  classify(text: string, context: ClassifyContext): Promise<CommandIntent>;
}

export class CommandRouter {
  constructor(
    stateManager: StateManager,
    orchestrator: Orchestrator,
    classifier: IntentClassifier,
    config: HarnessConfig,
  );

  /** Handle a structured command (! prefix already stripped). */
  handleCommand(command: string, args: string, channelId: string): Promise<string>;

  /** Handle natural language (post-accumulator). */
  handleNaturalLanguage(text: string, channelId: string, userId: string): Promise<string>;
}
```

**Supported commands:**
| Command | Action | Response |
|---------|--------|----------|
| `!task <prompt>` | Create task file in task_dir | "Task `{id}` created" |
| `!status [taskId]` | Query task state | Task summary or all-tasks overview |
| `!abort <taskId>` | Abort active session | "Task `{id}` aborted" |
| `!retry <taskId>` | Re-queue failed task | "Task `{id}` re-queued" |
| `!reply <taskId> <message>` | Respond to escalation | "Response sent to `{id}`" |

**NL classification (deterministic-first, LLM fallback):**
- **Step 1 — keyword match**: Check for deterministic patterns before LLM call:
  - `/^(create|add|build|implement|fix)\b/i` → `new_task`
  - `/^(status|progress|what'?s?\s+(happening|going))/i` → `status_query`
  - `/^(reply|respond)\s+(to\s+)?[a-zA-Z0-9_-]+/i` → `escalation_response`
  - Keyword match returns immediately — no LLM cost
- **Step 2 — LLM fallback**: Single haiku call via SDK `query()` with structured output
  - Context includes: list of active tasks, tasks in escalation_wait, channel name
  - Returns `CommandIntent`
  - LLM failure defaults to `{ type: "unknown" }` (safe — asks operator to rephrase)

**Acceptance criteria:**
- `!task` creates task file in task_dir with sanitized ID
- `!status` returns formatted task overview
- `!abort` calls `sessionManager.abortTask()`
- `!reply` writes escalation response (see Item 9)
- NL classification routes correctly for new task, status, feedback
- LLM classify failure returns "unknown" (never crashes)
- Commands from non-configured channels are ignored

**Test strategy:**
- `tests/discord/commands.test.ts` (new): 12 tests — each command type, NL classification for each intent, LLM failure fallback, invalid command, channel filtering

**Estimated effort:** ~100 lines source, 12 tests

---

#### Item 9: Reaction Acknowledgments

**What:** Cosmetic receipt confirmation on Discord messages. Eyes emoji on receive, checkmark on success, X on error. Never blocks processing. Requires discord.js Client (not webhook) for reaction API.

**Files:**
- CREATE `src/discord/client.ts` (~40 lines) — discord.js Client wrapper implementing `DiscordSender.addReaction()`
- MODIFY `src/discord/sender.ts` — compose webhook sender + client sender (~10 lines)

**Interface:**
```typescript
export interface BotClient {
  addReaction(channelId: string, messageId: string, emoji: string): Promise<void>;
  onMessage(handler: (msg: IncomingMessage) => void): void;
}

export interface IncomingMessage {
  content: string;
  channelId: string;
  messageId: string;
  userId: string;
  isBot: boolean;
}
```

**Acceptance criteria:**
- Eyes reaction added when message received
- Checkmark reaction added on successful processing
- X reaction added on error
- Reaction failures swallowed silently
- Bot messages ignored (no self-reaction loops)

**Test strategy:**
- `tests/discord/client.test.ts` (new): 5 tests — reaction on receive, success reaction, error reaction, failure swallowed, bot messages filtered

**Estimated effort:** ~50 lines source, 5 tests

---

### Wave 4: Escalation Response + Dialogue

Depends: Wave 3 (inbound message handling working).

---

#### Item 10: Escalation Response

**What:** Operator responds to escalation notification in the escalation channel. Response is written to task context. Session resumed via `resumeSession()` (verified working in Phase 1.5) with operator input appended to prompt. Fallback: if resume unavailable (session expired), spawn new session with original prompt + operator response + escalation context.

**Files:**
- CREATE `src/discord/escalation-handler.ts` (~80 lines)
- MODIFY `src/orchestrator.ts` — add `resolveEscalation(taskId, response)` method (~25 lines)

**Interface:**
```typescript
export class EscalationHandler {
  constructor(
    stateManager: StateManager,
    sessionManager: SessionManager,
    orchestrator: Orchestrator,
    sender: DiscordSender,
    config: DiscordConfig,
  );

  /** Handle operator response to an escalation. */
  async handleResponse(taskId: string, response: string): Promise<void>;
}
```

**Orchestrator addition:**
```typescript
// New method on Orchestrator
async resolveEscalation(taskId: string, operatorResponse: string): Promise<void> {
  const task = this.state.getTask(taskId);
  if (!task || task.state !== "escalation_wait") return;

  // Transition back to active
  this.state.transition(taskId, "active");

  // Resume or respawn session with operator input
  const resumePrompt = `Operator response to your escalation:\n\n${operatorResponse}\n\nContinue your work.`;
  // ... resume via SDK resumeSession() or spawn new session
}
```

**Resume strategy:**
1. Try `resumeSession()` with task's `sessionId` (requires `persistSession: true` — fixed in Wave 1 Item 1 pre-requisite)
2. If resume fails (session expired, no sessionId), spawn new session with: original task prompt + escalation context + operator response
3. Either path: task goes back through `processTask()` lifecycle

**Acceptance criteria:**
- `!reply <taskId> <message>` routes through escalation handler
- Task must be in `escalation_wait` state (reject otherwise)
- `resumeSession()` attempted first with full Options (settingSources, systemPrompt, plugins)
- Fallback to new session if resume fails
- Task re-enters `processTask()` lifecycle after resume
- Notification sent to escalation channel confirming resolution

**Test strategy:**
- `tests/discord/escalation-handler.test.ts` (new): 8 tests — successful resume, resume failure fallback, wrong state rejection, notification sent, prompt construction, sessionId preservation, re-entry to processTask, concurrent escalation guard

**Estimated effort:** ~105 lines source, 8 tests

---

#### Item 11: Escalation Dialogue (Multi-Turn)

**What:** Structured multi-turn conversation during escalation. Instead of single `!reply`, operator and agent exchange messages until resolution. Resolution detection via LLM classify call (haiku — "resolution" vs "continuation"). Defaults to "continuation" on LLM failure (safe — keeps dialogue open, never prematurely resumes).

**Files:**
- MODIFY `src/discord/escalation-handler.ts` — add dialogue mode (~50 lines)
- MODIFY `src/lib/state.ts` — add dialogue fields to TaskRecord (~8 lines)
- CREATE `src/discord/classify.ts` (~40 lines) — shared LLM classification functions

**Interface:**
```typescript
// New fields on TaskRecord
dialogueMessages?: Array<{ role: "operator" | "agent"; content: string; timestamp: string }>;
dialoguePendingConfirmation?: boolean;

// src/discord/classify.ts
export interface ClassifyFn {
  (prompt: string, systemPrompt: string): Promise<string>;
}

export async function classifyResolution(
  messages: Array<{ role: string; content: string }>,
  classifyFn: ClassifyFn,
): Promise<"resolution" | "continuation">;
```

**Resolution detection (deterministic-first, LLM fallback — same pattern as Item 8):**
- **Step 1 — keyword match**: Check for deterministic resolution signals:
  - `/^(yes|approve|approved|go ahead|lgtm|ship it|proceed|do it)\b/i` → `resolution`
  - `/^(no|reject|stop|hold|wait|not yet)\b/i` → `continuation`
  - Keyword match returns immediately — no LLM cost
- **Step 2 — LLM fallback**: Haiku classify call for ambiguous messages
  - LLM failure defaults to `"continuation"` (safe — keeps dialogue open)

**Flow:**
1. Escalation fires -> task enters `escalation_wait` -> notification in escalation channel
2. Operator sends NL in escalation channel -> accumulator flushes -> routes to escalation handler
3. Message appended to `dialogueMessages` on task record
4. Resolution detection: keyword match first, LLM fallback for ambiguous
5. If "continuation": forward message to agent context, wait for more
6. If "resolution": notify operator with confirmation prompt ("Looks like a resolution. Confirm with 'yes' or `!reply`?")
7. Operator confirms -> resume pipeline
8. `!reply` at any point -> immediate resume (bypass dialogue, backward compat)

**Acceptance criteria:**
- NL messages during `escalation_wait` enter dialogue mode
- Messages accumulate on task record
- LLM classifies each message as resolution/continuation
- LLM failure defaults to "continuation" (safe)
- Resolution requires explicit operator confirmation
- `!reply` always works as immediate override
- Dialogue messages preserved in task record for context

**Test strategy:**
- `tests/discord/escalation-handler.test.ts`: 8 additional tests — dialogue entry, message accumulation, resolution detection, confirmation flow, continuation flow, LLM failure safe default, `!reply` override, dialogue state cleanup

**Estimated effort:** ~98 lines source, 8 tests

---

### Wave 5: Review Gate (Phase 3)

Depends: Wave 2 (notifier for review events). Does NOT depend on Waves 3-4 (can be developed in parallel with Discord inbound).

---

#### Item 12: External Review Gate

**What:** Spawns a separate read-only sonnet session with a contrarian review prompt (ported from Python `agents/reviewer.md`). Produces a structured verdict. Gates merge for high-stakes tasks. Uses the existing `SDKClient` to spawn the review session.

**Files:**
- CREATE `src/gates/review.ts` (~120 lines)
- CREATE `config/harness/review-prompt.md` (~100 lines) — contrarian review prompt

**Interface:**
```typescript
export type ReviewVerdict = "approve" | "reject" | "request_changes";

export interface ReviewResult {
  verdict: ReviewVerdict;
  riskScore: {
    correctness: number;    // 0-1
    integration: number;     // 0-1
    stateCorruption: number; // 0-1
    performance: number;     // 0-1
    regression: number;      // 0-1
    weighted: number;        // weighted composite
  };
  findings: Array<{
    severity: "critical" | "warning" | "note";
    file: string;
    line?: number;
    description: string;
    suggestion?: string;
  }>;
  summary: string;
}

export class ReviewGate {
  constructor(
    sdk: SDKClient,
    config: ReviewGateConfig,
  );

  /** Run review on a worktree. Returns structured verdict. */
  async review(worktreePath: string, taskPrompt: string, completion: CompletionSignal): Promise<ReviewResult>;
}

export interface ReviewGateConfig {
  model?: string;           // default "sonnet" (cheaper than opus for reviews)
  maxBudgetUsd?: number;    // default 0.30
  rejectThreshold?: number; // weighted risk score threshold, default 0.25
  timeoutMs?: number;       // default 120_000
}
```

**Review session setup:**
- Model: sonnet (configurable)
- `permissionMode: "plan"` (read-only — reviewer cannot modify files)
- `disallowedTools: ["Write", "Edit", "Bash"]` (defense in depth)
- System prompt: contrarian review prompt (ported from `agents/reviewer.md`)
- User prompt: diff of worktree changes + completion signal + task prompt
- `persistSession: false` (ephemeral — review sessions are not resumed)

**Verdict parsing:**
- Reviewer writes `.harness/review.json` in the worktree (same signal-file pattern)
- Fallback: parse structured JSON from reviewer's last assistant message
- Malformed verdict defaults to `request_changes` (safe — doesn't auto-approve)

**Acceptance criteria:**
- Review session spawned with read-only permissions
- Contrarian prompt includes risk dimensions from `reviewer.md`
- Verdict parsed from `.harness/review.json` or assistant message
- Malformed verdict defaults to `request_changes`
- Review timeout aborts session and returns `request_changes`
- Budget cap enforced via `maxBudgetUsd`

**Test strategy:**
- `tests/gates/review.test.ts` (new): 10 tests — approve flow, reject flow, request_changes flow, timeout handling, malformed verdict, risk score calculation, read-only enforcement, budget cap, prompt construction, verdict parsing from assistant message

**Estimated effort:** ~120 lines source + ~100 lines prompt, 10 tests

---

#### Item 13: Review Trigger Logic + Hard Gating

**What:** Determines when to fire the review gate. Integrates review into `processTask()` between session completion and merge. Promotes Phase 2A's informational response levels to hard gates.

**Files:**
- MODIFY `src/orchestrator.ts` — add review gate routing in `processTask()` (~40 lines)
- MODIFY `src/orchestrator.ts` — add review-related events to `OrchestratorEvent` (~5 lines)
- MODIFY `src/lib/config.ts` — add review gate config section (~10 lines)

**Trigger conditions (any one fires review):**
1. `totalCostUsd > reviewCostUsd` (default 0.50)
2. `filesChanged.length > reviewFileCount` (default 10)
3. Confidence has degraded dimensions (`partial`/`unclear`/`alternatives_exist`/`guessing`)
4. Task file includes `mode: "reviewed"`
5. Response level >= 2 (from graduated response routing)

**Hard gating (Phase 3 promotion):**
- Level 0-1: direct to merge (unchanged)
- Level 2: review gate fires, verdict determines merge/fail
- Level 3-4: task pauses for operator input (escalation_wait or dialogue)
- Checkpoints with `assessment` containing degraded dimensions: pause for review

**New events:**
```typescript
| { type: "review_started"; taskId: string }
| { type: "review_complete"; taskId: string; result: ReviewResult }
```

**processTask flow change:**
```
session complete -> read completion -> check escalation -> evaluate response level
  -> level 0-1: merge
  -> level 2 OR trigger condition: review gate -> approve: merge / reject: fail
  -> level 3-4: escalation_wait (dialogue/operator)
```

**Acceptance criteria:**
- Review fires on any trigger condition
- Review verdict "approve" proceeds to merge
- Review verdict "reject" transitions to failed
- Review verdict "request_changes" transitions to failed with findings in lastError
- Level 2+ responses route through review before merge
- Level 3-4 responses pause for operator (escalation_wait)
- `mode: "reviewed"` in task file forces review
- Review events emitted for notifier

**Test strategy:**
- `tests/orchestrator.test.ts`: 10 additional tests — review triggered by cost, by file count, by confidence, by mode flag, by response level, approve-then-merge, reject-then-fail, request_changes-then-fail, review skip for level 0-1, review events emitted

**Estimated effort:** ~55 lines source, 10 tests

---

### Wave 6: Dialogue Agent (Phase 3)

Depends: Wave 4 (escalation handler for dialogue resolution), Wave 5 (review gate for post-dialogue review).

---

#### Item 14: Dialogue Agent

**What:** For build-from-scratch and ambiguous tasks. Agent writes `.harness/proposal.json` with a design proposal. Orchestrator pauses. Operator reviews via Discord. If approved, implementation proceeds (resume same session). If rejected, agent revises. Single-session-with-pause via `resumeSession()`.

**Files:**
- CREATE `src/session/dialogue.ts` (~90 lines)
- MODIFY `src/orchestrator.ts` — add dialogue routing (~25 lines)
- ADD to system prompt — proposal contract section (~30 lines)

**Interface:**
```typescript
export interface ProposalSignal {
  summary: string;
  approach: string;
  scope: string[];
  risks: string[];
  alternatives?: string[];
  estimatedFiles: string[];
}

export function readProposal(worktreePath: string): ProposalSignal | null;
export function validateProposal(raw: unknown): ProposalSignal | null;

export class DialogueSession {
  constructor(
    sdk: SDKClient,
    sessionManager: SessionManager,
    config: HarnessConfig,
  );

  /** Resume a paused dialogue session with operator feedback. */
  async resume(
    task: TaskRecord,
    operatorFeedback: string,
  ): Promise<{ result: SessionResult; completion: CompletionSignal | null }>;
}
```

**Flow:**
1. Task enters pipeline with `mode: "dialogue"` OR auto-triggered by response level 3-4
2. Agent session spawns with dialogue-mode system prompt addition
3. Agent writes `.harness/proposal.json` instead of doing implementation
4. Orchestrator detects proposal -> transitions to `paused` -> emits event -> notifies Discord
5. Operator reviews proposal in Discord dialogue channel
6. Operator approves ("approve", `!approve <taskId>`) or requests changes
7. If approved: `resumeSession()` with "Proposal approved. Proceed with implementation."
8. Agent implements, writes `completion.json` -> normal merge flow
9. If changes requested: `resumeSession()` with operator feedback -> agent revises proposal -> back to step 4

**Trigger timing (Critic Major #3 — two distinct paths):**

1. **Pre-session trigger (`mode: "dialogue"`):** Checked BEFORE the agent session spawns. The orchestrator reads `TaskFile.mode` during task pickup. If `mode === "dialogue"`, the system prompt is modified to include the proposal contract (write `.harness/proposal.json`, not implementation code). The agent never enters implementation mode — it produces a proposal from the start.

2. **Post-session trigger (response level 3-4):** Checked AFTER the agent session completes. If `routeByResponseLevel()` evaluates the completion signal and finds level 3-4 (unclear/guessing dimensions), the task transitions to `escalation_wait` with a dialogue flag. The NEXT session (after operator input) uses the proposal contract. This is the existing escalation flow — dialogue enhances it by adding structured back-and-forth before resuming.

The two triggers converge at the same proposal review flow (steps 4-9 below) but diverge on when the system prompt is modified.

**Auto-trigger conditions:
- Task file includes `mode: "dialogue"`
- Response level evaluates to 3 or 4 (unclear/guessing dimensions)
- This is checked BEFORE the agent session starts — the system prompt is modified to include the proposal contract when dialogue mode is active

**Acceptance criteria:**
- Proposal signal read and validated (same pattern as completion/escalation)
- Proposal detected -> task paused -> event emitted
- `resumeSession()` works with approval/feedback
- Fallback to new session if resume fails
- Auto-trigger on level 3-4 response evaluation
- `mode: "dialogue"` in task file forces dialogue mode
- Proposal notification includes summary, approach, risks

**Test strategy:**
- `tests/session/dialogue.test.ts` (new): 10 tests — proposal read/validate, approval resume, feedback resume, resume failure fallback, auto-trigger on unclear dimensions, mode flag trigger, proposal notification, invalid proposal handling, dialogue-to-implementation transition, dialogue-then-review chain

**Estimated effort:** ~115 lines source + ~30 lines prompt, 10 tests

---

#### Item 15: Dialogue Discord Channel

**What:** Dedicated Discord channel linked to dialogue sessions. Pre-pipeline design discussion surface. Operator and agent hash out architecture, scope, constraints before the task enters the pipeline. Essentially a `ralplan`/`deep-interview` surface — once consensus is reached, the refined task spec is submitted as a fully-scoped task.

**Files:**
- CREATE `src/discord/dialogue-channel.ts` (~80 lines)
- MODIFY `src/discord/commands.ts` — add `!dialogue` command (~10 lines)
- MODIFY `src/lib/config.ts` — add `dialogue_channel` to DiscordConfig (~3 lines)

**Interface:**
```typescript
export class DialogueChannelHandler {
  constructor(
    commandRouter: CommandRouter,
    sender: DiscordSender,
    classifier: IntentClassifier,
    config: DiscordConfig,
  );

  /** Handle message in dialogue channel. Routes to active dialogue session or starts new one. */
  async handleMessage(msg: IncomingMessage): Promise<void>;

  /** Submit refined task from dialogue to pipeline. */
  async submitTask(taskId: string, refinedPrompt: string): Promise<void>;
}
```

**Commands in dialogue channel:**
| Command | Action |
|---------|--------|
| `!dialogue <topic>` | Start new dialogue session |
| `!submit` | Submit current dialogue result as pipeline task |
| NL messages | Continue active dialogue |

**Acceptance criteria:**
- Dedicated channel configured in project.toml
- `!dialogue` starts a new pre-pipeline discussion
- NL messages in channel route to active dialogue
- `!submit` creates task from dialogue consensus
- Multiple concurrent dialogues tracked by thread/topic
- Dialogue channel messages do NOT enter the task pipeline directly

**Test strategy:**
- `tests/discord/dialogue-channel.test.ts` (new): 7 tests — start dialogue, continue dialogue, submit task, concurrent dialogues, channel isolation, NL routing, command handling

**Estimated effort:** ~93 lines source, 7 tests

---

## Summary

### File Inventory

**New source files (12):**
| File | Wave | Lines (est) |
|------|------|-------------|
| `src/discord/types.ts` | 2 | 30 |
| `src/discord/notifier.ts` | 2 | 120 |
| `src/discord/sender.ts` | 2 | 60 |
| `src/discord/accumulator.ts` | 3 | 50 |
| `src/discord/commands.ts` | 3 | 100 |
| `src/discord/client.ts` | 3 | 40 |
| `src/discord/escalation-handler.ts` | 4 | 130 |
| `src/discord/classify.ts` | 4 | 40 |
| `src/discord/dialogue-channel.ts` | 6 | 80 |
| `src/gates/review.ts` | 5 | 120 |
| `src/session/dialogue.ts` | 6 | 90 |
| `config/harness/review-prompt.md` | 5 | 100 |

**Modified source files (5):**
| File | Waves | Changes |
|------|-------|---------|
| `src/session/sdk.ts` | 1 | +settings, +hooks, +enabledPlugins (~20 lines) |
| `src/session/manager.ts` | 1 | +disallowedTools, +tmuxOps, +plugin config (~25 lines) |
| `src/lib/config.ts` | 1, 5, 6 | +plugins, +review config, +dialogue_channel (~25 lines) |
| `src/lib/state.ts` | 4 | +dialogue fields on TaskRecord (~10 lines) |
| `src/orchestrator.ts` | 4, 5, 6 | +resolveEscalation, +review routing, +dialogue routing (~90 lines) |

**New test files (12):**
| File | Wave | Tests (est) |
|------|------|-------------|
| `tests/discord/notifier.test.ts` | 2 | 12 |
| `tests/discord/sender.test.ts` | 2 | 7 |
| `tests/discord/accumulator.test.ts` | 3 | 8 |
| `tests/discord/commands.test.ts` | 3 | 12 |
| `tests/discord/client.test.ts` | 3 | 5 |
| `tests/discord/escalation-handler.test.ts` | 4 | 16 |
| `tests/discord/classify.test.ts` | 4 | 5 |
| `tests/discord/dialogue-channel.test.ts` | 6 | 7 |
| `tests/gates/review.test.ts` | 5 | 10 |
| `tests/session/dialogue.test.ts` | 6 | 10 |

**Modified test files (3):**
| File | Waves | New Tests |
|------|-------|-----------|
| `tests/session/sdk.test.ts` | 1 | 6 |
| `tests/session/manager.test.ts` | 1 | 6 |
| `tests/orchestrator.test.ts` | 5 | 10 |

### Totals

| Metric | Count |
|--------|-------|
| New source files | 12 |
| Modified source files | 5 |
| New test files | 10 |
| Modified test files | 4 (+`tests/lib/state.test.ts` for schema) |
| New source lines (est) | ~1,097 (+17 schema, +20 sender rate limiter) |
| Modified source lines (est) | ~170 (+ processTask reorganization, net ~0) |
| New test count (est) | ~142 (+4 schema, +2 sender rate limiter, +4 keyword classify) |
| New runtime dependency | 1 (`discord.js@^14.x`) |
| Prompt files | 1 new (`review-prompt.md`), 1 modified (`system-prompt.md`) |
| Spike scripts | 1 (`scripts/verify-plugin-loading.ts`, deleted after Wave 1) |

### Wave Dependencies

```
Wave 1 (Pre-Reqs) ──> Wave 1.5 (Decompose + Schema) ──> Wave 1.75 (Smoke Test) ──┬──> Wave 2 (Discord Outbound) ──> Wave 3 (Discord Inbound) ──> Wave 4 (Escalation)
                                                                                    │                                                                       │
                                                                                    └──> Wave 5 (Review Gate) ─────────────────────────────────────────────> Wave 6 (Dialogue)
```

- Wave 1 must complete first (affects all sessions)
- Wave 1.5 depends on Wave 1 (decompose before adding new routing)
- Wave 1.75 depends on Wave 1.5 (validate pipeline end-to-end before building Discord)
- Wave 2 depends on Wave 1.75 pass
- Wave 3 depends on Wave 2
- Wave 4 depends on Wave 3
- Wave 5 depends on Wave 2 only (can parallelize with Waves 3-4)
- Wave 6 depends on Waves 4 and 5

---

## Open Design Questions

1. **Escalation severity field.** If agent writes both `escalation.json` and `completion.json`, escalation currently wins unconditionally. Edge case: successful completion + non-blocking question. May need `severity: "blocking" | "advisory"` in escalation signal. **Resolution deferred:** Observe real agent behavior post-2B before adding severity. Current behavior (escalation always wins) is the safer default.

2. **Response level thresholds.** `reviewCostUsd: 0.50`, `reviewFileCount: 10`, `maxDirectCostUsd: 0.20` are initial values. Need calibration from real agent observation data. **Resolution:** Ship with defaults, tune after Post-2B Testing Option #3 (graduated response calibration).

3. **Mid-stream cost tracking.** SDK cost fields may only appear in the result message (post-session). Budget alarms can only fire post-session, not mid-stream. The `BudgetTracker` class exists but may not receive incremental updates. **Resolution:** Accept post-session-only cost for now. SDK may add streaming cost in future versions.

4. ~~**Discord rate limits.**~~ **RESOLVED in revision 1:** Rate limiter built into `WebhookSender` in Wave 2, Item 6. 2s minimum spacing, 50-message queue cap.

5. **Review gate cost.** Each review spawns a sonnet session. At $0.10-0.30 per review, this adds meaningful cost to high-stakes tasks. **Resolution:** Budget cap on review sessions (`maxBudgetUsd: 0.30`). Skip review for level 0-1 (cheap/simple tasks). Cost is justified for tasks that would otherwise merge bad code.

6. **Dialogue session persistence.** `resumeSession()` requires `persistSession: true` on the original session. If the session expires before operator responds (hours/days), resume fails and fallback to new session loses context. **Resolution:** Document the limitation. The fallback (new session with full context in prompt) is acceptable. Future: explore session snapshotting.

7. **Config migration for new optional fields.** Waves 2-6 add optional config fields (`discord.webhook_url`, `discord.dialogue_channel`, `pipeline.review_cost_usd`, `pipeline.review_file_count`, `pipeline.plugins`). All new fields have defaults — existing `project.toml` files work without changes. No migration script needed. Document new fields in `config/harness/project.toml.example` alongside each wave.

8. **discord.js version pinning.** Pin to `discord.js@^14.x` (latest LTS). v14 requires Node 18+, which is compatible with our Node 22 requirement. Do NOT use v15 (if released) without verifying API compatibility.
