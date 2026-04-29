# Wave E-δ — `nudge_check` Periodic Introspection + Per-Role Mention Routing (harness-ts Discord)

**Created:** 2026-04-27
**Status:** REVISED (2026-04-27) — architect REQUEST CHANGES verdict addressed; manual write per RALPLAN-postmortem fork B (skips consensus loop)
**Predecessor (LANDED):** Wave E-α (commits `66801b0` / `5bec3dc` / `72a3ea0`); Wave E-β (commits `6a14bfe` / `b2bcd76`); Wave E-γ (commits `0b562e3` / `86da9ab`); follow-up fixes (channel-collapse `2c1fd3f` / `d5fc652`, v2 prompts `61a233b`, SDK refactor `b3cf2c4`, transcript `7164517`, M7 `191cd55`).
**Wiki spec:** `.omc/wiki/phase-e-agent-perspective-discord-rendering-intended-features.md` § E.6 (nudge_check) + § E.8 (per-role mention routing).
**Wave context:** **Closes Phase E.** Final sub-features per the operator-confirmed scope. After this wave lands, Phase F (persistent message-context, breaker auto-recovery, etc.) opens.

---

## Architecture invariants (LOCKED — Principle 1)

These are not negotiable. Every decision below MUST satisfy them.

- **I-1 Discord opaque to agents.** Nudges are an orchestrator-only feature: agent sessions NEVER know `nudge_check` events fire. The `NudgeIntrospector` reads project state via existing `state.getTask` / `state.getAllTasks` / `projectStore.getProject` snapshots; it never spawns or messages an agent session. The Reviewer-mention v1 path is a permanent NO-OP per § E.8 (Reviewer is ephemeral — no session to relay to). Operator @-mentions resolve in the orchestrator dispatcher; agents see only the distilled `cleanedContent` per existing `extractMentions()`.
- **I-3 Additive optional fields only.** `nudge_check` is a NEW `OrchestratorEvent` variant. Additive — bumps the union from 28 entries (after `session_stalled` landed) to 29. No existing variants are renamed or removed. No `TaskRecord` / `CompletionSignal` changes. No `state.ts` API surface changes (the introspector composes existing `getAllTasks` + per-task `state` fields).
- **I-4 Verbatim allow-list.** Verbatim event-type name `nudge_check`. Verbatim `sourceAgent` values from `IdentityRole` (`architect` | `reviewer` | `executor` | `orchestrator`) per `src/discord/identity.ts:13`. Verbatim `status` values `stagnant` | `progressing` | `blocked`.
- **I-7 Substring pin titlecase preservation.** No changes to existing renderer pins — `nudge_check` epistle template is NEW (no prior pin). New pins introduced by this wave (e.g., `Status:`, `Last Activity:`, `Next Action:`) follow titlecase convention. Existing 30+ `notifier.test.ts` `.toContain(...)` assertions untouched.
- **I-10 Single owner per file layer.** `NudgeIntrospector` lives at `src/lib/nudge-introspector.ts` per § E.6 (matches existing `src/lib/` flat layout — no new `src/orchestrator/` subdir). Imported by `orchestrator.ts` only. No `src/discord/*` import. Mention routing changes stay in `src/discord/dispatcher.ts`.

---

## Goal

A single coherent narrative of "what is this bot doing right now?" — the orchestrator periodically introspects project state and emits structured `nudge_check` events. Those events render via the E-α deterministic epistle path AND can voice through the E-γ LLM transformer (per role's perspective) when `outbound_epistle_enabled` is on, without ever burdening agents with self-reporting. Operator can additionally @-mention specific roles to inject directives — Architect routes through existing CW-4.5; Reviewer is a permanent NO-OP (ephemeral session); Executor is a NO-OP-with-notice until Wave 6 dialogue split lands.

---

## Non-goals

- Persistent `NudgeIntrospector` state across orchestrator restart (Phase F)
- Bot-to-bot nudge dialogue (render-only fictions stay)
- Real `@reviewer` handler (permanent v1 NO-OP per § E.8 — Reviewer is ephemeral)
- Real `@executor` handler if Wave 6 dialogue-split not yet shipped (NO-OP with operator-visible notice)
- Operator nudge silencing per project (defer to Phase F if requested)
- Multi-language nudges
- `MessageContext` chain integration for `nudge_check` (chain-rule explicitly says standalone — see § B5 of E-β plan)
- Adding a `state.getEventLog` query surface for fine-grained progress detection (Phase F — see N3 below)

---

## Why this last in Phase E

1. **Builds on every prior wave.** E-α gave per-role identity; E-β gave threading semantics; E-γ gave LLM voice. Nudges leverage all three: each `nudge_check` resolves identity from `sourceAgent`, sends standalone (not chained), and can voice through the LLM transformer when whitelisted.
2. **Mention routing is small and self-contained.** ~+6 LOC dispatcher + ~+12 LOC tests for the per-role branch. Bundling with `nudge_check` lets one wave close E.6 + E.8 simultaneously.
3. **Closes Phase E.** Operator's reference-target gap analysis (see `reference-screenshot-analysis-conversational-discord-operator-st`) lists nudges as one of the four remaining missing features (per-role identity ✓ E-α, threading ✓ E-β, voice ✓ E-γ, **periodic nudges + per-role mention** ← this wave).

---

## Sub-feature 1 — `nudge_check` event + scheduled introspection (E.6)

### N1 — New `OrchestratorEvent` variant

**File:** `harness-ts/src/orchestrator.ts:107-137` (current 28-entry union, additive +1).

Verbatim per § E.6 spec:

```ts
| { type: "nudge_check"; projectId?: string; sourceAgent: "architect"|"reviewer"|"executor"|"orchestrator"; status: "stagnant"|"progressing"|"blocked"; observations: string[]; nextAction?: string }
```

Bumps union to 29 entries. All existing tests on the union (`events.some(e => e.type === ...)`) continue to pass. The variant is appended next to `poll_tick` / `shutdown` / `session_stalled` (an organizational convention for "system-category" events; not a formal section comment in source — these events all share the trait of being orchestrator-emitted and not tied to a single TaskRecord lifecycle).

`projectId` is **optional** because orchestrator-perspective nudges may be project-agnostic (e.g., "no active projects in 30min — quiet day"). Per-role nudges always set `projectId`.

### N2 — `NudgeIntrospector` class

**File:** `harness-ts/src/lib/nudge-introspector.ts` (NEW — matches existing `src/lib/` flat layout per § E.6 — NOT under `src/orchestrator/`).

```ts
export interface NudgeIntrospectorOpts {
  state: Pick<StateManager, "getTask" | "getAllTasks">;
  projectStore?: Pick<ProjectStore, "getAllProjects" | "getProject">;
  emit: (event: OrchestratorEvent) => void;
  intervalMs: number;     // default 600_000 (10 min)
  stagnantThresholdMs: number;   // default same as intervalMs
  recentActivityWindowMs: number; // default 5 * 60_000
  now?: () => number;     // injectable for tests
}

export class NudgeIntrospector {
  constructor(opts: NudgeIntrospectorOpts);
  /** Idempotent — second start() is a no-op. */
  start(): void;
  /** Drains pending timer + clears stall-suppression map. Idempotent. Called by Orchestrator.shutdown. */
  stop(): void;
  /** Test-callable — synchronous fire of one introspection pass. */
  tick(): void;
  /** Wired by orchestrator: stall events arriving here suppress next nudge for that project. */
  noteStall(event: Extract<OrchestratorEvent, { type: "session_stalled" }>): void;
}
```

**Read-snapshot atomicity** per § E.6: each `tick()` calls `state.getAllTasks()` (returns shallow-copy array of records per `src/lib/state.ts:228`) and `projectStore.getAllProjects()` (returns shallow-copy array per `src/lib/project.ts:208`) ONCE per pass. Iterates the snapshot only — never re-reads mid-iteration. This avoids races where a task transitions during iteration and is double-counted. AC: snapshot atomicity test mutates `state.getAllTasks` mid-iteration (mock) and confirms the iteration completes against the original snapshot.

**`sourceAgent` derivation** (no schema change required — option A from architect feedback). Project-state pre-filter first; if no active phase, fall through to project-state-only mapping; else map TaskState to owning role. `ProjectRecord.state` exists at `src/lib/project.ts:70` — values `decomposing` | `executing` | `completed` | `failed` | `aborted`.

```ts
function deriveSourceAgent(
  project: ProjectRecord,
  tasks: ReadonlyMap<string, TaskRecord>,
): SourceAgent {
  // Pre-filter: project-state-derived attribution when no executor/reviewer phase is live.
  if (project.state === "decomposing") return "architect";

  // Pick the most-recently-active phase (latest taskId attached).
  const activePhase = project.phases
    .filter((p) => p.state !== "done" && p.state !== "failed")
    .sort((a, b) => (b.taskId ?? "").localeCompare(a.taskId ?? ""))[0];
  if (!activePhase || !activePhase.taskId) return "orchestrator";

  const task = tasks.get(activePhase.taskId);
  if (!task) return "orchestrator";

  // Map TaskState (src/lib/state.ts:13-26) → owning role.
  switch (task.state) {
    case "active":
    case "pending":
    case "shelved":
    case "escalation_wait":
      return "executor";
    case "reviewing":
    case "review_arbitration":
      return "reviewer";
    case "merging":
    case "done":
    case "failed":
    case "paused":
      return "orchestrator"; // post-execution states
    default:
      return "orchestrator";
  }
}
```

(Note: `ProjectPhase` lacks a `startedAt` field per `src/lib/project.ts:26-33`; use lexicographic `taskId` ordering as a stable proxy for "most recently attached." When two phases sort identically, the first match wins — acceptable for v1 attribution heuristics.)

### N3 — Status derivation rules

Per `tick()` for each active project. Status is derived from `TaskRecord.state` + `TaskRecord.updatedAt` (both fields present per `src/lib/state.ts:64-95`) — NO event-log query is required. Precedence: `blocked` > `progressing` > `stagnant` (single matching rule wins; tie-breaker explicit below).

| Status | Rule | Precedence |
|--------|------|------------|
| `blocked` | At least one task in this project has `state ∈ {escalation_wait, review_arbitration, paused, shelved}` | Highest — checked first |
| `progressing` | At least one task in this project has `state ∈ {merging, done, reviewing}` AND `(now - updatedAt) < recentActivityWindowMs` (default 5 min) | Mid — checked when not blocked |
| `stagnant` | No task in this project has `(now - updatedAt) < stagnantThresholdMs` (default 10 min) | Default fallback when neither prior matches |

When NO active projects exist, emit a single `nudge_check { sourceAgent: "orchestrator", status: "stagnant", observations: ["No active projects."], nextAction: undefined }` event with `projectId` unset.

**Per-pass cap:** at most one `nudge_check` event emitted per project per pass. Prevents thrash if multiple status conditions match.

**AC** for status precedence: fixture with a project containing both a `stagnant` candidate task AND a `paused` task → expect `blocked`; fixture with both `progressing` and `paused` → expect `blocked`; fixture with `progressing` only → expect `progressing`.

> **Future extensibility (Phase F):** A `state.getEventLog(projectId, sinceMs)` API could enable finer-grained progress detection (e.g., distinguishing `merge_result` vs `task_done` events in the last N minutes, or counting cycle velocity). v1 deliberately uses the heuristic above to stay strictly additive at the code level — `state.ts:223-234` exposes only `getTask` / `getAllTasks` / `getTasksByState` and we do not extend that API in this wave.

### N3.5 — Interaction with `session_stalled` watchdog

The Wave watchdog (commit `2ae53c9`, see `src/orchestrator.ts:116`) emits `session_stalled` when an SDK stream goes inactive past a tier threshold. A stalled task is also a "blocked" state from the nudge perspective. Without coordination, both events fire for the same paralysis — operator gets two attention pings for one event.

**Rule:** if a task has emitted `session_stalled` within the last `nudge_interval_ms × 2` (default 20 min), the `NudgeIntrospector` SUPPRESSES `nudge_check` emission for that task's project. The stall event itself carries the operator-attention signal; a follow-up nudge would be redundant.

**Implementation:** `NudgeIntrospector` exposes `noteStall(event)` (see N2 interface). The orchestrator subscribes its existing event bus and forwards `session_stalled` events to `noteStall`, which records `Map<projectId, lastStalledAtMs>`. On `tick()`, for each project the introspector skips emission when `now - lastStalledAt < nudge_interval_ms × 2`. Resolution (stall → recovery → re-stall) is automatic: the suppression window naturally ages out; a fresh stall reseeds it via the next forwarded event.

**`stop()`** clears the suppression map (idempotent; double-stop is safe).

**AC:** emit `session_stalled` for a task; confirm next `tick()` suppresses `nudge_check` for that project. Advance time past `nudge_interval_ms × 2`; confirm next `tick()` resumes emission.

### N4 — Deterministic opener strings

Per § E.6 (verbatim):

- `stagnant`: `"No progress on this in {duration}."`
- `progressing`: `"Things are moving — last task {taskId} completed {duration} ago."`
- `blocked`: `"Stuck — {nextAction ?? 'awaiting input'}."`
- Closing default: `nextAction ?? "I'll check again at the next interval."`

`{duration}` formatted as a human-readable span (e.g., `"12 minutes"`, `"2 hours"`). Helper at `src/lib/duration-format.ts` if not already present (check; if absent, add tiny formatter — ~10 LOC).

**`observations[]` population (per status):**

- `stagnant` → `["no events in {duration}"]`
- `progressing` → `["last task {taskId} done {duration} ago", "{N} phases remaining"]`
- `blocked` → `["stuck in {state}"]` (one entry per blocking task, capped — see R7)

These deterministic openers form the body the renderer assembles. When `outbound_epistle_enabled` is ON AND the `(nudge_check, role)` tuple is whitelisted (see N5), the E-γ LLM transformer **replaces** the deterministic body with prose voice per the role's prompt; on any failure it falls back to the deterministic string verbatim. Same replacement-with-fallback semantic as E-γ § D6.

### N5 — `OUTBOUND_LLM_WHITELIST` extension

**File:** `harness-ts/src/discord/outbound-whitelist.ts` (created in E-γ; extend by 4 tuples).

Add four tuples (one per `sourceAgent` value):

- `nudge_check::architect`
- `nudge_check::reviewer`
- `nudge_check::executor`
- `nudge_check::orchestrator`

Bumps whitelist from **9 → 13 tuples**. Test in `tests/discord/outbound-whitelist.test.ts` updates the size assertion from 9 to 13 and asserts the four new members are present.

### N6 — Notifier wiring

**File:** `harness-ts/src/discord/notifier.ts` (modified, +~25 LOC).

`NOTIFIER_MAP` gains a `nudge_check` row routing to `dev_channel` (channel-collapse policy — operator opted into nudges, dev_channel is the natural place for self-narration). Operator-mention: NO ping by default (nudges are informational, not action-required).

**Identity resolution exception.** `resolveIdentity(event)` (`src/discord/identity.ts:23-65`) currently is an exhaustive switch on `event.type`. For `nudge_check`, identity must read `event.sourceAgent` (NOT the event type) so each of the four sourceAgent variants renders with its own avatar/username from `DISCORD_AGENT_DEFAULTS` (`src/lib/config.ts:287`). Add a dedicated `case "nudge_check": return event.sourceAgent;` arm to the exhaustive switch. (Commit-1 mechanical scope adds a placeholder; commit-2 wires the live `sourceAgent` value — see "Two-commit split" below.)

**Epistle template.** Add to `src/discord/epistle-templates.ts` (E-α D3 format, +~30 LOC):

```
{emoji} **{Bold Label}** — `YYYY-MM-DDTHH:MM:SSZ`

{N4 deterministic opener for status}

- **Status:** {stagnant|progressing|blocked}
- **Source:** {architect|reviewer|executor|orchestrator}
{- **Project:** {projectId} (if defined)}
{- **Observations:** • {obs1} • {obs2} ... (joined with " • ")}

{N4 closing string}
```

Emoji per § E.6 reference target: `🦀` for nudge_check (matches operator screenshot "🦀 Nudge Check — 20:40 UTC" example).

**Chain rule** per E-β § B5: `nudge_check` is **standalone** (no `replyToMessageId`). Confirms here for cross-reference.

### N7 — Config flag

**File:** `harness-ts/src/lib/config.ts` (modified, +2 fields under `DiscordConfig`). Snake_case to match the convention used elsewhere in `DiscordConfig` (`outbound_epistle_enabled`, `llm_daily_cap_usd`, `operator_user_id`).

```ts
export interface DiscordConfig {
  // ... existing fields ...
  /** E-δ N7 — when true, NudgeIntrospector is constructed and started at bootstrap.
   *  Default false. Operator opts in. */
  nudge_enabled?: boolean;
  /** E-δ N7 — periodic timer interval. Default 600_000 (10 min). Min 60_000 (1 min) enforced at construction. */
  nudge_interval_ms?: number;
}
```

`nudge_enabled` defaults to `false`. Verified via `tests/lib/config.test.ts` default-config assertion.

### N8 — Bootstrap wiring + shutdown ordering

**File:** `harness-ts/scripts/live-bot-listen.ts` (modified, +~25 LOC).

When `config.discord.nudge_enabled === true`:
1. Construct `new NudgeIntrospector({ state, projectStore, emit: orchestrator.emit.bind(orchestrator), intervalMs, ...thresholds })`.
2. Pass to `Orchestrator` constructor as new optional `nudgeIntrospector?` dep.
3. Subscribe the introspector to `session_stalled` events: `orchestrator.on(e => { if (e.type === "session_stalled") nudgeIntrospector.noteStall(e); })` — required for N3.5 suppression.
4. `Orchestrator.start()` calls `nudgeIntrospector.start()` after existing `startStallWatchdog()`.
5. **Shutdown ordering:** `Orchestrator.shutdown()` MUST stop the nudge timer BEFORE `sessions.abortAll()` and BEFORE `emit({ type: "shutdown" })`. Co-locate with the existing watchdog teardown at `src/orchestrator.ts:208-212`. This prevents a final tick firing `nudge_check` for a session being torn down.

When flag is `false` (default), introspector is not constructed — zero timers, zero overhead. Verified via test `nudge introspector is null when flag is false`.

**Smoke fixture.** `harness-ts/scripts/live-discord-smoke.ts` adds 4 fixtures (one per `sourceAgent` × one status variant each) so an operator running the smoke can visually validate per-role nudge identity and emoji routing in dev_channel. The fixtures honor the existing `--llm` flag (per E-γ § D7 convention) — when `--llm` is passed AND `outbound_epistle_enabled === true`, the LLM transformer voices the four nudges through the role-specific prompts.

---

## Sub-feature 2 — Per-role @-mention routing (E.8)

### MR1 — Mention extraction (already done; route per resolved role)

**File:** `harness-ts/src/discord/dispatcher.ts:374-445` (`tryMentionRoute`).

CW-4.5 already extracts `@architect-foo`-style mentions and calls `architectManager.relayOperatorInput`. The dispatcher dispatches the **first mention only** (existing behavior at `src/discord/dispatcher.ts:396` — preserved by this wave's design; an explicit "multiple_mentions" notice already exists in the static template, see `src/discord/response-generator.ts:69-77`). If the operator types `@reviewer @architect "do X"`, the dispatcher routes to the `@reviewer` branch (NO-OP) and silently skips `@architect`. Acceptable for v1.

Extend the routing branch to switch on the resolved role:

```ts
const targetRole = identityMap.lookupRole(first.agentKey); // see MR3
switch (targetRole) {
  case "architect":
    await this.architectManager.relayOperatorInput(resolved.projectId, extracted.cleanedContent);
    break;
  case "reviewer":
    // MR2 — permanent v1 NO-OP. Send operator-visible notice via NEW no_active_role kind (see below).
    await this.sendNoActiveAgentNotice(msg, resolved.projectId, "reviewer");
    break;
  case "executor":
    // MR4 — Wave 6 dialogue-split scope. Until then, NO-OP-with-notice (same shape as reviewer).
    await this.sendNoActiveAgentNotice(msg, resolved.projectId, "executor");
    break;
  case "orchestrator":
    // Bare `@orchestrator` — there is no agent for the orchestrator. Fall through to NL parser.
    return false;
}
return true;
```

**New `no_active_role` ResponseKind.** The existing `no_session` kind (`src/discord/response-generator.ts:31, 87-94`) is **Architect-specific** copy ("The Architect for `{pid}` isn't running anymore — there's no live Architect session..."). Reusing it for reviewer/executor would mis-name the role. Add a NEW ResponseKind, additive per I-3:

- **File:** `src/discord/response-generator.ts` (modified, +~12 LOC).
- **Kind:** `"no_active_role"` appended to the `ResponseKind` union (line 28-37).
- **Static template:** `"No active {agentName} session for project \`{projectId}\` — operator input dropped."`
- **Used by:** `sendNoActiveAgentNotice(msg, projectId, role)` for `reviewer` and (future) `executor` branches.
- **Existing `no_session` stays put** for the Architect-relay-failure code path (semantic difference: architect HAS a live session that threw "no Architect session for X"; reviewer has no session in the first place).

`sendNoActiveAgentNotice(msg, projectId, role)` is a tiny dispatcher helper that calls `responseGenerator.generate({ kind: "no_active_role", operatorMessage: msg.content, fields: { projectId, agentName: role } })` and posts the result to the source channel.

### MR2 — Reviewer NO-OP rationale (LOCKED)

**Source:** `harness-ts/src/gates/review.ts:175-260`.

Confirmed at line 249: `persistSession: false`. Reviewer is ephemeral — there is NO long-running Reviewer session anywhere to receive `relayOperatorInput`. Per § E.8 (verbatim):

> `@reviewer` → **PERMANENT v1 NO-OP**: `review.ts:175-247` spawns ephemeral reviewer with `persistSession: false`. There is NO long-running reviewer session to receive `relayOperatorInput`. Operator-visible "no active reviewer for {projectId}" message via NEW `no_active_role` ResponseKind. NO code change to `review.ts`. Routing logic added to mention dispatcher only (~+6 LOC dispatcher.ts + +12 dispatcher.test.ts).

This wave does **NOT** touch `review.ts`. The NO-OP is achieved purely through the dispatcher branch in MR1.

If a future Phase F wave introduces a long-running Reviewer "rapporteur" session (operator's stated possibility), the routing branch flips from NO-OP to a real `reviewerManager.relayOperatorInput` call. v1 commits the NO-OP path explicitly.

### MR3 — Identity-map role lookup extension

**File:** `harness-ts/src/discord/identity-map.ts:33-55` (modified, +~10 LOC).

Current `IdentityMap.lookup(username): AgentKey | null` returns the agent key (Discord username slug). Add a parallel method:

```ts
export interface IdentityMap {
  lookup(username: string): AgentKey | null;
  /** E-δ MR3 — return the role for a previously-resolved agent key.
   *  Strict allowlist; unknown keys return null. */
  lookupRole(agentKey: AgentKey): "architect" | "reviewer" | "executor" | "orchestrator" | null;
}
```

**Implementation note.** `DISCORD_AGENT_DEFAULTS[key]` (`src/lib/config.ts:287-293`) holds `{ name, avatar_url }` only — there is **no `role` field** to read. The agent KEY itself ("architect" / "reviewer" / "executor" / "orchestrator") IS the role identifier. `lookupRole` is therefore a small case-insensitive switch on the lowercased input against the four `IdentityRole` literals; no `DISCORD_AGENT_DEFAULTS` lookup is needed. Unknown / mistyped names (e.g., `"reviewr"`) return `null`, which the dispatcher treats as "fall through to NL parser" (no false positive routing).

### MR4 — Agents NEVER see @-mentions

**Invariant preserved.** `extractMentions` already strips resolved mentions from `cleanedContent` (dispatcher.ts:142-153). Architect's `relayOperatorInput` receives only the cleaned plain text. Reviewer / Executor branches don't relay at all (NO-OP). The orchestrator-side dispatcher is the only code path that sees `@architect-foo` substrings.

I-1 enforced. New test in `tests/discord/dispatcher-routing.test.ts` asserts the `relayOperatorInput` mock for the `@architect` branch receives `cleanedContent` with the mention stripped, and the `@reviewer` / `@executor` branches do NOT call `relayOperatorInput` at all.

**AC for I-1 reverse direction:** `NudgeIntrospector` NEVER calls `architectManager.relayOperatorInput` and NEVER spawns an SDK session — mock both, assert zero calls.

---

## File list

| File | Change | LOC |
|------|--------|----:|
| `src/orchestrator.ts` | Add `nudge_check` event variant; integrate optional `NudgeIntrospector` dep + start/stop + stall forwarding | ~55 |
| `src/lib/nudge-introspector.ts` | NEW — periodic timer + status derivation + tick() + noteStall() + suppression map | ~220 |
| `src/lib/duration-format.ts` (if absent) | NEW — tiny `formatDurationSince(ms)` helper | ~15 |
| `src/lib/config.ts` | `discord.nudge_enabled` + `discord.nudge_interval_ms` config fields (snake_case) | ~20 |
| `src/discord/notifier.ts` | Route `nudge_check` to dev_channel; `NOTIFIER_MAP` row | ~25 |
| `src/discord/identity.ts` | `nudge_check` exhaustive arm — commit-1 placeholder, commit-2 reads `event.sourceAgent` | ~10 |
| `src/discord/epistle-templates.ts` | `nudge_check` template (E-α D3 multi-paragraph format) | ~30 |
| `src/discord/outbound-whitelist.ts` | Extend whitelist with 4 `nudge_check::*` tuples | ~6 |
| `src/discord/dispatcher.ts` | Per-role mention routing switch + `sendNoActiveAgentNotice` helper | ~35 |
| `src/discord/identity-map.ts` | `lookupRole(agentKey)` method | ~10 |
| `src/discord/response-generator.ts` | NEW `no_active_role` ResponseKind + static template | ~15 |
| `scripts/live-bot-listen.ts` | Wire `NudgeIntrospector` when flag true; subscribe to `session_stalled` | ~30 |
| `scripts/live-discord-smoke.ts` | Add `nudge_check` fixtures × 4 sourceAgent values; honor `--llm` flag | ~30 |
| `tests/lib/nudge-introspector.test.ts` | NEW — status derivation matrix + suppression + atomicity + idle-when-flag-false | ~180 |
| `tests/discord/dispatcher-routing.test.ts` | NEW — per-role mention routing + reviewer/executor NO-OP + first-mention-only | ~110 |
| `tests/discord/notifier.test.ts` | Add `nudge_check` rendering assertions (4 sourceAgent × 3 status) | ~50 |
| `tests/discord/outbound-whitelist.test.ts` | Update whitelist size 9 → 13 + new-tuple membership | ~5 |
| `tests/discord/response-generator.test.ts` | Add `no_active_role` template assertion | ~10 |
| `tests/lib/config.test.ts` | `nudge_enabled` default-false assertion | ~5 |

**Estimated total:** ~770 LOC src + ~360 LOC tests across 19 files.

---

## Acceptance criteria

- **AC1** — `npm run lint` clean. `npm test` clean. All existing tests still pass (no regressions).
- **AC2** — `OUTBOUND_LLM_WHITELIST.size === 13` (was 9 in E-γ). Verified by `tests/discord/outbound-whitelist.test.ts` size + membership assertion (4 new `nudge_check::*` tuples present).
- **AC3** — `discord.nudge_enabled` defaults to `false`. Verified by `tests/lib/config.test.ts` default-config assertion.
- **AC4** — When `nudge_enabled === false`, `NudgeIntrospector` is NOT constructed at bootstrap (verified by `live-bot-listen.ts` integration test or unit assertion that `orchestrator.nudgeIntrospector === undefined`). Zero timers scheduled.
- **AC5** — All four `sourceAgent` variants (`architect` / `reviewer` / `executor` / `orchestrator`) emit correctly via `NudgeIntrospector.tick()`. Identity resolution renders the matching role's avatar/username. Verified by `tests/discord/notifier.test.ts` 4-fixture assertion.
- **AC6** — All three `status` variants (`stagnant` / `progressing` / `blocked`) produce the correct N4 deterministic opener string in the rendered body. Verified by `tests/lib/nudge-introspector.test.ts` matrix test.
- **AC7** — `@reviewer` mention emits the `no_active_role` notice (`"No active reviewer session for project \`{projectId}\` — operator input dropped."`) AND `architectManager.relayOperatorInput` is NEVER called. Verified by `tests/discord/dispatcher-routing.test.ts` mock assertion (`expect(mockRelay).not.toHaveBeenCalled()`).
- **AC8** — `@architect` mention routes to `architectManager.relayOperatorInput(resolved.projectId, cleanedContent)` with the mention substring stripped. Verified by `tests/discord/dispatcher-routing.test.ts`.
- **AC9** — I-1 preserved: `tests/lib/no-discord-leak.test.ts` still passes. `nudge-introspector.ts` does NOT import from `src/discord/*` (uses `emit` callback only).
- **AC10** — I-7 preserved: `npm run audit:epistle-pins` clean. New `nudge_check` template pins (`Status:`, `Source:`, `Observations:`) follow titlecase convention.
- **AC11** — Orchestrator `shutdown()` calls `nudgeIntrospector.stop()` exactly once **before** `sessions.abortAll()` and **before** `emit({ type: "shutdown" })`. Subsequent timer fires are suppressed. Verified by `tests/lib/nudge-introspector.test.ts` shutdown-ordering assertion (mock recording the call sequence).
- **AC12** — Status precedence: fixture with both `stagnant`-shaped and `paused`-shaped tasks emits `blocked`; fixture with `progressing` + `paused` emits `blocked`; fixture with `progressing` only emits `progressing`. Verified by `tests/lib/nudge-introspector.test.ts` precedence matrix.
- **AC13** — Snapshot atomicity: `state.getAllTasks` mocked to mutate mid-iteration; introspector's iteration completes against the original snapshot and emits the expected single `nudge_check`. Verified by `tests/lib/nudge-introspector.test.ts`.
- **AC14** — Stall suppression: emit `session_stalled { taskId: T1, ... }` (forwarded via `noteStall`); next `nudge_check` tick suppresses emission for T1's project; advance time past `nudge_interval_ms × 2`; tick after that emits normally. Verified by `tests/lib/nudge-introspector.test.ts`.
- **AC15** — I-1 reverse direction: `NudgeIntrospector` NEVER calls `architectManager.relayOperatorInput` AND NEVER spawns an SDK session (both mocked; `expect(...).not.toHaveBeenCalled()` for both).
- **AC16** — Multiple-mention precedence: when operator types `@reviewer @architect "..."`, the dispatcher routes to `@reviewer` (NO-OP notice) and never invokes `relayOperatorInput`. Preserves CW-4.5 first-mention behavior at `src/discord/dispatcher.ts:396`.

(16 ACs vs. typical 10 — added per architect feedback to pin status precedence, snapshot atomicity, stall suppression, I-1 reverse, and first-mention behavior explicitly.)

---

## Two-commit split (per I-6)

**Commit 1 — Mechanical scaffolding (zero behavior change in production):**

- N1 `nudge_check` event variant added to `OrchestratorEvent` union.
- N2 `NudgeIntrospector` class file (NEW; not yet integrated).
- N4 `formatDurationSince` helper (if not already present).
- N5 `OUTBOUND_LLM_WHITELIST` extension (4 new tuples).
- N7 config fields (default `false`).
- MR3 `IdentityMap.lookupRole` method.
- New `no_active_role` ResponseKind + template (additive per I-3).
- **`src/discord/identity.ts` — placeholder arm for the new `nudge_check` variant.** TypeScript exhaustive switch will fail to compile without an arm; commit-1 adds:

  ```ts
  case "nudge_check":
    return "orchestrator"; // placeholder per E-δ commit-1 mechanical scope; commit-2 wires sourceAgent-derived identity
  ```
  Mechanical addition; preserves zero-behavior-change because no `nudge_check` events are emitted yet.
- Tests for the introspector class (mocked `state` + `emit`), whitelist size update, `lookupRole` unit tests, `no_active_role` static template test.
- `it.todo("orchestrator wires nudgeIntrospector — commit 2")` placeholders for integration tests.

Production notifier untouched. Pin assertions unaffected. Operator can revert this commit cleanly.

**Commit 2 — MANDATORY sub-split (each is a clean revertable unit):**

**Commit 2a — Notifier integration:**

- Notifier `NOTIFIER_MAP` row for `nudge_check`.
- `src/discord/identity.ts` — replace commit-1 placeholder with `return event.sourceAgent` so identity is derived from the event's role attribution.
- Epistle template for `nudge_check`.
- `OutboundResponseGenerator` whitelist hook fires for `nudge_check::*` tuples (relies on N5 from commit 1).
- AC5 / AC10 tests (4-sourceAgent fixture + epistle-pins audit).

**Commit 2b — Dispatcher routing + bootstrap + smoke:**

- Dispatcher per-role mention routing switch + `sendNoActiveAgentNotice` helper + reviewer NO-OP + executor NO-OP.
- Bootstrap wiring in `live-bot-listen.ts` (introspector construction + start + `session_stalled` subscription).
- `Orchestrator.shutdown()` ordering: stop nudge timer BEFORE `abortAll` and BEFORE `emit({ type: "shutdown" })`.
- Smoke fixtures × 4 sourceAgent values; honors existing `--llm` flag.
- Un-skip `it.todo` placeholders + dispatcher-routing test.
- AC7 / AC8 / AC11 / AC14 / AC15 / AC16 tests.

Production behavior changes ONLY when operator sets `nudge_enabled: true` in their config (introspector loop) or types an `@reviewer` / `@executor` mention (NO-OP-with-notice path). Default behavior preserves Wave E-γ byte-equal.

Single `git revert <commit2b>` returns to commit-2a (notifier-only); `git revert <commit2a>` returns to commit-1 mechanical baseline cleanly.

---

## Cost analysis

**Per-call cost assumption (explicit):** ~$0.0005-0.001 per nudge LLM call on Haiku 4.5. The nudge body is small (deterministic opener + 1-3 observations + project metadata) — the per-call payload is well below the E-γ `OutboundResponseGenerator.maxBudgetUsd` cap of $0.02. Range reflects model variance; assume the upper bound for budget planning.

**Daily volume per active project:**

- Default `nudge_interval_ms = 600_000` (10 min) → 6 ticks/hour.
- 4 nudge tuples (architect / reviewer / executor / orchestrator) potentially eligible per project, but **at most one nudge per project per pass** per N3 cap → 6 calls/hour per project.
- 16 active hrs/day → ~96 nudge LLM calls/day per project.
- Per-project nudge spend: ~96 × $0.001 ≈ **$0.10/day typical**, **$0.20/day worst case**.

**Comparison to E-γ baseline:** E-γ's existing 9-tuple whitelist fires only on actual lifecycle events (not on a fixed schedule), so its daily cost depends on event volume — typical projects emit ~30 narrative-relevant events/day per E-γ § Cost analysis, totaling ~$0.03-0.06/day per project at Haiku rates. Nudges add a fixed-frequency floor (~$0.10/day) on top.

**Per-project total daily LLM spend (when both flags ON):** dominated by nudges if frequency is high; capped by the existing $5/day `LlmBudgetTracker` (E-γ § D3). If the budget hits cap, nudges silently degrade to the deterministic body (graceful fallback, same semantic as E-γ).

**Default deployment cost (both flags OFF):** $0 — introspector is not constructed; LLM voice is gated.

**Flag-flip cost (`nudge_enabled=true`, `outbound_epistle_enabled=false`):** $0 LLM; only Discord webhook send overhead.

**Both flags ON cost:** ~$0.10-0.20/project/day typical, hard-capped at $5/project/day by `LlmBudgetTracker`.

---

## Risk register

- **R1: Nudges fire too often → operator fatigue.** Mitigated by 10-min default interval + opt-in flag (default `false`). Operator can tune `nudge_interval_ms` upward without code change.
- **R2: Status derivation too sensitive (false-positive `stagnant`).** Mitigated by configurable thresholds (`stagnantThresholdMs`, `recentActivityWindowMs`). Smoke fixture covers each status branch for visual validation. If operator finds a status mis-classified in the wild, thresholds tune in config — no plan iteration needed.
- **R3: Mention routing accidentally pings agent (I-1 leak).** Mitigated by AC9 + AC15 + the existing `extractMentions` strip-only-resolved semantics. Dispatcher routing test (`tests/discord/dispatcher-routing.test.ts`) explicitly asserts agent never sees raw `@-mention`; only `cleanedContent` flows through `relayOperatorInput`.
- **R4: Reviewer NO-OP confuses operators ("why isn't my @reviewer doing anything?").** Mitigated by explicit `no_active_role` notice ("No active reviewer session for project `{projectId}` — operator input dropped."). Notice fires via `responseGenerator.generate({ kind: "no_active_role", ... })` — semantically distinct from the Architect-relay-failure `no_session` kind.
- **R5: `NudgeIntrospector` timer leak on shutdown.** Mitigated by AC11 + explicit `stop()` call in `Orchestrator.shutdown()`. Idempotent `stop()` so double-shutdown is safe. Test asserts no timer fires after stop and that `stop()` runs before `abortAll`.
- **R6: `sourceAgent` derivation mis-attributes nudge identity.** Mitigated by N2 `deriveSourceAgent` decision tree (project-state pre-filter → most-recent-active-phase → TaskState mapping → orchestrator fallback). When project has no active phase OR the task is in a post-execution state, fallback is `orchestrator` — never undefined. Verified by AC5 fixture covering all four sourceAgent variants explicitly.
- **R7: `nudge_check` LLM voice prompt uses unbounded `observations[]`.** Mitigated by N3 per-pass cap (one nudge per project per pass) AND truncation: `observations.slice(0, 5).join(" • ")` capped at 5 entries before rendering.
- **R8: Stall + nudge double-emit.** Mitigated by N3.5 suppression (`nudge_interval_ms × 2` quiet window after `session_stalled`). Verified by AC14.

---

## Out of scope (explicit)

- Persistent `NudgeIntrospector` state across restart (Phase F)
- Multi-language nudges (English only)
- Operator-configurable nudge silencing per project (defer; could be added as `discord.nudge_silenced_projects?: string[]` in Phase F)
- Real `@reviewer` handler — permanent v1 NO-OP per § E.8
- Real `@executor` handler if Wave 6 dialogue-split not yet shipped — NO-OP-with-notice
- `nudge_check` chain integration with E-β `MessageContext` — explicitly standalone per E-β § B5
- `state.getEventLog` API surface for fine-grained progress detection — Phase F (see N3 footnote)

---

## Cross-references

- `.omc/wiki/phase-e-agent-perspective-discord-rendering-intended-features.md` § E.6 + § E.8 — source spec
- `.omc/wiki/harness-ts-architecture-snapshot-2026-04-27-as-built.md` — current Discord layer state
- `.omc/wiki/harness-ts-core-invariants.md` — I-1, I-3, I-4, I-7, I-10
- `.omc/wiki/harness-ts-types-reference-source-of-truth.md` — verbatim event-type names + IdentityRole exhaustive list
- `.omc/plans/2026-04-26-discord-wave-e-alpha.md` — E-α (LANDED) — identity diversification + epistle templates
- `.omc/plans/2026-04-27-discord-wave-e-beta.md` — E-β (LANDED) — message_reference threading; chain-rule for `nudge_check` documented as standalone
- `.omc/plans/2026-04-27-discord-wave-e-gamma.md` — E-γ (LANDED) — LLM voice; whitelist source extended here from 9 → 13
- `.omc/wiki/phase-f-discord-richness-resilience-backlog.md` — Phase F successor concerns (persistent message-context, breaker auto-recovery, etc.)
- `harness-ts/src/orchestrator.ts:107-137` — current 28-entry OrchestratorEvent union
- `harness-ts/src/orchestrator.ts:202-216` — shutdown ordering target (nudge stop co-locates with watchdog stop at :208-212)
- `harness-ts/src/orchestrator.ts:116` — `session_stalled` event variant (input to N3.5 suppression)
- `harness-ts/src/lib/state.ts:13-26` — `TASK_STATES` literal list (used for status derivation in N3)
- `harness-ts/src/lib/state.ts:223-234` — `getTask` / `getAllTasks` / `getTasksByState` only (no `getEventLog` — see N3 footnote)
- `harness-ts/src/lib/state.ts:64-95` — `TaskRecord.state` + `TaskRecord.updatedAt` source-of-truth
- `harness-ts/src/lib/project.ts:23-82` — `ProjectRecord.state` + `ProjectPhase` shapes (no `currentPhase`, no `role` field; pre-filter target in N2)
- `harness-ts/src/lib/project.ts:208` — `getAllProjects` snapshot source
- `harness-ts/src/lib/config.ts:106` — `outbound_epistle_enabled?: boolean` (snake_case convention reference)
- `harness-ts/src/lib/config.ts:287-293` — `DISCORD_AGENT_DEFAULTS` (no `role` field; key IS the role per MR3)
- `harness-ts/src/discord/identity.ts:23-65` — exhaustive `resolveIdentity` switch (commit-1 placeholder + commit-2a wiring target)
- `harness-ts/src/discord/response-generator.ts:31, 87` — existing `no_session` ResponseKind (Architect-specific copy; new `no_active_role` is distinct)
- `harness-ts/src/discord/dispatcher.ts:374-445` — current `tryMentionRoute` (CW-4.5; MR1 extension target)
- `harness-ts/src/discord/dispatcher.ts:396` — first-mention-only behavior preserved by AC16
- `harness-ts/src/gates/review.ts:249` — `persistSession: false` confirms Reviewer ephemerality (MR2 NO-OP rationale)
- `harness-ts/src/session/architect.ts:404` — `relayOperatorInput(projectId, message)` signature (MR1 architect target)
