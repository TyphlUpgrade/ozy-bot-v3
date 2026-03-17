# Phase 11: Execution Fidelity — Stale Price Fix + TA Size Modulation

Read the `Post-MVP Architecture` section of CLAUDE.md and the relevant Phase 09 section of DRIFT_LOG.md (`_medium_try_entry` and `_run_claude_cycle`) before starting.

The first paper trading session (2026-03-16) exposed a structural execution flaw: `_medium_try_entry` uses `top.suggested_entry` — Claude's suggested price from up to 60 minutes ago — as the limit order price. On high-volatility equities, this produces silent non-fills (price moved away) or stale-level fills (price drifted into the limit for the wrong reasons). This phase makes the execution layer price-precise and TA-quality-aware without changing any strategic or signal-detection architecture.

## 1. Use Current Market Price for Entry Limit Orders

In `_medium_try_entry` (`core/orchestrator.py`):

- Fetch `current_price = self._latest_indicators.get(symbol, {}).get("price")`
- If `current_price` is not None: use it as `entry_price` for the limit order
- If `current_price` is None: fall back to `top.suggested_entry` (log a WARNING that current price is unavailable)
- `top.suggested_entry` is retained as a reference level for the staleness check below — do not discard it
- Update `_pending_intentions[symbol]` and the `OrderRecord` to reflect the actual price used, not Claude's suggestion

## 2. Entry Price Staleness / Drift Check

After resolving `entry_price` from current data, check whether the current price has drifted too far from Claude's original target. Two failure modes in opposite directions:

**Chase check** (price ran past Claude's entry): Skip entry if current price has moved unfavorably beyond Claude's entry by more than `max_entry_drift_pct`. For a buy, this means the stock is already X% above where Claude wanted to enter; the momentum has been captured without us.

**Adverse drift check** (price broke through Claude's level): Skip entry if current price has moved adversely from Claude's entry by more than `max_adverse_drift_pct`. For a buy, this means the stock is X% below Claude's entry — the level Claude intended to buy has been violated, likely invalidating the thesis.

For short entries, direction of both checks is inverted.

New config keys (add to `RankerConfig` in `config.py` and `ranker` section of `config.json`):
- `max_entry_drift_pct: float = 0.015` — skip buy if current price > suggested_entry × 1.015
- `max_adverse_drift_pct: float = 0.020` — skip buy if current price < suggested_entry × 0.980

Log INFO (not WARNING) when drift check skips an entry — this is expected, normal behavior.

## 3. Minimum Technical Score Hard Filter

In `apply_hard_filters` (`intelligence/opportunity_ranker.py`), add a coarse TA quality floor check using the `composite_technical_score` already computed by `generate_signal_summary`. This is not the blind 6-condition gate — it's a summary-level sanity check that catches degenerate cases.

New config key (add to `RankerConfig` and `config.json`):
- `min_technical_score: float = 0.30`

In `apply_hard_filters`, after the existing min_conviction check:
- Retrieve `composite_technical_score` from `technical_signals.get(symbol, {}).get("composite_technical_score", 0.0)` when `technical_signals` is provided
- If score < `min_technical_score`: reject with reason "composite_technical_score {score:.2f} below floor {min}"
- If `technical_signals` is None (not provided): skip this check (backward compatible)

## 4. TA Signal Strength as Position Size Modifier

After ATR-based position sizing in `_medium_try_entry`, apply a modulation factor based on the symbol's current `composite_technical_score`:

```python
tech_score = self._latest_indicators.get(symbol, {}).get("composite_technical_score", 0.5)
size_factor = config.ranker.ta_size_factor_min + (1.0 - config.ranker.ta_size_factor_min) * tech_score
quantity = max(1, int(quantity * size_factor))
```

New config key (add to `RankerConfig` and `config.json`):
- `ta_size_factor_min: float = 0.60` — at composite_technical_score=0, enter at 60% of risk-sized quantity; at score=1.0, enter at 100%

Log the size modulation at DEBUG: "TA size factor {size_factor:.2f} (tech_score={tech_score:.2f}), qty {original} → {final}"

## 5. Tests to Write

Create `tests/test_execution_fidelity.py`:

- **Current price substitution**: when `_latest_indicators` has a price, the order uses it as limit price, not `top.suggested_entry`
- **Current price fallback**: when indicators lack a price entry, the order falls back to `top.suggested_entry` and logs a WARNING
- **Chase check blocked (buy)**: current_price > suggested_entry × (1 + max_entry_drift_pct) → entry returns False
- **Adverse drift blocked (buy)**: current_price < suggested_entry × (1 - max_adverse_drift_pct) → entry returns False
- **Chase check blocked (short)**: direction inverted — current_price < suggested_entry × (1 - max_entry_drift_pct) → entry returns False
- **Within tolerance (buy)**: drift within bounds → entry proceeds
- **Min technical score filter**: score=0.25 < floor=0.30 → `apply_hard_filters` rejects
- **Min technical score passes**: score=0.35 ≥ floor=0.30 → filter does not reject on this criterion
- **Min technical score skipped when no signals**: `technical_signals=None` → no rejection
- **Size modifier at min**: tech_score=0.0, ta_size_factor_min=0.60 → quantity = 60% of base (rounded)
- **Size modifier at max**: tech_score=1.0 → quantity = 100% of base
- **Size modifier midpoint**: tech_score=0.5 → quantity = 80% of base (with factor_min=0.60)
- **Size modifier floors at 1**: result never below 1 share

## Done When

- All existing tests pass; all new `test_execution_fidelity.py` tests pass
- In a manual test run with mocked indicators: entry limit prices match the current indicator price, not Claude's suggested_entry
- Drift check blocks an entry when current price is 2.5% above suggested_entry for a buy
- Size modifier visibly reduces quantity for low-tech-score setups in log output
- DRIFT_LOG.md has a Phase 11 entry covering each deviation from prior behavior
