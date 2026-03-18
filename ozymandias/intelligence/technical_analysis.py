"""
Technical analysis module.

All indicators are hand-rolled with pandas and numpy.
No third-party TA libraries (no pandas-ta, no ta-lib).

Each indicator is a standalone pure function for individual testability.
Functions take a DataFrame with lowercase columns (open, high, low, close, volume)
and return a Series or DataFrame.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Core indicator functions
# ---------------------------------------------------------------------------

def compute_ema(series: pd.Series, length: int) -> pd.Series:
    """EMA using pandas ewm(span=length, adjust=False)."""
    return series.ewm(span=length, adjust=False).mean()


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    VWAP = cumsum(typical_price * volume) / cumsum(volume).

    Typical price = (high + low + close) / 3.
    Resets at the start of each calendar day when the index is a DatetimeIndex.
    """
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    tpv = typical_price * df['volume']

    if isinstance(df.index, pd.DatetimeIndex):
        dates = df.index.date
        date_series = pd.Series(dates, index=df.index)
        cum_tpv = tpv.groupby(date_series).cumsum()
        cum_vol = df['volume'].groupby(date_series).cumsum()
    else:
        # No datetime index — treat entire series as a single trading day
        cum_tpv = tpv.cumsum()
        cum_vol = df['volume'].cumsum()

    vwap = cum_tpv / cum_vol
    return vwap.rename('vwap')


def compute_rsi(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    RSI using Wilder's smoothing.

    RSI = 100 - 100 / (1 + avg_gain / avg_loss)

    Wilder's alpha = 1/length, equivalent to ewm(com=length-1, adjust=False).
    The first bar has no delta, so RSI[0] is NaN.
    When avg_loss is 0 (all gains), RSI is 100.
    """
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(com=length - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=length - 1, adjust=False).mean()

    # Avoid division by zero; where avg_loss=0 RSI=100
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)

    # Index 0 has no delta — force NaN regardless of fill above
    rsi.iloc[0] = np.nan
    return rsi.rename('rsi')


def compute_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """
    MACD indicator.

    Returns DataFrame with columns: macd, signal, histogram.
    """
    ema_fast = compute_ema(df['close'], fast)
    ema_slow = compute_ema(df['close'], slow)
    macd_line = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {'macd': macd_line, 'signal': signal_line, 'histogram': histogram},
        index=df.index,
    )


def compute_roc(df: pd.DataFrame, length: int = 5) -> pd.Series:
    """Rate of change: (price - price.shift(length)) / price.shift(length) * 100."""
    price = df['close']
    roc = (price - price.shift(length)) / price.shift(length) * 100
    return roc.rename('roc')


def compute_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    Average True Range using Wilder's EMA smoothing.

    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = Wilder's EMA(true_range, length)

    Wilder's alpha = 1/length, equivalent to ewm(com=length-1, adjust=False).
    """
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(com=length - 1, adjust=False).mean()
    return atr.rename('atr')


def compute_bollinger_bands(
    df: pd.DataFrame,
    length: int = 20,
    std: float = 2.0,
) -> pd.DataFrame:
    """
    Bollinger Bands.

    Returns DataFrame with columns: upper, middle, lower.
    Middle = SMA(close, length)
    Bands  = middle ± std * rolling_std(close, length)
    Uses sample standard deviation (ddof=1).
    """
    close = df['close']
    middle = close.rolling(length).mean()
    std_dev = close.rolling(length).std(ddof=1)
    upper = middle + std * std_dev
    lower = middle - std * std_dev
    return pd.DataFrame(
        {'upper': upper, 'middle': middle, 'lower': lower},
        index=df.index,
    )


def compute_volume_sma(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    return df['volume'].rolling(length).mean().rename('volume_sma')


def compute_volatility_regime(
    df: pd.DataFrame,
    short: int = 20,
    long: int = 100,
) -> pd.Series:
    """
    Volatility regime ratio: rolling_std(log_returns, short) / rolling_std(log_returns, long).

    > 1.0 → short-term vol exceeds long-term → trending / directional regime
    < 1.0 → short-term vol below long-term  → choppy / mean-reverting regime

    Uses log returns so the ratio is scale-independent.
    Returns NaN where either window hasn't warmed up.
    """
    log_returns = np.log(df['close'] / df['close'].shift(1))
    short_std = log_returns.rolling(short).std()
    long_std  = log_returns.rolling(long).std()
    ratio = short_std / long_std.replace(0.0, np.nan)
    return ratio.rename('vol_regime_ratio')


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def detect_rsi_divergence(
    df: pd.DataFrame,
    rsi: pd.Series,
    lookback: int = 20,
) -> str:
    """
    Detect RSI divergence within the lookback window.

    Compares the last two local price highs against the corresponding RSI
    values at those highs:
    - Bearish: price high[n] > price high[n-1]  AND  rsi[n] < rsi[n-1]
    - Bullish: price high[n] < price high[n-1]  AND  rsi[n] > rsi[n-1]

    Local highs: price[i] > price[i-1] AND price[i] > price[i+1]

    Returns: 'bearish', 'bullish', or 'none'
    """
    prices = df['close'].iloc[-lookback:]
    rsi_vals = rsi.iloc[-lookback:]

    price_highs: list[float] = []
    rsi_highs: list[float] = []

    for i in range(1, len(prices) - 1):
        if prices.iloc[i] > prices.iloc[i - 1] and prices.iloc[i] > prices.iloc[i + 1]:
            price_highs.append(float(prices.iloc[i]))
            rsi_highs.append(float(rsi_vals.iloc[i]))

    if len(price_highs) < 2:
        return 'none'

    if price_highs[-1] > price_highs[-2] and rsi_highs[-1] < rsi_highs[-2]:
        return 'bearish'
    if price_highs[-1] < price_highs[-2] and rsi_highs[-1] > rsi_highs[-2]:
        return 'bullish'
    return 'none'


def detect_macd_cross(macd_df: pd.DataFrame) -> str:
    """
    Classify MACD state based on the last two histogram bars.

    - bullish_cross: histogram crossed from ≤0 to >0
    - bearish_cross: histogram crossed from ≥0 to <0
    - bullish:       histogram is positive (no fresh cross)
    - bearish:       histogram is negative (no fresh cross)

    Returns: 'bullish_cross', 'bullish', 'bearish', or 'bearish_cross'
    """
    hist = macd_df['histogram'].dropna()
    if len(hist) < 2:
        return 'bearish'

    current = float(hist.iloc[-1])
    previous = float(hist.iloc[-2])

    if previous <= 0 and current > 0:
        return 'bullish_cross'
    if previous >= 0 and current < 0:
        return 'bearish_cross'
    if current > 0:
        return 'bullish'
    return 'bearish'


def classify_trend_structure(
    df: pd.DataFrame,
    emas: dict[int, pd.Series],
) -> str:
    """
    Classify EMA alignment as bullish, bearish, or mixed.

    Bullish aligned: each shorter EMA is above the next longer EMA.
    Bearish aligned: each shorter EMA is below the next longer EMA.
    Mixed:           neither condition holds uniformly.

    Args:
        df:   OHLCV DataFrame (used only for length/context)
        emas: dict mapping EMA length → Series, e.g. {9: ..., 20: ..., 50: ..., 200: ...}

    Returns: 'bullish_aligned', 'bearish_aligned', or 'mixed'
    """
    if len(emas) < 2:
        return 'mixed'

    sorted_lengths = sorted(emas.keys())
    current = {length: float(emas[length].iloc[-1]) for length in sorted_lengths}

    bullish = all(
        current[sorted_lengths[i]] > current[sorted_lengths[i + 1]]
        for i in range(len(sorted_lengths) - 1)
    )
    bearish = all(
        current[sorted_lengths[i]] < current[sorted_lengths[i + 1]]
        for i in range(len(sorted_lengths) - 1)
    )

    if bullish:
        return 'bullish_aligned'
    if bearish:
        return 'bearish_aligned'
    return 'mixed'


# ---------------------------------------------------------------------------
# Composite technical score — direction-aware lookup tables
# ---------------------------------------------------------------------------
# Each table maps direction → (signal_value → sub-score).
# Adding a new tradeable direction requires one entry per table; scoring logic
# is unchanged.  "long" is the default and matches the original spec values.

_VWAP_SCORE: dict[str, dict[str, float]] = {
    "long":  {"above": 0.7, "at": 0.5, "below": 0.3},
    "short": {"below": 0.7, "at": 0.5, "above": 0.3},
}
_MACD_SCORE: dict[str, dict[str, float]] = {
    "long":  {"bullish_cross": 0.8, "bullish": 0.6, "bearish": 0.3, "bearish_cross": 0.1},
    "short": {"bearish_cross": 0.8, "bearish": 0.6, "bullish": 0.3, "bullish_cross": 0.1},
}
_TREND_SCORE: dict[str, dict[str, float]] = {
    "long":  {"bullish_aligned": 0.9, "mixed": 0.5, "bearish_aligned": 0.1},
    "short": {"bearish_aligned": 0.9, "mixed": 0.5, "bullish_aligned": 0.1},
}
_BOLLINGER_SCORE: dict[str, dict[str, float]] = {
    "long":  {"upper_half": 0.7, "middle": 0.5, "lower_half": 0.3},
    "short": {"lower_half": 0.7, "middle": 0.5, "upper_half": 0.3},
}
_RSI_DIV_BONUS: dict[str, dict[str, float]] = {
    "long":  {"bullish": 0.1, "bearish": -0.2},
    "short": {"bearish": 0.1, "bullish": -0.2},
}


# ---------------------------------------------------------------------------
# Composite technical score
# ---------------------------------------------------------------------------

def compute_composite_score(signals: dict, direction: str = "long") -> float:
    """
    Compute a composite technical score from individual signal values.

    Weights and scoring rules per spec section 4.4. Returns 0.0–1.0 (clamped).

    ``direction`` controls which side of each signal is favourable.  Pass
    ``"short"`` for short opportunities; all other values default to ``"long"``.
    The score stored by :func:`generate_signal_summary` uses the default
    (long) direction for Claude-context display; the ranker recomputes with
    the actual trade direction before scoring or filtering.

    Expected keys in ``signals``:
        vwap_position     : 'above' | 'at' | 'below'
        rsi               : float (0–100)
        rsi_divergence    : 'bearish' | 'bullish' | False
        macd_signal       : 'bullish_cross' | 'bullish' | 'bearish' | 'bearish_cross'
        trend_structure   : 'bullish_aligned' | 'mixed' | 'bearish_aligned'
        roc_5             : float
        roc_deceleration  : bool
        volume_ratio      : float
        bollinger_position: 'upper_half' | 'middle' | 'lower_half'
    """
    dir_ = direction if direction in _VWAP_SCORE else "long"
    score = 0.0

    # VWAP position — weight 0.20
    score += 0.20 * _VWAP_SCORE[dir_].get(signals.get('vwap_position', 'at'), 0.5)

    # RSI — weight 0.15
    # For shorts, mirror RSI so overbought (70+) maps to the same sub-score as
    # oversold (30-) for longs.  roc_deceleration only fires on positive ROC so
    # the same mirror technique is applied there.
    rsi = float(signals.get('rsi', 50.0))
    effective_rsi = (100.0 - rsi) if dir_ == "short" else rsi
    if effective_rsi < 30:
        rs = 0.7
    elif effective_rsi < 40:
        rs = 0.6
    elif effective_rsi <= 60:
        rs = 0.5
    elif effective_rsi <= 70:
        rs = 0.4
    else:
        rs = 0.3
    score += 0.15 * rs

    # MACD — weight 0.15
    score += 0.15 * _MACD_SCORE[dir_].get(signals.get('macd_signal', 'bearish'), 0.3)

    # Trend structure — weight 0.15
    score += 0.15 * _TREND_SCORE[dir_].get(signals.get('trend_structure', 'mixed'), 0.5)

    # ROC — weight 0.10
    roc_5 = float(signals.get('roc_5', 0.0))
    roc_decel = bool(signals.get('roc_deceleration', False))
    effective_roc = (-roc_5) if dir_ == "short" else roc_5
    if effective_roc > 0 and not roc_decel:
        roc_s = 0.8
    elif effective_roc > 0:
        roc_s = 0.5
    else:
        roc_s = 0.2
    score += 0.10 * roc_s

    # Volume ratio — weight 0.10 (direction-agnostic: both sides need participation)
    vol_ratio = float(signals.get('volume_ratio', 1.0))
    if vol_ratio > 1.5:
        vol_s = 0.8
    elif vol_ratio >= 1.0:
        vol_s = 0.5
    else:
        vol_s = 0.3
    score += 0.10 * vol_s

    # Bollinger position — weight 0.10
    score += 0.10 * _BOLLINGER_SCORE[dir_].get(signals.get('bollinger_position', 'middle'), 0.5)

    # RSI divergence — direct adjustment (not a weighted component)
    rsi_div = signals.get('rsi_divergence', False)
    if isinstance(rsi_div, str):
        score += _RSI_DIV_BONUS[dir_].get(rsi_div, 0.0)

    return float(max(0.0, min(1.0, score)))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_signal_summary(symbol: str, df: pd.DataFrame) -> dict:
    """
    Run all indicators on an OHLCV DataFrame and return a structured signal
    summary per spec section 4.4.

    Args:
        symbol: Ticker symbol (included in output metadata)
        df:     OHLCV DataFrame with lowercase columns

    Returns:
        dict matching the signal output format from the spec.
    """
    # --- Compute indicators ---
    ema_9   = compute_ema(df['close'], 9)
    ema_20  = compute_ema(df['close'], 20)
    ema_50  = compute_ema(df['close'], 50)
    ema_200 = compute_ema(df['close'], 200)

    vwap       = compute_vwap(df)
    rsi        = compute_rsi(df)
    macd_df    = compute_macd(df)
    roc        = compute_roc(df)
    atr        = compute_atr(df)
    bb         = compute_bollinger_bands(df)
    vol_sma    = compute_volume_sma(df)
    vol_regime = compute_volatility_regime(df)

    # --- Extract latest values with NaN-safe defaults ---
    last_close   = float(df['close'].iloc[-1])
    last_vwap    = float(vwap.iloc[-1])   if not pd.isna(vwap.iloc[-1])   else last_close
    last_rsi     = float(rsi.iloc[-1])    if not pd.isna(rsi.iloc[-1])    else 50.0
    last_roc        = float(roc.iloc[-1])        if not pd.isna(roc.iloc[-1])        else 0.0
    last_atr        = float(atr.iloc[-1])        if not pd.isna(atr.iloc[-1])        else 0.0
    last_vol_regime = float(vol_regime.iloc[-1]) if not pd.isna(vol_regime.iloc[-1]) else 1.0
    # Use the last bar with non-zero volume. The current (most recent) bar may be
    # partially formed — yfinance returns it with volume=0 for the first seconds of
    # each new interval, which would make volume_ratio=0 for every symbol at bar
    # boundaries and falsely trigger the RVOL gate. Fall back to iloc[-2] when the
    # last bar has zero volume.
    _raw_last_vol = float(df['volume'].iloc[-1])
    if _raw_last_vol == 0.0 and len(df) >= 2:
        last_vol = float(df['volume'].iloc[-2])
    else:
        last_vol = _raw_last_vol
    # If the 20-bar vol SMA hasn't warmed up yet, use the mean of all available
    # bars as a proxy rather than last_vol (which would always produce ratio=1.0
    # and mask real spikes/collapses during warm-up).
    if not pd.isna(vol_sma.iloc[-1]):
        last_vol_sma = float(vol_sma.iloc[-1])
    else:
        available_mean = float(df['volume'].mean())
        last_vol_sma = available_mean if available_mean > 0 else last_vol
    # If the 20-bar Bollinger middle hasn't warmed up yet, use the mean of all
    # available close prices so the position signal reflects actual price location
    # rather than always defaulting to 'upper_half'.
    if not pd.isna(bb['middle'].iloc[-1]):
        last_bb_mid = float(bb['middle'].iloc[-1])
    else:
        last_bb_mid = float(df['close'].mean())

    # --- VWAP position ---
    threshold = last_vwap * 0.001   # 0.1% band around VWAP counts as "at"
    if last_close > last_vwap + threshold:
        vwap_position = 'above'
    elif last_close < last_vwap - threshold:
        vwap_position = 'below'
    else:
        vwap_position = 'at'

    # --- RSI divergence (store as string or False for composite scoring) ---
    rsi_div = detect_rsi_divergence(df, rsi)
    rsi_divergence = rsi_div if rsi_div != 'none' else False

    # --- MACD signal ---
    macd_signal = detect_macd_cross(macd_df)

    # --- Trend structure (only include EMAs with valid last values) ---
    ema_map: dict[int, pd.Series] = {}
    for length, series in [(9, ema_9), (20, ema_20), (50, ema_50), (200, ema_200)]:
        if not pd.isna(series.iloc[-1]):
            ema_map[length] = series
    trend_structure = classify_trend_structure(df, ema_map)

    # --- ROC deceleration (positive momentum slowing) ---
    roc_deceleration = False
    roc_clean = roc.dropna()
    if len(roc_clean) >= 2:
        prev_roc = float(roc_clean.iloc[-2])
        if last_roc > 0 and prev_roc > 0 and last_roc < prev_roc:
            roc_deceleration = True

    # --- Volume ratio vs SMA ---
    volume_ratio = last_vol / last_vol_sma if last_vol_sma > 0 else 1.0

    # --- Bollinger position ---
    bollinger_position = 'upper_half' if last_close >= last_bb_mid else 'lower_half'

    avg_daily_volume = float(df['volume'].mean())

    signals = {
        'vwap_position':    vwap_position,
        'rsi':              round(last_rsi, 2),
        'rsi_divergence':   rsi_divergence,
        'macd_signal':      macd_signal,
        'trend_structure':  trend_structure,
        'roc_5':            round(last_roc, 4),
        'roc_deceleration': roc_deceleration,
        'volume_ratio':     round(volume_ratio, 4),
        'atr_14':           round(last_atr, 4),
        'bollinger_position': bollinger_position,
        'price':            round(last_close, 4),
        'avg_daily_volume': round(avg_daily_volume, 0),
        'vol_regime_ratio': round(last_vol_regime, 4),
    }

    return {
        'symbol':                   symbol,
        'timestamp':                datetime.now(timezone.utc).isoformat(),
        'signals':                  signals,
        'composite_technical_score': round(compute_composite_score(signals), 4),
        'bars_available':           len(df),  # raw bar count; used by orchestrator warm-up guard
    }
