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

Last spec phase completed: Phase 10 (March 15)
Last post-MVP phase completed: Phase 18 — Watchlist Intelligence (March 23)

### Completed post-MVP work
- **Anti-bias hardening** (March 15): conviction sanity floor, `rejected_opportunities` logging,
  adversarial `updated_reasoning` prompts, thesis challenge for large positions
- **Paper session bug fixes** (March 16): 9 bugs fixed — re-adoption runaway, Claude exits ignored,
  short direction inference, fill dispatch routing, ghost cleanup race, test isolation,
  cash sync, exit price fallback, PDT broker floor (see BUGS_2026-03-16.md)
- **Index ticker blacklist + dead field cleanup** (March 16): Anomalies 10 + 12 fixed
- **Phase 11 — Execution Fidelity** (March 16): see `phases/11_execution_fidelity.md`
- **Phase 12 — Direction Unification** (March 17): canonical `Direction` type + all cross-convention
  mappings in `core/direction.py`; geometry-agnostic RAR, direction-aware VWAP/trend filters,
  direction-aware `compute_composite_score` — shorts now score correctly against bearish signals
- **Phase 13 — Strategy Modularity** (March 17): `is_intraday`/`uses_market_orders`/`blocks_eod_entries`
  traits + `apply_entry_gate()` abstract method on Strategy ABC; `_build_strategies()` uses registry;
  `StrategyConfig.strategy_params: dict[str,dict]` replaces per-strategy fields;
  adding a new strategy now requires only 2 files
- **Phase 14 — Claude-Directed Entry Conditions** (March 18): `entry_conditions` field in Claude
  prompt and `ScoredOpportunity`; `evaluate_entry_conditions()` in ranker; medium loop defers
  entries when live signals fail Claude's per-trade TA gates; 6 new condition keys for shorts
- **Phase 16 — Pattern Signal Layer + Short Protection** (March 18): 5 new TA signals
  (`rsi_slope_5`, `roc_negative_deceleration`, `macd_histogram_expanding`, `bb_squeeze`,
  `volume_trend_bars`); slope-aware RSI gate in `apply_entry_gate` (direction-aware, live path);
  fast-loop short exits (ATR trailing stop + VWAP crossover + hard stop);
  EOD forced close for momentum shorts; `_recently_closed` persisted; ATR position size cap
- **Post-Phase-16 paper trading fixes** (March 19):
  - *Token budget*: `_TOTAL_TOKEN_BUDGET = 25_000`; template size subtracted at runtime so Claude
    sees full 26-symbol watchlist instead of being trimmed to ~7 symbols
  - *Entry defer expiry*: `_entry_defer_counts` on orchestrator; stale gates expire after
    `max_entry_defer_cycles` (default 5) consecutive misses — prevents AMD-frozen-RSI deadlock
  - *Composite score floor*: `min_composite_score: float = 0.45` in `RankerConfig`; checked in
    `_medium_try_entry` before sizing to block degenerate multi-component-weak entries
  - *Position review logging*: `_apply_position_reviews` logs each review action at INFO for audit
  - *`position_in_profit` slow-loop trigger*: fires at `position_profit_trigger_pct` (1.5%) gain
    intervals; re-arms each interval; direction-aware for shorts; stored in
    `SlowLoopTriggerState.last_profit_trigger_gain`; cleared on close
  - *Prompt profit protection*: reasoning.txt instruction 1 updated to prompt Claude to consider
    stop-tightening when thesis milestones are reached (neutral framing, not mechanical)
  - *Trade journal versioning*: `prompt_version` and `bot_version` appended to every journal entry;
    old entries archived to `state/trade_journal_archive_pre_v3.4.jsonl`
  - *Position sizing fix*: `_medium_try_entry` now uses Claude's `position_size_pct` as the primary
    sizing target (`equity × pct / price`), clamped by `max_position_pct`. Previously the ATR
    formula ignored `position_size_pct` entirely, always sizing near the 20% cap regardless of
    conviction. TA scale factor and ATR risk cap still apply on top.
- **Operational hardening** (March 20):
  - *RSI entry floor*: `rsi_entry_min: 60` in `strategy_params.momentum`; momentum long entries
    blocked when RSI < 60. Raised from 45 based on backtest: RSI 45–55 → 17% win rate vs RSI ≥65 → 37%.
  - *Trade journal lifecycle*: `record_type` field (`open`/`snapshot`/`review`/`close`) on all
    journal entries; `trade_id` UUID generated at fill time and shared across all records for a
    trade; adoption path and startup restore now also generate a UUID so restarted positions link
    their future records. `position_size_pct` and `claude_conviction` written to `open` records.
  - *Session-based logging*: `core/logger.py` rewritten — each startup creates
    `logs/session_YYYY-MM-DDTHH-MM-SSZ.log`; `current.log` symlink always points to active file;
    `max_session_logs` param (default 0 = unlimited, never auto-deletes in production).
  - *Graceful degradation on startup failure*: credential load, broker connect, and market hours
    fetch each wrapped with CRITICAL log before re-raise; `main.py` catches unhandled exceptions
    and exits cleanly with `FATAL: ...` on stderr instead of a raw Python traceback.
  - *Stop adjustment guard*: `_apply_position_reviews` rejects any `adjusted_targets.stop_loss`
    that would sit on the wrong side of current price (above price for long, below for short),
    preventing Claude from forcing an immediate exit by over-tightening. Triggered by XOM incident
    where stop was raised to $162 while price was $161.25. WARNING logged with rejected value.
  - *Emergency exit/shutdown commands*: signal files `state/EMERGENCY_EXIT` and
    `state/EMERGENCY_SHUTDOWN` checked at top of every fast-loop tick. Emergency exit: cancels
    all pending orders, places market exits for all positions, polls broker every 2s for 60s to
    confirm fills, logs CRITICAL if any remain open. Trigger via `python -m ozymandias.scripts.emergency exit|shutdown`
    or `touch state/EMERGENCY_EXIT`. Designed for future Discord integration.

- **Phase 15 — Context Enrichment** (March 20): `RankResult` wraps ranker output with rejections;
  `_recommendation_outcomes` tracker in orchestrator; `WatchlistEntry.expected_direction`;
  `ta_readiness` dict replaces `technical_summary` string in tier-1 context (direction-adjusted
  composite score); `TradeJournal.load_recent` + `compute_session_stats`; `recent_executions` and
  `execution_stats` passed to Claude each cycle; prompt v3.5.0; 50 new tests.

- **Phase 17 — Trigger Responsiveness & Data Freshness** (March 23): parallel medium loop fetch
  with `asyncio.Semaphore(medium_loop_scan_concurrency=10)`; `_all_indicators` merged dict;
  `_last_medium_loop_completed_utc` gate so slow loop only calls Claude after fresh data;
  bidirectional macro/sector triggers (`market_move:SPY/QQQ/IWM`, `sector_move:<etf>`,
  `market_rsi_extreme` for panic + euphoria); `_SECTOR_MAP` + `_CONTEXT_SECTOR_ETFS` extension
  points; `last_claude_call_prices` baseline in `SlowLoopTriggerState`; adaptive reasoning cache
  TTL (`cache_max_age_panic_min=10` when SPY RSI < 25, `cache_max_age_stressed_min=20` < 30).

- **Post-Phase-17 hardening** (March 23): `_apply_position_reviews` stale-portfolio race fix
  (loads fresh snapshot internally); `"sell_short"` → `"short"` direction normalization on state
  load; session-level filter suppression (`_filter_suppressed: dict[str, str]` — symbols that fail
  hard filters `max_filter_rejection_cycles` times are blocked + shown to Claude as suppressed).

- **Phase 18 — Watchlist Intelligence** (March 23): `UniverseFetcher` — Yahoo Finance screener
  (`most_actives` + `day_gainers`) + Wikipedia S&P 500/Nasdaq 100 (24h cache); `UniverseScanner`
  — RVOL filter with OR-gate price-move path (`min_price_move_pct_for_candidate=1.5%`), earnings
  calendar, news enrichment, sort by RVOL descending; `SearchAdapter` — Brave Search API with 429
  retry, graceful degradation when no key; `call_claude_with_tools` — multi-turn tool-use loop in
  `claude_reasoning.py` with forced final call on round exhaustion; `run_watchlist_build` updated
  with `candidates` + `search_adapter` params; `scripts/reset_watchlist.py` CLI tool; prompt v3.6.0.

- **2026-03-24 paper session fixes**: strategy-specific limit order timeout
  (`swing_limit_order_timeout_sec=1200`, momentum keeps 300s); Brave Search 429 retry
  (`search_429_retry_count=2`, `search_429_retry_sec=5.0`).

- **2026-03-24 observability**: no-opportunity streak WARN at configurable threshold (default 8)
  with gate-breakdown summary; `record_type="rejected"` journal entries for conviction-floor and
  composite-score-floor rejections for cross-session calibration.

- **2026-03-25 paper session fixes**:
  - *Defer expiry suppression*: on hitting `max_entry_defer_cycles`, symbol now added to
    `_filter_suppressed` (session suppression) instead of clearing `top.entry_conditions` — the
    prior approach was a no-op because `top` is rebuilt from the reasoning cache each medium loop.
  - *Dead zone exempt for swing*: `dead_zone_exempt` property on `Strategy` ABC (default `False`);
    `SwingStrategy.dead_zone_exempt = True`; `validate_entry` + `_check_market_hours` in
    `risk_manager.py` accept and enforce the flag. Swing theses are multi-day and unaffected by
    the noon lull the dead zone was designed for.
  - *Universe scanner OR-gate*: RVOL path OR price-move path (abs(roc_5) ≥
    `min_price_move_pct_for_candidate`). Direction-agnostic — captures shorts and low-float movers
    that have elevated moves without proportionally elevated RVOL.
  - *Watchlist build re-fire suppression*: on build failure, `last_watchlist_build_utc` back-dated
    so `watchlist_stale` re-fires after `circuit_breaker_probe_min` minutes, not every 60s.
  - *Prompt v3.8.0*: clean version boundary (content unchanged from v3.7.0 — reserved for rsi_max
    swing instruction pending further discussion).

- **2026-03-27 — Two-Profile Indicator Layer (daily TA for swing reviews)**:
  - *Root cause fixed*: intraday SPY RSI (5-min bars) was driving swing position exits ("macro panic
    exits"). SPY RSI 35 intraday was triggering Claude to exit multi-day swing positions.
  - *`generate_daily_signal_summary(symbol, df)`* added to `technical_analysis.py`: computes
    `rsi_14d`, `price_vs_ema20`, `price_vs_ema50`, `ema20_vs_ema50`, `daily_trend`
    (uptrend/downtrend/mixed), `roc_5d`, `volume_trend_daily`, `macd_signal_daily` from daily bars.
    Returns `{}` for < 20 bars.
  - *`_daily_indicators: dict[str, dict]`* on orchestrator: populated each slow loop for SPY, QQQ,
    and all open swing position symbols. Fetch failures logged at WARNING (not silently dropped).
  - *`spy_daily` / `qqq_daily`* added to `_build_market_context` output for macro regime context.
  - *`daily_signals` block* injected into swing position context in `assemble_reasoning_context`.
    Momentum positions receive no `daily_signals` block — intraday is correct for them.
  - *Prompt v3.9.0*: `TWO-PROFILE MARKET CONTEXT` section; `OPEN POSITIONS — DAILY SIGNALS`
    section; swing entry restrictions in daily downtrend (`daily_trend == "downtrend"` → near-
    prohibited swing longs, require catalyst_driven + conviction ≥ 0.70); `review.txt` updated to
    instruct Claude to weight `daily_signals` over intraday context for swing reviews; `watchlist.txt`
    updated with `spy_daily.daily_trend` calibration instructions.
  - *Swing intraday gate removal*: `apply_entry_gate` trend_structure block commented out (wrong
    timeframe). `evaluate_position` and `suggest_exit` bearish_aligned exits commented out (bypassed
    4h hold guard via medium loop). Code preserved, not deleted, for future re-entry/repositioning
    logic. 13 new tests in `test_technical_analysis.py`. 3 test renames in `test_strategies.py`,
    `test_strategy_traits.py`, `test_opportunity_ranker.py`.

- **2026-03-27 — Watchlist pipeline fixes**:
  - *`tier1_max_symbols` raised 8 → 18*: with 3 open positions, Claude was seeing only 5 candidates
    per cycle out of 33 tier-1 symbols (15%). Now sees ~15 candidates (45%).
  - *`watchlist_max_entries` raised 40 → 60*: the size-cap pruner was evicting ~20 symbols every
    watchlist build because `target_count=20` additions + 40 cap = 20 forced evictions. Pruner used
    intraday composite score as eviction criterion — a category error for swing setups with low
    intraday scores by design (e.g., POOL RSI 29 oversold thesis).
  - *`watchlist_build_target` config key added (default 8)*: replaces hardcoded `target_count=20`
    in `run_watchlist_build`. Forces Claude to add ≤ 8 selective picks per build rather than a
    wholesale refresh. Wired through `ClaudeConfig` and orchestrator.
  - *Watchlist prompt framing fixed*: "TARGET WATCHLIST SIZE: N" → "ADD UP TO N NEW TICKERS THIS
    BUILD" — disambiguates additive vs. rebuild semantics.

### Post-MVP Roadmap: Phase 19

- **Phase 19 — Context Compression**: see `phases/19_context_compression.md`
  - Haiku pre-screener ranks all watchlist candidates before strategic reasoning context assembly;
    most valuable now that Phase 18 populates a large, diverse tier1/tier2 candidate pool

## Spec Drift Log
See `DRIFT_LOG.md`. Read the relevant phase section of DRIFT_LOG.md before modifying or debugging any module built in a previous phase.
