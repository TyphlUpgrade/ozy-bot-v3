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
Phase: 01
Last completed: March 13 7:10pm  <!--Claude implemented and showed nothing to debug after tests-->
Next up: Phase 02 : Broker Abstraction + Alpaca Paper Trading

## Dependencies
```
aiohttp>=3.9, alpaca-py>=0.30, yfinance>=0.2.31, pandas>=2.1, numpy>=1.26, anthropic>=0.40, python-dateutil>=2.8
```
