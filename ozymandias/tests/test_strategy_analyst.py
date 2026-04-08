"""
Unit tests for Phase 27 — Strategy Analyst Agent role definition and schemas.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "agent_roles"


class TestAnalystRoleFile:
    def test_exists(self):
        assert (ROLES_DIR / "strategy_analyst.md").exists()

    def test_has_frontmatter(self):
        content = (ROLES_DIR / "strategy_analyst.md").read_text()
        assert content.startswith("---")
        assert "name: strategy_analyst" in content
        assert "model:" in content

    def test_has_four_categories(self):
        content = (ROLES_DIR / "strategy_analyst.md").read_text()
        assert "Signal Present, Bot Ignored" in content
        assert "Signal Present, Bot Saw But Filtered" in content
        assert "Signal Ambiguous" in content
        assert "Truly Unforeseeable" in content

    def test_has_hindsight_gate(self):
        content = (ROLES_DIR / "strategy_analyst.md").read_text()
        assert "Hindsight" in content or "hindsight" in content
        assert "AT DECISION TIME" in content or "at decision time" in content

    def test_has_ontologist(self):
        content = (ROLES_DIR / "strategy_analyst.md").read_text()
        assert "Ontologist" in content

    def test_has_findings_output_convention(self):
        content = (ROLES_DIR / "strategy_analyst.md").read_text()
        assert "findings.json" in content
        assert "state/signals/analyst/" in content


class TestFindingsOutputSchema:
    def test_expected_fields(self):
        findings = {
            "date": "2026-04-07",
            "trades_analyzed": 12,
            "watchlist_symbols_analyzed": 25,
            "findings": [
                {
                    "finding_id": "2026-04-07-nke-oversold-bounce",
                    "category": "signal_present_bot_filtered",
                    "symbol": "NKE",
                    "signal_citation": "BB squeeze firing at 10:15 ET, RSI 22",
                    "what_happened": "NKE bounced 3.2%",
                    "what_blocked_it": "min_composite_score floor",
                    "recommendation": "Lower composite floor during oversold regime",
                    "severity": "medium",
                }
            ],
            "summary": "12 trades analyzed. 1 finding.",
        }
        required = {"date", "trades_analyzed", "findings", "summary"}
        assert required.issubset(set(findings.keys()))

        finding = findings["findings"][0]
        finding_required = {"finding_id", "category", "symbol", "signal_citation", "severity"}
        assert finding_required.issubset(set(finding.keys()))
        assert finding["category"] in (
            "signal_present_bot_ignored",
            "signal_present_bot_filtered",
            "signal_ambiguous",
            "truly_unforeseeable",
        )


class TestFindingsLogSchema:
    def test_log_entry_fields(self):
        entry = {
            "date": "2026-04-07",
            "finding_id": "2026-04-07-nke-oversold-bounce",
            "category": "signal_present_bot_filtered",
            "status": "queued",
            "summary": "NKE oversold bounce blocked by composite score floor",
        }
        required = {"date", "finding_id", "category", "status", "summary"}
        assert required.issubset(set(entry.keys()))
        assert entry["status"] in ("queued", "completed", "dismissed")
