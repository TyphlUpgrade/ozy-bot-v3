"""
Tests for core/logger.py
"""
from __future__ import annotations

import logging
import os
import re
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


# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------

class TestLogRotation:
    def test_creates_current_log_on_fresh_dir(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        assert (tmp_path / "current.log").exists()

    def test_no_previous_log_if_no_prior_current(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        # previous.log should not exist since there was no current.log before
        assert not (tmp_path / "previous.log").exists()

    def test_current_renamed_to_previous_on_startup(self, tmp_path: Path):
        # Create a "previous session" current.log with sentinel content
        current = tmp_path / "current.log"
        current.write_text("session one content\n")

        setup_logging(log_dir=tmp_path)

        previous = tmp_path / "previous.log"
        assert previous.exists(), "current.log should have been rotated to previous.log"
        assert "session one content" in previous.read_text()

    def test_previous_log_overwritten_on_second_rotation(self, tmp_path: Path):
        current = tmp_path / "current.log"
        previous = tmp_path / "previous.log"

        # First session
        current.write_text("session one\n")
        setup_logging(log_dir=tmp_path)
        assert "session one" in previous.read_text()

        # Reset handlers, write something to current
        logging.getLogger().handlers.clear()
        current.write_text("session two\n")

        # Second rotation — previous should now hold session two content
        setup_logging(log_dir=tmp_path)
        assert "session two" in previous.read_text()
        assert "session one" not in previous.read_text()

    def test_fresh_current_log_created_after_rotation(self, tmp_path: Path):
        current = tmp_path / "current.log"
        current.write_text("old session\n")
        setup_logging(log_dir=tmp_path)

        # current.log should now be a fresh file (not contain old session content)
        content = current.read_text()
        assert "old session" not in content


# ---------------------------------------------------------------------------
# Log format
# ---------------------------------------------------------------------------

class TestLogFormat:
    def test_log_format_contains_iso_utc_timestamp(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        logger = get_logger("test.module")
        logger.info("hello world")

        logging.getLogger().handlers[0].flush()
        content = (tmp_path / "current.log").read_text()

        # Expect: 2025-03-10T14:30:00.123Z | ...
        iso_pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
        assert re.search(iso_pattern, content), f"No ISO timestamp found in: {content!r}"

    def test_log_format_contains_module_name(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        logger = get_logger("ozymandias.core.orchestrator")
        logger.warning("test warning")

        logging.getLogger().handlers[0].flush()
        content = (tmp_path / "current.log").read_text()
        assert "ozymandias.core.orchestrator" in content

    def test_log_format_contains_level(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        logger = get_logger("test")
        logger.error("something bad")

        logging.getLogger().handlers[0].flush()
        content = (tmp_path / "current.log").read_text()
        assert "ERROR" in content

    def test_log_format_contains_message(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path)
        logger = get_logger("test")
        logger.info("trade executed for NVDA")

        logging.getLogger().handlers[0].flush()
        content = (tmp_path / "current.log").read_text()
        assert "trade executed for NVDA" in content

    def test_debug_messages_written_to_file(self, tmp_path: Path):
        setup_logging(log_dir=tmp_path, level=logging.DEBUG)
        logger = get_logger("test")
        logger.debug("debug detail")

        logging.getLogger().handlers[0].flush()
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

        logging.getLogger().handlers[0].flush()
        content = (tmp_path / "current.log").read_text()
        assert "from A" in content
        assert "from B" in content
