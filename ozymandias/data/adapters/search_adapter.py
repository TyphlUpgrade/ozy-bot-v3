"""
Brave Search adapter for Claude tool use.

Wraps the Brave Search API for web search queries. When no API key is
configured the adapter is silently disabled — all calls return [].

API key: injected via os.environ["BRAVE_SEARCH_API_KEY"], which is set from
the credentials file in _load_credentials (same pattern as ANTHROPIC_API_KEY).

Extension point: to add a second search provider, implement the same interface
(search(query, n_results) -> list[dict]) and swap it in SearchAdapter.__init__.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class SearchAdapter:
    """
    Thin async wrapper around the Brave Search API.

    Returns [] on any failure so Claude reasoning degrades gracefully when
    search is unavailable (no API key, network error, rate limit, etc.).

    429 rate-limit responses are retried with a short sleep (configurable via
    ``retry_count`` and ``retry_sec``). The 3-round cap in call_claude_with_tools
    remains the hard ceiling on total tool-use rounds regardless of retries here.
    """

    def __init__(
        self,
        api_key: str | None,
        retry_count: int = 2,
        retry_sec: float = 5.0,
    ) -> None:
        self._api_key = api_key or None
        self._retry_count = retry_count
        self._retry_sec = retry_sec
        if not self._api_key:
            log.info("Brave Search not configured — watchlist build will use screener data only.")

    @property
    def enabled(self) -> bool:
        """True when an API key is configured."""
        return bool(self._api_key)

    async def search(self, query: str, n_results: int = 5) -> list[dict]:
        """
        Search the web and return a list of {title, url, description} dicts.
        Returns [] if disabled (no API key) or on any exception.

        Retries up to ``retry_count`` times on 429 rate-limit responses,
        sleeping ``retry_sec`` between attempts.
        """
        if not self._api_key:
            return []
        try:
            import asyncio
            results = await asyncio.to_thread(self._fetch_with_retry, query, n_results)
            return results
        except Exception as exc:
            log.warning("Brave Search failed for query %r — %s", query, exc)
            return []

    def _fetch_with_retry(self, query: str, n_results: int) -> list[dict]:
        """Synchronous HTTP fetch with 429 retry — called via asyncio.to_thread."""
        for attempt in range(self._retry_count + 1):
            try:
                return self._fetch(query, n_results)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self._retry_count:
                    log.warning(
                        "Brave Search 429 for query %r (attempt %d/%d) — retrying in %.0fs",
                        query, attempt + 1, self._retry_count + 1, self._retry_sec,
                    )
                    time.sleep(self._retry_sec)
                else:
                    raise
        return []  # unreachable, satisfies type checker

    def _fetch(self, query: str, n_results: int) -> list[dict]:
        """Single synchronous HTTP fetch — raises on any HTTP error."""
        params = urllib.parse.urlencode({"q": query, "count": min(n_results, 20)})
        url = f"{_BRAVE_SEARCH_URL}?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self._api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        web_results = data.get("web", {}).get("results", [])
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
            }
            for r in web_results[:n_results]
        ]
