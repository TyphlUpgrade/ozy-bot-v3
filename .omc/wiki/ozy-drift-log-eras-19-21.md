---
title: "Ozy Drift Log — Eras 19-21"
tags: [ozymandias, drift-log, archive, phases-19-21, sonnet-haiku-durability]
category: reference
ceiling_override: frozen-archive
frozen: true
created: 2026-04-09
updated: 2026-04-09
---

# Ozy Drift Log — Eras 19-21 (Sonnet/Haiku/Durability)

Frozen archive of spec deviations from Phase 19 (Sonnet Strategic Output), Phase 20 (Haiku Operational Layer), and Phase 21 (Durability and Regime Response).
For the active drift log and filing rules, see [[ozy-drift-log]].

---

### 2026-03-27 — Phase 19: Sonnet Strategic Output

**`compute_sector_dispersion`** · *(new function)* · `intelligence/technical_analysis.py`
- New helper: `compute_sector_dispersion(watchlist_entries, sector_map, daily_indicators) -> dict`.
- For each sector ETF that has watchlist symbols: computes `symbol_roc_5d - etf_roc_5d`, surfaces top 3 outperformers and bottom 3 underperformers. Bidirectional — long candidates breaking out and short candidates breaking down.
- **Key fix vs. original spec:** `if not etf_data.get('roc_5d')` is `True` when `roc_5d == 0.0` (falsy). Changed to `if etf_data.get('roc_5d') is None` to avoid silently skipping sectors with flat ETF performance.
- `watchlist_entries` accepts `list[WatchlistEntry]` or `list[dict]` (uses `.symbol` attr if present, else `["symbol"]` key).

**Four new `ReasoningResult` fields** · *(schema extension)* · `intelligence/claude_reasoning.py`
- Added: `regime_assessment: dict | None`, `sector_regimes: dict | None`, `filter_adjustments: dict | None`, `active_theses: list[dict] | None` — all default to `None`.
- Added helpers: `_safe_dict(val) -> dict | None` and `_safe_list_of_dicts(val) -> list[dict] | None` for defensive parsing.
- `_result_from_raw_reasoning()` parses all four fields using these helpers. Malformed or absent fields silently return `None` — no crash path.

**`filter_adjustments` application** · *(new behaviour)* · `intelligence/opportunity_ranker.py`, `core/orchestrator.py`
- `_clamp_filter_adjustments(fa) -> dict | None`: pre-clamps Claude-proposed thresholds to config floor constants (`_FILTER_ADJ_MIN_RVOL = 0.5`, `_FILTER_ADJ_MIN_COMPOSITE = 0.35`) before passing to strategy `apply_entry_gate`. Strategies never see unclamped values and don't need config access.
- `rank_opportunities` and `apply_hard_filters` gain `filter_adjustments: dict | None = None` param.
- `_medium_try_entry` in orchestrator: composite floor applies `filter_adjustments["min_composite_score"]` with hard floor `filter_adj_min_composite` guard.
- `self._filter_adjustments: dict | None = None` stored on orchestrator; reset to `None` at start of each Sonnet cycle, then repopulated from `ReasoningResult`. Stale adjustments never persist across cycles.
- **Spec bug fixed:** Spec said apply `min_rvol` in `_medium_try_entry` and `evaluate_entry_conditions`. Actual RVOL floor lives in strategy `apply_entry_gate`. Fixed by passing `filter_adjustments` through the ranker to `apply_entry_gate` (pre-clamped), not via orchestrator direct path.

**Strategy trend gate override** · *(new behaviour)* · `intelligence/strategies/momentum_strategy.py`, `intelligence/strategies/swing_strategy.py`, `intelligence/strategies/base_strategy.py`
- `apply_entry_gate` abstract signature updated: `(action, signals, entry_conditions=None, filter_adjustments=None)`.
- `MomentumStrategy.apply_entry_gate`: VWAP gate (`require_vwap_gate`) is skipped when `entry_conditions` is non-empty (Claude explicitly specified TA gates → Claude's conditions take precedence over the strategy-level VWAP block). Logged at DEBUG.
- RVOL floor: uses `filter_adjustments["min_rvol"]` when present (pre-clamped by caller); falls back to strategy param `min_rvol_for_entry`.
- `SwingStrategy.apply_entry_gate`: signature updated; no additional logic changes (swing gate doesn't have a VWAP block to yield).

**`_last_regime_assessment` and regime condition expiry** · *(new behaviour)* · `core/orchestrator.py`
- `self._last_regime_assessment: dict | None = None` stored on orchestrator; updated from `ReasoningResult.regime_assessment` after each Sonnet cycle.
- `_check_regime_conditions()`: parses `valid_until_conditions` list from `_last_regime_assessment`; evaluates each condition string against live `_daily_indicators` via regex (e.g., `"SPY daily RSI > 40"`, `"VIX < 20"`). Returns `True` if any condition is now met → `"regime_condition"` appended to triggers list in `_check_triggers`, forcing a fresh Sonnet cycle. No LLM evaluation — pure string/regex match against known indicator keys.

**Context enrichment for Sonnet** · *(new inputs)* · `core/orchestrator.py`, `_build_market_context`
- `sector_dispersion`: computed via `compute_sector_dispersion` using all watchlist entries, `_SECTOR_MAP` (module-level constant, not `self._SECTOR_MAP`), and `_daily_indicators`.
- `recent_rejections`: sourced from `_recommendation_outcomes`; capped at 10 most recent; format `{"symbol": ..., "reason": stage_detail, "cycles_rejected": rejection_count}`.
  - **Spec bug fixed:** Phase file used `data["last_rejection_reason"]` and `data["consecutive_rejections"]`; actual dict keys are `"stage_detail"` and `"rejection_count"]`. Fixed in spec file and implementation.
- `news_themes`: pure string aggregation of `WatchlistEntry.reason` fields grouped by sector ETF key; no new Claude calls.
- Daily indicator fetch scope extended: now includes all open position symbols and all watchlist entries (previously only SPY/QQQ/IWM + open swing positions).

**`filter_adj_min_rvol` / `filter_adj_min_composite` config fields** · *(new config)* · `core/config.py`, `config/config.json`, `intelligence/opportunity_ranker.py`
- `RankerConfig.filter_adj_min_rvol: float = 0.5` and `filter_adj_min_composite: float = 0.35` — absolute floors below which Claude cannot push thresholds regardless of `filter_adjustments`.
- Module-level constants `_FILTER_ADJ_MIN_RVOL` and `_FILTER_ADJ_MIN_COMPOSITE` in `opportunity_ranker.py` mirror these values; used by `_clamp_filter_adjustments`. Config-driven override path possible in future.

**Prompt v3.10.0** · *(new version)* · `config/prompts/v3.10.0/`, `config/config.json`
- `reasoning.txt`: Added `PHASE 19 — MARKET CONTEXT ADDITIONS` section documenting `sector_dispersion`, `recent_rejections`, `news_themes` input fields; `REGIME ASSESSMENT` instruction block; extended `RESPONSE FORMAT` JSON schema with all four new optional output fields (`regime_assessment`, `sector_regimes`, `filter_adjustments`, `active_theses`) with field-level documentation.
- All other prompt files (`review.txt`, `watchlist.txt`, `thesis_challenge.txt`, `compress.txt`) copied from v3.9.0 unchanged.

**Tests** · 28 new tests, all passing (1074 total)
- `test_technical_analysis.py`: `TestComputeSectorDispersion` (7 tests) — basic output, top-3 cap, missing symbols, ETF-only entries, roc_5d=0.0 falsy fix, multiple sectors.
- `test_strategy_traits.py`: `TestPhase19EntryGate` (5 tests) — VWAP gate yields with entry_conditions, RVOL floor from filter_adjustments, filter_adjustments=None falls back to strategy param, swing signature accepts params.
- `test_claude_reasoning.py`: `TestPhase19ReasoningResultParsing` (8 tests) — all four fields parse correctly; missing fields default to None; malformed dicts/lists handled defensively.
- `test_opportunity_ranker.py`: `TestClampFilterAdjustments` (8 tests) — values above floor pass through; values below floor clamped; None input returns None; partial dict preserved.

---

### 2026-03-27 — Phase 20: Haiku Operational Layer

**`ContextCompressor`** · *(new module)* · `intelligence/context_compressor.py`
- New class: `ContextCompressor(config, prompts_dir)`.
- `compress(all_candidates, indicators, market_data, regime_assessment, sector_regimes, max_symbols_out, cycle_id)` — ranks watchlist candidates using Haiku and returns a `CompressorResult` with an ordered symbol shortlist.
- Gate: only calls Haiku when `len(all_candidates) > max_symbols_out`. Below the threshold, falls back to deterministic composite-score sort immediately (no API call).
- Fallback on any failure (API error, timeout, parse error, no prompt template): `_fallback_sort` — deterministic direction-adjusted composite-score sort, same logic as the existing tier1 sort in `assemble_reasoning_context`.
- Symbol validation: `_parse_response` only accepts symbols that appear in `all_candidates`. Haiku cannot inject unknown symbols.
- `needs_sonnet` per-cycle guard: fires at most once per Sonnet cycle (keyed by `cycle_id`). Suppresses repeat fire if `self._last_needs_sonnet_cycle == cycle_id`.
- Helper functions `_sym(entry)` and `_attr(entry, attr, default)` handle both `WatchlistEntry` objects and plain dicts uniformly.
- `NEEDS_SONNET_REASONS` frozenset: typed extension point for trigger reasons. To add a new reason, add one string here and handle it in orchestrator.

**`CompressorResult`** · *(new dataclass)* · `intelligence/context_compressor.py`
- Fields: `symbols`, `rationale`, `notes`, `from_fallback`, `needs_sonnet`, `sonnet_reason`.
- `from_fallback=True` signals that deterministic sort was used instead of Haiku.

**`compress.txt`** · *(new prompt)* · `config/prompts/v3.10.0/compress.txt`
- Haiku prompt: given a list of candidates with key signals and Sonnet's regime context, select and rank the top `max_symbols` most actionable candidates.
- Ranking priorities: regime alignment (sector_regimes bias), signal readiness (composite_score, RSI, RVOL), catalyst freshness, tier preference, sector diversity.
- `needs_sonnet` flag with three specific typed triggers: `regime_shift`, `all_candidates_failing`, `watchlist_stale`.

**`assemble_reasoning_context` modification** · *(new param)* · `intelligence/claude_reasoning.py`
- Added `selected_symbols: list[str] | None = None` parameter.
- When provided: builds a lookup across all watchlist entries (any tier, not just tier1), selects entries in `selected_symbols` order up to `slots`, skips unknown symbols silently. Tier-2 symbols from the compressor's shortlist are included.
- When `None`: existing composite-score sort unchanged (backward compatible).
- Comment in code: `# NOTE: if position scaling is ever implemented, remove the open_position_symbols exclusion`

**`run_reasoning_cycle` modification** · *(new params + pre-screening logic)* · `intelligence/claude_reasoning.py`
- Added: `all_indicators: dict | None = None`, `regime_assessment: dict | None = None`, `sector_regimes: dict | None = None`.
- Pre-screening block before `assemble_reasoning_context`: builds `all_candidates` (all watchlist entries excluding open positions); if `self._compressor is not None and len(all_candidates) > max_symbols_out`, calls `self._compressor.compress(...)`.
- `selected_symbols` from compressor result passed to `assemble_reasoning_context`.
- `needs_sonnet=True` from compressor: logged at WARNING (note that Sonnet is already running in this cycle — Phase 21 will handle independent Haiku-triggered Sonnet calls).
- Fallback on unexpected exception: logs WARNING and proceeds without pre-screen (no crash path).
- `ClaudeReasoningEngine.__init__`: creates `self._compressor = ContextCompressor(config.claude, prompts_dir)` when `compressor_enabled=True`; `None` when disabled.

**`_last_sector_regimes` on orchestrator** · *(new state)* · `core/orchestrator.py`
- `self._last_sector_regimes: dict | None = None` added alongside `_last_regime_assessment`.
- Updated after each successful reasoning cycle when `result.sector_regimes` is non-empty.
- Passed as `sector_regimes=self._last_sector_regimes` to `run_reasoning_cycle` for Haiku to use.

**Orchestrator `run_reasoning_cycle` call** · *(new args)* · `core/orchestrator.py`
- `all_indicators=self._all_indicators` (merged dict covering all watchlist symbols).
- `regime_assessment=self._last_regime_assessment` (prior Sonnet cycle's regime).
- `sector_regimes=self._last_sector_regimes` (prior Sonnet cycle's sector regimes).

**Compressor config fields** · *(new config)* · `core/config.py`, `config/config.json`
- `ClaudeConfig.compressor_enabled: bool = True` — disables Haiku call when False; falls back to composite-score sort.
- `ClaudeConfig.compressor_model: str = "claude-haiku-4-5-20251001"` — Haiku model for pre-screening.
- `ClaudeConfig.compressor_max_symbols_out: int = 18` — matches `tier1_max_symbols`; Haiku cannot return more than this.
- `ClaudeConfig.compressor_max_tokens: int = 512` — Haiku output budget (short JSON list, not prose).

**Tests** · 34 new tests, all passing (1108 total)
- `test_context_compressor.py`: `TestHelperFunctions` (6), `TestFallbackSort` (5), `TestParseResponse` (9), `TestCompressGate` (3), `TestCompressWithMockedHaiku` (3), `TestAssembleContextSelectedSymbols` (4), `TestCompressorConfigFields` (4).

---

### 2026-03-27 — Phase 21: Durability and Regime Response

**Multi-tier watchlist pruner eviction** · *(behavior change)* · `core/orchestrator.py`
- **Spec:** evict by lowest intraday composite score when watchlist hits `watchlist_max_entries`.
- **Impl:** `_eviction_priority(entry, sector_regimes)` returns a sort key tuple `(tier_score, conflict_score, -composite)` where tier_score=0 for tier2, tier_score=1 for tier1; conflict_score=0 when direction conflicts with current sector regime, 1 otherwise. Sort ascending: tier-2 evicted first, then direction-conflicting tier-1, then lowest composite within remaining tier-1.
- **Why:** Old single-key sort evicted swing setups with deliberately low intraday composite scores (e.g. oversold mean-reversion entries). Multi-tier order preserves Claude's highest-conviction tier-1 entries and targets strategically stale entries first.

**`_clear_directional_suppression(affected_sectors)`** · *(new helper)* · `core/orchestrator.py`
- Clears entries in `_filter_suppressed` for symbols in `affected_sectors` when their suppression reason is direction-dependent: `rvol`, `composite_score`, `conviction_floor`, `defer_expired`.
- Direction-neutral reasons preserved: `fetch_failure`, `blacklist`, `no_entry`.
- `affected_sectors=None` clears all direction-dependent suppressions across every sector (used for broad panic regime change).
- **Why:** A symbol suppressed as a long candidate (e.g., repeated RVOL failures on a bullish thesis) remains blocked as a short candidate after a regime flip — a different directional thesis entirely. Clearing on regime reset lets Claude re-evaluate the symbol with the new regime context.

**`_regime_reset_build(prev_sector_regimes, new_sector_regimes, new_regime, changed_sectors, broad_regime_changed)`** · *(new method)* · `core/orchestrator.py`
- Fire-and-forget background task (`asyncio.ensure_future`) triggered when Sonnet's `regime_assessment.regime` changes OR any `sector_regimes` entry changes regime value.
- Eviction rules (applied to `_watchlist`):
  - Broad panic: evict all tier-1 swing longs without `catalyst_driven=True` flag.
  - Sector rotation: for each changed sector, evict entries whose `expected_direction` conflicts with the new sector bias (long entries in correcting/downtrend sectors, short entries in breaking_out/uptrend sectors), unless `catalyst_driven=True`.
  - Preserves entries in unchanged sectors.
- After eviction: calls `_clear_directional_suppression(affected_sectors)` then fires `run_watchlist_build(target_count=20)` to repopulate with regime-aligned candidates.
- **Why:** Addresses the core panic-day failure: on 2026-03-27, the watchlist remained long-biased while SPY dropped 3%. The regime-reset build ensures that within one reasoning cycle after a regime flip, the watchlist is rebuilt around the new regime.

**Regime change detection** · *(new orchestrator logic)* · `core/orchestrator.py`
- Added `self._prior_regime_name: str | None = None` alongside `_last_regime_assessment`.
- After each Sonnet cycle: compares new `result.sector_regimes` vs `self._last_sector_regimes` (entry-level regime value for each ETF). Any change triggers `_regime_reset_build`.
- Also compares new `result.regime_assessment.regime` vs `self._prior_regime_name`. Broad regime change triggers `_regime_reset_build` with `broad_regime_changed=True`.

**Startup persistence of regime_assessment / sector_regimes** · *(behavior change)* · `core/orchestrator.py`
- **Spec:** not specified for startup.
- **Impl:** Step 4c of startup reconciliation restores `_last_regime_assessment` and `_last_sector_regimes` from the persisted reasoning cache (`state/reasoning_cache.json`) if cache is not expired. Uses top-level `_result_from_raw_reasoning` import (no inline imports — see Phase 20 bug note).
- **Why:** Without restoration, the first post-restart medium loop runs Haiku with no regime context. Haiku cannot align candidates with sector biases until Sonnet fires, which may not happen for several minutes.

**Position thesis monitoring** · *(new logic — later superseded, see 2026-03-31 entry)* · `intelligence/context_compressor.py`, `core/orchestrator.py`
- `ContextCompressor.check_position_theses(positions, active_theses, indicators, cycle_id)`: for each open position with a matching `active_theses` entry, evaluates each `thesis_breaking_conditions` string against live `indicators` using `_condition_met`.
- `_condition_met(condition, signals, daily)`: parses condition strings of the form `field op value` (e.g. `daily_trend becomes downtrend`, `rsi_14d < 35`). Only handles simple `key op value` patterns — narrative/event conditions silently return `False`. **This was found to fail on 87/87 production conditions in the 2026-03-31 session and was replaced.**
- Medium loop Step 6: calls `check_position_theses` each cycle; if breach, fires `_run_claude_cycle("thesis_breach")` immediately.
- Per-cycle guard (inherited from Phase 20): `_last_needs_sonnet_cycle` prevents re-triggering within the same Sonnet cycle.

**Regime-aware universe scanner** · *(new params)* · `intelligence/universe_scanner.py`
- `get_top_candidates` new params: `sector_regimes`, `regime_assessment`, `sector_map`.
- Broad panic detection: `regime_assessment.regime == "risk-off panic"` → doubles `min_price_move_pct_for_candidate` floor to suppress noise.
- Correcting sectors: for each ETF with `regime in ("correcting", "downtrend")`, adds `day_losers` Yahoo Finance screener results to the universe (deduped against existing + exclude/blacklist). Short-side candidates from those sectors surface alongside the existing most_actives/day_gainers universe.
- `effective_price_move_floor` variable: used in filter step instead of config value directly, so panic mode can raise the floor without mutating config state.

**`watchlist.txt` regime-reset instructions** · *(prompt addition)* · `config/prompts/v3.10.0/watchlist.txt`
- Added `REGIME-RESET BUILD INSTRUCTIONS` section: instructs Claude to prioritize candidates aligned with the *new* regime when this build is triggered by a regime change.
- Eviction-aligned direction instructions: `expected_direction` field set to `"short"` for correcting setups, `"long"` for breakout setups.

**Tests** · 39 new tests, all passing (1147 total)
- `test_phase21.py`: `TestMultiTierPruner` (5), `TestClearDirectionalSuppression` (7), `TestRegimeResetEvictionLogic` (6), `TestUniverseScannerRegimeAware` (4), `TestPositionThesisMonitoring` (7), `TestConditionMet` (10).

---

## Cross-References

- [[ozy-drift-log]] — Active drift log (new entries)
- [[ozy-drift-log-eras-18]] — Previous era (watchlist intelligence)
- [[ozy-drift-log-eras-22-23]] — Next era (split-call & workflow)
- [[ozy-doc-index]] — Full routing table
