# Phase 09: Orchestrator — Full System Wiring

Read section 4.1 (Orchestrator / Scheduler) of `ozymandias_v3_spec_revised.md` thoroughly. This is the most complex phase.

## Context
Phases 01-08 built every module individually: state management, broker, fill protection, PDT guard, market data, technical analysis, risk management, Claude reasoning, opportunity ranking, and strategies. The orchestrator wires them all together into three concurrent async loops.

**Approach this phase incrementally.** Don't try to build all three loops at once. Build and test them one at a time: fast loop first, then medium loop, then slow loop.

## What to Build

### 1. Orchestrator core (`core/orchestrator.py`)

Implement an `Orchestrator` class that:
- Initializes all modules (broker, data adapters, TA engine, Claude reasoning, risk manager, ranker, strategies, fill protection, PDT guard, state manager)
- Runs three concurrent async tasks using `asyncio.TaskGroup`
- Handles shutdown gracefully (cancel all tasks, save state, close connections)

```python
class Orchestrator:
    async def run(self):
        """Main entry point. Runs until interrupted."""
        await self._startup()
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._fast_loop())
                tg.create_task(self._medium_loop())
                tg.create_task(self._slow_loop())
        except* KeyboardInterrupt:
            await self._shutdown()
```

### 2. Fast loop (every 5-15 seconds)

Per section 4.1, each cycle:
1. **Poll broker** for order status updates on all PENDING and PARTIALLY_FILLED orders.
2. **Reconcile** local order state with broker-reported state (via fill protection manager).
3. **Handle stale orders**: cancel any limit orders past the timeout threshold. Wait for confirmation. Process the cancel result (including the cancel-during-fill race).
4. **Execute quant overrides**: for each open position, check all override signals using current indicator data. If triggered, place a market exit order immediately. Log the override. Flag for Claude re-evaluation.
5. **PDT guard check**: verify day-trade count hasn't been exceeded. If approaching the limit, log a WARNING.
6. **Position sync**: compare local portfolio state with broker-reported positions. Log any discrepancies.

The fast loop must never throw an unhandled exception. Wrap each step in try/except, log errors, continue to the next step.

```python
async def _fast_loop(self):
    while True:
        try:
            await self._fast_loop_cycle()
        except Exception as e:
            self.logger.error(f"Fast loop error: {e}", exc_info=True)
        await asyncio.sleep(self.config.scheduler.fast_loop_sec)
```

### 3. Medium loop (every 1-5 minutes)

Each cycle:
1. **Fetch latest bars** from yfinance for all watchlist Tier 1 symbols and open positions.
2. **Run technical analysis** on all fetched data. Compute indicators and generate signal summaries.
3. **Detect entry/exit signals** by running active strategies against the indicators.
4. **Re-rank opportunity queue**: combine the latest Claude reasoning output (from the most recent slow loop cycle) with fresh technical data through the opportunity ranker.
5. **Execute top opportunities**: take the highest-ranked opportunity from the queue, validate through risk manager, and if approved, place the order via broker. Only execute one new entry per medium loop cycle to avoid overtrading.
6. **Re-evaluate open positions**: run strategy `evaluate_position()` on each open position. If any strategy recommends exit, validate through risk manager and place exit order.

### 4. Slow loop (event-driven, checked every 5 minutes)

This is the most nuanced loop. Per section 4.1:

**Trigger evaluation** — each 5-minute check evaluates:
- Time ceiling: 60+ minutes since last Claude call
- Price move: any Tier 1 symbol or position moved >2% since last evaluation
- Position approaching target: within 1% of profit target or stop loss
- Override exit occurred: a quant override forced an exit since last Claude call
- Market session transition: market just opened (9:30 AM ET) or approaching close (3:30 PM ET)
- Watchlist critically small: fewer than 10 tickers

If NO trigger fires, the cycle is a no-op (just update trigger state and return).

**When triggered:**
1. Assemble context and call Claude reasoning engine.
2. Process Claude's response:
   - Apply watchlist changes (add/remove tickers, update tiers)
   - Update position review notes
   - Feed new opportunities to the ranker (they'll be picked up by the next medium loop)
3. Cache the reasoning response.

**Critical rules:**
- If a Claude call is already in-flight, skip (no concurrent Claude requests).
- Claude calls are async — while waiting for the response, the fast and medium loops continue.
- On Claude API failure: enter quantitative-only mode (no new AI-driven entries, continue managing positions with technical signals and overrides). Retry with exponential backoff.

Implement the trigger system:
```python
class SlowLoopTriggerState:
    last_claude_call_utc: datetime | None
    last_prices: dict[str, float]  # symbol -> price at last evaluation
    last_override_exit_count: int
    # ... etc

def check_triggers(self, state: SlowLoopTriggerState) -> list[str]:
    """Return list of triggered condition names, or empty if none."""
    ...
```

### 5. Entry point (`main.py`)

Create a clean entry point:
- Parse command-line arguments (config path, log level, dry-run mode)
- Load configuration
- Initialize orchestrator
- Handle SIGINT/SIGTERM for graceful shutdown
- Print startup banner with: environment (paper/live), account equity, watchlist size, active strategies

### 6. Graceful degradation

Implement the degradation table from section 4.1:
- Claude API failure → quantitative-only mode
- yfinance failure → halt new entries, monitor positions only
- Broker API failure → safe mode (no new orders), retry with backoff
- If broker is unreachable for >5 minutes → log alert

Track degradation state in the orchestrator and adjust loop behavior accordingly:
```python
class DegradationState:
    claude_available: bool = True
    market_data_available: bool = True
    broker_available: bool = True
    safe_mode: bool = False
```

## Tests to Write

This is hard to unit test in isolation because it's integration code. Focus on:

Create `tests/test_orchestrator.py`:
- Test that the trigger evaluation correctly identifies when each trigger fires
- Test that no trigger = no Claude call
- Test that multiple triggers in the same cycle result in only one Claude call
- Test that concurrent Claude call prevention works (second trigger while call is in-flight is skipped)
- Test graceful degradation: mock Claude API failure, verify system enters quantitative-only mode
- Test graceful degradation: mock broker failure, verify safe mode activation after 5 minutes
- Test fast loop error handling: simulate an exception in one step, verify other steps still execute
- Test that the medium loop only executes one new entry per cycle

## Done When
- All tests pass
- You can run `python main.py` and see the orchestrator start up, detect the current market session, and begin running loops (even if the market is closed, it should handle that gracefully)
- The fast loop polls the broker without errors
- The slow loop trigger system correctly identifies trigger conditions
- Graceful shutdown on Ctrl+C saves state and exits cleanly
