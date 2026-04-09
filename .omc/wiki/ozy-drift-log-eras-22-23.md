---
title: "Ozy Drift Log — Eras 22-23"
tags: [ozymandias, drift-log, archive, phases-22-23, split-call, agentic-workflow]
category: reference
ceiling_override: frozen-archive
frozen: true
created: 2026-04-09
updated: 2026-04-09
---

# Ozy Drift Log — Eras 22-23 (Split-Call & Workflow)

Frozen archive of spec deviations from Phases 22-23, post-phase fixes, and Agentic Workflow signal wiring (March 28 - April 2026).
For the active drift log and filing rules, see [[ozy-drift-log]].

---

### 2026-03-28 — Prompt v3.10.1: ContextCompressor rationale removal

**Bug fix** · `config/prompts/v3.10.1/compress.txt`
- **Problem:** Haiku was truncating its response mid-JSON. The `rationale` dict (~25 tokens per symbol × 18 symbols) consumed ~450 of the 512-token budget before `notes`/`needs_sonnet` could be written, leaving an unclosed JSON object. All four parse stages in `_parse_response` failed, triggering the fallback sort every cycle.
- **Fix:** Removed `rationale` from the compress.txt response schema. `_parse_response` never read it — only `selected_symbols`, `needs_sonnet`, and `sonnet_reason` are consumed. The field was dead output.
- **Prompt version bumped:** `v3.10.0` → `v3.10.1`. All other prompt files (`reasoning.txt`, `review.txt`, `watchlist.txt`, `thesis_challenge.txt`) copied unchanged.
- **Token budget:** `compressor_max_tokens` left at 512. Without rationale, actual output is ~120 tokens (18 symbols + notes + 2 bool fields), giving substantial headroom.
- **Debug improvement added in same session:** `_parse_response` now logs `raw=<first 500 chars>` at WARNING level on parse failure, enabling root-cause diagnosis.

---

### 2026-03-28 — Cross-session trade memory (last_view) + max_tokens fix

**`WatchlistEntry.last_view`** · *(new field)* · `core/state_manager.py`
- New optional fields: `last_view: Optional[str]` and `last_view_date: Optional[str]`.
- `last_view` is a single synthesised string: `"{considered_reason} | blocked: {rejection_reason}"` for rejected symbols, or `"Proposed {action} {strategy} — {reasoning[:80]}"` for proposed entries. Capped at 120 chars.
- `last_view_date` is ISO date of last update. Views older than `last_view_max_age_days` (default 7) are excluded from context.
- Deserialization: `_from_dict_watchlist_entry` reads both via `d.get(...)` — backward compatible with existing state files that lack these fields.
- **Why:** Each session previously started cold — Claude re-derived its view of every symbol from raw TA alone. `last_view` carries the prior cycle's reasoning (the bullish thesis and the specific blocker) across restarts.

**Orchestrator writeback** · *(new logic)* · `core/orchestrator.py`
- After each successful reasoning cycle, iterates `result.rejected_opportunities` and `result.new_opportunities` and writes synthesised `last_view` + `last_view_date` to matching `WatchlistEntry` objects.
- Persisted automatically via the existing `save_watchlist` call in `_apply_watchlist_changes`, which is always invoked at the end of every successful reasoning cycle.

**Context assembly** · *(new behaviour)* · `intelligence/claude_reasoning.py`
- `assemble_reasoning_context` includes `last_view` in each tier-1 symbol dict when present and `last_view_date >= cutoff` (now − `last_view_max_age_days` days).
- ~30 tokens per symbol × 18 symbols = ~540 additional input tokens per cycle when all views are populated.

**`max_tokens_per_cycle` raised to 8192** · `core/config.py`, `config/config.json`
- Was 4096 — too small for 18 tier-1 symbols + Phase 19 fields. Caused `stop_reason=max_tokens` truncation every cycle, breaking JSON parse and producing 0 candidates.
- Set to 8192 (Sonnet model maximum). Treated as a hard ceiling that should never be reached in normal operation, not an active budget. Rate limit protections (backoff, circuit breaker) handle availability failures — token limits handle a completely different failure mode and serve no value when set below the model max.

**New config key:** `ClaudeConfig.last_view_max_age_days: int = 7`

---

### 2026-03-30 — Pre-market warmup + Claude retry trigger fix

**`pre_market_warmup` trigger** · *(new)* · `core/orchestrator.py`, `core/market_hours.py`, `core/config.py`
- New `get_next_market_open()` in `market_hours.py`: pure datetime arithmetic (no broker call) — walks forward from now to the next NYSE trading day at 09:30 ET, respecting weekends and `_NYSE_HOLIDAYS`.
- New `_is_pre_market_warmup()` on orchestrator: True when within `pre_market_warmup_min` minutes of the next open and `bypass_market_hours=False`.
- `pre_market_warmup` trigger fires once per session (guarded by `SlowLoopTriggerState.last_warmup_session_date`) when entering the warmup window.
- Medium loop gate changed from `_is_market_open()` to `_is_market_open() or _is_pre_market_warmup()` — allows TA fetching and indicator seeding during the warmup window.
- Steps 5 & 6 (entries and `_medium_evaluate_positions`) remain gated on `_is_market_open()` — no orders placed during warmup.
- Slow loop gate similarly updated — Claude reasoning allowed during warmup window.
- **Effect:** bot can be started hours before open. At `pre_market_warmup_min` before 9:30 (default 10 min), one Sonnet cycle fires and warms the cache. At open, fresh candidates are ready within seconds of the first medium loop tick.
- New config key: `SchedulerConfig.pre_market_warmup_min: int = 10`

**`claude_retry_pending` trigger** · *(new)* · `core/orchestrator.py`
- `SlowLoopTriggerState.claude_retry_pending: bool` set by `_handle_claude_failure`.
- `_check_triggers` fires `claude_retry` trigger immediately after backoff expires, bypassing `time_ceiling` and `no_previous_call`.
- **Why:** when `last_claude_call_utc` is restored from a prior-session cache at startup, a Claude failure can leave the bot waiting up to 60 minutes for `time_ceiling` to fire — `no_previous_call` doesn't trigger because the timestamp is not None. Observed in session 2026-03-28T03:11 (8+ empty medium loops, no retry).
- New field: `SlowLoopTriggerState.claude_retry_pending: bool = False`

---

### 2026-03-28 — Context pruning + configurable API timeout

**Removed `news_themes`** · `core/orchestrator.py` `_build_market_context`
- Removed the Phase 19 `news_theme_synthesis` block that aggregated `WatchlistEntry.reason` strings by sector ETF.
- **Why:** `news_themes` was derived from the same `reason` field already visible to Sonnet via `watchlist_tier1[].reason`. Pure duplication adding ~200–400 input tokens with no additional information.

**Filter `watchlist_news` to evaluated symbols only** · `intelligence/claude_reasoning.py` `assemble_reasoning_context`
- `_build_market_context` fetches news for all tier-1 symbols (up to 35) before Haiku runs. After Haiku selects 18, news for the excluded ~17 symbols was still sent to Sonnet.
- News is now filtered in `assemble_reasoning_context` to symbols in `watchlist_tier1` (the Haiku-selected set) plus open positions. Excluded symbols' news cannot inform reasoning about entries that won't be considered.
- Saves ~1,000–2,000 input tokens per cycle.

**Removed 7 unused `ta_readiness` fields** · `intelligence/claude_reasoning.py` `assemble_reasoning_context`
- Excluded from `ta_readiness` per symbol: `rsi_divergence`, `roc_deceleration`, `roc_negative_deceleration`, `bollinger_position`, `bb_squeeze`, `avg_daily_volume`, `vol_regime_ratio`.
- None of these have corresponding `entry_conditions` schema keys in `reasoning.txt`. Sonnet cannot use them as structured gates. Saves ~500–800 input tokens per cycle.

**Configurable API call timeout** · `intelligence/claude_reasoning.py`, `core/config.py`, `config/config.json`
- Timeout was hardcoded at 120s in three places (`call_claude`, tools call, Gemini fallback).
- Root cause of 120s timeouts: Sonnet generating 6,000–7,000 output tokens can take 150–180s. Timeout fired before response completed, producing 0 candidates — identical failure mode to truncation.
- New config key: `ClaudeConfig.api_call_timeout_sec: float = 200.0`. All three `asyncio.wait_for` calls now use this value.
- Token usage logging promoted from DEBUG to INFO in the primary call path.

---

### 2026-03-30 — Phase 22: Split-Call Reasoning Architecture + Graceful Degradation

**Root cause:** Monolithic `run_reasoning_cycle` combined position reviews and opportunity discovery in one Claude call. With 10–12 open positions + Phase 19-21 context fields, input tokens grew to ~28K. The 8192-token output ceiling caused `stop_reason=max_tokens` truncation and skipped cycles — failing exactly when AI guidance is most needed.

**Split-call architecture** · `intelligence/claude_reasoning.py`, `core/orchestrator.py`, `config/prompts/v3.10.1/`
- Position reviews now run as a separate compact Call A (`position_reviews.txt`, 2048 max_tokens).
- Opportunity discovery runs as Call B (`reasoning.txt`, 8192 max_tokens, compact position summary only).
- Call A failure is non-fatal: positions continue under quant rules; bot continues to Call B.
- Results merged into a single `ReasoningResult` before downstream processing (no change to consumers).
- Context JSON for Call B excludes `daily_signals` from positions (compact summary only), saving ~40% of per-position token cost.
- New config: `split_reasoning_enabled: bool = True` (kill switch), `review_call_max_tokens: int = 4096`, `review_call_verbose: bool = False`.
- When `review_call_verbose=True`, position review prompt requests full prose (stop-adjustment rationale, explicit bear case). Default compact = two sentences max.

**New prompt files:**
- `position_reviews.txt`: compact batch review — `action`, `thesis_intact`, `updated_reasoning`, optional `adjusted_targets`. No watchlist candidates or regime context.
- `emergency_reasoning.txt`: Tier 3 (Haiku) prompt — `new_opportunities` + `rejected_opportunities` only, max 3 entries, conservative defaults.

**Graceful degradation tiers (opportunity call only)** · `core/orchestrator.py`, `intelligence/claude_reasoning.py`, `core/config.py`
- Tier 1 (default): Sonnet, 18 symbols, 8192 tokens, full context.
- Tier 2: Sonnet, 8 symbols, 4096 tokens, drops `last_view` + `sector_dispersion`.
- Tier 3: Haiku, 5 symbols, 1024 tokens, emergency prompt, bypasses ContextCompressor.
- Downgrade triggers: timeout → drop tier after 2 consecutive failures; 529 overload → jump to Tier 3 directly; 429/other → backoff only, no tier change.
- Upgrade: time-based probe after `tier_upgrade_probe_min` (15 min) since last degradation; success confirms tier restore, failure resets probe timer.
- Every slow-loop Claude call logs `[Tier 1 — Sonnet full]`, `[Tier 2 — Sonnet reduced]`, or `[Tier 3 — Haiku emergency]`.
- New config: `reasoning_tier2_max_symbols: 8`, `reasoning_tier3_max_symbols: 5`, `reasoning_tier2_max_tokens: 4096`, `reasoning_tier3_max_tokens: 1024`, `reasoning_tier3_model: "claude-haiku-4-5-20251001"`, `tier_downgrade_failures: 2`, `tier_upgrade_probe_min: 15`.

**`call_claude` model override** · `intelligence/claude_reasoning.py`
- `call_claude` accepts new `model_override: str | None = None` parameter.
- Uses `model_override or self._claude_cfg.model` in `messages.create()`, enabling Haiku emergency calls without changing the primary model config.

**`reasoning.txt` modification** · `config/prompts/v3.10.1/reasoning.txt`
- Added `{position_review_notice}` template variable injected before INSTRUCTIONS.
- When split mode is active, this instructs Claude not to produce `position_reviews` output and that positions are shown as a compact summary only.

---

### 2026-03-31 — Thesis Monitoring Rewrite: Haiku-Based Async Evaluation

**Root cause:** Phase 21's `_condition_met()` deterministic evaluator failed silently on 87/87 thesis-breaking conditions in the 2026-03-31 production session. Claude writes conditions as natural-language sentences describing catalysts (e.g. `"Iran ceasefire announced — removes geopolitical risk premium from energy"`). The regex parser only handled `key op value` patterns; all geopolitical, event-driven, and narrative conditions returned `False` without any breach signal. Thesis breach detection was effectively disabled for the entire session.

**Fix: `check_position_theses()` rebuilt as async Haiku call** · `intelligence/context_compressor.py`
- **Old:** Synchronous method calling `_condition_met()` for each condition string via regex parser.
- **New:** Async method making a dedicated Haiku API call (`max_tokens=128`, 30s timeout). Haiku receives enriched payload and evaluates conditions using natural-language reasoning — the approach originally intended in Phase 20.
- New signature:
  ```
  async def check_position_theses(
      positions, active_theses, indicators, daily_indicators,
      market_data, regime_assessment, sector_regimes, cycle_id
  ) -> Optional[CompressorResult]
  ```
- `_condition_met()` deleted entirely. No replacement — Haiku handles all condition types.

**New prompt: `config/prompts/v3.10.1/thesis_check.txt`**
- Dedicated minimal prompt for thesis breach evaluation. Template variables: `{positions_json}`, `{regime_json}`, `{market_context_json}`.
- Conservative evaluation instructions: fire on concrete evidence, not speculation; narrative/event conditions only fire when current signals and news corroborate the scenario.
- Output: `{"needs_sonnet": false, "breach": null}` or `{"needs_sonnet": true, "breach": "SYMBOL: condition that is met"}`.
- Token budget: ~2,530 input tokens (12 positions × ~165 tokens + regime + market context). 128 output tokens trivially sufficient.

**New `_build_thesis_check_payload()` helper** · `intelligence/context_compressor.py`
- Returns three JSON strings for the template: `positions_json`, `regime_json`, `market_context_json`.
- Per-position payload: `symbol`, `thesis[:150]`, `thesis_breaking_conditions`, `live_signals` (rsi/daily_trend/composite_score/price/trend_structure/volume_ratio — None values omitted), `recent_news` (up to 3 headlines from `market_data["watchlist_news"][sym]`).
- `daily_trend` sourced from `daily_indicators[sym]` if provided; otherwise omitted from live_signals.
- `regime_json`: `{regime, confidence, sector_regimes}`.
- `market_context_json`: `{spy_trend, spy_rsi, qqq_trend, spy_daily, macro_news}` (2 SPY + 1 QQQ macro headlines).
- Positions with no matching `active_theses` entry are excluded — nothing to evaluate.

**Orchestrator call site updated** · `core/orchestrator.py`
- Medium loop call now `await`s `check_position_theses` and passes: `indicators=self._all_indicators`, `daily_indicators=self._daily_indicators`, `market_data=self._latest_market_context or {}`, `regime_assessment=self._last_regime_assessment`, `sector_regimes=self._last_sector_regimes`.
- `cycle_id` pattern unchanged: `f"medium_{self._trigger_state.last_claude_call_utc}"`.
- Per-cycle guard (`_last_needs_sonnet_cycle`) and downstream `_run_claude_cycle("thesis_breach")` trigger unchanged.

**Tests updated** · `tests/test_phase21.py`, `tests/test_context_compressor.py`
- `TestConditionMet` deleted (class gone).
- `TestPositionThesisMonitoring` rewritten as async tests with mocked Haiku client: breach detected, no breach, parse failure (→ None), API failure (→ None), per-cycle guard, no active theses / no positions (→ skip), position not in theses (→ Haiku not called).
- `TestThesisCheckPayloadBuilder` added to `test_context_compressor.py`: 9 tests covering live signals enrichment, daily_trend from daily_indicators, news inclusion, missing news, thesis exclusion when no matching entry, thesis truncation at 150 chars, empty positions, missing indicators, macro news.

---

### 2026-04-01 — Composite Score Redesign: Direction-Aware TA Scoring

**Root cause / motivation:** `compute_composite_score(signals, direction=...)` was a single scalar used for both long and short candidates. It was direction-aware at call time but never stored directionally — the `_latest_indicators` cache merged it in as `composite_technical_score` using the long direction unconditionally. Short candidates were therefore always scored against a long-biased TA metric. Additionally, the score was exposed to Claude as a filter_adjustments target (`min_composite_score`), which was architecturally incoherent — Claude never sees the scores and cannot usefully advise on the floor.

**`compute_directional_scores(intraday_signals, daily_signals)`** · *(new function)* · `intelligence/technical_analysis.py`
- **Spec:** *(not defined)*
- **Impl:** Replaces `compute_composite_score`. Returns `(long_score, short_score)` tuple. Four components: Extension (30%), Exhaustion (25%), Participation (25%), Trend context (20%). Swing-aware — oversold RSI is a positive signal for longs, a negative one for shorts; overbought RSI is the reverse.
- `compute_composite_score` kept in `technical_analysis.py` for `universe_scanner.py` backward compatibility (universe scanner computes directional scores separately as `composite_score_long`/`composite_score_short`).
- Added `warnings.warn` for unrecognized `daily_trend` or `intraday_trend` labels; canonical daily values are `"uptrend"/"downtrend"/"mixed"`, canonical intraday are `"bullish_aligned"/"bearish_aligned"/"mixed"`.

**`_latest_indicators` cache** · `core/orchestrator.py`
- **Old:** `"composite_technical_score": v.get("composite_technical_score", 0.0)` (long-direction only)
- **New:** `"long_score": v.get("long_score", 0.0), "short_score": v.get("short_score", 0.0)` — both directions stored flat alongside merged signals; no `"signals"` sub-key in the cache.

**`_medium_try_entry` position sizing** · `core/orchestrator.py`
- **Old:** `ind.get("composite_technical_score", 0.5)` — always defaulted to 0.5 since the key was never in the flat cache (bug)
- **New:** `ind.get("short_score" if is_short(entry_direction) else "long_score", 0.5)` — direction-correct score used for the TA size factor

**Exit urgency** · `intelligence/opportunity_ranker.py`
- **Old:** `float(signals.get("composite_technical_score", 0.5))` — always defaulted to 0.5 (bug)
- **New:** `max(float(signals.get("long_score", 0.0)), float(signals.get("short_score", 0.0))) or 0.5`

**`min_composite_score` advisory removed** · `intelligence/opportunity_ranker.py`, `core/config.py`, `core/orchestrator.py`
- `filter_adj_min_composite = 0.35` config constant removed from `RankerConfig`; `"filter_adj_min_composite": 0.35` removed from `config.json`.
- `_clamp_filter_adjustments` silently pops both `min_composite_score` and `min_directional_score` from Claude's output. Claude cannot lower this floor because it never observes the scores.
- `min_composite_score` in `RankerConfig` (line 158) is retained as the *ranker's own composite score floor* (conviction × 0.35 + tech × 0.30 + rar × 0.20 + liq × 0.15 ≥ 0.45) — this is a different concept and is NOT adjustable by Claude.

**`_DIRECTION_DEPENDENT_PATTERNS`** · `core/orchestrator.py`
- Entry `"composite_score"` renamed to `"directional_score"` — the suppression reason written at entry time is now `"directional_score_too_low"`, so the clear-on-regime-reset pattern must match.

**`_regime_reset_build` observability** · `core/orchestrator.py`
- Added `_direction_summary(entries)` inline helper (returns `"N total — XL / YS / Zeither"` string).
- Three log points: before-eviction composition, per-eviction log, after-rebuild composition. Makes bad-tape adaptation visible in logs without a full state audit.

**`assemble_reasoning_context` / `context_compressor.py`** · `intelligence/claude_reasoning.py`, `intelligence/context_compressor.py`
- `_tier1_score_simple` (Tier 3 bypass): replaced `compute_composite_score` calls with `compute_directional_scores`.
- `_build_candidates_payload` and `_fallback_sort`: replaced `composite_score` key with `directional_score`; scoring now direction-aware.
- Thesis payload: `daily_sig` was not passed to `compute_directional_scores` (bug); now correctly passed.
- `run_position_review`: exposes `long_score`/`short_score` in signals summary instead of dead `composite_technical_score` key.

**Prompt updates** · `config/prompts/v3.10.1/reasoning.txt`, `config/prompts/v3.10.1/compress.txt`
- `sector_performance` description updated to reference `long_score`/`short_score`.
- `filter_adjustments` schema: `min_composite_score` removed; only `min_rvol` remains.
- SHORT entry conditions: added explicit guidance distinguishing breakdown shorts (`rsi_slope_max`) from fade/mean-reversion shorts (`rsi_accel_max` with negative value). Explains when RSI is decelerating but still positive, `rsi_accel_max` is the correct gate.
- `compress.txt`: `composite_score` → `directional_score` in candidate payload description.

---

### 2026-04-01 — Breach Context Propagation: Passing Detected Breach to Sonnet

**Root cause:** When Haiku detected a thesis breach via `check_position_theses`, the orchestrator fired a `thesis_breach` Sonnet cycle, but Sonnet received no information about *which* condition was detected. Without the breach detail, Sonnet would re-examine the position using only its prior context and often reaffirm its prior hold recommendation unchanged.

**`_thesis_breach_context`** · *(new field)* · `core/orchestrator.py`
- `self._thesis_breach_context: str | None = None` — stores the breach detail string (from `CompressorResult.notes`) between the medium loop (where breach is detected) and the next `_run_claude_cycle` invocation (where it is consumed).
- Cleared before `await` to prevent double-use if concurrent slow loops fire.

**Medium loop writeback** · `core/orchestrator.py`
- On thesis breach: `self._thesis_breach_context = _breach.notes` before firing `asyncio.ensure_future(_run_claude_cycle("thesis_breach"))`.

**`_run_claude_cycle` — breach context routing** · `core/orchestrator.py`
- Reads and clears `_pending_breach = self._thesis_breach_context; self._thesis_breach_context = None` before any Claude calls.
- Split mode (Call A): passed as `breach_context=_pending_breach` to `run_position_review_call`.
- Non-split mode (Call B/only): passed as `breach_context=_pending_breach if not _split else None` to `run_reasoning_cycle`.

**`run_position_review_call` breach injection** · `intelligence/claude_reasoning.py`
- Accepts `breach_context: str | None = None`.
- Builds `thesis_breach_notice` string with the detected condition and instructions to re-examine; injected as `{thesis_breach_notice}` template variable.
- When no breach, `thesis_breach_notice = ""` (template variable resolves to empty string).

**`position_reviews.txt`** · `config/prompts/v3.10.1/position_reviews.txt`
- Added `{thesis_breach_notice}` placeholder between the opening description and CONSTRAINTS section.

**`run_reasoning_cycle` breach injection (non-split path)** · `intelligence/claude_reasoning.py`
- Accepts `breach_context: str | None = None`.
- When `not skip_position_reviews and breach_context`: prepends breach notice to `position_review_notice`, which maps to the existing `{position_review_notice}` placeholder in `reasoning.txt`. No prompt file changes needed.

**Swing minimum hold guard removed** · `core/orchestrator.py`, `config/prompts/v3.10.1/position_reviews.txt`, `config/prompts/v3.10.1/reasoning.txt`
- Removed the `hold_hours < swing_min_hold_hours` guard in `_apply_position_reviews` that blocked Claude-recommended exits for swing positions held < 4h.
- Removed corresponding constraints from both prompt files.
- **Why:** A thesis breach is a concrete invalidation event, not intraday noise — holding a position through a detected breach because of an arbitrary time gate defeats the entire purpose of thesis monitoring. The stop-loss is the correct mechanical guard for genuine noise; the swing hold constraint was duplicating that protection at the cost of blocking legitimate exits.
- Tests for swing hold guard behavior (`test_swing_exit_blocked_within_min_hold_window`, `test_swing_exit_allowed_after_min_hold_window`) removed from `test_orchestrator.py`.

---

### 2026-04-01 — Catalyst Expiry and Fetch Failure Context Suppression

**`catalyst_expiry_utc`** · *(new field)* · `core/state_manager.py`, `core/orchestrator.py`, `config/prompts/v3.10.1/watchlist.txt`, `config/prompts/v3.10.1/reasoning.txt`
- **Spec:** *(not defined)*
- **Impl:** `catalyst_expiry_utc: Optional[str]` added to `WatchlistEntry`. ISO 8601 UTC string. Set by the watchlist build prompt when an entry is event-driven (earnings, FDA, data release). Absent for thesis-driven entries (technical setups, sector rotation). Hard limit — not modified after creation.
- **Why:** Event-driven entries become noise after their catalysts resolve. Without expiry, stale earnings plays sit in tier 1 occupying a slot and receiving full Claude analysis every cycle, while their thesis is moot.
- **Convention:** Market close on event day = `21:00:00+00:00` (EDT) / `22:00:00+00:00` (EST). Re-adding after expiry = fresh evaluation — intentional correct behavior.

**`_prune_expired_catalysts`** · *(new method)* · `core/orchestrator.py`
- Runs at top of `_apply_watchlist_changes` and in the medium loop after loading the watchlist.
- Iterates entries, parses `catalyst_expiry_utc` via `datetime.fromisoformat`, removes entries where expiry ≤ now. Malformed timestamps log WARNING and keep the entry (safe default).
- Medium loop: saves watchlist only if at least one entry was pruned.

**Fetch failure context suppression** · *(new behavior)* · `core/orchestrator.py`, `intelligence/claude_reasoning.py`
- **Spec:** *(not defined)*
- **Impl:** First yfinance fetch failure for a symbol immediately sets `_filter_suppressed[sym] = "fetch_failure"`. Clears on next successful fetch. Existing 3-failure watchlist removal unchanged.
- `assemble_reasoning_context` filters `_suppressed_set` from both `all_tier1` and `sym_to_entry` — suppressed symbols are excluded from Claude's context payload entirely.
- **Why:** A symbol with a failed data feed produces stale or zero indicators. Sending it to Claude with zeros causes false signals; Claude may recommend entry on a symbol the system cannot price. Suppressing from context immediately on first failure prevents this without waiting for the 3-failure removal threshold.

---

### 2026-04-01 — Watchlist Build Decoupled from Reasoning Cycle

**`_run_claude_cycle` watchlist build path removed** · `core/orchestrator.py`
- **Old:** `_run_claude_cycle` ran `run_watchlist_build` as a blocking `await` before reasoning. When `watchlist_stale` co-triggered with any reasoning trigger, the build blocked Call A and Call B for 30–120s.
- **New:** Build fires as `asyncio.ensure_future(_run_watchlist_build_task())` from `_slow_loop_cycle` before the reasoning cycle starts. Reasoning proceeds immediately. The existing `_call_lock` in `ClaudeReasoningEngine` serializes the build's API call after reasoning completes.
- **Why:** Watchlist curation and opportunity evaluation are independent concerns with different latency requirements. The build never needed to block reasoning — reasoning reads from whatever watchlist state exists at cycle start.

**`_run_watchlist_build_task()`** · *(new method)* · `core/orchestrator.py`
- Background task following `_regime_reset_build` pattern. Owns the universe scan, `run_watchlist_build` call, `_apply_watchlist_changes`, `last_watchlist_build_utc` update, and failure back-date logic. Clears `_watchlist_build_in_flight` in `finally`.
- **Backdate policy change:** Old code stamped full timestamp on `wl_result is None` (treating it as a completed-but-empty build). New code backdates on both `wl_result is None` AND exception — both are failure modes that warrant retry after `probe_min` minutes, not a full `watchlist_refresh_interval_min` cooldown.

**`_watchlist_build_in_flight: bool`** · *(new field)* · `core/orchestrator.py`
- Separate guard from `claude_call_in_flight`. The slow loop checks for reasoning triggers only against `claude_call_in_flight`. A build running in the background does not block the next reasoning cycle.

**`watchlist_changes.add` removed from reasoning output** · `config/prompts/v3.10.1/reasoning.txt`, `core/orchestrator.py`
- Claude no longer adds symbols during the reasoning call. Adds go exclusively through `_run_watchlist_build_task` (dedicated build call with universe scan context and web search). `watchlist_changes.remove` is preserved — Claude can still flag dead candidates for removal as a byproduct of evaluation.
- **Why:** The reasoning call had no universe scan context, no news search, and no candidate pool awareness. Adds from reasoning bypassed the research process and created a dual-path watchlist mutation pattern. The build call is the correct and only entry point for new symbols.

---

### 2026-04-02 — Watchlist/Reasoning Full Separation + Build Reliability (Phase 23)

Root cause: 2026-04-02T14:00Z session showed 4 watchlist removals in 8 minutes from reasoning, depleting the watchlist to 4 symbols and triggering `candidates_exhausted`, which fired more reasoning on the same depleted pool — a negative feedback spiral.

**`watchlist_changes.remove` removed from reasoning output** · `config/prompts/v3.10.1/reasoning.txt`, `core/orchestrator.py`
- **Old:** Reasoning could remove watchlist entries via `watchlist_changes.remove`. During a bad session, Claude removed live candidates as "no valid setup today" — exhausting the pool and starving future reasoning cycles.
- **New:** Reasoning is fully read-only on the watchlist. `_run_claude_cycle` calls `_apply_watchlist_changes(watchlist, [], [], open_symbols)` — no adds, no removes.
- **Why:** The reasoning cycle evaluates opportunities, not watchlist health. Removal authority belongs to the build, which has full watchlist context and conservative removal criteria.

**`remove` field moved to watchlist build** · `config/prompts/v3.10.1/watchlist.txt`, `intelligence/claude_reasoning.py`, `core/orchestrator.py`
- Build now returns an optional `remove` list. `WatchlistResult` gains `removes: list[str]`. `_run_watchlist_build_task` and `_regime_reset_build` pass `wl_result.removes` to `_apply_watchlist_changes`.
- Removal criteria in watchlist prompt: permanently invalidated theses only (catalyst confirmed and passed, company event resolved). "No valid setup today" is explicitly not a removal criterion.
- `_apply_watchlist_changes` remove path already guarded against open positions (added in same session, pre-Phase-23).

**`candidates_exhausted` rerouted to build trigger** · `core/orchestrator.py` → `_slow_loop_cycle`
- **Old:** `candidates_exhausted` fired a reasoning cycle on the existing (depleted) watchlist.
- **New:** Treated as a build trigger (`_BUILD_TRIGGERS = {"watchlist_small", "watchlist_stale", "candidates_exhausted"}`). Fires `_run_watchlist_build_task`. Sets `_reasoning_needed_after_build = True`.
- **Why:** Exhausted candidates means the watchlist is depleted. The correct response is new candidates (build), not more reasoning on the same empty pool.

**`_reasoning_needed_after_build: bool`** · *(new field)* · `core/orchestrator.py`
- Set by `_slow_loop_cycle` when `candidates_exhausted` fires or `require_watchlist_before_reasoning=True` defers reasoning. Cleared by `_run_watchlist_build_task` on any exit path (success, parse failure, exception). On success with `added > 0`, fires `_post_build_reasoning("post_build_candidates")` before clearing.

**`_post_build_reasoning(trigger_name: str)`** · *(new method)* · `core/orchestrator.py`
- Fires a reasoning cycle after a successful build that added new candidates. Guards: market must be open or pre-market warmup; `_latest_indicators` must be populated; `claude_call_in_flight` must be False. Uses `ensure_future` pattern consistent with `_run_watchlist_build_task`.

**Parse failure vs API failure retry** · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Old:** Both `wl_result is None` (parse failure) and `except Exception` (API failure) used `probe_min` (10 min) for the backdate interval.
- **New:** Parse failure uses `watchlist_build_parse_failure_retry_min` (default 3 min). API exception keeps `probe_min` (10 min). Parse failures are transient (almost always succeed next attempt); API failures need meaningful backoff.

**`require_watchlist_before_reasoning: bool`** · *(new config)* · `core/config.py`, `config/config.json`
- Default `false`. When `true`, if a watchlist build and reasoning trigger co-fire at startup, reasoning is deferred until after the build completes (via `_reasoning_needed_after_build`). Prevents Sonnet evaluating a stale/empty watchlist before the first build runs.

**`valid_until_conditions` must not be currently true** · `config/prompts/v3.10.1/reasoning.txt`
- Added explicit instruction: each condition must be a threshold NOT currently met. Example: if SPY RSI is currently 38, do not write "SPY daily RSI > 40" (nearly already true). Write "SPY daily RSI > 50". Prevents spurious regime flips within seconds of being written.


---

### Post-Phase-23 — RVOL-Conditional Dead Zone (2026-04-02)

**`_dead_zone_rvol_bypass()` + `dead_zone_rvol_bypass_enabled/threshold` config** · *(not in spec)* · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** The dead zone (11:30–2:30 ET) entry block now lifts when SPY RVOL ≥ `dead_zone_rvol_bypass_threshold` (default 1.5). Pure predicate `_dead_zone_rvol_bypass()` on the orchestrator reads `_all_indicators["SPY"]` with the try-flat-then-nested fallback (SPY is a context symbol stored with nested `"signals"`). `risk_manager.py` is not modified — bypass is expressed via the existing `dead_zone_exempt` parameter to `validate_entry`. All three dead zone call sites updated: ranker rejection suppression loop, entry defer count guard in `_medium_try_entry`, and `validate_entry` call. One-per-cycle log in `_medium_loop_cycle` after `_all_indicators` is populated.
- **Why:** The dead zone was a static time proxy for "low volume = bad entries." On volatile days (Fed, macro events, sector catalysts) midday is the most active window of the session. Phase 23 decoupled suppression from the dead zone; this change makes the entry block itself data-driven.

---

### Post-Phase-23 — Entry Condition Calibration + Trigger Guards (2026-04-06)

**`evaluate_entry_conditions` RSI calibration error** · *(not in spec)* · `intelligence/opportunity_ranker.py`
- **Spec:** `evaluate_entry_conditions` returns `(met: bool, reason: str)` with pass/fail per condition key.
- **Impl:** Two new synthetic failure modes: `rsi_max_calibration_error` (short: `rsi_max` set >5pts below current RSI) and `rsi_min_calibration_error` (long: `rsi_min` set >5pts above current RSI). Returns a diagnostic string suggesting `rsi_slope_max`/`rsi_slope_min` instead. Tolerance controlled by `entry_condition_rsi_level_tolerance` (default 5.0; set 0 to disable).
- **Why:** Claude was setting `rsi_max=35` when RSI was 48 — an impossible gate that deferred forever. The calibration error surfaces the miscalibration in `last_block_reason` feedback so Claude can self-correct on the next reasoning cycle.

**`filter_adjustments.min_rvol` clamped to strategy floor** · *(not in spec)* · `strategies/momentum_strategy.py`
- **Spec:** `filter_adjustments.min_rvol` from Claude reasoning output is applied directly as the RVOL filter threshold.
- **Impl:** Claude's `min_rvol` can only *lower* the strategy's configured RVOL floor, not raise it above the default. `MomentumStrategy.validate_entry` clamps `min(claude_rvol, config_default)`.
- **Why:** On small-sample execution stats (e.g. 0/3 short streak), Claude was raising `min_rvol` to 1.5+ which filtered out nearly everything — a self-reinforcing cage. The clamp preserves Claude's ability to be more permissive but prevents it from being more restrictive than the operator's configured baseline.

**`approaching_close` fires exactly once per session** · *(not in spec)* · `core/trigger_engine.py`
- **Spec:** *(not defined — Phase 17 added this trigger without a one-shot guard)*
- **Impl:** `approaching_close_fired: bool` on `SlowLoopTriggerState`. Set `True` when trigger fires; cleared on `session_open` and `session_close` transitions.
- **Why:** The trigger window is 4 minutes (15:28–15:32 ET) but `slow_loop_check_sec` is 60s, so without the flag the trigger fired on every tick within the window.

**`regime_condition` 20-min cooldown** · *(not in spec)* · `core/trigger_engine.py`, `core/config.py`
- **Spec:** *(not defined)*
- **Impl:** `last_regime_condition_utc` on `SlowLoopTriggerState`; trigger suppressed if within `regime_condition_cooldown_min` (default 20). Config-driven.
- **Why:** Rapid regime oscillation (normal→sector_rotation→normal within minutes) caused `regime_condition` to chain-fire, producing multiple redundant Claude calls.

**Thesis breach Sonnet suppression** · *(not in spec)* · `core/orchestrator.py`, `core/position_manager.py`
- **Spec:** *(not defined)*
- **Impl:** `_last_position_review_utc: dict[str, datetime]` stamped after every Sonnet position review. Thesis breach scheduling gate suppresses Sonnet call if symbol was reviewed within `thesis_breach_review_cooldown_min` (default 15). Haiku check (cheap) runs regardless; Sonnet call (expensive) is the suppression target.
- **Why:** A thesis breach Haiku check could schedule a Sonnet review for a position that was just reviewed 2 minutes ago by the regular slow-loop cycle, wasting ~15s of API time on a redundant call.

---

### Agentic Workflow — Signal Wiring (Phases 22-28)

*These entries cover changes to trading bot code made by the agentic workflow phases.
For full workflow architecture see `docs/agentic-workflow.md`.*

**Signal file emitters in orchestrator** · *(not in spec)* · `core/orchestrator.py`, `core/signals.py`
- **Spec:** *(not defined — agentic workflow addition)*
- **Impl:** `write_status(data)` called at end of every `_fast_loop_cycle()` with portfolio, orders, and health data. `write_last_review(data)` called after `_apply_position_reviews()`. Three `write_alert()` calls: equity drawdown (>2% session drop, fires once per session), broker error (first failure in `_mark_broker_failure()`), loop stall (tick >60s in `_fast_loop()`). All calls wrapped in `try/except Exception` — fire-and-forget, never crashes the bot.
- **Why:** Downstream agents (Ops Monitor, clawhip) need machine-readable bot state without importing from `ozymandias/`. Signal files are the universal bus.

**Inbound signal processing** · *(not in spec)* · `core/orchestrator.py`
- **Spec:** *(not defined — extends existing `EMERGENCY_EXIT` pattern)*
- **Impl:** `_check_inbound_signals()` called in `_fast_loop()` after emergency signal check. Reads three signals: `PAUSE_ENTRIES` (persistent — sets `_entries_paused` bool, checked at top of `_medium_try_entry()`), `FORCE_REASONING` (one-shot — consumed, sets `_force_reasoning` injected as synthetic trigger in `_slow_loop_cycle()`), `FORCE_BUILD` (one-shot — consumed, sets `_force_build` added to `_BUILD_TRIGGERS` and injected in `_slow_loop_cycle()`).
- **Why:** Operator and Discord companion need to control bot behavior without restarting it. Same touch-file pattern as `EMERGENCY_EXIT`.

**`write_last_trade` in fill handler** · *(not in spec)* · `core/fill_handler.py`
- **Spec:** *(not defined)*
- **Impl:** `write_last_trade(data)` called at end of `dispatch_confirmed_fill()` with symbol, side, qty, price, order_id, timestamp. Wrapped in `try/except Exception`.
- **Why:** clawhip routes `last_trade.json` changes to Discord `#trades` channel for real-time fill notifications.

**Alert filename microsecond resolution** · *(not in spec)* · `core/signals.py`
- **Spec:** *(not defined — plan showed second-level resolution)*
- **Impl:** Alert filenames use `strftime('%Y%m%dT%H%M%S%f')` (microsecond resolution) instead of `strftime('%Y%m%dT%H%M%S')` (second resolution).
- **Why:** Two alerts within the same second produced identical filenames, causing the second to silently overwrite the first. Microsecond resolution eliminates collisions.

**`_session_start_equity` field** · *(not in spec)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `_session_start_equity` seeded from `acct.equity` in `_startup()`. Used to calculate session drawdown percentage for the equity drawdown alert (>2% threshold). `_drawdown_alert_fired: bool` prevents repeat alerts within the same session.
- **Why:** The drawdown alert needs a baseline. Using session start equity (not daily high) matches the operator's mental model of "how much am I down since I started watching."

---

## Cross-References

- [[ozy-drift-log]] — Active drift log (new entries)
- [[ozy-drift-log-eras-19-21]] — Previous era (Sonnet/Haiku/Durability)
- [[ozy-doc-index]] — Full routing table
