# Ozymandias v3 — Automated Trading Bot Specification

**Revision:** 3.3 (revised: Alpaca broker, cost optimization, custom TA)
**Status:** Pre-implementation — designed for Claude Code buildout
**Target Platform:** Python 3.12+, asyncio concurrency, Alpaca API paper trading (initial), modular broker swap

---

## 1. System overview

Ozymandias is an automated stock trading bot that uses Claude API for institutional-grade trade reasoning, combined with quantitative technical analysis for execution and risk management. It targets aggressive momentum and swing trading strategies on high-volatility, high-liquidity equities.

The system is designed with strict modularity at every boundary: broker API, strategy logic, data sources, and AI reasoning are all independently swappable components. This protects against the high cost of broker migration and allows strategy iteration without full-system rewrites.

Options trading is not implemented in this version, but the architecture must accommodate future options strategy modules without structural changes.

### Implementation constraints

- **Concurrency model:** asyncio throughout. All loops, API calls, and I/O use async/await. No threading or multiprocessing unless explicitly noted.
- **Technical analysis:** Hand-rolled indicator calculations using pandas and numpy only. Do not use pandas-ta (incompatible with Python 3.14), ta-lib (C dependency complicates deployment), or any third-party TA library. The indicator set is finite and well-defined — pure pandas/numpy implementations are ~150 lines total, have zero dependency risk, and are trivially testable against known values. All indicator functions live in a single module (`intelligence/technical_analysis.py`) with unit tests that verify output against manually calculated expected values.
- **Timezone handling:** All internal timestamps are UTC. All market-hours logic converts to US/Eastern before comparison. Use `zoneinfo.ZoneInfo("America/New_York")` (stdlib, no pytz). Every function that deals with market hours must accept or derive the current time in ET explicitly — never rely on the local system clock's timezone.
- **JSON parsing from Claude:** Claude API responses that expect structured JSON must be parsed defensively. Expect malformed JSON on ~5% of calls. Implement: (1) strip markdown code fences, (2) attempt `json.loads`, (3) on failure, attempt regex extraction of the JSON object, (4) on second failure, log the raw response and skip the cycle. Never crash on bad Claude output.
- **Python version:** 3.12+ (compatible through 3.14). Uses `TaskGroup`, `ExceptionGroup`, and modern typing features.

---

## 2. Architecture diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                        EXTERNAL DATA SOURCES                               ║
║                  (non-brokerage, modular adapters)                          ║
║                                                                            ║
║  ┌─────────────────────────────┐   ┌──────────────────────────────────┐    ║
║  │     Market Data Adapters    │   │    News / Sentiment Adapters     │    ║
║  │                             │   │                                  │    ║
║  │  MVP:                       │   │  MVP:                            │    ║
║  │  - yfinance (bars, fundmtl) │   │  - Finnhub (news, earnings,     │    ║
║  │                             │   │    insider transactions)        │    ║
║  │  Post-MVP:                  │   │                                  │    ║
║  │  - Alpha Vantage (tech ind) │   │  Post-MVP:                      │    ║
║  │  - SEC EDGAR (filings)      │   │  - Reddit/PRAW (WSB, r/stocks)  │    ║
║  │                             │   │  - Stocktwits (sentiment)       │    ║
║  │  Common interface:          │   │  - RSS feeds (general news)     │    ║
║  │  DataAdapter.fetch()        │   │  - Econ calendar (events)       │    ║
║  │  DataAdapter.subscribe()    │   │                                  │    ║
║  └──────────────┬──────────────┘   │  Common interface:               │    ║
║                 │                   │  SentimentAdapter.poll()         │    ║
║                 │                   └───────────────┬──────────────────┘    ║
╚═════════════════╪═══════════════════════════════════╪══════════════════════╝
                  │                                   │
                  ▼                                   ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                      ORCHESTRATOR / SCHEDULER                              ║
║                      (asyncio event loop)                                  ║
║                                                                            ║
║  Three async task groups (all run concurrently):                            ║
║                                                                            ║
║  FAST LOOP (5-15 sec)        MED LOOP (1-5 min)     SLOW LOOP (15-60 min) ║
║  ┌─────────────────────┐    ┌──────────────────┐    ┌───────────────────┐  ║
║  │ - Order fill monitor│    │ - Technical scan  │    │ - Claude AI       │  ║
║  │ - Fill protection   │    │ - Signal detect   │    │   reasoning cycle │  ║
║  │ - Quant overrides   │    │ - Opportunity     │    │ - Watchlist prune │  ║
║  │ - PDT guard check   │    │   re-ranking      │    │ - News digest     │  ║
║  │ - Position sync     │    │ - Position re-eval│    │ - Thesis review   │  ║
║  └─────────┬───────────┘    └────────┬─────────┘    └─────────┬─────────┘  ║
║            │                         │                        │             ║
╚════════════╪═════════════════════════╪════════════════════════╪═════════════╝
             │                         │                        │
             ▼                         ▼                        ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                        INTELLIGENCE LAYER                                  ║
║                                                                            ║
║  ┌────────────────────────┐  ┌──────────────────┐  ┌───────────────────┐  ║
║  │   Claude AI Reasoning  │  │ Technical Analysis│  │ Opportunity Ranker│  ║
║  │                        │  │  (pandas + numpy) │  │                   │  ║
║  │ - Thesis generation    │──▶ - VWAP crossover  │──▶ - Composite score │  ║
║  │ - Watchlist mgmt (≤40) │  │ - RSI / RSI div.  │  │ - AI weight       │  ║
║  │ - Position review      │  │ - ROC decel.      │  │ - Technical weight│  ║
║  │ - Catalyst evaluation  │  │ - Vol-wtd momentum│  │ - Risk-adjusted   │  ║
║  │ - Short/med term split │  │ - Moving averages │  │ - Position sizing │  ║
║  │                        │  │ - Volume profile   │  │                   │  ║
║  │ Output: compact JSON   │  │                   │  │ Output: ranked    │  ║
║  │ (trade_reasoning.json) │  │ Output: signals   │  │ trade queue       │  ║
║  └────────────────────────┘  └──────────────────┘  └─────────┬─────────┘  ║
║                                                               │            ║
╚═══════════════════════════════════════════════════════════════╪════════════╝
                                                                │
                                                                ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                         EXECUTION LAYER                                    ║
║                                                                            ║
║  ┌────────────────────────┐  ┌──────────────────┐  ┌───────────────────┐  ║
║  │   Risk Management      │  │ Broker Abstraction│  │  Broker Impl.     │  ║
║  │                        │  │    (Interface)     │  │                   │  ║
║  │ - PDT day-trade counter│  │                   │  │  ┌─────────────┐  │  ║
║  │ - Max position size    │──▶ - place_order()   │──▶ │ Alpaca      │  │  ║
║  │   (15-20% portfolio)   │  │ - cancel_order()  │  │  │ (paper)     │  │  ║
║  │ - Max concurrent (5-8) │  │ - get_positions() │  │  └─────────────┘  │  ║
║  │ - Per-trade max loss   │  │ - get_orders()    │  │  ┌─────────────┐  │  ║
║  │ - Drawdown circuit brkr│  │ - get_account()   │  │  │ Future:     │  │  ║
║  │ - Market hours aware   │  │ - get_fills()     │  │  │ IBKR/other  │  │  ║
║  │ - Buying power check   │  │                   │  │  └─────────────┘  │  ║
║  └────────────────────────┘  └──────────────────┘  └───────────────────┘  ║
║                                                                            ║
╚════════════════════════════════════════════════════════════════════════════╝
             │                         │                        │
             ▼                         ▼                        ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                     PERSISTENT STATE (JSON files)                          ║
║                                                                            ║
║  ┌──────────────┐ ┌──────────────┐ ┌────────────┐ ┌──────────────────┐   ║
║  │  Portfolio    │ │  Watchlist   │ │  Config    │ │  Logging         │   ║
║  │              │ │              │ │            │ │                  │   ║
║  │ - Positions  │ │ - Up to 40   │ │ - Settings │ │ - current.log   │   ║
║  │ - Intentions │ │   tickers    │ │ - Params   │ │ - previous.log  │   ║
║  │   per trade: │ │ - Priority   │ │ - API keys │ │ - Rotate on     │   ║
║  │   catalyst,  │ │   tiers      │ │ - Prompt   │ │   restart       │   ║
║  │   direction, │ │ - Add/prune  │ │   versions │ │                  │   ║
║  │   exp. move, │ │   timestamps │ │ - Strategy │ │                  │   ║
║  │   AI reason, │ │              │ │   params   │ │                  │   ║
║  │   exit tgts, │ │              │ │            │ │                  │   ║
║  │   max loss   │ │              │ │            │ │                  │   ║
║  └──────────────┘ └──────────────┘ └────────────┘ └──────────────────┘   ║
║                                                                            ║
║  ◄──── All state fed back to Orchestrator on each relevant loop cycle ───► ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### Architecture notes for implementation

The data flow is top-down for new information, bottom-up for state feedback. Every module communicates via well-defined interfaces and JSON payloads, never by direct coupling. The orchestrator is the only component that knows about all other components; individual modules do not reference each other.

All async I/O (broker calls, Claude API calls, data adapter fetches) must use `asyncio` and `aiohttp` (not `requests`). The orchestrator runs the three loops as concurrent `asyncio.Task` instances within a `TaskGroup`.

---

## 3. MVP vs. post-MVP scope

To prevent scope creep during initial implementation, the following delineates what must be built first versus what can be added incrementally later.

### MVP (build first)

- State management (portfolio, watchlist, orders, config) + logging + reasoning cache
- Broker abstraction + Alpaca paper trading implementation
- Order state machine + fill protection
- Risk manager + PDT guard
- yfinance market data adapter (the only data adapter needed for MVP)
- Technical analysis module (all indicators hand-rolled with pandas/numpy)
- Claude reasoning integration (with defensive JSON parsing)
- Opportunity ranker
- Momentum strategy + swing strategy
- Orchestrator wiring all three loops
- Market hours awareness with proper ET timezone handling

### Post-MVP (add incrementally after MVP works end-to-end)

- Alpha Vantage adapter (supplement to yfinance)
- Finnhub news/earnings adapter
- Reddit/PRAW sentiment adapter
- Stocktwits sentiment adapter
- SEC EDGAR filings adapter
- RSS feed adapter
- Economic calendar adapter
- Backtesting harness + SimulatedBroker
- Alpaca websocket streaming for order updates
- Notification system (critical event alerts)
- Options strategy modules

---

## 4. Module specifications

### 4.1 Orchestrator / scheduler

The orchestrator is the central loop manager. It runs three concurrent async tasks at different cadences, each responsible for a distinct set of concerns.

**Fast loop (every 5-15 seconds):**
- Poll broker for order status updates and reconcile with local order state machine.
- Execute fill protection logic (see section 7.1).
- Check quantitative override signals on open positions and execute hard exits if triggered.
- Verify PDT day-trade count has not exceeded safe threshold.
- Sync local position state with broker-reported positions.

**Medium loop (every 1-5 minutes):**
- Run technical indicator scans across the watchlist.
- Detect entry and exit signals from technical analysis module.
- Re-rank the opportunity queue using the latest AI reasoning output and fresh technical data.
- Re-evaluate open positions against their recorded exit targets and stop-loss levels.

**Slow loop (event-driven, checked every 5 minutes, Claude called only when triggered):**

The slow loop runs a check every 5 minutes but does NOT call Claude on every cycle. Instead, it evaluates whether any **trigger condition** has been met since the last Claude reasoning call. If no trigger is met, the loop is a no-op (just updates the trigger state). This reduces Claude API calls from ~26/day to ~8-12/day, cutting costs by 50-70%.

**Trigger conditions (any one triggers a Claude reasoning cycle):**
- **Time ceiling:** At least 60 minutes have elapsed since the last Claude call. This ensures the system gets fresh strategic reasoning at least every hour during market hours, even in quiet markets.
- **Price move threshold:** Any Tier 1 watchlist symbol or open position has moved more than 2% since the last Claude evaluation.
- **New catalyst detected:** A news/sentiment adapter has flagged a new catalyst (earnings release, insider filing, major news) for any Tier 1 symbol. (Post-MVP; in MVP, this trigger is inactive.)
- **Position approaching target:** Any open position is within 1% of its profit target or stop-loss price.
- **Override exit occurred:** A quantitative override signal forced an exit since the last Claude call. Claude must evaluate whether to re-enter.
- **Market session transition:** The market just opened (9:30 AM ET) or is approaching close (3:30 PM ET). These are high-value reasoning moments.
- **Watchlist is empty or critically small:** Fewer than 10 tickers on the watchlist. Claude needs to populate it.

**When triggered, the slow loop:**
- Assembles context and invokes Claude API reasoning cycle.
- Prunes and appends to the watchlist based on Claude's analysis.
- Digests new news catalysts (earnings, lawsuits, insider moves, macro events).
- Reviews and updates thesis for each open position.
- Identifies new short-term and medium-short-term opportunities.
- Caches the reasoning response (see section 5.5).

**Claude reasoning calls are async and non-blocking.** The slow loop fires the Claude API request and `await`s the response. While waiting, the fast and medium loops continue operating. If a Claude call is already in-flight when a new trigger fires, skip — do not queue multiple concurrent Claude requests.

**Graceful degradation rules:**

| Dependency | Failure mode | Fallback |
|-----------|-------------|----------|
| Claude API | Timeout / 5xx / rate limit | Quantitative-only mode: no new AI-driven entries, continue position management with technical signals and override rules. Retry with exponential backoff (base 30s, max 10min). |
| yfinance | Rate limit / unavailable | Fall back to Alpha Vantage if implemented. If no data source available, use broker's position data for open position monitoring only. Halt new entries. |
| News/sentiment APIs | Rate limit / unavailable | Skip enrichment for this cycle. Log the gap. Claude operates with stale or absent news context. |
| Broker API | Timeout / auth failure | Log error. Retry with exponential backoff (base 5s, max 5min). If unreachable for > 5 min continuously, enter safe mode: no new orders, log alert. |
| Local state files | Corruption / missing | On startup, validate JSON integrity with schema checks. If corrupted, refuse to start and require manual intervention. Never trade with uncertain state. |

### 4.2 External data sources

All market data and news/sentiment data must come from non-brokerage sources, accessed through a common adapter interface. Each adapter implements a standard async interface so sources can be swapped, added, or removed without touching any other module.

**Market data adapter interface:**
```python
from abc import ABC, abstractmethod
from pandas import DataFrame
from dataclasses import dataclass

class DataAdapter(ABC):
    @abstractmethod
    async def fetch_bars(self, symbol: str, interval: str, period: str) -> DataFrame:
        """Return OHLCV DataFrame. Interval: '1m','5m','1h','1d'. Period: '1d','5d','1mo'."""
        ...

    @abstractmethod
    async def fetch_quote(self, symbol: str) -> Quote:
        """Return latest quote with bid/ask/last/volume."""
        ...

    @abstractmethod
    async def fetch_fundamentals(self, symbol: str) -> Fundamentals:
        """Return market cap, P/E, sector, etc."""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """Health check. Return False if rate-limited or unreachable."""
        ...
```

**Sentiment adapter interface:**
```python
class SentimentAdapter(ABC):
    @abstractmethod
    async def poll(self, symbols: list[str]) -> list[SentimentSignal]:
        ...

    @abstractmethod
    async def get_news(self, symbol: str, since: datetime) -> list[NewsItem]:
        ...

    @abstractmethod
    async def get_calendar_events(self, date_range: tuple[date, date]) -> list[CalendarEvent]:
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        ...
```

**MVP implementation: yfinance only.**

| Source | Purpose | Free tier limits | Notes |
|--------|---------|-----------------|-------|
| yfinance | Price bars, fundamentals, historical data | Unlimited (unofficial) | No API key needed; primary market data source. Use `yfinance` synchronous calls wrapped in `asyncio.to_thread()` since the library is not natively async. |

**Post-MVP data sources (add incrementally):**

| Source | Purpose | Free tier limits | Notes |
|--------|---------|-----------------|-------|
| Alpha Vantage | Technical indicators, intraday data | 25 calls/min, 500/day | API key required (free); good supplement/fallback for yfinance |
| Finnhub | Company news, earnings calendar, insider transactions | 60 calls/min | Best single free source for catalysts; API key required |
| Reddit/PRAW | Retail sentiment from r/wallstreetbets, r/stocks | Reddit API free tier | OAuth required; parse for ticker mentions and sentiment |
| Stocktwits | Trending symbols, message sentiment | Free tier available | Good for retail momentum signals |
| SEC EDGAR | Insider trading filings, institutional moves (13F) | Unlimited (public) | No key needed; parse XBRL filings |
| RSS feeds | General market news, macro events | Unlimited | Bloomberg, Reuters, CNBC RSS feeds |
| Trading Economics | Economic calendar, macro data | Limited free tier | Alternative: scrape Investing.com calendar |

Each adapter must implement retry logic with exponential backoff, rate-limit awareness (track remaining calls and throttle proactively), and response caching with configurable TTL to avoid redundant calls. If a source is unavailable, the system logs the failure and continues with remaining sources.

**Future paid upgrade paths:** Polygon.io (real-time data), Benzinga (premium news), Quiver Quantitative (congressional trading, lobbying data), Unusual Whales (options flow for when options are implemented).

### 4.3 Claude AI reasoning module

Claude is the strategic intelligence layer. It receives structured context and returns compact JSON trade reasoning that feeds the opportunity ranker.

**Design principles for Claude integration:**
- Token efficiency is paramount. Each reasoning cycle should use the minimum context necessary.
- Batch tickers into priority tiers: Tier 1 (current positions + top watchlist candidates, full context), Tier 2 (remaining watchlist, minimal context). Only Tier 1 goes to Claude each cycle.
- Claude prompt templates are versioned files stored in `config/prompts/`, not hardcoded strings. Small prompt changes can radically alter trade behavior; treat them as code with version control.
- Target 4,000-8,000 input tokens per reasoning cycle. With event-driven triggering (~8-12 calls/day instead of ~26), estimated daily cost drops to ~$5-15/day.

**Defensive JSON parsing (required):**

Claude will occasionally return malformed JSON (~5% of calls). The parsing pipeline must be:

```python
import json
import re

def parse_claude_response(raw_text: str) -> dict | None:
    """Parse Claude's response into structured JSON. Returns None on failure."""
    # Step 1: Strip markdown code fences
    cleaned = re.sub(r'```(?:json)?\s*', '', raw_text).strip()
    cleaned = re.sub(r'```\s*$', '', cleaned).strip()

    # Step 2: Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Step 3: Try to extract JSON object via regex
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Step 4: Log failure, return None (caller skips this cycle)
    return None
```

**Input context sent to Claude each cycle:**
```json
{
  "portfolio": {
    "cash": 28500.00,
    "buying_power": 28500.00,
    "positions": [
      {
        "symbol": "NVDA",
        "shares": 15,
        "avg_cost": 142.30,
        "current_price": 148.50,
        "unrealized_pnl": 93.00,
        "intention": {
          "catalyst": "AI chip demand cycle, data center buildout",
          "direction": "long",
          "strategy": "momentum",
          "expected_move": "+8-12% over 2 weeks",
          "reasoning": "Institutional accumulation pattern, RSI not yet overbought",
          "exit_targets": { "profit_target": 155.00, "stop_loss": 138.00 },
          "max_expected_loss": -64.50,
          "entry_date": "2025-03-10",
          "review_notes": []
        }
      }
    ]
  },
  "watchlist_tier1": [
    {
      "symbol": "TSLA",
      "latest_price": 245.80,
      "day_change_pct": 3.2,
      "volume_vs_avg": 1.8,
      "technical_summary": "Above 20 EMA, RSI 62, MACD bullish cross",
      "recent_catalysts": ["Q4 delivery beat", "FSD v13 rollout"],
      "sentiment_score": 0.72
    }
  ],
  "market_context": {
    "spy_trend": "bullish, above 50 SMA",
    "vix": 16.5,
    "sector_rotation": "tech outperforming, energy lagging",
    "macro_events_today": ["Fed speakers at 2pm", "CPI data tomorrow"],
    "trading_session": "regular_hours",
    "pdt_trades_remaining": 2
  }
}
```

**Expected output from Claude:**
```json
{
  "timestamp": "2025-03-11T14:30:00Z",
  "position_reviews": [
    {
      "symbol": "NVDA",
      "action": "hold",
      "thesis_intact": true,
      "updated_reasoning": "Momentum still strong, volume confirming. Hold for target.",
      "adjusted_targets": null
    }
  ],
  "new_opportunities": [
    {
      "symbol": "TSLA",
      "action": "buy",
      "strategy": "momentum",
      "timeframe": "short_term",
      "conviction": 0.78,
      "reasoning": "Delivery beat catalyst + FSD momentum. Technical breakout above resistance.",
      "suggested_entry": 244.00,
      "suggested_exit": 268.00,
      "suggested_stop": 235.00,
      "position_size_pct": 0.12
    }
  ],
  "watchlist_changes": {
    "add": ["PLTR", "COIN"],
    "remove": ["XOM"],
    "rationale": "Rotating out of energy into high-beta tech on momentum shift"
  },
  "market_assessment": "Bullish bias, but CPI tomorrow is a risk event. Size positions conservatively until data clears."
}
```

**Claude focus areas:**
- Short-term opportunities (profit in days): primary focus, majority of analysis bandwidth.
- Medium-short-term opportunities (profit in weeks/months): secondary focus, only flagged when conviction is exceptionally high.
- Long-term opportunities: Claude may note these but should not recommend entry unless the setup is extremely compelling. The bot is not designed for long-term holding.

### 4.4 Technical analysis module

The technical analysis module runs independently of Claude, operating on the medium loop cadence. It computes indicators using **hand-rolled pandas/numpy functions**, detects signals, and provides the quantitative backbone for the opportunity ranker and the quantitative override system.

**Core indicators to implement (all pure pandas/numpy in `intelligence/technical_analysis.py`):**

| Indicator | Implementation | Parameters | Purpose |
|-----------|---------------|------------|---------|
| VWAP | `cumsum(price * volume) / cumsum(volume)` | Resets daily | Primary intraday reference |
| RSI | Wilders smoothing on gains/losses: `100 - 100/(1 + avg_gain/avg_loss)` | length=14 | Overbought/oversold + divergence |
| MACD | `ema(fast) - ema(slow)`, signal = `ema(macd, signal_period)` | fast=12, slow=26, signal=9 | Trend direction, momentum |
| EMA | `df['close'].ewm(span=length, adjust=False).mean()` | length=9, 20, 50, 200 | Trend structure, S/R |
| ROC | `(price - price.shift(length)) / price.shift(length) * 100` | length=5 | Momentum speed |
| ATR | `ema(max(high-low, abs(high-prev_close), abs(low-prev_close)))` | length=14 | Volatility for sizing/stops |
| Bollinger Bands | `sma ± (std * multiplier)` | length=20, std=2 | Squeeze/expansion detection |
| Volume SMA | `df['volume'].rolling(length).mean()` | length=20 | Volume comparison baseline |

Each indicator function must be a standalone pure function that takes a DataFrame and returns a Series or DataFrame. This makes them individually unit-testable. Tests should verify output against hand-calculated expected values for known price sequences.

**RSI divergence detection:** Compare the last two local price highs against the corresponding RSI values. If price high[n] > price high[n-1] but RSI high[n] < RSI high[n-1], flag bearish divergence. Inverse for bullish. Local highs are detected as points where `price[i] > price[i-1]` and `price[i] > price[i+1]` over a configurable lookback window (default: 20 bars).

**Signal output format:**
```json
{
  "symbol": "TSLA",
  "timestamp": "2025-03-11T14:30:00Z",
  "signals": {
    "vwap_position": "above",
    "rsi": 62.4,
    "rsi_divergence": false,
    "macd_signal": "bullish_cross",
    "trend_structure": "bullish_aligned",
    "roc_5": 2.1,
    "roc_deceleration": false,
    "volume_ratio": 1.8,
    "atr_14": 8.5,
    "bollinger_position": "upper_half"
  },
  "composite_technical_score": 0.72
}
```

**Composite technical score calculation:**

The composite score (0.0 to 1.0) is a weighted sum of individual signal scores. Each signal is mapped to a 0-1 range:

| Signal | Scoring rule | Weight |
|--------|-------------|--------|
| VWAP position | above=0.7, at=0.5, below=0.3 | 0.20 |
| RSI | 40-60=0.5 (neutral), 30-40/60-70=bullish/bearish context-dependent, <30/>70=extreme | 0.15 |
| MACD | bullish_cross=0.8, bullish=0.6, bearish=0.3, bearish_cross=0.1 | 0.15 |
| Trend structure | all EMAs aligned bullish=0.9, mixed=0.5, all bearish=0.1 | 0.15 |
| ROC | positive and accelerating=0.8, positive decelerating=0.5, negative=0.2 | 0.10 |
| Volume ratio | >1.5=0.8, 1.0-1.5=0.5, <1.0=0.3 | 0.10 |
| Bollinger position | upper band=0.7, middle=0.5, lower=0.3 (context-dependent on strategy) | 0.10 |
| RSI divergence | bearish divergence present=-0.2 penalty, bullish divergence=+0.1 bonus | 0.05 |

### 4.5 Opportunity ranker

The ranker receives Claude's trade reasoning JSON and the technical analysis signals, then produces a prioritized queue of trade actions. This is the bridge between intelligence and execution.

**Ranking formula (configurable weights):**
```
composite_score = (ai_conviction * W_ai) + (technical_score * W_tech) + (risk_adjusted_return * W_risk) + (liquidity_score * W_liq)
```

Default weights: W_ai = 0.35, W_tech = 0.30, W_risk = 0.20, W_liq = 0.15.

**Risk-adjusted return calculation:**
```
risk_adjusted_return = (suggested_exit - suggested_entry) / (suggested_entry - suggested_stop)
```
This is essentially a reward-to-risk ratio. Normalize to 0-1 range by capping at 5:1 (score = min(ratio / 5.0, 1.0)).

**Liquidity score calculation:**
```
liquidity_score = min(avg_daily_volume / 1_000_000, 1.0)
```
Stocks trading over 1M shares/day get a perfect liquidity score. Below that, linearly scaled.

**Hard filters (applied before scoring — any failure removes the opportunity):**
- Sufficient buying power for the intended position size.
- Adding this position would not exceed max concurrent positions.
- Entering and potentially exiting this position today would not violate PDT limits.
- Market is in regular hours, OR the opportunity is explicitly flagged for extended hours.
- The stock has a minimum average daily volume of 100,000 shares.

### 4.6 Strategy modules

Strategy modules are pluggable components that define entry/exit logic for different trading approaches. Each strategy implements a common async interface.

**Strategy base class interface:**
```python
from abc import ABC, abstractmethod

class Strategy(ABC):
    @abstractmethod
    async def generate_signals(self, symbol: str, market_data: DataFrame, indicators: dict) -> list[Signal]:
        """Produce entry signals for a symbol given current data and indicators."""
        ...

    @abstractmethod
    async def evaluate_position(self, position: Position, market_data: DataFrame, indicators: dict) -> PositionEval:
        """Evaluate whether an open position should be held, scaled, or exited."""
        ...

    @abstractmethod
    async def suggest_exit(self, position: Position, market_data: DataFrame, indicators: dict) -> ExitSuggestion:
        """Suggest specific exit parameters (price, order type, urgency)."""
        ...

    def get_parameters(self) -> dict:
        """Return current strategy parameters."""
        return self._params

    def set_parameters(self, params: dict) -> None:
        """Update strategy parameters at runtime."""
        self._params.update(params)
```

**Initial implementations:**
- `MomentumStrategy`: Targets stocks with strong directional moves, high volume, and technical breakouts. Entry on breakout confirmation (price above resistance + volume > 1.3x average + MACD bullish). Exit on momentum exhaustion (ROC deceleration + volume drop below average, or profit target hit).
- `SwingStrategy`: Targets stocks oscillating between support and resistance. Entry near support with confirming reversal signals (RSI bouncing from oversold + bullish candle pattern + volume increase). Exit near resistance or on breakdown below support.

**Future implementations (not in v3):**
- `OptionsStrategy`: Placeholder for future options plays (covered calls, spreads, etc.).
- `MeanReversionStrategy`: For range-bound markets when volatility is low.

### 4.7 Risk management module

Risk management operates on the fast loop and has override authority over all other modules. It can cancel pending orders, force exits, and block new entries.

**Hard rules (non-configurable, enforced in code):**
- No single position may exceed 20% of portfolio value.
- No more than 8 concurrent open positions.
- No new entries if account equity drops below $25,500 (PDT buffer).
- No new entries during the final 5 minutes of regular trading hours (3:55-4:00 PM ET) to avoid overnight risk on momentum plays without explicit AI approval.

**Configurable parameters (stored in config.json):**
- Maximum daily loss: default -2% of portfolio value. If hit, halt all new entries for the day.
- Per-trade maximum loss: default -3% of position value (enforced via stop-loss).
- PDT day-trade limit: 3 round-trips per rolling 5 business days. **Default buffer: 1** (meaning the system will use at most 2 of the 3 allowed day trades, reserving 1 for emergency exits). Configurable from 0-2.
- Position size calculator: based on ATR and account risk tolerance. Formula: `shares = (account_value * risk_per_trade_pct) / (atr_14 * atr_multiplier)`, where `atr_multiplier` defaults to 2.0.

**Quantitative override signals (hard exit triggers):**

These signals operate independently of Claude AI reasoning and execute immediately via market order when triggered. They protect against rapid momentum reversals where waiting for the next AI reasoning cycle would be too slow.

1. **VWAP crossover with volume confirmation:** Price crosses below VWAP on above-average volume (volume ratio > 1.3) after a long-side run-up. This is the single most reliable intraday reversal indicator for momentum trading.

2. **RSI divergence (confirmation only):** Price makes a new high but RSI does not (bearish divergence). This does NOT trigger alone. Must combine with at least one other signal.

3. **Rate-of-change deceleration:** The 5-period ROC drops below its 10-period moving average while price is still rising. Early warning of momentum fade.

4. **Volume-weighted momentum score flip:** Compute `score = price_change_pct * volume_ratio`. When this composite score flips sign after being strongly positive (> 1.5) or strongly negative (< -1.5), trigger exit. This captures smart money rotation.

5. **ATR-based trailing stop:** If price drops more than 2x ATR(14) from its intraday high since entry, exit. Catches flash crashes and sudden reversals.

**Override trigger logic:** Signals 1, 3, 4, and 5 can each trigger independently. Signal 2 (RSI divergence) requires at least one other signal to also be active. When any valid trigger condition is met:

**Override execution protocol:**
- Place a market order to exit the full position immediately.
- Log the override signal(s) that triggered, the price at trigger, and all indicator values at the moment.
- Do NOT immediately re-enter. Flag the position for re-evaluation in the next Claude reasoning cycle.
- If Claude determines the thesis is not broken (just a temporary pullback), the system may re-enter at a better price. If the thesis is broken, the position stays closed.

### 4.8 Broker abstraction layer

The broker abstraction is a critical interface that isolates all broker-specific API calls from the rest of the system. This is the most important modularity boundary in the entire architecture.

**Broker interface:**
```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

class BrokerInterface(ABC):
    # Account
    @abstractmethod
    async def get_account(self) -> AccountInfo: ...
    @abstractmethod
    async def get_buying_power(self) -> float: ...

    # Orders
    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult: ...
    @abstractmethod
    async def cancel_order(self, order_id: str) -> CancelResult: ...
    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus: ...
    @abstractmethod
    async def get_open_orders(self) -> list[Order]: ...

    # Positions
    @abstractmethod
    async def get_positions(self) -> list[Position]: ...
    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None: ...

    # Fills
    @abstractmethod
    async def get_fills(self, since: datetime) -> list[Fill]: ...

    # Market
    @abstractmethod
    async def is_market_open(self) -> bool: ...
    @abstractmethod
    async def get_market_hours(self) -> MarketHours: ...
```

**Implementation notes:**
- The Alpaca implementation is the first concrete class. It translates between the common interface and Alpaca's REST API endpoints, authentication, and data formats.
- Alpaca provides **integrated paper trading** via the same API — simply use the paper trading base URL (`https://paper-api.alpaca.markets`) instead of the live URL. No separate sandbox environment needed. The `environment` config field switches between `paper` and `live`.
- Alpaca's Python SDK (`alpaca-py`) provides native async support via `aiohttp`. Prefer the async client (`AsyncRest`) over synchronous calls.
- Alpaca uses API key + secret key authentication (no OAuth flow). Store in credentials file.
- All broker-specific quirks (order types, symbology, account structures) are contained within the implementation class.
- When migrating to a new broker (IBKR, etc.), only a new implementation class is needed. No other module changes.
- The broker interface must not expose any functionality that is only available on one broker. If a broker-specific feature is needed, it goes in an optional extension interface that the system can check for at runtime.
- **Alpaca-specific advantages to leverage:** native websocket streaming for order updates (can supplement polling in the fast loop), built-in fractional shares support, and free real-time market data for subscribed symbols via the data API. However, do NOT use Alpaca's data API as the primary market data source — keep yfinance as primary to maintain broker independence. Alpaca data can be used as a fallback.

### 4.9 Market hours awareness

The system must track the current market session and adjust behavior accordingly. **All comparisons use US/Eastern time derived from UTC, never the local system clock.**

```python
from zoneinfo import ZoneInfo
from datetime import datetime

ET = ZoneInfo("America/New_York")

def get_current_session() -> str:
    now_et = datetime.now(ET)
    # ... compare against session boundaries
```

| Session | Hours (ET) | Behavior |
|---------|-----------|----------|
| Pre-market | 4:00 AM - 9:30 AM | No new entries unless explicitly flagged by AI. Monitor fills only. |
| Regular hours | 9:30 AM - 4:00 PM | Full operation. All loops active. |
| Post-market | 4:00 PM - 8:00 PM | No new entries. Exit monitoring only. |
| Closed | 8:00 PM - 4:00 AM | System idle. Slow loop may run once for overnight analysis. |
| Last 5 min (3:55 - 4:00 PM) | — | No new momentum entries. Swing entries OK with AI approval. |
| Weekends / market holidays | — | System fully idle. No loops run. Check NYSE holiday calendar. |

---

## 5. Persistent state management

All state is stored as JSON files in the `state/` directory. The system reads state at startup, validates against schemas, and writes state after every mutation. Writes are atomic: write to a temp file, then rename (to prevent corruption on crash).

### 5.1 Portfolio state (`portfolio.json`)

Stores all open positions and their associated trade intentions. This is the most critical state file — the system must never lose track of an open position.

Each position record includes:
- Symbol, shares, average cost basis, entry date.
- Trade intention (written at entry, immutable except for review notes):
  - Catalyst that prompted the trade.
  - Direction (long; short not implemented yet).
  - Strategy type (momentum, swing).
  - Expected move (e.g., "+8-12% over 2 weeks").
  - Claude's reasoning summary at entry.
  - Exit targets: profit target price, stop-loss price.
  - Maximum expected loss in dollars.
  - Target technical indicators for exit.
- Review notes: appended by Claude each review cycle with updated assessment.
- Order history: all order IDs associated with this position.

### 5.2 Watchlist state (`watchlist.json`)

The watchlist is maintained at **up to 40 tickers** (not exactly 40 — the system can operate with fewer, especially during early runtime before Claude has populated it). Aggressively pruned and appended by Claude to reflect changing market conditions.

Each entry includes:
- Symbol, date added, reason added.
- Priority tier (1 = active candidate, 2 = monitoring, 3 = cooling off).
- Last evaluated timestamp.
- Strategy classification (momentum candidate, swing candidate, or both).
- Removal candidate flag (set by Claude when a ticker is losing relevance).

**Startup behavior:** If the watchlist is empty (first run), Claude's first reasoning cycle should focus on building an initial watchlist of 15-25 tickers before any trading begins.

### 5.3 Configuration (`config.json`)

Stores all tunable parameters, API keys, and prompt template versions.

```json
{
  "broker": {
    "name": "alpaca",
    "environment": "paper",
    "credentials_file": "credentials.enc",
    "base_url_paper": "https://paper-api.alpaca.markets",
    "base_url_live": "https://api.alpaca.markets"
  },
  "risk": {
    "max_position_pct": 0.20,
    "max_concurrent_positions": 8,
    "max_daily_loss_pct": 0.02,
    "per_trade_max_loss_pct": 0.03,
    "pdt_buffer": 1,
    "min_equity_for_trading": 25500
  },
  "scheduler": {
    "fast_loop_sec": 10,
    "medium_loop_sec": 120,
    "slow_loop_check_sec": 300,
    "slow_loop_max_interval_sec": 3600,
    "slow_loop_price_move_threshold_pct": 2.0
  },
  "claude": {
    "model": "claude-sonnet-4-20250514",
    "max_tokens_per_cycle": 4096,
    "prompt_version": "v3.3.0",
    "tier1_max_symbols": 12,
    "tier2_max_symbols": 28
  },
  "ranker": {
    "weight_ai": 0.35,
    "weight_technical": 0.30,
    "weight_risk": 0.20,
    "weight_liquidity": 0.15
  },
  "strategy": {
    "active_strategies": ["momentum", "swing"],
    "momentum_params": {},
    "swing_params": {}
  },
  "timezone": "America/New_York"
}
```

### 5.4 Order state machine (`orders.json`)

Tracks all orders placed by the system. This is the foundation of fill protection (see section 7.1).

Each order record includes:
- Order ID (broker-assigned), symbol, side (buy/sell), quantity, order type (market/limit), limit price (if applicable).
- Local state: `PENDING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`.
- For `PARTIALLY_FILLED`: `filled_quantity` and `remaining_quantity` fields (integer). Updated on each broker status poll.
- Timestamps: created_at, last_checked_at, filled_at/cancelled_at.
- Associated position ID (links to portfolio.json entry).
- Timeout threshold for stale order cancellation (default: 60 seconds for limit orders).

**Partial fill handling:** When a broker status poll reports a partial fill, update `filled_quantity` and `remaining_quantity`. The position in portfolio.json is updated to reflect the filled shares. The order remains in `PARTIALLY_FILLED` state until fully filled or cancelled. The fill protection module must treat `PARTIALLY_FILLED` as a blocking state (no new orders for the same symbol).

### 5.5 Claude reasoning cache (`reasoning_cache/`)

Claude reasoning responses are cached temporarily for debugging, strategy iteration, and cost avoidance (if the system restarts mid-session, it can use the last cached response instead of making a redundant API call).

**Cache design — same philosophy as logs (temporary, rotated, not permanent):**

- Each Claude response is saved as a JSON file: `reasoning_cache/reasoning_{timestamp_utc}.json`
- The file contains the full request context (input sent to Claude), the raw response text, the parsed JSON output (or `null` if parsing failed), and the trigger condition that initiated the call.
- **Retention:** Keep only the current session's cache files plus the previous session's. On startup, delete any cache files from sessions older than the previous one. This mirrors the two-log-file rotation design.
- **Startup reuse:** On startup during market hours, if a cached response from the current trading day exists and is less than 60 minutes old, load it as the initial AI reasoning state instead of making an immediate Claude call. This avoids a redundant call if the system is restarted mid-session.
- **Maximum cache files per session:** 30 (well above the expected ~8-12 calls/day). If exceeded, delete the oldest in the current session.

```json
{
  "timestamp": "2025-03-11T14:30:00Z",
  "trigger": "price_move_threshold",
  "session_id": "2025-03-11",
  "input_context_hash": "sha256:abc123...",
  "input_tokens": 5200,
  "output_tokens": 1800,
  "raw_response": "...",
  "parsed_response": { },
  "parse_success": true
}
```

---

## 6. Logging

Two log files, rotated on restart:
- `current.log`: All activity for the current session.
- `previous.log`: All activity from the last session.

On startup, `current.log` is renamed to `previous.log` (overwriting the old previous), and a new `current.log` is created.

**Log levels and content:**
- `INFO`: Trade entries/exits, position updates, watchlist changes, Claude reasoning summaries.
- `WARNING`: Fill timeouts, API rate limits approached, PDT threshold approaching, degraded mode activation.
- `ERROR`: API failures, state inconsistencies, override triggers, order rejections.
- `DEBUG`: Full Claude prompts/responses, raw API payloads, indicator calculations.

Each log entry includes: ISO 8601 UTC timestamp, module name, log level, and structured message. All trade-related entries include the symbol, action, and relevant prices.

Use Python's `logging` module with a custom formatter. Do not use print statements.

---

## 7. Critical safeguards

### 7.1 Fill protection and double-order prevention

This is the most important safeguard in the system. A failure here results in uncontrolled positions and unnecessary capital consumption.

**Order state machine rules:**
1. Before placing any order for a symbol, check local order state. If ANY order for that symbol is in `PENDING` or `PARTIALLY_FILLED` state, do NOT place a new order. This is the core double-order prevention rule.
2. On each fast-loop cycle, poll the broker for order status updates on all `PENDING` and `PARTIALLY_FILLED` orders and reconcile with local state.
3. If a `PENDING` limit order has been open longer than a configurable timeout (default: 60 seconds), cancel it broker-side. Wait for cancellation confirmation from the broker before taking any further action on that symbol.
4. After confirmed cancellation, decide whether to re-enter at a new price or abandon the trade.
5. If the broker reports a fill that the local state does not expect (edge case: fill happened between status checks), update local state immediately and log a `WARNING`. Do not attempt to reverse the fill.

**Race condition handling:**
The critical race condition is: order is `PENDING`, the bot decides to cancel, but the order fills between the cancel decision and the cancel API call. The solution: after issuing a cancel, poll the broker for the final order state before proceeding. If the order was filled, accept the fill and update state accordingly. If it was cancelled, proceed with re-entry logic. Never assume a cancel succeeded without broker confirmation.

**Partial fill race condition:** A cancel request may succeed but only after some shares have filled. The cancel confirmation from the broker should include the final fill quantity. Update portfolio state to reflect the partially filled shares. The risk manager must then evaluate whether the partial position is worth holding or should be immediately closed.

### 7.2 Pattern day trader (PDT) protection

With ~$30,000 starting capital, the account may lose PDT privileges if equity drops below $25,000 after too many day trades.

**Rules:**
- Track rolling 5-business-day day-trade count. A day trade is defined as opening and closing the same position on the same trading day.
- Allow a maximum of 3 day trades per rolling 5-day window. With the default buffer of 1, the system uses at most 2 day trades, reserving 1 for emergency exits triggered by override signals.
- Before any order that would constitute a day trade, check the counter. If at the limit (accounting for buffer), block the order and log a `WARNING`.
- If account equity drops below $25,500, halt all new entries (not just day trades) to prevent further equity erosion that could trigger PDT restrictions.
- Separate tracking: if the account is flagged as PDT by the broker (equity > $25,000 with the flag), track differently — PDT-flagged accounts with > $25k equity have unlimited day trades.

### 7.3 Additional broker/regulatory safeguards

- **Buying power check:** Before every order, verify sufficient buying power. Account for pending orders that consume buying power but haven't filled yet. Calculate: `available_buying_power = reported_buying_power - sum(pending_order_values)`.
- **Good Faith Violations (GFV):** In cash accounts or when margin is limited, selling a position before the purchase settles (T+1 for equities) and using those unsettled funds can trigger a GFV. Track settlement dates per position.
- **Free riding:** Buying with unsettled funds and then selling before the purchase settles. The system should not enter a position if the funds to cover it are unsettled.
- **Odd lot handling:** Some brokers treat orders under 100 shares differently. Log when placing odd-lot orders and note potential fill quality differences.
- **Price bands / limit-up-limit-down (LULD):** During extreme volatility, exchanges halt trading or restrict order types. Handle "order rejected" responses gracefully: log the rejection reason, do not retry for 30 seconds, then check if the halt has lifted before retrying.

---

## 8. Backtesting harness (post-MVP)

The modular architecture supports backtesting by swapping the broker implementation for a simulated executor. This is a post-MVP feature but the architecture must support it from the start (which it does via the broker abstraction).

**SimulatedBroker implementation:**
- Implements the same `BrokerInterface` as the live broker.
- Replays historical price data (loaded from CSV or yfinance historical API).
- Simulates fills with configurable slippage (default: 0.05% per trade).
- Tracks simulated portfolio, P&L, and trade history.
- Respects the same PDT rules and position limits as live trading.

**Backtesting workflow:**
1. Load historical data for the target period and symbols.
2. Initialize the system with `SimulatedBroker` instead of `AlpacaBroker`.
3. Run the orchestrator loops against the historical data timeline.
4. Claude API calls during backtesting can be replaced with cached responses (to avoid cost) or run live (for strategy validation).
5. Output: trade log, daily P&L, cumulative return, max drawdown, Sharpe ratio, win rate.

---

## 9. Recommended implementation order

Build in this sequence to catch integration problems early and test each layer independently. Each step should produce working, tested code before moving to the next.

1. **Persistent state management + logging + reasoning cache.** Foundation everything else depends on. Implement JSON read/write with atomic writes (write to temp, rename), schema validation, log rotation, reasoning cache directory with session-based rotation. Include timezone utilities here.

2. **Broker abstraction + Alpaca paper trading.** Validate that orders actually work. Implement the full async `BrokerInterface`, authenticate with Alpaca paper environment, place/cancel test orders. Use `alpaca-py` async client directly.

3. **Fast loop: order state machine, fill protection, PDT guard.** This is the safety net. Must work before any trading logic is added. Write thorough unit tests covering all edge cases including partial fills and the cancel-during-fill race condition.

4. **Market data ingestion + technical analysis module.** Implement yfinance adapter (async-wrapped), compute all core indicators with pure pandas/numpy, produce signal output format. Test each indicator function against hand-calculated expected values for known price sequences.

5. **Risk management module.** Implement all hard rules and configurable parameters. Implement quantitative override signals. Test with mock market data scenarios.

6. **Claude reasoning integration.** Build prompt templates, implement context assembly, implement defensive JSON parsing with all fallback layers. Implement reasoning cache write/read with session rotation. Test with manual triggers before connecting to the orchestrator.

7. **Opportunity ranker.** Connect Claude output + technical signals. Implement composite scoring and hard filters.

8. **Strategy modules.** Implement `MomentumStrategy` and `SwingStrategy` with their entry/exit logic.

9. **Full orchestrator integration.** Wire all three async loops together using `asyncio.TaskGroup`. Run in sandbox with real market data. Verify graceful degradation for each failure mode.

10. **Live paper trading.** Run full system on Alpaca paper trading during market hours for at least 2 weeks before considering real money. Alpaca paper trading uses the same API with a different base URL, so the transition to live is a single config change.

**Post-MVP additions (after step 10):**
11. Additional data adapters (Alpha Vantage, Finnhub, Reddit, etc.) — one at a time.
12. Backtesting harness + SimulatedBroker.
13. Notification system.
14. Alpaca websocket streaming for order updates (supplement/replace REST polling in fast loop).

---

## 10. Directory structure

```
ozymandias/
├── main.py                      # Entry point, orchestrator init
├── config/
│   ├── config.json              # Runtime configuration
│   ├── credentials.enc          # Encrypted API keys
│   └── prompts/
│       └── v3.3.0/
│           ├── reasoning.txt    # Claude reasoning prompt template
│           ├── watchlist.txt    # Claude watchlist management prompt
│           └── review.txt       # Claude position review prompt
├── core/
│   ├── orchestrator.py          # Three async-loop scheduler (event-driven slow loop)
│   ├── state_manager.py         # JSON state read/write/validate (atomic writes)
│   ├── logger.py                # Dual-file log rotation
│   └── market_hours.py          # Session detection, timezone utilities
├── intelligence/
│   ├── claude_reasoning.py      # Claude API integration + defensive JSON parsing
│   ├── technical_analysis.py    # Indicator computation (pure pandas/numpy, no TA libs)
│   └── opportunity_ranker.py    # Composite scoring
├── data/
│   ├── adapters/
│   │   ├── base.py              # DataAdapter / SentimentAdapter ABC interfaces
│   │   ├── yfinance_adapter.py  # MVP: primary market data
│   │   ├── alpha_vantage.py     # Post-MVP
│   │   ├── finnhub_adapter.py   # Post-MVP
│   │   ├── reddit_adapter.py    # Post-MVP
│   │   ├── stocktwits_adapter.py # Post-MVP
│   │   └── sec_edgar.py         # Post-MVP
│   └── aggregator.py            # Merges data from all active adapters
├── execution/
│   ├── broker_interface.py      # Abstract async BrokerInterface
│   ├── alpaca_broker.py         # Alpaca implementation (paper + live)
│   ├── simulated_broker.py      # Post-MVP: backtesting implementation
│   ├── risk_manager.py          # Risk rules + quant overrides
│   └── fill_protection.py       # Order state machine + partial fill handling
├── strategies/
│   ├── base_strategy.py         # Strategy ABC interface
│   ├── momentum_strategy.py
│   └── swing_strategy.py
├── state/
│   ├── portfolio.json
│   ├── watchlist.json
│   └── orders.json
├── reasoning_cache/             # Temporary Claude response cache (rotated per session)
│   └── reasoning_{timestamp}.json
├── logs/
│   ├── current.log
│   └── previous.log
├── tests/
│   ├── test_state_manager.py
│   ├── test_fill_protection.py
│   ├── test_risk_manager.py
│   ├── test_pdt_guard.py
│   ├── test_technical_analysis.py
│   ├── test_claude_json_parsing.py
│   ├── test_market_hours.py
│   ├── test_broker_interface.py
│   └── test_opportunity_ranker.py
├── requirements.txt
└── README.md
```

---

## 11. Dependencies (`requirements.txt`)

```
# Async HTTP
aiohttp>=3.9

# Broker
alpaca-py>=0.30  # Alpaca SDK with native async support

# Market data
yfinance>=0.2.31

# Data & indicators (hand-rolled TA, no third-party TA libraries)
pandas>=2.1
numpy>=1.26

# Claude API
anthropic>=0.40

# Utilities
python-dateutil>=2.8
```

---

## 12. Cost projections

| Item | Estimated daily cost | Notes |
|------|---------------------|-------|
| Claude API (Sonnet) | $5 - $15 | Event-driven, ~8-12 calls/day, ~6k tokens/cycle |
| Alpha Vantage | $0 | Free tier sufficient (post-MVP) |
| Finnhub | $0 | Free tier sufficient (post-MVP) |
| Alpaca API | $0 | No API fees for paper or live; commission-free equity trades |
| Infrastructure (local) | $0 | Runs on local machine initially |
| **Total** | **~$5 - $15/day** | Primary cost is Claude API |

The event-driven slow loop (section 4.1) reduces Claude API calls by 50-70% compared to fixed-interval polling. On quiet market days with little price movement, Claude may be called as few as 4-5 times (market open, hourly ceiling, market close approach). On volatile days with frequent triggers, it may reach 12-15 calls. The cost scales with market activity, which is the correct behavior — you want more AI reasoning when more is happening.

---

## 13. Open questions for implementation

1. What are Alpaca's paper trading rate limits for REST API calls? This constrains the fast loop frequency. Needs testing during step 2. (Alpaca documents 200 requests/minute for paper; verify this is sufficient for 10-second polling.)
2. Should the backtesting harness support parallel strategy testing (run momentum-only vs. swing-only vs. combined)?
3. What notification mechanism for critical events (email, SMS, desktop notification)? Defer decision until post-MVP.
4. Should Alpaca's websocket streaming for order updates replace or supplement REST polling in the fast loop? Websocket would reduce latency and API call count but adds connection management complexity.
