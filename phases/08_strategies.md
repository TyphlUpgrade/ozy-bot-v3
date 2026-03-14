# Phase 08: Strategy Modules

Read section 4.6 (Strategy Modules) of `ozymandias_v3_spec_revised.md`.

## Context
Phases 01-07 gave us the complete data pipeline (market data → technical analysis → Claude reasoning → opportunity ranking) and the complete execution pipeline (risk management → fill protection → broker). Strategy modules sit between these — they define the specific entry/exit logic for each trading approach.

## What to Build

### 1. Strategy base class (`strategies/base_strategy.py`)

Implement the `Strategy` ABC from section 4.6:
```python
class Strategy(ABC):
    async def generate_signals(self, symbol, market_data, indicators) -> list[Signal]
    async def evaluate_position(self, position, market_data, indicators) -> PositionEval
    async def suggest_exit(self, position, market_data, indicators) -> ExitSuggestion
    def get_parameters(self) -> dict
    def set_parameters(self, params: dict) -> None
```

Define the data types:
- `Signal`: symbol, direction (long), strength (0-1), entry_price, stop_price, target_price, timeframe, reasoning
- `PositionEval`: symbol, action (hold/scale_in/scale_out/exit), confidence, reasoning, adjusted_targets (optional)
- `ExitSuggestion`: symbol, exit_price, order_type (market/limit), urgency (0-1 where 1 = immediate), reasoning

### 2. Momentum strategy (`strategies/momentum_strategy.py`)

Implement `MomentumStrategy(Strategy)` for short-term (days) momentum plays:

**Entry signals** — generate a buy signal when:
- Price is above VWAP
- RSI is between 40-70 (not overbought, has room to run)
- MACD has a bullish crossover or is bullish
- Volume is above average (volume ratio > 1.2)
- Trend structure: at least the 9 and 20 EMAs are bullishly aligned
- No bearish RSI divergence

Signal strength is a weighted combination of these factors. All of them present = high strength; minimum 4 of 6 for a valid signal.

**Position evaluation:**
- HOLD if: price still above VWAP, RSI not overbought (< 75), no override signals triggered, thesis from Claude still intact
- EXIT if: price broke below VWAP on volume, RSI > 80 (extremely overbought), ROC decelerating sharply, or Claude recommends exit
- SCALE_OUT if: price approaching profit target (within 2%), take partial profits

**Exit logic:**
- At profit target: limit order at target price, urgency 0.3 (patient)
- Stop loss hit: market order, urgency 1.0 (immediate)
- RSI extremely overbought + momentum fading: limit order slightly below current price, urgency 0.7
- End of day with no swing hold thesis: market order before 3:55 PM ET, urgency 0.8

**Parameters** (configurable via `set_parameters`):
- `min_volume_ratio`: 1.2
- `rsi_entry_max`: 70
- `rsi_overbought`: 80
- `min_signals_for_entry`: 4
- `partial_profit_pct`: 0.5 (take 50% at target)

### 3. Swing strategy (`strategies/swing_strategy.py`)

Implement `SwingStrategy(Strategy)` for medium-term (days to weeks) swing trades:

**Entry signals** — generate a buy signal when:
- Price is near a support level (close to lower Bollinger band or key EMA)
- RSI is between 30-50 (oversold or approaching, potential reversal)
- MACD histogram is decreasing in negativity (momentum shifting)
- The longer-term trend (50 and 200 EMA) is still bullish (buying the dip in an uptrend)
- Volume is not significantly elevated on the downside (not panic selling)

Signal strength weighted differently than momentum — more emphasis on trend structure and less on immediate momentum.

**Position evaluation:**
- HOLD if: trend structure (50/200 EMA) intact, stop not threatened, Claude thesis intact
- EXIT if: trend structure breaks (50 EMA crosses below 200 EMA), stop hit, Claude says thesis broken
- SCALE_IN if: price dips further toward stop but trend structure holds and Claude conviction remains high (average down)

**Exit logic:**
- At profit target: limit order, urgency 0.3
- Stop loss: market order, urgency 1.0
- Trend structure breakdown: market order, urgency 0.9
- Swing trades can hold overnight and through multiple sessions — no end-of-day forced exit

**Parameters:**
- `rsi_entry_min`: 30
- `rsi_entry_max`: 50
- `trend_ema_short`: 50
- `trend_ema_long`: 200
- `max_scale_in_count`: 2
- `scale_in_dip_pct`: 3.0 (only scale in if price dips 3% further)

### 4. Strategy registry

Create a simple registry that maps strategy names to instances:
- `get_strategy(name: str) -> Strategy`: Returns the strategy instance for "momentum" or "swing".
- Load active strategies from config (`strategy.active_strategies`).

## Tests to Write

Create `tests/test_strategies.py`:
- **Momentum entry signals:** Create indicator sets that satisfy all momentum conditions → verify signal generated. Remove one condition at a time → verify signal degrades or disappears.
- **Momentum exit:** Simulate price breaking below VWAP → verify exit suggestion with high urgency. Simulate approaching profit target → verify partial exit.
- **Swing entry signals:** Create indicator sets showing an oversold dip in an uptrend → verify signal. Create a dip with broken trend structure → verify NO signal.
- **Swing hold:** Verify position holds when trend structure intact. Verify exit when 50 EMA crosses below 200 EMA.
- **Swing scale-in:** Simulate price dipping 3% below entry with trend intact → verify scale-in suggestion.
- **Parameter changes:** Verify `set_parameters` modifies behavior (e.g., changing `rsi_entry_max` changes what generates signals).

## Done When
- All tests pass
- Both strategies produce sensible signals from realistic indicator data
- Entry and exit logic matches the spec's trading philosophy (momentum = fast in/out, swing = patient with trend)
