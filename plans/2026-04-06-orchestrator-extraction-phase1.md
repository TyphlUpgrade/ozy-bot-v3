# Orchestrator Extraction Phase 1 — TriggerEngine, MarketContextBuilder, FillHandler

**Date:** 2026-04-06  
**Motivated by:** Orchestrator god object (5393 lines, 47 methods) blocking parallel agent development. These three extractions have zero shared mutable state writes and are the safest starting point.

---

## Goal

Extract ~914 lines from `orchestrator.py` into three independently testable modules. Prove the extraction pattern (class wrapping orchestrator methods, dependency injection, thin delegation from orchestrator). Orchestrator drops to ~4480 lines.

This is prep work for the OmX/clawhip/OmO multi-agent development workflow: each extracted module becomes a parallel work zone that a separate coding agent can modify without merge conflicts in the orchestrator.

---

## Extraction Pattern (applies to all three)

Each extraction follows the same structure:

1. **New class** in `core/` with `__init__` accepting only the dependencies it needs (config, state_manager, broker, etc.) — never the orchestrator itself.
2. **Methods move verbatim.** No refactoring, no cleanup, no interface changes. The goal is mechanical extraction, not improvement. Preserving exact behavior makes review trivial and rollback safe.
3. **Orchestrator keeps a thin delegation method** with the same name, same signature, that calls `self._<module>.<method>()`. This preserves all call sites in the loop bodies unchanged.
4. **Instance variables** that the extracted method reads but doesn't write are passed as constructor args or method params. Variables it writes are returned and the orchestrator delegation wrapper applies them.
5. **Tests** for the extracted module import from the new location. Existing orchestrator integration tests continue to work via delegation.

---

## Extraction 1: TriggerEngine → `core/trigger_engine.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:216-279` | 64 | `SlowLoopTriggerState` dataclass |
| `orchestrator.py:3353-3760` | ~408 | `_check_triggers(self, now)` |
| `orchestrator.py:3761-3810` | ~50 | `_check_regime_conditions(self)` |
| `orchestrator.py:3811-3820` | ~10 | `_update_trigger_prices(self)` |
| **Total** | **~532** | |

### Instance variable dependencies

**Reads (pass as constructor or method args):**
- `self._config` — scheduler thresholds, macro move config
- `self._trigger_state` — the `SlowLoopTriggerState` instance (owned by TriggerEngine after extraction)
- `self._all_indicators` — merged TA dict for price/RSI lookups
- `self._market_context_indicators` — SPY RVOL for dead zone bypass check
- `self._override_exit_count` — override exit counter for trigger comparison
- `self._last_regime_assessment` — regime conditions for `_check_regime_conditions`
- `self._last_medium_loop_completed_utc` — medium loop staleness gate
- `self._watchlist_build_in_flight` — prevents triggering during builds

**Writes:** None. Returns `list[str]` of trigger names. `_update_trigger_prices` mutates `_trigger_state.last_prices` which TriggerEngine owns. `_check_regime_conditions` returns `bool`.

### New class shape

```python
class TriggerEngine:
    def __init__(self, config: Config):
        self.state = SlowLoopTriggerState()  # owns the trigger state

    def check_triggers(
        self,
        all_indicators: dict,
        market_context_indicators: dict,
        override_exit_count: int,
        last_regime_assessment: dict | None,
        last_medium_loop_completed_utc: datetime | None,
        watchlist_build_in_flight: bool,
        now: datetime | None = None,
    ) -> list[str]: ...

    def check_regime_conditions(
        self,
        all_indicators: dict,
        last_regime_assessment: dict | None,
    ) -> bool: ...

    def update_trigger_prices(self, all_indicators: dict) -> None: ...
```

### Orchestrator delegation

```python
# In __init__:
self._trigger_engine = TriggerEngine(self._config)
self._trigger_state = self._trigger_engine.state  # alias for backward compat

# Delegation:
async def _check_triggers(self, now=None):
    return self._trigger_engine.check_triggers(
        self._all_indicators, self._market_context_indicators,
        self._override_exit_count, self._last_regime_assessment,
        self._last_medium_loop_completed_utc, self._watchlist_build_in_flight,
        now,
    )
```

---

## Extraction 2: MarketContextBuilder → `core/market_context.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:3821-3974` | ~154 | `_build_market_context(self, acct, pdt_remaining)` |
| **Total** | **~154** | |

### Instance variable dependencies

**Reads (pass as method args):**
- `self._config` — Claude config (news limits, tier counts), risk config, timezone
- `self._market_context_indicators` — SPY/QQQ/sector TA for trend classification
- `self._latest_indicators` — watchlist symbol indicators for `ta_readiness`
- `self._daily_indicators` — daily-bar signals for swing context
- `self._recommendation_outcomes` — passed through to Claude context
- `self._filter_suppressed` — passed through to Claude context
- `self._filter_adjustments` — passed through to Claude context
- `self._last_regime_assessment` — current regime for context
- `self._last_sector_regimes` — sector regimes for context
- `self._claude` — calls `self._claude.fetch_news()` for watchlist news
- `self._state_manager` — loads watchlist state
- `self._data_adapter` — not used directly (news goes through claude engine)

**Writes:** None. Returns `dict` (the market_data context).

### New class shape

```python
class MarketContextBuilder:
    def __init__(self, config: Config):
        self._config = config

    async def build(
        self,
        acct,
        pdt_remaining: int,
        market_context_indicators: dict,
        latest_indicators: dict,
        daily_indicators: dict,
        recommendation_outcomes: dict,
        filter_suppressed: dict,
        filter_adjustments: dict | None,
        last_regime_assessment: dict | None,
        last_sector_regimes: dict | None,
        state_manager: StateManager,
        claude_engine: ClaudeReasoningEngine,
    ) -> dict: ...
```

### Note on parameter count

The parameter list is long because `_build_market_context` assembles data from many sources — that's its job. The alternative (passing the orchestrator) would defeat the purpose of extraction. The parameter list is the honest declaration of what this method actually touches.

If the parameter list proves unwieldy during implementation, a `MarketContextInputs` dataclass can bundle them — but do that as a follow-up, not during extraction. Mechanical extraction first.

---

## Extraction 3: FillHandler → `core/fill_handler.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:1416-1441` | ~26 | `_dispatch_confirmed_fill(self, change)` |
| `orchestrator.py:1442-1537` | ~96 | `_register_opening_fill(self, change)` |
| `orchestrator.py:1538-1658` | ~121 | `_journal_closed_trade(self, change, exit_reason_hint)` |
| **Total** | **~243** | |

### Instance variable dependencies

**Reads (pass as constructor or method args):**
- `self._config` — prompt version, model name for journal entries
- `self._state_manager` — load/save portfolio
- `self._trade_journal` — append journal records
- `self._pending_intentions` — pop intention on opening fill
- `self._pending_exit_hints` — pop hint on closing fill
- `self._entry_contexts` — pop/read signal context
- `self._latest_indicators` — signals at exit time
- `self._trigger_state` — clear profit/near-target/near-stop tracking on close

**Writes (returned to orchestrator or passed mutably):**
- `self._entry_contexts[symbol]` — set on opening fill (write)
- `self._recommendation_outcomes[symbol]["stage"]` — set to "filled" on opening fill (write)
- `self._position_entry_times[symbol]` — set on opening fill (write)
- `self._recently_closed[symbol]` — set on closing fill (write)
- `self._cycle_consumed_symbols` — add on closing fill (write)
- `self._override_closed` — pop on closing fill (write)
- `self._trigger_state.last_profit_trigger_gain` — pop on close (write, via trigger_state ref)
- `self._trigger_state.last_near_target_time` — pop on close (write, via trigger_state ref)
- `self._trigger_state.last_near_stop_time` — pop on close (write, via trigger_state ref)

### Write strategy

The fill handler has more write coupling than TriggerEngine or MarketContextBuilder, but all writes are simple dict mutations (set/pop/add). Two approaches:

**Option A (preferred): Pass mutable containers by reference.**
The orchestrator passes `self._entry_contexts`, `self._recently_closed`, etc. to the FillHandler constructor. Python dicts are mutable references — the FillHandler mutates them in place, and the orchestrator sees the changes. No return-value ceremony needed. This is safe because the fast loop is single-threaded (asyncio).

**Option B: Return a mutation descriptor.**
FillHandler returns a dataclass describing what changed; orchestrator applies it. Cleaner contract but more ceremony for zero practical benefit in a single-threaded event loop.

### New class shape (Option A)

```python
class FillHandler:
    def __init__(
        self,
        config: Config,
        state_manager: StateManager,
        trade_journal: TradeJournal,
        trigger_state: SlowLoopTriggerState,
        # Mutable shared dicts (references, not copies)
        entry_contexts: dict,
        pending_intentions: dict,
        pending_exit_hints: dict,
        recommendation_outcomes: dict,
        position_entry_times: dict,
        recently_closed: dict,
        cycle_consumed_symbols: set,
        override_closed: dict,
    ):
        ...

    async def dispatch_confirmed_fill(self, change) -> None: ...
    async def register_opening_fill(self, change) -> None: ...
    async def journal_closed_trade(self, change, exit_reason_hint=None) -> None: ...
```

### Orchestrator delegation

```python
# In _startup:
self._fill_handler = FillHandler(
    config=self._config,
    state_manager=self._state_manager,
    trade_journal=self._trade_journal,
    trigger_state=self._trigger_state,
    entry_contexts=self._entry_contexts,
    pending_intentions=self._pending_intentions,
    pending_exit_hints=self._pending_exit_hints,
    recommendation_outcomes=self._recommendation_outcomes,
    position_entry_times=self._position_entry_times,
    recently_closed=self._recently_closed,
    cycle_consumed_symbols=self._cycle_consumed_symbols,
    override_closed=self._override_closed,
)

async def _dispatch_confirmed_fill(self, change):
    await self._fill_handler.dispatch_confirmed_fill(change)
```

---

## Execution order

1. **TriggerEngine first.** Zero writes, cleanest extraction, proves the pattern.
2. **MarketContextBuilder second.** Zero writes, but longer parameter list — validates the "pass everything explicitly" approach.
3. **FillHandler third.** Mutable reference pattern — validates that approach before Phase 2 uses it for WatchlistLifecycle and Reconciliation.

Each extraction is one commit. Run full test suite after each. If any extraction breaks tests, fix before proceeding to the next.

---

## Files touched

| File | Change |
|---|---|
| `ozymandias/core/trigger_engine.py` | New — SlowLoopTriggerState + TriggerEngine class |
| `ozymandias/core/market_context.py` | New — MarketContextBuilder class |
| `ozymandias/core/fill_handler.py` | New — FillHandler class |
| `ozymandias/core/orchestrator.py` | Remove moved methods, add delegation wrappers, instantiate new modules |
| `ozymandias/tests/test_trigger_engine.py` | New — unit tests for TriggerEngine |
| `ozymandias/tests/test_market_context.py` | New — unit tests for MarketContextBuilder |
| `ozymandias/tests/test_fill_handler.py` | New — unit tests for FillHandler |

---

## What this does not change

- No behavioral changes. Every method behaves identically after extraction.
- No new features. No refactoring. No cleanup.
- Loop bodies (`_fast_loop_cycle`, `_medium_loop_cycle`, `_slow_loop_cycle`) stay in orchestrator.
- `_medium_try_entry` and `_run_claude_cycle` stay in orchestrator (too coupled — see NOTES.md).
- No config changes. No prompt changes.

---

## Success criteria

- `orchestrator.py` drops from ~5393 to ~4480 lines
- All existing tests pass without modification (delegation preserves call sites)
- Each new module has its own test file with direct unit tests (no orchestrator needed)
- Three new parallel work zones unlocked for independent agent development
