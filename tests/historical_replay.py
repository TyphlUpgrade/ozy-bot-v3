#!/usr/bin/env python3
"""
tests/historical_replay.py
===========================
Standalone backtest: walk historical 5-minute bars for a configurable set of
symbols, feed data into ``generate_signal_summary()`` and both strategies'
``generate_signals()``, simulate entries and exits, and report statistics.

All tunable parameters live in ``tests/replay_configs.json`` — do not edit
this script to change what is tested.

Methodology
-----------
- Fetch bars via yfinance (cached in memory across grid iterations).
- Walk bars chronologically.  At each bar feed *data-so-far* into
  ``generate_signal_summary()`` and both strategies' ``generate_signals()``.
- Entry fires at the **next bar's open** after a signal.
- Exits checked against each bar's low/high.  Stop takes priority when both
  stop and target are hit intrabar (conservative assumption).
- Market-hours filter: skip bars in the first/last N minutes of each session
  (configurable via ``exclude_open_close_mins`` in globals).
- Cooldown: after a stop-out, ignore re-entry signals on the same
  symbol+strategy for ``reentry_cooldown_bars`` bars.
- Results are auto-saved to ``tests/replay_results.jsonl`` (one JSON line
  per run) unless ``--no-save`` is passed.

No broker, no Claude, no fill protection, no order management.

Usage
-----
    # Single baseline run (pretty stats)
    PYTHONPATH=. python tests/historical_replay.py

    # Run all configs defined in replay_configs.json, save results
    PYTHONPATH=. python tests/historical_replay.py --grid

    # Run every config across all periods in globals.periods (cross-regime validation)
    PYTHONPATH=. python tests/historical_replay.py --multi-period

    # Run one named config
    PYTHONPATH=. python tests/historical_replay.py --config momentum_tight

    # Show a ranked table of all past grid runs
    PYTHONPATH=. python tests/historical_replay.py --results

    # Grid run with a longer data window, without saving
    PYTHONPATH=. python tests/historical_replay.py --grid --period 30d --no-save

CLI flags
---------
--grid              Run every config in replay_configs.json.
--multi-period      Run the full grid across each period in globals.periods.
                    Prints a grouped table with per-period rows and an AVG per config.
--config NAME       Run the single named config from the file.
--results           Print past results from replay_results.jsonl and exit.
--sort-by FIELD     Sort --results table by: net_pnl (default), win_rate,
                    total_trades, date.
--period PERIOD     Override globals.period (e.g. 14d, 30d, 60d).
--interval INTERVAL Override globals.interval (e.g. 5m, 15m, 1h).
--no-save           Do not write to replay_results.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

from ozymandias.intelligence.technical_analysis import (
    generate_signal_summary,
    compute_ema,
    compute_vwap,
    compute_rsi,
    compute_macd,
    compute_roc,
    compute_atr,
    compute_bollinger_bands,
    compute_volume_sma,
    compute_composite_score,
    compute_volatility_regime,
)
from ozymandias.strategies.momentum_strategy import MomentumStrategy
from ozymandias.strategies.swing_strategy import SwingStrategy

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE            = Path(__file__).resolve().parent
CONFIGS_FILE     = _HERE / "replay_configs.json"
RESULTS_FILE     = _HERE / "replay_results.jsonl"

CACHE_DIR = _HERE / "cache"
CACHE_DIR.mkdir(exist_ok=True)
TRADES_FILE = _HERE / "trade_journal_backtest.jsonl"

# Canonical set of indicator names the current _precompute_indicators produces.
# Update this set whenever a new key is added to the signals dict.
_INDICATOR_NAMES: frozenset[str] = frozenset({
    "vwap_position", "rsi", "rsi_divergence", "macd_signal",
    "trend_structure", "roc_5", "roc_deceleration", "volume_ratio",
    "atr_14", "bollinger_position", "price", "avg_daily_volume",
    "vol_regime_ratio",
})


def _ts_slug(ts: str) -> str:
    """Sanitise a timestamp string for use in a filename."""
    return ts.replace(":", "").replace(" ", "T").replace("+", "p").replace("/", "-")[:20]


def _bar_cache_path(symbol: str, period: str, interval: str) -> Path:
    return CACHE_DIR / f"bars_{symbol}_{period}_{interval}.pkl"


def _bar_meta_path(symbol: str, period: str, interval: str) -> Path:
    return CACHE_DIR / f"bars_{symbol}_{period}_{interval}.meta.json"


def _ind_cache_path(symbol: str, period: str, interval: str, latest_bar_ts: str) -> Path:
    return CACHE_DIR / f"ind_{symbol}_{period}_{interval}_{_ts_slug(latest_bar_ts)}.json"


def _bar_cache_is_stale(meta_path: Path) -> bool:
    """True if the cached bar data is old enough that a new trading session
    has likely completed, shifting the window."""
    if not meta_path.exists():
        return True
    with meta_path.open() as f:
        meta = json.load(f)
    fetched_at = datetime.fromisoformat(meta["fetched_at"])
    age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
    dow = datetime.now(timezone.utc).weekday()  # 0=Mon … 6=Sun
    # Stale if >24 h on a trading day, or >72 h any day (weekend rollover)
    return age_hours > 72 or (age_hours > 24 and dow < 5)


def _load_bar_cache(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    pkl  = _bar_cache_path(symbol, period, interval)
    meta = _bar_meta_path(symbol, period, interval)
    if not pkl.exists() or _bar_cache_is_stale(meta):
        return None
    try:
        return pd.read_pickle(pkl)
    except Exception:
        return None


def _save_bar_cache(symbol: str, period: str, interval: str, df: pd.DataFrame) -> None:
    try:
        pkl  = _bar_cache_path(symbol, period, interval)
        meta = _bar_meta_path(symbol, period, interval)
        df.to_pickle(pkl)
        with meta.open("w") as f:
            json.dump({
                "fetched_at":    datetime.now(timezone.utc).isoformat(),
                "latest_bar_ts": str(df.index[-1]),
                "bar_count":     len(df),
            }, f)
    except Exception:
        pass  # cache write failure is non-fatal


def _load_ind_cache(
    symbol: str, period: str, interval: str, latest_bar_ts: str
) -> tuple[dict[int, dict], set[str]] | None:
    """Returns (cache_dict, cached_indicator_names) or None if not found."""
    path = _ind_cache_path(symbol, period, interval, latest_bar_ts)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            raw = json.load(f)
        names = set(raw.get("__indicator_names__", []))
        cache = {int(k): v for k, v in raw.items() if not k.startswith("__")}
        return cache, names
    except Exception:
        return None


def _save_ind_cache(
    symbol: str, period: str, interval: str, latest_bar_ts: str, cache: dict[int, dict]
) -> None:
    path = _ind_cache_path(symbol, period, interval, latest_bar_ts)
    try:
        raw: dict = {"__indicator_names__": sorted(_INDICATOR_NAMES)}
        raw.update({str(k): v for k, v in cache.items()})
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(raw, f)
        tmp.rename(path)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    strategy: str
    entry_bar: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float = 0.0
    exit_bar: int = -1
    exit_reason: str = ""             # "stop" | "target" | "end_of_data"
    signals_at_entry: dict = field(default_factory=dict)

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def is_winner(self) -> bool:
        return self.pnl_pct > 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config_file() -> dict:
    if not CONFIGS_FILE.exists():
        sys.exit(f"ERROR: config file not found: {CONFIGS_FILE}")
    with CONFIGS_FILE.open() as f:
        return json.load(f)


def get_named_config(cfg_file: dict, name: str) -> dict:
    for cfg in cfg_file.get("configs", []):
        if cfg["name"] == name:
            return cfg
    names = [c["name"] for c in cfg_file.get("configs", [])]
    sys.exit(f"ERROR: config '{name}' not found. Available: {names}")


# ---------------------------------------------------------------------------
# Market-hours filter helper
# ---------------------------------------------------------------------------

def _strategy_filter_settings(strat_params: dict | None, globals_: dict) -> tuple[int, int]:
    """Return (exclude_open_close_mins, reentry_cooldown_bars) for one strategy.
    Strategy-level values take precedence over globals."""
    p = strat_params or {}
    excl = p.get("exclude_open_close_mins", globals_.get("exclude_open_close_mins", 0))
    cool = p.get("reentry_cooldown_bars",   globals_.get("reentry_cooldown_bars",   0))
    return int(excl), int(cool)


def _mask_allows(masks: dict[str, pd.Series], strat_name: str, i: int) -> bool:
    m = masks.get(strat_name)
    return bool(m.iloc[i]) if m is not None else True


def _precompute_indicators(
    symbol: str, df: pd.DataFrame, warmup_bars: int
) -> dict[int, dict]:
    """O(n) precompute: compute all indicator Series once on the full DataFrame,
    then extract per-bar signal dicts by indexing into those series.

    Previous approach called generate_signal_summary(df.iloc[:i+1]) per bar,
    which reruns all EWM/rolling from scratch each call → O(n²).  Computing
    on the full df first and indexing is O(n).  EWM/rolling indicators are
    causal, so series.iloc[i] from the full-df computation is identical to
    series.iloc[-1] from a slice computation on df.iloc[:i+1].

    Returns {bar_index: signals_dict}. Bars that raise are omitted.
    """
    n      = len(df)
    close  = df["close"]
    volume = df["volume"]

    # Compute all indicator series once — O(n) total
    ema_9   = compute_ema(close, 9)
    ema_20  = compute_ema(close, 20)
    ema_50  = compute_ema(close, 50)
    ema_200 = compute_ema(close, 200)
    vwap_s  = compute_vwap(df)
    rsi_s   = compute_rsi(df)
    macd_df = compute_macd(df)
    roc_s   = compute_roc(df)
    atr_s   = compute_atr(df)
    bb      = compute_bollinger_bands(df)
    vol_sma    = compute_volume_sma(df)
    vol_regime = compute_volatility_regime(df)

    hist    = macd_df["histogram"]
    bb_mid  = bb["middle"]

    # Expanding means for NaN-warmup fallbacks — O(n), avoids O(i) per bar
    vol_exp_mean   = volume.expanding().mean()
    close_exp_mean = close.expanding().mean()

    # Scalar used for avg_daily_volume; same approximation as original
    avg_daily_vol = float(volume.mean())

    cache: dict[int, dict] = {}

    for i in range(warmup_bars, n - 1):   # n-1: last bar is never a signal bar
        try:
            c = float(close.iloc[i])

            # VWAP position
            vw = float(vwap_s.iloc[i]) if not pd.isna(vwap_s.iloc[i]) else c
            thr = vw * 0.001
            if c > vw + thr:
                vwap_pos = "above"
            elif c < vw - thr:
                vwap_pos = "below"
            else:
                vwap_pos = "at"

            # RSI
            last_rsi = float(rsi_s.iloc[i]) if not pd.isna(rsi_s.iloc[i]) else 50.0

            # RSI divergence — inline the 20-bar lookback window
            lb = max(0, i - 19)
            pw = close.iloc[lb : i + 1]
            rw = rsi_s.iloc[lb : i + 1]
            ph: list[float] = []
            rh: list[float] = []
            for j in range(1, len(pw) - 1):
                if pw.iloc[j] > pw.iloc[j - 1] and pw.iloc[j] > pw.iloc[j + 1]:
                    ph.append(float(pw.iloc[j]))
                    rh.append(float(rw.iloc[j]))
            if len(ph) >= 2:
                if ph[-1] > ph[-2] and rh[-1] < rh[-2]:
                    rsi_div: str | bool = "bearish"
                elif ph[-1] < ph[-2] and rh[-1] > rh[-2]:
                    rsi_div = "bullish"
                else:
                    rsi_div = False
            else:
                rsi_div = False

            # MACD cross — inline 2-bar histogram check
            if i >= 1 and not pd.isna(hist.iloc[i]) and not pd.isna(hist.iloc[i - 1]):
                cur  = float(hist.iloc[i])
                prev = float(hist.iloc[i - 1])
                if prev <= 0 and cur > 0:
                    macd_sig = "bullish_cross"
                elif prev >= 0 and cur < 0:
                    macd_sig = "bearish_cross"
                elif cur > 0:
                    macd_sig = "bullish"
                else:
                    macd_sig = "bearish"
            else:
                macd_sig = "bearish"

            # Trend structure — inline EMA alignment at bar i
            ema_vals: dict[int, float] = {}
            for length, series in (
                (9, ema_9), (20, ema_20), (50, ema_50), (200, ema_200)
            ):
                v = series.iloc[i]
                if not pd.isna(v):
                    ema_vals[length] = float(v)
            if len(ema_vals) >= 2:
                sl = sorted(ema_vals)
                vs = [ema_vals[l] for l in sl]
                if all(vs[k] > vs[k + 1] for k in range(len(vs) - 1)):
                    trend = "bullish_aligned"
                elif all(vs[k] < vs[k + 1] for k in range(len(vs) - 1)):
                    trend = "bearish_aligned"
                else:
                    trend = "mixed"
            else:
                trend = "mixed"

            # ROC + deceleration
            last_roc = float(roc_s.iloc[i]) if not pd.isna(roc_s.iloc[i]) else 0.0
            roc_decel = False
            if i >= 1 and not pd.isna(roc_s.iloc[i - 1]):
                prev_roc = float(roc_s.iloc[i - 1])
                if last_roc > 0 and prev_roc > 0 and last_roc < prev_roc:
                    roc_decel = True

            # Volume ratio
            last_vol = float(volume.iloc[i])
            vs_raw = vol_sma.iloc[i]
            if not pd.isna(vs_raw):
                vsma = float(vs_raw)
            else:
                vsma = float(vol_exp_mean.iloc[i])
                if vsma <= 0:
                    vsma = last_vol
            vol_ratio = last_vol / vsma if vsma > 0 else 1.0

            # Bollinger position
            bm_raw = bb_mid.iloc[i]
            bm = float(bm_raw) if not pd.isna(bm_raw) else float(close_exp_mean.iloc[i])
            bb_pos = "upper_half" if c >= bm else "lower_half"

            # ATR
            last_atr = float(atr_s.iloc[i]) if not pd.isna(atr_s.iloc[i]) else 0.0

            last_vol_regime = (
                float(vol_regime.iloc[i]) if not pd.isna(vol_regime.iloc[i]) else 1.0
            )

            signals = {
                "vwap_position":     vwap_pos,
                "rsi":               round(last_rsi, 2),
                "rsi_divergence":    rsi_div,
                "macd_signal":       macd_sig,
                "trend_structure":   trend,
                "roc_5":             round(last_roc, 4),
                "roc_deceleration":  roc_decel,
                "volume_ratio":      round(vol_ratio, 4),
                "atr_14":            round(last_atr, 4),
                "bollinger_position": bb_pos,
                "price":             round(c, 4),
                "avg_daily_volume":  round(avg_daily_vol, 0),
                "vol_regime_ratio":  round(last_vol_regime, 4),
            }
            cache[i] = signals
        except Exception:
            pass   # absent key → skip bar during replay, same as current behaviour

    return cache


def _make_session_mask(df: pd.DataFrame, exclude_mins: int) -> pd.Series:
    """
    Return a boolean Series (same index as df) that is True for bars that
    fall within the tradeable window (i.e. not in the first or last
    ``exclude_mins`` minutes of the session).

    Works on both timezone-aware and timezone-naive DatetimeIndex.
    """
    if exclude_mins <= 0:
        return pd.Series(True, index=df.index)

    idx = df.index
    # Normalise to Eastern time for open/close boundary detection
    try:
        et = idx.tz_convert("America/New_York")
    except Exception:
        # Already tz-naive or conversion failed — use as-is
        et = idx

    session_open_mins  = 9 * 60 + 30    # 09:30 ET in minutes-since-midnight
    session_close_mins = 16 * 60         # 16:00 ET

    minutes = et.hour * 60 + et.minute
    mask = (
        (minutes >= session_open_mins  + exclude_mins) &
        (minutes <  session_close_mins - exclude_mins)
    )
    return pd.Series(mask, index=df.index)


# ---------------------------------------------------------------------------
# Per-symbol replay core
# ---------------------------------------------------------------------------

async def _replay_symbol(
    symbol: str,
    df: pd.DataFrame,
    momentum: MomentumStrategy,
    swing: Optional[SwingStrategy],
    warmup_bars: int,
    strategy_cooldown_bars: dict[str, int],
    strategy_masks: dict[str, pd.Series],
    indicators_cache: dict[int, dict] | None = None,
) -> list[Trade]:
    """Walk bars for one symbol and return completed trades."""

    strategies: list[tuple[str, MomentumStrategy | SwingStrategy]] = [
        ("momentum", momentum),
    ]
    if swing is not None:
        strategies.append(("swing", swing))

    completed: list[Trade] = []
    active:    dict[str, Trade] = {}                        # strat_name → open Trade
    pending:   dict[str, tuple[float, float, dict]] = {}    # strat_name → (stop, target, signals)
    cooldown:  dict[str, int] = {}                          # strat_name → bar index after which re-entry allowed

    n = len(df)

    for i in range(warmup_bars, n):
        bar = df.iloc[i]
        bar_open  = float(bar["open"])
        bar_high  = float(bar["high"])
        bar_low   = float(bar["low"])

        # ----------------------------------------------------------------
        # 1. Fill pending entries at this bar's open
        # ----------------------------------------------------------------
        for strat_name, (stop_px, target_px, entry_signals) in list(pending.items()):
            if strat_name in active:
                continue
            if bar_open <= 0:
                continue
            active[strat_name] = Trade(
                symbol=symbol,
                strategy=strat_name,
                entry_bar=i,
                entry_price=bar_open,
                stop_price=stop_px,
                target_price=target_px,
                signals_at_entry=entry_signals,
            )
        pending.clear()

        # ----------------------------------------------------------------
        # 2. Check exits for open positions
        # ----------------------------------------------------------------
        for strat_name, trade in list(active.items()):
            stop_hit   = trade.stop_price   > 0 and bar_low  <= trade.stop_price
            target_hit = trade.target_price > 0 and bar_high >= trade.target_price

            if stop_hit and target_hit:
                # Both intrabar — conservative: stop wins
                trade.exit_price  = trade.stop_price
                trade.exit_bar    = i
                trade.exit_reason = "stop"
                cooldown[strat_name] = i + strategy_cooldown_bars.get(strat_name, 0)
                completed.append(trade)
                del active[strat_name]
            elif stop_hit:
                trade.exit_price  = trade.stop_price
                trade.exit_bar    = i
                trade.exit_reason = "stop"
                cooldown[strat_name] = i + strategy_cooldown_bars.get(strat_name, 0)
                completed.append(trade)
                del active[strat_name]
            elif target_hit:
                trade.exit_price  = trade.target_price
                trade.exit_bar    = i
                trade.exit_reason = "target"
                completed.append(trade)
                del active[strat_name]

        # ----------------------------------------------------------------
        # 3. Generate signals on data-so-far
        #    (skip last bar; per-strategy session masks applied below)
        # ----------------------------------------------------------------
        if i >= n - 1:
            continue

        eligible = [
            (sn, st) for sn, st in strategies
            if sn not in active
            and sn not in pending
            and cooldown.get(sn, 0) <= i
            and _mask_allows(strategy_masks, sn, i)
        ]
        if not eligible:
            continue

        if indicators_cache is not None:
            indicators = indicators_cache.get(i)
            if indicators is None:
                continue          # bar failed during precompute — skip
            df_so_far = df.iloc[: i + 1]
        else:
            df_so_far = df.iloc[: i + 1]
            try:
                summary = generate_signal_summary(symbol, df_so_far)
            except Exception:
                continue
            indicators = summary.get("signals", summary)

        for strat_name, strategy in eligible:
            try:
                signals = await strategy.generate_signals(symbol, df_so_far, indicators)
            except Exception:
                continue

            if not signals:
                continue

            sig = signals[0]
            if sig.direction != "long":
                continue
            if sig.stop_price <= 0 or sig.target_price <= 0:
                continue

            pending[strat_name] = (sig.stop_price, sig.target_price, dict(indicators) if indicators else {})

    # ----------------------------------------------------------------
    # 4. Close anything still open at end of data
    # ----------------------------------------------------------------
    last_close = float(df.iloc[-1]["close"])
    for strat_name, trade in active.items():
        trade.exit_price  = last_close
        trade.exit_bar    = n - 1
        trade.exit_reason = "end_of_data"
        completed.append(trade)

    return completed


# ---------------------------------------------------------------------------
# Full replay driver
# ---------------------------------------------------------------------------

async def run_replay(
    cfg: dict,
    globals_: dict,
    bar_cache: dict[str, pd.DataFrame] | None = None,
    indicators_cache: dict[str, dict[int, dict]] | None = None,
    verbose: bool = True,
) -> list[Trade]:
    """
    Run a single named configuration against all symbols.

    Parameters
    ----------
    cfg:
        One entry from ``replay_configs.json``'s ``configs`` list.
    globals_:
        The ``globals`` block from the config file (merged with CLI overrides).
    bar_cache:
        In-memory cache of fetched DataFrames.  Pass the same dict across
        multiple calls to avoid re-fetching.
    verbose:
        Print per-symbol fetch progress.
    """
    symbols      = globals_["symbols"]
    period       = globals_["period"]
    interval     = cfg.get("interval") or globals_["interval"]
    warmup_bars  = int(globals_.get("warmup_bars", 60))

    momentum_params = cfg.get("momentum") or {}
    swing_params    = cfg.get("swing")          # None → swing disabled

    momentum = MomentumStrategy(params=momentum_params)
    swing    = SwingStrategy(params=swing_params) if swing_params is not None else None

    # Resolve per-strategy filter settings (strategy block overrides globals)
    mom_excl, mom_cool = _strategy_filter_settings(cfg.get("momentum"), globals_)
    swg_excl, swg_cool = (
        _strategy_filter_settings(cfg.get("swing"), globals_)
        if swing is not None else (0, 0)
    )

    all_trades: list[Trade] = []

    for symbol in symbols:
        if bar_cache is not None and symbol in bar_cache:
            df = bar_cache[symbol]
        else:
            # Try disk cache before hitting yfinance
            cached_df = _load_bar_cache(symbol, period, interval)
            if cached_df is not None:
                if verbose:
                    print(f"  {symbol:6s} ... {len(cached_df)} bars (disk cache)", flush=True)
                df = cached_df
                if bar_cache is not None:
                    bar_cache[symbol] = df
            else:
                if verbose:
                    print(f"  {symbol:6s} ...", end=" ", flush=True)
                try:
                    ticker = yf.Ticker(symbol)
                    df = ticker.history(period=period, interval=interval)
                except Exception as exc:
                    if verbose:
                        print(f"FETCH ERROR: {exc}")
                    continue

                if df.empty:
                    if verbose:
                        print("no data returned")
                    continue

                df.columns = [c.lower() for c in df.columns]

                if len(df) < warmup_bars + 2:
                    if verbose:
                        print(f"insufficient bars ({len(df)})")
                    continue

                if verbose:
                    print(f"{len(df)} bars", flush=True)

                _save_bar_cache(symbol, period, interval, df)
                if bar_cache is not None:
                    bar_cache[symbol] = df

        strategy_masks = {
            "momentum": _make_session_mask(df, mom_excl),
            "swing":    _make_session_mask(df, swg_excl),
        }
        strategy_cooldown_bars = {
            "momentum": mom_cool,
            "swing":    swg_cool,
        }

        try:
            trades = await _replay_symbol(
                symbol, df, momentum, swing,
                warmup_bars=warmup_bars,
                strategy_cooldown_bars=strategy_cooldown_bars,
                strategy_masks=strategy_masks,
                indicators_cache=indicators_cache.get(symbol) if indicators_cache else None,
            )
        except Exception as exc:
            if verbose:
                print(f"  ERROR during replay of {symbol}: {exc}")
            continue

        all_trades.extend(trades)

    return all_trades


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _compute_stats(trades: list[Trade]) -> dict:
    if not trades:
        return {}

    winners = [t for t in trades if t.pnl_pct > 0]
    losers  = [t for t in trades if t.pnl_pct <= 0]
    total   = len(trades)

    by_strategy: dict[str, dict] = {}
    for t in trades:
        by_strategy.setdefault(t.strategy, []).append(t)

    by_symbol: dict[str, float] = {}
    for t in trades:
        by_symbol[t.symbol] = by_symbol.get(t.symbol, 0.0) + t.pnl_pct

    best  = max(trades, key=lambda t: t.pnl_pct)
    worst = min(trades, key=lambda t: t.pnl_pct)

    return {
        "total":               total,
        "winners":             len(winners),
        "losers":              len(losers),
        "win_rate_pct":        round(len(winners) / total * 100, 2),
        "avg_win_pct":         round(sum(t.pnl_pct for t in winners) / len(winners), 4) if winners else 0.0,
        "avg_loss_pct":        round(sum(t.pnl_pct for t in losers)  / len(losers),  4) if losers  else 0.0,
        "net_pnl_pct":         round(sum(t.pnl_pct for t in trades), 4),
        "stop_exits":          sum(1 for t in trades if t.exit_reason == "stop"),
        "target_exits":        sum(1 for t in trades if t.exit_reason == "target"),
        "eod_exits":           sum(1 for t in trades if t.exit_reason == "end_of_data"),
        "largest_winner_pct":  round(best.pnl_pct, 4),
        "largest_winner_sym":  best.symbol,
        "largest_loser_pct":   round(worst.pnl_pct, 4),
        "largest_loser_sym":   worst.symbol,
        "by_strategy": {
            name: {
                "total":       len(st),
                "win_rate_pct": round(sum(1 for t in st if t.pnl_pct > 0) / len(st) * 100, 1),
                "net_pnl_pct": round(sum(t.pnl_pct for t in st), 4),
            }
            for name, st in by_strategy.items()
        },
        "by_symbol_net_pnl": {
            sym: round(pnl, 4) for sym, pnl in sorted(by_symbol.items(), key=lambda x: x[1])
        },
    }


def print_stats(trades: list[Trade], label: str = "") -> None:
    print()
    print("=" * 68)
    if label:
        print(f"  {label}")
    print(f"  HISTORICAL REPLAY RESULTS  (10 symbols)")
    print("=" * 68)

    if not trades:
        print("  No trades recorded.")
        print("=" * 68)
        return

    s = _compute_stats(trades)

    print(f"  Total trades    : {s['total']}")
    print(f"  Win rate        : {s['win_rate_pct']:.1f}%  ({s['winners']}W / {s['losers']}L)")
    print(f"  Avg win         : +{s['avg_win_pct']:.2f}%")
    print(f"  Avg loss        : {s['avg_loss_pct']:.2f}%")
    print(f"  Net P&L (sum %) : {s['net_pnl_pct']:+.2f}%")
    print(f"  Largest winner  : {s['largest_winner_sym']}  {s['largest_winner_pct']:+.2f}%")
    print(f"  Largest loser   : {s['largest_loser_sym']}  {s['largest_loser_pct']:+.2f}%")

    print()
    print("  By strategy:")
    for strat, st in s["by_strategy"].items():
        print(f"    {strat:14s}  {st['total']:3d} trades  "
              f"win={st['win_rate_pct']:.0f}%  net={st['net_pnl_pct']:+.2f}%")

    print()
    print("  Exit breakdown:")
    total = s["total"]
    for key, label_str in [("stop_exits","stop"), ("target_exits","target"), ("eod_exits","end_of_data")]:
        n = s[key]
        print(f"    {label_str:15s}: {n:3d}  ({n/total*100:.1f}%)")

    print()
    print("  Net P&L by symbol:")
    for sym, pnl in s["by_symbol_net_pnl"].items():
        bar = "█" * int(abs(pnl) / 2) if pnl != 0 else ""
        sign = "+" if pnl >= 0 else ""
        print(f"    {sym:6s}  {sign}{pnl:.2f}%  {bar}")

    print("=" * 68)


# ---------------------------------------------------------------------------
# Results persistence
# ---------------------------------------------------------------------------

def _save_result(
    cfg: dict,
    globals_: dict,
    stats: dict,
) -> None:
    record = {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "config_name":   cfg.get("name", "unnamed"),
        "config_label":  cfg.get("label", ""),
        "momentum_params": cfg.get("momentum"),
        "swing_params":    cfg.get("swing"),
        "period":        globals_.get("period"),
        "interval":      globals_.get("interval"),
        "symbols":       globals_.get("symbols"),
        "warmup_bars":   globals_.get("warmup_bars"),
        "cooldown_bars": globals_.get("reentry_cooldown_bars"),
        "excl_open_close_mins": globals_.get("exclude_open_close_mins"),
        "stats":         stats,
    }
    with RESULTS_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _save_trades(trades: list[Trade], cfg: dict, df_map: dict | None = None) -> int:
    """Append per-trade rows to TRADES_FILE.  Returns number of rows written."""
    if not trades:
        return 0
    config_name = cfg.get("name", "unnamed")
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    with TRADES_FILE.open("a") as f:
        for t in trades:
            record = {
                "trade_id":        str(uuid.uuid4()),
                "source":          "backtest",
                "config_name":     config_name,
                "symbol":          t.symbol,
                "strategy":        t.strategy,
                "direction":       "long",
                "entry_price":     t.entry_price,
                "exit_price":      t.exit_price,
                "stop_price":      t.stop_price,
                "target_price":    t.target_price,
                "exit_reason":     t.exit_reason,
                "pnl_pct":         round(t.pnl_pct, 4),
                "signals_at_entry": t.signals_at_entry,
                "recorded_at":     now,
            }
            f.write(json.dumps(record) + "\n")
            count += 1
    return count


def show_results(sort_by: str = "net_pnl") -> None:
    if not RESULTS_FILE.exists() or RESULTS_FILE.stat().st_size == 0:
        print("No results recorded yet.  Run --grid to generate some.")
        return

    records: list[dict] = []
    with RESULTS_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    sort_key_map = {
        "net_pnl":     lambda r: r["stats"].get("net_pnl_pct", 0),
        "win_rate":    lambda r: r["stats"].get("win_rate_pct", 0),
        "total_trades":lambda r: r["stats"].get("total", 0),
        "date":        lambda r: r.get("timestamp", ""),
    }
    key_fn = sort_key_map.get(sort_by, sort_key_map["net_pnl"])
    records.sort(key=key_fn, reverse=True)

    print()
    print("=" * 100)
    print(f"  PAST RESULTS  ({len(records)} runs)  —  sorted by {sort_by}")
    print("=" * 100)
    hdr = (
        f"  {'Date':>19}  {'Config':<38}  {'Period':>6}  "
        f"{'Trades':>6}  {'Win%':>5}  {'AvgW':>6}  {'AvgL':>6}  {'Net%':>7}"
    )
    print(hdr)
    print("  " + "-" * 96)
    for r in records:
        s   = r.get("stats", {})
        ts  = r.get("timestamp", "")[:19].replace("T", " ")
        lbl = r.get("config_label") or r.get("config_name", "")
        per = r.get("period", "?")
        print(
            f"  {ts:>19}  {lbl:<38}  {per:>6}  "
            f"{s.get('total',0):>6}  {s.get('win_rate_pct',0):>4.1f}%  "
            f"{s.get('avg_win_pct',0):>+5.2f}%  {s.get('avg_loss_pct',0):>+5.2f}%  "
            f"{s.get('net_pnl_pct',0):>+6.2f}%"
        )
    print("=" * 100)


# ---------------------------------------------------------------------------
# Grid runner
# ---------------------------------------------------------------------------

async def run_multi_period_grid(
    cfg_file: dict,
    globals_: dict,
    save: bool = True,
) -> None:
    """
    Run the full config grid across every period listed in globals['periods'],
    printing a grouped table with per-period rows and an AVG summary per config.
    """
    periods     = globals_.get("periods", [globals_["period"]])
    configs     = cfg_file["configs"]
    warmup_bars = int(globals_.get("warmup_bars", 60))

    # all_results[cfg_name][period] = stats dict (or {} if no trades)
    all_results: dict[str, dict[str, dict]] = {cfg["name"]: {} for cfg in configs}

    # Group configs by their resolved interval so we fetch/precompute once per interval
    def _cfg_interval(c: dict) -> str:
        return c.get("interval") or globals_["interval"]

    configs_by_interval: dict[str, list] = {}
    for cfg in configs:
        configs_by_interval.setdefault(_cfg_interval(cfg), []).append(cfg)

    for period in periods:
        period_globals = {**globals_, "period": period}

        for interval_key, interval_configs in configs_by_interval.items():
            iv_globals = {**period_globals, "interval": interval_key}
            bar_cache: dict[str, pd.DataFrame] = {}

            print(f"--- Fetching {period} of {interval_key} bars ---")
            # Pre-warm bar cache (disk cache checked inside run_replay)
            await run_replay(interval_configs[0], iv_globals, bar_cache=bar_cache, verbose=True)
            print()

            print("  precomputing indicators: ", end="", flush=True)
            ind_cache: dict[str, dict[int, dict]] = {}
            for symbol, df in bar_cache.items():
                latest_bar_ts = str(df.index[-1])
                loaded = _load_ind_cache(symbol, period, interval_key, latest_bar_ts)
                if loaded is not None:
                    existing_cache, cached_names = loaded
                    missing = _INDICATOR_NAMES - cached_names
                    if missing:
                        # New indicators added — recompute and merge only missing keys
                        fresh = _precompute_indicators(symbol, df, warmup_bars)
                        for i, signals in fresh.items():
                            if i in existing_cache:
                                existing_cache[i].update(
                                    {k: signals[k] for k in missing if k in signals}
                                )
                            else:
                                existing_cache[i] = {
                                    k: signals[k] for k in missing if k in signals
                                }
                        _save_ind_cache(symbol, period, interval_key, latest_bar_ts, existing_cache)
                    ind_cache[symbol] = existing_cache
                    print(f"{symbol}(cached)", end=" ", flush=True)
                else:
                    fresh = _precompute_indicators(symbol, df, warmup_bars)
                    _save_ind_cache(symbol, period, interval_key, latest_bar_ts, fresh)
                    ind_cache[symbol] = fresh
                    print(symbol, end=" ", flush=True)
            print(f"— done ({len(ind_cache)} symbols)")
            print()

            for cfg in interval_configs:
                trades = await run_replay(
                    cfg, iv_globals,
                    bar_cache=bar_cache,
                    indicators_cache=ind_cache,
                    verbose=False,
                )
                stats  = _compute_stats(trades)
                all_results[cfg["name"]][period] = stats or {}
                if save and stats:
                    _save_result(cfg, iv_globals, stats)

    # ----------------------------------------------------------------
    # Print grouped summary table
    # ----------------------------------------------------------------
    periods_str = ", ".join(periods)
    print()
    print("=" * 88)
    print(f"  MULTI-PERIOD GRID  ({len(configs)} configs × {len(globals_['symbols'])} symbols × {len(periods)} periods)")
    intervals_str = "/".join(sorted(configs_by_interval.keys()))
    print(f"  periods={periods_str}  interval={intervals_str}"
          f"  |  cooldown={globals_.get('reentry_cooldown_bars',0)}bars"
          f"  excl={globals_.get('exclude_open_close_mins',0)}min")
    print("=" * 88)
    hdr = (
        f"  {'Config / Period':<42}  {'Trades':>6}  {'Win%':>5}  "
        f"{'AvgW':>6}  {'AvgL':>6}  {'Net%':>7}"
    )
    print(hdr)

    for cfg in configs:
        label = cfg.get("label") or cfg["name"]
        print(f"  {'─'*84}")
        # Truncate label to 42 chars for header row
        print(f"  {label[:42]}")

        period_stats = all_results[cfg["name"]]
        valid = [s for s in period_stats.values() if s]

        for period in periods:
            s = period_stats.get(period, {})
            if not s:
                print(f"    {period:<8}                                    {'—':>6}  {'—':>5}  {'—':>6}  {'—':>6}  {'—':>7}")
            else:
                print(
                    f"    {period:<8}  {'':30}  {s['total']:>6}  {s['win_rate_pct']:>4.1f}%  "
                    f"{s['avg_win_pct']:>+5.2f}%  {s['avg_loss_pct']:>+5.2f}%  "
                    f"{s['net_pnl_pct']:>+6.2f}%"
                )

        if valid:
            avg_trades  = round(sum(s["total"]        for s in valid) / len(valid))
            avg_win_rt  = round(sum(s["win_rate_pct"] for s in valid) / len(valid), 1)
            avg_win_pct = round(sum(s["avg_win_pct"]  for s in valid) / len(valid), 2)
            avg_los_pct = round(sum(s["avg_loss_pct"] for s in valid) / len(valid), 2)
            avg_net     = round(sum(s["net_pnl_pct"]  for s in valid) / len(valid), 2)
            print(
                f"    {'AVG':<8}  {'':30}  {avg_trades:>6}  {avg_win_rt:>4.1f}%  "
                f"{avg_win_pct:>+5.2f}%  {avg_los_pct:>+5.2f}%  {avg_net:>+6.2f}%"
            )

    print(f"  {'─'*84}")
    print("=" * 88)
    if save:
        print(f"\n  Results appended to {RESULTS_FILE.name}")


async def run_grid(
    cfg_file: dict,
    globals_: dict,
    save: bool = True,
) -> None:
    print(
        f"Fetching {globals_['period']} of {globals_['interval']} bars "
        f"for {len(globals_['symbols'])} symbols...\n"
    )
    bar_cache: dict[str, pd.DataFrame] = {}

    # Pre-warm cache using first config (or baseline if present)
    first_cfg = cfg_file["configs"][0]
    await run_replay(first_cfg, globals_, bar_cache=bar_cache, verbose=True)

    configs = cfg_file["configs"]
    print()
    print("=" * 88)
    print(f"  PARAM GRID  ({len(configs)} configs × {len(globals_['symbols'])} symbols"
          f"  |  period={globals_['period']}  interval={globals_['interval']}"
          f"  |  cooldown={globals_.get('reentry_cooldown_bars',0)}bars"
          f"  excl={globals_.get('exclude_open_close_mins',0)}min)")
    print("=" * 88)
    hdr = (
        f"  {'Config':<40}  {'Trades':>6}  {'Win%':>5}  "
        f"{'AvgW':>6}  {'AvgL':>6}  {'Net%':>7}"
    )
    print(hdr)
    print("  " + "-" * 84)

    for cfg in configs:
        trades = await run_replay(cfg, globals_, bar_cache=bar_cache, verbose=False)
        stats  = _compute_stats(trades)
        label  = cfg.get("label") or cfg["name"]

        if not stats:
            print(f"  {label:<40}  {'—':>6}  {'—':>5}  {'—':>6}  {'—':>6}  {'—':>7}")
            continue

        print(
            f"  {label:<40}  {stats['total']:>6}  {stats['win_rate_pct']:>4.1f}%  "
            f"{stats['avg_win_pct']:>+5.2f}%  {stats['avg_loss_pct']:>+5.2f}%  "
            f"{stats['net_pnl_pct']:>+6.2f}%"
        )

        if save:
            _save_result(cfg, globals_, stats)

    print("=" * 88)
    if save:
        print(f"\n  Results appended to {RESULTS_FILE.name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Historical replay backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--grid",         action="store_true", help="Run all configs in replay_configs.json")
    mode.add_argument("--multi-period", action="store_true", help="Run grid across all periods in globals.periods")
    mode.add_argument("--config",       metavar="NAME",      help="Run a single named config")
    mode.add_argument("--results",      action="store_true", help="Print past results table and exit")

    parser.add_argument("--sort-by",  default="net_pnl",
                        choices=["net_pnl", "win_rate", "total_trades", "date"],
                        help="Sort column for --results (default: net_pnl)")
    parser.add_argument("--period",   metavar="PERIOD",   help="Override globals.period (e.g. 30d)")
    parser.add_argument("--interval", metavar="INTERVAL", help="Override globals.interval (e.g. 15m)")
    parser.add_argument("--no-save",     action="store_true",
                        help="Do not write results to replay_results.jsonl")
    parser.add_argument("--save-trades", action="store_true",
                        help="Append per-trade rows (with signals) to trade_journal_backtest.jsonl")
    args = parser.parse_args()

    if args.results:
        show_results(sort_by=args.sort_by)
        return

    cfg_file = load_config_file()
    globals_ = dict(cfg_file.get("globals", {}))

    # CLI overrides for period/interval
    if args.period:
        globals_["period"] = args.period
    if args.interval:
        globals_["interval"] = args.interval

    save        = not args.no_save
    save_trades = args.save_trades

    if args.grid:
        asyncio.run(run_grid(cfg_file, globals_, save=save))

    elif args.multi_period:
        asyncio.run(run_multi_period_grid(cfg_file, globals_, save=save))

    elif args.config:
        cfg = get_named_config(cfg_file, args.config)
        print(
            f"Running config '{cfg['name']}'  "
            f"({globals_['period']} {globals_['interval']})\n"
        )
        trades = asyncio.run(run_replay(cfg, globals_, verbose=True))
        stats  = _compute_stats(trades)
        print_stats(trades, label=cfg.get("label") or cfg["name"])
        if save and stats:
            _save_result(cfg, globals_, stats)
            print(f"\nResult saved to {RESULTS_FILE.name}")
        if save_trades:
            n = _save_trades(trades, cfg)
            print(f"{n} trade rows appended to {TRADES_FILE.name}")

    else:
        # Default: run first config (baseline) with pretty output
        cfg = cfg_file["configs"][0]
        print(
            f"Running '{cfg['name']}'  "
            f"({globals_['period']} {globals_['interval']})\n"
        )
        print(f"Fetching bars for {len(globals_['symbols'])} symbols...\n")
        trades = asyncio.run(run_replay(cfg, globals_, verbose=True))
        print_stats(trades, label=cfg.get("label") or cfg["name"])
        if save and _compute_stats(trades):
            _save_result(cfg, globals_, _compute_stats(trades))
            print(f"\nResult saved to {RESULTS_FILE.name}")
        if save_trades:
            n = _save_trades(trades, cfg)
            print(f"{n} trade rows appended to {TRADES_FILE.name}")


if __name__ == "__main__":
    main()
