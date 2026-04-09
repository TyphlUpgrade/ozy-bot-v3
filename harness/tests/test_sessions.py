"""Tests for harness.lib.sessions — directive loading, caveman config."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.lib.sessions import VALID_LEVELS, _load_caveman_directives


class TestCavemanDirectiveLoading:
    def test_load_from_real_skill(self):
        skill_path = Path("~/.claude/plugins/marketplaces/caveman/caveman/SKILL.md").expanduser()
        if not skill_path.exists():
            pytest.skip("Caveman SKILL.md not installed")
        directives = _load_caveman_directives(skill_path)
        assert len(directives) == 6
        for level in ("lite", "full", "ultra", "wenyan-lite", "wenyan-full", "wenyan-ultra"):
            assert level in directives
            assert f"Active level: {level}" in directives[level]

    def test_load_missing_skill(self, tmp_path):
        directives = _load_caveman_directives(tmp_path / "nonexistent.md")
        assert directives == {}

    def test_load_strips_frontmatter(self, tmp_path):
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("---\nname: test\n---\nDirective content here.")
        directives = _load_caveman_directives(skill_file)
        assert len(directives) == 6
        for level, text in directives.items():
            assert not text.startswith("---")
            assert "Directive content here." in text
            assert f"Active level: {level}" in text


class TestDiscordCompanionParsers:
    """Test the command parsers via discord_companion module."""

    def test_parse_caveman_backward_compat(self):
        from harness.discord_companion import parse_caveman
        assert parse_caveman("full") == ("all", "full")
        assert parse_caveman("ultra") == ("all", "ultra")
        assert parse_caveman("off") == ("all", "off")

    def test_parse_caveman_per_agent(self):
        from harness.discord_companion import parse_caveman
        assert parse_caveman("executor ultra") == ("executor", "ultra")
        assert parse_caveman("architect off") == ("architect", "off")

    def test_parse_caveman_special(self):
        from harness.discord_companion import parse_caveman
        assert parse_caveman("status") == ("status", "")
        assert parse_caveman("reset") == ("reset", "")
        assert parse_caveman("") == ("status", "")

    def test_parse_tell(self):
        from harness.discord_companion import parse_tell
        assert parse_tell("executor stop using print") == ("executor", "stop using print")
        assert parse_tell("") == ("", "")
        assert parse_tell("oneword") == ("", "")

    def test_parse_reply(self):
        from harness.discord_companion import parse_reply
        assert parse_reply("task-001 yes do it") == ("task-001", "yes do it")
        assert parse_reply("") == ("", "")
