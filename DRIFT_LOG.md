# Spec Drift Log

Deviations from `ozymandias_v3_spec_revised.md` introduced during implementation. This file takes precedence over the spec on any listed item.

Read the relevant phase section before modifying or debugging any module built in that phase.

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
