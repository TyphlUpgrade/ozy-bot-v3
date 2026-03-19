"""
Dual-file log rotation.

On startup, current.log is renamed to previous.log (overwriting old previous),
then a fresh current.log is created.

Format: ISO 8601 UTC timestamp | module | level | message
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
CURRENT_LOG = LOG_DIR / "current.log"
PREVIOUS_LOG = LOG_DIR / "previous.log"


class _UTCFormatter(logging.Formatter):
    """Custom formatter: ISO 8601 UTC timestamp, module name, level, message."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def format(self, record: logging.LogRecord) -> str:
        asctime = self.formatTime(record)
        return f"{asctime} | {record.name:<30} | {record.levelname:<8} | {record.getMessage()}"


def setup_logging(
    log_dir: Optional[Path] = None,
    level: int = logging.DEBUG,
) -> logging.Logger:
    """
    Configure root logger with two handlers:
    - File handler → logs/current.log
    - Stream handler → stdout (INFO+)

    Rotates log files on every call (startup behavior).
    Returns the root logger.
    """
    target_dir = Path(log_dir) if log_dir else LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    current = target_dir / "current.log"
    previous = target_dir / "previous.log"

    # Rotate: current → previous
    if current.exists():
        os.replace(current, previous)

    formatter = _UTCFormatter()

    # File handler — DEBUG and above
    file_handler = logging.FileHandler(current, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Stream handler — INFO and above
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    # Remove any previously attached handlers to avoid duplication on repeated calls
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # Suppress third-party library debug noise — they inherit root DEBUG level
    for noisy_logger in ("yfinance", "urllib3", "urllib3.connectionpool",
                         "httpx", "httpcore", "alpaca_trade_api", "anthropic._base_client"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    root.info("Logging initialized. Log file: %s", current)
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call setup_logging() first."""
    return logging.getLogger(name)
