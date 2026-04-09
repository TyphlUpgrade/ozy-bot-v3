---
title: "Ozy Drift Log — Era 18"
tags: [ozymandias, drift-log, archive, phase-18, watchlist-intelligence]
category: reference
ceiling_override: frozen-archive
frozen: true
created: 2026-04-09
updated: 2026-04-09
---

# Ozy Drift Log — Era 18 (Watchlist Intelligence)

Frozen archive of spec deviations from Post-Phase-17 Bug Fixes and Phase 18 sessions (March 23-27).
For the active drift log and filing rules, see [[ozy-drift-log]].

---

## Post-Phase-17 Bug Fixes & Hardening (2026-03-23)

**`_apply_position_reviews` stale-portfolio race condition** · *(bug fix)* · `core/orchestrator.py`
- **Bug:** `_apply_position_reviews` received `portfolio` loaded at slow-loop cycle start (before the ~39s Claude API call). Concurrent medium-loop cash-sync saves overwrote disk state, so review adjustments (stop/target updates, notes) were silently lost.
- **Fix:** Removed `portfolio` parameter from `_apply_position_reviews`. Method now loads a fresh portfolio snapshot internally before applying changes. Call site updated.
- **Tests:** Updated all test call sites to drop the `portfolio` argument; 3 tests that used in-memory `PortfolioState` without persisting to disk were fixed to use `_set_portfolio`.

**Direction normalization for legacy `"sell_short"` states** · *(bug fix)* · `core/state_manager.py`
- **Bug:** `_from_dict_position` deserialized `intention.direction` verbatim. State files written before Phase 12 stored `"sell_short"`. `is_short()` only matches `"short"`, so un-normalized values broke hard stop, ATR direction tracking, and P&L.
- **Fix:** Added normalization: `"sell_short"` → `"short"` on load. All other values pass through unchanged.
- **Tests:** Added `test_load_portfolio_normalises_sell_short_direction` regression test.

**Session-level filter suppression** · *(new feature)* · `core/orchestrator.py` + `intelligence/opportunity_ranker.py` + `intelligence/claude_reasoning.py` + `core/config.py`
- **Spec:** *(not defined)*
- **Impl:** `_filter_suppressed: dict[str, str]` on orchestrator. After a symbol fails hard filters `max_filter_rejection_cycles` consecutive times (default 3), it is added to `_filter_suppressed` with the rejection reason. Suppressed symbols are:
  1. Skipped before the ranker runs (pre-filter in `rank_opportunities`)
  2. Passed as `session_suppressed` context to Claude each cycle so Claude stops nominating them
- **Config:** `scheduler.max_filter_rejection_cycles: int = 3` (already present from Phase 17 — now consumed by this path too)
- **Why:** Phase 15's `rejection_count` context relied on Claude self-correcting; mechanical gate needed to stop re-nomination of RVOL/volume failures every cycle (e.g., AAPL, JPM).

**Dead `== "sell_short"` fallback removed** · *(cleanup)* · `core/orchestrator.py` + `execution/risk_manager.py`
- **Impl:** Two residual legacy checks (`_is_short_dir = _pos_is_short or direction == "sell_short"` style) removed. Direction normalization on load makes these dead.

---

## Phase 18 — Watchlist Intelligence (2026-03-23)

**Dynamic universe pipeline** · *(new feature)* · `intelligence/universe_fetcher.py` (new) + `intelligence/universe_scanner.py` (new)
- **Spec:** `phases/18_watchlist_intelligence.md`
- **Impl:** `UniverseFetcher` merges Source A (Yahoo Finance screener: `most_actives` 50 + `day_gainers` 25) and Source B (S&P 500 + Nasdaq 100 from Wikipedia, 24h cache). `UniverseScanner` fetches bars + runs TA in parallel (semaphore-bounded), filters by `bars_available < 5` and `min_rvol_for_candidate`, sorts by RVOL descending, enriches top symbols with news + earnings calendar, returns top `n` candidates.
- **Candidate dict schema:** `{symbol, rvol, technical_summary, composite_score, price, recent_news, earnings_within_days}`
- **Config:** `universe_scanner` section — `enabled`, `scan_concurrency=20`, `max_candidates=50`, `min_rvol_for_candidate=0.8`, `cache_ttl_min=60`

**`SearchAdapter`** · *(new feature)* · `data/adapters/search_adapter.py` (new)
- **Spec:** `phases/18_watchlist_intelligence.md`
- **Impl:** Wraps Brave Search API. `enabled` = `bool(api_key)`. All failures return `[]`. `_fetch` uses `urllib.request`. Credentials injected via `BRAVE_SEARCH_API_KEY` from credentials file in `_load_credentials`.
- **Config:** `search` section — `max_searches_per_build=3`, `result_count_per_query=5`

**`call_claude_with_tools` + `_call_claude_raw`** · *(new feature)* · `intelligence/claude_reasoning.py`
- **Spec:** `phases/18_watchlist_intelligence.md`
- **Impl:** `call_claude_with_tools(prompt_template, context, tools, tool_executor, max_tool_rounds=3)` — multi-turn loop. On `tool_use` stop: calls `tool_executor(tool_name, tool_input)`, appends result, loops. Exhausted rounds → forced final call with `tool_choice={"type": "none"}`. `_call_claude_raw` is an internal helper with full 529/5xx/RateLimitError retry logic returning raw response (no Gemini fallback for tool calls).
- **`_WEB_SEARCH_TOOL`:** Class variable with Brave Search tool definition.

**`run_watchlist_build` updated** · *(interface change)* · `intelligence/claude_reasoning.py`
- **Spec:** *(extended beyond spec)*
- **Impl:** Added `candidates: list[dict] | None` and `search_adapter` parameters. Routes to `call_claude_with_tools` when `search_adapter.enabled`, else `call_claude`. `{candidates}` added to context dict.

**Orchestrator Phase 18 wiring** · *(new feature)* · `core/orchestrator.py`
- **Impl:** `UniverseScanner` + `SearchAdapter` instantiated in `_startup`. Added `_last_universe_scan: list[dict]` + `_last_universe_scan_time: float` for session-level cache. In `_run_claude_cycle`: when `watchlist_small` is in triggers, runs universe scan (respects `cache_ttl_min`), calls `run_watchlist_build` with candidates + search adapter, applies results. If `watchlist_small` is the sole trigger, returns early without calling `run_reasoning_cycle` or updating `last_claude_call_utc`.

**Prompt version bump** · *(new version)* · `config/prompts/v3.6.0/watchlist.txt` (new)
- **Impl:** Copied from `v3.5.0`, added `{candidates}` slot and instructions for using screener data and `web_search` tool at watchlist build time.

**`scripts/reset_watchlist.py`** · *(new feature)* · `scripts/reset_watchlist.py` (new)
- **Spec:** `phases/18_watchlist_intelligence.md`
- **Impl:** CLI tool. Positional SYMBOL args or `--empty` flag; `--tier`, `--strategy`, `--dry-run` options. Uses `StateManager` for atomic writes and schema validation. Prints current and new watchlist; warns about `watchlist_small` trigger when emptying.

**`pytest.ini` testpaths updated** · *(build fix)*
- **Fix:** Added `tests` to `testpaths` (was `ozymandias/tests` only). Phase 18 tests live in root `tests/`.

**Test count:** 1077 (up from 978 — 99 new tests across 4 new test files)


---

### 2026-03-24 Paper Session Fixes (LOG-FINDING-A, LOG-FINDING-B)

**Strategy-specific limit order timeout** · *(operational fix)* · `execution/fill_protection.py`, `core/orchestrator.py`, `core/config.py`, `core/state_manager.py`
- **Problem:** STX and GRMN swing limit orders were cancelled after 300s (momentum timeout). Swing entries are wider-spread and need more fill time than momentum scalps.
- **Impl:** `OrderRecord.timeout_seconds` (already existed, was unused) is now wired into `get_stale_orders` — effective timeout = `order.timeout_seconds` if > 0, else `timeout_sec` global fallback (sentinel pattern). In `_medium_try_entry`, entry orders set `timeout_seconds` based on strategy via a lookup dict (`_strategy_timeouts`). To add a new strategy timeout, add one entry to that dict.
- **Config:** `scheduler.swing_limit_order_timeout_sec = 1200` (20 min). Momentum uses existing `limit_order_timeout_sec = 300`.
- **`OrderRecord.timeout_seconds` default changed:** 60 → 0 (0 = use global). Deserialization default updated to match.
- **`get_stale_orders` default changed:** `timeout_sec=60` → `timeout_sec=300` to match the global config default.

**Brave Search 429 retry** · *(operational fix)* · `data/adapters/search_adapter.py`, `core/config.py`
- **Problem:** Two 429s during watchlist builds exhausted tool-use rounds, cutting Claude's research short.
- **Impl:** `SearchAdapter` now accepts `retry_count` and `retry_sec` params. `_fetch_with_retry` wraps `_fetch` with retry logic for `urllib.error.HTTPError` code 429 only. Non-429 errors re-raised immediately. All retries exhausted → `search()` still returns `[]` (graceful degradation unchanged). The 3-round `max_searches_per_build` cap is unaffected.
- **Config:** `search.search_429_retry_count = 2`, `search.search_429_retry_sec = 5.0`. Passed to `SearchAdapter.__init__` in orchestrator `_startup`.

**LOG-FINDING-C (WMT fill count mismatch)** · *(deferred)* · N/A
- **Status:** The "local=16 vs broker=6" log message was not found in the codebase. It may have come from an external debug tool or a now-removed log statement. No code change made. Monitor in future sessions.

**Test count:** 1002 (stable — 6 new tests in `test_fill_protection.py`, 6 in new `test_search_adapter.py`, replaced some previously-counted tests from root `tests/` dir)

### 2026-03-24 Observability Additions (Finding 4 + Finding 6)

**No-opportunity streak WARN** · *(new observability)* · `core/orchestrator.py`
- **Problem:** 8 consecutive empty medium loops produced no diagnostic output about *why* no candidates were passing. The operator couldn't tell whether the watchlist was stale, a specific gate was too tight, or market conditions were genuinely unfavorable.
- **Impl:** `self._no_opportunity_streak` counter increments each medium loop cycle when `len(ranked) == 0`. At `no_opportunity_streak_warn_threshold` (default 8) consecutive misses, a WARNING is logged with a gate-breakdown summary: rejection counts grouped by gate category (conviction_floor, technical_score_floor, rvol_gate, rsi_gate, vwap_gate, entry_conditions, pdt_guard, market_hours, etc.). "already_open" rejections excluded from the breakdown (expected noise while holding positions). Counter resets when ranked candidates appear OR when a fresh Claude reasoning cycle fires.
- **`_rejection_gate_category(reason)`:** Module-level helper in `orchestrator.py` mapping rejection-reason strings to short labels. Extension point: add one entry per new gate category.
- **Config:** `scheduler.no_opportunity_streak_warn_threshold = 8`

**Ranker-rejection journal records** · *(new observability)* · `core/orchestrator.py`
- **Problem:** Rejections by `min_conviction_threshold` and `min_composite_score` were only visible in INFO logs and `_recommendation_outcomes` (in-memory). No cross-session calibration data.
- **Impl:** Two `record_type="rejected"` entries appended to `trade_journal.jsonl`:
  1. **`conviction_floor`** — written in the `rank_result.rejections` loop, on `rejection_count == 1` (first occurrence this session). Includes: `symbol`, `strategy`, `conviction`, `conviction_threshold`, `reason`.
  2. **`composite_score_floor`** — written in `_medium_try_entry` every time the gate fires (these pass the ranker hard filters, so they don't appear in `rank_result.rejections`). Includes: `symbol`, `strategy`, `conviction`, `composite_score`, `composite_score_floor`.
- Both gate values and actual values are included so calibration analysis can identify marginal near-misses without reading config.
- `load_recent` and `compute_session_stats` already exclude `record_type != "close"` records, so these don't affect Claude's execution context.

---

### 2026-03-25 Paper Session Fixes

**Defer expiry now uses session suppression** · *(bug fix)* · `core/orchestrator.py`
- **Bug:** When a symbol's `_entry_defer_counts` reached `max_entry_defer_cycles`, the code set `top.entry_conditions = {}` on the live `ScoredOpportunity` object. This was a no-op: `top` is rebuilt from the reasoning cache on every medium loop, restoring the original `entry_conditions`. The opportunity continued being ranked and attempted indefinitely (PFE accumulated 18 consecutive defers today before being cleared by a watchlist rotation).
- **Fix:** On expiry, symbol is now added to `_filter_suppressed` with reason `"stale thesis gate: entry conditions expired after N consecutive defers"`. This immediately prevents the ranker from evaluating it and stops Claude from re-nominating it via the session suppression context. Claude reconsiders at the next watchlist build.
- **Log message updated:** Was "awaiting fresh Claude call" (misleading — no such trigger existed). Now "suppressed for session after N consecutive defers (stale thesis gate)".

**`dead_zone_exempt` trait on Strategy ABC** · *(new feature)* · `strategies/base_strategy.py`, `strategies/swing_strategy.py`, `execution/risk_manager.py`, `core/orchestrator.py`
- **Problem:** The dead zone (11:30–14:30 ET) blocked all new entries regardless of strategy. Swing entries have multi-day theses and are unaffected by the noon lull the dead zone was designed for. Today MRK (conviction 0.75) was blocked for 2+ hours by dead zone then pruned from the watchlist before it could enter.
- **Impl:** `dead_zone_exempt: bool` property on `Strategy` ABC (default `False` — safe). `SwingStrategy.dead_zone_exempt = True`. `validate_entry` in `risk_manager.py` accepts `dead_zone_exempt: bool = False` and passes it to `_check_market_hours`, which skips the dead zone gate when set. Orchestrator passes `strategy_obj.dead_zone_exempt` at the `validate_entry` call site. Same extension pattern as `blocks_eod_entries`.
- **To add a new strategy:** set `dead_zone_exempt` in the concrete class — no other file changes needed.

**Universe scanner OR-gate filter** · *(new feature)* · `intelligence/universe_scanner.py`, `core/config.py`, `config/config.json`
- **Problem:** RVOL-only filtering excluded symbols with large price moves but low volume (shorts, low-float movers, pre-market gappers). Directionally it was also biased toward long setups since RVOL doesn't capture short-side activity well.
- **Impl:** Filter is now `volume_path OR move_path`. `via_rvol = rvol >= min_rvol_for_candidate`. `via_move = abs(roc_5) >= min_price_move_pct_for_candidate` (default 1.5%). Both paths are direction-agnostic. Sort remains RVOL-descending; price-move-only candidates appear at the bottom of the candidate pool. Log line updated to show both path counts.
- **Config:** `universe_scanner.min_price_move_pct_for_candidate = 1.5`
- **9 new tests** in `tests/test_universe_scanner.py`.

**Watchlist build re-fire suppression on failure** · *(bug fix)* · `core/orchestrator.py`
- **Problem:** When a watchlist build failed (Claude 529, network error), `last_watchlist_build_utc` was not updated. On the next slow loop check (~60s), elapsed time was ∞ → `watchlist_stale` fired again → build failed again → 12 cascading failures over 25 minutes during today's 529 outage.
- **Fix:** In the `except` block, `last_watchlist_build_utc` is back-dated to `now - (interval_sec - probe_sec)`, so elapsed time at the next check equals `probe_sec`. This aligns the re-fire cadence with the circuit breaker's probe interval (~10 min) instead of every 60s.

**Prompt version v3.8.0** · *(new version)* · `config/prompts/v3.8.0/` (new), `config/config.json`
- Copied from v3.7.0. Content unchanged — reserved as a clean version boundary for the rsi_max swing entry instruction (pending discussion).


---

### 2026-03-27 — Two-Profile Indicator Layer + Watchlist Pipeline Fixes

**Root cause: macro panic exits on swing positions** · *(bug fix)* · `intelligence/technical_analysis.py`, `core/orchestrator.py`, `intelligence/claude_reasoning.py`
- **Problem:** `_build_market_context` read `_market_context_indicators` (5-min bars) to produce `spy_rsi`. When SPY RSI dropped to 35 intraday, Claude exited multi-day swing positions citing bearish market structure — reasoning categorically inappropriate for a 5+ ATR multi-day thesis. DPZ exited this way after < 4 hours despite the 4h hold guard (guard only prevents `_apply_position_reviews` from acting on review output; `evaluate_position` in the medium loop was a separate path that bypassed it).
- **Fix:** Two-profile indicator layer: intraday signals unchanged for momentum; daily-bar signals added separately for swing reviews and macro regime context.

**`generate_daily_signal_summary(symbol, df)`** · *(new function)* · `intelligence/technical_analysis.py`
- Returns `{}` for < 20 bars or NaN RSI. Signals: `rsi_14d`, `price_vs_ema20`, `price_vs_ema50` (50+ bars only), `ema20_vs_ema50` (50+ bars only), `daily_trend` (uptrend/downtrend/mixed based on EMA20/EMA50 alignment), `roc_5d`, `volume_trend_daily` (expanding/contracting/neutral: 5d avg vs 20d avg ±10%), `macd_signal_daily`.
- Uses existing `compute_rsi`, `compute_ema`, `compute_macd` — no new dependencies.

**`_daily_indicators`** · *(new orchestrator state)* · `core/orchestrator.py`
- `dict[str, dict]` populated each slow loop in `_run_claude_cycle` after `_build_market_context`.
- Fetches daily bars for: SPY, QQQ, all open swing position symbols.
- Uses `asyncio.gather(return_exceptions=True)` — failures logged at WARNING, silently skipped.
- Cache key `bars:SYMBOL:1d:3mo` is distinct from intraday keys — no cache collision.

**`spy_daily` / `qqq_daily` in market context** · *(interface change)* · `core/orchestrator.py`
- `_build_market_context` now appends `spy_daily` and `qqq_daily` keys from `_daily_indicators` when available. Absent during first slow loop (before daily fetch runs) — keys simply omitted.
- Co-exists with existing `spy_rsi` (intraday) — both present simultaneously; different timeframes.

**`daily_signals` in swing position context** · *(interface change)* · `intelligence/claude_reasoning.py`
- `assemble_reasoning_context` accepts `daily_indicators: dict[str, dict] | None = None`.
- Swing positions with non-empty `daily_indicators[symbol]` get `pos_entry["daily_signals"] = ...`.
- Momentum positions receive no `daily_signals` — intraday is correct timeframe for them.
- `run_reasoning_cycle` signature extended with `daily_indicators` param; passed through from orchestrator.

**Swing intraday gates disabled** · *(behaviour change)* · `strategies/swing_strategy.py`
- `apply_entry_gate`: `trend_structure` block commented out (wrong timeframe for swing entries). Was blocking POOL and other valid oversold setups when 5-min trend was bearish.
- `evaluate_position`: `bearish_aligned` exit commented out. Was bypassing the 4h hold guard via `_medium_evaluate_positions` (which calls `evaluate_position` directly, outside `applicable_override_signals()` frozenset).
- `suggest_exit`: `bearish_aligned` market exit commented out.
- Code preserved (not deleted) for future re-entry/repositioning logic.
- **Tests updated:** `test_strategies.py` (3 renames), `test_strategy_traits.py` (2 renames + 1 removal), `test_opportunity_ranker.py` (2 renames). All 1046 tests passing.

**Prompt v3.9.0** · *(new version)* · `config/prompts/v3.9.0/` (new), `config/config.json`
- `reasoning.txt`: `TWO-PROFILE MARKET CONTEXT` section explaining intraday vs. daily signal usage; `OPEN POSITIONS — DAILY SIGNALS` section weighting `daily_signals` over intraday for swing reviews; swing entry restrictions: `daily_trend == "downtrend"` → near-prohibited swing longs (require catalyst_driven + conviction ≥ 0.70 + named near-term event); `daily_trend == "mixed"` → conviction cap 0.65 + 10% equity size cap.
- `review.txt`: `TIMEFRAME GUIDANCE FOR SWING POSITIONS` block instructing Claude to weight `daily_signals` over intraday `market_context` for swing reviews. Intraday weakness alone is not a valid exit reason for swing positions.
- `watchlist.txt`: `spy_daily.daily_trend` calibration instructions — favor short candidates in downtrend regime; raise bar for swing long additions in mixed regime.
- `review.txt`, `watchlist.txt`, `thesis_challenge.txt` copied from v3.8.0 (were missing — caused "no such file" error on startup).

---

### 2026-03-27 — Watchlist Pipeline Fixes

**`tier1_max_symbols` raised 8 → 18** · *(config change)* · `config/config.json`
- **Problem:** With 3 open positions, Claude was seeing only 5 watchlist candidates per reasoning cycle out of 33 tier-1 symbols (15%). New short candidates from daily watchlist builds were invisible to Claude's reasoning — the 50-candidate screener runs at 9:30 ET, Claude reasons at 9:34 ET, newly-added symbols may not have indicators yet and score 0, ranking out of the visible 5. Token trim guard in `assemble_reasoning_context` is the real safety net; 8 was an overly conservative cap set before Phase 18 grew the watchlist.
- **Fix:** Raised to 18. With 3 positions, Claude now sees ~15 candidates (~45% of watchlist) per cycle.

**`watchlist_max_entries` raised 40 → 60 + `watchlist_build_target` 20 → 8** · *(config + code change)* · `config/config.json`, `core/config.py`, `intelligence/claude_reasoning.py`, `core/orchestrator.py`
- **Problem:** The size-cap pruner in `_apply_watchlist_changes` was evicting ~20 symbols every watchlist build. Mechanism: `target_count=20` → Claude adds 20 new symbols → 40+20=60 entries → pruner fires to restore cap of 40, evicting 20 lowest intraday composite scores. Pruning by intraday composite score is a category error for swing setups (e.g., POOL added for RSI 29 oversold thesis scores low intraday — gets immediately evicted). Net result: watchlist churned ~50% of its contents daily with no thesis continuity.
- **Fix 1:** `watchlist_max_entries` raised 40 → 60. Builds of ≤ 8 new symbols no longer trigger pruning.
- **Fix 2:** `watchlist_build_target` config key added to `ClaudeConfig` (default 8). Replaces hardcoded `target_count=20` in `run_watchlist_build`. Forces Claude to add its 8 highest-conviction picks per build rather than a wholesale refresh. Wired through orchestrator call site.
- **Fix 3:** Watchlist prompt framing: "TARGET WATCHLIST SIZE: N tickers" → "ADD UP TO N NEW TICKERS THIS BUILD". Disambiguates additive vs. rebuild semantics — the old framing caused Claude to treat the watchlist build as a full replacement rather than incremental addition.
- **`ClaudeConfig.watchlist_max_entries` default** updated 30 → 60 in `core/config.py`.

---

## Cross-References

- [[ozy-drift-log]] — Active drift log (new entries)
- [[ozy-drift-log-eras-15-17]] — Previous era (entry conditions & enrichment)
- [[ozy-drift-log-eras-19-21]] — Next era (Sonnet/Haiku/Durability)
- [[ozy-doc-index]] — Full routing table
