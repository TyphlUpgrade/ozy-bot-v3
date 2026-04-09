---
title: Ozymandias Completed Phases (Post-MVP)
tags: [ozymandias, phases, completed, trading-bot, post-mvp]
category: reference
created: 2026-04-09
updated: 2026-04-09
---

# Completed Post-MVP Work — Ozymandias v3 (continued)

Post-MVP phases: paper session fixes (2026-04-06), orchestrator extraction, and agentic development workflow.
For earlier phases see [[ozy-completed-phases]].

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


---

## Cross-References

- [[ozy-completed-phases]] — Phases 11-18 and earlier paper session fixes
- [[ozy-drift-log]] — Active drift log
- [[ozy-doc-index]] — Full routing table
