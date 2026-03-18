# Phase 16: Pattern Signal Layer + Short Position Protection

Read the Phase 14 section of DRIFT_LOG.md and BUGS_2026-03-16.md (Bug 1, Bug 3, Bug 9) before
starting. This phase assumes Phase 14 is complete.

**Implementation order note:** Phase 16 must be implemented before Phase 15. Phase 15's
`ta_readiness` dict is a pass-through of `indicators[symbol]["signals"]` — all new signals added
here automatically appear in Claude's context when Phase 15 is implemented, with no additional
work required in that phase.

This phase has two independent concerns:

1. **Short position safety** — the fast loop has no quant exit coverage for short positions, and
   `_recently_closed` resets on every restart.
2. **TA pattern signal layer** — all current signals are instantaneous snapshots. The bot cannot
   distinguish RSI 73 climbing from RSI 73 falling, MACD histogram building from topping, or a
   Bollinger squeeze from a normal low-vol tape. These are pattern gaps that cause rejected entries
   (INTC at RSI 73 with rising RSI) and mis-scored opportunities.

---

## 1. Short Position Quant Override Exits

In `_fast_step_quant_overrides`, add a processing block for short positions after the existing
long-position block. For each short position:

**ATR trailing stop** (symmetric to long): track `_intraday_lows` on the orchestrator (analogous
to `_intraday_highs` for longs). Each fast cycle update to the minimum price seen. Trail stop =
intraday low + ATR × `config.risk.short_atr_stop_multiplier`. If current price breaches the
trail stop, place a buy-to-cover market order.

**VWAP crossover exit** (symmetric to long): if `vwap_position == "above"` and
`volume_ratio > config.risk.short_vwap_exit_volume_threshold`, place a buy-to-cover market order.
Gated by `config.risk.short_vwap_exit_enabled`.

**Hard stop from intention**: if `exit_targets.stop_loss > 0` and current price ≥ stop loss,
place buy-to-cover immediately.

New config keys in `RiskConfig` and `config.json`:
- `short_atr_stop_multiplier: float = 2.0`
- `short_vwap_exit_enabled: bool = True`
- `short_vwap_exit_volume_threshold: float = 1.3`

All short exit orders go through the same fill protection and order record path as long exits.

---

## 2. Short Position EOD Forced Close (Medium Loop)

In `_medium_evaluate_positions`, after the existing strategy `evaluate_position()` call: if the
position is a momentum short and it is the last five minutes of session, place a buy-to-cover
market order. Swing shorts are excluded — they may be intended overnight.

---

## 3. Persist `_recently_closed` Through Restarts

`_recently_closed: dict[str, float]` currently uses monotonic timestamps that do not survive
restarts. Add `recently_closed: dict[str, str]` to `PortfolioState` (values are UTC ISO strings).
On every close event, write the symbol and timestamp to `portfolio.recently_closed` and save.
On startup reconciliation, reload entries younger than 60 seconds into the in-memory
`_recently_closed` guard; discard older entries. The in-memory dict continues to use monotonic
time for all in-session checks.

---

## 4. Verify Short Direction Inference in Startup Reconciliation

Audit the adopted-position block in `startup_reconciliation`. Confirm all Alpaca short-position
`side` field values are covered by the Bug 3 direction inference fix. Add a log line per adopted
position showing the inferred direction.

---

## 5. TA Pattern Signal Layer

All signals below are added to `generate_signal_summary()` in `technical_analysis.py` and
included in the returned `signals` dict. They flow automatically into `_latest_indicators`,
strategy `apply_entry_gate()` calls, and (after Phase 15) Claude's `ta_readiness` context.

**Extension point:** to add a new pattern signal, add its computation to `generate_signal_summary`
and add it to the `signals` dict. No other file needs to change unless the signal also requires a
composite score adjustment or a strategy gate update.

### 5a. `roc_negative_deceleration` (bool)

`roc_deceleration` only fires when ROC is positive, leaving short scoring overstated when bearish
momentum fades. `roc_negative_deceleration` is the symmetric counterpart: fires when ROC is
negative on both the current and previous bar and the magnitude is shrinking (the downmove is
losing steam).

In `compute_composite_score`, replace the single `roc_decel` lookup with a direction-resolved
one: shorts use `roc_negative_deceleration` for the deceleration penalty; longs use
`roc_deceleration` as before. A short with decelerating bearish momentum scores 0.5 instead of
0.8 on the ROC component.

### 5b. `rsi_slope_5` (float)

**Gap:** RSI is a snapshot. RSI 73 rising from 55 over five bars = momentum acceleration.
RSI 73 falling from 85 = exhaustion. Currently indistinguishable — this is the direct cause of
the INTC rejected-entry.

`rsi_slope_5` is the change in RSI over the last five bars (current RSI minus RSI five bars ago).
Positive = rising, negative = falling. Defaults to 0.0 when fewer than six RSI values are
available.

**Composite score:** Add a small direct bonus (modelled after the existing `rsi_divergence`
adjustment, not a weighted component) when RSI is in the extended zone (between `rsi_entry_max`
and `rsi_max_absolute`) and slope is strongly positive for longs, or strongly negative for
shorts. This partially counteracts the score penalty that would otherwise be applied to RSI in
that zone.

### 5c. `macd_histogram_expanding` (bool)

**Gap:** `macd_signal` captures the cross event but not trajectory. A histogram building from
0.1 to 1.2 over four bars and one contracting from 2.0 to 0.7 both read `"bullish"`.

`macd_histogram_expanding` is true when the histogram's absolute value grew bar-over-bar AND the
sign is unchanged (same-direction movement, not a zero-crossing).

**Composite score:** Small directional bonus when MACD is bullish and histogram is expanding
(momentum building); small penalty when MACD is bullish but histogram is contracting (momentum
topping). Symmetric for shorts.

### 5d. `bb_squeeze` (bool)

**Gap:** Bollinger Band width (compression) signals coiling before a breakout. We compute the
full band DataFrame but only use position. Band width — how compressed the bands are relative to
recent history — is categorically different from all other signals: it detects *potential energy*
before the move, not confirmation of a move underway.

`bb_squeeze` is true when the current band width (as a percentage of the middle band) is at or
near its 20-bar minimum, indicating price is coiling. Use a small tolerance (approximately 5%
above the rolling minimum) to avoid flickering.

**Composite score:** Not affected. `bb_squeeze` is context for a pending move, not a directional
quality signal. Strategies and Claude's `entry_conditions` are the correct consumers. The
`evaluate_entry_conditions` extension point in the ranker already supports adding a
`require_bb_squeeze` condition key if Claude wants to gate entries on it.

### 5e. `volume_trend_bars` (int, 0–5)

**Gap:** `volume_ratio` is a snapshot. It shows whether volume is elevated now but not whether
it is building. Consecutive bars of increasing volume is accumulation; consecutive bars of
shrinking volume on a rising price is distribution.

`volume_trend_bars` counts the number of consecutive recent bars in which volume exceeded the
prior bar, stopping at the first bar where volume did not increase. A value of 3+ indicates a
developing accumulation pattern.

**Composite score:** Not affected. The existing `volume_ratio` weight captures volume magnitude.
`volume_trend_bars` is contextual pattern information suited for Claude's `ta_readiness` context
and strategy gates rather than the mechanical score.

---

## 6. Slope-Aware RSI Momentum Gate

**The INTC failure:** RSI 73 with a rising slope was blocked by `rsi_entry_max: 65`. The ceiling
was designed to avoid buying exhausted tops, but cannot distinguish acceleration from exhaustion.

In `MomentumStrategy._evaluate_entry_conditions`, replace the static `rsi_in_range` condition
with a slope-aware one. The new logic has three zones:

- **Normal zone** (RSI between `rsi_entry_min` and `rsi_entry_max`, i.e. 45–65): passes
  unconditionally, unchanged from current behaviour.
- **Extended zone** (RSI between `rsi_entry_max` and `rsi_max_absolute`, i.e. 65–78): passes
  only when `rsi_slope_5` meets or exceeds `rsi_slope_threshold` — RSI must be actively climbing
  to enter here. A flat or falling RSI in the extended zone is rejected.
- **Hard ceiling** (RSI above `rsi_max_absolute`, i.e. > 78): always blocked regardless of
  slope. Genuinely overextended.

New config keys in `strategy_params.momentum` (`config.json`) and `_DEFAULT_PARAMS` in
`momentum_strategy.py`:
- `rsi_max_absolute: 78` — hard RSI ceiling; slope cannot override above this level
- `rsi_slope_threshold: 2.0` — minimum `rsi_slope_5` required for entries in the extended zone

---

## 7. ATR-Based Position Sizing Cap

Claude specifies `position_size_pct` per trade but has no visibility into realised intraday
volatility. On a high-ATR day a 10% position can incur 3%+ portfolio risk from a single stop-out.

In `_medium_try_entry`, after the drift check and before `place_order`: if
`atr_position_size_cap_enabled` is true, compute the maximum position size implied by
`max_risk_per_trade_pct` and the symbol's ATR as a percentage of price. If Claude's requested
size exceeds this cap, reduce it and log at INFO with the original and capped sizes.

The cap is direction-agnostic; ATR measures two-way risk symmetrically.

New config keys in `RiskConfig` and `config.json`:
- `atr_position_size_cap_enabled: bool = True`
- `max_risk_per_trade_pct: float = 0.02`

At defaults, a stock with 2% ATR hits the cap at 100% position size — normal 5–10% positions
are unaffected unless ATR is extreme.

---

## 8. Tests to Write

### `tests/test_short_protection.py`

- ATR trailing stop fires when price breaches intraday low + ATR × multiplier
- ATR trailing stop does not fire when price is below threshold
- VWAP crossover exit fires: `vwap_position="above"`, volume above threshold → buy-to-cover
- VWAP crossover disabled via config → no order despite conditions being met
- Hard stop from intention fires when price reaches stop loss level
- `_intraday_lows` updated each cycle to the running minimum
- Momentum short EOD exit fires in last five minutes
- Swing short is not closed by EOD logic
- `_recently_closed` written to `portfolio.recently_closed` on close event and saved
- Entry < 60 seconds old reloaded into in-memory `_recently_closed` on startup
- Entry ≥ 60 seconds old discarded on reload (guard does not fire)
- ATR cap reduces position size on high-volatility symbol and logs at INFO
- ATR cap does not fire on normal-volatility symbol
- ATR cap disabled via `atr_position_size_cap_enabled=False`

### `tests/test_ta_pattern_signals.py`

`roc_negative_deceleration`:
- Fires when two consecutive negative ROC bars with shrinking magnitude
- Does not fire when negative ROC is deepening (magnitude growing)
- Does not fire when ROC is positive
- Short composite score uses `roc_negative_deceleration`: decelerating bearish ROC scores 0.5
  not 0.8 on the ROC component

`rsi_slope_5`:
- Rising RSI over six bars produces a positive slope value
- Falling RSI over six bars produces a negative slope value
- Fewer than six RSI values available → slope = 0.0
- Composite score: RSI in extended zone with positive slope scores higher than same RSI with
  zero slope

`macd_histogram_expanding`:
- Expanding bullish histogram → True
- Contracting bullish histogram → False
- Expanding bearish histogram → True (bearish side can expand too)
- Zero-crossing (sign change between bars) → False
- Composite score: bullish MACD + expanding histogram scores higher than bullish MACD alone

`bb_squeeze`:
- Current width at rolling 20-bar minimum → True
- Current width within tolerance of minimum → True
- Current width above tolerance → False
- Fewer than 20 bars available → False
- `compute_composite_score` is not affected by `bb_squeeze` — score identical True vs False

`volume_trend_bars`:
- Three consecutive volume increases → value of 3
- Two consecutive increases then a drop → value of 2
- No consecutive increases → value of 0
- `compute_composite_score` is not affected by `volume_trend_bars` — score identical

### `tests/test_momentum_strategy.py` additions

- RSI in extended zone + slope ≥ threshold → `rsi_in_range = True`
- RSI in extended zone + slope below threshold → `rsi_in_range = False`
- RSI in extended zone + falling slope → `rsi_in_range = False`
- RSI above hard ceiling + any slope → `rsi_in_range = False`
- RSI in normal range → `rsi_in_range = True` (unchanged behaviour)
- RSI at min boundary → `rsi_in_range = True`
- RSI below min boundary → `rsi_in_range = False`

---

## 9. Done When

- All existing tests pass; all new tests in `test_short_protection.py` and
  `test_ta_pattern_signals.py` pass; all updated `test_momentum_strategy.py` tests pass
- `generate_signal_summary` output includes all five new signals
- `compute_composite_score` applies `roc_negative_deceleration` for shorts, `rsi_slope_5` bonus
  in the extended zone, and `macd_histogram_expanding` modifier; `bb_squeeze` and
  `volume_trend_bars` do not affect the composite score (verified by unit tests)
- Momentum gate: RSI in extended zone with qualifying slope passes `rsi_in_range`; RSI above
  hard ceiling is always blocked; both thresholds are configurable via `config.json`
- Short positions have ATR trailing stop, VWAP crossover exit, and hard stop coverage in the
  fast loop
- `portfolio.recently_closed` field present and persisted; reload on startup confirmed by test
- ATR position size cap applied in `_medium_try_entry`; disabled cleanly via config flag
- DRIFT_LOG.md has a Phase 16 entry covering all five new signals, the RSI gate change, all
  new config keys, and the short protection additions
