"""Tiered escalation routing, confidence gating, and timeout logic."""

from __future__ import annotations

import logging
from datetime import datetime, UTC

from .signals import ArchitectResolution, EscalationRequest

logger = logging.getLogger("harness.escalation")


# ---------- Category Routing ----------

# Categories where architect gets first shot (Tier 1)
TIER1_CATEGORIES = frozenset({
    "ambiguous_requirement",
    "design_choice",
})

# Categories that always skip to operator (Tier 2)
OPERATOR_DIRECT_CATEGORIES = frozenset({
    "security_concern",
    "cost_approval",
    "scope_question",
    "permission_request",
})

# All valid escalation categories
VALID_CATEGORIES = TIER1_CATEGORIES | OPERATOR_DIRECT_CATEGORIES | {"persistent_failure"}


def route_escalation(esc: EscalationRequest) -> str:
    """Determine escalation tier based on category and retry_count.

    Returns "tier1" (architect-first) or "tier2" (operator-direct).
    """
    if esc.category in OPERATOR_DIRECT_CATEGORIES:
        return "tier2"
    if esc.category == "persistent_failure":
        # After 2 retries, skip architect — circular replanning risk
        return "tier1" if esc.retry_count < 2 else "tier2"
    if esc.category in TIER1_CATEGORIES:
        return "tier1"
    # Unknown category — safe default to operator
    logger.warning("Unknown escalation category %r — routing to operator", esc.category)
    return "tier2"


# ---------- Message Formatting ----------


def format_escalation_for_architect(esc: EscalationRequest) -> str:
    """Format an escalation question for the architect's FIFO."""
    options_str = ", ".join(esc.options) if esc.options else "(no options provided)"
    return (
        f"[ESCALATION from {esc.agent}] {esc.question}\n"
        f"Context: {esc.context}\n"
        f"Options: {options_str}"
    )


def format_tier2_notification(
    esc: EscalationRequest,
    architect_assessment: str | None = None,
) -> str:
    """Format an escalation summary for Discord (Tier 2 notification)."""
    options_str = ", ".join(esc.options) if esc.options else "(no options)"
    parts = [
        f"ESCALATION [{esc.severity}] from {esc.agent} on task {esc.task_id}",
        f"Category: {esc.category}",
        f"Question: {esc.question}",
        f"Options: {options_str}",
    ]
    if architect_assessment:
        parts.append(f"Architect assessment: {architect_assessment}")
    parts.append(f"Reply with: !reply {esc.task_id} <your response>")
    return "\n".join(parts)


# ---------- Confidence Gating ----------


def should_promote(resolution: ArchitectResolution) -> bool:
    """Return True if architect resolution should be promoted to Tier 2.

    Promotes when: cannot_resolve, or anything other than high confidence.
    Safe-by-default: unknown confidence values promote rather than silently resolving.
    """
    if resolution.resolution == "cannot_resolve":
        return True
    return resolution.confidence != "high"


# ---------- Timeout Logic ----------


def _elapsed_seconds(started_ts: str) -> float:
    """Seconds elapsed since a timestamp."""
    started = datetime.fromisoformat(started_ts)
    return (datetime.now(UTC) - started).total_seconds()


def should_renotify(
    started_ts: str | None,
    interval_seconds: int,
    last_renotify_ts: str | None = None,
) -> bool:
    """Return True if a blocking escalation should re-notify the operator.

    Re-notifies every `interval_seconds` (default 4 hours = 14400s).
    Must have been waiting at least one full interval before first re-notify.
    Uses `last_renotify_ts` to track the last notification time, avoiding the
    poll-window assumption of the previous modulo approach.
    """
    if started_ts is None:
        return False
    elapsed = _elapsed_seconds(started_ts)
    if elapsed < interval_seconds:
        return False
    if last_renotify_ts is None:
        return True
    return _elapsed_seconds(last_renotify_ts) >= interval_seconds


def should_auto_proceed(esc: EscalationRequest, started_ts: str | None,
                        timeout_seconds: int) -> bool:
    """Return True if an advisory escalation should auto-proceed.

    Advisory escalations let the agent continue with their best guess after timeout.
    Blocking escalations never auto-proceed.
    """
    if esc.severity != "advisory":
        return False
    if started_ts is None:
        return False
    return _elapsed_seconds(started_ts) >= timeout_seconds
