# Ozymandias v3 — Automated Trading Bot

## What This Is
An automated stock trading bot using Claude API for strategic reasoning + quantitative technical analysis for execution. Targets aggressive momentum and swing trading on high-volatility, high-liquidity equities. Alpaca paper trading initially.

## Full Spec
The complete specification is in `ozymandias_v3_spec_revised.md` at the project root. Consult relevant spec sections to resolve ambiguities. The Spec Drift Log (see below) takes precedence where it contradicts the spec.

## Architecture (Mental Model)
```
External Data (yfinance) → Orchestrator (3 async loops) → Intelligence (Claude + TA + Ranker) → Execution (Risk + Broker)
                                  ↕
                          Persistent State (JSON files)
```

- **Fast loop (5-15s):** Order fills, fill protection, quant overrides, PDT guard, position sync
- **Medium loop (1-5min):** Technical scans, signal detection, opportunity ranking, position re-eval
- **Slow loop (event-driven, checked every 5min):** Claude reasoning, watchlist management, news digest, thesis review. Only calls Claude when a trigger fires (price move, time ceiling, session transition, etc.)

## Hard Technical Constraints
- **Python 3.12+**, asyncio throughout (no threading/multiprocessing)
- **No third-party TA libraries** (no pandas-ta, no ta-lib). All indicators hand-rolled with pandas + numpy in `intelligence/technical_analysis.py`
- **Timezone:** All internal timestamps UTC. Market hours logic uses `zoneinfo.ZoneInfo("America/New_York")`. Never rely on local system clock timezone.
- **Claude JSON parsing:** Expect ~5% malformed. 4-step defensive pipeline: strip fences → json.loads → regex extract → skip cycle. Never crash on bad Claude output.
- **State files:** JSON with atomic writes (write temp file, then rename). Validate schemas on startup.
- **Broker abstraction:** All broker-specific code behind `BrokerInterface` ABC. No broker imports outside `execution/alpaca_broker.py`.

## Modularity Philosophy

This is an experimental system with no proven approach. The trading logic, signals, strategies, and
direction types will change as paper trading reveals what works. **The primary structural goal is
that any single component — a strategy, an indicator, a signal, an instrument type, a direction —
can be added or removed without requiring changes scattered across multiple modules.**

Concrete rules that enforce this:

- **Prefer lookup tables over if/elif chains** for any categorisation that might grow. A new
  direction type, strategy, or signal value should require adding one entry to one dict, not finding
  and updating every conditional that branches on it.
- **Every lookup table is a documented extension point.** Comment it: *"To add X, add one entry
  here."* If a reader can't see where to add a new case without grepping the codebase, the
  abstraction is incomplete.
- **Cross-cutting concerns live in `core/`.** Direction, market hours, config, and state are used
  by every layer. They must not be defined in `intelligence/` or `execution/` and then imported
  upward — that creates circular dependency risk and hides extension points.
- **No string-literal conventions duplicated across modules.** If two modules both check
  `action == "sell_short"`, one of them is wrong. The convention belongs in one place and
  everything else imports it.
- **Strategies and signals are independently testable.** A new TA signal added to
  `technical_analysis.py` should not require changes to `orchestrator.py` to test. A new strategy
  in `strategies/` should not require changes to `opportunity_ranker.py` to register.

When a new feature requires touching more than two modules to add a single new value to an existing
category (direction, strategy type, signal name), that is a signal the abstraction is wrong — fix
the abstraction first, then add the value.

## Key Design Rules
- Modules communicate via interfaces and JSON, never direct coupling
- Only the orchestrator knows about all other modules
- Prompt templates are versioned files in `config/prompts/`, never hardcoded
- Risk manager has override authority over everything — can cancel orders, force exits, block entries
- Fill protection: never place a new order for a symbol that has a PENDING or PARTIALLY_FILLED order
- PDT buffer: default reserve 1 of 3 allowed day trades for emergency exits
- Never hardcode tunable parameters (thresholds, multipliers, intervals, weights, limits). Put them in config.json with a comment in the code explaining what the config value controls.
- Entry limit orders use current market price (from `_latest_indicators`), never Claude's cached
  `suggested_entry`. Claude's suggested price is a reference for drift checks only.
- Claude specifies per-trade `entry_conditions` in `new_opportunities` output. The medium loop
  evaluates these against current indicator values before entering. Absent conditions = no gate.
  This is the bridge between Claude's per-ticker knowledge and TA's real-time confirmation.
- Never modify files in `phases/` or `ozymandias_v3_spec_revised.md` unless explicitly instructed.
  Phase files are historical records; spec deviations go in DRIFT_LOG.md and CLAUDE.md only.

## Testing Standards
- Every module gets unit tests. TA functions tested against hand-calculated values.
- Fill protection tested for all edge cases: partial fills, cancel-during-fill race, unexpected fills.
- Use pytest + pytest-asyncio. Mock broker and external APIs — never hit real APIs in unit tests.
- **When tests fail:** First categorize every failure — is the code wrong or is the test wrong? Fix all code bugs first, then fix broken tests in a single batch. If a test asserts implementation details (exact mock call counts, internal state shapes, specific log strings) rather than behavioral outcomes, rewrite it to test behavior. Do not fix tests one at a time to make them green.

## Workflow Rules
- **Before starting work:** Read this file. Read the current phase prompt only — not previous ones.
  Consult the spec only to resolve a specific ambiguity; don't re-read it in full.
- **Bug handling during implementation:**
  - Bug in code you're currently building → fix immediately before continuing
  - Bug in a previously built module that blocks you → fix it
  - Bug in a previously built module that doesn't block you → add a `# BUG: description` comment and continue
  - Never add broad `try/except` or unnecessary `None` checks to work around bugs in other modules
- **Updating this file:** Update CLAUDE.md (especially the Spec Drift Log) when: a module's interface
  changes from what the phase file specified, a dependency or assumption is discovered that affects
  future phases, or a meaningful architectural decision is made. Do not modify phase files or the
  spec file — document deviations here instead.
- **Never modify phase documents or the spec file.** `phases/` files and `ozymandias_v3_spec_revised.md`
  are immutable historical records. All deviations, decisions, and post-MVP additions belong in
  DRIFT_LOG.md and CLAUDE.md only. Only create new phase files when explicitly instructed to do so.

## Adding Features Beyond the Spec
Features not described in the spec may be requested. When implementing them, follow existing patterns:
- New strategies → implement the `Strategy` ABC in `strategies/`
- New Claude calls → new method in `claude_reasoning.py` + new prompt template in `config/prompts/`
- New persistent state → extend existing JSON schemas in `state/`, don't create new files
- New loop logic → integrate into the orchestrator's existing loop structure
- All new code uses `async`, `get_logger()`, and `StateManager`
- New Claude output fields (like `entry_conditions`) → add to prompt schema in `config/prompts/vN.N.N/reasoning.txt`, add to `ReasoningResult` or carry through `ScoredOpportunity`, add evaluator function in `intelligence/opportunity_ranker.py`

## Post-MVP Status

All 10 spec phases complete per `ozymandias_v3_spec_revised.md`. Post-MVP phases (11–18) complete.
Full narrative history in `COMPLETED_PHASES.md`.

Last spec phase completed: Phase 10 (March 15)
Last post-MVP phase completed: Phase 21 — Durability and Regime Response (March 27)

### Decisions from completed phases that affect active development

- **Directional TA scoring** (`compute_directional_scores`): `composite_technical_score` is fully
  removed. All code uses `long_score`/`short_score` from `compute_directional_scores(intraday, daily)`.
  The `_latest_indicators` cache is flat — both scores stored at the top level, no `"signals"` sub-key.
  `ScoredOpportunity.composite_score` is a *different concept* (ranker weighted output: conviction ×
  0.35 + tech × 0.30 + rar × 0.20 + liq × 0.15) and is correct/intentional — do not confuse the two.
  Claude cannot adjust the ranker's `min_composite_score` floor via `filter_adjustments` (it never
  sees the scores). The only Claude-adjustable filter parameter is `min_rvol`.

- **Position sizing** (`_medium_try_entry`): Claude's `position_size_pct` is the primary sizing
  target (`equity × pct / price`), clamped by `max_position_pct`. The ATR formula applies as a
  risk cap on top — it does not drive size. Do not revert to ATR-first sizing.

- **Stop adjustment guard** (`_apply_position_reviews`): must reject any `adjusted_targets.stop_loss`
  that sits on the wrong side of current price (above price for long, below for short). This
  prevents Claude from forcing an immediate exit by over-tightening. Triggered by XOM incident
  (stop raised to $162, price $161.25). Log WARNING with rejected value.

- **Swing intraday gate removal**: the `apply_entry_gate` trend_structure block and the
  `evaluate_position` / `suggest_exit` bearish_aligned exit checks are intentionally commented out
  — intraday timeframe is wrong for swing reviews. The commented code is preserved for future
  re-entry/repositioning logic. Do not delete it or restore it as an active gate.

### Post-MVP Completed Work

See `COMPLETED_PHASES.md`. When completing a phase or named feature session, document it there — not here. CLAUDE.md is for active conventions that affect future development, not history.

## Reference Documents

Four documents together form the complete knowledge base. Each has a distinct purpose — read the right one for the job:

### `COMPLETED_PHASES.md` — What was built and why
Phase-level narrative history. Answers: *"What did this phase introduce? What are the key methods, fields, and behaviors it added? What constraints must I respect when touching this area?"* One entry per phase or named feature session. Written for someone who is about to work in that area and needs a mental model of what exists and why.

- **Read before:** modifying a module built in a post-MVP phase, or when you need to understand the intent behind an existing feature.
- **Update when:** a post-MVP phase or named feature session completes.
- **Does not belong here:** individual method signatures, interface deviations, or per-change rationale — those go in DRIFT_LOG.

### `DRIFT_LOG.md` — How specific changes deviate from the spec
Change-level technical record. Answers: *"How does this method's signature or behavior differ from what the spec said? What is the exact interface contract? What edge case was decided during implementation?"* One entry per non-obvious deviation, keyed to a file and spec section. Written for someone debugging unexpected behavior or verifying a method's contract.

- **Read before:** modifying or debugging any method whose behavior might differ from a naive spec reading — especially signatures, return types, and behavioral edge cases.
- **Update when:** a change would not be fully explained by reading the code and commit message together — a behavioral contract change, a non-obvious trade-off, or a decision whose *why* isn't visible in the implementation. Update the File Index when adding an entry.
- **Does not belong here:** phase narratives, session summaries, or anything a reader would immediately understand from the code alone. Signal-to-noise ratio is the drift log's value.

### `NOTES.md` — Open concerns and engineering analyses
Living register of open problems and the reasoning behind past architectural decisions. Answers: *"Is there a known issue in this area? Has this problem been analyzed before?"*

- **Read before:** starting work on any component with an open concern, or planning a new architectural change.
- **Update when:** a lasting concern surfaces (open it) or resolves (mark resolved; delete next session — DRIFT_LOG owns the permanent record).
- **Does not belong here:** resolved concerns older than one session, session logs, or content that belongs in DRIFT_LOG or CLAUDE.md.

### `CLAUDE.md` (this file) — Active conventions for all future development
The rules that apply right now, every session. Answers: *"What must I know before touching this codebase?"* Covers architecture, hard constraints, design rules, and decisions from past phases that still govern live code.

- **Update when:** a convention changes, a new constraint is established, or a past phase decision continues to govern how new code must be written.
- **Does not belong here:** history, resolved issues, or anything captured more precisely in the other three documents.
