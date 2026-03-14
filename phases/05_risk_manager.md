# Phase 05: Risk Management Module

Read sections 4.7 (Risk Management) and 7.3 (Additional Safeguards) of `ozymandias_v3_spec_revised.md`.

## Context
Phases 01-04 gave us: state management, broker, fill protection, PDT guard, market data, and technical analysis. The risk manager ties many of these together — it uses the broker for account info, the PDT guard for day trade checks, and technical analysis for override signals.

## What to Build

### 1. Risk manager (`execution/risk_manager.py`)

Implement `RiskManager` class with these responsibilities:

**Pre-trade validation (called before any order placement):**
- `validate_entry(symbol, side, quantity, price, strategy) -> tuple[bool, str]`: Run ALL pre-trade checks and return (allowed, reason). Checks include:
  - Position size check: would this position exceed 20% of portfolio value?
  - Concurrent positions check: are we already at 8 open positions?
  - Daily loss check: has the daily loss limit (-2% of portfolio) been hit?
  - Equity floor check: is equity below $25,500? (delegates to PDT guard)
  - PDT day trade check: would this order create a day trade that exceeds limits? (delegates to PDT guard)
  - Market hours check: is the market in regular hours? Block new entries in pre/post market unless explicitly flagged. Block momentum entries in last 5 minutes (3:55-4:00 PM ET).
  - Buying power check: is there sufficient buying power after accounting for pending orders?
  - Minimum volume check: does the stock trade at least 100,000 shares/day average?

**Position sizing:**
- `calculate_position_size(symbol, entry_price, atr, account_value) -> int`: Compute shares using the ATR-based formula: `shares = (account_value * risk_per_trade_pct) / (atr * atr_multiplier)`. Cap at max position size (20% of portfolio / entry_price). Return integer shares.

**Quantitative override signals (section 4.7):**
These are hard exit triggers that operate independently of Claude. Implement each as a separate method:

- `check_vwap_crossover(position, indicators) -> bool`: Price crosses below VWAP on above-average volume (ratio > 1.3).
- `check_rsi_divergence(position, indicators) -> bool`: Bearish divergence detected. **Confirmation only** — cannot trigger alone.
- `check_roc_deceleration(position, indicators) -> bool`: 5-period ROC drops below its 10-period MA while price still rising.
- `check_momentum_score_flip(position, indicators) -> bool`: `price_change_pct * volume_ratio` flips sign after being strongly positive (>1.5) or negative (<-1.5).
- `check_atr_trailing_stop(position, indicators, intraday_high) -> bool`: Price drops more than 2x ATR(14) from intraday high since entry.

**Override evaluation:**
- `evaluate_overrides(position, indicators, intraday_high) -> tuple[bool, list[str]]`: Run all five override checks. Apply the trigger logic from section 4.7: signals 1, 3, 4, 5 trigger independently; signal 2 requires at least one other signal. Return (should_exit, list_of_triggered_signal_names).

**Daily loss tracking:**
- Track realized + unrealized P&L for the current trading day.
- `check_daily_loss(account, positions) -> tuple[bool, str]`: Return (trading_halted, reason) if daily loss exceeds the configured threshold.
- Reset the daily tracker at market open each day.

### 2. Settlement tracking (section 7.3)
- `check_settlement(symbol, portfolio) -> tuple[bool, str]`: For cash accounts or limited margin, check if selling a position would use unsettled funds (T+1). Track settlement dates per position entry.
- This is a lighter-touch check — log a WARNING if a potential GFV is detected, but don't hard-block (Alpaca handles settlement internally for margin accounts, so this is mostly defensive logging).

## Tests to Write

Create `tests/test_risk_manager.py`:
- Test position size calculation with known ATR and account values
- Test max position size cap (20% of portfolio)
- Test concurrent position limit blocks entry when at 8 positions
- Test daily loss halt triggers correctly
- Test equity floor blocks all entries below $25,500
- Test market hours blocking (pre-market, post-market, last 5 minutes)
- Test buying power check with pending orders subtracted

**Override signal tests** — these are critical:
- Test VWAP crossover: create indicators where price just crossed below VWAP with high volume → should trigger
- Test VWAP crossover: price below VWAP but low volume → should NOT trigger
- Test RSI divergence alone → should NOT trigger exit
- Test RSI divergence + VWAP crossover → should trigger
- Test ROC deceleration detection
- Test momentum score flip from positive to negative
- Test ATR trailing stop with various price/ATR combinations
- Test combined override evaluation with multiple signals

## Done When
- All tests pass
- The `validate_entry()` method correctly blocks trades that violate any risk rule
- Override signals correctly detect reversal conditions from synthetic indicator data
- Position sizing produces sensible share counts for realistic account sizes and ATR values
