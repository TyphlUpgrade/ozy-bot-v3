"""
Tests for core/logger.py — session-based log files.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pytest

from ozymandias.core.logger import get_logger, setup_logging


@pytest.fixture(autouse=True)
def reset_root_logger():
    """Ensure root logger is clean before and after each test."""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.level = original_level


def _session_files(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("session_*.log"))


def _flush_file_handler():
    """Flush the first handler on the root logger (the file handler)."""
    handlers = logging.getLogger().handlers
    if handlers:
        handlers[0].flush()


# ---------------------------------------------------------------------------
# Session file creation
# ---------------------------------------------------------------------------

class TestSessionFileCreation:
    def test_creates_session_log_on_startup(self, tmp_path: Path):
        """A session_*.log file is created on every call to setup_logging."""
        setup_logging(log_dir=tmp_path)
        files = _session_files(tmp_path)
        assert len(files) == 1
        assert files[0].name.startswith("session_")
        assert files[0].name.endswith(".log")

    def test_session_filename_contains_timestamp(self, tmp_path: Path):
        """Session filename encodes the UTC start time."""
        setup_logging(log_dir=tmp_path)
        name = _session_files(tmp_path)[0].name
        # e.g. session_2026-03-20T14-30-00Z.log
        assert re.match(r"session_\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.log", name), (
            f"Unexpected filename format: {name!r}"
        )

    def test_each_startup_creates_new_session_file(self, tmp_path: Path):
        """Two separate startups → two distinct session files."""
        setup_logging(log_dir=tmp_path)
        logging.getLogger().handlers.clear()
        time.sleep(1.1)  # ensure the second timestamp differs by at least 1 second
        setup_logging(log_dir=tmp_path)

        files = _session_files(tmp_path)
        assert len(files) == 2
        assert files[0].name != files[1].name

    def test_old_session_files_are_kept_by_default(self, tmp_path: Path):
        """All session files accumulate when max_session_logs=0 (unlimited)."""
        for _ in range(3):
            setup_logging(log_dir=tmp_path)
            logging.getLogger().handlers.clear()
            time.sleep(1.1)

        assert len(_session_files(tmp_path)) == 3

    def test_no_previous_log_created(self, tmp_path: Path):
        """The old previous.log rotation file is no longer created."""
        setup_logging(log_dir=tmp_path)
        assert not (tmp_path / "previous.log").exists()


# ---------------------------------------------------------------------------
# current.log symlink
# ---------------------------------------------------------------------------

class TestCurrentLogSymlink:
    def test_current_log_symlink_created(self, tmp_path: Path):
        """current.log symlink is created pointing at the session file."""
        setup_logging(log_dir=tmp_path)
        link = tmp_path / "current.log"
        assert link.exists(), "current.log symlink not found"

    def test_current_log_resolves_to_session_file(self, tmp_path: Path):
        """current.log symlink resolves to the active session_*.log file."""
        setup_logging(log_dir=tmp_path)
        link = tmp_path / "current.log"
        session = _session_files(tmp_path)[0]
        assert link.resolve() == session.resolve()

    def test_current_log_symlink_updated_on_restart(self, tmp_path: Path):
        """After a second startup current.log points at the new session file."""
        setup_logging(log_dir=tmp_path)
        logging.getLogger().handlers.clear()
        first_session = _session_files(tmp_path)[0]

        time.sleep(1.1)
        setup_logging(log_dir=tmp_path)
        second_session = _session_files(tmp_path)[1]

        link = tmp_path / "current.log"
        assert link.resolve() == second_session.resolve()
        assert link.resolve() != first_session.resolve()

    def test_current_log_readable_via_symlink(self, tmp_path: Path):
        """Content written to the session file is readable via the symlink."""
        setup_logging(log_dir=tmp_path)
        get_logger("test.symlink").info("symlink read test")
        _flush_file_handler()
        content = (tmp_path / "current.log").read_text()
        assert "symlink read test" in content


# ---------------------------------------------------------------------------
# Session pruning
# ---------------------------------------------------------------------------

class TestSessionPruning:
    def test_oldest_pruned_when_cap_exceeded(self, tmp_path: Path):
        """With max_session_logs=2, the third startup removes the oldest file."""
        setup_logging(log_dir=tmp_path, max_session_logs=2)
        logging.getLogger().handlers.clear()
        time.sleep(1.1)
        first = _session_files(tmp_path)[0]

        setup_logging(log_dir=tmp_path, max_session_logs=2)
        logging.getLogger().handlers.clear()
        time.sleep(1.1)

        setup_logging(log_dir=tmp_path, max_session_logs=2)

        remaining = _session_files(tmp_path)
        assert len(remaining) == 2
        assert first not in remaining, "Oldest session file should have been pruned"

    def test_no_pruning_when_under_cap(self, tmp_path: Path):
        """With max_session_logs=5, three startups keep all three files."""
        for _ in range(3):
            setup_logging(log_dir=tmp_path, max_session_logs=5)
            logging.getLogger().handlers.clear()
            time.sleep(1.1)

        assert len(_session_files(tmp_path)) == 3

    def test_zero_cap_means_unlimited(self, tmp_path: Path):
        """max_session_logs=0 (default) never prunes."""
        for _ in range(4):
            setup_logging(log_dir=tmp_path, max_session_logs=0)
            logging.getLogger().handlers.clear()
            time.sleep(1.1)

        assert len(_session_files(tmp_path)) == 4


# ---------------------------------------------------------------------------
# Log format
# ---------------------------------------------------------------------------

class TestLogFormat:
    def test_log_format_contains_iso_utc_timestamp(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        get_logger("test.module").info("hello world")
        _flush_file_handler()
        content = (tmp_path / "current.log").read_text()
        iso_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
        assert re.search(iso_pattern, content), f"No ISO timestamp found in: {content!r}"

    def test_log_format_contains_module_name(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        get_logger("ozymandias.core.orchestrator").warning("test warning")
        _flush_file_handler()
        content = (tmp_path / "current.log").read_text()
        assert "ozymandias.core.orchestrator" in content

    def test_log_format_contains_level(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        get_logger("test").error("something bad")
        _flush_file_handler()
        content = (tmp_path / "current.log").read_text()
        assert "ERROR" in content

    def test_log_format_contains_message(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        get_logger("test").info("trade executed for NVDA")
        _flush_file_handler()
        content = (tmp_path / "current.log").read_text()
        assert "trade executed for NVDA" in content

    def test_debug_messages_written_to_file(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path, level=logging.DEBUG)
        get_logger("test").debug("debug detail")
        _flush_file_handler()
        content = (tmp_path / "current.log").read_text()
        assert "debug detail" in content


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------

class TestGetLogger:
    def test_get_logger_returns_named_logger(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        logger = get_logger("my.module")
        assert logger.name == "my.module"

    def test_multiple_loggers_write_to_same_file(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        get_logger("mod.a").info("from A")
        get_logger("mod.b").info("from B")
        _flush_file_handler()
        content = (tmp_path / "current.log").read_text()
        assert "from A" in content
        assert "from B" in content
