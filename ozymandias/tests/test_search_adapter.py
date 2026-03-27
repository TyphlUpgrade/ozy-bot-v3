"""
Tests for SearchAdapter — specifically the 429 retry logic.
"""
from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from ozymandias.data.adapters.search_adapter import SearchAdapter


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=None)


class TestSearchAdapter429Retry:
    def test_returns_empty_when_no_api_key(self):
        adapter = SearchAdapter(api_key=None)
        assert not adapter.enabled

    def test_retry_succeeds_after_one_429(self):
        """_fetch_with_retry retries once on 429 and returns results on second attempt."""
        call_count = 0

        def _fake_fetch(query, n_results):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _http_error(429)
            return [{"title": "T", "url": "http://x", "description": "D"}]

        adapter = SearchAdapter(api_key="key", retry_count=2, retry_sec=0.0)
        with patch.object(adapter, "_fetch", side_effect=_fake_fetch):
            result = adapter._fetch_with_retry("query", 5)

        assert call_count == 2
        assert len(result) == 1
        assert result[0]["title"] == "T"

    def test_raises_after_exhausting_retries(self):
        """_fetch_with_retry raises after retry_count+1 total attempts."""
        def _always_429(query, n_results):
            raise _http_error(429)

        adapter = SearchAdapter(api_key="key", retry_count=2, retry_sec=0.0)
        with patch.object(adapter, "_fetch", side_effect=_always_429):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                adapter._fetch_with_retry("query", 5)
        assert exc_info.value.code == 429

    def test_non_429_http_error_not_retried(self):
        """Non-429 HTTP errors are re-raised immediately without retry."""
        call_count = 0

        def _fake_fetch(query, n_results):
            nonlocal call_count
            call_count += 1
            raise _http_error(500)

        adapter = SearchAdapter(api_key="key", retry_count=2, retry_sec=0.0)
        with patch.object(adapter, "_fetch", side_effect=_fake_fetch):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                adapter._fetch_with_retry("query", 5)
        assert call_count == 1  # no retry on 500
        assert exc_info.value.code == 500

    @pytest.mark.asyncio
    async def test_search_returns_empty_after_all_429_retries(self):
        """search() returns [] (never raises) even when all retries are exhausted."""
        def _always_429(query, n_results):
            raise _http_error(429)

        adapter = SearchAdapter(api_key="key", retry_count=1, retry_sec=0.0)
        with patch.object(adapter, "_fetch", side_effect=_always_429):
            result = await adapter.search("query", n_results=5)
        assert result == []

    def test_retry_count_zero_means_no_retry(self):
        """retry_count=0 means try once — no retry at all."""
        call_count = 0

        def _fake_fetch(query, n_results):
            nonlocal call_count
            call_count += 1
            raise _http_error(429)

        adapter = SearchAdapter(api_key="key", retry_count=0, retry_sec=0.0)
        with patch.object(adapter, "_fetch", side_effect=_fake_fetch):
            with pytest.raises(urllib.error.HTTPError):
                adapter._fetch_with_retry("query", 5)
        assert call_count == 1
