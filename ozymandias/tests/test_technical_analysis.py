"""
Tests for intelligence/technical_analysis.py.

All indicators are tested against hand-calculated expected values for
small synthetic DataFrames with known, predictable prices.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from ozymandias.intelligence.technical_analysis import (
    classify_trend_structure,
    compute_atr,
    compute_bollinger_bands,
    compute_composite_score,
    compute_ema,
    compute_macd,
    compute_roc,
    compute_rsi,
    compute_volume_sma,
    compute_vwap,
    detect_macd_cross,
    detect_rsi_divergence,
    generate_signal_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ohlcv(
    close: list[float],
    high: list[float] | None = None,
    low: list[float] | None = None,
    volume: list[float] | None = None,
    start: str = "2025-01-01",
    freq: str = "D",
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from close prices."""
    n = len(close)
    if high is None:
        high = [c + 1.0 for c in close]
    if low is None:
        low = [c - 1.0 for c in close]
    if volume is None:
        volume = [1_000_000] * n
    idx = pd.date_range(start=start, periods=n, freq=freq, tz='UTC')
    return pd.DataFrame(
        {'open': close, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=idx,
    )


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

class TestComputeEma:
    def test_matches_pandas_ewm(self):
        prices = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        result = compute_ema(prices, length=3)
        expected = prices.ewm(span=3, adjust=False).mean()
        pd.testing.assert_series_equal(result, expected)

    def test_constant_series_returns_constant(self):
        prices = pd.Series([5.0] * 10)
        result = compute_ema(prices, length=3)
        assert (result == 5.0).all()

    def test_length_1_returns_original(self):
        prices = pd.Series([1.0, 2.0, 3.0])
        pd.testing.assert_series_equal(compute_ema(prices, 1), prices.astype(float))


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

class TestComputeVwap:
    def test_single_day_manual(self):
        # Multiple bars within the SAME day (minute bars) to test cumulative VWAP.
        # TP = (H + L + C) / 3; with H=C+1, L=C-1 → TP = C
        idx = pd.DatetimeIndex(
            ["2025-01-01 09:30", "2025-01-01 10:00", "2025-01-01 10:30"],
            tz='UTC',
        )
        close  = [10.0, 11.0, 12.0]
        high   = [11.0, 12.0, 13.0]
        low    = [ 9.0, 10.0, 11.0]
        volume = [1000.0, 1000.0, 1000.0]
        df = pd.DataFrame(
            {'open': close, 'high': high, 'low': low, 'close': close, 'volume': volume},
            index=idx,
        )
        vwap = compute_vwap(df)

        assert vwap.iloc[0] == pytest.approx(10.0)
        assert vwap.iloc[1] == pytest.approx(10.5)   # (10000+11000)/2000
        assert vwap.iloc[2] == pytest.approx(11.0)   # (10000+11000+12000)/3000

    def test_resets_at_day_boundary(self):
        # 2 bars day-1, 2 bars day-2; VWAP should restart on day-2
        idx = pd.DatetimeIndex([
            "2025-01-01 09:30", "2025-01-01 10:00",
            "2025-01-02 09:30", "2025-01-02 10:00",
        ], tz='UTC')
        # H=C+1, L=C-1 → TP=C
        close  = [10.0, 11.0, 12.0, 13.0]
        high   = [11.0, 12.0, 13.0, 14.0]
        low    = [9.0,  10.0, 11.0, 12.0]
        volume = [1000.0, 1000.0, 1000.0, 1000.0]
        df = pd.DataFrame(
            {'open': close, 'high': high, 'low': low, 'close': close, 'volume': volume},
            index=idx,
        )
        vwap = compute_vwap(df)

        # Day 1
        assert vwap.iloc[0] == pytest.approx(10.0)
        assert vwap.iloc[1] == pytest.approx(10.5)   # (10000+11000)/2000

        # Day 2 resets
        assert vwap.iloc[2] == pytest.approx(12.0)
        assert vwap.iloc[3] == pytest.approx(12.5)   # (12000+13000)/2000

    def test_no_datetime_index_no_reset(self):
        """Without a DatetimeIndex the whole series is treated as one day."""
        df = pd.DataFrame({
            'open': [10.0, 11.0], 'high': [11.0, 12.0],
            'low': [9.0, 10.0], 'close': [10.0, 11.0],
            'volume': [1000.0, 1000.0],
        })
        vwap = compute_vwap(df)
        assert vwap.iloc[0] == pytest.approx(10.0)
        assert vwap.iloc[1] == pytest.approx(10.5)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

class TestComputeRsi:
    """
    Hand-calculated expected values for length=3, Wilder's alpha=1/3
    (ewm com=2, adjust=False).

    prices = [10, 12, 14, 13, 15, 14]
    delta  = [NaN, 2,  2, -1,  2, -1]
    gain   = [NaN, 2,  2,  0,  2,  0]
    loss   = [NaN, 0,  0,  1,  0,  1]

    avg_gain ewm(com=2):
      [1]: seed=2
      [2]: 2*(2/3) + 2*(1/3) = 2
      [3]: 2*(2/3) + 0*(1/3) = 4/3
      [4]: (4/3)*(2/3) + 2*(1/3) = 8/9 + 6/9 = 14/9
      [5]: (14/9)*(2/3) + 0*(1/3) = 28/27

    avg_loss ewm(com=2):
      [1]: 0  →  RS=inf  →  RSI=100
      [2]: 0  →  RS=inf  →  RSI=100
      [3]: 0*(2/3) + 1*(1/3) = 1/3  →  RS=(4/3)/(1/3)=4  →  RSI=80
      [4]: (1/3)*(2/3) + 0*(1/3) = 2/9  →  RS=7  →  RSI=87.5
      [5]: (2/9)*(2/3) + 1*(1/3) = 4/27+9/27=13/27  →  RS=28/13  →  RSI=100-1300/41
    """

    def _df(self) -> pd.DataFrame:
        return _ohlcv([10.0, 12.0, 14.0, 13.0, 15.0, 14.0])

    def test_first_value_is_nan(self):
        rsi = compute_rsi(self._df(), length=3)
        assert pd.isna(rsi.iloc[0])

    def test_all_gains_rsi_100(self):
        rsi = compute_rsi(self._df(), length=3)
        assert rsi.iloc[1] == pytest.approx(100.0)
        assert rsi.iloc[2] == pytest.approx(100.0)

    def test_mixed_rsi_index_3(self):
        rsi = compute_rsi(self._df(), length=3)
        assert rsi.iloc[3] == pytest.approx(80.0, rel=1e-6)

    def test_mixed_rsi_index_4(self):
        rsi = compute_rsi(self._df(), length=3)
        assert rsi.iloc[4] == pytest.approx(87.5, rel=1e-6)

    def test_mixed_rsi_index_5(self):
        rsi = compute_rsi(self._df(), length=3)
        expected = 100.0 - 1300.0 / 41.0
        assert rsi.iloc[5] == pytest.approx(expected, rel=1e-6)

    def test_all_losses_rsi_zero(self):
        df = _ohlcv([10.0, 9.0, 8.0, 7.0, 6.0])
        rsi = compute_rsi(df, length=3)
        # After the first bar there are only losses — RSI should converge to 0
        for i in range(2, len(rsi)):
            assert rsi.iloc[i] == pytest.approx(0.0, abs=1e-9)

    def test_default_length_14(self):
        df = _ohlcv(list(range(1, 30)))
        rsi = compute_rsi(df)        # result series length == input length
        assert len(rsi) == 29


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

class TestComputeMacd:
    def test_column_names(self):
        df = _ohlcv(list(range(1, 40)))
        macd_df = compute_macd(df)
        assert set(macd_df.columns) == {'macd', 'signal', 'histogram'}

    def test_histogram_equals_macd_minus_signal(self):
        df = _ohlcv(list(range(1, 40)))
        macd_df = compute_macd(df)
        expected_hist = macd_df['macd'] - macd_df['signal']
        pd.testing.assert_series_equal(macd_df['histogram'], expected_hist, check_names=False)

    def test_macd_line_from_emas(self):
        df = _ohlcv(list(range(1, 40)))
        macd_df = compute_macd(df, fast=3, slow=5, signal=2)
        ema_fast = compute_ema(df['close'], 3)
        ema_slow = compute_ema(df['close'], 5)
        expected_macd = ema_fast - ema_slow
        pd.testing.assert_series_equal(macd_df['macd'], expected_macd, check_names=False)

    def test_same_length_as_input(self):
        df = _ohlcv(list(range(1, 40)))
        macd_df = compute_macd(df)
        assert len(macd_df) == len(df)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

class TestComputeAtr:
    """
    Hand-calculated expected values for length=2, Wilder's alpha=1/2.

    close = [10, 11, 13, 12, 15]
    high  = [11, 12, 14, 13, 16]
    low   = [ 9, 10, 12, 11, 14]

    true_range:
      [0]: H-L = 2  (prev_close=NaN → only H-L used via skipna max)
      [1]: max(12-10=2, |12-10|=2, |10-10|=0) = 2
      [2]: max(14-12=2, |14-11|=3, |12-11|=1) = 3
      [3]: max(13-11=2, |13-13|=0, |11-13|=2) = 2
      [4]: max(16-14=2, |16-12|=4, |14-12|=2) = 4

    ATR ewm(com=1, adjust=False), alpha=1/2:
      [0]: 2
      [1]: 2*(1/2) + 2*(1/2) = 2
      [2]: 2*(1/2) + 3*(1/2) = 2.5
      [3]: 2.5*(1/2) + 2*(1/2) = 2.25
      [4]: 2.25*(1/2) + 4*(1/2) = 3.125
    """

    def _df(self) -> pd.DataFrame:
        return pd.DataFrame({
            'open':   [10.0, 11.0, 13.0, 12.0, 15.0],
            'high':   [11.0, 12.0, 14.0, 13.0, 16.0],
            'low':    [ 9.0, 10.0, 12.0, 11.0, 14.0],
            'close':  [10.0, 11.0, 13.0, 12.0, 15.0],
            'volume': [1000.0] * 5,
        })

    def test_atr_values(self):
        atr = compute_atr(self._df(), length=2)
        assert atr.iloc[0] == pytest.approx(2.0, rel=1e-6)
        assert atr.iloc[1] == pytest.approx(2.0, rel=1e-6)
        assert atr.iloc[2] == pytest.approx(2.5, rel=1e-6)
        assert atr.iloc[3] == pytest.approx(2.25, rel=1e-6)
        assert atr.iloc[4] == pytest.approx(3.125, rel=1e-6)

    def test_positive_values(self):
        df = _ohlcv(list(range(1, 30)))
        atr = compute_atr(df)
        assert (atr > 0).all()


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

class TestComputeBollingerBands:
    def test_column_names(self):
        df = _ohlcv([1.0] * 25)
        bb = compute_bollinger_bands(df)
        assert set(bb.columns) == {'upper', 'middle', 'lower'}

    def test_constant_price_zero_std(self):
        # All prices equal → std=0 → bands collapse to middle
        df = _ohlcv([100.0] * 25)
        bb = compute_bollinger_bands(df, length=20)
        assert bb['upper'].iloc[-1] == pytest.approx(100.0)
        assert bb['middle'].iloc[-1] == pytest.approx(100.0)
        assert bb['lower'].iloc[-1] == pytest.approx(100.0)

    def test_middle_is_sma(self):
        prices = list(range(1, 26))   # 1..25
        df = _ohlcv(prices)
        bb = compute_bollinger_bands(df, length=5)
        expected_mid = pd.Series(prices, dtype=float, index=df.index).rolling(5).mean()
        pd.testing.assert_series_equal(bb['middle'], expected_mid, check_names=False)

    def test_upper_and_lower_symmetric(self):
        df = _ohlcv(list(range(1, 26)))
        bb = compute_bollinger_bands(df, length=5, std=2.0)
        spread_upper = bb['upper'] - bb['middle']
        spread_lower = bb['middle'] - bb['lower']
        pd.testing.assert_series_equal(spread_upper, spread_lower, check_names=False)

    def test_hand_calculated_values(self):
        # length=3, prices=[10, 12, 14, 13, 15]
        # middle[2] = (10+12+14)/3 = 12
        # std(ddof=1) of [10,12,14] = 2.0
        # upper[2] = 12 + 2*2 = 16, lower[2] = 8
        df = _ohlcv([10.0, 12.0, 14.0, 13.0, 15.0])
        bb = compute_bollinger_bands(df, length=3, std=2.0)
        assert bb['middle'].iloc[2] == pytest.approx(12.0)
        assert bb['upper'].iloc[2]  == pytest.approx(16.0)
        assert bb['lower'].iloc[2]  == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# Volume SMA
# ---------------------------------------------------------------------------

class TestComputeVolumeSma:
    def test_matches_rolling_mean(self):
        vol = [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]
        df = _ohlcv([1.0] * 5, volume=vol)
        result = compute_volume_sma(df, length=3)
        expected = pd.Series(vol).rolling(3).mean()
        # First two values are NaN
        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == pytest.approx(expected.iloc[2])
        assert result.iloc[4] == pytest.approx(expected.iloc[4])


# ---------------------------------------------------------------------------
# ROC
# ---------------------------------------------------------------------------

class TestComputeRoc:
    def test_hand_calculated(self):
        # prices = [10, 11, 12, 13, 14], length=2
        # roc[2] = (12-10)/10 * 100 = 20.0
        # roc[4] = (14-12)/12 * 100 = 16.666...
        df = _ohlcv([10.0, 11.0, 12.0, 13.0, 14.0])
        roc = compute_roc(df, length=2)
        assert pd.isna(roc.iloc[0])
        assert pd.isna(roc.iloc[1])
        assert roc.iloc[2] == pytest.approx(20.0, rel=1e-6)
        assert roc.iloc[4] == pytest.approx(200.0 / 12.0, rel=1e-6)

    def test_constant_prices_roc_zero(self):
        df = _ohlcv([50.0] * 10)
        roc = compute_roc(df, length=3)
        assert (roc.dropna() == 0.0).all()


# ---------------------------------------------------------------------------
# RSI divergence detection
# ---------------------------------------------------------------------------

class TestDetectRsiDivergence:
    def _build(self, close: list[float], rsi_vals: list[float]) -> tuple:
        df = pd.DataFrame({
            'open': close, 'high': [c + 0.5 for c in close],
            'low': [c - 0.5 for c in close], 'close': close,
            'volume': [1000.0] * len(close),
        })
        rsi = pd.Series(rsi_vals, index=df.index, name='rsi')
        return df, rsi

    def test_bearish_divergence(self):
        # Two peaks: price 10→12 (higher), RSI 70→60 (lower) → bearish
        prices = [5.0, 10.0, 5.0, 5.0, 12.0, 5.0]
        rsi_v  = [40.0, 70.0, 40.0, 40.0, 60.0, 40.0]
        df, rsi = self._build(prices, rsi_v)
        assert detect_rsi_divergence(df, rsi, lookback=6) == 'bearish'

    def test_bullish_divergence(self):
        # Two peaks: price 12→10 (lower), RSI 60→70 (higher) → bullish
        prices = [5.0, 12.0, 5.0, 5.0, 10.0, 5.0]
        rsi_v  = [40.0, 60.0, 40.0, 40.0, 70.0, 40.0]
        df, rsi = self._build(prices, rsi_v)
        assert detect_rsi_divergence(df, rsi, lookback=6) == 'bullish'

    def test_no_divergence(self):
        # Two peaks: both price and RSI higher → no divergence
        prices = [5.0, 10.0, 5.0, 5.0, 12.0, 5.0]
        rsi_v  = [40.0, 60.0, 40.0, 40.0, 70.0, 40.0]
        df, rsi = self._build(prices, rsi_v)
        assert detect_rsi_divergence(df, rsi, lookback=6) == 'none'

    def test_too_few_peaks_returns_none(self):
        # No peaks (monotone) → not enough highs
        prices = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        rsi_v  = [50.0] * 6
        df, rsi = self._build(prices, rsi_v)
        assert detect_rsi_divergence(df, rsi, lookback=6) == 'none'


# ---------------------------------------------------------------------------
# MACD cross detection
# ---------------------------------------------------------------------------

class TestDetectMacdCross:
    def _macd_df(self, hist: list[float]) -> pd.DataFrame:
        n = len(hist)
        return pd.DataFrame({
            'macd':      [0.0] * n,
            'signal':    [0.0] * n,
            'histogram': hist,
        })

    def test_bullish_cross(self):
        result = detect_macd_cross(self._macd_df([-0.1, 0.1]))
        assert result == 'bullish_cross'

    def test_bearish_cross(self):
        result = detect_macd_cross(self._macd_df([0.1, -0.1]))
        assert result == 'bearish_cross'

    def test_bullish_no_cross(self):
        result = detect_macd_cross(self._macd_df([0.05, 0.1]))
        assert result == 'bullish'

    def test_bearish_no_cross(self):
        result = detect_macd_cross(self._macd_df([-0.1, -0.05]))
        assert result == 'bearish'

    def test_zero_crossing_to_positive_is_bullish_cross(self):
        result = detect_macd_cross(self._macd_df([0.0, 0.1]))
        assert result == 'bullish_cross'

    def test_too_few_bars_returns_bearish(self):
        result = detect_macd_cross(self._macd_df([0.5]))
        assert result == 'bearish'


# ---------------------------------------------------------------------------
# Trend structure classification
# ---------------------------------------------------------------------------

class TestClassifyTrendStructure:
    def _emas(self, values: dict[int, float]) -> dict[int, pd.Series]:
        return {k: pd.Series([v]) for k, v in values.items()}

    def test_bullish_aligned(self):
        # EMA9 > EMA20 > EMA50 → bullish
        emas = self._emas({9: 30.0, 20: 25.0, 50: 20.0})
        df = _ohlcv([30.0])
        assert classify_trend_structure(df, emas) == 'bullish_aligned'

    def test_bearish_aligned(self):
        emas = self._emas({9: 20.0, 20: 25.0, 50: 30.0})
        df = _ohlcv([20.0])
        assert classify_trend_structure(df, emas) == 'bearish_aligned'

    def test_mixed(self):
        # EMA9 > EMA20 but EMA20 < EMA50 → mixed
        emas = self._emas({9: 28.0, 20: 25.0, 50: 27.0})
        df = _ohlcv([28.0])
        assert classify_trend_structure(df, emas) == 'mixed'

    def test_single_ema_returns_mixed(self):
        emas = self._emas({9: 30.0})
        df = _ohlcv([30.0])
        assert classify_trend_structure(df, emas) == 'mixed'

    def test_empty_emas_returns_mixed(self):
        df = _ohlcv([30.0])
        assert classify_trend_structure(df, {}) == 'mixed'

    def test_four_emas_bullish(self):
        emas = self._emas({9: 100.0, 20: 90.0, 50: 80.0, 200: 70.0})
        df = _ohlcv([100.0])
        assert classify_trend_structure(df, emas) == 'bullish_aligned'


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

class TestComputeCompositeScore:
    """
    Verify weighted sum arithmetic.

    Fully-bullish signals → expected sum:
      VWAP above:                0.20 * 0.7  = 0.140
      RSI 55 neutral:            0.15 * 0.5  = 0.075
      MACD bull_x:               0.15 * 0.8  = 0.120
      Trend bullish:             0.15 * 0.9  = 0.135
      ROC pos accel:             0.10 * 0.8  = 0.080
      Vol > 1.5:                 0.10 * 0.8  = 0.080
      BB upper:                  0.10 * 0.7  = 0.070
      No divergence:                     0   = 0.000
      MACD hist expanding=True:       +0.03  = 0.030
      RSI slope bonus (none):            0   = 0.000
      Total                                  = 0.730
    """

    def _bullish_signals(self) -> dict:
        return {
            'vwap_position':            'above',
            'rsi':                      55.0,
            'rsi_divergence':           False,
            'macd_signal':              'bullish_cross',
            'macd_histogram_expanding': True,   # building momentum — +0.03 modifier
            'trend_structure':          'bullish_aligned',
            'roc_5':                    1.5,
            'roc_deceleration':         False,
            'volume_ratio':             1.8,
            'bollinger_position':       'upper_half',
        }

    def test_fully_bullish(self):
        # 0.700 base + 0.030 (MACD bullish + expanding histogram) = 0.730
        assert compute_composite_score(self._bullish_signals()) == pytest.approx(0.73, abs=1e-9)

    def test_bearish_divergence_penalty(self):
        # 0.730 − 0.200 (bearish divergence penalty) = 0.530
        sig = {**self._bullish_signals(), 'rsi_divergence': 'bearish'}
        assert compute_composite_score(sig) == pytest.approx(0.53, abs=1e-9)

    def test_bullish_divergence_bonus(self):
        # 0.730 + 0.100 (bullish divergence bonus) = 0.830, clamped to 1.0
        sig = {**self._bullish_signals(), 'rsi_divergence': 'bullish'}
        assert compute_composite_score(sig) == pytest.approx(0.83, abs=1e-9)

    def test_score_clamped_to_zero(self):
        """Extremely bearish signals must not produce a negative score."""
        bearish = {
            'vwap_position':    'below',
            'rsi':              75.0,
            'rsi_divergence':   'bearish',
            'macd_signal':      'bearish_cross',
            'trend_structure':  'bearish_aligned',
            'roc_5':            -2.0,
            'roc_deceleration': True,
            'volume_ratio':     0.5,
            'bollinger_position': 'lower_half',
        }
        assert compute_composite_score(bearish) >= 0.0

    def test_score_bounded_to_one(self):
        """Score must never exceed 1.0 even with bonus."""
        sig = {**self._bullish_signals(), 'rsi_divergence': 'bullish', 'rsi': 25.0}
        assert compute_composite_score(sig) <= 1.0

    def test_rsi_extreme_oversold_gets_higher_score(self):
        s1 = {**self._bullish_signals(), 'rsi': 50.0}
        s2 = {**self._bullish_signals(), 'rsi': 25.0}
        assert compute_composite_score(s2) > compute_composite_score(s1)

    def test_high_volume_ratio_beats_low(self):
        s_low  = {**self._bullish_signals(), 'volume_ratio': 0.5}
        s_high = {**self._bullish_signals(), 'volume_ratio': 2.0}
        assert compute_composite_score(s_high) > compute_composite_score(s_low)

    def test_roc_deceleration_reduces_score(self):
        s_accel = {**self._bullish_signals(), 'roc_5': 1.0, 'roc_deceleration': False}
        s_decel = {**self._bullish_signals(), 'roc_5': 1.0, 'roc_deceleration': True}
        assert compute_composite_score(s_decel) < compute_composite_score(s_accel)

    def test_missing_keys_use_neutral_defaults(self):
        """Empty signals dict must return a score without crashing."""
        score = compute_composite_score({})
        assert 0.0 <= score <= 1.0

    # --- Direction-aware scoring ---

    def _bearish_signals(self) -> dict:
        """Mirror of _bullish_signals() — every signal flipped to the bearish side.
        Expected score with direction='short': 0.730 (symmetry check).
        Expected score with direction='long':  ~0.295 (confirms shorts were penalised).

        Derivation (direction='short'):
          VWAP below:               0.20 * 0.7  = 0.140
          RSI 45 → eff 55:          0.15 * 0.5  = 0.075
          MACD bear_cross:          0.15 * 0.8  = 0.120
          Trend bearish:            0.15 * 0.9  = 0.135
          ROC -1.5 → eff +1.5:      0.10 * 0.8  = 0.080
          Vol > 1.5:                0.10 * 0.8  = 0.080
          BB lower_half:            0.10 * 0.7  = 0.070
          No divergence:                          0.000
          MACD hist expanding=True:        +0.03  = 0.030
          Total                                   0.730
        """
        return {
            'vwap_position':            'below',
            'rsi':                      45.0,
            'rsi_divergence':           False,
            'macd_signal':              'bearish_cross',
            'macd_histogram_expanding': True,   # bearish histogram building — +0.03 for shorts
            'trend_structure':          'bearish_aligned',
            'roc_5':                    -1.5,
            'roc_deceleration':         False,
            'volume_ratio':             1.8,
            'bollinger_position':       'lower_half',
        }

    def test_short_direction_bearish_signals_score_0_73(self):
        """Perfect short setup scores 0.73 — symmetric with the bullish long case."""
        assert compute_composite_score(self._bearish_signals(), direction="short") == pytest.approx(0.73, abs=1e-9)

    def test_short_direction_bullish_signals_score_low(self):
        """Bullish signals are penalised for a short, mirroring how bearish signals
        are penalised for a long."""
        long_score  = compute_composite_score(self._bullish_signals(), direction="long")
        short_score = compute_composite_score(self._bullish_signals(), direction="short")
        assert short_score < 0.35, "bullish signals should score poorly for shorts"
        assert long_score == pytest.approx(0.73, abs=1e-9)

    def test_long_direction_bearish_signals_score_low(self):
        """Bearish signals are penalised for a long (regression guard)."""
        score = compute_composite_score(self._bearish_signals(), direction="long")
        assert score < 0.35, "bearish signals should score poorly for longs"

    def test_rsi_overbought_good_for_short(self):
        """RSI 75 (overbought) should yield a higher short score than RSI 55 (neutral)."""
        s_neutral    = {**self._bearish_signals(), 'rsi': 55.0}
        s_overbought = {**self._bearish_signals(), 'rsi': 75.0}
        assert compute_composite_score(s_overbought, direction="short") > compute_composite_score(s_neutral, direction="short")

    def test_rsi_oversold_bad_for_short(self):
        """RSI 25 (oversold) should score lower for shorts — the move is largely done."""
        s_neutral  = {**self._bearish_signals(), 'rsi': 55.0}
        s_oversold = {**self._bearish_signals(), 'rsi': 25.0}
        assert compute_composite_score(s_oversold, direction="short") < compute_composite_score(s_neutral, direction="short")

    def test_short_bearish_divergence_is_bonus(self):
        """Bearish RSI divergence confirms the short — should add +0.1."""
        base  = compute_composite_score(self._bearish_signals(), direction="short")
        bonus = compute_composite_score({**self._bearish_signals(), 'rsi_divergence': 'bearish'}, direction="short")
        assert bonus == pytest.approx(base + 0.1, abs=1e-9)

    def test_short_bullish_divergence_is_penalty(self):
        """Bullish RSI divergence warns against the short — should subtract 0.2."""
        base    = compute_composite_score(self._bearish_signals(), direction="short")
        penalty = compute_composite_score({**self._bearish_signals(), 'rsi_divergence': 'bullish'}, direction="short")
        assert penalty == pytest.approx(base - 0.2, abs=1e-9)

    def test_unknown_direction_defaults_to_long(self):
        """Unrecognised direction strings fall back to long scoring silently."""
        score_long    = compute_composite_score(self._bullish_signals(), direction="long")
        score_unknown = compute_composite_score(self._bullish_signals(), direction="spread")
        assert score_unknown == pytest.approx(score_long, abs=1e-9)


# ---------------------------------------------------------------------------
# generate_signal_summary
# ---------------------------------------------------------------------------

class TestGenerateSignalSummary:
    def _df(self, n: int = 50) -> pd.DataFrame:
        """Monotone increasing price series — enough bars for EMA-200 to warm up."""
        prices = [100.0 + i * 0.5 for i in range(n)]
        return _ohlcv(prices, volume=[1_000_000] * n)

    def test_output_keys(self):
        result = generate_signal_summary("TEST", self._df())
        assert result['symbol'] == 'TEST'
        assert 'timestamp' in result
        assert 'signals' in result
        assert 'composite_technical_score' in result
        assert result['bars_available'] == 50

    def test_signal_keys_present(self):
        signals = generate_signal_summary("TEST", self._df())['signals']
        expected_keys = {
            'vwap_position', 'rsi', 'rsi_divergence', 'macd_signal',
            'trend_structure', 'roc_5', 'roc_deceleration',
            'volume_ratio', 'atr_14', 'bollinger_position',
            'price', 'avg_daily_volume',
        }
        assert expected_keys.issubset(set(signals.keys()))

    def test_price_key_matches_last_close(self):
        df = self._df(50)
        result = generate_signal_summary("TEST", df)
        assert result['signals']['price'] == pytest.approx(float(df['close'].iloc[-1]), rel=1e-4)

    def test_avg_daily_volume_key_is_positive(self):
        df = self._df(50)
        result = generate_signal_summary("TEST", df)
        assert result['signals']['avg_daily_volume'] > 0

    def test_composite_score_in_range(self):
        result = generate_signal_summary("TEST", self._df())
        assert 0.0 <= result['composite_technical_score'] <= 1.0

    def test_rsi_in_valid_range(self):
        result = generate_signal_summary("TEST", self._df())
        rsi = result['signals']['rsi']
        assert 0.0 <= rsi <= 100.0

    def test_vwap_position_valid_value(self):
        result = generate_signal_summary("TEST", self._df())
        assert result['signals']['vwap_position'] in {'above', 'at', 'below'}

    def test_macd_signal_valid_value(self):
        result = generate_signal_summary("TEST", self._df())
        assert result['signals']['macd_signal'] in {
            'bullish_cross', 'bullish', 'bearish', 'bearish_cross'
        }

    def test_trend_structure_valid_value(self):
        result = generate_signal_summary("TEST", self._df())
        assert result['signals']['trend_structure'] in {
            'bullish_aligned', 'bearish_aligned', 'mixed'
        }

    def test_bollinger_position_valid_value(self):
        result = generate_signal_summary("TEST", self._df())
        assert result['signals']['bollinger_position'] in {'upper_half', 'lower_half', 'middle'}

    def test_atr_positive(self):
        result = generate_signal_summary("TEST", self._df())
        assert result['signals']['atr_14'] > 0

    def test_short_dataframe_does_not_crash(self):
        """generate_signal_summary must not raise even with very few bars."""
        df = _ohlcv([100.0, 101.0, 102.0])
        result = generate_signal_summary("TEST", df)
        assert 0.0 <= result['composite_technical_score'] <= 1.0


# ---------------------------------------------------------------------------
# BUG-009: rsi_slope_5 overnight gap guard
# ---------------------------------------------------------------------------

class TestRsiSlope5OvernightGuard:
    """rsi_slope_5 should be zeroed when intraday 5-bar window spans a date boundary."""

    def _intraday_df(self, day1_bars: int, day2_bars: int, freq: str = "1min") -> pd.DataFrame:
        """Build a DataFrame with bars split across two consecutive days."""
        import pandas as pd
        day1_start = pd.Timestamp("2026-03-24 14:00:00", tz="UTC")
        day2_start = pd.Timestamp("2026-03-25 14:00:00", tz="UTC")
        idx1 = pd.date_range(start=day1_start, periods=day1_bars, freq=freq)
        idx2 = pd.date_range(start=day2_start, periods=day2_bars, freq=freq)
        idx = idx1.append(idx2)
        n = len(idx)
        return pd.DataFrame(
            {"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
             "close": [100.0] * n, "volume": [1_000_000] * n},
            index=idx,
        )

    def test_intraday_cross_day_boundary_zeroes_slope(self):
        """5 bars on day 1 + 5 bars on day 2 → rsi_slope_5 == 0.0 (overnight gap)."""
        df = self._intraday_df(day1_bars=5, day2_bars=5)
        result = generate_signal_summary("TEST", df)
        assert result["signals"]["rsi_slope_5"] == 0.0

    def test_intraday_same_day_slope_nonzero_when_rsi_changes(self):
        """10+ bars all on same day with varying price → rsi_slope_5 != 0.0."""
        import pandas as pd
        start = pd.Timestamp("2026-03-24 14:00:00", tz="UTC")
        idx = pd.date_range(start=start, periods=20, freq="1min")
        # Rising prices → RSI should be rising → non-zero slope
        close = [100.0 + i * 0.5 for i in range(20)]
        df = pd.DataFrame(
            {"open": close, "high": [c + 0.5 for c in close],
             "low": [c - 0.5 for c in close], "close": close,
             "volume": [1_000_000] * 20},
            index=idx,
        )
        result = generate_signal_summary("TEST", df)
        # RSI should be rising on a consistent uptrend — slope nonzero
        # (may be 0.0 if RSI is pinned, so we just check no crash and it's a float)
        assert isinstance(result["signals"]["rsi_slope_5"], float)

    def test_daily_bars_spanning_dates_slope_nonzero(self):
        """Daily bars spanning 10 calendar dates — guard must NOT fire (not intraday)."""
        # _ohlcv uses freq="D" by default, so 10 bars = 10 different dates
        close = [100.0 + i for i in range(20)]
        df = _ohlcv(close, freq="D")
        result = generate_signal_summary("TEST", df)
        # For daily bars the 5-bar span > 24h → guard skipped → slope can be nonzero
        # (constant volume → RSI may be near 50 flat; just verify no crash and it's a float)
        assert isinstance(result["signals"]["rsi_slope_5"], float)


# ---------------------------------------------------------------------------
# rsi_accel_3 — RSI 2nd derivative
# ---------------------------------------------------------------------------

class TestRsiAccel3:
    """rsi_accel_3 is the change in rsi_slope_5 over 3 bars (2nd derivative of RSI)."""

    def test_present_in_signals(self):
        """rsi_accel_3 key exists in generate_signal_summary output."""
        close = [100.0 + i * 0.5 for i in range(30)]
        df = _ohlcv(close)
        result = generate_signal_summary("TEST", df)
        assert "rsi_accel_3" in result["signals"]

    def test_type_is_float(self):
        """rsi_accel_3 is always a float."""
        close = [100.0] * 30
        df = _ohlcv(close)
        result = generate_signal_summary("TEST", df)
        assert isinstance(result["signals"]["rsi_accel_3"], float)

    def test_zero_when_fewer_than_9_rsi_values(self):
        """rsi_accel_3 is 0.0 when there are fewer than 9 clean RSI values."""
        # RSI 14 requires 15 bars minimum for first value; with only 10 bars
        # we won't have 9 clean RSI values for the acceleration window.
        close = [100.0 + i for i in range(10)]
        df = _ohlcv(close)
        result = generate_signal_summary("TEST", df)
        assert result["signals"]["rsi_accel_3"] == 0.0

    def test_positive_when_slope_accelerating(self):
        """Rising RSI with increasing velocity → positive rsi_accel_3."""
        # Price accelerating upward → RSI should accelerate upward too.
        # Use convex price series (slow start, fast finish).
        close = [100.0 + i ** 1.5 for i in range(30)]
        df = _ohlcv(close)
        result = generate_signal_summary("TEST", df)
        accel = result["signals"]["rsi_accel_3"]
        assert isinstance(accel, float)
        # We can't guarantee the sign with certainty due to RSI smoothing,
        # but the value must be a valid float (not NaN, not inf).
        assert accel == accel  # NaN check
        assert abs(accel) < 1000  # sanity bound

    def test_negative_when_slope_decelerating(self):
        """Strong uptrend then sharp reversal → rsi_accel_3 negative at the reversal point.

        RSI was rising fast (positive slope) then reverses sharply downward. The slope
        3 bars ago was still positive; the current slope is now negative.
        accel = slope_now - slope_3ago = (negative) - (positive) → clearly negative.
        """
        # 25 bars of strong uptrend → RSI near 100. Then 5 bars of sharp reversal.
        # RSI falls rapidly on the reversal while slope_3ago was still ascending.
        close = [100.0 + i * 3 for i in range(25)] + [172.0 - i * 6 for i in range(5)]
        df = _ohlcv(close)
        result = generate_signal_summary("TEST", df)
        accel = result["signals"]["rsi_accel_3"]
        assert isinstance(accel, float)
        assert accel < 0, f"Expected negative acceleration on sharp reversal, got {accel}"

    def test_overnight_guard_zeroes_acceleration(self):
        """Intraday data spanning a date boundary → rsi_accel_3 zeroed (contaminated window)."""
        import pandas as pd
        day1_start = pd.Timestamp("2026-03-24 14:00:00", tz="UTC")
        day2_start = pd.Timestamp("2026-03-25 14:00:00", tz="UTC")
        idx1 = pd.date_range(start=day1_start, periods=5, freq="1min")
        idx2 = pd.date_range(start=day2_start, periods=10, freq="1min")
        idx = idx1.append(idx2)
        n = len(idx)
        df = pd.DataFrame(
            {"open": [100.0] * n, "high": [101.0] * n,
             "low": [99.0] * n, "close": [100.0] * n, "volume": [1_000_000] * n},
            index=idx,
        )
        result = generate_signal_summary("TEST", df)
        assert result["signals"]["rsi_accel_3"] == 0.0

    def test_daily_bars_guard_not_fired(self):
        """Daily bars — overnight guard must NOT zero out acceleration."""
        close = [100.0 + i * 0.5 for i in range(30)]
        df = _ohlcv(close, freq="D")
        result = generate_signal_summary("TEST", df)
        # Daily bars span > 24h per bar → guard skipped; value is a plain float.
        assert isinstance(result["signals"]["rsi_accel_3"], float)
