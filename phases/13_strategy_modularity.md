# Phase 13: Strategy Modularity — Adding a Strategy Costs 2 Files

Read the Phase 12 section of DRIFT_LOG.md before starting. This phase has no hard prerequisites beyond Phase 12 being complete.

Paper trading revealed that adding a new strategy required touching `orchestrator.py`, `config.py`, `opportunity_ranker.py`, and `risk_manager.py` in addition to the new strategy file — four non-strategy files for a single new strategy value. This phase enforces the modularity rule: "adding a new strategy should touch ≤ 2 modules."

## What Was Built

### 1. Strategy ABC Extensions (`strategies/base_strategy.py`)

Added three concrete trait properties and one abstract method to the `Strategy` ABC:

```python
@property
def is_intraday(self) -> bool:
    return True   # safe default; swing overrides to False

@property
def uses_market_orders(self) -> bool:
    return False  # safe default; momentum overrides to True

@property
def blocks_eod_entries(self) -> bool:
    return False  # safe default; momentum overrides to True

@abstractmethod
def apply_entry_gate(self, action: str, signals: dict) -> tuple[bool, str]:
    """Strategy-specific hard filter. Returns (True, "") to pass."""
```

`get_strategy(name, params)` is the registry extension point. To add a new strategy: add one entry to the `registry` dict in `base_strategy.py` and implement the `Strategy` ABC. Zero other files.

### 2. Concrete Implementations

**`MomentumStrategy`**: `is_intraday=True`, `uses_market_orders=True`, `blocks_eod_entries=True`. `apply_entry_gate` checks RVOL floor and VWAP direction (both configurable via `_params`). `_MOMENTUM_WRONG_VWAP` lookup table moved here from `opportunity_ranker.py`.

**`SwingStrategy`**: all three traits `False`. `apply_entry_gate` checks trend alignment (configurable). `_SWING_WRONG_TREND` lookup table moved here from `opportunity_ranker.py`.

### 3. Config — `strategy_params: dict[str, dict]`

`StrategyConfig` replaced `momentum_params: dict` and `swing_params: dict` with a single unified dict:

```python
strategy_params: dict[str, dict] = field(default_factory=dict)
# Maps strategy name → param overrides. Add one entry per new strategy.
```

`config.json` updated accordingly. Fixed latent bug: `active_strategies` was `["momentum, swing"]` (one comma-separated string) → corrected to `["momentum", "swing"]`.

### 4. Orchestrator (`core/orchestrator.py`)

- `_build_strategies()` now uses `get_strategy()` registry — no hardcoded imports
- `self._strategy_lookup: dict[str, Strategy]` built from `_build_strategies()`
- PDT gate uses `strategy_obj.is_intraday`
- Market order decision uses `strategy_obj.uses_market_orders`
- `validate_entry` call passes `strategy_obj.blocks_eod_entries`
- Ranker receives `strategy_lookup=self._strategy_lookup`

### 5. Ranker (`intelligence/opportunity_ranker.py`)

Removed if/elif strategy name branches. Replaced with delegate call to `strategy_obj.apply_entry_gate(action, signals)`. Lazy fallback: when `strategy_lookup=None`, constructs a default object via `get_strategy()` for test compatibility.

### 6. Risk Manager (`execution/risk_manager.py`)

`validate_entry` and `_check_market_hours` signatures: `strategy: str` → `blocks_eod_entries: bool`. EOD block: `if strategy == "momentum"` → `if blocks_eod_entries`. Avoids importing `Strategy` into the execution layer.

## Tests

`tests/test_strategy_traits.py` — 28 tests covering trait properties, `apply_entry_gate` for both strategies, registry dispatch, and `strategy_params` override forwarding.

## Done

- 692 tests passing
- Adding a "scalp" strategy requires only `strategies/scalp_strategy.py` + `config.json` entries
- DRIFT_LOG.md Phase 17 section documents all interface changes
