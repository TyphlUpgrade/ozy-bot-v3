"""
WatchlistManager — watchlist lifecycle: build, prune, regime-reset, apply changes.

Extracted from orchestrator.py to enable independent testing and parallel
development. The orchestrator delegates to this module via thin wrappers.

Uses mutable shared references (Python dicts passed by reference) for
state that the orchestrator's other loops also read/write. This is safe
because all loops run in a single asyncio event loop (no concurrent mutation).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from ozymandias.core.config import Config
from ozymandias.core.state_manager import WatchlistEntry
from ozymandias.core.trigger_engine import SlowLoopTriggerState

log = logging.getLogger(__name__)


class WatchlistManager:
    """Manages watchlist lifecycle: builds, prunes, regime-driven resets.

    Owns the build guard flags and universe scan cache. All other state
    is accessed via mutable shared references passed by the orchestrator.
    """

    def __init__(
        self,
        config: Config,
        state_manager,
        claude_engine,
        universe_scanner,
        search_adapter,
        trigger_state: SlowLoopTriggerState,
        sector_map: dict[str, str],
        # Mutable shared state
        filter_suppressed: dict,
        latest_indicators: dict,
    ) -> None:
        self._config = config
        self._state_manager = state_manager
        self._claude = claude_engine
        self._universe_scanner = universe_scanner
        self._search_adapter = search_adapter
        self._trigger_state = trigger_state
        self._sector_map = sector_map
        self._filter_suppressed = filter_suppressed
        self._latest_indicators = latest_indicators

        # Build guard flags (owned by this module)
        self.build_in_flight: bool = False
        self.reasoning_needed_after_build: bool = False
        self.last_universe_scan: list[dict] = []
        self.last_universe_scan_time: float = 0.0

    def clear_directional_suppression(self, affected_sectors: set[str] | None) -> None:
        """Clear direction-dependent session suppressions on regime reset.

        Called when a regime change causes a watchlist rebuild so that symbols
        suppressed under the old regime's direction can be reconsidered under
        the new regime's direction.

        Args:
            affected_sectors: ETF keys whose regime changed. Pass None to clear
                              all sectors (broad panic flip).

        Direction-dependent reasons (cleared): rvol, directional_score, conviction_floor,
                                                defer_expired
        Direction-neutral reasons (preserved): fetch_failure, blacklist, no_entry
        """
        _DIRECTION_DEPENDENT_PATTERNS = (
            "rvol", "directional_score", "conviction_floor", "defer_expired",
        )

        to_clear: list[str] = []
        for sym, reason in list(self._filter_suppressed.items()):
            if not any(pat in reason.lower() for pat in _DIRECTION_DEPENDENT_PATTERNS):
                continue
            if affected_sectors is not None:
                etf = self._sector_map.get(sym)
                if etf not in affected_sectors:
                    continue
            to_clear.append(sym)

        for sym in to_clear:
            del self._filter_suppressed[sym]

        if to_clear:
            log.info(
                "Regime reset: cleared %d directional suppression(s): %s",
                len(to_clear), to_clear,
            )

    async def regime_reset_build(
        self,
        prev_sector_regimes: dict | None,
        new_sector_regimes: dict | None,
        new_regime: str,
        changed_sectors: set[str],
        broad_regime_changed: bool,
        *,
        latest_market_context: dict,
        last_sector_regimes: dict | None,
    ) -> None:
        """Evict direction-conflicting watchlist entries and rebuild for the new regime.

        Called (fire-and-forget) after a regime or sector-regime change is detected.
        Does not block the reasoning cycle that triggered it — runs as a background task.

        latest_market_context and last_sector_regimes are passed at call time because
        they are orchestrator scalars that get reassigned.
        """
        try:
            watchlist = await self._state_manager.load_watchlist()
            portfolio = await self._state_manager.load_portfolio()
            open_syms = {p.symbol for p in portfolio.positions}

            broad_panic = new_regime == "risk-off panic"

            def _direction_summary(entries) -> str:
                longs  = sum(1 for e in entries if e.expected_direction == "long")
                shorts = sum(1 for e in entries if e.expected_direction == "short")
                either = sum(1 for e in entries if e.expected_direction == "either")
                return f"{len(entries)} total — {longs}L / {shorts}S / {either}either"

            log.info(
                "Regime reset triggered: %s → %s | changed_sectors=%s | watchlist before: %s",
                new_regime, "broad_panic" if broad_panic else "sector_granular",
                sorted(changed_sectors) if changed_sectors else "[]",
                _direction_summary(watchlist.entries),
            )

            to_evict: list[str] = []
            for e in watchlist.entries:
                if e.symbol in open_syms:
                    continue
                if broad_panic:
                    if e.expected_direction == "long":
                        to_evict.append(e.symbol)
                elif new_sector_regimes:
                    etf = self._sector_map.get(e.symbol)
                    if etf not in changed_sectors:
                        continue
                    sector_info = new_sector_regimes.get(etf, {})
                    sector_regime = sector_info.get("regime", "neutral")
                    if e.expected_direction == "long" and sector_regime in ("correcting", "downtrend"):
                        to_evict.append(e.symbol)
                    elif e.expected_direction == "short" and sector_regime in ("breaking_out", "uptrend"):
                        to_evict.append(e.symbol)

            if to_evict:
                evict_set = set(to_evict)
                watchlist.entries = [e for e in watchlist.entries if e.symbol not in evict_set]
                log.info(
                    "Regime reset: evicted %d conflicting watchlist entries: %s",
                    len(to_evict), to_evict,
                )
                await self._state_manager.save_watchlist(watchlist)

            if broad_panic:
                self.clear_directional_suppression(None)
            elif changed_sectors:
                self.clear_directional_suppression(changed_sectors)

            market_data = latest_market_context or {}
            if self.last_universe_scan:
                _n = self._config.universe_scanner.max_candidates_to_claude
                _candidates = self.last_universe_scan[:_n]
            else:
                _candidates = None

            try:
                wl_result = await self._claude.run_watchlist_build(
                    market_context=market_data,
                    current_watchlist=watchlist,
                    target_count=self._config.claude.watchlist_build_target,
                    candidates=_candidates,
                    search_adapter=self._search_adapter,
                    no_entry_symbols=self._config.ranker.no_entry_symbols,
                )
                if wl_result is not None:
                    symbols_before = {e.symbol for e in watchlist.entries}
                    await self.apply_watchlist_changes(
                        watchlist, wl_result.watchlist, wl_result.removes, open_syms,
                        last_sector_regimes=last_sector_regimes,
                    )
                    added = [e for e in watchlist.entries if e.symbol not in symbols_before]
                    added_longs  = [e.symbol for e in added if e.expected_direction == "long"]
                    added_shorts = [e.symbol for e in added if e.expected_direction == "short"]
                    added_either = [e.symbol for e in added if e.expected_direction == "either"]
                    log.info(
                        "Regime reset build complete — %d added (L:%s S:%s either:%s) | watchlist after: %s",
                        len(added),
                        added_longs or "[]",
                        added_shorts or "[]",
                        added_either or "[]",
                        _direction_summary(watchlist.entries),
                    )
                    self._trigger_state.last_watchlist_build_utc = datetime.now(timezone.utc)
            except Exception as exc:
                log.error("Regime reset watchlist build failed: %s", exc, exc_info=True)

        except Exception as exc:
            log.error("_regime_reset_build failed: %s", exc, exc_info=True)

    async def run_watchlist_build_task(
        self,
        *,
        latest_market_context: dict,
        last_sector_regimes: dict | None,
        last_regime_assessment: dict | None,
        on_post_build_reasoning: Callable[[str], None],
    ) -> None:
        """Background watchlist build — fires from _slow_loop_cycle, never blocks reasoning.

        Runtime params passed at call time because they are orchestrator scalars
        that get reassigned.
        """
        try:
            watchlist = await self._state_manager.load_watchlist()
            portfolio = await self._state_manager.load_portfolio()
            open_syms = {p.symbol for p in portfolio.positions}
            market_data = latest_market_context or {}

            # Universe scan (session cache; second call within cache_ttl_min is free)
            if self._config.universe_scanner.enabled and self._universe_scanner is not None:
                cache_age_min = (time.monotonic() - self.last_universe_scan_time) / 60
                if cache_age_min > self._config.universe_scanner.cache_ttl_min or not self.last_universe_scan:
                    existing_symbols = {e.symbol for e in watchlist.entries}
                    blacklist_symbols = set(self._config.ranker.no_entry_symbols)
                    try:
                        self.last_universe_scan = await self._universe_scanner.get_top_candidates(
                            n=self._config.universe_scanner.max_candidates,
                            exclude=existing_symbols,
                            blacklist=blacklist_symbols,
                            sector_regimes=last_sector_regimes,
                            regime_assessment=last_regime_assessment,
                            sector_map=self._sector_map,
                        )
                        self.last_universe_scan_time = time.monotonic()
                        log.info(
                            "Watchlist build: universe scan %d candidates (top RVOL: %s)",
                            len(self.last_universe_scan),
                            [c["symbol"] for c in self.last_universe_scan[:5]],
                        )
                    except Exception as exc:
                        log.warning("Watchlist build: universe scan failed — proceeding without candidates: %s", exc)
                else:
                    log.debug("Watchlist build: universe scan cache fresh (%.1f min old)", cache_age_min)

            _n = self._config.universe_scanner.max_candidates_to_claude
            _candidates = (self.last_universe_scan or [])[:_n] or None

            log.info(
                "Watchlist build: starting [candidates=%d  search=%s]",
                len(_candidates) if _candidates else 0,
                "enabled" if (self._search_adapter and self._search_adapter.enabled) else "disabled",
            )

            wl_result = await self._claude.run_watchlist_build(
                market_context=market_data,
                current_watchlist=watchlist,
                target_count=self._config.claude.watchlist_build_target,
                candidates=_candidates,
                search_adapter=self._search_adapter,
                no_entry_symbols=self._config.ranker.no_entry_symbols,
            )

            if wl_result is None:
                parse_retry_min = self._config.scheduler.watchlist_build_parse_failure_retry_min
                interval_min = self._config.scheduler.watchlist_refresh_interval_min
                back_min = max(0, interval_min - parse_retry_min)
                self._trigger_state.last_watchlist_build_utc = (
                    datetime.now(timezone.utc) - timedelta(minutes=back_min)
                )
                self.reasoning_needed_after_build = False
                log.warning(
                    "Watchlist build: response unparseable — will retry in ~%d min", parse_retry_min
                )
                return

            added = await self.apply_watchlist_changes(
                watchlist, wl_result.watchlist, wl_result.removes, open_syms,
                last_sector_regimes=last_sector_regimes,
            )
            self._trigger_state.last_watchlist_build_utc = datetime.now(timezone.utc)
            log.info("Watchlist build: complete — %d added", added)

            if self.reasoning_needed_after_build:
                self.reasoning_needed_after_build = False
                if added > 0:
                    log.info(
                        "Watchlist build: new candidates available — firing post-build reasoning"
                    )
                    on_post_build_reasoning("post_build_candidates")
                else:
                    log.info(
                        "Watchlist build: no new candidates added — skipping post-build reasoning"
                    )

        except Exception as exc:
            probe_min = self._config.ai_fallback.circuit_breaker_probe_min
            interval_min = self._config.scheduler.watchlist_refresh_interval_min
            back_min = max(0, interval_min - probe_min)
            self._trigger_state.last_watchlist_build_utc = (
                datetime.now(timezone.utc) - timedelta(minutes=back_min)
            )
            self.reasoning_needed_after_build = False
            log.error(
                "Watchlist build: unexpected error — will retry in ~%d min: %s",
                probe_min, exc, exc_info=True,
            )
        finally:
            self.build_in_flight = False

    def prune_expired_catalysts(self, watchlist) -> list[str]:
        """Remove entries whose catalyst_expiry_utc has passed. Returns removed symbols."""
        now_utc = datetime.now(timezone.utc)
        expired, kept = [], []
        for e in watchlist.entries:
            if e.catalyst_expiry_utc:
                try:
                    if datetime.fromisoformat(e.catalyst_expiry_utc) <= now_utc:
                        expired.append(e.symbol)
                        log.info("Watchlist: pruned %s — catalyst expired at %s", e.symbol, e.catalyst_expiry_utc)
                        continue
                except Exception:
                    log.warning(
                        "Watchlist: malformed catalyst_expiry_utc for %s: %r — keeping entry",
                        e.symbol, e.catalyst_expiry_utc,
                    )
            kept.append(e)
        watchlist.entries = kept
        return expired

    async def apply_watchlist_changes(
        self,
        watchlist,
        add_list: list[dict],
        remove_list: list[str],
        open_symbols: set[str] | None = None,
        *,
        last_sector_regimes: dict | None = None,
    ) -> int:
        """Apply Claude-suggested watchlist additions and removals, then enforce the size cap.

        ``open_symbols`` is the set of symbols with active positions — these are
        never pruned regardless of score or rank. Pass an empty set when no
        positions are open.

        last_sector_regimes is passed at call time because it's an orchestrator
        scalar that gets reassigned.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Non-tradeable index tickers that Alpaca cannot order.
        _INDEX_BLACKLIST = {
            "VIX", "VXN", "SPX", "NDX", "RUT", "DJI", "COMP",
            "INDU", "NYA", "XAX", "OEX", "MID", "SML",
        }

        self.prune_expired_catalysts(watchlist)
        existing_symbols = {e.symbol for e in watchlist.entries}
        added = 0

        for item in add_list:
            if isinstance(item, str):
                symbol = item.strip().upper()
                reason = "Added by Claude"
                tier = 1
                strategy = "both"
                expected_direction = "either"
                catalyst_expiry_utc = None
            else:
                symbol = item.get("symbol", "").upper()
                reason = item.get("reason", "Added by Claude")
                tier = item.get("priority_tier", 1)
                strategy = item.get("strategy", "both")
                _raw_ed = item.get("expected_direction", "either")
                if _raw_ed not in {"long", "short", "either"}:
                    log.warning(
                        "Watchlist add %s: Claude returned invalid expected_direction %r — using 'either'",
                        symbol, _raw_ed,
                    )
                    _raw_ed = "either"
                expected_direction = _raw_ed
                catalyst_expiry_utc = item.get("catalyst_expiry_utc")
            if not symbol or symbol in existing_symbols:
                continue
            if symbol in _INDEX_BLACKLIST or symbol.startswith("^"):
                log.warning("Watchlist: rejected non-tradeable index ticker %s", symbol)
                continue
            watchlist.entries.append(WatchlistEntry(
                symbol=symbol,
                date_added=now_iso,
                reason=reason,
                priority_tier=tier,
                strategy=strategy,
                expected_direction=expected_direction,
                catalyst_expiry_utc=catalyst_expiry_utc,
            ))
            existing_symbols.add(symbol)
            added += 1
            log.info("Watchlist: added %s (tier=%s direction=%s)", symbol, tier, expected_direction)

        if remove_list:
            _safe_removes = set(remove_list) - (open_symbols or set())
            _protected = set(remove_list) & (open_symbols or set())
            if _protected:
                log.debug("Watchlist: skipped removal of open position symbol(s): %s", sorted(_protected))
            before = len(watchlist.entries)
            watchlist.entries = [
                e for e in watchlist.entries if e.symbol not in _safe_removes
            ]
            removed = before - len(watchlist.entries)
            if removed:
                log.info("Watchlist: removed %d symbol(s): %s", removed, sorted(_safe_removes))

        # Hard size cap — prune lowest-value entries beyond the limit.
        max_entries = self._config.claude.watchlist_max_entries
        if len(watchlist.entries) > max_entries:
            newly_added = {
                (item if isinstance(item, str) else item.get("symbol", "")).upper()
                for item in add_list
            } - {""}
            protected = (open_symbols or set()) | newly_added

            def _composite(e) -> float:
                ind = self._latest_indicators.get(e.symbol, {})
                ed = getattr(e, "expected_direction", "either")
                if ed == "long":  return float(ind.get("long_score",  0.0))
                if ed == "short": return float(ind.get("short_score", 0.0))
                return max(float(ind.get("long_score", 0.0)), float(ind.get("short_score", 0.0)))

            def _direction_conflicts(e) -> bool:
                if not last_sector_regimes:
                    return False
                etf = self._sector_map.get(e.symbol)
                if not etf:
                    return False
                sector_info = last_sector_regimes.get(etf, {})
                sector_regime = sector_info.get("regime", "neutral")
                ed = getattr(e, "expected_direction", "either")
                if ed == "long" and sector_regime in ("correcting", "downtrend"):
                    return True
                if ed == "short" and sector_regime in ("breaking_out", "uptrend"):
                    return True
                return False

            def _eviction_priority(e) -> tuple:
                composite = _composite(e)
                if getattr(e, "priority_tier", 1) == 2:
                    return (0, composite)
                if _direction_conflicts(e):
                    return (1, composite)
                return (2, composite)

            keep_protected = [e for e in watchlist.entries if e.symbol in protected]
            prunable = [e for e in watchlist.entries if e.symbol not in protected]
            slots = max(0, max_entries - len(keep_protected))
            prunable.sort(key=_eviction_priority, reverse=True)
            pruned = [e.symbol for e in prunable[slots:]]
            watchlist.entries = keep_protected + prunable[:slots]
            if pruned:
                log.info(
                    "Watchlist: pruned %d entries over cap=%d: %s",
                    len(pruned), max_entries, pruned,
                )

        await self._state_manager.save_watchlist(watchlist)
        return added
