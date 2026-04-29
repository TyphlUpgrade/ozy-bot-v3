# Open Questions

## claudemd-wiki-integration - 2026-04-09
- [ ] Where to position the wiki entry in Reference Documents — after plans/ (thematic grouping) or at the end before CLAUDE.md? — Affects reading order and perceived importance.
- [ ] Should the `docs/operator-guide.md` entry in ozy-doc-index.md note that the original file is now redundant (fully migrated), or leave both listed silently? — Affects whether someone edits the wrong file.
- [ ] The `docs/Multilayer agentic workflow spec.pdf` — is this a predecessor draft of `docs/agentic-workflow.md` or a separate document? — Determines how it should be described in the parenthetical reference.

## harness-ts-phase2a - 2026-04-11
- [ ] SDK mid-stream cost tracking: do SDKMessages include per-turn cost fields, or only the final result message? — Determines whether budget alarms can fire mid-session (Phase 2A) or only post-session. Currently assuming post-session only.
- [ ] Checkpoint file format: should the agent append to a JSON array in checkpoint.json, or write separate files in a checkpoints/ directory? — Plan assumes single array file for simplicity, but agent may find it easier to write separate files.
- [ ] ConfidenceAssessment type location: currently defined in manager.ts and imported by escalation.ts, checkpoint.ts, response.ts. If coupling becomes a problem, extract to shared types.ts. — Decision deferred until implementation reveals whether the import graph is clean.
- [ ] Escalation priority over completion: if agent writes both escalation.json and completion.json, escalation wins. Is this always correct? Edge case: agent completes successfully but has a non-blocking question. — May need an escalation severity field (blocking vs advisory) in Phase 2B.
- [ ] Response level thresholds (reviewCostUsd, reviewFileCount, maxDirectCostUsd): what are good defaults? Plan uses 0.50, 10, 0.20 respectively. — Needs calibration during Phase 2A observation period before Phase 3 adds hard gates.

## harness-ts-phase2b-3 - 2026-04-12
- [ ] Escalation severity field: should escalation.json support `severity: "blocking" | "advisory"` for cases where agent completes successfully but has a non-blocking question? — Currently escalation always wins over completion. Observe real agent behavior before adding.
- [ ] Response level threshold calibration: defaults (reviewCostUsd: 0.50, reviewFileCount: 10, maxDirectCostUsd: 0.20) need tuning from real observation data. — Run Post-2B Testing Option #3 before Phase 3 hard gates.
- [ ] Mid-stream cost tracking: SDK cost fields may only appear in result message (post-session). BudgetTracker may not get incremental updates. — Accept post-session-only for now; SDK may add streaming cost later.
- [ ] Discord rate limits: webhook limit is 30 messages/60s/channel. High-activity pipelines could hit this. — Add rate limiter in WebhookSender (2s minimum spacing).
- [ ] Review gate cost: each review spawns a sonnet session at $0.10-0.30. Need to verify cost is justified vs false positive rate. — Budget cap review sessions at $0.30, skip for level 0-1.
- [ ] Dialogue session persistence: resumeSession() requires persistSession: true. Long operator response times may cause session expiry. — Fallback to new session with context is acceptable but loses session state.

## harness-ts-three-tier-architect - 2026-04-23

### Resolved (BLOCKING items resolved in Revision 2 — 2026-04-23)

- [x] **RESOLVED** — Architect `cwd` / worktree location. **Decision:** dedicated worktree at `{worktree_base}/architect-{projectId}` on branch `harness/architect-{projectId}`. Created on `createProject()`, removed on `completeProject` / `failProject` / `!project <id> abort`. Plan Section C.1.
- [x] **RESOLVED** — Project budget ceiling default. **Decision:** `budgetCeilingUsd = 10 * pipeline.max_budget_usd` (e.g., $10 with default $1 session cap). Orchestrator-level precheck before every session spawn; breach → `escalate_operator` with `reason: "budget_ceiling_reached"`. Plan Section C.4.
- [x] **RESOLVED** — `tier1EscalationCount` scope. **Decision:** two-counter model. Per-task `tier1EscalationCount` preserved (Phase 2A semantics). New per-project `totalTier1EscalationCount` with own cap (default `5 × max_tier1_escalations` = 10). Either cap exceeded → escalate operator. Plan Section C.3.
- [x] **RESOLVED** — Tier-1 resolution rate threshold X. **Decision:** X=60% locked as Wave D **acceptance gate** (not target). Sample size raised to 50 (up from 20 in Rev 1 per Critic item 7). Wave D fails if rate < 60%. Plan Section C.3 + Wave D.
- [x] **RESOLVED** — Arbitration verdict after 2 retries. **Decision:** per-task/per-project cap check BEFORE each Architect invocation; cap reached → `escalate_operator` with `reason: "tier1_cap_reached"`. Plan Wave 4 + Wave C + Section C.3.
- [x] **RESOLVED** — Architect compaction mechanism. **Decision:** orchestrator-driven abort-and-respawn with summary. Summary schema locked (`ArchitectCompactionSummary` — Section C.5) with REQUIRED verbatim `nonGoals` field. Validation fires before respawn. Plan Section C.5 + Wave D.

### Remaining INFORMATIONAL (can resolve during execution)

- [ ] Phase task-file naming convention: `project-{projectId}-phase-{NN}.json` selected. Document in `project.toml` example. — Low risk.
- [ ] Concurrent projects v1 allowed (ArchitectManager map keyed by projectId). Multi-project stress-test deferred to Phase 4. — Wave 1.75 check #9 covers two-session contention.
- [ ] Review session persistSession flag confirmed `false` (ephemeral). Regression test in Wave A (`tests/gates/review.test.ts`). — Low risk.
- [ ] Operator override mechanism for Reviewer rejection. v1 limitation: operator manually merges via git if urgent. Documented in Wave A acceptance. — Post-v1 consideration.
- [ ] Dialogue channel empty-state. **Decision:** bot prompts operator ("Use `!task`, `!project`, or `!dialogue`"). Tested in Wave 6-split. — Low risk.
- [ ] Architect prompt size. Measure during Wave B; split if > 8k tokens. — Wave B acceptance.
- [ ] Standalone dialogue → project promotion. Spec non-goal. v1 limitation: operator cancels and uses `!project`. — Documented.
- [ ] Architect Prompt Iteration Protocol (Critic item 24). Tune architect-prompt.md based on post-Wave D tier-1 resolution rate data. — Phase 4 scope.
- [ ] Reviewer observation-only graduation (Critic item 15). Deferred per Plan Section L rationale (spec locks mandatory-for-project; graduation would invalidate Wave D validation). — Post-Wave D consideration if reject rate > 40%.
- [ ] Architect session SDK-native compaction. Currently orchestrator-driven. Revisit when/if SDK supports native streaming summarize. — Phase 4+ consideration.

## conversational-discord - 2026-04-24
- [ ] Webhook ID self-filter discoverability: should `provision-webhooks.ts` write IDs to `.env.local` automatically, or just print and require operator paste? — Affects automation-vs-visibility trade-off. Default: print only.
- [ ] Active-project resolution when multiple projects run concurrently: ship `MessageContext` (per-message `messageId → projectId` map) from CW-3 day one, or stage with a "most recently declared" heuristic first? — Recommendation: ship full message-context in CW-3; ~50 LOC and avoids confusion in dual-project flows.
- [ ] `DiscordSender.sendToChannel` return-type change from `Promise<void>` to `Promise<{ messageId: string | null }>`: every existing fake/stub in tests needs a one-line update. — Mechanical migration, but pre-merge global grep required so no fake is missed.
- [ ] Should `relayOperatorInput` accept a `replyMessageId` so the Architect's response can be posted as a Discord-thread reply (visual continuity matching screenshot reference)? — Affects whether `DiscordNotifier` needs a `sendAsReplyTo` method. Defer to CW-3 follow-up if not needed for first conversational pass.
- [ ] Gateway implementation choice (`discord.js` vs raw `ws`): plan recommends `discord.js` for protocol robustness; Architect/Critic may push for raw WS to keep deps minimal. — If raw WS wins, CW-2 LOC grows from ~150 to ~300 and test mocks must speak Discord op-code dialect.
- [ ] Provisioning idempotency: should `provision-webhooks.ts` GET `/channels/{id}/webhooks` first and reuse existing webhooks, or always create new ones? — Reuse is safer (avoids webhook spam in Discord channel settings); create-always is simpler. Default: GET-then-reuse.
- [ ] `MessageContext` LRU cap (default 1000 entries ≈ 200 KB RAM): adequate for multi-day runs? — Tunable via `DiscordNotifierOptions`; persistence (sqlite-backed) deferred to CW-4 if cache-miss reports are frequent.

## llm-intent-classifier (CW-4 v2) - 2026-04-24

### v2 in-scope (resolve during/after CW-4 wave)
- [ ] **REQUIRED** Confidence floor calibration: `minConfidence: 0.7` is PLACEHOLDER. After 100 production calls, pull `intent_classifier_called`/`intent_classified`/`intent_classifier_unknown` log lines, build confidence histogram + manual-correctness rate per decile, tune floor such that correctness ≥0.95. — If unknown-rate stays >30% across all candidate floors, escalate (model upgrade, prompt tuning, or revisit decision).
- [ ] Logging destination: v2 ships console-only structured JSON. Should we route to `.omc/logs/intent-classifier.jsonl` for persistent offline mining? — Recommended within first week of production.
- [ ] Per-channel classifier rate limit (max 1 in-flight): needed to avoid pile-up under slow-API conditions? — Defer to first observed pile-up; AbortController + 10s timeout is the v1 backstop.

### Deferred to CW-4.5 (separate plan — to be written)
- [ ] `Project.channelId` modelling: does `Project` carry a channel id, or do we use single-active-project heuristic for `@<agent>` mention routing? — Decision needed before mention-routing implementation. v1 incorrectly assumed this existed; v2 verified it does not.
- [ ] READY-parser `selfBotUsername` capture: v2 verified it's NOT in `bot-gateway.ts:200-203` (only `selfBotId` is captured). CW-4.5 must add type-narrowed extraction from READY payload `user.username`. — Required for `@<bot>` mention strip.
- [ ] `ChannelContextBuffer` capacity & memory bound: v1 proposed 10 msgs × 3 channels × ~500 B = ~15 KB. — Validate under sustained traffic before merging buffer.
- [ ] `recentMessages` integration test: once CW-4.5 wires the buffer, add tests for pronoun resolution ("abort it" after a project-abort discussion). — `ClassifyContext.recentMessages` field declared in v2 but unwired.
- [ ] Buffer noise (Critic minor #9): what happens when an operator's context is dominated by status replies that crowd out the actual conversation? — Cap-by-author? Cap-by-message-type? Defer.
- [ ] Prompt-cache behaviour for haiku in single-turn classifier mode: does caching engage, or does each call pay the full prompt cost? — Verify via first 100 production calls. If no cache, follow-up wave should batch classifications via long-running session.
- [ ] `directAddress` raises lower confidence floor: when CW-4.5 lands `@bot` mention, should it permit a lower floor for direct-addressed messages (operator's intent is explicit)? — Open question for that wave.

## conversational-discord-cw45 - 2026-04-24 (v2 — supersedes v1 entries below)
- [ ] Should `extractMentions` live inline in `dispatcher.ts` or in a new `src/discord/mention-extraction.ts`? — Current target: separate file if helper > 60 LOC. Tests are clearer with module boundary.
- [ ] Should the operator-instructive reply when `multi_active_no_hint` include a list of active project ids? — UX vs verbosity tradeoff; defer to first live test feedback.
- [ ] `directAddress` flag plumbed into `ClassifyContext` but not yet consumed in v2 — should CW-4.5 wire it as a no-op interface, or defer the field entirely until CW-4.6 lands the threshold change? — v2 recommends interface-only plumbing now.
- [ ] Affinity hint cold start: when the operator FIRST mentions in a multi-active channel without ever having used reply-UI, instructive-reply fires. Is that confusing UX, or is it teaching the right reflex (reply-then-mention)? — Observe in live test scenario C.
- [ ] Should `computeProjectIdHint` also inherit from `repliedToAuthorUsername` matching an agent (independent of `MessageContext` having a record), as a redundancy belt? — v2 leaves it conservative; revisit if MessageContext miss rate is non-trivial.
- [ ] Bot-self vs agent-username collision warning at startup: log WARN if `selfBotUsername` (post-READY) collides with any IdentityMap entry. — Verify the warning surface is reachable in `live-bot-listen.ts`.

### v1 entries SUPERSEDED by Iteration 2 (traceability; do not re-act)
- [x] **SUPERSEDED** — `IdentityMap.lookupById` stub. v2 dropped it (Iteration 2 change #1, dead code without `discordId` schema). Revisit when CW-4.7 wires discord-id provisioning.
- [x] **SUPERSEDED** — Buffer eviction policy LRU-on-append vs LRU-on-read. v2 locks LRU-on-append (Iteration 2 change #3 + Test 4b).
- [x] **SUPERSEDED** — When `@<bot>` AND `<@id>` agent mention coexist. v2 removed agent-id resolution (Iteration 2 change #1); only bot-self uses Discord-ID form.
