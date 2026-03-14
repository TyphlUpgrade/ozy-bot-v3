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
Phase: 03
Last completed: March 13  <!--All 150 tests pass: 62 Phase 01 + 28 Phase 02 + 60 Phase 03-->
Next up: Phase 04: Market Data Ingestion + Technical Analysis

## Dependencies
```
aiohttp>=3.9, alpaca-py>=0.30, yfinance>=0.2.31, pandas>=2.1, numpy>=1.26, anthropic>=0.40, python-dateutil>=2.8
```

## Spec Drift Log
Deviations from `ozymandias_v3_spec_revised.md` introduced during implementation. Read this before assuming the spec is authoritative on any listed item.

### Phase 02 — Broker Abstraction

**`BrokerInterface` return types (section 4.8)**

- `get_open_orders()`: spec says `list[Order]` → implemented as `list[OrderStatus]`
  — `Order` is the *input* type (what you submit); returning it here would be a spec bug. `OrderStatus` is correct.
- `get_position()`: spec says `Position | None` → implemented as `Optional[BrokerPosition]`
- `get_positions()`: spec says `list[Position]` → implemented as `list[BrokerPosition]`
  — The name `Position` conflicts with the `Position` dataclass in `state_manager.py`. Renamed to `BrokerPosition` in `broker_interface.py` to avoid ambiguity. The two types are distinct: `BrokerPosition` is the broker's live snapshot; `Position` is the persistent state record.

**`Order` dataclass**

- Added `client_order_id: Optional[str] = None` — not in spec. Used to correlate Alpaca orders back to local records without a second API call.

**`AlpacaBroker` async strategy**

- Spec (section 4.8) says "prefer the async client (`AsyncRest`)". Implemented with the synchronous `TradingClient` wrapped in `asyncio.to_thread()`.
  — Reason: `alpaca-py >= 0.30` deprecated `AsyncRest`; `TradingClient` is the current recommended client. `to_thread()` gives the same non-blocking behavior for our use case.

**`AlpacaBroker` constructor**

- Spec says constructor takes `environment` string (`"paper"` or `"live"`) → implemented as `paper: bool = True`
  — Boolean is cleaner at call sites; paper mode is the only mode used in this project.

### Phase 03 — Fill Protection + PDT Guard

**`StateChange` dataclass (section 7.1)**

- Spec describes the concept but does not define the dataclass fields. Implementation adds:
  - `change_type: str` — one of `"fill"`, `"partial_fill"`, `"cancel"`, `"partial_then_cancel"`, `"unexpected_fill"`, `"reject"`. Required for the orchestrator to route state changes to the right downstream handlers.

**`FillProtectionManager.available_buying_power()`**

- Not in main spec sections 7.1 or 5.4. Defined in `phases/03_fill_protection.md` §3 as an optional addition. Implemented as a method on `FillProtectionManager`.
  - Market orders deduct $0 (price unknown at submission time); limit orders deduct `qty × limit_price`.

**`PDTGuard.count_day_trades()` (section 7.2)**

- Spec signature: `count_day_trades(orders, portfolio) -> int`
- Implemented signature: `count_day_trades(orders, portfolio, reference_date: date | None = None) -> int`
  — `reference_date` overrides "today" for deterministic unit testing. Has no effect in production (defaults to current ET date).

**`PDTGuard.can_day_trade()` (section 7.2)**

- Spec signature: `can_day_trade(symbol: str) -> tuple[bool, str]` (single param)
- Implemented signature: `can_day_trade(symbol, orders, portfolio, is_emergency: bool = False, reference_date: date | None = None) -> tuple[bool, str]`
  — `orders` and `portfolio` must be passed explicitly (no internal state caching) to keep `PDTGuard` stateless and thread-safe. `is_emergency` replaces the separate `is_emergency_exit()` call pattern implied by the spec. `reference_date` is for testing (same as `count_day_trades`).

**`PDTGuard.is_emergency_exit()` (section 7.2)**

- Spec implies this drives the `is_emergency` path inside `can_day_trade()`. Implemented as a stub returning `False`; Phase 05 risk manager quant overrides will set this signal. The parameter was promoted to `can_day_trade(is_emergency=...)` so callers control it directly rather than the guard querying itself.

**Order status strings**

- Spec uses uppercase throughout (`PENDING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`). Alpaca returns lowercase (`new`, `partially_filled`, `filled`, `canceled`).
- `fill_protection.py` maps broker strings to uppercase via `_BROKER_STATUS_MAP` before storing. All internal state and comparisons use uppercase. `"canceled"` (Alpaca spelling) maps to `"CANCELLED"` (spec spelling).
