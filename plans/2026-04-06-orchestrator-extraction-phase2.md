# Orchestrator Extraction Phase 2 — WatchlistLifecycle, PositionManager, QuantOverrides, PositionSync, Reconciliation

**Date:** 2026-04-06  
**Depends on:** Phase 1 (2026-04-06-orchestrator-extraction-phase1.md) — proves the extraction pattern  
**Motivated by:** Continue reducing orchestrator to ~3030 lines, unlock five more parallel work zones

---

## Goal

Extract ~1446 lines from `orchestrator.py` into five modules. These have slightly more coupling than Phase 1 (two modules touch `_filter_suppressed` and `_recommendation_outcomes`) but all coupling is resolvable by passing mutable references — the same pattern validated by FillHandler in Phase 1.

After Phase 2, the orchestrator retains only: loop bodies, `_medium_try_entry` (529L), `_run_claude_cycle` (512L), startup/shutdown lifecycle, and thin delegation wrappers. That's the irreducible coordinator core.

---

## Extraction pattern

Same as Phase 1: verbatim method move, dependency injection, thin delegation wrapper, no behavioral changes. See Phase 1 plan for the full pattern description.

---

## Extraction 1: QuantOverrides → `core/quant_overrides.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:1659-1712` | ~54 | `_place_override_exit(self, position, exit_hint)` |
| `orchestrator.py:1713-1829` | ~117 | `_fast_step_quant_overrides(self)` |
| **Total** | **~171** | |

### Dependencies

**Constructor args (immutable refs):**
- `config` — scheduler.min_hold_before_override_min
- `broker` — place_order()
- `state_manager` — load_portfolio()
- `fill_protection` — can_place_order(), record_order()
- `risk_manager` — check_hard_stop(), evaluate_overrides()
- `strategies` — list of Strategy instances for per-strategy override config

**Mutable shared refs (same pattern as FillHandler):**
- `latest_indicators: dict` — read current price/signals per symbol
- `pending_exit_hints: dict` — write exit reason tag for journal
- `position_entry_times: dict` — read entry timestamp for min-hold check
- `intraday_highs: dict` — read/write session high tracking (longs)
- `intraday_lows: dict` — read/write session low tracking (shorts)

**Writes via side effect:**
- `self._override_exit_count += 1` — counter read by TriggerEngine. Pass as mutable container (single-element list or expose as property) or return increment flag.
- `self._override_closed[symbol]` — write override cooldown timestamp

### New class shape

```python
class QuantOverrides:
    def __init__(
        self,
        config: Config,
        broker: BrokerInterface,
        state_manager: StateManager,
        fill_protection: FillProtectionManager,
        risk_manager: RiskManager,
        strategies: list[Strategy],
        # Mutable shared state
        latest_indicators: dict,
        pending_exit_hints: dict,
        position_entry_times: dict,
        intraday_highs: dict,
        intraday_lows: dict,
        override_closed: dict,
    ): ...

    async def step(self) -> int:
        """Run quant override checks. Returns number of override exits placed."""
        ...

    async def place_override_exit(self, position, exit_hint: str) -> bool:
        """Place override exit. Returns True if order was placed."""
        ...
```

The orchestrator wrapper adds the return value to `self._override_exit_count`.

---

## Extraction 2: PositionSync → `core/position_sync.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:1873-2076` | ~204 | `_fast_step_position_sync(self)` |
| **Total** | **~204** | |

### Dependencies

**Constructor args:**
- `config` — risk params, scheduler params
- `broker` — get_positions()
- `state_manager` — load/save portfolio, load orders
- `fill_protection` — can_place_order(), record_order()
- `risk_manager` — validate_entry() (for adopted positions)

**Mutable shared refs:**
- `recently_closed: dict` — read to avoid re-adopting just-closed positions
- `pending_intentions: dict` — read to match broker positions with entry intent
- `entry_contexts: dict` — read/restore on restart
- `position_entry_times: dict` — write on adoption
- `override_closed: dict` — read for cooldown check

**Side effects:**
- Calls `self._mark_broker_failure()` / `self._mark_broker_available()` — degradation state. Pass the `DegradationState` instance or expose two callbacks.

### New class shape

```python
class PositionSync:
    def __init__(
        self,
        config: Config,
        broker: BrokerInterface,
        state_manager: StateManager,
        fill_protection: FillProtectionManager,
        risk_manager: RiskManager,
        degradation: DegradationState,
        # Mutable shared state
        recently_closed: dict,
        pending_intentions: dict,
        entry_contexts: dict,
        position_entry_times: dict,
        override_closed: dict,
    ): ...

    async def step(self) -> None: ...
```

---

## Extraction 3: PositionManager → `core/position_manager.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:3103-3352` | ~250 | `_medium_evaluate_positions(self, portfolio, bars, indicators, acct, orders)` |
| `orchestrator.py:5042-5393` | ~352 | `_apply_position_reviews(self, reviews, portfolio)` |
| **Total** | **~602** | |

*Note: line count is higher than the NOTES.md estimate (~325) because `_apply_position_reviews` is longer than the rough estimate. The actual extraction is the correct scope.*

### Dependencies

**Constructor args:**
- `config` — risk params, scheduler params
- `broker` — place_order()
- `state_manager` — load/save portfolio
- `fill_protection` — can_place_order(), record_order()
- `risk_manager` — validate_entry() for target/stop adjustment validation
- `strategies` — strategy lookup for per-position routing
- `trade_journal` — snapshot records during review

**Mutable shared refs:**
- `latest_indicators: dict` — read current signals
- `pending_exit_hints: dict` — write exit reason on Claude-recommended exit
- `latest_market_context: dict` — read for thesis challenge
- `thesis_challenge_cache: dict` — read/write challenge results
- `entry_contexts: dict` — read for peak unrealized tracking

**Writes:**
- Portfolio position mutations (stop/target adjustments) — applied via `state_manager.save_portfolio()`
- Exit orders placed via broker — same pattern as QuantOverrides

### New class shape

```python
class PositionManager:
    def __init__(
        self,
        config: Config,
        broker: BrokerInterface,
        state_manager: StateManager,
        fill_protection: FillProtectionManager,
        risk_manager: RiskManager,
        strategies: list[Strategy],
        trade_journal: TradeJournal,
        # Mutable shared state
        latest_indicators: dict,
        pending_exit_hints: dict,
        latest_market_context: dict,
        thesis_challenge_cache: dict,
        entry_contexts: dict,
    ): ...

    async def evaluate_positions(self, portfolio, bars, indicators, acct, orders) -> None: ...
    async def apply_position_reviews(self, reviews, portfolio) -> None: ...
```

---

## Extraction 4: WatchlistLifecycle → `core/watchlist_manager.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:4512-4554` | ~43 | `_clear_directional_suppression(self, affected_sectors)` |
| `orchestrator.py:4555-4696` | ~142 | `_regime_reset_build(self, prev_sector_regimes, ...)` |
| `orchestrator.py:4697-4865` | ~169 | `_run_watchlist_build_task(self)` |
| `orchestrator.py:4866-4885` | ~20 | `_prune_expired_catalysts(self, watchlist)` |
| `orchestrator.py:4886-5041` | ~156 | `_apply_watchlist_changes(self, changes, ...)` |
| **Total** | **~530** | |

*Note: actual line count exceeds NOTES.md estimate (~445) — `_run_watchlist_build_task` and `_apply_watchlist_changes` are each larger than estimated.*

### Dependencies

**Constructor args:**
- `config` — scheduler, claude, universe_scanner settings
- `state_manager` — load/save watchlist
- `claude` — run_watchlist_build(), fetch_news()
- `universe_scanner` — scan() for candidate generation
- `search_adapter` — web search for build context
- `data_adapter` — fetch bars for new watchlist symbols

**Mutable shared refs:**
- `filter_suppressed: dict` — read in `_clear_directional_suppression`, write to clear entries
- `recommendation_outcomes: dict` — read/write outcome tracking for watchlist changes
- `latest_indicators: dict` — read for trigger price seeding
- `trigger_state: SlowLoopTriggerState` — write `last_watchlist_build_utc`
- `last_universe_scan: list` — read/write scan cache
- `last_universe_scan_time: float` — read/write scan timestamp
- `watchlist_build_in_flight: bool` — read/write build guard flag
- `reasoning_needed_after_build: bool` — read/write deferred reasoning flag

### Coupling note

`_clear_directional_suppression` writes to `_filter_suppressed` — but the writes are simple dict pops with a clear predicate (direction-dependent reasons only). Passing the dict by reference is safe. The orchestrator doesn't read `_filter_suppressed` concurrently during a watchlist build because builds run as fire-and-forget tasks within the same event loop.

### New class shape

```python
class WatchlistManager:
    def __init__(
        self,
        config: Config,
        state_manager: StateManager,
        claude_engine: ClaudeReasoningEngine,
        universe_scanner: UniverseScanner | None,
        search_adapter: SearchAdapter | None,
        data_adapter: YFinanceAdapter,
        trigger_state: SlowLoopTriggerState,
        # Mutable shared state
        filter_suppressed: dict,
        recommendation_outcomes: dict,
        latest_indicators: dict,
    ): ...

    # Build guard flags managed as instance attrs
    build_in_flight: bool = False
    reasoning_needed_after_build: bool = False
    last_universe_scan: list = []
    last_universe_scan_time: float = 0.0

    def clear_directional_suppression(self, affected_sectors: set[str] | None) -> None: ...
    async def regime_reset_build(self, prev_sector_regimes, new_sector_regimes, ...) -> None: ...
    async def run_watchlist_build_task(self) -> None: ...
    def prune_expired_catalysts(self, watchlist) -> list[str]: ...
    async def apply_watchlist_changes(self, changes, ...) -> None: ...
```

---

## Extraction 5: Reconciliation → `core/reconciliation.py`

### What moves

| Current location | Lines | Method |
|---|---|---|
| `orchestrator.py:723-1024` | ~302 | `startup_reconciliation(self)` |
| **Total** | **~302** | |

### Dependencies

**Constructor args:**
- `config` — risk params
- `broker` — get_positions(), get_orders(), get_account()
- `state_manager` — load/save portfolio, load/save orders
- `fill_protection` — rebuild from live orders
- `risk_manager` — validate reconciled positions
- `pdt_guard` — count day trades after reconciliation

**Mutable shared refs:**
- `filter_suppressed: dict` — write (suppress symbols with reconciliation issues; 3 write sites)
- `recommendation_outcomes: dict` — write (mark reconciled entries; 1 write site)
- `entry_contexts: dict` — restore from portfolio on restart
- `recently_closed: dict` — restore from portfolio.recently_closed on restart
- `position_entry_times: dict` — restore entry timestamps

### New class shape

```python
class Reconciliation:
    def __init__(
        self,
        config: Config,
        broker: BrokerInterface,
        state_manager: StateManager,
        fill_protection: FillProtectionManager,
        risk_manager: RiskManager,
        pdt_guard: PDTGuard,
        # Mutable shared state
        filter_suppressed: dict,
        recommendation_outcomes: dict,
        entry_contexts: dict,
        recently_closed: dict,
        position_entry_times: dict,
    ): ...

    async def run(self) -> None: ...
```

---

## Execution order

1. **QuantOverrides** — fast-loop internal, smallest, fewest cross-references
2. **PositionSync** — fast-loop internal, similar dependency shape
3. **Reconciliation** — startup-only, can't break loop behavior
4. **PositionManager** — medium/slow loop wiring needs more care
5. **WatchlistLifecycle** — largest, most mutable refs, highest risk in this phase

Each extraction is one commit. Full test suite after each.

---

## Files touched

| File | Change |
|---|---|
| `ozymandias/core/quant_overrides.py` | New — QuantOverrides class |
| `ozymandias/core/position_sync.py` | New — PositionSync class |
| `ozymandias/core/reconciliation.py` | New — Reconciliation class |
| `ozymandias/core/position_manager.py` | New — PositionManager class |
| `ozymandias/core/watchlist_manager.py` | New — WatchlistManager class |
| `ozymandias/core/orchestrator.py` | Remove moved methods, add delegation wrappers |
| `ozymandias/tests/test_quant_overrides.py` | New |
| `ozymandias/tests/test_position_sync.py` | New |
| `ozymandias/tests/test_reconciliation.py` | New |
| `ozymandias/tests/test_position_manager.py` | New |
| `ozymandias/tests/test_watchlist_manager.py` | New |

---

## Risk assessment

| Module | Risk | Why |
|---|---|---|
| QuantOverrides | Low | Fast-loop internal, no cross-loop coupling |
| PositionSync | Low | Fast-loop internal, degradation state is the only tricky part |
| Reconciliation | Low | Runs once at startup, isolated from loop timing |
| PositionManager | Medium | Called from both medium and slow loops — wiring must preserve call order |
| WatchlistManager | Medium | Fire-and-forget task scheduling, build guard flags, sector regime coupling |

---

## What remains in orchestrator after Phase 2

| Component | Lines (approx) | Why it stays |
|---|---|---|
| `__init__` + `_startup` + `_shutdown` | ~400 | Lifecycle — wires everything together |
| `_fast_loop_cycle` | ~120 | Coordinator — calls extracted fast-loop modules |
| `_medium_loop_cycle` | ~200 | Coordinator — calls TA scan, ranker, position eval |
| `_slow_loop_cycle` | ~150 | Coordinator — calls triggers, Claude, watchlist |
| `_medium_try_entry` | ~529 | Core entry pipeline — too coupled (8 writes to shared state) |
| `_run_claude_cycle` | ~512 | Core reasoning pipeline — too coupled (5 writes to shared state) |
| Delegation wrappers | ~200 | Thin forwarders to extracted modules |
| Misc helpers | ~250 | `_mark_broker_failure`, `_handle_claude_failure`, session helpers |
| **Total** | **~3030** | Down from 5393 — 44% reduction |

---

## What this does not change

- No behavioral changes. Mechanical extraction only.
- `_medium_try_entry` and `_run_claude_cycle` remain in orchestrator — they are the irreducible core.
- No config changes. No prompt changes.
- The "only the orchestrator knows about all other modules" rule is preserved — extracted modules don't know about each other, only about their injected dependencies.

---

## Parallel work zones unlocked (cumulative with Phase 1)

After both phases, 11 files can be worked on independently by parallel agents:

| Zone | File | Conflicts with |
|---|---|---|
| TA indicators | `technical_analysis.py` | Nothing |
| Strategies | `strategies/*.py` | Nothing |
| Triggers | `core/trigger_engine.py` | Nothing |
| Market context | `core/market_context.py` | Nothing |
| Fill handling | `core/fill_handler.py` | Nothing |
| Quant overrides | `core/quant_overrides.py` | Nothing |
| Position sync | `core/position_sync.py` | Nothing |
| Reconciliation | `core/reconciliation.py` | Nothing |
| Position reviews | `core/position_manager.py` | Nothing |
| Watchlist mgmt | `core/watchlist_manager.py` | Nothing |
| Risk manager | `execution/risk_manager.py` | Nothing |

**Serialize only:** `core/orchestrator.py` (coordinator), `config/prompts/` (Claude output format)
