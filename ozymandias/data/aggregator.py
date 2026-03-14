"""
Data aggregator — routes market data requests across registered adapters.

For MVP, only yfinance is registered as the primary adapter. Additional
sources (Alpha Vantage, Finnhub, etc.) can be added as secondary adapters
later without changing any consumer code.

Fallback pattern: try primary adapter first; if it raises, try secondary.
If both fail, raise RuntimeError.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from ozymandias.data.adapters.base import DataAdapter, Fundamentals, Quote

log = logging.getLogger(__name__)


class DataAggregator:
    """
    Routes data requests to the primary adapter with optional fallback.

    Usage::

        adapter = YFinanceAdapter()
        aggregator = DataAggregator(primary=adapter)

        df    = await aggregator.get_bars("AAPL", "5m", "1d")
        quote = await aggregator.get_quote("AAPL")
        fund  = await aggregator.get_fundamentals("AAPL")
    """

    def __init__(
        self,
        primary: DataAdapter,
        secondary: Optional[DataAdapter] = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    async def get_bars(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        """
        Fetch OHLCV bars. Falls back to secondary adapter if primary fails.

        Args:
            symbol:   Ticker symbol, e.g. "AAPL"
            interval: Bar interval — "1m", "5m", "1h", "1d"
            period:   Lookback period — "1d", "5d", "1mo"

        Returns:
            DataFrame with columns: open, high, low, close, volume.

        Raises:
            RuntimeError: If all adapters fail.
        """
        try:
            return await self._primary.fetch_bars(symbol, interval, period)
        except Exception as exc:
            if self._secondary is not None:
                log.warning(
                    "Primary adapter failed for get_bars(%s): %s — trying secondary",
                    symbol, exc,
                )
            else:
                log.warning("Primary adapter failed for get_bars(%s): %s", symbol, exc)

        if self._secondary is not None:
            try:
                return await self._secondary.fetch_bars(symbol, interval, period)
            except Exception as exc:
                log.warning(
                    "Secondary adapter also failed for get_bars(%s): %s",
                    symbol, exc,
                )

        raise RuntimeError(f"All adapters failed to fetch bars for {symbol}")

    async def get_quote(self, symbol: str) -> Quote:
        """
        Fetch the latest quote. Falls back to secondary adapter if primary fails.

        Raises:
            RuntimeError: If all adapters fail.
        """
        try:
            return await self._primary.fetch_quote(symbol)
        except Exception as exc:
            if self._secondary is not None:
                log.warning(
                    "Primary adapter failed for get_quote(%s): %s — trying secondary",
                    symbol, exc,
                )
            else:
                log.warning("Primary adapter failed for get_quote(%s): %s", symbol, exc)

        if self._secondary is not None:
            try:
                return await self._secondary.fetch_quote(symbol)
            except Exception as exc:
                log.warning(
                    "Secondary adapter also failed for get_quote(%s): %s",
                    symbol, exc,
                )

        raise RuntimeError(f"All adapters failed to fetch quote for {symbol}")

    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        """
        Fetch fundamental data. Falls back to secondary adapter if primary fails.

        Raises:
            RuntimeError: If all adapters fail.
        """
        try:
            return await self._primary.fetch_fundamentals(symbol)
        except Exception as exc:
            if self._secondary is not None:
                log.warning(
                    "Primary adapter failed for get_fundamentals(%s): %s — trying secondary",
                    symbol, exc,
                )
            else:
                log.warning("Primary adapter failed for get_fundamentals(%s): %s", symbol, exc)

        if self._secondary is not None:
            try:
                return await self._secondary.fetch_fundamentals(symbol)
            except Exception as exc:
                log.warning(
                    "Secondary adapter also failed for get_fundamentals(%s): %s",
                    symbol, exc,
                )

        raise RuntimeError(f"All adapters failed to fetch fundamentals for {symbol}")
