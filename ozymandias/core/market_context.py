"""
MarketContextBuilder — assembles the market_data context dict for Claude reasoning calls.

Extracted from orchestrator.py to enable independent testing and parallel
development. The orchestrator delegates to this module via a thin wrapper.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ozymandias.core.config import Config
from ozymandias.core.market_hours import get_current_session
from ozymandias.intelligence.technical_analysis import compute_sector_dispersion

log = logging.getLogger(__name__)

# Sector ETF display names — maps ETF ticker to human-readable sector name.
_SECTOR_ETF_NAMES = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "XLY": "Consumer Discretionary",
    "XLC": "Communication Services",
    "ITA": "Aerospace & Defense",
    "XBI": "Biotechnology",
}


class MarketContextBuilder:
    """Builds the market_data context dict consumed by Claude reasoning calls.

    Pure read-only: assembles data from indicators, news, daily signals,
    and recommendation outcomes into a single dict. Never writes to any
    shared state.
    """

    def __init__(
        self,
        config: Config,
        context_symbols: list[str],
        sector_map: dict[str, str],
    ) -> None:
        self._config = config
        self._context_symbols = context_symbols
        self._sector_map = sector_map

    async def build(
        self,
        acct,
        pdt_remaining: int,
        *,
        market_context_indicators: dict,
        daily_indicators: dict,
        recommendation_outcomes: dict,
        state_manager,
        data_adapter,
    ) -> dict:
        """
        Build the market_data context dict for Claude reasoning calls.

        Derives macro trend summary from market_context_indicators (populated
        each medium cycle) and fetches tier-1 watchlist news concurrently.
        """
        ctx = market_context_indicators

        def _classify_trend(sym: str) -> str:
            """Map a context symbol's TA signals to a simple trend label."""
            signals = ctx.get(sym, {}).get("signals", {})
            ts   = signals.get("trend_structure", "")
            vwap = signals.get("vwap_position", "")
            if ts == "bullish_aligned" and vwap in ("above", "at"):
                return "bullish"
            if ts == "bearish_aligned" and vwap in ("below", "at"):
                return "bearish"
            if ts or vwap:
                return "mixed"
            return "unknown"

        spy_rsi = ctx.get("SPY", {}).get("signals", {}).get("rsi")

        bullish_count = sum(
            1 for sym in self._context_symbols
            if ctx.get(sym, {}).get("signals", {}).get("trend_structure") == "bullish_aligned"
        )
        market_breadth = f"{bullish_count}/{len(self._context_symbols)} context instruments bullish-aligned"

        sector_performance = []
        for etf, sector in _SECTOR_ETF_NAMES.items():
            ind = ctx.get(etf)
            if not ind:
                continue
            # Sort sectors by best directional score — max(long_score, short_score) gives
            # a regime-neutral strength signal for ranking sectors without direction bias.
            _sort_score = max(ind.get("long_score", 0.0), ind.get("short_score", 0.0))
            sector_performance.append({
                "sector":          sector,
                "etf":             etf,
                "trend":           ind.get("signals", {}).get("trend_structure", "unknown"),
                "long_score":      round(ind.get("long_score",  0.0), 3),
                "short_score":     round(ind.get("short_score", 0.0), 3),
                "_sort_score":     _sort_score,
            })
        sector_performance.sort(key=lambda x: x.pop("_sort_score"), reverse=True)

        # Fetch news for tier-1 watchlist symbols + macro instruments (SPY, QQQ) concurrently.
        # Macro news gives Sonnet narrative context for *why* broad market indicators are moving
        # (tariff shocks, Fed surprises, geopolitical events) — the per-symbol feed won't carry this.
        watchlist = await state_manager.load_watchlist()
        tier1 = [e.symbol for e in watchlist.entries if e.priority_tier == 1]
        max_items = self._config.claude.news_max_items_per_symbol
        max_age = self._config.claude.news_max_age_hours
        macro_news_items = self._config.claude.macro_news_max_items  # tighter cap for broad-market symbols
        _MACRO_NEWS_SYMBOLS = ["SPY", "QQQ"]
        all_fetch_syms = tier1 + _MACRO_NEWS_SYMBOLS
        all_news_results = await asyncio.gather(
            *[
                data_adapter.fetch_news(
                    s,
                    max_items=(macro_news_items if s in _MACRO_NEWS_SYMBOLS else max_items),
                    max_age_hours=max_age,
                )
                for s in all_fetch_syms
            ],
            return_exceptions=True,
        )
        watchlist_news: dict[str, list] = {}
        macro_news: dict[str, list] = {}
        for sym, result in zip(all_fetch_syms, all_news_results):
            if isinstance(result, Exception):
                continue
            if not result:
                continue
            if sym in _MACRO_NEWS_SYMBOLS:
                macro_news[sym] = result
            else:
                watchlist_news[sym] = result

        market_ctx: dict = {
            "spy_trend":         _classify_trend("SPY"),
            "spy_rsi":           spy_rsi,
            "qqq_trend":         _classify_trend("QQQ"),
            "market_breadth":    market_breadth,
            "sector_performance": sector_performance,
            "macro_news":        macro_news,
            "watchlist_news":    watchlist_news,
            "trading_session":   get_current_session().value,
            "pdt_trades_remaining": max(0, pdt_remaining),
            "account_equity":    acct.equity,
            "buying_power":      acct.buying_power,
            # Active strategies from config — Claude must only recommend strategies in this list.
            "active_strategies": self._config.strategy.active_strategies,
        }

        # Daily-bar macro regime context — added when daily_indicators is populated.
        # These are daily signals for SPY/QQQ; useful for multi-day swing thesis evaluation.
        # spy_rsi above remains the intraday 5-min signal; spy_daily.rsi_14d is the daily view.
        _spy_daily = daily_indicators.get("SPY")
        if _spy_daily:
            market_ctx["spy_daily"] = {
                "rsi_14d":       _spy_daily.get("rsi_14d"),
                "daily_trend":   _spy_daily.get("daily_trend"),
                "roc_5d":        _spy_daily.get("roc_5d"),
                "ema20_vs_ema50": _spy_daily.get("ema20_vs_ema50"),
            }
        _qqq_daily = daily_indicators.get("QQQ")
        if _qqq_daily:
            market_ctx["qqq_daily"] = {
                "rsi_14d":     _qqq_daily.get("rsi_14d"),
                "daily_trend": _qqq_daily.get("daily_trend"),
            }

        # Phase 19: sector_dispersion — relative performance of watchlist symbols vs sector ETF
        _sector_dispersion = compute_sector_dispersion(
            watchlist.entries,
            self._sector_map,
            daily_indicators,
        )
        if _sector_dispersion:
            market_ctx["sector_dispersion"] = _sector_dispersion

        # Phase 19: recent_rejections — per-symbol hard-filter failure counts from this session
        # Shows Claude which candidates the quant system keeps blocking and why.
        _rejections = [
            {
                "symbol": sym,
                "reason": data["stage_detail"],
                "cycles_rejected": data["rejection_count"],
                **({"strategy": data["strategy"]} if data.get("strategy") else {}),
            }
            for sym, data in recommendation_outcomes.items()
            if data.get("rejection_count", 0) >= 1 and data.get("stage_detail")
        ]
        if _rejections:
            market_ctx["recent_rejections"] = sorted(
                _rejections, key=lambda x: x["cycles_rejected"], reverse=True
            )[:10]

        return market_ctx
