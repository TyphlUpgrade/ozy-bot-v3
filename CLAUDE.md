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
Last post-MVP phase completed: Phase 18 — Watchlist Intelligence (March 23)

### Decisions from completed phases that affect active development

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

### Post-MVP Roadmap: Phases 19–21

- **Context Compression** *(historical spec, superseded — see `phases/context_compression_historical.md`)*:
  Original plan for a Haiku pre-screener. Replaced by the two-tier architecture below, which
  incorporates pre-screening as part of a broader regime-aware operator layer.

- **Phase 19 — Sonnet Strategic Output**: see `phases/19_sonnet_strategic_output.md`
  - Richer Sonnet inputs: `sector_dispersion` (watchlist symbols vs sector ETF 1w return),
    `recent_rejections` (filter kill feedback), `news_theme_synthesis`
  - New `ReasoningResult` fields: `regime_assessment`, `sector_regimes`, `filter_adjustments`,
    `active_theses` with `thesis_breaking_conditions`
  - `filter_adjustments` applied in ranker with config-floor guards; strategy trend gate yields
    to `entry_conditions` when Claude explicitly specified TA gates; regime condition expiry;
    prompt v3.10.0

- **Phase 20 — Haiku Operational Layer**: see `phases/20_haiku_operational_layer.md`
  - `ContextCompressor`: Haiku pre-screener consuming Sonnet's `regime_assessment` +
    `sector_regimes`; regime-aware candidate ranking; `needs_sonnet` escalation flag with typed
    reasons (`regime_shift`, `all_candidates_failing`, `position_thesis_breach`, `watchlist_stale`)
  - `run_reasoning_cycle` returns `(ReasoningResult, CompressorResult)`; `assemble_reasoning_context`
    accepts `selected_symbols`; `_needs_sonnet_fired` guard; candidates exhaustion trigger

- **Phase 21 — Durability and Regime Response**: see `phases/21_durability_and_regime_response.md`
  - Regime-reset watchlist build: conflict eviction on regime flip, `catalyst_driven` flag,
    `full_rebuild` path in `run_watchlist_build`
  - `_clear_directional_suppression`: clears direction-dependent session suppression after regime change
  - Regime-aware universe scanner: `day_losers` screener source for correcting sectors
  - Multi-tier pruner eviction: tier-2 first, then regime-conflicting tier-1, then composite score
  - `regime_assessment` persisted across restarts in `bot_state.json`

## Spec Drift Log
See `DRIFT_LOG.md`. Read the relevant phase section of DRIFT_LOG.md before modifying or debugging any module built in a previous phase.
