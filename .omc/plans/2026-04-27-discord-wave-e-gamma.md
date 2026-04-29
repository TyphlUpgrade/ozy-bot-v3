# Wave E-γ — LLM Voice Per Role (harness-ts Discord)

**Created:** 2026-04-27
**Status:** Plan body — manual write per RALPLAN-postmortem fork B (skips consensus loop)
**Predecessor:** Wave E-α LANDED (commits 66801b0 / 5bec3dc / 72a3ea0). See `.omc/plans/2026-04-26-discord-wave-e-alpha.md` and `.omc/wiki/session-log-wave-e-completion-2026-04-27.md`.
**Wave context:** Third in delivery order per `.omc/wiki/phase-e-agent-perspective-discord-rendering-intended-features.md` §E.4 (E-α landed; E-β deferred to next; E-γ delivers biggest UX jump per reference-target gap analysis).
**Pre-requisite spikes (closed):**
- Spike-caveman-json (U1) → Executor caveman OFF (commit `4b425d5`)
- Spike-omc-overhead (U2) → Executor OMC OFF (commit `4b425d5`)
- Spike-architect-caveman (U5) → Architect caveman ON validated (commits `bc98868` / `dc1bf81`)
- Architect resumeSession disallowedTools restored (commit `25ae1ca`)

---

## Architecture invariants (LOCKED — Principle 1)

These are not negotiable. Every decision below MUST satisfy them.

- **I-1 Discord opaque to agents.** Agent sessions never see Discord directly. The OutboundResponseGenerator runs INSIDE the orchestrator process (not inside agent sessions). It calls the SDK as a separate spawn — that spawn is a generator, not a development agent. No `WebhookSender` / `BotSender` / `DiscordNotifier` import from `src/session/*`. The generator lives in `src/discord/`.
- **I-2 Never invent fields.** Every value passed to LLM must trace to existing `OrchestratorEvent` fields (cited at `filename:line`). No "synthesized" metrics.
- **I-3 Additive optional fields only.** No changes to `OrchestratorEvent` / `TaskRecord` / `CompletionSignal` types in this wave. Renderer reads existing fields only.
- **I-4 Verbatim allow-list.** LLM-bound event kinds must be drawn from the 27-event allow-list at `tests/discord/fixtures/allowed-events.txt`. Whitelist is a strict subset.
- **I-7 Substring pin titlecase preservation.** Phase A pin `notifier.test.ts:309` (em-dash U+2014 + `; ` glue + bracketed terminalReason) MUST be byte-equal in the static-fallback path. LLM output is not pin-asserted (model-output is non-deterministic by nature) but the static fallback IS asserted.
- **I-10 Single owner per file layer.** OutboundResponseGenerator is in `src/discord/`; only `notifier.ts` (or a new `epistle-runner.ts`) imports it. `src/session/*` does NOT.

---

## Top-level mechanic

For a whitelisted (event-kind × role) tuple where the feature flag is ON and the budget tracker permits, the generator produces a per-role first-person prose body. **Replacement-with-fallback semantic** (NOT addendum):

1. Renderer assembles E-α deterministic body (`renderEpistle(event, ctx)`).
2. If the (kind, role) tuple is whitelisted AND `harness.outboundEpistleEnabled === true` AND budget permits AND circuit breaker for that role is closed: spawn LLM generator with role's system prompt + the deterministic body as input. LLM returns a prose body that **replaces** the deterministic body.
3. On any failure (timeout, budget overage, SDK error, schema violation, circuit breaker tripped): renderer emits the **deterministic body** verbatim. The notifier sends ONE message regardless of which path produced the body.

This guarantees: at most ONE message per event; output is always at least as informative as the deterministic body; LLM never doubles emissions; static path is byte-equal to E-α.

---

## Decisions

### D1 — OutboundResponseGenerator class

**File:** `src/discord/outbound-response-generator.ts` (NEW)

**Pattern:** mirror `LlmResponseGenerator` at `src/discord/response-generator.ts:192`. Same `SDKClient.spawnSession` shape; same timeout handling; same fallback chain.

**Differences from LlmResponseGenerator:**
- Per-role systemPrompt (not single prompt). Constructor takes `Record<Role, string>` map of role → systemPrompt path.
- Input is event + role + deterministic-body, not operator-message + agent-output. Build a different user prompt structure (XML fenced).
- Whitelist: `Set<\`${OrchestratorEventType}::${Role}\`>` keys; `generate()` rejects non-whitelisted tuples by returning the deterministic-body unchanged (NOT a fallback path — a guard).

**Interface:**
```ts
export type OutboundRole = "architect" | "reviewer" | "executor" | "orchestrator";

export interface OutboundResponseGeneratorOpts {
  sdk: SDKClient;
  cwd: string;                                    // worktree-base or .harness root
  promptPaths: Record<OutboundRole, string>;     // 4 file paths
  whitelist: ReadonlySet<string>;                 // `${event.type}::${role}` tuples
  budget: LlmBudgetTracker;                       // see D3
  circuitBreaker: PerRoleCircuitBreaker;          // see D4
  model?: string;                                 // default DEFAULT_OUTBOUND_MODEL (claude-haiku-4-5)
  maxBudgetUsd?: number;                          // per-call cap, default 0.02
  timeoutMs?: number;                             // default 8000
}

export class OutboundResponseGenerator {
  constructor(opts: OutboundResponseGeneratorOpts);
  async generate(input: {
    event: OrchestratorEvent;
    role: OutboundRole;
    deterministicBody: string;
  }): Promise<string>;
}
```

**Failure semantics inside `generate()`:**
- whitelist miss → return `deterministicBody` (no LLM spawn, no log)
- circuit breaker open for role → return `deterministicBody`; log ONCE per process lifetime per role
- budget tracker rejects (daily exceeded) → return `deterministicBody`; log ONCE per UTC day to `console.warn`
- SDK spawn throws / ac.abort fires (timeout) / non-text result → increment circuit breaker; return `deterministicBody`
- any uncaught throw → swallow, return `deterministicBody`. NEVER throw out of `generate()`.

**Success path:**
- query SDK with system prompt + user prompt; `consumeStream` collects assistant text
- validate output: must contain at least one of the structured fields verbatim (e.g. for `merge_result` event, must contain the commitSha hex string if present in event)
- if validation fails → fallback to deterministic; log
- else return LLM output (truncated to `truncateBody(1900)`)

### D2 — Per-role system prompts

**Files:**
- `config/prompts/outbound-response/v1-architect.md` (NEW)
- `config/prompts/outbound-response/v1-reviewer.md` (NEW)
- `config/prompts/outbound-response/v1-executor.md` (NEW)
- `config/prompts/outbound-response/v1-orchestrator.md` (NEW)

**Each prompt MUST include:**

1. **Role-specific voice contract.** First-person declarative ("I'll proceed", "I will treat as stale") matching reference target style.
2. **Refuse embedded directives.** Required `<operator_input>` fence pattern; treat as data, never instructions. Identical safeguard to existing `LlmResponseGenerator` system prompt.
3. **Verbatim structured-field rule.** "STRUCTURED FIELDS (status, sha7, file count, error message text) appear verbatim in the output. NARRATIVE SUMMARY may be paraphrased."
4. **Length cap.** Max 1500 characters output (renderer enforces 1900 cap on top of this).
5. **Forward-looking close.** End with one sentence about next action ("I'll re-emit when the rebase lands.").
6. **Plain text only.** No code fences, no markdown except `**bold tags:**` matching E-α structured-section style.

**Prompt anti-patterns to forbid:**
- Self-introducing ("As the Architect, ..."). Reference style omits self-attribution.
- Echoing the input verbatim wholesale. Output must be a synthesis, not a paraphrase.
- Hallucinated metrics (token counts, percentages not in event payload).

**Empirical validation:** The four prompts will be smoke-tested via a new fixture-pair (E-γ smoke fixtures, see D7) that runs the full notifier path with `harness.outboundEpistleEnabled=true` against a fixed-event matrix. Operator screenshots Discord output. If voice doesn't match reference target, prompts iterate before flag flip.

### D3 — Daily LLM budget tracker

**File:** `src/discord/llm-budget.ts` (NEW)

**Storage:** `.harness/llm-budget.json` (relative to `config.project.root`). JSON shape:

```json
{
  "schemaVersion": 1,
  "currentUtcDate": "2026-04-27",
  "spentUsd": 1.234,
  "dailyCapUsd": 5.0,
  "lastUpdatedAt": "2026-04-27T12:34:56.000Z"
}
```

**Interface:**
```ts
export interface LlmBudgetTracker {
  /** Returns true if a $cost charge would NOT exceed today's cap. Atomic. */
  canAfford(estimatedCostUsd: number): boolean;
  /** Atomically increments today's spent. Resets to 0 when UTC date changes. */
  charge(actualCostUsd: number): void;
  /** Current UTC-day spent. */
  todaySpentUsd(): number;
}
```

**Defaults:** `dailyCapUsd = config.discord.llm_daily_cap_usd ?? 5.0`.

**Semantics:**
- `canAfford(0.02)` returns `false` if `spentUsd + 0.02 > dailyCapUsd` after rolling over to today.
- `charge(actualCostUsd)` only increments — never refunds. Uses `result.totalCostUsd` from `consumeStream`.
- Roll-over: read on every call. If `currentUtcDate !== now's UTC date`, reset `spentUsd = 0`, update `currentUtcDate`, write file before returning.
- File I/O is synchronous (low-frequency). Write is atomic via temp+rename per project convention.

**No persistence across process restarts is broken** — file persists, but in-memory state is rebuilt from disk on each call (last-write-wins is acceptable: budget overshoot bounded by `maxBudgetUsd × concurrent calls = $0.02 × 4 roles = $0.08`).

### D4 — Per-role circuit breaker

**File:** same `src/discord/llm-budget.ts` (co-located; both are runtime guards).

**Interface:**
```ts
export interface PerRoleCircuitBreaker {
  /** Allow next attempt for this role? */
  isClosed(role: OutboundRole): boolean;
  /** Record a failure. After 3 consecutive, open the breaker for the rest of this process lifetime. */
  recordFailure(role: OutboundRole): void;
  /** Record a success. Resets the consecutive-failure counter. */
  recordSuccess(role: OutboundRole): void;
}
```

**Implementation:**
- In-memory only. Resets on process restart (Wave E.4 spec explicit: "in-memory state, resets on restart").
- Per-role independent state.
- Threshold: 3 consecutive failures.
- Once open, stays open until process restart. Operator must restart orchestrator if the underlying issue is fixed.

**Failure types that increment counter:**
- SDK spawn throws
- Timeout (AbortController fired)
- Non-text result (no assistant text in stream)
- Schema validation failure (no required structured field present)

**Failure types that DO NOT increment:**
- Whitelist miss (not a failure)
- Budget exceeded (separate guard)

### D5 — Whitelist construction

**File:** `src/discord/outbound-whitelist.ts` (NEW)

```ts
import type { OrchestratorEvent } from "../orchestrator.js";
import type { OutboundRole } from "./outbound-response-generator.js";

/** Event-kind × role tuples eligible for LLM voice transformation.
 * Subset of the 27-event allow-list × 4 roles. Not all events are narrative.
 */
export const OUTBOUND_LLM_WHITELIST: ReadonlySet<string> = new Set([
  // Executor identity
  "session_complete::executor",
  "task_done::executor",
  // Reviewer identity
  "review_mandatory::reviewer",
  "review_arbitration_entered::reviewer",
  // Architect identity
  "architect_decomposed::architect",
  "architect_arbitration_fired::architect",
  "arbitration_verdict::architect",
  // Orchestrator identity
  "escalation_needed::orchestrator",
  "merge_result::orchestrator",
]);

export function isOutboundLlmEligible(
  eventType: OrchestratorEvent["type"],
  role: OutboundRole,
): boolean {
  return OUTBOUND_LLM_WHITELIST.has(`${eventType}::${role}`);
}
```

**9 tuples chosen** based on:
- Narrative-relevant (operator wants prose for these)
- Not high-frequency / system-noise events (`poll_tick` excluded)
- Not future-wave events (`nudge_check` is E-δ scope)

`merge_result` stays orchestrator-routed (per E-α decision); LLM voice for it is the orchestrator's perspective (e.g. "I'll proceed with the next phase since the rebase landed cleanly.").

### D6 — Notifier integration

**File:** `src/discord/notifier.ts` (modified, +~40 LOC)

Inject `OutboundResponseGenerator?` (optional) into `DiscordNotifier` via a new optional `OutboundResponseGenerator` constructor field. When unset, notifier behavior is identical to E-α.

For each `notify(event)` call:
1. Resolve identity + channel + deterministic body via `renderEpistle(event, ctx)` (existing E-α path).
2. If outbound generator is set:
   - `eligible = isOutboundLlmEligible(event.type, role)`
   - `flagOn = config.discord.outboundEpistleEnabled === true`
   - if `eligible && flagOn`:
     - `body = await outbound.generate({ event, role, deterministicBody: body })`
     - (generate() handles all fallback internally; always returns a string)
3. Truncate body via `truncateBody(1900)`.
4. Send via the appropriate sender as before.

**No new event emissions.** No new lifecycle. No state changes.

### D7 — E-γ smoke fixtures

**Files:**
- `scripts/live-discord-smoke.ts` — extend SMOKE_FIXTURES with one fixture per whitelist tuple (9 fixtures × LLM-mode flag = 18 invocations max). Add `--llm` CLI flag that sets `outboundEpistleEnabled=true`.
- `tests/discord/outbound-response-generator.test.ts` (NEW) — unit tests for whitelist guard, fallback paths, circuit breaker increments, budget tracker enforcement. Mock SDKClient.

### D8 — Feature flag wiring

**File:** `src/lib/config.ts` (modified, +1 field)

```ts
export interface DiscordConfig {
  // ... existing fields ...
  /** E-γ feature flag. When true, eligible (event, role) tuples route through
   * OutboundResponseGenerator for first-person LLM voice. Default false until
   * 48h Batch-1 smoke window completes + operator visual sign-off. */
  outboundEpistleEnabled?: boolean;
  /** E-γ daily LLM spend cap (USD). Default 5.0. */
  llm_daily_cap_usd?: number;
}
```

### D9 — Fail-loud on prompt-file missing

If any of the 4 prompt files (`config/prompts/outbound-response/v1-{role}.md`) is missing or empty at construction time, throw. Same pattern as `LlmResponseGenerator:206` (already enforced).

This guarantees: a misconfigured deployment cannot accidentally silently fall back to deterministic-only without operator awareness.

---

## Acceptance criteria

- **AC1** — `npm run lint` clean.
- **AC2** — `npm test` clean. New tests for D1 (generator), D3 (budget), D4 (circuit breaker), D5 (whitelist), D6 (notifier integration with feature flag both ways).
- **AC3** — Whitelist exactly the 9 tuples from D5 (not 8, not 10). Verified by exporting the set and asserting size + members in `tests/discord/outbound-whitelist.test.ts`.
- **AC4** — `harness.outboundEpistleEnabled` defaults to `false`. Verified by `tests/lib/config.test.ts` default-config assertion.
- **AC5** — Prompt files all exist and contain required fences: `<operator_input>` (refuse-embedded-directives) AND `<event_payload>` (event-data fence). Verified by a new test that reads each prompt and asserts substring presence.
- **AC6** — Phase A pin `notifier.test.ts:309` byte-equality preserved when `outboundEpistleEnabled=false` (deterministic path unchanged).
- **AC7** — Audit script `npm run audit:epistle-pins` still passes (deterministic body path unaffected).
- **AC8** — Architecture invariant guard `tests/lib/no-discord-leak.test.ts` still passes (generator stays in `src/discord/`).
- **AC9** — Budget tracker UTC-rollover correct: a charge on day N+1 reads/resets the file. Tested with mocked `Date`.
- **AC10** — Circuit breaker opens after exactly 3 consecutive failures per role; success between failures resets counter. Tested.

---

## Commit policy (atomic split)

Two commits per the I-6 wave-work convention:

**Commit 1 — Mechanical scaffolding (zero behavior change in production):**
- D1 OutboundResponseGenerator class (no notifier wiring yet)
- D3 LlmBudgetTracker class
- D4 PerRoleCircuitBreaker class
- D5 OUTBOUND_LLM_WHITELIST constant + isOutboundLlmEligible
- D8 config field added (default `false`)
- D2 the four prompt files committed
- Tests for D1/D3/D4/D5 (mocked SDK)

Production notifier untouched. Pin assertions unaffected. Operator can revert this commit cleanly.

**Commit 2 — Notifier integration + smoke fixture (behavior change behind flag):**
- D6 notifier changes (DiscordNotifier accepts optional generator)
- D7 smoke fixture + `--llm` flag
- Wiring tests in `tests/integration/notifier-integration.test.ts`
- AC4/AC6/AC9/AC10 tests

Production behavior changes ONLY when operator sets `outboundEpistleEnabled: true` in their config. Default OFF preserves Wave E-α behavior byte-equal.

---

## Cost analysis

- Per LLM call: ≤$0.02 (Haiku 4.5 cap; Sonnet would be ~$0.06 — too expensive)
- Eligible events: 9 tuples
- Typical daily emission rate per project: ~30 narrative-relevant events/day per E.4 estimate
- Daily LLM spend per project: ~$0.60 typical, $5/day cap
- Cold-start circuit-breaker probe per role: ≤$0.06 worst case (3 timeouts × $0.02), ~$0.01 typical
- Smoke fixture validation: ~$0.20 for 9 LLM-mode fixtures × $0.02

**Total worst-case Wave E-γ daily cost: ~$5/project (capped). Validation budget: ~$1 one-time.**

---

## Risk register

- **R1: Voice doesn't match reference target.** Mitigated by D7 smoke + operator screenshot review BEFORE flag flip. Iterate prompts in `config/prompts/outbound-response/v2-*.md` (additive — keep v1 for rollback) if needed.
- **R2: LLM spawn cost overrun.** Mitigated by D3 daily cap + D4 circuit breaker. Worst case is $5/day/project even under attacker-induced LLM thrash.
- **R3: Schema-validation rejection rate too high.** If LLM frequently omits structured fields, fallback fires often, looking degraded. Mitigated by prompt requirement #3 (verbatim fields) + D7 fixture coverage.
- **R4: Circuit breaker stays open after transient outage.** No auto-recovery in v1. Operator must restart orchestrator. Acceptable for v1 — alternative (auto-recovery) adds state-machine complexity not justified by frequency.
- **R5: Budget file corruption.** Atomic write via temp+rename. On parse failure, treat as `spentUsd: 0` for current day (start fresh). Alternative (refuse to spend) deadlocks on stale file.
- **R6: Operator confused by which path produced a body.** Add a prefix `[deterministic]` / `[llm]` to debug logs only — NOT to user-visible output. Operator can grep notifier logs.

---

## Out of scope

- **Wave E-β** — message_reference reply chains. Separate plan; deferred per phase-e wiki delivery order.
- **Wave E-δ** — `nudge_check` periodic + per-role mention routing. Separate plan.
- **Per-role avatars** — already populated via webhook config in E-α. No changes.
- **Multi-language voice** — English only.
- **Persistent budget across restart** — Already persistent via JSON file. No further work.
- **Persistent circuit breaker across restart** — Explicitly NOT persistent per E.4 spec. Operator restart resets.

---

## Phased delivery within this wave

If Commit 2 turns out larger than estimated, allow optional sub-split:
- **Commit 2a** — notifier integration + tests; flag default false
- **Commit 2b** — smoke fixture + `--llm` flag

Both must land before flag flip. If sub-split, document in commit 2a body.

---

## Cross-refs

- `.omc/wiki/phase-e-agent-perspective-discord-rendering-intended-features.md` — full Phase E intent
- `.omc/wiki/reference-screenshot-analysis-conversational-discord-operator-st.md` — operator's reference target
- `.omc/wiki/ralplan-procedure-failure-modes-and-recommended-mitigations.md` — why this plan was hand-written instead of consensus-driven
- `.omc/wiki/session-log-wave-e-completion-2026-04-27.md` — Wave E-α completion log; recommended-next E-γ
- `src/discord/response-generator.ts:192` — `LlmResponseGenerator` (mirror pattern source)
- `src/discord/notifier.ts` — Wave E-α deterministic renderer (integration target)
- `tests/discord/fixtures/allowed-events.txt` — 27-event allow-list (whitelist subset source)
