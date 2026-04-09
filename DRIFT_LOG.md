> **Migration in progress:** This file's content is being migrated to the OMC wiki.
> See `.omc/wiki/ozy-doc-index.md` for the wiki routing table.
> **Cutoff: 2026-04-14.** After this date, new entries go in wiki pages only. This file is frozen.

# Spec Drift Log

Deviations from `ozymandias_v3_spec_revised.md` introduced during implementation. This file takes precedence over the spec on any listed item.

Read the relevant phase section before modifying or debugging any module built in that phase.

## File Index

Maps key source files to the sections that contain relevant drift entries. Use this to find what changed in a module before touching it. Omits test files and one-off prompt version entries — those are searchable.

**Update when adding an entry:** add the new section name to the row(s) for the file(s) it touches.

| File | Relevant sections |
|------|-------------------|
| `core/signals.py` | Agentic Workflow — Signal Wiring |
| `core/orchestrator.py` | Agentic Workflow — Signal Wiring |
| `core/fill_handler.py` | Agentic Workflow — Signal Wiring |

---

### Entry format
```
**`identifier`** · spec §X.Y · `path/to/file.py`
- **Spec:** what the spec says, or *(not defined)* for pure additions
- **Impl:** what was actually implemented
- **Why:** reason for the deviation
```

---

### Phase 02 — Broker Abstraction

**`BrokerInterface.get_open_orders()`** · spec §4.8 · `execution/broker_interface.py`
- **Spec:** returns `list[Order]`
- **Impl:** returns `list[OrderStatus]`
- **Why:** `Order` is the submission input type; returning it as query output would be a spec bug. `OrderStatus` is the correct query result type.

**`BrokerInterface.get_position()` / `get_positions()`** · spec §4.8 · `execution/broker_interface.py`
- **Spec:** returns `Position | None` / `list[Position]`
- **Impl:** returns `Optional[BrokerPosition]` / `list[BrokerPosition]`
- **Why:** `Position` conflicts with the `Position` dataclass in `state_manager.py`. Renamed to `BrokerPosition` to avoid ambiguity. The two types are distinct: `BrokerPosition` is the broker's live snapshot; `Position` is the persistent state record.

**`Order.client_order_id`** · spec §4.8 · `execution/broker_interface.py`
- **Spec:** *(not defined)*
- **Impl:** `client_order_id: Optional[str] = None` added to `Order` dataclass
- **Why:** Required to correlate Alpaca orders back to local records without a second API round-trip.

**`AlpacaBroker` async strategy** · spec §4.8 · `execution/alpaca_broker.py`
- **Spec:** "prefer the async client (`AsyncRest`)"
- **Impl:** synchronous `TradingClient` wrapped in `asyncio.to_thread()`
- **Why:** `alpaca-py >= 0.30` deprecated `AsyncRest`; `TradingClient` is the current recommended client. `to_thread()` gives the same non-blocking behaviour.

**`AlpacaBroker` constructor `environment` param** · spec §4.8 · `execution/alpaca_broker.py`
- **Spec:** constructor takes `environment: str` (`"paper"` or `"live"`)
- **Impl:** `paper: bool = True`
- **Why:** Boolean is cleaner at call sites; paper mode is the only mode used in this project.

---

### Phase 03 — Fill Protection + PDT Guard

**`StateChange.change_type`** · spec §7.1 · `execution/fill_protection.py`
- **Spec:** *(not defined — spec describes the concept but not the dataclass fields)*
- **Impl:** `change_type: str` added; one of `"fill"`, `"partial_fill"`, `"cancel"`, `"partial_then_cancel"`, `"unexpected_fill"`, `"reject"`
- **Why:** The orchestrator needs to route state changes to the correct downstream handlers.

**`FillProtectionManager.available_buying_power()`** · phases/03 §3 · `execution/fill_protection.py`
- **Spec:** *(not in main spec §7.1/§5.4; listed as optional in `phases/03_fill_protection.md` §3)*
- **Impl:** method implemented on `FillProtectionManager`; limit orders deduct `qty × limit_price`, market orders deduct $0
- **Why:** Needed to prevent over-commitment of buying power across pending limit orders.

**`PDTGuard.count_day_trades()` signature** · spec §7.2 · `execution/pdt_guard.py`
- **Spec:** `count_day_trades(orders, portfolio) -> int`
- **Impl:** `count_day_trades(orders, portfolio, reference_date: date | None = None) -> int`
- **Why:** `reference_date` overrides "today" for deterministic unit testing; no effect in production.

**`PDTGuard.can_day_trade()` signature** · spec §7.2 · `execution/pdt_guard.py`
- **Spec:** `can_day_trade(symbol: str) -> tuple[bool, str]`
- **Impl:** `can_day_trade(symbol, orders, portfolio, is_emergency: bool = False, reference_date: date | None = None) -> tuple[bool, str]`
- **Why:** `orders`/`portfolio` passed explicitly to keep `PDTGuard` stateless and thread-safe. `is_emergency` subsumes the separate `is_emergency_exit()` call pattern the spec implies. `reference_date` is for testing.

**`PDTGuard.is_emergency_exit()`** · spec §7.2 · `execution/pdt_guard.py`
- **Spec:** implied as a method that drives the emergency path inside `can_day_trade()`
- **Impl:** stub returning `False`; the `is_emergency` parameter on `can_day_trade()` serves this role
- **Why:** Phase 05 risk manager will set the emergency signal; promoting it to a caller-controlled parameter is cleaner than the guard querying itself.

**Order status string casing** · spec §7.1 · `execution/fill_protection.py`
- **Spec:** uppercase throughout (`PENDING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`)
- **Impl:** Alpaca returns lowercase (`new`, `partially_filled`, `filled`, `canceled`); `_BROKER_STATUS_MAP` in `fill_protection.py` normalises to uppercase on ingestion. `"canceled"` (Alpaca spelling) → `"CANCELLED"` (spec spelling).
- **Why:** Broker wire format differs from spec; normalisation at the boundary keeps all internal logic spec-compliant.

---

### Phase 04 — Market Data + Technical Analysis

**`rsi_divergence` signal output type** · spec §4.4 · `intelligence/technical_analysis.py`
- **Spec:** `"rsi_divergence": false` (plain boolean in the example output)
- **Impl:** `False | "bearish" | "bullish"` — `False` when no divergence detected, string otherwise
- **Why:** A plain bool loses the direction. `compute_composite_score()` applies different adjustments for bearish (−0.2) vs bullish (+0.1); it needs to distinguish the two.

**RSI divergence composite score treatment** · spec §4.4 · `intelligence/technical_analysis.py`
- **Spec:** table lists RSI divergence with weight `0.05` and values `−0.2 penalty` / `+0.1 bonus`
- **Impl:** `−0.2` and `+0.1` are applied as direct absolute adjustments to the final score, not multiplied by `0.05`
- **Why:** All other signals are `score × weight` where score ∈ [0, 1]. A value of `−0.2` is outside that range and cannot fit the weighted pattern. The spec's "penalty/bonus" language signals these are additive, not multiplicative.

**`compute_atr()` smoothing method** · spec §4.4 · `intelligence/technical_analysis.py`
- **Spec:** "Average True Range using EMA smoothing" (unspecified which EMA variant)
- **Impl:** Wilder's smoothing (`ewm(com=length-1, adjust=False)`; alpha = 1/length)
- **Why:** Wilder's ATR is the industry standard and uses the same smoothing constant as Wilder's RSI, keeping the two indicators consistent.

---

### Phase 05 — Risk Manager

**`validate_entry()` signature** · spec §4.7 · `execution/risk_manager.py`
- **Spec:** `validate_entry(symbol, side, quantity, price, strategy) -> tuple[bool, str]`
- **Impl:** `validate_entry(symbol, side, quantity, price, strategy, account, portfolio, orders, avg_daily_volume=None, now=None)`
- **Why:** The spec omits state parameters. All state (account, portfolio, orders) is passed explicitly to keep `RiskManager` stateless per call — same pattern as `PDTGuard.can_day_trade()`. `avg_daily_volume` allows skipping the min-volume check when fundamentals aren't available. `now` enables deterministic testing.

**`check_daily_loss()` signature** · spec §4.7 · `execution/risk_manager.py`
- **Spec:** `check_daily_loss(account, positions) -> tuple[bool, str]`
- **Impl:** `check_daily_loss(account, now=None) -> tuple[bool, str]` — no `positions` parameter
- **Why:** The spec mentions tracking "realized + unrealized P&L" but the simplest correct implementation compares current equity to start-of-day equity, which already includes unrealized P&L in the broker's equity figure. Computing it from positions separately would be redundant. `now` overrides the clock for testing.

**`_pending_order_commitment()` buying power helper** · spec §7.3 · `execution/risk_manager.py`
- **Spec:** "Calculate: `available_buying_power = reported_buying_power - sum(pending_order_values)`" — no explicit location specified
- **Impl:** Module-level function `_pending_order_commitment(orders)` in `risk_manager.py` rather than delegating to `FillProtectionManager.available_buying_power()`
- **Why:** `validate_entry()` needs this calculation but should not require a `FillProtectionManager` instance (which is stateful and disk-backed). The arithmetic is identical; avoiding the dependency keeps `RiskManager` testable without a full FillProtectionManager setup.

**`check_vwap_crossover()` — "crossing" vs "currently below"** · spec §4.7 · `execution/risk_manager.py`
- **Spec:** "Price *crosses* below VWAP" (implies detecting the transition event)
- **Impl:** Detects "price is currently below VWAP with volume_ratio > 1.3" — effectively a level check, not a transition check
- **Why:** Override checks run every fast loop (~10s). On the first cycle where price drops below VWAP, the condition becomes true — that cycle IS the cross event. Tracking a prior-state flag would add complexity with no practical benefit at 10-second resolution.

**Settlement check does not hard-block** · spec §7.3 · `execution/risk_manager.py`
- **Spec:** *(implied that GFV check could block)*
- **Impl:** `check_settlement()` returns a risk flag and logs a WARNING but does not prevent the trade
- **Why:** The spec explicitly says "this is mostly defensive logging" and Alpaca handles settlement for margin accounts. Hard-blocking would cause unnecessary friction; the WARNING surfaces the risk for the operator to notice.

---

### Phase 06 — Claude AI Reasoning

**Prompt template substitution uses regex, not `str.format_map()`** · spec §4.3 · `intelligence/claude_reasoning.py`
- **Spec:** *(not specified — spec shows templates with `{placeholder}` syntax)*
- **Impl:** `re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", ...)` — only substitutes `{plain_identifier}` tokens
- **Why:** The prompt templates contain JSON response schema examples with `{"key": value}` blocks. `str.format_map()` interprets these as Python format strings and crashes. The regex pattern restricts substitution to bare identifiers, leaving JSON `{` `}` untouched.

**`assemble_reasoning_context` `indicators` parameter accepts both formats** · spec §4.3 · `intelligence/claude_reasoning.py`
- **Spec:** *(indicators dict format not precisely specified)*
- **Impl:** Accepts either `{symbol: signal_summary_dict}` (output of `generate_signal_summary`) or `{symbol: signals_flat_dict}`. The assembler checks for a `"signals"` sub-key and falls back to treating the whole dict as signals.
- **Why:** `generate_signal_summary` wraps signals under a `"signals"` key alongside `"composite_technical_score"`. Callers that pass the full summary dict and callers that pass just the signals dict both work without special-casing.

---

### Phase 07 — Opportunity Ranker

**`apply_hard_filters()` signature** · spec §4.5 · `intelligence/opportunity_ranker.py`
- **Spec:** `apply_hard_filters(opportunity, account_info, portfolio, pdt_guard, market_hours)`
- **Impl:** adds three optional params: `market_hours_fn=None` (injectable callable, defaults to `is_market_open()`), `orders: list | None = None` (forwarded to PDT guard), `technical_signals: dict | None = None` (used for volume filter)
- **Why:** `PDTGuard.can_day_trade()` requires `orders` explicitly (Phase 03 pattern). `market_hours_fn` enables off-hours testing without patching globals.

**`rank_opportunities()` signature** · spec §4.5 · `intelligence/opportunity_ranker.py`
- **Spec:** `rank_opportunities(..., market_hours)`
- **Impl:** `market_hours` → `market_hours_fn` (optional, defaults to `is_market_open()`); `orders` parameter added and forwarded to `apply_hard_filters()`
- **Why:** Consistent with Phase 03/05 pattern of passing state explicitly.

**`rank_exit_actions()` — signal key name** · spec §4.5 · `intelligence/opportunity_ranker.py`
- **Spec:** signal dict key is `"composite_technical_score"` (from `generate_signal_summary()`)
- **Impl:** fixed to read `signals.get("composite_technical_score", 0.5)`; tests updated to use nested format `{"composite_technical_score": X}` matching the real TA output schema
- **Why:** Was reading wrong key `"composite_score"`, silently defaulting to 0.5 — fixed 2026-03-15.

---

### Phase 08 — Strategy Modules

**`Strategy._DEFAULT_PARAMS` + `_p()` helper** · spec §4.6 · `strategies/base_strategy.py`
- **Spec:** *(not defined)*
- **Impl:** `_DEFAULT_PARAMS: dict[str, Any] = {}` class attribute (subclasses override); `_p(key)` shorthand for `self._params[key]`
- **Why:** Pure additions for convenience. Reduces boilerplate in strategy subclasses.

**`MomentumStrategy` end-of-day exit** · spec §4.6 · `strategies/momentum_strategy.py`
- **Spec:** "exit before 3:55 PM ET if no swing hold thesis"
- **Impl:** Unconditional forced exit at end of day via `is_last_five_minutes()` — no swing-hold thesis check
- **Why:** Stricter than spec; simplifies implementation. All momentum positions are exited EOD.

**`get_strategy()` registry** · spec §4.6 · `strategies/base_strategy.py`
- **Spec:** "load active strategies from config (`strategy.active_strategies`)"
- **Impl:** Hardcoded dict `{"momentum": MomentumStrategy, "swing": SwingStrategy}`; new strategies require code changes
- **Why:** Adequate for v3 scope; deferred config-driven loading to future phase if new strategies are added.

**`SwingStrategy` trend double-check** · spec §4.6 · `strategies/swing_strategy.py`
- **Spec:** trend-not-broken is a hard requirement
- **Impl:** Checked twice: hard reject if `trend == "bearish_aligned"` (line 76) AND counted as soft signal condition (line 129)
- **Why:** Redundant but not incorrect. Hard filter ensures no false positives; soft condition boosts score when trend is healthy.

---

### Phase 09 — Orchestrator

**`DegradationState`** · spec §N/A · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `DegradationState` dataclass tracks `broker_available`, `claude_available`, `market_data_available`, `safe_mode`, `claude_backoff_until_utc`. All degradation logic flows through this.
- **Why:** Needed to coordinate backoff and safe-mode behaviour across the three loops.

**`SlowLoopTriggerState`** · spec §N/A · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `SlowLoopTriggerState` dataclass holds `last_claude_call_utc`, `last_trigger_prices`, `last_session`, `last_override_exit_count`, `claude_call_in_flight`.
- **Why:** Encapsulates all mutable trigger-evaluation state to make `_check_triggers()` testable without side effects.

**`_result_from_raw_reasoning()` call contract** · spec §4.3 · `intelligence/claude_reasoning.py`
- **Spec:** *(internal helper not specified)*
- **Impl:** Accepts the `parsed_response` sub-dict (i.e. the Claude JSON itself), NOT the full cache record. Callers must extract: `parsed = (cached or {}).get("parsed_response") or {}` before calling.
- **Why:** `ReasoningCache.load_latest_if_fresh()` returns an envelope `{"timestamp": …, "parsed_response": {…}}`. Passing the envelope directly causes all fields to resolve to empty lists/dicts.

**Claude failure backoff timing** · spec §4.3 · `core/orchestrator.py`
- **Spec:** "exponential backoff" — values unspecified
- **Impl:** base 30 s, doubles each failure (`30 × 2^(n-1)`), capped at 600 s. Tracked via `_claude_failure_count: int` on `Orchestrator`.
- **Why:** Values chosen to be responsive for transient errors while not hammering the API.

**`is_market_open()` in orchestrator medium loop** · spec §N/A · `core/orchestrator.py`
- **Spec:** *(not specified for test environments)*
- **Impl:** `is_market_open()` uses the real clock. Tests run outside NYSE hours MUST patch `ozymandias.core.orchestrator.is_market_open` to `True`, otherwise the ranker's `apply_hard_filters()` rejects all candidates. Integration tests that also call `validate_entry` must additionally patch `ozymandias.execution.risk_manager.get_current_session` to `Session.REGULAR_HOURS`.
- **Why:** Hard filter is correct in production; patching is necessary for deterministic off-hours testing.

---

### Phase 10 — Integration + Startup Reconciliation

**`Position.reconciled`** · spec §N/A · `core/state_manager.py`
- **Spec:** *(not defined)*
- **Impl:** `reconciled: bool = False` field added to the `Position` dataclass; set to `True` for positions discovered during startup reconciliation that have no local trade record.
- **Why:** Flags unknown positions for Claude to evaluate on the next reasoning cycle without blocking the system from tracking them.

**`Orchestrator.startup_reconciliation()`** · spec §N/A · `core/orchestrator.py`
- **Spec:** *(not defined — gap in original spec)*
- **Impl:** 5-step protocol run once after `_startup()`: (1) compare broker positions vs. local, update mismatches; (2) mark orphaned local orders CANCELLED; (3) log full account snapshot; (4) check reasoning cache; (5) enter conservative startup mode for `scheduler.conservative_startup_mode_min` minutes if any errors were found.
- **Why:** Necessary for safe restart after crashes. Without reconciliation, the bot could act on stale local state that diverged from the broker during downtime.

**`scheduler.conservative_startup_mode_min`** · spec §N/A · `core/config.py`
- **Spec:** *(not defined — Phase 10 spec mentioned "10 minutes" as a hardcoded value)*
- **Impl:** `conservative_startup_mode_min: int = 10` added to `SchedulerConfig`; read by `startup_reconciliation()` instead of a hardcoded literal.
- **Why:** Operator-configurable; default 10 minutes.

**`Orchestrator.__init__` dry-run and conservative-mode attributes** · spec §N/A · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `self._dry_run: bool` and `self._conservative_mode_until: Optional[datetime]` added in `__init__`. `_apply_dry_run_mode()` monkey-patches `broker.place_order` with a logging stub when `--dry-run` is active.
- **Why:** Dry-run is implemented as a broker-layer shim so all upstream logic (risk checks, ranking, position sizing) runs identically — only the actual order submission is suppressed.

**`OrderStatus` has no `symbol` or `side` fields** · spec §4.8 · `execution/broker_interface.py`
- **Spec:** *(not explicitly specified — Phase 10 reconciliation code initially assumed these fields existed)*
- **Impl:** `OrderStatus` contains only `order_id`, `status`, `filled_qty`, `remaining_qty`, `filled_avg_price`, `submitted_at`, `filled_at`, `canceled_at`. Symbol and side are on `Fill`, not `OrderStatus`.
- **Why:** Discovered during Phase 10 integration. Startup reconciliation step 2 and the `test_integration.py` `OrderStatus` constructors were corrected to use only fields that exist.

**`validate_entry` market hours check uses real clock** · spec §4.7 · `execution/risk_manager.py`
- **Spec:** *(not specified for test environments)*
- **Impl:** `_check_market_hours` calls `get_current_session(now)` where `now` defaults to `datetime.now(ET)`. Integration tests that run end-to-end through `validate_entry` outside NYSE hours must patch `ozymandias.execution.risk_manager.get_current_session` to return `Session.REGULAR_HOURS` in addition to patching `is_market_open` in the orchestrator.
- **Why:** Two separate real-clock calls exist at different layers of the stack; patching only one is insufficient for full cycle tests.

**PDT `count_day_trades` only counts business-day fills** · spec §7.2 · `execution/pdt_guard.py`
- **Spec:** *(behaviour on weekends not specified)*
- **Impl:** `_business_days_window` never includes Saturday/Sunday. Orders with `filled_at` on a weekend will never be counted as day trades, regardless of the reference date.
- **Why:** Correct behaviour — markets are closed on weekends. Tests that construct order records must use a weekday `filled_at` and pass a matching `reference_date` to `can_day_trade`; otherwise count will be 0 when the test suite runs on a weekend.

---

### Post-MVP (Anti-bias hardening)

**`ReasoningResult.rejected_opportunities`** · spec *(not defined)* · `intelligence/claude_reasoning.py`
- **Spec:** *(not defined)*
- **Impl:** New field `rejected_opportunities: list[dict]` added to `ReasoningResult`. `_result_from_raw_reasoning()` populates it from `raw.get("rejected_opportunities", [])`. `run_reasoning_cycle()` logs each entry at INFO after a successful parse.
- **Why:** Forces Claude to articulate specific bear cases for candidates it considered but rejected. Creates a visible audit trail of near-misses without adding to the execution pipeline.

**`reasoning.txt` and `review.txt` adversarial instructions** · spec *(not defined)* · `config/prompts/v3.3.0/`
- **Spec:** *(not defined)*
- **Impl:** `reasoning.txt` instruction 1 strengthened to require a specific bear argument inside `updated_reasoning` for every position review (even holds). New instruction 5 added requiring `rejected_opportunities` list with specific, non-generic rejection reasons. `review.txt` `notes` description updated to require adversarial content; evaluation item 5 added.
- **Why:** Both prompts previously incentivised only optimistic framing. The changes force Claude to surface counterarguments without changing the response schema.

**`min_conviction_threshold` in ranker** · spec *(not defined)* · `intelligence/opportunity_ranker.py`, `config/config.json`
- **Spec:** *(not defined)*
- **Impl:** `min_conviction_threshold: float = 0.10` in `RankerConfig` and `config.json`. Hard filter runs before scoring (cheapest check first). Rejections logged at INFO.
- **Why:** 0.10 is a sanity floor, not a quality gate — it catches degenerate zero/near-zero conviction values from malformed Claude output while leaving technically-strong, narratively-uncertain setups untouched. The existing `weight_ai=0.35` already penalises low conviction in the composite score; a high threshold would incorrectly block legitimate short-term technical momentum plays.

**`call_claude()` `max_tokens_override` param** · spec §4.3 · `intelligence/claude_reasoning.py`
- **Spec:** fixed `max_tokens_per_cycle` used for all calls
- **Impl:** `max_tokens_override: int | None = None` added. When provided, overrides the config value for that call. All existing callers pass no override (unchanged). Truncation via `stop_reason == "max_tokens"` logged at WARNING.
- **Why:** Thesis challenge responses are structurally tiny (`{proceed, conviction, reasoning}`); 512 tokens is sufficient and reduces cost. The override avoids adding a new config key for a single specialised call.

**`run_thesis_challenge()` method** · spec *(not defined)* · `intelligence/claude_reasoning.py`
- **Spec:** *(not defined)*
- **Impl:** New async method on `ClaudeReasoningEngine`. Loads `thesis_challenge.txt`, sends compact key-signals subset (not full TA summary), calls Claude with `max_tokens_override=512`. Returns `{proceed, conviction, challenge_reasoning}` dict or `None` on parse failure (caller proceeds with original sizing).
- **Why:** Adversarial second opinion specifically for large-position entries. Separate method keeps the fast path (`run_reasoning_cycle`) unchanged.

**`Orchestrator._latest_market_context`** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** `self._latest_market_context: dict = {}` stored in `__init__`; populated by the slow loop immediately before calling Claude. Consumed by `_medium_try_entry()` for thesis challenge calls.
- **Why:** Medium and slow loops run on independent timers. The medium loop needs access to the last-known market context without triggering a new broker fetch.

**Thesis challenge in `_medium_try_entry()`** · spec *(not defined)* · `core/orchestrator.py`
- **Spec:** *(not defined)*
- **Impl:** After fill-protection check passes, if `top.position_size_pct >= config.ranker.thesis_challenge_size_threshold` (default 0.15), `_claude.run_thesis_challenge()` is called. `proceed=False` → return immediately (no order). Lower `conviction` → quantity scaled proportionally (`max(1, int(qty * ratio))`). `None` return (parse failure) → proceed with original quantity.
- **Why:** Large positions have the highest damage potential if wrong. Adding a synchronous adversarial check here is acceptable because the medium loop runs every 120 s — not latency-sensitive. Small positions (< 15%) skip the check entirely.

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
