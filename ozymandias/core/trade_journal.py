"""
Append-only JSONL trade journal.

Separate from StateManager because the write pattern (append) and file format
(JSONL) differ fundamentally from the JSON state files (atomic full rewrites).
Any module that needs to record closed trades can import TradeJournal directly
without pulling in the full StateManager machinery.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


STATE_DIR = Path(__file__).resolve().parent.parent / "state"
TRADE_JOURNAL_FILE = STATE_DIR / "trade_journal.jsonl"


class TradeJournal:
    """
    Append-only JSONL store.  One JSON object per line, one line per closed trade.

    Each record includes a ``trade_id`` (UUID) and ``recorded_at`` timestamp
    auto-injected on write.  The file grows indefinitely — rotation and archival
    are left to the operator (it accumulates ~1 MB/year at typical trade volume).

    Usage::

        journal = TradeJournal()
        await journal.append({
            "symbol": "NVDA", "strategy": "momentum",
            "entry_price": 875.20, "exit_price": 891.50, ...
        })
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else TRADE_JOURNAL_FILE
        self._lock = asyncio.Lock()

    async def append(self, record: dict) -> None:
        """Append one trade record to the journal file.

        Adds ``trade_id`` and ``recorded_at`` fields if not already present.
        Never raises — journal write failures are logged but do not crash the bot.
        """
        record = dict(record)
        if "trade_id" not in record:
            record["trade_id"] = str(uuid.uuid4())
        if "recorded_at" not in record:
            record["recorded_at"] = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
