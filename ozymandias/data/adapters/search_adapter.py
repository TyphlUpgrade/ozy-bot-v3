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
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class SearchAdapter:
    """
    Thin async wrapper around the Brave Search API.

    Returns [] on any failure so Claude reasoning degrades gracefully when
    search is unavailable (no API key, network error, rate limit, etc.).
    """

    def __init__(self, api_key: str | None) -> None:
        self._api_key = api_key or None
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
        """
        if not self._api_key:
            return []
        try:
            import asyncio
            results = await asyncio.to_thread(self._fetch, query, n_results)
            return results
        except Exception as exc:
            log.warning("Brave Search failed for query %r — %s", query, exc)
            return []

    def _fetch(self, query: str, n_results: int) -> list[dict]:
        """Synchronous HTTP fetch — called via asyncio.to_thread."""
        params = urllib.parse.urlencode({"q": query, "count": min(n_results, 20)})
        url = f"{_BRAVE_SEARCH_URL}?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
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
