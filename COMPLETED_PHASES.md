# Completed Post-MVP Work — Ozymandias v3

Narrative history of completed phases and paper session fixes. For per-module interface changes
consult DRIFT_LOG.md. For active design rules and constraints see CLAUDE.md.

**Read before:** modifying a module built in a post-MVP phase, or when trying to understand why a
feature works the way it does. Start here for the "what was built"; go to DRIFT_LOG for the "how
it deviated from the plan."

**Update when:** a post-MVP phase or named feature session completes. Document here, not in
CLAUDE.md. One entry per phase/feature with bullet points covering the key implementation
decisions, new methods/fields, and any non-obvious constraints future work must respect.

---

## Phases 11–18

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

---

## Paper Session Fixes (post-Phase-18)

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

- **Post-Phase 21 — Watchlist Build Decoupled from Reasoning Cycle** *(2026-04-01)*
  - `_run_watchlist_build_task()`: new background method (fire-and-forget via `asyncio.ensure_future`
    from `_slow_loop_cycle`). Owns universe scan, `run_watchlist_build`, `_apply_watchlist_changes`,
    `last_watchlist_build_utc` update, and failure back-date logic.
  - `_watchlist_build_in_flight: bool`: guard separate from `claude_call_in_flight`. A running build
    never blocks the next reasoning cycle.
  - `watchlist_changes.add` removed from reasoning output: new symbols added exclusively through the build task.
  - Watchlist build section removed from `_run_claude_cycle`. `_run_claude_cycle` now receives only
    reasoning triggers and runs Call A + Call B with no watchlist mutation.

- **Post-Phase 23 — Watchlist/Reasoning Full Separation + Dead Zone Rework** *(2026-04-02)*
  - Reasoning fully read-only on watchlist: `watchlist_changes.remove` also removed from reasoning output. Removes now exclusively in `_run_watchlist_build_task` via `WatchlistResult.removes`. Build sees `expected_direction` per entry (format: `SYMBOL(dir:long,tier:1)`) and has explicit instructions to remove and re-add direction-conflicting entries.
  - `candidates_exhausted` rerouted to build trigger; `_reasoning_needed_after_build` flag coordinates post-build reasoning; `_post_build_reasoning` fires after successful build adds candidates.
  - Parse failure retries in 3 min (`watchlist_build_parse_failure_retry_min`); API failure keeps 10 min.
  - `require_watchlist_before_reasoning: bool` config — defers reasoning until build completes on co-fire.
  - Dead zone suppression fixes: ranker rejections and entry condition defer counts no longer accumulate during dead zone. `SwingStrategy.dead_zone_exempt` changed `True` → `False`.
  - RVOL-conditional dead zone bypass: `_dead_zone_rvol_bypass(symbol=None)` — two-tier predicate. Tier 1: SPY RVOL ≥ 1.5 lifts block globally. Tier 2: individual symbol RVOL ≥ 2.0 lifts block for that entry only. `risk_manager.py` untouched — bypass via existing `dead_zone_exempt` param on `validate_entry`. Config: `dead_zone_rvol_bypass_enabled`, `dead_zone_rvol_bypass_threshold`, `dead_zone_symbol_rvol_bypass_threshold`.
  - Prompt: `rsi_slope_5`/`rsi_accel_3` indicator reference — units, noise floors, typical ranges, calibration instruction against live `ta_readiness` values.
  - CONCERN-1 resolved: no-opportunity streak WARN now distinguishes zero-Claude-output vs. ranker-blocking all candidates.

---

## Paper Session Fixes (2026-04-06)

- **Entry condition feedback + RVOL cage + approaching_close one-shot** *(214a3a0)*
  - *last_block_reason surfaced*: `conditions_waiting` stage_detail now includes the specific
    reason the last entry condition check failed, so Claude receives a diagnostic string on the
    next reasoning cycle rather than a silent defer count.
  - *RVOL cage clamp*: `filter_adjustments.min_rvol` can only lower the strategy floor, not raise
    it above the configured default. Prevents Claude from creating a self-reinforcing cage based on
    a 0/N short streak from a statistically meaningless sample.
  - *approaching_close one-shot*: `approaching_close_fired` flag on `SlowLoopTriggerState` prevents
    the trigger from firing on every tick within the 4-minute window (15:28–15:32 ET). Cleared on
    `session_open` and `session_close` transitions.
  - *execution_stats_min_trades 3→5*: 0/3 streak was triggering global filter changes.
  - *Strategy field in rejections*: ranker rejection outcomes now include `strategy` for richer logs.

- **Prompt v3.10.2 + RSI calibration error detector** *(fb25d25)*
  - *RSI calibration error detection*: `evaluate_entry_conditions` gains
    `entry_condition_rsi_level_tolerance` (default 5.0, config-driven). When `rsi_max` is set
    >5pts below current RSI, returns `rsi_max_calibration_error` with diagnostic suggesting
    `rsi_slope_max` instead. Symmetric `rsi_min_calibration_error` for long miscalibrations.
  - Prompt additions: ta_readiness echo before entry_conditions (forces write-time verification),
    pre-entry thesis/news cross-check (ALB class), short entries with long_score > short_score
    by >0.10 must name overriding catalyst, sector regime flips without catalyst flagged as noise.

- **Slow loop over-triggering — regime cooldown, thesis breach suppression** *(b19ef39)*
  - *regime_condition cooldown*: 20-min cooldown via `last_regime_condition_utc` on
    `SlowLoopTriggerState`; suppressed in `_check_triggers` if within
    `regime_condition_cooldown_min`. Prevents chain-fire from rapid regime oscillation.
  - *thesis breach double-review suppression*: `_last_position_review_utc` dict stamped after every
    Sonnet position review; thesis breach scheduling gate suppresses Sonnet call if symbol reviewed
    within `thesis_breach_review_cooldown_min` (15 min). Haiku check is cheap — suppression is at
    Sonnet scheduling only.
  - Prompt v3.10.3: `valid_until_conditions` must state current signal value before each condition
    with explicit example of immediate-fire failure mode. `thesis_breaking_conditions` must be
    verifiable events, not analyst previews.

---

## Orchestrator Extraction — Phase 1 *(82f345c, 2026-04-06)*

Reduces `orchestrator.py` from 5393 to 4559 lines (-15.5%) by extracting three zero-write-coupling
method groups into independent modules. All existing call sites preserved via thin delegation
wrappers. Verbatim method moves — no behavioral changes.

- **`core/trigger_engine.py`** (613 lines): `SlowLoopTriggerState` dataclass + `_check_triggers` +
  `_check_regime_conditions` + `_update_trigger_prices`. Pure evaluation logic — reads state,
  returns trigger list, mutates only its own dataclass. Orchestrator holds the engine instance
  and calls `engine.check_triggers(now, ...)`.

- **`core/market_context.py`** (202 lines): `MarketContextBuilder` — `_build_market_context`
  extracted as a stateless builder. Takes account/PDT/indicators/session as inputs, returns dict.
  `_recommendation_outcomes` passed as parameter (read-only by this module).

- **`core/fill_handler.py`** (308 lines): `FillHandler` — `_dispatch_confirmed_fill` +
  `_register_opening_fill` + `_journal_closed_trade`. Mutable shared state (`entry_contexts`,
  `recently_closed`, `position_entry_times`, `intraday_highs`, `intraday_lows`, `override_closed`,
  `pending_exit_hints`) passed by reference at construction time. Safe because all loops run in a
  single asyncio event loop.

- **Async lifecycle fix**: `asyncio.ensure_future()` → `_spawn_background_task()` with proper task
  tracking set + cancellation on shutdown. Eliminates fire-and-forget coroutine GC warnings.

- **Other fixes bundled in this commit**: `SwingStrategy.dead_zone_exempt` restored to `True`,
  entry condition auto-correction, stop distance clamping, cache token logging, prompt cache
  restructure.

## Orchestrator Extraction — Phase 2 *(uncommitted, 2026-04-06)*

Reduces `orchestrator.py` from 4559 to 3605 lines (-21%, cumulative -33% from original 5393).
Four modules extracted using the same patterns established in Phase 1: verbatim method move,
dependency injection via mutable shared references, thin delegation wrapper on orchestrator.

- **`core/quant_overrides.py`** (241 lines): `QuantOverrides` — `_fast_step_quant_overrides` +
  `_place_override_exit`. `step(latest_indicators)` returns exit count; `latest_indicators` passed
  at call time (not stored) because tests commonly reassign `orch._latest_indicators` which breaks
  stored references. Instantiated in `_startup()` (not `__init__`) because `_risk_manager` is None
  at construction time.

- **`core/position_sync.py`** (262 lines): `PositionSync` — `_fast_step_position_sync`. Handles
  ghost locals (external close), untracked broker positions (adoption), quantity mismatches.
  `step(broker, latest_indicators)` — both passed at call time for the same reference-breaking
  reason as QuantOverrides. Uses `on_broker_failure`/`on_broker_available` callbacks.

- **`core/position_manager.py`** (398 lines): `PositionManager` — `_medium_evaluate_positions` +
  `_apply_position_reviews`. Routes position evaluation to the strategy that opened the position.
  Handles EOD forced close for momentum shorts, stop adjustment guard, Claude exit recommendations.
  `broker` and `latest_indicators` passed at call time.

- **`core/watchlist_manager.py`** (476 lines): `WatchlistManager` — 5 methods extracted:
  `_clear_directional_suppression`, `_regime_reset_build`, `_run_watchlist_build_task`,
  `_prune_expired_catalysts`, `_apply_watchlist_changes`. Owns build guard flags:
  `build_in_flight`, `reasoning_needed_after_build`, `last_universe_scan`,
  `last_universe_scan_time`. Property proxies on orchestrator maintain backward compatibility.

- **Reconciliation intentionally skipped**: too many orchestrator scalar writes
  (`_last_known_equity`, `_conservative_mode_until`, `_last_regime_assessment`,
  `_prior_regime_name`, `_last_sector_regimes`, `_trigger_state.*`).

- **Key pattern: runtime parameters vs stored references**: When tests commonly reassign an
  orchestrator attribute (creating a new object rather than mutating the existing dict), the
  extracted module receives that value at call time, not at construction time. This is the
  "runtime parameter" pattern that emerged as the solution to test fixture rebinding.

---

## Agentic Development Workflow (2026-04-08)

- **Phases 22-28 — v4 Agentic Workflow** *(phases/22-28, config/agent_roles/, tools/)*

  Built a multi-agent development pipeline using Claude Code instances coordinated by a
  deterministic bash wrapper. Seven phases implementing: signal file bus, clawhip event
  routing, Discord companion, conductor wrapper, and 7 agent role definitions.

  **Trading code changes** (the parts that affect the running bot):
  - `core/signals.py`: Signal file bus utility with atomic JSON writes. 6 writer functions,
    inbound signal check/consume, directory setup.
  - `core/orchestrator.py`: `write_status()` in fast loop, inbound signal checks
    (`PAUSE_ENTRIES`, `FORCE_REASONING`, `FORCE_BUILD`), `write_last_review()` after position
    reviews, three alert emitters (equity drawdown >2%, broker error, loop stall >60s).
    New state fields: `_entries_paused`, `_force_reasoning`, `_force_build`,
    `_session_start_equity`, `_drawdown_alert_fired`.
  - `core/fill_handler.py`: `write_last_trade()` call in `dispatch_confirmed_fill()`.

  **Development infrastructure** (does not affect the running bot):
  - `tools/conductor.sh` (~140 lines): Deterministic bash wrapper. Polls for tasks,
    invokes `claude -p` for judgment, manages tmux agent sessions.
  - `tools/start_conductor.sh` (~45 lines): Outer restart loop with exit intent dispatch.
  - `tools/discord_companion.py` (~180 lines): Standalone Discord command handler. 8
    commands, intent filter. No imports from `ozymandias/`.
  - `clawhip.toml`: Event routing config — workspace + git monitors, 6 routes to Discord.
  - 7 role files in `config/agent_roles/`: conductor, executor, architect, reviewer,
    ops_monitor, dialogue, strategy_analyst.

  **Full architecture and operational details:** See `docs/agentic-workflow.md`.
  **91 new tests** across 7 test files; 159 orchestrator regression tests unaffected.
