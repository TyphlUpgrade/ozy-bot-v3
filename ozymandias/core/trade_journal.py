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

    async def load_recent(self, n: int) -> list[dict]:
        """Return last *n* close records in reverse-chronological order (newest first).

        Includes records where ``record_type == "close"`` OR ``record_type`` is
        absent (pre-lifecycle records that predate this field).  Excludes records
        where ``entry_price == 0`` (ghost/phantom trades).

        Acquires ``self._lock`` to avoid races with concurrent ``append()`` calls.
        Returns an empty list if the journal file does not exist yet.
        """
        async with self._lock:
            if not self._path.exists():
                return []
            lines = self._path.read_text(encoding="utf-8").splitlines()

        records: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rt = rec.get("record_type")
            if rt is not None and rt != "close":
                continue
            if float(rec.get("entry_price", 0) or 0) == 0:
                continue
            records.append(rec)

        # Return last n records, newest first (reverse-chronological)
        return list(reversed(records[-n:]))

    async def compute_session_stats(self, min_trades: int = 3) -> dict:
        """Compute win-rate and averages over the last 20 close records.

        Returns ``{}`` if fewer than *min_trades* qualifying records exist.

        Computes:
        - ``total_trades`` — count of records used
        - ``win_rate_pct`` — integer percentage, trades where ``pnl_pct > 0``
        - ``short_win_rate_pct`` — same filter, ``direction == "short"`` only;
          key omitted when no short trades are present
        - ``avg_hold_min`` — mean of ``hold_duration_min`` rounded to int
        - ``avg_pnl_pct`` — mean ``pnl_pct`` rounded to 2dp
        - ``high_conviction_win_rate_pct`` — win rate for ``claude_conviction >= 0.75``;
          key omitted when fewer than 3 such trades exist

        Acquires ``self._lock`` to avoid races with concurrent ``append()`` calls.
        """
        recent = await self.load_recent(20)
        if len(recent) < min_trades:
            return {}

        total = len(recent)
        wins = sum(1 for r in recent if float(r.get("pnl_pct", 0) or 0) > 0)
        win_rate_pct = int(round(wins / total * 100))

        # avg_hold_min — skip records with missing / None hold_duration_min
        hold_values = [
            float(r["hold_duration_min"])
            for r in recent
            if r.get("hold_duration_min") is not None
        ]
        avg_hold_min = int(round(sum(hold_values) / len(hold_values))) if hold_values else 0

        pnl_values = [float(r.get("pnl_pct", 0) or 0) for r in recent]
        avg_pnl_pct = round(sum(pnl_values) / total, 2)

        stats: dict = {
            "total_trades": total,
            "win_rate_pct": win_rate_pct,
            "avg_hold_min": avg_hold_min,
            "avg_pnl_pct": avg_pnl_pct,
        }

        # shorts_entered — always present so Claude sees when it has never entered a short.
        # short_win_rate_pct — only included when short trades exist (no denominator when 0).
        shorts = [r for r in recent if r.get("direction") == "short"]
        stats["shorts_entered"] = len(shorts)
        if shorts:
            short_wins = sum(1 for r in shorts if float(r.get("pnl_pct", 0) or 0) > 0)
            stats["short_win_rate_pct"] = int(round(short_wins / len(shorts) * 100))

        # high_conviction_win_rate_pct — only include when >= 3 high-conviction trades
        high_conv = [
            r for r in recent
            if float(r.get("claude_conviction", 0) or 0) >= 0.75
        ]
        if len(high_conv) >= 3:
            hc_wins = sum(1 for r in high_conv if float(r.get("pnl_pct", 0) or 0) > 0)
            stats["high_conviction_win_rate_pct"] = int(round(hc_wins / len(high_conv) * 100))

        return stats

    async def append(self, record: dict) -> None:
        """Append one trade record to the journal file.

        Adds ``trade_id`` and ``recorded_at`` fields if not already present.
        Never raises — journal write failures are logged but do not crash the bot.
        """
        record = dict(record)
        if not record.get("trade_id"):
            # Generate a UUID when trade_id is absent or explicitly None.
            # None is passed by close/snapshot/review paths when the in-memory
            # trade_id was lost (e.g. bot restarted between open and close).
            record["trade_id"] = str(uuid.uuid4())
        if "recorded_at" not in record:
            record["recorded_at"] = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
