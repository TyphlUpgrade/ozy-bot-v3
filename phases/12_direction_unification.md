# Phase 12: Direction Unification — Canonical Direction Type

This phase has no hard prerequisites and should be implemented **before Phases 15 and 16**, not after. Phases 15 and 16 both introduce new direction-sensitive code (watchlist `expected_direction`, `roc_negative_deceleration`, ATR cap). Implementing Phase 12 first means those phases import from `core/direction` from the start rather than adding new ad-hoc mappings that must be migrated later.

Read DRIFT_LOG.md for any direction-related deviations logged by prior phases before starting.

Direction is currently expressed four different ways across the codebase:

1. **Claude action strings**: `"buy"` / `"sell_short"` — from `ReasoningResult.new_opportunities`
2. **Internal position direction**: `"long"` / `"short"` — stored in `position.intention.direction`
3. **Broker order side**: `"buy"` / `"sell"` — where `"sell"` means *both* closing a long and opening a short
4. **Ad-hoc mappings**: `_ACTION_TO_DIRECTION` in `opportunity_ranker.py`, inline `action == "sell_short"` checks in `orchestrator.py`, `is_short = ...` local variables scattered across modules

This is a modularity liability: adding a new action type (e.g. a spread, or a covered call side) requires finding and updating every ad-hoc check. More concretely, it is already the source of the three bugs fixed before this phase was written — each was caused by a module that didn't know about another module's direction convention.

## 1. New Module: `core/direction.py`

Create `ozymandias/core/direction.py`. This is the **single source of truth** for all direction-related mappings. Every module that needs to reason about direction imports from here.

```python
"""
core/direction.py
=================
Canonical direction type and all cross-convention mappings.

The codebase uses three direction-adjacent conventions:
  - Claude action strings  : "buy" | "sell_short"
  - Internal direction     : "long" | "short"
  - Broker order side      : "buy" | "sell"

All conversions go through this module.  To add a new action type, add
one entry to each applicable table below; no other file needs to change.
"""
from __future__ import annotations

from typing import Literal

# Canonical direction type.  Use this in function signatures and dataclasses
# wherever a direction value is stored or passed.
Direction = Literal["long", "short"]

# ---------------------------------------------------------------------------
# Claude action string → Direction
# ---------------------------------------------------------------------------
# Maps the "action" field from ReasoningResult.new_opportunities to the
# canonical direction used internally.  Add one entry here for each new
# Claude action type.
ACTION_TO_DIRECTION: dict[str, Direction] = {
    "buy":        "long",
    "sell_short": "short",
}

# ---------------------------------------------------------------------------
# Direction → broker order side (entry)
# ---------------------------------------------------------------------------
# Maps internal direction to the broker side string used when opening a position.
ENTRY_SIDE: dict[Direction, str] = {
    "long":  "buy",
    "short": "sell",
}

# ---------------------------------------------------------------------------
# Direction → broker order side (exit)
# ---------------------------------------------------------------------------
# Maps internal direction to the broker side string used when closing a position.
EXIT_SIDE: dict[Direction, str] = {
    "long":  "sell",
    "short": "buy",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def direction_from_action(action: str) -> Direction:
    """
    Return the canonical Direction for a Claude action string.

    Defaults to ``"long"`` for unrecognised action strings so callers never
    crash on unexpected Claude output — they just treat it as a long.
    """
    return ACTION_TO_DIRECTION.get(action, "long")


def is_short(direction: Direction) -> bool:
    """Convenience predicate — avoids string literals at call sites."""
    return direction == "short"
```

## 2. Migration

Replace every ad-hoc direction check across the codebase with imports from `core/direction`:

| Location | Current pattern | Replace with |
|---|---|---|
| `opportunity_ranker.py` | `_ACTION_TO_DIRECTION = {...}` | `from ozymandias.core.direction import ACTION_TO_DIRECTION` |
| `opportunity_ranker.py` | `_ACTION_TO_DIRECTION.get(action, "long")` | `direction_from_action(action)` |
| `orchestrator.py` | `is_short = opportunity.get("action") == "sell_short"` | `is_short(direction_from_action(action))` |
| any module | inline `"long"` / `"short"` string literals in conditionals | `is_short(direction)` or `direction == "long"` with imported type |

The `Direction` type annotation should be used in:
- `ScoredOpportunity.action` — annotate as the Claude string, not Direction (it is passed through to the broker)
- `PositionIntention.direction` — annotate as `Direction`
- Any new function that takes or returns a direction value

Do not change the Claude prompt output format or the broker API call format — those conventions are external contracts. Only internal plumbing migrates.

## 3. Guard Against Unknown Actions

Any code path that calls `direction_from_action` on a Claude-supplied value should log a WARNING when the action is not in `ACTION_TO_DIRECTION`, so new Claude output fields are caught quickly:

```python
def direction_from_action(action: str) -> Direction:
    if action not in ACTION_TO_DIRECTION:
        import logging
        logging.getLogger(__name__).warning(
            "Unrecognised action '%s' — defaulting to 'long'. "
            "Add to ACTION_TO_DIRECTION if intentional.",
            action,
        )
    return ACTION_TO_DIRECTION.get(action, "long")
```

## 4. Tests to Write

Create `tests/test_direction.py`:

- **All `ACTION_TO_DIRECTION` entries map correctly**: `"buy"` → `"long"`, `"sell_short"` → `"short"`
- **`direction_from_action` unknown action**: `"buy_to_cover"` → returns `"long"`, emits WARNING log
- **`direction_from_action` unknown action does not raise**: no exception on unrecognised input
- **`ENTRY_SIDE` and `EXIT_SIDE` are inverses**: for each direction, `ENTRY_SIDE[d] != EXIT_SIDE[d]`
- **Round-trip long**: `"buy"` → `direction_from_action` → `ENTRY_SIDE` → `"buy"`, `EXIT_SIDE` → `"sell"`
- **Round-trip short**: `"sell_short"` → `direction_from_action` → `ENTRY_SIDE` → `"sell"`, `EXIT_SIDE` → `"buy"`
- **`is_short` predicate**: `is_short("short")` → True, `is_short("long")` → False

## 5. Done When

- `ozymandias/core/direction.py` exists and all tables/helpers are implemented
- All existing ad-hoc `ACTION_TO_DIRECTION`, `is_short = action == "sell_short"`, and equivalent patterns in `opportunity_ranker.py` and `orchestrator.py` are replaced with imports from `core/direction`
- `PositionIntention.direction` annotated as `Direction` type
- All existing tests pass; `test_direction.py` tests pass
- No new `== "sell_short"` or `== "buy"` direction-check string literals introduced outside `core/direction.py` and `core/broker_interface.py` (the broker contract layer)
- DRIFT_LOG.md has a Phase 16 entry noting the new module and migrated call sites
