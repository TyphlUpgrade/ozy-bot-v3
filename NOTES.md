# Engineering Notes

Permanent record of open concerns, deferred work, and architectural analyses.
Not a session log — session-specific observations belong here only if they surface a lasting concern.

**Status labels:** `open` · `deferred` · `resolved` · `won't fix`

---

## Open Concerns

### CONCERN-2: Entry conditions path bypasses composite score floor
**Status:** `open`  
**Severity:** Low (inconsistency, not a bug)

A candidate in `new_opportunities` with `entry_conditions` set is evaluated by `_medium_try_entry` using Claude's conviction as a score proxy (no composite floor check). A freshly-evaluated candidate in the normal ranker path must clear `min_composite_score` (default 0.30). PFE demonstrated this: it scored 0.56–0.58 via the conviction path while scoring 0.00 via the fresh ranker path after being pruned from Claude's output.

The inconsistency allows stale thesis candidates to compete for entries indefinitely as long as they remain in `new_opportunities`, regardless of live TA deterioration. The correct fix is to apply the composite floor check before entry even when entry_conditions are present.

**First observed:** 2026-03-25 session log (FINDING-11)

---

---

### DEFERRED-1: DRIFT_LOG File Index needs to be built
**Status:** `deferred`  
**Effort:** ~30–45 minutes

`DRIFT_LOG.md` has 47 sections with no index. Finding what's relevant to a given file currently requires reading across multiple sections. A File Index table (file → relevant section names) is stubbed at the top of DRIFT_LOG with a maintenance instruction, but the table itself is empty.

Build the index by reading each section and mapping file references. The instruction to update it on each new entry is already in place. Focus on the files developers actually touch — `orchestrator.py`, `risk_manager.py`, `claude_reasoning.py`, `opportunity_ranker.py`, `strategy` modules — not test files or one-off prompt entries. `orchestrator.py` and `config.py` will list nearly every section; for those, consider whether a row is useful or just noise.

**Do at the start of a fresh session — not as an end-of-day addition.**

---

### CONCERN-3: Slope/accel indicators underutilised in entry conditions
**Status:** `open`  
**Severity:** Medium (suboptimal entry gates, not a correctness bug)

`rsi_slope_5` and `rsi_accel_3` are computed, not in `_TA_EXCLUDED`, and visible to Claude in `ta_readiness`. The prompt mandates slope conditions for momentum entries (reasoning.txt line 70). In practice Claude ignores this mandate for swing entries and inconsistently for momentum — defaulting to simpler gates (`rsi_max`, `require_below_vwap`) that require less calibration work.

The FISV `rsi_slope_max=0.5` case (2026-04-02 session) is the clearest symptom: Claude used the slope condition but got the sign wrong, and the feedback loop gave it no signal that the condition was structurally invalid. The `last_block_reason` fix (2026-04-02) addresses this for the invalid-value case.

**Remaining gap:** Prompt design. The mandate approach has not worked — making it louder won't either. The correct approach is to tie condition selection to the setup type Claude already declared rather than mandating specific fields categorically. For swing entries specifically:

> *"Your entry_conditions must be consistent with the setup description you wrote. Breakdown/momentum continuation → rsi_slope_max is the primary gate (check ta_readiness.rsi_slope_5). Extended/fade short → rsi_accel_max is the primary gate (check ta_readiness.rsi_accel_3). If neither fits, explain why."*

This asks for internal consistency rather than rule compliance, which is a more natural ask for a reasoning model and preserves judgment on atypical setups.

**Precondition:** Run one or two sessions with `last_block_reason` live first. If Claude starts self-correcting invalid conditions, the feedback loop is functioning and the prompt change is worth making. If not, the problem is deeper than salience.

**Do not:** Add escalating-skepticism signals to deferral counts ("you've been waiting N cycles, consider revising"). This creates pressure to lower entry standards mid-session, which is the opposite of what's needed. The 15-defer limit is the correct abandonment mechanism.

**First observed:** 2026-04-02 session log analysis

---

### CONCERN-4: reasoning.txt context-unaware — sends full content regardless of session state
**Status:** `open`  
**Severity:** Medium (wasted tokens, unfocused context)

reasoning.txt is always sent in full (~30KB, ~187 lines). Several sections are irrelevant depending on session state: position review instructions when there are no open positions (~15 lines), the swing daily signals block when there are no swing positions (~7 lines), the full entry_conditions reference (available keys, slope/accel calibration, rsi_max rules, ~27 lines, ~580 tokens) when watchlist_tier1 is empty. On a pure position-review cycle these sections are noise that Claude must parse and weight.

**Fix:** Conditional prompt assembly in `_build_reasoning_prompt()`. Already partially done via `{position_review_notice}` — extend it to conditionally include/exclude section blocks based on `open_positions > 0`, `swing_positions > 0`, `len(watchlist_tier1) > 0`. Prompt file gets section delimiters; builder assembles from them. Code change is ~30–40 lines in claude_reasoning.py.

**Do not:** Extract sections into separate files and inject via template placeholders — same token count, just adds indirection with no benefit.

**First observed:** 2026-04-06 reasoning.txt audit

---

### CONCERN-5: Prompt versioning scheme copies entire directory on each bump
**Status:** `open`  
**Severity:** Low (maintenance overhead, no runtime cost)

Every prompt version bump copies all 8 files in `config/prompts/`. Between v3.10.1 and v3.10.3, reasoning.txt changed 3 times; the other 7 files are identical across all three versions. The versioned directory scheme made sense when multiple files changed per version — now it creates 7 decorative duplicates per bump and obscures which file actually changed.

**Fix:** Only version reasoning.txt. Move the 7 stable prompts to `config/prompts/` root (unversioned). Config stores `reasoning_prompt_version` only; all other prompts are loaded directly from the root path. Version bumps then touch only the file that changed.

**Precondition:** Do not do this mid-session when other changes are in flight. Clean start, single-purpose session.

**First observed:** 2026-04-06 reasoning.txt audit (Umbra: "29kb, rest are decorative")

---

### CONCERN-6: Agentic Workflow v3 — Orchestrator monolith
**Status:** `open`  
**Severity:** High (highest blast radius, hardest to test, most likely to cause cascading failures)

`tools/agent_runner.py` is assigned ~18 distinct responsibilities: task watching, intent classification, API calls to Architect, worktree creation, CLAUDE.md generation, tmux session spawning, checkpoint polling, Architect response routing, Reviewer API calls, merge execution, post-merge testing, revert on failure, cost tracking, budget enforcement, session killing, failure escalation, heartbeat emission, task deduplication, priority scheduling, and state persistence. This is the monolith the plan claims to avoid. A bug in cost tracking can cascade into merge failures. The plan's own modularity philosophy says "if a new feature requires touching more than two modules, the abstraction is wrong."

**Solution analysis (2026-04-07):** Extract 3 infrastructure modules, keep 2 domain clusters. The 18 responsibilities cluster into 5 natural groups. **TaskManager** (task watching, deduplication, priority scheduling, intent classification, state persistence — pure data operations on `state/agent_tasks/`), **WorktreeManager** (worktree creation, CLAUDE.md generation, tmux spawning, session killing, timeout — git/tmux operations), and **CostTracker** (cost tracking, budget enforcement, heartbeat — pure accounting) extract cleanly using the same pattern proven in the trading bot extraction: mutable shared ref to `orchestrator_state` dict at construction, runtime params at call time, single asyncio event loop. Pipeline sequencing (Architect → Executor → Reviewer + checkpoint loops) and merge/revert stay in the orchestrator — they are the orchestrator's reason to exist. Extracting them creates a god-context object, the same problem that kept `_medium_try_entry` in the trading bot. ~60% size reduction. Phase E restructure required.

**First observed:** 2026-04-07 architectural review of agentic-workflow-v3.md

---

### CONCERN-7: Agentic Workflow v3 — Checkpoint polling burns Executor context
**Status:** `open`  
**Severity:** High (directly degrades Opus Executor quality)

The Executor writes `checkpoint.json`, then polls for `architect_response.json`. During this wait, the Claude Code session is alive — consuming an API seat and holding its full context window. Each poll-wait-read cycle adds low-value turns to the conversation. Over a 5-checkpoint plan, context degradation could meaningfully erode Executor quality at the most expensive model tier.

**Solution analysis (2026-04-07):** Replace polling with exit-and-respawn. The zone file was already designed for crash recovery (records last completed unit, approach, test status). Checkpoint polling exists because the plan assumes session continuity is valuable — it isn't, because zone file + git branch carry all state. New protocol: Executor completes checkpoint unit → updates zone file → commits → **exits**. Orchestrator detects checkpoint → calls Architect API → pre-seeds `architect_response.json` into worktree → spawns fresh Executor with "continue from unit N." Zero idle burn, fresh context window per segment. Tradeoff: loses in-session mental model (files read, approaches tried), mitigated by zone file + git commits + Architect response. Over a 5-checkpoint plan, fresh context per segment dominates vs 4 rounds of idle-polling degradation at Opus cost. Low effort — Execution_Policy rewrite + one zone file field (`resumed_from_checkpoint`).

**First observed:** 2026-04-07 architectural review of agentic-workflow-v3.md

---

### CONCERN-8: Agentic Workflow v3 — Dual signal bus violates Principle 1
**Status:** `open`  
**Severity:** High (class of silent-failure bugs)

The plan declares "signal files are the universal bus" (Principle 1) but the architecture has two distinct signal namespaces: `<worktree>/.executor/` for Executor signals and `state/signals/` in the main repo for everything else. The orchestrator translates between them. Stale or orphaned signals in worktrees after crashes are silently dropped if the orchestrator's worktree-path mapping gets out of sync with reality.

**Solution analysis (2026-04-07):** Rename, don't unify. The dual bus is architecturally correct — `.executor/` is a sandboxed outbox, the orchestrator is a signal gateway. Unifying would break worktree isolation (the core safety property): Executor writing to `state/signals/` enables cross-task signal corruption; symlinks are fragile (dynamic worktree lifecycle); signal writer abstraction silently breaks the trust boundary. Fix: (1) rename in plan — `.executor/` is "executor outbox," orchestrator polling is "signal gateway," Principle 1 becomes true by definition; (2) add explicit startup reconciliation — on orchestrator restart, scan all active worktree `.executor/` dirs for unprocessed signals. Orphaned signals on crash are low severity: orchestrator tracks worktree paths in `orchestrator_state.json`, re-polls on restart, worktrees preserved on failure. Trivial effort — terminology + one sentence addition.

**First observed:** 2026-04-07 architectural review of agentic-workflow-v3.md

---

### CONCERN-9: Agentic Workflow v3 — Additional architectural issues (5 items)
**Status:** `open`  
**Severity:** Medium

**a) Claude Code token tracking is aspirational.** The plan says "Executor writes cumulative token usage to zone file" but Claude Code doesn't expose token counts programmatically. The 80%/100% budget enforcement layer for Executor sessions won't work or will be wildly inaccurate.

**Solution (2026-04-07):** Drop token tracking for Executor sessions entirely. Claude Code's `--max-budget-usd` and `--output-format json` only work with `--print` mode, not interactive tmux sessions. The Executor runs on Max plan (zero API cost), so token counts have no dollar consequence. Keep the 60-minute wall-clock timeout (already planned). Keep token tracking for API-based agents only (Architect, Reviewer, Strategy Analyst) where it's trivially available in the response. Zone file `cumulative_tokens` field → replace with `wall_clock_seconds` for Executor zones.

**b) Post-merge revert unsafe with parallel Executors.** `git revert --no-edit HEAD` assumes merge ordering. If two Executors merge close together and the first merge's tests are still running, the revert can target the wrong commit.

**Solution (2026-04-07):** Use merge commit SHA instead of HEAD. The race window is near-zero under current design (orchestrator is single-threaded, manages merges procedurally), but `HEAD` is fragile regardless — an operator hotfix between merge and revert breaks it. The orchestrator already performs the merge; capture the SHA from `git merge` output and pass to `git revert --no-edit <sha>`. One variable, one line. No merge lock or queue serialization needed — the plan already serializes merges.

**c) Haiku context vs day-long pattern accumulation.** Ops Monitor uses persistent Haiku to detect patterns over the trading day, but compaction will discard the oldest pattern history — exactly what the persistence is meant to preserve.

**Solution (2026-04-07):** Structured daily summary file + reduced poll frequency. (1) Ops Monitor maintains `state/ops_daily_summary.json` with rolling anomaly counts, pattern timestamps, and trend flags — updated every cycle, read back after compaction. Decouples pattern memory from conversation memory entirely. Follows existing signal-file convention. (2) Reduce polling from 60s to 2-3 minutes (130-195 polls vs 390, same detection quality for multi-minute anomalies, longer between compactions). (3) PreCompact hook injects one-line reminder: "Read ops_daily_summary.json for pattern history." Two-role split (stateless poller + periodic Sonnet) rejected as over-engineering — adds a second agent and coordination protocol to solve what a single JSON file solves.

**d) No backpressure on task ingestion.** Three sources write tasks (Ops Monitor, Strategy Analyst, humans). Nothing limits ingestion rate. A bad trading day could create a 15-task backlog that takes hours to drain, making bug reports stale by the time they're processed.

**Solution (2026-04-07):** The real problem is staleness, not volume. Three layered defenses: (1) Reproduction gate — before calling Architect, orchestrator re-runs the bug report's reproduction test. If it passes, auto-close as `resolved_before_processing`. Highest value, catches the 9AM-bug-fixed-by-2PM scenario. Skip for `source: human` tasks. (2) TTL per task type — bug reports 2h, strategy findings 8h, human tasks no TTL. Orchestrator checks file age at dequeue. (3) Ops Monitor rate limit — cap at 3 bug reports per rolling hour (prevents cascade from a single root cause generating many reports). Queue depth limit rejected — priority ordering + TTL + reproduction gate cover all failure modes without forcing a drop policy.

**e) Worktree leak on failure path.** Failed/timed-out worktrees are "preserved for debugging" with no automated cleanup, no TTL, no disk monitoring. Worktrees accumulate indefinitely.

**Solution (2026-04-07):** TTL + startup sweep + max count cap. (1) 48-hour TTL on failed worktrees (timestamp recorded in `orchestrator_state.json`, configurable). (2) Startup sweep removes worktrees past TTL. (3) Max-5 cap — if a new failure exceeds the cap, remove the oldest. Tarball archiving rejected: adds compression logic and its own cleanup problem. After 48h, the git branch still exists — `git log`/`git diff`/`git show` recover all committed state. Uncommitted changes are the only loss, and Executors commit before signaling completion.

**First observed:** 2026-04-07 architectural review of agentic-workflow-v3.md

---

## Resolved Concerns

Resolved items are deleted after one session. See `DRIFT_LOG.md` for the permanent record of what was implemented and why.

---

## Engineering Analyses

### 2026-04-06 — Orchestrator Extraction (completed)

**Result:** orchestrator.py reduced from 5393 → 3605 lines (-33%) across two phases. 7 modules
extracted into `core/`. Reconciliation extraction intentionally skipped (too many scalar writes).
`_medium_try_entry` and `_run_claude_cycle` remain in orchestrator — too coupled to shared mutable
state. See COMPLETED_PHASES.md for full implementation details and `plans/2026-04-06-orchestrator-extraction-phase*.md` for the approved designs.

**Parallel work zones now available:**

| Zone | Files | Safe for parallel agents? |
|------|-------|-----------------------------|
| TA indicators | `technical_analysis.py` | Yes |
| Strategies | `strategies/*.py` | Yes |
| Trigger logic | `core/trigger_engine.py` | Yes |
| Context building | `core/market_context.py` | Yes |
| Watchlist mgmt | `core/watchlist_manager.py` | Yes |
| Fill handling | `core/fill_handler.py` | Yes |
| Position reviews | `core/position_manager.py` | Yes |
| Quant overrides | `core/quant_overrides.py` | Yes |
| Position sync | `core/position_sync.py` | Yes |
| Risk manager | `execution/risk_manager.py` | Yes |
| Broker | `execution/alpaca_broker.py` | Yes |
| Claude prompts | `config/prompts/` | **Serialize** |
| Orchestrator core | `core/orchestrator.py` | **Serialize** |

---

### 2026-04-07 — Agentic Development Workflow Design

**Goal:** Enable parallel AI agent development of Ozymandias + autonomous bot operation, both
coordinated through Discord. Informed by the claw-code orchestration model (OmX/clawhip/oh-my-openagent
stack that shipped 48K LOC Rust in an hour using coordinated agents driven from a Discord text box).

**Architecture: three separated concerns**

The claw-code insight is that workflow, monitoring, and coordination are three distinct problems that
must not share context windows. An agent implementing a feature should never have notification logic
in its working memory. A monitoring daemon should never reason about code architecture.

| Layer | Claw-code equivalent | Ozymandias implementation | Owns |
|-------|---------------------|--------------------------|------|
| **Directive parser** | OmX (`$team`, `$ralph`) | Discord command → structured task decomposition | Turns human text into zone-scoped agent tasks |
| **Signal daemon** | clawhip | Separate Python process, watches `state/signals/` + git | All monitoring, notification, Discord I/O. Never touches agent context. |
| **Agent coordinator** | oh-my-openagent | Manages Architect → Executor → Reviewer cycle | Conflict resolution, task handoffs, verification loops |

**Signal files are the universal bus.** The trading bot, the dev agents, and the signal daemon all
communicate through structured JSON files in `state/signals/`. Discord is one client of this bus,
not the bus itself. If Discord goes down, signal files still work. SSH `touch state/PAUSE_ENTRIES`
still works. A cron job still works.

```
Trading Bot → writes signal files (trade events, reviews, alerts)
Dev Agents  → write signal files (task claims, completions, test results)
                    ↓
            [Signal Daemon — separate process]
                    ↓
            Discord webhooks (outbound notifications)
            Discord listener (inbound commands → signal files)
            Git watcher (commits, PRs → Discord)
```

**Signal daemon design (clawhip equivalent):**

Separate process. Stateless. Watches:
- `state/signals/` — bot trade events, position reviews, alerts, agent task claims/completions
- Git — new commits, branch creation, PR activity
- Agent tmux sessions — lifecycle events (started, completed, errored)

Routes to Discord channels:
- `#trades` — entry/exit fills, stop hits, target hits, Claude exits
- `#reviews` — position review summaries, thesis breach alerts, regime changes
- `#alerts` — emergency exit, broker degradation, daily loss limit
- `#daily` — session open/close summary, P&L, equity
- `#agent-tasks` — task claims, completions, test results
- `#agent-prs` — automated PR notifications

Writes inbound signal files from Discord commands:
- `!pause` → `state/PAUSE_ENTRIES`
- `!resume` → removes `state/PAUSE_ENTRIES`
- `!status` → reads `state/signals/status.json`, posts to channel
- `!exit <symbol>` → `state/EMERGENCY_EXIT` (or per-symbol variant)
- `$team "implement X"` → writes structured task to `state/agent_tasks/`

**Bot signal file API (extends existing EMERGENCY_* pattern):**

Outbound (bot writes, daemon reads):
- `state/signals/last_trade.json` — most recent entry/exit with full context
- `state/signals/last_review.json` — most recent Claude position review
- `state/signals/status.json` — equity, positions, open orders, loop health (written every fast tick)
- `state/signals/alert.json` — emergency/degradation events (append-only, daemon consumes)

Inbound (daemon writes, bot reads on fast-loop tick):
- `state/PAUSE_ENTRIES` / `state/RESUME_ENTRIES` — suppress/allow new entries
- `state/FORCE_REASONING` — trigger immediate slow loop cycle
- `state/FORCE_BUILD` — trigger immediate watchlist build
- `state/APPROVE_<symbol>` — approval gate response (for future supervised mode)

**Agent roles and verification loop:**

Three roles, mapped to Claude Code instances:

- **Architect** — reads directive + CLAUDE.md + COMPLETED_PHASES.md + NOTES.md + DRIFT_LOG.
  Produces a plan with zone boundaries, file constraints, and verification criteria.
  Context: full doc set. Does not write code.

- **Executor** — picks up plan, works within assigned zone, writes code + tests.
  Multiple Executors can run in parallel on different zones.
  Context: CLAUDE.md + plan + zone files only. Does not see full architecture.

- **Reviewer** — runs tests, reads diff, checks against CLAUDE.md conventions,
  checks for integration issues across zone boundaries.
  Context: diff + test output + CLAUDE.md. Does not see Architect's reasoning.

Cycle: Architect → Executor(s) → Reviewer → (back to Architect if re-planning needed) → merge.

Agent coordination through signal files:
- `state/agent_tasks/<task-id>.json` — task definition (zone, files, plan, criteria)
- `state/agent_claims/<zone>.lock` — agent claims zone before working (prevents collision)
- `state/agent_results/<task-id>.json` — completion status, test results, branch name

**Context boundary enforcement:**

Each agent role gets a defined context window scope. This prevents bloat and keeps agents focused:
- Architect: full doc set, no source code beyond structure
- Executor: CLAUDE.md conventions + plan + zone source files
- Reviewer: diff + test output + CLAUDE.md conventions

The signal daemon keeps all monitoring/notification outside every agent's context. An Executor deep
in a complex implementation never has its limited memory filled with Discord formatting or git
webhook logic.

**Bot autonomy escalation (runtime, not dev):**

Four levels, config-driven (`autonomy_level` in config.json):
1. **Supervised** — bot proposes entries via signal file, waits for `APPROVE_<symbol>` before placing
2. **Guided** — entries auto-execute, exits auto-execute, all events notify Discord
3. **Autonomous** — notify only on fills, reviews, and alerts
4. **Silent** — daily summary only

Default starts at Guided. Escalation requires explicit operator command via Discord.

**Prerequisites before implementation:**

1. Commit Phase 2 extraction (done, needs commit) — parallel zones depend on it
2. Fix CONCERN-5 (prompt versioning copies 8 files per bump) — multi-agent prompt footgun
3. Discord server setup (channels, webhook URLs, bot token) — operator task, not code
4. Signal daemon is the first code deliverable — everything else plugs into it

**Implementation order:**

1. Signal file API on the bot (extend `EMERGENCY_*` pattern, add `state/signals/` output)
2. Signal daemon (standalone process, webhook-only outbound first)
3. Discord inbound commands (daemon listens, writes signal files)
4. Agent task format + claim/completion protocol
5. Architect/Executor/Reviewer role definitions + context scoping
6. Bot autonomy levels (approval gates, escalation)

**What not to build:**

- No custom agent runtime. Claude Code instances in tmux sessions, coordinated by signal files
  and the daemon. The orchestration is in the file protocol, not in a framework.
- No complex message broker. Signal files with polling. The fast loop already polls every 5-15s.
  The daemon polls or uses `watchdog`. No Redis, no RabbitMQ, no Kafka.
- No agent-to-agent direct communication. All coordination goes through signal files. If the
  Reviewer needs to tell the Executor something, it writes a signal file. The daemon routes it.

---

### 2026-04-06 — Trade Journal Performance Audit (68 trades, 2026-03-19 to 2026-04-06)

**Overall:** 68 completed trades, 42.6% win rate, +$576.76 / +9.80% total P&L. System is net profitable because average wins (+2.12%) are 1.52x average losses (-1.39%). Best trade: SLB +9.33% (swing/long). Worst: MKC -9.55% (swing/long, stop hit).

**Finding 1: Shorts are a significant drag.**
9 short trades, 11.1% win rate, -7.75% total P&L. Only one winner (UHS +0.69%). Swing/short is 1/7 (14%), momentum/short is 0/2. The system has no demonstrated edge on the short side. Claude is already citing "0% short win rate" to reject shorts in real-time, but continues proposing them each session.

**Finding 2: The edge is entirely in multi-day swing longs.**
Trades held 1-3 days: 67% win rate, +14.51%. Trades held 3+ days: 78% win rate, +23.09%. Everything under 24 hours is net negative. Momentum has 5 trades at 20% win rate (-0.80%). The system makes all its money when it holds swing longs for days and loses it on short-duration entries.

**Finding 3: Profit targets are nearly irrelevant.**
Only 2 of 68 trades (2.9%) hit their profit target. 73.5% of exits are Claude "strategy" exits (+18.22% total from those). Targets may be too ambitious. The 2 target hits produced +12.64% — huge when they land, but 66 other trades never reached them.

**Finding 4: Stop losses are the biggest P&L destroyer.**
10 stop exits totaled -20.57%. MKC lost 9.55% (stop at $51 was 4.8% below entry for a low-vol consumer staples stock — too wide). LNG lost 3.69% in 45 minutes. XOM lost 3.16% in 130 minutes. Some stops are calibrated for swing hold duration but applied to positions that should have been cut faster.

**Finding 5: 13:00 ET hour is toxic.**
7 trades entered at 13:00 ET: 14% win rate, -17.14% total P&L. This single hour accounts for nearly all gross losses. Early morning (09:00-10:00) also underperforms. Late afternoon (14:00-15:00) is the strongest window at 60%+ win rate and +20.53% combined.

**Finding 6: Ultra-short holds indicate entry quality problems.**
15 trades held under 10 minutes, 27% win rate. These are positions entered and immediately reversed — false entries where Claude or quant override killed the position before the thesis had time to play out.

**Finding 7: Prompt v3.10.1 is underperforming — but the v3.6.0 baseline is inflated.**
42 trades at 38% win rate and -8.26% total P&L. v3.6.0 was 8 trades at 88% win rate and +25.88% — but 96% of v3.6.0's dollar profit came from 4 energy swing longs (HAL, SLB, XLE, CVX) riding a single sector rally over 5-6 days. That's one good macro call, not a structurally superior prompt. Strip the energy cluster and system total P&L drops from +9.80% to roughly +$450 across 60 trades. v3.10.1's underperformance is real (net negative over 42 trades) but the comparison benchmark needs this asterisk.

---

### 2026-04-06 — Session Log Analysis (6 sessions, full trading day)

**Day result:** equity $30,056.46 → $30,020.12 (-$36.34, -0.12%). 3 completed trades (0 wins). 1 position held overnight (WBD long 121 shares @ $27.38, merger arb thesis).

**Completed trades — all losses, all thesis breach exits:**

| Symbol | Dir | Entry | PnL | Duration | Exit Reason |
|--------|-----|-------|-----|----------|-------------|
| NKE | short | $43.64 | -0.71% | 59 min | Zacks earnings upside surprise |
| ALB | short | $172.24 | -0.10% | 3 min | US supply chain initiative headline |
| TKO | long | $199.66 | -0.48% | 1 min | Daily downtrend deterioration |

**Claude API usage:** 15 Tier-1 reasoning calls (~240K input tokens, ~66K output), 10 position reviews (~13K input, ~1.9K output). All 15 Tier-1 calls exceeded the 60s warning threshold (range 64.8s–111.1s). Cache token logging shows 0 cache_read / 0 cache_create — the prompt restructuring from this session has not yet been deployed to a running bot instance.

**Dead zone behavior:** WBD blocked by dead zone ~20 times across sessions 3-5. Dead zone bypassed once at 12:55 ET when SPY RVOL hit 1.97 (≥ 1.50 threshold). After bypass, WBD entered and filled immediately. The bypass is working as designed. Note: swing entries were still being blocked because `SwingStrategy.dead_zone_exempt` was incorrectly returning `False` (fixed this session — restored to `True`).

**Claude calibration errors observed:**
- ALB: `rsi_slope_max=0.5` for a short (positive value, must be negative) — blocked 7+ times
- CTVA: `rsi_max=35` vs RSI 47-48 — entry impossible without massive RSI crash
- IVZ: `rsi_max=30` vs RSI 44-49 — same calibration error
- These are the same class of error documented in CONCERN-3

**Post-market order churn:** WING placed/cancelled 7 times (300s timeout each), L placed/cancelled 5 times. Extended hours with thin liquidity — bot kept trying stale limit prices that never filled. No mechanism to detect "this price isn't going to fill in extended hours" and stop trying.

**yfinance mass failure at 20:25Z:** 50+ symbols returned NoneType. TA cycle spiked to 52.3s (normally 2-3s). Auto-recovered in ~2 minutes. Expected behavior after market close.

**RVOL filter drift through the day:** min_rvol started at 1.2 (Claude raised it citing 0% short win rate), drifted to 0.7-0.8 by afternoon as most symbols fell below threshold. Claude is adjusting this filter reactively each call but the adjustments are not persistent — each new reasoning call re-evaluates from scratch, causing oscillation.

**Regime assessment:** Spent entire day in "sector rotation" (confidence 0.58-0.72) with one brief "risk-off panic" from cache at 17:46Z, then settled to "normal" by close.

---

### 2026-04-02 — Orchestrator God Object: Analysis and Disentanglement Path *(superseded by 2026-04-06 extraction)*

`orchestrator.py` is 5,305 lines and 58 methods. It currently owns: startup/shutdown lifecycle, all three loop bodies, fill handling, entry execution, trigger evaluation, Claude cycle orchestration, market context assembly, watchlist lifecycle, regime management, position review application, degradation/broker failure state, and PDT management. CLAUDE.md deliberately encodes "only the orchestrator knows about all other modules" — that rule is doing real work and should be preserved. The question is whether everything currently living inside the class needs to be there to honour it.

**What makes this hard to split**

The loops share mutable state inline: `_filter_suppressed`, `_recommendation_outcomes`, `_entry_defer_counts`, `_all_indicators`, `_trigger_state`, `_degradation`. The fast loop writes fill state that the medium loop reads for suppression. The medium loop writes indicator state that the slow loop trigger check reads. This isn't accidental coupling — it's a consequence of the loops being designed to share a consistent world-view within a single asyncio event loop. Any extraction that doesn't account for this will introduce subtle ordering bugs.

**What can be extracted cleanly today**

Three clusters have low coupling to the shared mutable state and could move without risk:

1. **`TriggerEngine`** → `core/trigger_engine.py`  
   `SlowLoopTriggerState` + `_check_triggers` + `_update_trigger_prices`. Pure evaluation logic — reads state, returns a list, sets flags on a dataclass. Already unit-testable in isolation (the trigger tests prove this). Orchestrator holds the engine and calls `engine.check(now)`. This is the safest first extraction.

2. **`MarketContextBuilder`** → stays in `intelligence/` or `core/`  
   `_build_market_context` is ~150 lines of data assembly. No loop state is written by it. It takes account/PDT/indicators/session as inputs and returns a dict. Stateless and pure once extracted.

3. **`WatchlistLifecycle`** → `core/watchlist_manager.py`  
   `_apply_watchlist_changes`, `_prune_expired_catalysts`, `_clear_directional_suppression`, `_regime_reset_build`. These share cohesion around watchlist state and don't depend on fill/entry state. The orchestrator calls them with the watchlist object and gets the mutated result back.

**What should stay in orchestrator for now**

`_medium_try_entry` (~500 lines), the fill pipeline (`_dispatch_confirmed_fill`, `_register_opening_fill`, `_journal_closed_trade`), and `_fast_step_quant_overrides` all depend heavily on the shared mutable state and on each other's ordering. Extracting them now would mean threading `_filter_suppressed`, `_recommendation_outcomes`, `_entry_defer_counts` through every call — either as a god-context object (different shape, same problem) or as per-call arguments (verbose and fragile). The benefit doesn't justify the risk yet.

**The right long-term shape**

If this ever gets a full refactor, the correct pattern is a `SessionContext` dataclass that holds all cross-cutting mutable state and is passed by reference to extracted components. The orchestrator becomes a thin loop dispatcher. But this is a significant rewrite — the right time to do it is when a specific loop body needs to be independently tested or deployed, not before.

**Recommendation:** Extract `TriggerEngine` first (lowest risk, already tested, would clean up `_check_triggers` which is the most self-contained heavy method). See if that creates a good template for the others. Do not attempt `_medium_try_entry` or the fill pipeline without a clear motivation beyond cleanliness.

---

### 2026-04-01 — Phase 23 Validation (post-market run)

Session `2026-04-01T20:40:47Z` was the first run with all Phase 23 changes active.

**Build decoupling confirmed:** Watchlist was 67 minutes old at startup (stale threshold typically 60 min), but `no_previous_call|indicators_ready` fired as a reasoning-only trigger. The Claude call started at 20:40:50Z and completed at 20:42:04Z (74.3s). No build blocking — under the old architecture this cycle would have run a 30-120s build first.

**Regime and output:** risk-off panic at confidence 0.72. Five candidates returned: NKE, LLY, NVO, XOM, CVX — all shorts or energy plays consistent with the regime. Four rejections (CAT, V, MS, WMT) with coherent rationale. LLY hard-filtered on RSI 38.8 (momentum at oversold RSI — prompt fixed). filter_adjustments applied min_rvol=0.55 for LLY/NKE/NVO catalysts; consistent across all three medium loop cycles.

**No entries — expected:** 7-minute post-market session. NKE deferred on `rsi_slope_max=0.00` (slope 5.10 — RSI still rising, correct gate for a short). NVO deferred on `require_macd_bearish` with signal='bullish' (correct). CVX blocked by wrong rsi_accel gate (prompt fixed). Session ended manually after 3 cycles.

**Token usage:** 11,924 input / 3,751 output. No truncation warnings in Call B context.

---

### 2026-04-01 — Slow Loop Latency

A full slow loop cycle with all triggers active made **four sequential Claude round-trips** before returning:

```
account fetch (500ms)
→ daily bars, parallel gather (2s)
→ watchlist build if stale        ← Claude call 1: 30–120s  [FIXED — now background]
→ position reviews, split Call A  ← Claude call 2: 2–5s     [FIXED — skipped when no positions]
→ Haiku pre-screen                ← Claude call 3: 2–3s
→ Sonnet reasoning, Call B        ← Claude call 4: 15–45s
```

Worst-case was ~200s in a single blocking cycle. Root cause: `_run_claude_cycle` handled both the watchlist build and reasoning paths sequentially. When `watchlist_stale` co-fired with any reasoning trigger (common given a 60-minute max interval), the build ran to completion — including web search tool-use rounds — before position reviews or opportunity discovery began.

**Resolution:** Phase 23 decoupled the build into `_run_watchlist_build_task()` as a background task. Call A is skipped when `portfolio.positions` is empty. The remaining two calls (Haiku, Sonnet) are sequential by design — Haiku pre-screens before Sonnet sees candidates.

### 2026-04-01 — Watchlist Churn Analysis

Three distinct churn sources identified:
1. **Time-bounded catalyst entries with no expiry** — WRB held for 109 hours after catalyst window. Fixed by `catalyst_expiry_utc`.
2. **Data-unavailable symbols in tier-1 context** — yfinance failures produced `long_score: 0.0` entries that Claude still spent reasoning budget on. Fixed by `fetch_failure` suppression.
3. **`_regime_reset_build` overshoot** — fixed; now uses `watchlist_build_target` (config default 8) instead of 20. New entries have no TA data and were pruned on the next pass under the old value.
