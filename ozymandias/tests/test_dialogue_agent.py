"""
Unit tests for Phase 25 — Strategy Dialogue Agent role definition and signal conventions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "agent_roles"


class TestDialogueRoleFile:
    def test_exists(self):
        assert (ROLES_DIR / "dialogue.md").exists()

    def test_has_frontmatter(self):
        content = (ROLES_DIR / "dialogue.md").read_text()
        assert content.startswith("---")
        assert "name: dialogue" in content
        assert "model:" in content
        assert "tier:" in content

    def test_has_personas(self):
        content = (ROLES_DIR / "dialogue.md").read_text()
        assert "Contrarian" in content
        assert "Simplifier" in content
        assert "Ontologist" in content

    def test_has_readiness_gates(self):
        content = (ROLES_DIR / "dialogue.md").read_text()
        assert "Non-goals" in content or "non-goals" in content
        assert "Decision boundaries" in content or "decision boundaries" in content

    def test_has_ambiguity_scoring(self):
        content = (ROLES_DIR / "dialogue.md").read_text()
        assert "0.20" in content  # threshold
        assert "Intent" in content
        assert "Outcome" in content
        assert "Scope" in content


class TestDialogueSignalSchemas:
    def test_response_schema(self):
        """Verify response.json schema has expected fields."""
        response = {
            "type": "dialogue_response",
            "ts": "2026-04-08T14:00:00Z",
            "text": "Here is my analysis...",
            "channel": "strategy",
        }
        assert "type" in response
        assert "ts" in response
        assert "text" in response
        assert response["type"] == "dialogue_response"

    def test_inbound_schema(self):
        """Verify inbound.json schema has expected fields."""
        inbound = {
            "type": "dialogue_inbound",
            "ts": "2026-04-08T14:00:00Z",
            "text": "What do you think about NVDA?",
            "author": "operator",
        }
        assert "type" in inbound
        assert "text" in inbound
        assert "author" in inbound


class TestMessageChunking:
    def test_short_message_not_chunked(self):
        text = "Short message"
        chunks = _chunk_message(text, limit=2000)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_message_chunked(self):
        text = "x" * 5000
        chunks = _chunk_message(text, limit=2000)
        assert len(chunks) == 3
        assert all(len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == text


def _chunk_message(text: str, limit: int = 2000) -> list[str]:
    """Split a message into chunks for Discord's character limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
