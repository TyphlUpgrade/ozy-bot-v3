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

import logging
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
    if action not in ACTION_TO_DIRECTION:
        logging.getLogger(__name__).warning(
            "Unrecognised action '%s' — defaulting to 'long'. "
            "Add to ACTION_TO_DIRECTION if intentional.",
            action,
        )
    return ACTION_TO_DIRECTION.get(action, "long")


def is_short(direction: Direction) -> bool:
    """Convenience predicate — avoids string literals at call sites."""
    return direction == "short"
