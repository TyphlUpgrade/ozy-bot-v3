# Phase 04: Market Data Adapter + Technical Analysis Module

Read sections 4.2 (External Data Sources) and 4.4 (Technical Analysis) of `ozymandias_v3_spec_revised.md`.

## Context
Phases 01-03 gave us: state management, broker abstraction, fill protection, and PDT guard. We can now safely place and manage orders. This phase adds the data and intelligence that will drive trading decisions.

## What to Build

### 1. Data adapter interfaces (`data/adapters/base.py`)

Define the abstract base classes from section 4.2:
- `DataAdapter` ABC with: `fetch_bars()`, `fetch_quote()`, `fetch_fundamentals()`, `is_available()`
- `SentimentAdapter` ABC with: `poll()`, `get_news()`, `get_calendar_events()`, `is_available()`

Also define the data types these return:
- `Quote` (symbol, bid, ask, last, volume, timestamp)
- `Fundamentals` (market_cap, pe_ratio, sector, industry, avg_volume, etc.)
- `SentimentSignal` (symbol, source, score, timestamp)
- `NewsItem` (headline, source, symbol, published_at, url, sentiment_hint)
- `CalendarEvent` (event_type, date, symbol_or_description)

### 2. yfinance adapter (`data/adapters/yfinance_adapter.py`)

Implement `YFinanceAdapter(DataAdapter)`:
- yfinance is synchronous, so **wrap all calls in `asyncio.to_thread()`** to keep them non-blocking.
- `fetch_bars()`: use `yf.download()` or `Ticker.history()`. Return a pandas DataFrame with columns: open, high, low, close, volume. Normalize column names to lowercase.
- `fetch_quote()`: get latest price data. Map to `Quote` dataclass.
- `fetch_fundamentals()`: use `Ticker.info`. Map to `Fundamentals` dataclass. Handle missing fields gracefully (yfinance doesn't always return everything).
- `is_available()`: make a lightweight test call. Return False on any exception.
- Implement response caching with configurable TTL (default: 30 seconds for quotes, 5 minutes for bars, 1 hour for fundamentals). Use a simple dict cache with timestamp expiry — nothing fancy.
- Log all fetch operations at DEBUG level, failures at WARNING level.

### 3. Data aggregator (`data/aggregator.py`)

A simple manager that holds references to active data adapters and routes requests:
- `get_bars(symbol, interval, period)`: try primary adapter (yfinance), fall back to secondary if available and primary fails.
- `get_quote(symbol)`: same fallback pattern.
- For MVP, this just wraps yfinance. But the fallback structure must exist so adding Alpha Vantage later is just registering a new adapter.

### 4. Technical analysis module (`intelligence/technical_analysis.py`)

**This is all hand-rolled with pandas and numpy. No third-party TA libraries.**

Implement every indicator from section 4.4's table. Each indicator is a standalone pure function that takes a DataFrame and returns a Series (or DataFrame for multi-output indicators).

```python
def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP = cumsum(price * volume) / cumsum(volume). Resets daily."""
    ...

def compute_rsi(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """RSI using Wilder's smoothing. 100 - 100/(1 + avg_gain/avg_loss)."""
    ...

def compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Returns DataFrame with columns: macd, signal, histogram."""
    ...

def compute_ema(series: pd.Series, length: int) -> pd.Series:
    """EMA using pandas ewm(span=length, adjust=False)."""
    ...

def compute_roc(df: pd.DataFrame, length: int = 5) -> pd.Series:
    """Rate of change: (price - price.shift(length)) / price.shift(length) * 100."""
    ...

def compute_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average True Range using EMA smoothing."""
    ...

def compute_bollinger_bands(df: pd.DataFrame, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    """Returns DataFrame with columns: upper, middle, lower."""
    ...

def compute_volume_sma(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    ...
```

Then implement the signal detection functions:
- `detect_rsi_divergence(df, rsi, lookback=20)`: detect bearish and bullish divergences per the spec's description.
- `detect_macd_cross(macd_df)`: detect bullish/bearish crossovers.
- `classify_trend_structure(df, emas)`: check if EMAs (9, 20, 50, 200) are bullishly aligned, bearishly aligned, or mixed.

Finally, implement the composite technical score:
- `compute_composite_score(signals: dict) -> float`: Apply the weighted scoring from section 4.4's table. Return 0.0-1.0.
- `generate_signal_summary(symbol, df) -> dict`: Run all indicators, detect all signals, compute composite score. Return the signal output format from section 4.4.

## Tests to Write

Create `tests/test_technical_analysis.py` — test each indicator against known values:
- Create a small synthetic DataFrame with known prices (e.g., 30 bars of predictable data).
- For RSI: compute expected RSI by hand for the synthetic data. Verify the function matches within floating point tolerance.
- For EMA: verify against `pandas.ewm` (since that's what we use, this is more of a sanity check on the wrapper).
- For MACD: verify the MACD line, signal line, and histogram against hand-calculated values.
- For VWAP: verify against manual cumulative calculation. Test that it resets at day boundaries.
- For ATR: verify against the manual true range → EMA pipeline.
- For RSI divergence: create a price series with a known bearish divergence (higher price high, lower RSI high) and verify detection.
- For composite score: feed known signal values and verify the weighted sum matches.

Create `tests/test_yfinance_adapter.py`:
- Mock yfinance calls (do NOT hit the real API in tests)
- Test that DataFrame column names are normalized
- Test cache behavior (second call within TTL returns cached data)
- Test `is_available()` returns False on exception
- Test `asyncio.to_thread` wrapping works correctly

## Done When
- All tests pass
- You can run `generate_signal_summary("AAPL", df)` with real data from yfinance and get a sensible output dict
- Every indicator function is individually testable and tested
