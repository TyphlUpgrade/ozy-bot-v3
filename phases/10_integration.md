# Phase 10: Integration Testing + Startup Reconciliation + Paper Trading Prep

This is the final MVP phase. Everything is built — now we make it robust and ready for live paper trading.

## What to Build

### 1. Startup reconciliation protocol

This was a gap in the original spec. When the bot starts (especially after a crash), local state may have drifted from the broker's reality. Implement a `startup_reconciliation()` method in the orchestrator:

**Step 1: Broker position check**
- Fetch all positions from the broker via `get_positions()`.
- Compare against `portfolio.json`. For each:
  - Position exists in both → verify shares match. If mismatch, **log ERROR** and update local state to match broker (broker is source of truth for what we actually hold).
  - Position in broker but not local → **log ERROR** ("unknown position detected"). Add to local state with a flag `reconciled: true` and empty intention. Claude will evaluate it on the next reasoning cycle.
  - Position in local but not broker → **log ERROR** ("phantom position in local state"). Remove from local state. Check order history to understand what happened.

**Step 2: Order cleanup**
- Fetch all open orders from broker.
- Any local PENDING orders that don't exist broker-side → mark as CANCELLED locally.
- Any broker orders not tracked locally → log WARNING and add to local tracking.

**Step 3: Account state**
- Fetch account info. Verify equity, buying power, PDT flag.
- If equity < $25,500 → log WARNING, will block new entries via risk manager.
- Log the full account snapshot at INFO level.

**Step 4: Reasoning cache check**
- Check for reusable cached Claude response (same trading day, <60 min old).
- If found, load it. If not, the first slow loop cycle will trigger a fresh Claude call.

**Step 5: Validation gate**
- If any ERRORS were logged during reconciliation, enter a conservative startup mode: no new entries for the first 10 minutes, only position monitoring. This gives the operator time to review logs.
- If reconciliation is clean, proceed normally.

### 2. End-to-end integration test

Create `tests/test_integration.py`. This test uses mocks for all external services (broker, yfinance, Claude API) but runs the real orchestrator logic:

- **Full cycle test:** Set up mock data, run one full cycle of each loop (fast → medium → slow), verify:
  - Fast loop reconciled orders correctly
  - Medium loop generated technical signals and ranked opportunities
  - Slow loop trigger fired (use the "watchlist critically small" trigger since mock starts with empty watchlist)
  - An opportunity from Claude's response made it through ranking and risk validation
  - An order was placed via the mock broker

- **Override exit test:** Set up a mock position with indicators that trigger a quant override. Run fast loop. Verify:
  - Override detected
  - Market exit order placed
  - Position flagged for Claude re-evaluation

- **PDT blocking test:** Set up mock state with 2 existing day trades (buffer=1). Attempt to enter and exit on the same day. Verify the exit is blocked by PDT guard (unless it's an emergency override).

- **Degradation test:** Start a cycle, then make the mock Claude API raise an exception. Verify:
  - System enters quantitative-only mode
  - Existing positions continue to be managed
  - No new AI-driven entries are attempted
  - Retry logic is active

### 3. Configuration validation

Create a `scripts/validate_config.py` that:
- Loads config.json and validates all fields
- Checks credentials file exists and contains required keys
- Verifies prompt template files exist for the configured version
- Tests Alpaca API connectivity with the configured credentials
- Tests Claude API connectivity with a minimal test prompt
- Reports any issues clearly

### 4. Paper trading dry-run mode

Add a `--dry-run` flag to `main.py`:
- Everything runs normally EXCEPT orders are not actually submitted to the broker.
- Instead, orders are logged at INFO level with all details (symbol, side, qty, price, type).
- This lets you watch the system's decision-making without any capital at risk.
- Useful for the first few days of paper trading to build confidence.

### 5. README.md

Create a README that covers:
- What Ozymandias is (one paragraph)
- Prerequisites (Python 3.12+, Alpaca paper account, Anthropic API key)
- Setup steps (install deps, configure credentials, run config validation)
- How to start paper trading
- How to read the logs
- How dry-run mode works
- Architecture overview (link to the spec for details)
- Known limitations and post-MVP roadmap

### 6. Final cleanup pass

Review all modules for:
- Consistent error handling (no bare `except:`, always log errors)
- All TODO comments addressed or documented
- Type hints on all public methods
- Docstrings on all public classes and methods
- No hardcoded values that should be in config
- No `print()` statements (only logging)
- All imports are clean (no unused imports)
- State manager is used for all persistence (nothing writing files directly)

## Tests to Write

All the integration tests described in section 2 above, plus:

Create `tests/test_startup_reconciliation.py`:
- Test clean startup (local state matches broker) → no errors
- Test position mismatch → local updated to match broker
- Test phantom local position → removed from local state
- Test unknown broker position → added to local with reconciled flag
- Test stale local orders → marked cancelled
- Test conservative startup mode activates on reconciliation errors

## Done When
- All unit tests from all phases pass: `pytest tests/` with no failures
- Integration tests pass
- Config validation script passes with real credentials
- `python main.py --dry-run` starts cleanly, runs through loops, and logs decisions without placing orders
- `python main.py` in paper mode connects to Alpaca, detects market session, and operates correctly
- README is complete and a new developer could set up the project from it
- You're confident enough to let it run on paper trading during market hours

## What's Next (Post-MVP)
After running paper trading for at least 2 weeks and reviewing performance:
1. Additional data adapters (Alpha Vantage, Finnhub, Reddit) — one at a time
2. Backtesting harness + SimulatedBroker
3. Notification system for critical events
4. Alpaca websocket streaming for order updates
5. Performance analytics dashboard
