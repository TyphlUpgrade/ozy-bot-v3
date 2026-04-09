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


class TestTokenTracking:
    """Tests for Session token tracking and rotation checks."""

    def _make_session_mgr(self, tmp_path):
        """Create a minimal SessionManager with mock config for token tests."""
        from harness.lib.sessions import Session, SessionManager
        from harness.lib.pipeline import ProjectConfig, AgentDef, CavemanConfig
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(exist_ok=True)
        for n in ("architect", "executor", "reviewer"):
            (agents_dir / f"{n}.md").write_text(f"# {n}")
        config = ProjectConfig(
            project_root=tmp_path, signal_dir=tmp_path / "signals",
            task_dir=tmp_path / "tasks", state_file=tmp_path / "state.json",
            session_dir=tmp_path / "sessions", worktree_base=tmp_path / "wt",
            poll_interval=1.0, max_retries=3, escalation_timeout=14400,
            tier1_timeout=1800, token_rotation_threshold=100_000,
            agents={
                "architect": AgentDef(name="architect", model="opus", mode="read-only",
                                      lifecycle="persistent", role_file=agents_dir / "architect.md"),
                "executor": AgentDef(name="executor", model="sonnet", mode="full",
                                     lifecycle="per-task", role_file=agents_dir / "executor.md"),
                "reviewer": AgentDef(name="reviewer", model="sonnet", mode="read-only",
                                     lifecycle="persistent", role_file=agents_dir / "reviewer.md"),
            },
            caveman=CavemanConfig(),
        )
        mgr = SessionManager(tmp_path / "sessions", config)
        return mgr

    def test_parse_token_usage_from_stream_json(self, tmp_path):
        import json
        from harness.lib.sessions import Session
        mgr = self._make_session_mgr(tmp_path)
        log_path = tmp_path / "test.log"
        log_path.write_text(json.dumps({"type": "result", "usage": {"input_tokens": 500, "output_tokens": 300}}) + "\n")
        session = Session(name="test", role="executor", fd=0, fifo=tmp_path / "f", log=log_path)
        mgr.sessions["test"] = session
        ti, to = mgr.parse_token_usage("test")
        assert ti == 500
        assert to == 300
        assert session.tokens_in == 500
        assert session.tokens_out == 300

    def test_parse_token_usage_sums_multiple_entries(self, tmp_path):
        import json
        from harness.lib.sessions import Session
        mgr = self._make_session_mgr(tmp_path)
        log_path = tmp_path / "test.log"
        lines = [
            json.dumps({"usage": {"input_tokens": 100, "output_tokens": 50}}),
            json.dumps({"usage": {"input_tokens": 200, "output_tokens": 150}}),
            json.dumps({"type": "text", "content": "hello"}),
            json.dumps({"usage": {"input_tokens": 300, "output_tokens": 200}}),
        ]
        log_path.write_text("\n".join(lines) + "\n")
        session = Session(name="test", role="executor", fd=0, fifo=tmp_path / "f", log=log_path)
        mgr.sessions["test"] = session
        ti, to = mgr.parse_token_usage("test")
        assert ti == 600
        assert to == 400

    def test_parse_token_usage_handles_missing_log(self, tmp_path):
        from harness.lib.sessions import Session
        mgr = self._make_session_mgr(tmp_path)
        session = Session(name="test", role="executor", fd=0, fifo=tmp_path / "f", log=tmp_path / "nope.log")
        mgr.sessions["test"] = session
        assert mgr.parse_token_usage("test") == (0, 0)

    def test_parse_token_usage_skips_malformed_lines(self, tmp_path):
        import json
        from harness.lib.sessions import Session
        mgr = self._make_session_mgr(tmp_path)
        log_path = tmp_path / "test.log"
        log_path.write_text("not json\n" + json.dumps({"usage": {"input_tokens": 42, "output_tokens": 7}}) + "\n{bad\n")
        session = Session(name="test", role="executor", fd=0, fifo=tmp_path / "f", log=log_path)
        mgr.sessions["test"] = session
        ti, to = mgr.parse_token_usage("test")
        assert ti == 42
        assert to == 7

    def test_needs_rotation_true_above_threshold(self, tmp_path):
        import json
        from harness.lib.sessions import Session
        mgr = self._make_session_mgr(tmp_path)
        log_path = tmp_path / "test.log"
        log_path.write_text(json.dumps({"usage": {"input_tokens": 60000, "output_tokens": 50000}}) + "\n")
        session = Session(name="test", role="executor", fd=0, fifo=tmp_path / "f", log=log_path)
        mgr.sessions["test"] = session
        assert mgr.needs_rotation("test", 100_000) is True

    def test_needs_rotation_false_below_threshold(self, tmp_path):
        import json
        from harness.lib.sessions import Session
        mgr = self._make_session_mgr(tmp_path)
        log_path = tmp_path / "test.log"
        log_path.write_text(json.dumps({"usage": {"input_tokens": 100, "output_tokens": 100}}) + "\n")
        session = Session(name="test", role="executor", fd=0, fifo=tmp_path / "f", log=log_path)
        mgr.sessions["test"] = session
        assert mgr.needs_rotation("test", 100_000) is False

    def test_parse_unknown_session(self, tmp_path):
        mgr = self._make_session_mgr(tmp_path)
        assert mgr.parse_token_usage("nonexistent") == (0, 0)


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
