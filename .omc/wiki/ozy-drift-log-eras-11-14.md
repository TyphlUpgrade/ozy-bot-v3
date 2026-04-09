---
title: "Ozy Drift Log — Eras 11-14"
tags: [ozymandias, drift-log, archive, phases-11-14, execution-fidelity]
category: reference
ceiling_override: frozen-archive
frozen: true
created: 2026-04-09
updated: 2026-04-09
---

# Ozy Drift Log — Eras 11-14 (Execution Fidelity)

Frozen archive of spec deviations from Phases 11-14, Context Blindness Fix, and Post-Phase-14 Debug.
For the active drift log and filing rules, see [[ozy-drift-log]].

---

### Phase 11 — Execution Fidelity

**Current market price for entry limit orders** · phase §1 · `core/orchestrator.py`
- **Spec:** *(phase 11 addition)*
- **Impl:** `_medium_try_entry()` now fetches `ind = self._latest_indicators.get(symbol, {})` at the top of the function, then resolves `entry_price = ind.get("price")`. Falls back to `top.suggested_entry` with a WARNING log when price is absent. `ind` is fetched once and reused for `atr_14`, `composite_technical_score`, etc. throughout the function — the previous duplicate `ind = ...` line removed.
- **Why:** `top.suggested_entry` is up to 60 minutes stale. High-volatility equities can move substantially in that window, causing silent non-fills.

**Entry price staleness / drift check** · phase §2 · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Spec:** *(phase 11 addition)*
- **Impl:** After resolving `entry_price`, computes `drift = (entry_price - top.suggested_entry) / top.suggested_entry`. For longs: blocks if `drift > max_entry_drift_pct` (chase) or `drift < -max_adverse_drift_pct` (adverse break). For shorts: directions inverted. Logs at INFO — normal expected behavior.
- **New config keys** in `RankerConfig` and `config.json`: `max_entry_drift_pct=0.015`, `max_adverse_drift_pct=0.020`.
- **Why:** Two failure modes — price ran past entry (momentum already captured) or broke through entry level (thesis invalid). Integration test `test_full_cycle_places_order` updated to pass `price=875.0` to the Claude mock to match bar prices.

**Minimum composite technical score hard filter** · phase §3 · `intelligence/opportunity_ranker.py`, `core/config.py`, `config/config.json`
- **Spec:** *(phase 11 addition)*
- **Impl:** Added filter 0.5 in `apply_hard_filters()`, between conviction check and market-hours check. Reads `composite_technical_score` from the top-level of the `sig_summary` dict (same level as `generate_signal_summary()` output). When `technical_signals is None`, skipped entirely (backward compatible).
- **New config key** in `RankerConfig` and `config.json`: `min_technical_score=0.30`.
- **New `OpportunityRanker.__init__` key**: `self._min_technical_score = float(cfg.get("min_technical_score", 0.30))`.
- **Orchestrator `ranker_cfg` dict**: `"min_technical_score"` added alongside existing keys.
- **Why:** Catches degenerate TA cases (score near 0) that slip through conviction threshold. 0.30 is a quality floor, not a high bar — composite RSI=50 + neutral MACD already clears it.

**TA signal strength as position size modifier** · phase §4 · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Spec:** *(phase 11 addition)*
- **Impl:** After `calculate_position_size()` and `quantity <= 0` check, applies: `size_factor = ta_size_factor_min + (1.0 - ta_size_factor_min) * tech_score`. Quantity = `max(1, int(quantity * size_factor))`. `tech_score` read from `ind.get("composite_technical_score", 0.5)`. Logged at DEBUG. Note: `_latest_indicators` stores the `"signals"` sub-dict (not the full summary), so `composite_technical_score` is not normally present — `tech_score` defaults to `0.5` in production until `_latest_indicators` is updated to store the full summary.
- **New config key** in `RankerConfig` and `config.json`: `ta_size_factor_min=0.60`.
- **Orchestrator `ranker_cfg` dict**: `"ta_size_factor_min"` added.
- **`_latest_indicators` updated**: line 1194 now merges `composite_technical_score` into the signals dict — `{**v["signals"], "composite_technical_score": v.get("composite_technical_score", 0.0)}`. Previously only `v["signals"]` was stored, which silently stripped this field and caused `tech_score` to always default to `0.5`.
- **Why:** Varies position size proportionally to TA quality: weak-signal setups enter smaller; strong-signal setups enter full size.

**Existing tests fixed** · `tests/test_orchestrator.py`, `tests/test_integration.py`
- Both `TestThesisChallenge._stub_entry_guards` and `TestThesisChallengeCache._stub_entry_guards` updated to set `_latest_indicators = {"AAPL": {"composite_technical_score": 1.0}}`, giving TA size factor=1.0 so those tests' quantity assertions remain correct.
- `TestFullCycle.test_full_cycle_places_order` updated to configure Claude mock with `price=875.0` matching the test's bar data, satisfying the new drift check.

---

### Post-MVP (Context Blindness Fix — Macro Data + News Headlines)

**`market_data` placeholder replaced with real macro context** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined — `market_data` block in `_run_claude_cycle` was hardcoded stubs)*
- **Impl:** The hardcoded `spy_trend="unknown"`, `vix=None`, `sector_rotation="unknown"`, `macro_events_today=[]` block replaced by `await self._build_market_context(acct, pdt_remaining)`, a new private async method that builds real macro context from live TA data and concurrent news fetches.
- **Why:** Claude received zero market context. With stubs only, watchlist suggestions defaulted to prominent large-caps regardless of what was actually moving.

**`_CONTEXT_SYMBOLS` module constant** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** Module-level constant `_CONTEXT_SYMBOLS = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLC"]`. Used both for the medium-loop context fetch and inside `_build_market_context` for breadth counting.
- **Why:** Single authoritative list prevents drift between the fetch loop and the context builder.

**`Orchestrator._market_context_indicators`** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `self._market_context_indicators: dict = {}` added in `__init__`. Populated by a best-effort context fetch at the end of every `_medium_loop_cycle()`. Stores full `generate_signal_summary()` output keyed by symbol. These symbols do NOT enter `_latest_indicators` — no entry pipeline contamination.
- **Why:** Medium and slow loops run on independent timers. Storing context results in `__init__`-level state lets `_build_market_context` consume them without triggering a new fetch.

**`_build_market_context()` output shape** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined — existing `market_data` shape was informal)*
- **Impl:** Returns: `spy_trend` (bullish/bearish/mixed/unknown derived from `trend_structure` + `vwap_position`), `spy_rsi` (float or null), `qqq_trend` (same classification), `market_breadth` (string e.g. "7/10 context instruments bullish-aligned"), `sector_performance` (list of `{sector, etf, trend, composite_score}` sorted by score descending, sector ETFs only), `watchlist_news` (dict symbol → headlines, omits symbols with no results), `trading_session`, `pdt_trades_remaining`, `account_equity`, `buying_power`.
- **Why:** Provides actionable signal in each field; no VIX (not available free via yfinance), no full news body (token budget).

**`YFinanceAdapter.fetch_news()`** · spec *(SentimentAdapter ABC is post-MVP Finnhub)* · `data/adapters/yfinance_adapter.py`
- **Spec:** Full `SentimentAdapter` ABC with Finnhub backend is planned post-MVP. This is NOT that implementation.
- **Impl:** New async method `fetch_news(symbol, max_items=5)` on `YFinanceAdapter`. Calls `yf.Ticker(symbol).news` in `asyncio.to_thread()`. Filters to items where `providerPublishTime` is within last 24 hours. Returns `[{title, publisher, age_hours}]` — no links or full body. Returns `[]` on any exception. Cache TTL: 15 min (`news_ttl=900` constructor param, same cache infrastructure as quotes/bars/fundamentals).
- **Why:** `yf.Ticker.news` requires no API key and adds zero new dependencies. When a real `SentimentAdapter` is built later, the orchestrator just changes the call site; Claude's context shape stays identical.

**`ClaudeConfig.news_max_age_hours` / `news_max_items_per_symbol`** · spec *(not defined)* · `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `news_max_age_hours: int = 168` (7 days — secondary age gate applied in `_build_market_context` after adapter's 24h filter) and `news_max_items_per_symbol: int = 3` added to `ClaudeConfig` and `config.json` `claude` section.
- **Why:** Operator-tunable; defaults are conservative. The adapter's 24h filter is the practical ceiling in normal operation; `news_max_age_hours` can be tightened to e.g. 12 if only breaking news is wanted.

**`reasoning.txt` MACRO AND NEWS USAGE section** · spec *(not defined)* · `config/prompts/v3.3.0/reasoning.txt`
- **Spec:** *(not defined)*
- **Impl:** New "MACRO AND NEWS USAGE" paragraph after the numbered instructions. Instructs Claude to: name leading/lagging sectors in `market_assessment` by ETF, reflect catalysts from `watchlist_news` in opportunity `reasoning`, cite sector headwinds in `rejection_reason`.
- **Why:** New context fields are silently ignored without explicit prompt instructions.

**Integration test `_data_adapter` mocks require `fetch_news = AsyncMock`** · spec *(not defined)* · `tests/test_integration.py`
- **Spec:** *(testing constraint)*
- **Impl:** Three test setups that assign `orch._data_adapter = MagicMock()` now also set `orch._data_adapter.fetch_news = AsyncMock(return_value=[])`.
- **Why:** `_build_market_context` calls `asyncio.gather(*[adapter.fetch_news(s) for s in tier1])`. A bare `MagicMock()` returns a non-awaitable on call; `asyncio.gather` raises `TypeError`. Tests that exercise the slow loop path (`test_full_cycle_places_order`) fail without this fix.

---

### Phase 12 — Direction Unification

**`ozymandias/core/direction.py`** · spec *(not defined — post-MVP Phase 16)* · `core/direction.py`
- **Spec:** *(not defined)*
- **Impl:** New module `core/direction.py` is the single source of truth for all direction-related mappings. Exports: `Direction = Literal["long", "short"]`, `ACTION_TO_DIRECTION`, `ENTRY_SIDE`, `EXIT_SIDE`, `direction_from_action(action) -> Direction`, `is_short(direction) -> bool`. Unknown action strings in `direction_from_action` log WARNING and default to `"long"`.
- **Why:** Direction was expressed four ways across the codebase (Claude action strings, internal direction strings, broker side strings, ad-hoc inline checks). Centralising in one module means adding a new action type is one dict entry with no logic changes elsewhere.

**`TradeIntention.direction` annotation** · spec *(not defined)* · `core/state_manager.py`
- **Spec:** `direction: str = "long"`
- **Impl:** `direction: Direction = "long"` — type narrowed from `str` to `Direction` (imported from `core/direction`). Runtime behaviour unchanged; adds static typing benefit.
- **Why:** Phase 16 requirement: annotate `PositionIntention.direction` as `Direction` type.

**Migrated call sites** · spec *(not defined)* · `intelligence/opportunity_ranker.py`, `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:**
  - `opportunity_ranker.py`: removed local `_ACTION_TO_DIRECTION` dict; imports `ACTION_TO_DIRECTION` and `direction_from_action` from `core/direction`. Both `_ACTION_TO_DIRECTION.get(action, "long")` call sites replaced with `direction_from_action(action)`.
  - `orchestrator.py`: imports `EXIT_SIDE`, `direction_from_action`, `is_short` from `core/direction`. `is_short = top.action == "sell_short"` in `_medium_try_entry` replaced with `entry_direction = direction_from_action(top.action)` + `is_short(entry_direction)` predicate calls. `is_short = position.intention.direction == "short"` in `_journal_closed_trade` replaced with `pos_is_short = is_short(position.intention.direction)`. Three `exit_side = "buy" if ... == "short" else "sell"` ternaries replaced with `EXIT_SIDE[direction]` table lookup. Inline `direction == "short"` check in `_fast_step_quant_overrides` and pnl calculation replaced with `is_short()` predicate.
- **Why:** Eliminates all ad-hoc direction string comparisons outside `core/direction.py` and `core/broker_interface.py`. Adding a new action type now requires only one dict entry in `core/direction.py`.

**`tests/test_direction.py`** · spec *(Phase 16 §4)* · `tests/test_direction.py`
- **Spec:** 7 tests covering round-trips, unknown action, `ENTRY_SIDE`/`EXIT_SIDE` inverses, `is_short` predicate.
- **Impl:** 13 tests in 5 classes (`TestActionToDirection`, `TestDirectionFromAction`, `TestSideInverses`, `TestRoundTrips`, `TestIsShort`). All pass.
- **Why:** Comprehensive coverage of all tables and helpers.


---

### Phase 13 — Strategy Modularity

**`Strategy` ABC: 3 trait properties + `apply_entry_gate` abstract method** · spec *(not defined — post-MVP Phase 17)* · `strategies/base_strategy.py`
- **Spec:** *(not defined)*
- **Impl:** Added `is_intraday`, `uses_market_orders`, `blocks_eod_entries` concrete properties (safe defaults: True/False/False) and `apply_entry_gate(action, signals) -> tuple[bool, str]` abstract method to the `Strategy` ABC. These are the single source of truth for strategy-specific behaviour that was previously scattered across orchestrator, ranker, and risk manager.
- **Why:** Adding a third strategy previously required touching 4 files outside the strategy itself. Phase 17 reduces this to `strategies/new_strategy.py` + one `config.json` entry.

**`MomentumStrategy`/`SwingStrategy`: implement new ABC members** · spec *(not defined)* · `strategies/momentum_strategy.py`, `strategies/swing_strategy.py`
- **Spec:** *(not defined)*
- **Impl:** Both concrete strategies implement `is_intraday`, `uses_market_orders`, `blocks_eod_entries`, and `apply_entry_gate`. Lookup tables `_MOMENTUM_WRONG_VWAP` and `_SWING_WRONG_TREND` moved from `opportunity_ranker.py` to their respective strategy files. New `_DEFAULT_PARAMS` keys: `"require_vwap_gate": True` (momentum) and `"block_bearish_trend": True` (swing).
- **Why:** Strategy-specific gate logic belongs in the strategy class.

**`StrategyConfig`: `momentum_params`/`swing_params` → `strategy_params: dict[str, dict]`** · spec *(not defined)* · `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `StrategyConfig` replaces two named fields with a single `strategy_params: dict[str, dict]` dict (maps strategy name → overrides). `config.json` updated to `"strategy_params": {"momentum": {...}, "swing": {...}}`. Note: `config.json` `active_strategies` was incorrectly `["momentum, swing"]` (one string with a comma); fixed to `["momentum", "swing"]` (two strings).
- **Why:** Per-strategy named fields required a new field per new strategy. The dict requires only a new key.

**`orchestrator.py`: `_build_strategies()` uses registry** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `_build_strategies()` now returns `dict[str, Strategy]` keyed by strategy name, using `get_strategy()` for all construction. `self._strategy_lookup` added alongside `self._strategies` (list). PDT gate uses `strategy_obj.is_intraday`; market order decision uses `strategy_obj.uses_market_orders`; `validate_entry` receives `strategy_obj.blocks_eod_entries`. Strategy-specific ranker config keys (`momentum_min_rvol`, `momentum_require_vwap_above`, `swing_block_bearish_trend`) removed from `ranker_cfg` dict — logic now lives in strategy classes.
- **Why:** Removes all hardcoded strategy name checks from the orchestrator.

**`opportunity_ranker.py`: delegates strategy gates to `apply_entry_gate()`** · spec *(not defined)* · `intelligence/opportunity_ranker.py`
- **Spec:** *(not defined)*
- **Impl:** `apply_hard_filters()` section 2b replaced with a delegate call to `strategy_obj.apply_entry_gate(action, sig)`. Accepts optional `strategy_lookup: dict` parameter; falls back to lazy `get_strategy()` construction when not provided (preserves existing test compatibility). `_MOMENTUM_WRONG_VWAP`, `_SWING_WRONG_TREND` removed from this module.
- **Why:** if/elif over strategy name violated modularity — adding a third strategy would require editing the ranker.

**`risk_manager.validate_entry`: `strategy: str` → `blocks_eod_entries: bool`** · spec *(not defined)* · `execution/risk_manager.py`
- **Spec:** `validate_entry(symbol, side, quantity, price, strategy, ...)`
- **Impl:** `strategy: str` parameter replaced with `blocks_eod_entries: bool`. `_check_market_hours` updated identically. Call site in orchestrator passes `strategy_obj.blocks_eod_entries`. Risk manager no longer imports from `strategies/`.
- **Why:** Avoids importing `Strategy` into `execution/` layer; passes only the boolean behaviour flag needed.

**`tests/test_strategy_traits.py`** · spec *(Phase 17)* · `tests/test_strategy_traits.py`
- **Spec:** *(not defined)*
- **Impl:** 28 tests in 4 classes covering all 3 trait properties for both strategies, `apply_entry_gate` for all action/signal combinations, and registry-based `_build_strategies` simulation. All pass.
- **Why:** Phase 17 requirement.

**`tests/test_risk_manager.py`**: `strategy="momentum"/"swing"` → `blocks_eod_entries=True/False`** · spec *(testing constraint)* · `tests/test_risk_manager.py`
- **Spec:** *(testing constraint)*
- **Impl:** All `validate_entry` and `_check_market_hours` call sites updated to pass `blocks_eod_entries: bool` instead of the removed `strategy: str` parameter. Test `test_dead_zone_applies_to_swing_strategy` renamed to `test_dead_zone_applies_regardless_of_eod_flag`. Test names for momentum/swing last-5-min tests updated to reflect the boolean semantics.
- **Why:** `validate_entry` API change required test updates.

---

### Phase 14 — Claude-Directed Entry Conditions

**`evaluate_entry_conditions()` function** · spec *(not defined — post-MVP Phase 14)* · `intelligence/opportunity_ranker.py`
- **Spec:** *(not defined)*
- **Impl:** Module-level function `evaluate_entry_conditions(conditions, signals) -> tuple[bool, str]`. Checks five condition keys: `require_above_vwap`, `rsi_min`, `rsi_max`, `require_volume_ratio_min`, `require_macd_bullish`. Missing signal key → condition unmet, not exception. Empty or `None` conditions → `(True, "")` always. Extension point: add new condition keys here with no other changes.
- **Why:** Context-blind TA gates apply identical thresholds to every symbol. Claude can now specify per-trade conditions calibrated to each name's regime (e.g. NVDA momentum RSI 52–72 vs a quieter name 48–65).

**`ScoredOpportunity.entry_conditions`** · spec *(not defined)* · `intelligence/opportunity_ranker.py`
- **Spec:** *(not defined)*
- **Impl:** `entry_conditions: dict = field(default_factory=dict)` added to `ScoredOpportunity`. Populated from `opportunity.get("entry_conditions") or {}` in `score_opportunity()`. `None` from Claude output normalised to `{}`.
- **Why:** Carries Claude's conditions through the ranker pipeline to `_medium_try_entry` intact without additional state.

**`_medium_try_entry` entry gate** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** After the PDT gate, before drift check and sizing. Reads `top.entry_conditions`, calls `evaluate_entry_conditions(entry_conds, self._latest_indicators.get(symbol, {}))`. Blocked entries log at INFO and return `False` (deferred to next cycle). Empty conditions = no-op. The caller's loop tries the next ranked candidate on `False` — a deferred entry does not block other candidates.
- **Why:** `_latest_indicators` holds the freshest available signals from the most recent medium loop scan. Checking them here guarantees conditions are evaluated against current data, not Claude's 60-minute-old snapshot.

**Prompt version `v3.4.0`** · spec *(not defined)* · `config/prompts/v3.4.0/reasoning.txt`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `v3.4.0/reasoning.txt` adds `entry_conditions` object to the `new_opportunities` schema and a FIELD INSTRUCTIONS bullet explaining per-key semantics. All other prompt files (`review.txt`, `watchlist.txt`, `thesis_challenge.txt`) copied unchanged from `v3.3.0`. `config.json` `claude.prompt_version` updated to `"v3.4.0"`.
- **Why:** Claude ignores new output fields without explicit schema documentation and instructions.

**Watchlist hard size cap** · spec *(not defined)* · `core/orchestrator.py`, `core/config.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `ClaudeConfig.watchlist_max_entries: int = 30` added. `_apply_watchlist_changes` enforces the cap after every Claude cycle (not just when changes are made). Pruning sorts by `max(compute_composite_score(raw, "long"), compute_composite_score(raw, "short"))` so bearish short setups score fairly. Open positions always protected. Newly-added symbols also protected for the cycle they are added — they have no `_latest_indicators` entry yet and would otherwise score `0.0` and be immediately evicted.
- **Why:** Without a cap, watchlist grew unboundedly — `removal_candidate` field was never set and Claude's `remove` list rarely fired. The immediate-eviction bug was discovered by log inspection and fixed in the same session.

**`_tier1_score` direction-agnostic sort** · spec *(not defined)* · `intelligence/claude_reasoning.py`
- **Spec:** *(not defined)*
- **Impl:** `_tier1_score()` previously returned `composite_technical_score` from `_latest_indicators`, which is always computed long-direction. Replaced with `max(compute_composite_score(raw, "long"), compute_composite_score(raw, "short"))` from raw signals when available. Falls back to cached score when signals absent.
- **Why:** Strong bearish setups scored ~0.275 (long-direction) and were being trimmed from tier-1 before Claude could see them. A bearish COIN setup with RSI 42, MACD bullish, RVOL 2.4x scored 0.580 short-direction but was invisible to Claude. Fix ensures short candidates compete fairly for tier-1 context slots.

**Entry conditions use single `ind` snapshot** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `_medium_try_entry` had two separate `_latest_indicators.get(symbol, {})` reads — one for price/drift at line ~1474 and one for condition evaluation at line ~1541. Consolidated to use the `ind` snapshot already captured at the top. No `await` between the reads so no actual async race existed, but a single read is the correct contract.
- **Why:** Defensive correctness; also removes the confusing `current_sigs` local variable.

**Log level promotions** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** Three orchestrator log statements promoted from DEBUG to INFO: (1) "Medium loop: ranker returned N opportunities", (2) "Medium loop: entry blocked for %s — %s", (3) new INFO log at top of `_medium_try_entry` showing symbol/action/conviction/score/strategy on each entry attempt.
- **Why:** Entry path was invisible at INFO level during paper trading. All three are normal operational events, not noise.

**Test file placement fix** · spec *(testing constraint)* · `tests/test_entry_conditions.py` → `ozymandias/tests/test_entry_conditions.py`
- **Spec:** *(testing constraint)*
- **Impl:** `test_entry_conditions.py` was created in `tests/` (project root). `pytest.ini` sets `testpaths = ozymandias/tests`. Moved to `ozymandias/tests/`. Three tests in the file also had a stale `AccountInfo` constructor (missing `currency` and `account_id` fields added in a prior phase) — fixed.
- **Why:** Tests in the wrong directory are silently uncollected; 22 new Phase 14 tests were not running.

---

### Post-Phase-14 Debug Fixes

**VWAP reclaim exception in `MomentumStrategy.apply_entry_gate`** · `strategies/momentum_strategy.py`
- **Impl:** New `_DEFAULT_PARAMS` key `vwap_reclaim_min_rvol: 1.8`. When `require_vwap_gate` fires (price on wrong side of VWAP), the rejection is bypassed if `macd_signal` is `"bullish"` or `"bullish_cross"` AND `volume_ratio >= vwap_reclaim_min_rvol`. Set to `0` to disable the exception.
- **Why:** Log inspection found COIN rejected with MACD bullish + RVOL 2.4x. Being below VWAP with bullish MACD divergence and elevated volume is a VWAP reclaim setup — accumulation before reclaim — which is a valid momentum long entry. The binary VWAP gate had no exception for this case. 7 new tests in `test_strategy_traits.py`.

**PDT gate respects equity floor** · `core/orchestrator.py`
- **Impl:** Two fixes applied. (1) `_medium_try_entry` PDT early gate: wrapped in `if acct.equity < self._config.risk.min_equity_for_trading` — accounts above $25,500 skip the day trade count check entirely. (2) Slow loop context assembly: `pdt_remaining` computation wrapped in same equity check; uses `pdt_remaining = 3` unconditionally above the floor. Previously both paths counted day trades with no equity check, so well-capitalised accounts saw `pdt_trades_remaining: 0` in Claude's context and `PDT block` log lines during momentum entry attempts.
- **Why:** FINRA PDT rules only apply below $25,500 equity. Above that level the broker permits unlimited day trades regardless of PDT flag or local trade count. Log showed broker=38, local=10 day trades, causing the gate to block all momentum entries on a $30k paper account. 2 new integration tests in `TestPDTBlocking`.

**Signal context persisted through bot restarts** · `core/state_manager.py`, `core/orchestrator.py`
- **Impl:** Three new fields added to `TradeIntention`: `entry_signals: dict`, `entry_conviction: float`, `entry_score: float`. `_from_dict_position` updated to deserialize them. `_register_opening_fill` writes these fields from `_entry_contexts` at the moment the position is created in `portfolio.json`. `startup_reconciliation` restores `_entry_contexts` from open positions' `TradeIntention` fields at boot. `_journal_closed_trade` falls back to `TradeIntention` fields if `_entry_contexts` is empty (e.g. restart occurred between fill and close).
- **Why:** `_entry_contexts` and `_pending_intentions` are in-memory dicts. Every trade in the journal showed `signals_at_entry: {}`, `claude_conviction: 0.0`, `composite_score: 0.0` because the bot was restarted between entry and exit in every recorded session. Phase 15's execution stats context feature reads `claude_conviction` from journal entries — without this fix all conviction values would be 0. 2 new tests in `TestRegisterOpeningFill`.

**Partial fill race: adoption guard in `_fast_step_position_sync`** · `core/orchestrator.py`
- **Impl:** Added an in-flight order guard to the untracked-position adoption block in `_fast_step_position_sync`. Before adopting a broker position that isn't in local portfolio, checks `_fill_protection.get_orders_for_symbol()` for any PENDING or PARTIALLY_FILLED order on that symbol. If found, skips adoption with a DEBUG log — the fill handler will register the position with full intention when the order completes.
- **Why:** Log showed CVX override exit 6 minutes after a swing entry. Root cause: partial fill (5/24 shares) arrived 0.7s after order placement, triggering position sync before `_register_opening_fill` could run. Position sync adopted the 5-share position (consuming `_pending_intentions["CVX"]`). When the full fill arrived, `_dispatch_confirmed_fill` saw an existing CVX position → routed as close → journaled with pnl=0.00%. After 60s cooldown, position was re-adopted without intention (`strategy="unknown"`). Override check's fallback from "unknown" to `self._strategies[0]` (MomentumStrategy) caused `roc_deceleration` to fire on a swing position. Fix prevents the adoption, keeping `_pending_intentions` intact for `_register_opening_fill`. 1 new regression test `test_skips_adoption_when_opening_order_in_flight` in `TestPositionSyncQtyCorrection`.

**yfinance news API schema change** · `data/adapters/yfinance_adapter.py`
- **Impl:** `_download_news` rewritten to detect and normalize both yfinance schemas. Old flat schema: top-level `providerPublishTime` (unix int), `title`, `publisher`. New nested schema (yfinance ≥ 0.2.54 / 2026 API change): `content.title`, `content.pubDate` (ISO-8601 string), `content.provider.displayName`. Both paths produce a normalized flat dict `{"title", "publisher", "providerPublishTime": unix_int}` before the age annotator runs. 2 new tests in `test_yfinance_adapter.py`.
- **Why:** `watchlist_news` was always `{}` in Claude's context despite the prior 168h age-window fix. Root cause: `item.get("providerPublishTime", 0)` always returned 0 under the new nested schema → computed age ≈ 497,000 hours → every item exceeded the 168h window and was filtered out.

---

## Cross-References

- [[ozy-drift-log]] — Active drift log (new entries)
- [[ozy-drift-log-eras-02-10]] — Previous era (foundational)
- [[ozy-drift-log-eras-15-17]] — Next era (entry conditions & enrichment)
- [[ozy-doc-index]] — Full routing table
