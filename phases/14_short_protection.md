# Phase 14: Short Position Risk Protection — Quantitative Exit Coverage + Restart Safety

Read the Phase 13 section of DRIFT_LOG.md and BUGS_2026-03-16.md (Bug 1, Bug 3, Bug 9) before starting. This phase assumes Phase 13 is complete.

Short positions currently have no quantitative exit protection from the fast loop. `_fast_step_quant_overrides` skips all short positions by design, meaning a short held against an adverse move (stock rallies strongly) has no automated stop until Claude's slow loop fires — up to 60 minutes away. On the bot's target universe of high-volatility equities, an adverse 8–15% move in that window is realistic. Additionally, the `_recently_closed` guard that prevents the Bug 1 re-adoption runaway resets on every restart, leaving a narrow but real vulnerability window. This phase closes both safety gaps.

## 1. Short Position Quant Override Exits

In `_fast_step_quant_overrides` (`core/orchestrator.py`), add a parallel processing block for short positions after the existing long-position block:

For each short position (`position.intention.direction == "short"`):

**ATR trailing stop** (symmetric to long):
- Track `_intraday_lows: dict[str, float]` on the orchestrator (analogous to `_intraday_highs` for longs), initialized in `__init__`
- Each fast cycle: update `_intraday_lows[symbol] = min(_intraday_lows.get(symbol, current_price), current_price)`
- Trail stop price = `_intraday_lows[symbol] + atr × config.risk.short_atr_stop_multiplier`
- If `current_price >= trail_stop`: place buy-to-cover market order, increment `_override_exit_count`

**VWAP crossover exit** (symmetric to long):
- If `vwap_position == "above"` AND `volume_ratio > config.risk.short_vwap_exit_volume_threshold` (default 1.3): place buy-to-cover market order
- Only fire if `config.risk.short_vwap_exit_enabled` is True (default True)

**Hard stop from intention**:
- If `position.intention.exit_targets.stop_loss > 0` and `current_price >= stop_loss`: place buy-to-cover market order immediately (no trail needed)

New config keys (add to `RiskConfig` and `config.json`):
- `short_atr_stop_multiplier: float = 2.0`
- `short_vwap_exit_enabled: bool = True`
- `short_vwap_exit_volume_threshold: float = 1.3`

All short exit orders placed through the same fill protection + order record path as long exits.

## 2. Short Position EOD Forced Close (Medium Loop)

In `_medium_evaluate_positions`, after the existing strategy `evaluate_position()` call, add a check for momentum shorts specifically:

- If `position.intention.direction == "short"` and `position.intention.strategy == "momentum"` and `is_last_five_minutes()`:
  - Place buy-to-cover market order if fill protection allows
  - Log INFO: "EOD forced close for momentum short position %s"

Swing shorts are excluded — they may be intended overnight. The `strategy == "momentum"` check is the gate.

## 3. Persist `_recently_closed` Through Restarts

`_recently_closed: dict[str, float]` currently stores `symbol → monotonic_timestamp`, which cannot survive a process restart. Monotonic timestamps have no meaning across processes.

Solution: persist close events to the portfolio state file.

In `PortfolioState` (`core/state_manager.py`):
- Add field: `recently_closed: dict[str, str] = field(default_factory=dict)` where value is UTC ISO timestamp string

In the orchestrator:
- On every `_journal_closed_trade` and ghost cleanup close event, write `portfolio.recently_closed[symbol] = datetime.now(timezone.utc).isoformat()` and save portfolio
- On `_startup` / `startup_reconciliation`, load `portfolio.recently_closed`, convert entries < 60 seconds old (by UTC wall time) to `_recently_closed[symbol] = time.monotonic()`, discard entries ≥ 60 seconds old
- The in-memory `_recently_closed` continues to use monotonic for all in-session checks (no change to existing guard logic)

This survives fast restarts within the 60-second window.

## 4. Verify Short Direction Inference in Startup Reconciliation

Audit `startup_reconciliation()` lines 253–350 (the adopted position block). The Bug 3 fix set `direction = "short"` if `broker_pos.side in ("short", "sell")`. Verify by reviewing Alpaca's `BrokerPosition.side` field values — confirm all short-position representations from the broker API are covered (e.g., `"short"`, `"sell"`, or any other value Alpaca returns for a short).

Add a log line for each adopted position showing the inferred direction so it's visible in startup logs.

## 5. Tests to Write

Create `tests/test_short_protection.py`:

- **ATR trailing stop fires**: `current_price >= intraday_low + atr × multiplier` → buy-to-cover placed
- **ATR trailing stop does not fire**: price below trail stop threshold → no order
- **VWAP crossover exit fires**: `vwap_position="above"`, `volume_ratio=1.5` → buy-to-cover placed
- **VWAP crossover disabled**: `short_vwap_exit_enabled=False` → no order despite VWAP/volume conditions
- **Hard stop from intention**: `current_price >= stop_loss` → immediate buy-to-cover
- **Intraday low tracking**: verify `_intraday_lows` updated each cycle to minimum seen
- **Short EOD exit fires**: `is_last_five_minutes()=True`, momentum short → buy-to-cover placed
- **Short EOD exit skips swing**: `strategy="swing"` short + EOD → no exit
- **`_recently_closed` persistence**: close event written to `portfolio.recently_closed` and saved
- **`_recently_closed` reload on startup**: entry < 60s old → loaded into `_recently_closed` in-memory dict
- **`_recently_closed` discard on reload**: entry ≥ 60s old → not loaded (guard does not fire)
- **Short direction adoption**: mock broker position with `side="short"` → adopted `Position` has `direction="short"`

## Done When

- All existing tests pass; all `test_short_protection.py` tests pass
- Short positions with ATR stops trigger buy-to-cover in fast loop integration test
- `portfolio.recently_closed` field present in loaded PortfolioState
- `_recently_closed` loaded correctly from state on orchestrator startup in test
- DRIFT_LOG.md has a Phase 14 entry covering all new fields and behaviors
