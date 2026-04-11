# discord presence test
"""Signal file I/O and dataclass schemas for inter-agent communication."""
# v5 pipeline signal I/O module

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC
from pathlib import Path

logger = logging.getLogger("harness.signals")

_SAFE_ID_RE = re.compile(r"[a-zA-Z0-9_\-]+")


def _safe_task_id(task_id: str) -> str:
    """Validate task_id is a safe filename component (no path traversal)."""
    if not _SAFE_ID_RE.fullmatch(task_id):
        raise ValueError(f"Invalid task_id: {task_id!r}")
    return task_id


# ---------- Signal Schemas ----------


@dataclass
class EscalationRequest:
    task_id: str
    agent: str                          # who is escalating
    stage: str                          # pipeline stage when escalation occurred
    severity: str                       # "blocking" | "advisory" | "informational"
    category: str                       # routing key for tiered escalation
    question: str
    options: list[str]
    context: str
    retry_count: int = 0                # for persistent_failure routing threshold
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class ArchitectResolution:
    task_id: str
    resolution: str                     # chosen option or "cannot_resolve"
    reasoning: str
    confidence: str                     # "high" | "low" — low auto-promotes to Tier 2
    resolved_by: str = "architect"
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class TaskSignal:
    """Inbound task from Discord or bot agents."""
    task_id: str
    description: str
    source: str = "discord"             # "discord" | "ops_monitor" | "manual"
    priority: str = "normal"            # "normal" | "high" | "low"
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------- Signal Reader ----------


class SignalReader:
    """Polls signal directories for new files."""

    def __init__(self, signal_dir: Path):
        self.signal_dir = signal_dir
        self._processed: set[str] = set()

    async def next_task(self, task_dir: Path) -> TaskSignal | None:
        """Return the oldest unprocessed task signal, or None."""
        if not task_dir.exists():
            return None
        # TODO(Phase 3): sort by TaskSignal.priority, not just mtime.
        # Priority field is parsed but unused — FIFO by arrival time is current behavior.
        files = sorted(task_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for f in files:
            if f.name in self._processed:
                continue
            try:
                data = json.loads(f.read_text())
                self._processed.add(f.name)
                task = TaskSignal(**data)
                _safe_task_id(task.task_id)
                return task
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning("Bad task signal %s: %s", f.name, e)
                self._processed.add(f.name)
        return None

    async def read_escalation(self, task_id: str) -> EscalationRequest | None:
        """Read an escalation request for a given task."""
        _safe_task_id(task_id)
        esc_dir = self.signal_dir / "escalation"
        path = esc_dir / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return EscalationRequest(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Bad escalation signal %s: %s", path, e)
            return None

    async def read_architect_resolution(self, task_id: str) -> ArchitectResolution | None:
        """Read an architect's resolution for an escalation."""
        _safe_task_id(task_id)
        res_dir = self.signal_dir / "escalation_resolution"
        path = res_dir / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return ArchitectResolution(**data)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Bad resolution signal %s: %s", path, e)
            return None

    async def check_stage_complete(self, stage: str, task_id: str) -> dict | None:
        """Check if a stage has a completion signal. Returns the signal data or None."""
        _safe_task_id(task_id)
        patterns = {
            "architect": self.signal_dir / "architect" / task_id / "plan.json",
            "executor": self.signal_dir / "executor" / f"completion-{task_id}.json",
            "reviewer": self.signal_dir / "reviewer" / task_id / "verdict.json",
        }
        path = patterns.get(stage)
        if path is None or not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning("Bad stage signal %s: %s", path, e)
            return None

    def clear_stage_signal(self, stage: str, task_id: str) -> None:
        """Remove a stage completion signal for a task.

        Prevents stale signals from causing spurious advancement after
        escalation→shelve→unshelve→resume cycles.
        """
        _safe_task_id(task_id)
        patterns = {
            "architect": self.signal_dir / "architect" / task_id / "plan.json",
            "executor": self.signal_dir / "executor" / f"completion-{task_id}.json",
            "reviewer": self.signal_dir / "reviewer" / task_id / "verdict.json",
        }
        path = patterns.get(stage)
        if path is not None and path.exists():
            path.unlink()
            logger.info("Cleared stale %s signal for %s", stage, task_id)

    def clear_escalation(self, task_id: str) -> None:
        """Remove processed escalation and resolution signals for a task."""
        _safe_task_id(task_id)
        for subdir in ("escalation", "escalation_resolution"):
            path = self.signal_dir / subdir / f"{task_id}.json"
            if path.exists():
                path.unlink()
                logger.info("Cleared %s signal for %s", subdir, task_id)

    def archive(self, task_id: str, archive_dir: Path) -> None:
        """Move processed signal files to archive directory."""
        _safe_task_id(task_id)
        archive_dir.mkdir(parents=True, exist_ok=True)
        for subdir in ("architect", "executor", "reviewer", "escalation", "escalation_resolution"):
            src_dir = self.signal_dir / subdir
            if not src_dir.exists():
                continue
            for f in src_dir.glob(f"*{task_id}*"):
                dest = archive_dir / subdir / f.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                f.rename(dest)
                logger.info("Archived %s → %s", f, dest)


def write_signal(directory: Path, filename: str, data: object) -> Path:
    """Atomically write a signal file (write temp, then rename)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(data) if hasattr(data, '__dataclass_fields__') else data, indent=2))
    tmp.rename(path)
    return path
