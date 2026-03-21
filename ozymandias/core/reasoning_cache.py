"""
Claude reasoning response cache.

Cache design:
- Files: reasoning_cache/reasoning_{timestamp_utc}.json
- Retention: current session + previous session (by date). Older files deleted on startup.
- Startup reuse: if a cached response from today and < 60 minutes old exists, return it.
- Max 30 files per session; oldest deleted if exceeded.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo


CACHE_DIR = Path(__file__).resolve().parent.parent / "reasoning_cache"
MAX_FILES_PER_SESSION = 30
REUSE_MAX_AGE_MINUTES = 60
_ET = ZoneInfo("America/New_York")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _session_date(dt: datetime) -> str:
    """Return session date string (YYYY-MM-DD ET) for cache rotation."""
    return dt.astimezone(_ET).strftime("%Y-%m-%d")


def _parse_timestamp_from_name(path: Path) -> Optional[datetime]:
    """Extract UTC timestamp from a cache filename."""
    # Expected: reasoning_2025-03-11T14-30-00Z.json
    stem = path.stem  # reasoning_2025-03-11T14-30-00Z
    try:
        ts_str = stem[len("reasoning_"):]
        # normalise dashes in time portion back to colons
        # format stored: YYYY-MM-DDTHH-MM-SSZ
        date_part, rest = ts_str.split("T", 1)
        time_part = rest.rstrip("Z").replace("-", ":")
        iso = f"{date_part}T{time_part}+00:00"
        return datetime.fromisoformat(iso)
    except (ValueError, IndexError):
        return None


def _make_filename(dt: datetime) -> str:
    ts = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"reasoning_{ts}.json"


class ReasoningCache:
    """
    Manages the reasoning_cache/ directory lifecycle.

    Usage::

        cache = ReasoningCache()
        cache.rotate()                          # call once on startup
        recent = cache.load_latest_if_fresh()   # returns dict or None
        cache.save(trigger, input_ctx, raw_response, parsed_response, tokens)
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self._dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session_date = _session_date(_utcnow())

    # ------------------------------------------------------------------
    # Startup rotation
    # ------------------------------------------------------------------

    def rotate(self) -> int:
        """
        Delete cache files from sessions older than the previous session.

        Returns the number of files deleted.
        """
        now = _utcnow()
        today = _session_date(now)
        # Keep today and yesterday
        yesterday = _session_date(datetime.fromtimestamp(
            now.timestamp() - 86400, tz=timezone.utc
        ))
        keep_dates = {today, yesterday}

        deleted = 0
        for path in sorted(self._dir.glob("reasoning_*.json")):
            ts = _parse_timestamp_from_name(path)
            if ts is None:
                continue
            file_date = _session_date(ts)
            if file_date not in keep_dates:
                path.unlink(missing_ok=True)
                deleted += 1
        return deleted

    # ------------------------------------------------------------------
    # Startup reuse
    # ------------------------------------------------------------------

    def load_latest_if_fresh(self, max_age_min: int | None = None) -> Optional[dict]:
        """
        Return the most recent cached response if it is from today and
        less than the configured max age (minutes) old. Returns None otherwise.

        Parameters
        ----------
        max_age_min:
            Override the default REUSE_MAX_AGE_MINUTES for this call.
            Used by the adaptive cache TTL (Fix 4 / Phase 17) to shorten
            the reuse window during high-stress or panic market regimes.
            When None, uses REUSE_MAX_AGE_MINUTES (60 min).
        """
        now = _utcnow()
        today = _session_date(now)
        effective_max = max_age_min if max_age_min is not None else REUSE_MAX_AGE_MINUTES
        candidates = []

        for path in self._dir.glob("reasoning_*.json"):
            ts = _parse_timestamp_from_name(path)
            if ts is None:
                continue
            if _session_date(ts) != today:
                continue
            age_minutes = (now - ts).total_seconds() / 60
            if age_minutes <= effective_max:
                candidates.append((ts, path))

        if not candidates:
            return None

        # Take the most recent
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, latest_path = candidates[0]
        try:
            with open(latest_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(
        self,
        trigger: str,
        input_context: Any,
        raw_response: str,
        parsed_response: Optional[dict],
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Path:
        """
        Persist a Claude response to cache.

        If the session now has > MAX_FILES_PER_SESSION files, the oldest
        file from the current session is deleted before writing.

        Returns the path of the written file.
        """
        now = _utcnow()
        today = _session_date(now)

        # Enforce max-files limit within the current session
        session_files = sorted(
            [
                (p, _parse_timestamp_from_name(p))
                for p in self._dir.glob("reasoning_*.json")
                if _parse_timestamp_from_name(p) is not None
                and _session_date(_parse_timestamp_from_name(p)) == today  # type: ignore[arg-type]
            ],
            key=lambda x: x[1],  # type: ignore[return-value]
        )
        while len(session_files) >= MAX_FILES_PER_SESSION:
            oldest_path, _ = session_files.pop(0)
            oldest_path.unlink(missing_ok=True)

        # Build context hash for deduplication / debugging
        context_str = json.dumps(input_context, default=str, sort_keys=True)
        context_hash = "sha256:" + hashlib.sha256(context_str.encode()).hexdigest()[:16]

        record = {
            "timestamp": now.isoformat(),
            "trigger": trigger,
            "session_id": today,
            "input_context_hash": context_hash,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "raw_response": raw_response,
            "parsed_response": parsed_response,
            "parse_success": parsed_response is not None,
        }

        filename = _make_filename(now)
        out_path = self._dir / filename
        # Use atomic write pattern
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, default=str)
            os.replace(tmp, out_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        return out_path
