# Ozymandias v3 — Automated Trading Bot

## What This Is
An automated stock trading bot using Claude API for strategic reasoning + quantitative technical analysis for execution. Targets aggressive momentum and swing trading on high-volatility, high-liquidity equities. Alpaca paper trading initially.

## Full Spec
The complete specification is in `ozymandias_v3_spec_revised.md` at the project root. **Read the relevant section(s) of this spec before implementing any module.** The spec is the source of truth for all design decisions.

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

## Key Design Rules
- Modules communicate via interfaces and JSON, never direct coupling
- Only the orchestrator knows about all other modules
- Prompt templates are versioned files in `config/prompts/`, never hardcoded
- Risk manager has override authority over everything — can cancel orders, force exits, block entries
- Fill protection: never place a new order for a symbol that has a PENDING or PARTIALLY_FILLED order
- PDT buffer: default reserve 1 of 3 allowed day trades for emergency exits

## Directory Structure
```
ozymandias/
├── main.py
├── config/           # config.json, credentials.enc, prompts/
├── core/             # orchestrator, state_manager, logger, market_hours
├── intelligence/     # claude_reasoning, technical_analysis, opportunity_ranker
├── data/adapters/    # base.py (ABCs), yfinance_adapter.py (MVP)
├── execution/        # broker_interface, alpaca_broker, risk_manager, fill_protection
├── strategies/       # base_strategy, momentum_strategy, swing_strategy
├── state/            # portfolio.json, watchlist.json, orders.json
├── reasoning_cache/  # Temporary Claude response cache
├── logs/             # current.log, previous.log
└── tests/
```

## Testing Standards
- Every module gets unit tests
- Technical analysis functions tested against hand-calculated expected values
- Fill protection tested for all edge cases: partial fills, cancel-during-fill race, unexpected fills
- Use pytest + pytest-asyncio for async tests
- Mock broker and external APIs in tests — never hit real APIs in unit tests

## Current Build Phase
<!-- UPDATE THIS as you complete each phase -->
Phase: 04
Last completed: March 13  <!--All 231 tests pass: 62 Phase 01 + 28 Phase 02 + 60 Phase 03 + 81 Phase 04-->
Next up: Phase 05: Risk Manager

## Dependencies
```
aiohttp>=3.9, alpaca-py>=0.30, yfinance>=0.2.31, pandas>=2.1, numpy>=1.26, anthropic>=0.40, python-dateutil>=2.8
```

## Spec Drift Log
Deviations from `ozymandias_v3_spec_revised.md` introduced during implementation. Read this before assuming the spec is authoritative on any listed item.

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
