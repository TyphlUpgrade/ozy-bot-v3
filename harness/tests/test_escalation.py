"""Tests for harness.lib.escalation — tiered routing, confidence gating, timeouts."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from harness.lib.escalation import (
    OPERATOR_DIRECT_CATEGORIES,
    TIER1_CATEGORIES,
    VALID_CATEGORIES,
    format_escalation_for_architect,
    format_tier2_notification,
    route_escalation,
    should_auto_proceed,
    should_promote,
    should_renotify,
)
from harness.lib.signals import ArchitectResolution, EscalationRequest


# ---------- Helpers ----------


def _make_esc(category: str = "ambiguous_requirement", severity: str = "blocking",
              retry_count: int = 0, agent: str = "executor",
              task_id: str = "task-001") -> EscalationRequest:
    return EscalationRequest(
        task_id=task_id,
        agent=agent,
        stage="executor",
        severity=severity,
        category=category,
        question="What should we do about X?",
        options=["option_a", "option_b"],
        context="Some context about the problem.",
        retry_count=retry_count,
    )


def _make_resolution(resolution: str = "option_a", confidence: str = "high",
                     reasoning: str = "Clear from codebase") -> ArchitectResolution:
    return ArchitectResolution(
        task_id="task-001",
        resolution=resolution,
        reasoning=reasoning,
        confidence=confidence,
    )


# ---------- Category Routing ----------


class TestRouteEscalation:
    def test_ambiguous_requirement_routes_tier1(self):
        esc = _make_esc(category="ambiguous_requirement")
        assert route_escalation(esc) == "tier1"

    def test_design_choice_routes_tier1(self):
        esc = _make_esc(category="design_choice")
        assert route_escalation(esc) == "tier1"

    def test_security_concern_routes_tier2(self):
        esc = _make_esc(category="security_concern")
        assert route_escalation(esc) == "tier2"

    def test_cost_approval_routes_tier2(self):
        esc = _make_esc(category="cost_approval")
        assert route_escalation(esc) == "tier2"

    def test_scope_question_routes_tier2(self):
        esc = _make_esc(category="scope_question")
        assert route_escalation(esc) == "tier2"

    def test_permission_request_routes_tier2(self):
        esc = _make_esc(category="permission_request")
        assert route_escalation(esc) == "tier2"

    def test_persistent_failure_low_retries_routes_tier1(self):
        esc = _make_esc(category="persistent_failure", retry_count=0)
        assert route_escalation(esc) == "tier1"
        esc = _make_esc(category="persistent_failure", retry_count=1)
        assert route_escalation(esc) == "tier1"

    def test_persistent_failure_high_retries_routes_tier2(self):
        esc = _make_esc(category="persistent_failure", retry_count=2)
        assert route_escalation(esc) == "tier2"
        esc = _make_esc(category="persistent_failure", retry_count=5)
        assert route_escalation(esc) == "tier2"

    def test_unknown_category_routes_tier2(self):
        esc = _make_esc(category="totally_unknown")
        assert route_escalation(esc) == "tier2"

    def test_all_operator_direct_categories_route_tier2(self):
        for cat in OPERATOR_DIRECT_CATEGORIES:
            esc = _make_esc(category=cat)
            assert route_escalation(esc) == "tier2", f"{cat} should route to tier2"

    def test_all_tier1_categories_route_tier1(self):
        for cat in TIER1_CATEGORIES:
            esc = _make_esc(category=cat)
            assert route_escalation(esc) == "tier1", f"{cat} should route to tier1"


# ---------- Confidence Gating ----------


class TestShouldPromote:
    def test_high_confidence_does_not_promote(self):
        res = _make_resolution(confidence="high")
        assert not should_promote(res)

    def test_low_confidence_promotes(self):
        res = _make_resolution(confidence="low")
        assert should_promote(res)

    def test_cannot_resolve_promotes(self):
        res = _make_resolution(resolution="cannot_resolve", confidence="high")
        assert should_promote(res)

    def test_cannot_resolve_low_confidence_promotes(self):
        res = _make_resolution(resolution="cannot_resolve", confidence="low")
        assert should_promote(res)

    def test_unknown_confidence_promotes(self):
        """Safe-by-default: unknown confidence values promote rather than silently resolving."""
        res = _make_resolution(confidence="medium")
        assert should_promote(res)

    def test_empty_confidence_promotes(self):
        res = _make_resolution(confidence="")
        assert should_promote(res)


# ---------- Message Formatting ----------


class TestFormatEscalation:
    def test_architect_message_contains_key_fields(self):
        esc = _make_esc()
        msg = format_escalation_for_architect(esc)
        assert "[ESCALATION from executor]" in msg
        assert "What should we do about X?" in msg
        assert "option_a" in msg
        assert "Some context" in msg

    def test_tier2_notification_contains_key_fields(self):
        esc = _make_esc()
        msg = format_tier2_notification(esc)
        assert "task-001" in msg
        assert "blocking" in msg
        assert "!reply task-001" in msg

    def test_tier2_notification_includes_architect_assessment(self):
        esc = _make_esc()
        msg = format_tier2_notification(esc, architect_assessment="Could not determine intent")
        assert "Could not determine intent" in msg

    def test_tier2_notification_without_architect_assessment(self):
        esc = _make_esc()
        msg = format_tier2_notification(esc)
        assert "Architect assessment" not in msg

    def test_architect_message_handles_empty_options(self):
        esc = _make_esc()
        esc.options = []
        msg = format_escalation_for_architect(esc)
        assert "no options provided" in msg


# ---------- Timeout Logic ----------


class TestShouldRenotify:
    def test_none_timestamp_returns_false(self):
        assert not should_renotify(None, 14400)

    def test_recent_escalation_does_not_renotify(self):
        ts = datetime.now(UTC).isoformat()
        assert not should_renotify(ts, 14400)

    def test_old_escalation_at_interval_boundary_renotifies(self):
        # 4 hours ago exactly
        ts = (datetime.now(UTC) - timedelta(seconds=14400)).isoformat()
        assert should_renotify(ts, 14400)


class TestShouldAutoProceed:
    def test_blocking_never_auto_proceeds(self):
        esc = _make_esc(severity="blocking")
        ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        assert not should_auto_proceed(esc, ts, 14400)

    def test_advisory_before_timeout_does_not_proceed(self):
        esc = _make_esc(severity="advisory")
        ts = datetime.now(UTC).isoformat()
        assert not should_auto_proceed(esc, ts, 14400)

    def test_advisory_after_timeout_proceeds(self):
        esc = _make_esc(severity="advisory")
        ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        assert should_auto_proceed(esc, ts, 14400)

    def test_advisory_none_timestamp_does_not_proceed(self):
        esc = _make_esc(severity="advisory")
        assert not should_auto_proceed(esc, None, 14400)

    def test_informational_does_not_auto_proceed(self):
        esc = _make_esc(severity="informational")
        ts = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        assert not should_auto_proceed(esc, ts, 14400)
