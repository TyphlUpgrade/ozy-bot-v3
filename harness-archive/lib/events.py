"""Append-only JSONL event log for audit trail and telemetry."""

from __future__ import annotations

import json
import logging
from datetime import datetime, UTC
from pathlib import Path

logger = logging.getLogger("harness.events")


class EventLog:
    """Append-only structured event logger.

    Writes one JSON object per line. Never read by the orchestrator —
    this is a telemetry sink for audit trails, cost tracking, and analytics.

    Event types emitted by the harness:
        task_activated, stage_advanced, task_completed, task_failed,
        escalation_created, escalation_resolved, session_launched,
        session_restarted, session_shutdown
    """

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    async def record(self, event_type: str, data: dict | None = None) -> None:
        """Append a structured event to the JSONL log."""
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event_type,
            **(data or {}),
        }
        try:
            line = json.dumps(event, default=str) + "\n"
            with open(self.log_path, "a") as f:
                f.write(line)
        except OSError as e:
            logger.warning("EventLog write failed: %s", e)
