"""
Unit tests for Phase 26 — Ops Monitor Agent role definition and schemas.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "agent_roles"


class TestOpsMonitorRoleFile:
    def test_exists(self):
        assert (ROLES_DIR / "ops_monitor.md").exists()

    def test_has_frontmatter(self):
        content = (ROLES_DIR / "ops_monitor.md").read_text()
        assert content.startswith("---")
        assert "name: ops_monitor" in content
        assert "model: haiku" in content

    def test_has_anomaly_rules(self):
        content = (ROLES_DIR / "ops_monitor.md").read_text()
        assert "Stale Timestamp" in content or "stale" in content.lower()
        assert "WARNING Cluster" in content or "warning" in content.lower()
        assert "Equity Drawdown" in content or "drawdown" in content.lower()

    def test_has_escalation_tiers(self):
        content = (ROLES_DIR / "ops_monitor.md").read_text()
        assert "Tier 1" in content or "Auto-handle" in content
        assert "Tier 2" in content or "Notify" in content
        assert "Tier 3" in content or "Escalate" in content

    def test_has_rate_limit(self):
        content = (ROLES_DIR / "ops_monitor.md").read_text()
        assert "3" in content and ("per" in content.lower() or "hour" in content.lower())

    def test_has_permission_tiers(self):
        content = (ROLES_DIR / "ops_monitor.md").read_text()
        assert "ReadOnly" in content
        assert "ProcessControl" in content
        assert "DangerFullAccess" in content


class TestDailySummarySchema:
    def test_expected_fields(self):
        summary = {
            "date": "2026-04-08",
            "last_updated": "2026-04-08T16:00:00Z",
            "anomaly_counts": {
                "stale_timestamp": 0,
                "warning_cluster": 2,
                "error_pattern": 0,
                "equity_drawdown": 1,
            },
            "bug_reports_this_hour": 1,
            "patterns": [],
            "restarts_this_hour": 0,
        }
        required = {"date", "last_updated", "anomaly_counts", "bug_reports_this_hour", "patterns", "restarts_this_hour"}
        assert required.issubset(set(summary.keys()))
        assert isinstance(summary["anomaly_counts"], dict)
        assert isinstance(summary["patterns"], list)
