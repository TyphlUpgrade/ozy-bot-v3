# Smoke test: pipeline verified this file
"""
Unit tests for Phase 28 — Agent role definitions for the v4 agentic workflow.

Verifies all 7 role files exist with correct frontmatter and required content sections.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "agent_roles"

ALL_ROLES = [
    "conductor.md",
    "ops_monitor.md",
    "strategy_analyst.md",
    "dialogue.md",
    "executor.md",
    "architect.md",
    "reviewer.md",
]


class TestAllRolesExist:
    def test_all_roles_exist(self):
        for role_file in ALL_ROLES:
            assert (ROLES_DIR / role_file).exists(), f"Missing role file: {role_file}"

    def test_all_roles_have_frontmatter(self):
        for role_file in ALL_ROLES:
            content = (ROLES_DIR / role_file).read_text()
            assert content.startswith("---"), f"{role_file} missing frontmatter"
            assert "name:" in content, f"{role_file} missing name in frontmatter"
            assert "model:" in content or "tier:" in content, (
                f"{role_file} missing model/tier in frontmatter"
            )


class TestExecutorRole:
    def test_exists(self):
        assert (ROLES_DIR / "executor.md").exists()

    def test_has_frontmatter(self):
        content = (ROLES_DIR / "executor.md").read_text()
        assert content.startswith("---")
        assert "name: executor" in content
        assert "model:" in content

    def test_has_trading_rules(self):
        content = (ROLES_DIR / "executor.md").read_text()
        assert "asyncio" in content
        assert "No third-party TA" in content or "no third-party TA" in content
        assert "atomic" in content.lower()

    def test_has_simplifier(self):
        content = (ROLES_DIR / "executor.md").read_text()
        assert "Simplifier" in content
        assert "0.15" in content

    def test_has_zone_protocol(self):
        content = (ROLES_DIR / "executor.md").read_text()
        assert "zone" in content.lower()
        assert "units_completed" in content
        assert "history" in content


class TestArchitectRole:
    def test_exists(self):
        assert (ROLES_DIR / "architect.md").exists()

    def test_has_frontmatter(self):
        content = (ROLES_DIR / "architect.md").read_text()
        assert content.startswith("---")
        assert "name: architect" in content
        assert "disallowedTools: Write, Edit" in content

    def test_has_intent_classification(self):
        content = (ROLES_DIR / "architect.md").read_text()
        assert "bug" in content
        assert "calibration" in content
        assert "feature" in content
        assert "refactor" in content
        assert "analysis" in content

    def test_has_readiness_gates(self):
        content = (ROLES_DIR / "architect.md").read_text()
        assert "Non-goals" in content or "non_goals" in content
        assert "Decision boundaries" in content or "decision_boundaries" in content

    def test_has_checkpoint_strategy(self):
        content = (ROLES_DIR / "architect.md").read_text()
        assert "checkpoint" in content.lower()
        assert "risky" in content.lower() or "integration" in content.lower()

    def test_has_trading_rules(self):
        content = (ROLES_DIR / "architect.md").read_text()
        assert "asyncio" in content
        assert "No third-party TA" in content
        assert "atomic" in content.lower()

    def test_has_signal_convention(self):
        content = (ROLES_DIR / "architect.md").read_text()
        assert "state/signals/architect/" in content


class TestReviewerRole:
    def test_exists(self):
        assert (ROLES_DIR / "reviewer.md").exists()

    def test_has_frontmatter(self):
        content = (ROLES_DIR / "reviewer.md").read_text()
        assert content.startswith("---")
        assert "name: reviewer" in content
        assert "disallowedTools: Write, Edit" in content

    def test_has_contrarian(self):
        content = (ROLES_DIR / "reviewer.md").read_text()
        assert "Contrarian" in content
        assert "0.25" in content

    def test_has_verification_tiers(self):
        content = (ROLES_DIR / "reviewer.md").read_text()
        assert "Light" in content
        assert "Standard" in content
        assert "Thorough" in content

    def test_has_trading_convention_checks(self):
        content = (ROLES_DIR / "reviewer.md").read_text()
        assert "Atomic writes" in content or "atomic" in content.lower()
        assert "Broker abstraction" in content or "broker" in content.lower()
        assert "asyncio" in content

    def test_has_verdict_format(self):
        content = (ROLES_DIR / "reviewer.md").read_text()
        assert "verdict.json" in content
        assert "approve" in content
        assert "reject" in content
        assert "file" in content and "line" in content

    def test_has_signal_convention(self):
        content = (ROLES_DIR / "reviewer.md").read_text()
        assert "state/signals/reviewer/" in content
