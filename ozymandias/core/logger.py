"""
Session-based log files.

On startup, a new timestamped log file is created:
    logs/session_YYYY-MM-DDTHH-MM-SSZ.log

A ``current.log`` symlink in the same directory always points to the active
session file, so ``tail -f logs/current.log`` works across restarts.

All session files are kept — nothing is deleted automatically.  Use the
config key ``max_session_logs`` (default 0 = unlimited) to cap the number of
retained files; the oldest are removed when the cap is exceeded.

Format: ISO 8601 UTC timestamp | module | level | message
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
CURRENT_SYMLINK = LOG_DIR / "current.log"


class _UTCFormatter(logging.Formatter):
    """Custom formatter: ISO 8601 UTC timestamp, module name, level, message."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def format(self, record: logging.LogRecord) -> str:
        asctime = self.formatTime(record)
        return f"{asctime} | {record.name:<30} | {record.levelname:<8} | {record.getMessage()}"


def _session_log_path(target_dir: Path) -> Path:
    """Return a new timestamped session log path (does not create the file)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return target_dir / f"session_{ts}.log"


def _update_current_symlink(target_dir: Path, session_file: Path) -> None:
    """Point current.log symlink at *session_file* (relative target).

    Silently skips if the filesystem does not support symlinks.
    """
    link = target_dir / "current.log"
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        # Use the filename only so the symlink works regardless of cwd.
        os.symlink(session_file.name, link)
    except (OSError, NotImplementedError):
        pass


def _prune_old_sessions(target_dir: Path, max_keep: int) -> None:
    """Delete the oldest session_*.log files when the count exceeds *max_keep*.

    Does nothing when max_keep is 0 (unlimited).
    """
    if max_keep <= 0:
        return
    session_files = sorted(target_dir.glob("session_*.log"), key=lambda p: p.stat().st_mtime)
    excess = len(session_files) - max_keep
    if excess <= 0:
        return
    for f in session_files[:excess]:
        try:
            f.unlink()
        except OSError:
            pass


def setup_logging(
    log_dir: Optional[Path] = None,
    level: int = logging.DEBUG,
    max_session_logs: int = 0,
) -> logging.Logger:
    """
    Configure root logger with two handlers:
    - File handler  → logs/session_<timestamp>.log  (DEBUG+)
    - Stream handler → stdout  (INFO+)

    Creates a ``current.log`` symlink pointing at the new session file.
    Prunes old session files if *max_session_logs* > 0.

    Returns the root logger.
    """
    target_dir = Path(log_dir) if log_dir else LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    session_file = _session_log_path(target_dir)
    _update_current_symlink(target_dir, session_file)

    formatter = _UTCFormatter()

    # File handler — DEBUG and above → timestamped session file.
    # Opening the handler creates the file on disk; prune AFTER so the new
    # session counts toward the cap and the correct oldest file is removed.
    file_handler = logging.FileHandler(session_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    _prune_old_sessions(target_dir, max_session_logs)

    # Stream handler — INFO and above
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # Suppress third-party library debug noise
    for noisy_logger in ("yfinance", "urllib3", "urllib3.connectionpool",
                         "httpx", "httpcore", "alpaca_trade_api", "anthropic._base_client"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    root.info("Logging initialized. Session log: %s", session_file)
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call setup_logging() first."""
    return logging.getLogger(name)
