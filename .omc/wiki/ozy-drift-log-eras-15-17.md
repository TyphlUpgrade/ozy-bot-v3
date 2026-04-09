---
title: "Ozy Drift Log — Eras 15-17"
tags: [ozymandias, drift-log, archive, phases-15-17, entry-conditions]
category: reference
ceiling_override: frozen-archive
frozen: true
created: 2026-04-09
updated: 2026-04-09
---

# Ozy Drift Log — Eras 15-17 (Entry Conditions & Enrichment)

Frozen archive of spec deviations from Phases 15-17, Post-Phase-16 fixes, Operational Hardening, and Direction-Aware Quant Overrides.
For the active drift log and filing rules, see [[ozy-drift-log]].

---

### Phase 16 Scope Expansion + Phase 15/16 Order Swap (March 18)

**Phase 16 renamed and expanded: Pattern Signal Layer + Short Position Protection** · `phases/16_short_protection.md`, `phases/15_context_enrichment.md`, `CLAUDE.md`
- **Impl:** Phase 16 renamed from "Short Position Protection" to "Pattern Signal Layer + Short Position Protection". Five new TA pattern signals added to Phase 16 scope (all computed in `generate_signal_summary`, all flow automatically into `_latest_indicators` and strategy gates):
  1. `roc_negative_deceleration` (bool) — symmetric counterpart to `roc_deceleration`; fires when ROC is negative on two consecutive bars and magnitude is shrinking. Used by `compute_composite_score` for shorts (replaces `roc_deceleration` in the direction-resolved decel penalty).
  2. `rsi_slope_5` (float) — RSI velocity over five bars (current RSI minus RSI five bars ago). Positive = climbing, negative = falling. Defaults to 0.0 when fewer than six values available. Provides small composite score bonus when RSI is in the extended zone with slope aligned to direction.
  3. `macd_histogram_expanding` (bool) — true when histogram absolute value grew bar-over-bar with unchanged sign (same-direction momentum building, not a zero-crossing). Provides directional composite score bonus/penalty.
  4. `bb_squeeze` (bool) — true when current Bollinger Band width is at or near its 20-bar minimum (≤ 5% tolerance above minimum). Signals price coiling before breakout. Does NOT affect composite score — context for strategies and Claude's entry_conditions only.
  5. `volume_trend_bars` (int 0–5) — count of consecutive recent bars with increasing volume. Does NOT affect composite score — accumulation pattern context for Claude and strategy gates.
- **Slope-aware RSI momentum gate:** `MomentumStrategy._evaluate_entry_conditions` RSI check replaced with three-zone logic: normal zone (45–65, always pass), extended zone (65–78, pass only when `rsi_slope_5 >= rsi_slope_threshold`), hard ceiling (> 78, always blocked). Two new config keys: `rsi_max_absolute: 78`, `rsi_slope_threshold: 2.0`.
- **Why (INTC case):** RSI 73 with a rising slope was blocked by `rsi_entry_max: 65`. The gate was designed to avoid exhausted tops but cannot distinguish RSI 73 climbing from 55 (momentum acceleration) vs RSI 73 falling from 85 (exhaustion). The slope-aware gate resolves the INTC case without removing the protection.

**Phase 15/16 implementation order swap** · `phases/15_context_enrichment.md`, `phases/16_short_protection.md`, `CLAUDE.md`
- **Impl:** Phase 16 must be implemented before Phase 15. Phase 15's `ta_readiness` section is a direct pass-through of `indicators[symbol]["signals"]` — all five new Phase 16 signals appear in Claude's context automatically when Phase 15 is implemented, with no additional mapping required in Phase 15. Phase files and CLAUDE.md updated to document this dependency and ordering.
- **Why:** Zero-cost signal propagation path discovered during Phase 16 scope analysis. Implementing 16 first means Phase 15 gets all five new pattern signals in `ta_readiness` for free, making the implementation order strictly correct rather than arbitrary.

---

### Phase 16 Implementation (March 18)

**Five new TA pattern signals** · `intelligence/technical_analysis.py`
- **Impl:** Added to `generate_signal_summary()` (all flow automatically into `_latest_indicators`, strategies, and `ta_readiness`):
  - `roc_negative_deceleration` (bool): ROC negative on both bars, magnitude shrinking.
  - `rsi_slope_5` (float): `rsi[-1] - rsi[-6]` (5-bar RSI velocity). Defaults to 0.0 when fewer than 6 RSI values available.
  - `macd_histogram_expanding` (bool): histogram absolute value grew bar-over-bar with unchanged sign. Zero-crossings excluded.
  - `bb_squeeze` (bool): current Bollinger Band width ≤ 105% of 20-bar minimum (price coiling).
  - `volume_trend_bars` (int 0–5): consecutive recent bars with increasing volume (accumulation pattern).
- **Score adjustments in `compute_composite_score`:** Direction-resolved ROC decel (shorts use `roc_negative_deceleration`); RSI slope bonus (+0.05 when RSI in extended zone 65–78 with slope ≥ 3.0 aligned to direction); MACD histogram modifier (±0.03 when MACD is directionally favorable and histogram expanding/contracting). `bb_squeeze` and `volume_trend_bars` do not affect composite score — context only.
- **Tests:** 32 new tests in `ozymandias/tests/test_ta_pattern_signals.py`. 5 existing tests in `test_technical_analysis.py` updated for new score values (±0.03 MACD modifier, `macd_histogram_expanding: True` added to `_bullish_signals` / `_bearish_signals` helpers).

**Slope-aware RSI gate in `MomentumStrategy`** · `strategies/momentum_strategy.py`
- **Impl:** `_evaluate_entry_conditions` RSI check replaced with three-zone logic. New config keys `rsi_max_absolute: 78` and `rsi_slope_threshold: 2.0` added to `_DEFAULT_PARAMS` and `config.json`. Zone boundaries: normal (45–65, always pass), extended (65–78, requires `rsi_slope_5 >= rsi_slope_threshold`), hard ceiling (> 78, always blocked).
- **Why:** RSI 73 with rising slope was blocked by the static `rsi_entry_max: 65`. The gate can't distinguish momentum acceleration (RSI 55→73 climbing) from late exhaustion (RSI 85→73 falling). Slope-aware gate fixes the INTC-class false rejection without removing the exhaustion block.

**RSI slope gate in `SwingStrategy`** · `strategies/swing_strategy.py`
- **Impl:** Replaced 2-bar `rsi_turning` check (`rsi[-1] > rsi[-3]`, computed from raw bars) with `rsi_slope_5 >= rsi_slope_min_for_entry (0.5)`. Removed raw-bar RSI computation import. New config key `rsi_slope_min_for_entry: 0.5` in `_DEFAULT_PARAMS` and `config.json`. Condition renamed from `"rsi_turning"` to `"rsi_slope_rising"` in conditions dict.
- **Why:** The 2-bar check used redundant raw-bar RSI computation instead of the already-computed `rsi_slope_5` signal. The 5-bar slope is more robust and flows automatically to Claude's `ta_readiness`. Configurable threshold replaces hardcoded 2-bar comparison.

**Short fast-loop exits** · `core/orchestrator.py`
- **Impl:** New `_fast_step_short_exits(position)` method, called from `_fast_step_quant_overrides` for short positions (replaces former `continue` skip). Three exit triggers evaluated in priority order:
  1. Hard stop from intention: `price >= stop_loss` → market buy, urgency 1.0.
  2. ATR trailing stop: `price >= intraday_low + ATR × short_atr_stop_multiplier` → market buy, urgency 0.95.
  3. VWAP crossover: `vwap_position == "above"` and `volume_ratio > short_vwap_exit_volume_threshold` → market buy, urgency 0.85.
  All exits go through fill protection path. `_intraday_lows` dict added (symmetric to `_intraday_highs`) to track per-symbol session minimum price for ATR trailing stop.
- **New config keys:** `short_atr_stop_multiplier: 2.0`, `short_vwap_exit_enabled: true`, `short_vwap_exit_volume_threshold: 1.3` in `risk`.

**EOD forced close for momentum shorts** · `core/orchestrator.py`
- **Impl:** In `_medium_evaluate_positions`, momentum short positions trigger market buy in the last 5 minutes of session (`is_last_five_minutes(now_et)` using already-imported helper). Swing shorts explicitly excluded.
- **Why:** Momentum strategy is intraday; holding short overnight has asymmetric gap-up risk (short squeeze). Swing shorts are held overnight by design.

**`_recently_closed` persistence** · `core/state_manager.py`, `core/orchestrator.py`
- **Impl:** `PortfolioState.recently_closed: dict[str, str]` (UTC ISO timestamps) added. Written to `portfolio.json` on close events (`_journal_closed_trade` and position sync). On startup (`startup_reconciliation`), entries are reloaded if recorded within the last 60 seconds using elapsed-time monotonic math. Entries older than 60s are discarded on reload.
- **Why:** `_recently_closed` was in-memory only; bot restarts during the 60s cooldown window allowed immediate re-entry into just-closed positions. Persisting timestamps through restarts closes the gap.

**ATR position size cap** · `core/orchestrator.py`, `core/config.py`
- **Impl:** In `_medium_try_entry`, after TA size factor is applied: `max_shares = int((equity × max_risk_per_trade_pct) / ATR)`. Applied when `atr_position_size_cap_enabled` is true and ATR > 0. Direction-agnostic.
- **New config keys:** `atr_position_size_cap_enabled: true`, `max_risk_per_trade_pct: 0.02` in `risk`.
- **Why:** High-ATR entries could size into positions that risk far more than the configured `per_trade_max_loss_pct` if stop distance exceeds ATR. Cap ensures risk per trade is bounded by a fixed equity fraction regardless of strategy-level stop placement.

**New tests** · `ozymandias/tests/test_ta_pattern_signals.py`, `tests/test_short_protection.py`, `ozymandias/tests/test_strategies.py`
- 32 tests in `test_ta_pattern_signals.py` (all five new signals + score modifiers).
- 21 tests in `test_short_protection.py` (ATR trailing stop, VWAP crossover, hard stop, EOD close, `_recently_closed` persistence, ATR position cap).
- 21 new tests in `test_strategies.py` classes `TestMomentumSlopeAwareRsiGate` and `TestSwingSlopeAwareRsiGate`.
- Updated `test_technical_analysis.py` (5 tests) and `test_orchestrator.py` (1 test).

---

### Phase 16 Option A — RSI Gate in Live Path + Prompt Audit (March 18)

**RSI gate moved from dead `_evaluate_entry_conditions` to live `apply_entry_gate`** · `strategies/momentum_strategy.py`
- **Impl:** After Phase 14 dead code cleanup removed `generate_signals` from the orchestrator's medium loop, `MomentumStrategy._evaluate_entry_conditions` became unreachable. The slope-aware RSI gate lived there but was never executed in production. Option A moves the gate into `apply_entry_gate` (the live production path, called by `apply_hard_filters` in the ranker), makes it direction-aware, and removes the dead method entirely.
- **Gate logic (direction-aware):**
  - Long: normal zone [45,65] always pass; extended zone (65,78] requires `rsi_slope_5 ≥ rsi_slope_threshold (2.0)`; >78 always block; <45 block.
  - Short (mirror symmetry): hard floor at `100 - rsi_max_absolute (22)` — below floor is oversold/bounce risk, block; low extended zone [22,35] requires `rsi_slope_5 ≤ -rsi_slope_threshold (-2.0)`; ≥35 passes (no ceiling — RSI 80 falling is a valid short entry).
- **Tests:** 22 new tests in `TestMomentumApplyEntryGateRsi` in `test_strategies.py`. Gate ordering test confirms RVOL failure takes priority over VWAP failure takes priority over RSI gate.

**`entry_conditions` expansion — 6 new short-direction keys** · `intelligence/opportunity_ranker.py`, `ozymandias/tests/test_entry_conditions.py`
- **Impl:** `evaluate_entry_conditions()` extended with 6 new keys:
  - `require_below_vwap` (bool, SHORT) — rejects if `vwap_position != "below"`.
  - `rsi_slope_min` (float, LONG) — rejects if `rsi_slope_5 < value`.
  - `rsi_slope_max` (float, SHORT) — rejects if `rsi_slope_5 > value`.
  - `require_volume_trend_bars_min` (int, BOTH) — rejects if `volume_trend_bars < value`.
  - `require_macd_bearish` (bool, SHORT) — rejects if `macd_signal` not in `{"bearish","bearish_cross"}`.
  - `require_macd_histogram_expanding` (bool, BOTH) — rejects if `macd_histogram_expanding` is not True.
- **Tests:** 6 new test classes (50 total in `test_entry_conditions.py`). Each covers pass/fail/missing-signal/False-is-noop.

**`catalyst_type` conviction cap enforced in code** · `intelligence/opportunity_ranker.py`, `ozymandias/tests/test_opportunity_ranker.py`
- **Impl:** `apply_hard_filters()` rejects swing entries with `catalyst_type == "technical_only"` and `conviction > 0.50`. Was prompt-only enforcement before.
- **Tests:** 6 tests in `TestCatalystTypeConvictionCap`.

**`reasoning.txt` v3.4.0 audit — 5 bugs fixed** · `ozymandias/config/prompts/v3.4.0/reasoning.txt`
1. Stop-loss direction wrong for shorts: "price has fallen below" was specified for all positions. Now: short position stop is breached when price rises above stop.
2. Phantom "long-term" strategy: FOCUS section listed a third strategy not in the system. Rewritten to document only momentum (intraday) and swing (multi-day).
3. `catalyst_type` template confusion: field appeared without "(swing only)" annotation, misleading Claude to include it on momentum entries. Annotated and instruction added: "SWING ENTRIES ONLY — omit this field for momentum".
4. `position_size_pct` default 0.10 → 0.05: template default was 2× the documented floor. Changed to 0.05.
5. PDT instruction misleading: old text implied Claude manages PDT. Rewritten to "system enforces PDT limits automatically — be conservative with exit recommendations when count is 1 or 0".
- **Additional:** `timeframe` template field removed (unused); all 11 `entry_conditions` keys documented with long/short direction examples; position review instruction made direction-aware.

**`test_integration.py` `_make_bars` fix** · `ozymandias/tests/test_integration.py`
- **Impl:** Old flat + step-up price series produced RSI ≈ 100 (one gain, no losses → RS = ∞). New series: first 50 bars alternating ±small moves (~54% up → RSI ≈ 58), last 10 bars biased upward [+,+,-,+,+,-,+,+,-,+] → RSI ≈ 73 with `rsi_slope_5 ≈ 7.2 > 2.0 threshold`. Final close ≈ 1.04% above base (within drift ceiling). Passes RVOL, VWAP, and new RSI gates.

---

### Post-Phase-16 Paper Trading Fixes (March 19)

**Claude context token budget** · `intelligence/claude_reasoning.py`, `core/config.py`
- **Spec:** *(not defined)*
- **Impl:** Replaced `_TOKEN_TARGET_MAX = 8_000` (context-only ceiling) with `_TOTAL_TOKEN_BUDGET = 25_000` (full request budget). Template token count computed at engine init: `self._prompt_template_tokens = len(template) // _CHARS_PER_TOKEN` (fallback 6,000 on OSError). Trim loop guard: `context_token_budget = _TOTAL_TOKEN_BUDGET - self._prompt_template_tokens`. Debug log now shows `context + template + total vs budget`.
- **Why:** Old 8,000 ceiling was applied to context JSON alone without accounting for the ~5,664-token prompt template. Real total was 8,000 + 5,664 ≈ 13,664 tokens — well within Claude's limit. But watchlist trimming fired aggressively, stripping down to ~7 visible symbols instead of the full 26. Fixing to a 25,000 combined budget stops over-trimming while staying safely under the 32K context window.

**Entry defer expiry** · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `self._entry_defer_counts: dict[str, int] = {}` added to `Orchestrator.__init__`. Incremented in `_medium_try_entry` on each `entry_conditions` miss. When count reaches `max_entry_defer_cycles` (default 5): warning logged, `top.entry_conditions` cleared so the gate no longer fires for the remainder of the reasoning cycle. Dict cleared alongside `_cycle_consumed_symbols` on each successful Claude slow-loop call. Order placement clears the count for that symbol.
- **New config key:** `SchedulerConfig.max_entry_defer_cycles: int = 5` in `config.json`.
- **Why:** AMD stayed frozen in the deferred queue for 3 hours because its RSI slope gate was never met — the opportunity persisted indefinitely from a stale Claude recommendation. Expiry forces the gate to clear and the next Claude cycle to make a fresh entry decision.

**Composite score floor** · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `RankerConfig.min_composite_score: float = 0.45` added. Checked at the top of `_medium_try_entry` (after cycle-consumed guard, before drift/sizing) — returns `False` if `top.composite_score < min_composite_score`.
- **Why:** Prevents degenerate entries where each individual gate clears but the combined score (conviction + technical + RAR + liquidity) is too weak to justify capital deployment. Set at 0.45 after SLB analysis: SLB scored 0.455 with conviction=0.60 and technical=0.63 (solid) dragged down only by a tight 1.1:1 RAR. Floor below that natural composite floor preserves legitimate setups.

**Position review audit logging** · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `_apply_position_reviews` logs each review at INFO: `"Position review: %s — action=%s — %s"`.
- **Why:** Position review actions were invisible in the log — no way to verify Claude's hold/exit/adjust decisions were being applied.

**`position_in_profit` slow-loop trigger** · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `SlowLoopTriggerState.last_profit_trigger_gain: dict[str, float]` tracks the unrealised gain fraction at which each position last triggered. In `_check_triggers()`, trigger 4b fires when `unrealised_gain >= last_trigger + position_profit_trigger_pct` (direction-aware for shorts: gain = `(avg_cost - price) / avg_cost`). On trigger, `last_profit_trigger_gain[symbol]` is updated to current gain. Snapshot written to `last_profit_trigger_gain` in `_run_claude_cycle` success block. Cleared in `_journal_closed_trade` when position closes.
- **New config key:** `SchedulerConfig.position_profit_trigger_pct: float = 0.015` in `config.json`.
- **Why:** HAL position gained +$130 then slipped to +$100 with no bot reaction. The system had no mechanism to call Claude progressively as unrealised gains grew. Mechanical trailing stops were rejected for swing positions (ATR noise would cause premature exits). Claude-based solution: trigger at 1.5% gain intervals, let Claude decide whether to tighten the stop based on thesis progress.

**`reasoning.txt` profit protection instruction** · `config/prompts/v3.4.0/reasoning.txt`
- **Spec:** *(not defined)*
- **Impl:** Position review instruction 1 extended with: when a position has meaningful unrealised gains, explicitly assess whether the current `stop_loss` still reflects thesis risk — raising the stop is appropriate when the primary catalyst has triggered, a key milestone has been reached, or the gain is large enough that returning to entry would represent a significant missed opportunity. Explicitly states: "Do not adjust mechanically on every profit — when the thesis has substantial remaining upside and no milestone has been reached, hold with original stops is correct."
- **Why:** Without explicit instruction, Claude had no prompt signal to act on profit trigger calls. Framing is intentionally neutral (lists when adjustment IS appropriate, not when it's required) to preserve Claude's discretion and avoid adversarial mechanical-adjustment bias.

**Trade journal versioning** · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `prompt_version` (`self._config.claude.prompt_version`) and `bot_version` (`self._config.claude.model`) appended to every `_trade_journal.append()` call in both `_journal_closed_trade` and the ghost-position cleanup path. Previous 79 unversioned entries archived to `state/trade_journal_archive_pre_v3.4.jsonl`; active journal starts fresh.
- **Why:** All 79 existing entries lacked version metadata, making them unfiltereable by build for training pipelines. Future entries are now filterable by `WHERE prompt_version >= "v3.4.0"`.

**Position sizing: Claude's `position_size_pct` as primary driver** · `core/orchestrator.py`
- **Spec:** *(not defined — spec implied ATR-based sizing)*
- **Impl:** `_medium_try_entry` sizing block replaced. Old: `calculate_position_size(symbol, price, atr, equity)` with `risk_per_trade_pct=0.01` hardcoded default — ignored `position_size_pct` entirely. New: `effective_pct = min(top.position_size_pct, cfg.risk.max_position_pct)`; `target_qty = int(equity × effective_pct / price)`. TA scale factor applied to `target_qty`; ATR cap remains as a hard risk ceiling. `calculate_position_size` no longer called from this path.
- **Why:** ATR formula with 1% risk on a $30k account always produced ~$4-5k positions regardless of Claude's conviction level — positions were uniformly near the 20% cap. Claude recommends 5%–20% based on setup quality; those recommendations were silently discarded. Now a 5% recommendation → ~$1,500 position; 15% → ~$4,500, correctly reflecting Claude's confidence. `max_position_pct` (config) clamps the effective percent so the ceiling remains operator-configurable.
- **Tests updated:** `TestThesisChallenge` and `TestTASizeModifier` in `test_orchestrator.py` and `test_execution_fidelity.py` — removed stale `calculate_position_size = MagicMock(return_value=N)` lines; updated quantity assertions to derive from `equity × pct / price`.

---

### Phase 15 — Context Enrichment (March 20)

**`RankResult` dataclass** · *(not in spec)* · `intelligence/opportunity_ranker.py`
- **Spec:** `rank_opportunities` returns `list[ScoredOpportunity]`
- **Impl:** New `RankResult` dataclass wraps `candidates: list[ScoredOpportunity]` and `rejections: list[tuple[str, str]]`. `rank_opportunities` now returns `RankResult`. Call sites unwrap `.candidates`; the orchestrator iterates `.rejections` to populate `_recommendation_outcomes`.
- **Why:** Callers needed access to the reason string for every hard-filter rejection without a second API call. Wrapping in a named dataclass avoids breaking existing call sites that only need candidates.

**`_recommendation_outcomes` tracker** · `phases/15_context_enrichment.md §2` · `core/orchestrator.py`
- **Spec:** In-memory `dict[str, dict]` tracking pipeline stage for each Claude-recommended symbol. States: `ranker_rejected`, `conditions_waiting`, `gate_expired`, `order_pending`, `filled`, `cancelled`. Purged daily at slow-loop start.
- **Impl:** As specified. Populated from `rank_result.rejections` (ranker_rejected), `_medium_try_entry` on defer (conditions_waiting), expiry (gate_expired), and after `broker.place_order()` (order_pending). Updated in `_dispatch_confirmed_fill` (filled) and `_fast_step_poll_and_reconcile` on cancel/reject changes (cancelled). Session-veto symbols not recorded. Entries purged at each slow-loop cycle start using `recommendation_outcome_max_age_min` config.

**`WatchlistEntry.expected_direction`** · `phases/15_context_enrichment.md §1` · `core/state_manager.py`
- **Spec:** *(not defined — Phase 15 addition)*
- **Impl:** `expected_direction: str = "either"` added to `WatchlistEntry` dataclass. Sentinel value `"either"` is never passed to `compute_composite_score` — callers map `"either"` → `"long"`. Loaded from JSON via `d.get("expected_direction", "either")` for backward compatibility.
- **`_apply_watchlist_changes`** extracts `expected_direction` from Claude's add items and passes it to `WatchlistEntry`. Watchlist pruning (`_prune_score`) uses direction-adjusted score when `expected_direction != "either"`.

**`ta_readiness` dict in context** · `phases/15_context_enrichment.md §3` · `intelligence/claude_reasoning.py`
- **Spec:** Structured dict replacing `technical_summary` string in tier-1 watchlist context. Direct pass-through of `indicators[symbol]["signals"]` + direction-adjusted `composite_score`.
- **Impl:** Each tier-1 entry now has `ta_readiness` (all signal key/values from `signals` dict + `composite_score` computed with direction). `technical_summary` string retained for `run_position_review` path only (not removed). `_tier1_score` sort key updated to use direction-adjusted score when `expected_direction != "either"`.

**`TradeJournal.load_recent` / `compute_session_stats`** · `phases/15_context_enrichment.md §4` · `core/trade_journal.py`
- **Spec:** *(not defined — Phase 15 addition)*
- **Impl:** `load_recent(n: int) -> list[dict]` — acquires lock, reads all lines, filters for close records (`record_type == "close"` or absent) with `entry_price > 0`, returns last n in reverse-chronological order. `compute_session_stats(min_trades: int = 3) -> dict` — calls `load_recent(20)`, computes overall/short/high-conviction win rates and avg win/loss; omits `short_win_rate_pct` when no short trades, omits `high_conviction_win_rate_pct` when < 3 high-conviction trades. Both methods are async with lock safety.

**`assemble_reasoning_context` extended** · `phases/15_context_enrichment.md §5` · `intelligence/claude_reasoning.py`
- **Spec:** Add `recommendation_outcomes`, `entry_defer_counts`, `recent_executions`, `execution_stats` to context. Async work done upstream; `assemble_reasoning_context` remains sync.
- **Impl:** 3 new optional parameters added (`recommendation_outcomes`, `recent_executions`, `execution_stats` — all default `None`). `entry_defer_counts` parameter dropped: the orchestrator already bakes defer counts into `stage_detail` strings before calling `assemble_reasoning_context`, making the parameter redundant. `_run_claude_cycle` in orchestrator pre-computes `recent_executions = await _trade_journal.load_recent(...)` and `execution_stats = await _trade_journal.compute_session_stats(...)` before calling `run_reasoning_cycle`. `recommendation_outcomes` assembled inside `assemble_reasoning_context`; age-filtered to `recommendation_outcome_max_age_min` minutes, capped at 15 entries, sorted ascending by age. Post-implementation audit removed a duplicate dead loop in the `recommendation_outcomes` assembly (first pass built `entry_dict` but never appended; second pass was the real implementation). Dead loop removed March 20.

**`ClaudeConfig` additions** · `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** 3 new fields: `recommendation_outcome_max_age_min: int = 60`, `recent_executions_count: int = 5`, `execution_stats_min_trades: int = 3`. Used by `_run_claude_cycle` to parameterize how much history is passed to Claude.

**Prompt v3.5.0** · `config/prompts/v3.5.0/reasoning.txt`
- **Spec:** New versioned prompt directory with Phase 15 context fields documented.
- **Impl:** Forked from v3.4.0. Added `CONTEXT FIELDS (Phase 15 additions):` section documenting `recommendation_outcomes`, `recent_executions`, `ta_readiness`, `execution_stats`, and `expected_direction`. Updated `watchlist_changes.add` format from plain string array to dict array with `expected_direction` field. `ClaudeConfig.prompt_version` default updated to `"v3.5.0"`.

**New tests** · `ozymandias/tests/test_context_enrichment.py`
- 50 new tests across 8 classes: `TestRankResult`, `TestRecommendationOutcomesLifecycle`, `TestRecommendationOutcomesContextAssembly`, `TestTradeJournalLoadRecent`, `TestComputeSessionStats`, `TestTaReadiness`, `TestWatchlistEntryExpectedDirection`, `TestBackwardCompat`.
- `test_opportunity_ranker.py`: `_rank()` helper updated to unwrap `.candidates`; `TestSessionVeto` refactored with `_rank_candidates()` helper.
- `test_orchestrator.py`: `_make_ranked()` and `_medium_loop_mocks()` updated to return `RankResult(candidates=..., rejections=[])`.

---

### Operational Hardening (March 20)

**RSI entry floor** · `config/config.json`, `strategies/momentum_strategy.py`
- **Spec:** *(not defined)*
- **Impl:** `rsi_entry_min: 60` added to `strategy_params.momentum`. Momentum long entries blocked when live RSI < 60. Raises `apply_entry_gate` rejection.
- **Why:** Backtest of paper trades showed RSI 45–55 at entry → 17% win rate; RSI ≥ 65 → 37%. Old floor of 45 was letting low-momentum entries through. Integration test `_make_bars` tail pattern updated to produce RSI ≈ 71 to match new floor.

**Trade journal lifecycle records** · `core/orchestrator.py`, `core/trade_journal.py`
- **Spec:** *(not defined — spec specified only closed-trade records)*
- **Impl:** `record_type` field added to all journal entries: `"open"` (written in `_register_opening_fill`), `"snapshot"` (written in `_check_triggers` on `position_in_profit`), `"review"` (written in `_apply_position_reviews`), `"close"` (existing path). All records for one trade share a `trade_id` UUID generated at fill time and stored in `_entry_contexts[symbol]["trade_id"]`. Adoption path (`_fast_step_position_sync`) and startup restore both generate a fresh UUID so restarted positions link their future records (but not to the original open, which predates the session). `position_size_pct` and `claude_conviction` written to `open` records. `trade_journal.py`: `if not record.get("trade_id")` replaces `if "trade_id" not in record` so explicitly-None trade_id (restarted positions) also gets a UUID.
- **Why:** Journal only recorded close snapshots. Could not reconstruct how a trade evolved — entry signals, mid-trade reviews, profit snapshots — without cross-referencing logs.
- **Tests:** `TestTradeJournalLifecycle` class (9 tests) in `test_orchestrator.py`.

**Session-based logging** · `core/logger.py`, `tests/test_logger.py`
- **Spec:** *(not defined)*
- **Impl:** Complete rewrite of `logger.py`. Replaced two-file rotation (`current.log` / `previous.log`) with session files: each `setup_logging()` call creates `logs/session_YYYY-MM-DDTHH-MM-SSZ.log`. `current.log` symlink always points to the active session. `max_session_logs: int = 0` (default = unlimited, never auto-deletes). Pruning only occurs when explicitly configured with a non-zero value. `setup_logging()` call removed from `orch.run()` to prevent double session file per launch.
- **Why:** Previous rotation overwrote `previous.log` on every restart — only two files ever existed. Comprehensive paper trading needed all session logs retained indefinitely.
- **Tests:** `test_logger.py` fully rewritten — 19 tests covering session creation, symlink, pruning, format, and `get_logger`.

**Graceful degradation on startup failure** · `core/orchestrator.py`, `main.py`
- **Spec:** *(not defined)*
- **Impl:** `_startup()` wraps `_load_credentials()`, `broker.get_account()`, and `broker.get_market_hours()` in separate try/except blocks; each logs CRITICAL with an operator-readable message before re-raising. `load_config()` in `_run()` similarly wrapped. `main()` adds `except Exception` catch after `KeyboardInterrupt`: prints `FATAL: {exc}` to stderr and calls `sys.exit(1)` instead of showing a raw Python traceback.
- **Why:** Bot crashed with an unformatted Python traceback on bad API keys or missing credentials file. Operators need a readable error with actionable guidance.

**Stop adjustment guard** · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** In `_apply_position_reviews`, before applying `adjusted_targets.stop_loss`, new stop is checked against current price from `_latest_indicators`. For longs: rejected if `new_stop >= current_price`. For shorts: rejected if `new_stop <= current_price` (checks both `"short"` and `"sell_short"` direction values). On rejection: WARNING logged with rejected value, current price, and kept value. Stop is left unchanged.
- **Why:** XOM incident (March 20): Claude's position review raised stop from $158.50 → $162.00 while price was $161.25. Strategy exited immediately at +1.71% rather than letting the trade run. Second review in the same cycle then proposed $160.00 — too late.
- **Tests:** 3 tests in `TestSlowLoopStateApplication`: long rejected, long accepted, short rejected.

**Adoption path trade_id** · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** Both adoption paths now generate `"trade_id": str(uuid.uuid4())` in `_entry_contexts`: (1) startup restore loop (positions that survived a bot restart), (2) mid-session adoption in `_fast_step_position_sync` (broker positions without a pending intention). Previously both paths left `trade_id` absent → each snapshot/review/close for a restarted position generated its own UUID, producing unlinked records.
- **Why:** Journal analysis after first paper session showed 5 different trade_ids for XOM across its reviews and close record — impossible to reconstruct the trade timeline.

**Emergency exit and shutdown commands** · `core/orchestrator.py`, `ozymandias/scripts/emergency.py`
- **Spec:** *(not defined)*
- **Impl:** Two signal files: `state/EMERGENCY_EXIT` and `state/EMERGENCY_SHUTDOWN`. Both checked at the top of every `_fast_loop` iteration (before market-hours guard, so shutdown works outside market hours). `EMERGENCY_SHUTDOWN`: calls `_shutdown()` immediately. `EMERGENCY_EXIT`: three-phase aggressive liquidation — (1) cancel all pending orders via `fill_protection.get_pending_orders()` + `broker.cancel_order()`; (2) place market exit for every local position, tagged `"emergency_exit"` in journal; (3) poll `broker.get_positions()` every 2 seconds for up to 60 seconds, logging CRITICAL for confirmed closes and CRITICAL MANUAL ACTION REQUIRED for any that remain open. Signal files deleted immediately after detection. `scripts/emergency.py`: CLI trigger with confirmation prompt; subcommands `exit` and `shutdown`; `--yes/-y` flag skips prompt.
- **Why:** No mechanism existed to liquidate all positions or stop the bot without killing the process. Designed with Discord integration in mind — Discord handler writes the signal file, bot acts within one fast-loop tick (~5–15 seconds).

---

### Phase 17 — Trigger Responsiveness & Data Freshness (March 20)

**Fix 1: Parallel medium loop fetch** · `core/orchestrator.py` — `_medium_loop_cycle()`
- **Spec:** Serial `for symbol in scan_symbols` loop
- **Impl:** Replaced with `asyncio.gather` bounded by `asyncio.Semaphore(medium_loop_scan_concurrency)`. `generate_signal_summary` wrapped in `asyncio.to_thread` (CPU-bound TA computation no longer blocks the event loop). Context symbol fetch (`_CONTEXT_SYMBOLS`) parallelized separately using the same semaphore. `_last_medium_loop_completed_utc` (Optional[datetime]) stamped at the end of each cycle. `self._all_indicators` (merged dict of `_latest_indicators + _market_context_indicators`) set once per cycle — consumed by `_check_triggers` and available for Phase 19 compressor. `self._latest_indicators` initialized to `{}` in `__init__` (was previously lazy-set in first medium loop cycle; now safe to read at any time without `getattr`).
- **New config keys:** `SchedulerConfig.medium_loop_scan_concurrency: int = 10`
- **Why:** Serial yfinance fetches at 120s interval meant real refresh was slower than the interval implied. With 20+ symbols plus 10 context ETFs, serial fetching consumed the full interval.

**Fix 2: Macro/sector/RSI extreme triggers** · `core/orchestrator.py` — `_check_triggers()`
- **Spec:** *(not defined)*
- **Impl:** Three new triggers added to `_check_triggers()`:
  1. `market_move:{sym}` — SPY/QQQ/IWM moves >1% (configurable) from `last_claude_call_prices` baseline. Fires Claude even during quiet per-stock periods when broad market breaks.
  2. `sector_move:{etf}` — sector ETF moves >1.5% (configurable) from last call. Threshold tightened to `1.5% × sector_exposure_threshold_factor (0.7)` = 1.05% when portfolio has open exposure to that sector (via `_SECTOR_MAP`). Directly-held ETF positions skip (covered by `price_move`).
  3. `market_rsi_extreme` — SPY RSI crosses below `macro_rsi_panic_threshold` (25) or above `macro_rsi_euphoria_threshold` (72). Single trigger name regardless of direction. Re-arm band (`macro_rsi_rearm_band = 5`) prevents rapid re-firing; `rsi_extreme_fired_low/high` flags in `SlowLoopTriggerState`. RSI key is `"rsi"` (not `"rsi_14"` as spec draft said — TA module uses `"rsi"`).
- **`_SECTOR_MAP`**: module-level constant mapping stock/ETF symbols → sector ETF (e.g. `"NVDA": "XLK"`). Used for exposure detection in sector_move trigger. Replaces the local `_SECTOR_ETFS` dict that was previously in `_build_market_context`; `_build_market_context` now uses a local `_SECTOR_ETF_NAMES` dict (ETF → display name) since `_SECTOR_MAP` serves a different purpose.
- **`SlowLoopTriggerState` new fields:** `last_claude_call_prices: dict[str, float]` (baseline snapshotted at each successful Claude call + seeded on `indicators_ready`), `rsi_extreme_fired_low: bool`, `rsi_extreme_fired_high: bool`.
- **New config keys** in `SchedulerConfig`: `macro_move_trigger_pct`, `macro_move_symbols`, `sector_move_trigger_pct`, `sector_exposure_threshold_factor`, `macro_rsi_panic_threshold`, `macro_rsi_euphoria_threshold`, `macro_rsi_rearm_band`.

**Fix 3: Medium-loop-gated slow loop** · `core/orchestrator.py` — `_slow_loop_cycle()`
- **Spec:** *(not defined)*
- **Impl:** Guard added to `_slow_loop_cycle` after existing guards: if `last_claude_call_utc is not None` AND `_last_medium_loop_completed_utc is None or ≤ last_claude_call_utc`, skip. This prevents Claude from reasoning on identical indicators twice in a row. Gate is bypassed when `last_claude_call_utc is None` (first call ever) — `indicators_ready` trigger already ensures TA data exists.
- **Test impact:** Integration tests and `TestDegradation` that seed `_latest_indicators` directly and backdate `last_claude_call_utc` needed `_last_medium_loop_completed_utc` seeded in their fixtures (2 fixtures updated).

**Fix 4: Adaptive reasoning cache TTL** · `core/reasoning_cache.py`, `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `load_latest_if_fresh(max_age_min: int | None = None)` — optional override parameter. `effective_max = max_age_min if max_age_min is not None else REUSE_MAX_AGE_MINUTES`. New method `_compute_cache_max_age()` on `Orchestrator`: reads SPY RSI from `_market_context_indicators`; returns panic (10 min), stressed (20 min), euphoria (15 min), or default (60 min) TTL. Medium loop Step 3 passes `max_age_min=self._compute_cache_max_age()` to `load_latest_if_fresh`.
- **RSI lookup:** `(spy_ind.get("signals") or spy_ind).get("rsi")` — handles both nested (`signals.rsi`) and flat (`rsi`) indicator formats from `_market_context_indicators`.
- **New config keys** in `ClaudeConfig`: `cache_max_age_default_min`, `cache_max_age_stressed_min`, `cache_max_age_panic_min`, `cache_max_age_euphoria_min`, `cache_stress_rsi_low`, `cache_panic_rsi_low`, `cache_euphoria_rsi_high`.

**`_all_indicators` instance attribute** · `core/orchestrator.py`
- **Spec:** *(not defined in Phase 17 spec — called out as a Phase 19 prerequisite)*
- **Impl:** `self._all_indicators: dict = {}` initialized in `__init__`. Set at the end of each `_medium_loop_cycle` as `{**self._latest_indicators, **self._market_context_indicators}`. Used by `_check_triggers` as the merged price/indicator lookup for all trigger types; falls back to on-demand merge when `_all_indicators` is empty (guards test compatibility for tests that seed `_latest_indicators` directly).

**New tests** · `ozymandias/tests/test_trigger_responsiveness.py`
- 29 tests across 5 classes: `TestParallelMediumLoop` (Fix 1), `TestMacroMoveTrigger` (Fix 2 macro), `TestSectorMoveTrigger` (Fix 2 sector), `TestRsiExtremeTrigger` (Fix 2 RSI), `TestMediumLoopGate` (Fix 3), `TestAdaptiveCacheTtl` (Fix 4).


---

### Direction-Aware Quant Overrides + Per-Strategy Thresholds (March 22, 2026)

**`check_vwap_crossover()` signature** · *(not defined in spec)* · `execution/risk_manager.py`
- **Spec:** *(not defined)*
- **Impl:** Added `direction: str` and `volume_threshold: float` as required keyword-only args. Long fires on `vwap_position=="below"`, short fires on `"above"`. `volume_threshold` replaces removed module constant `_VWAP_VOLUME_RATIO_THRESHOLD`.
- **Why:** Direction-aware inversion required to support short exits via the same code path as longs.

**`check_roc_deceleration()` signature** · *(not defined in spec)* · `execution/risk_manager.py`
- **Spec:** *(not defined)*
- **Impl:** Added `direction: str` keyword-only arg. Long uses `roc_deceleration` flag; short uses `roc_negative_deceleration` flag.
- **Why:** ROC deceleration semantics invert for shorts (negative ROC decelerating = bearish momentum exhausting).

**`check_momentum_score_flip()` signature** · *(not defined in spec)* · `execution/risk_manager.py`
- **Spec:** *(not defined)*
- **Impl:** Added `direction: str` keyword-only arg. Long fires on prev > +1.5 → now < 0. Short fires on prev < -1.5 → now > 0. Previously both branches fired for all positions.
- **Why:** Negative-to-positive flip is bullish recovery (bad exit for long). Direction-aware logic fires only the adverse flip per direction.

**`check_atr_trailing_stop()` signature** · *(not defined in spec)* · `execution/risk_manager.py`
- **Spec:** *(not defined)*
- **Impl:** Parameter `intraday_high` renamed to `intraday_extremum`. Added `direction: str` and `atr_multiplier: float` as required keyword-only args. Long measures drop from HIGH; short measures rise from LOW.
- **Why:** ATR trail for shorts uses intraday LOW as the reference, not intraday HIGH.

**`check_hard_stop()` new method** · *(not defined in spec)* · `execution/risk_manager.py`
- **Spec:** *(not defined)*
- **Impl:** New method, short-only. Fires when `price >= stop_loss`. Bypasses `allow_signals` gating — the hard stop is unconditional for shorts. Long stops managed by broker limit orders.
- **Why:** Hard stop needed at `RiskManager` level for testability; also moves the check into `_fast_step_quant_overrides` before the min-hold guard.

**`evaluate_overrides()` signature** · *(not defined in spec)* · `execution/risk_manager.py`
- **Spec:** *(not defined)*
- **Impl:** `intraday_high` positional param renamed to `intraday_extremum`. Added optional kwargs: `direction`, `atr_multiplier`, `vwap_volume_threshold`. All `check_*` calls now pass these kwargs through.
- **Why:** Per-strategy thresholds and direction-awareness needed; kwargs maintain backward compatibility (caller can omit for long/default behavior).

**`_fast_step_short_exits()` removed** · *(not defined in spec)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** Method deleted. All logic merged into `_fast_step_quant_overrides()`. Hard stop fires first (before min-hold gate); VWAP/ATR/ROC signals go through the same allow_signals and min-hold path as longs.
- **Why:** Separate short exits path had no strategy gating, no ROC/RSI divergence signals for shorts, and hardcoded thresholds.

**`_place_override_exit()` new helper** · *(not defined in spec)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** Extracted order placement, fill protection, `record_order`, `_pending_exit_hints`, and `_override_exit_count` into a shared helper used by both hard stop and signal paths.
- **Why:** DRY — previously duplicated across `_fast_step_quant_overrides` and `_fast_step_short_exits`.

**`override_atr_multiplier()` / `override_vwap_volume_threshold()` new methods** · *(not defined in spec)* · `strategies/base_strategy.py`
- **Spec:** *(not defined)*
- **Impl:** Two concrete methods on `Strategy` ABC. Read from `_params` with defaults 2.0 and 1.3. Swing defaults to 3.0/1.5 (wider, prevents intraday noise exits on multi-day holds).
- **Why:** Thresholds previously hardcoded as module constants in `risk_manager.py`; now per-strategy configurable via `config.json strategy_params`.

**Deprecated config fields** · *(not defined in spec)* · `config/config.json` → `execution/risk_manager.py`
- **Spec:** *(not defined)*
- **Impl:** `short_vwap_exit_enabled` and `short_vwap_exit_volume_threshold` in `risk.` section are no longer read by any code path. `short_atr_stop_multiplier` also unused. VWAP threshold is now `strategy_params.{strategy}.override_vwap_volume_threshold`; ATR multiplier is `strategy_params.{strategy}.override_atr_multiplier`.
- **Why:** Replaced by per-strategy threshold accessors.

**`_pending_exit_hints` value `"short_protection"` replaced by `"hard_stop"`** · *(not defined in spec)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** Hard stop exits now tag `"hard_stop"` instead of `"short_protection"`. Signal-triggered short exits tag `"quant_override"` (same as longs). Old hint `"short_protection"` no longer emitted.
- **Why:** More specific hint; separates hard stop (priority 1, no gates) from signal exits (evaluated through allow_signals path).

---

## Cross-References

- [[ozy-drift-log]] — Active drift log (new entries)
- [[ozy-drift-log-eras-11-14]] — Previous era (execution fidelity)
- [[ozy-drift-log-eras-18]] — Next era (watchlist intelligence)
- [[ozy-doc-index]] — Full routing table
