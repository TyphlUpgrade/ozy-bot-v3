# Ozymandias v3

An automated stock trading bot that combines Claude AI strategic reasoning with quantitative technical analysis. Targets aggressive momentum and swing trading on high-volatility, high-liquidity equities. Runs on Alpaca paper trading by default.

## Prerequisites

- **Python 3.12+**
- **Alpaca paper trading account** — create one at [alpaca.markets](https://alpaca.markets)
- **Anthropic API key** — get one at [console.anthropic.com](https://console.anthropic.com)

## Setup

### 1. Install dependencies

```bash
pip install aiohttp>=3.9 alpaca-py>=0.30 yfinance>=0.2.31 \
            pandas>=2.1 numpy>=1.26 anthropic>=0.40 python-dateutil>=2.8
```

### 2. Configure credentials

Create `ozymandias/config/credentials.enc` (plain JSON — the `.enc` extension is a naming convention, not actual encryption in v3):

```json
{
  "api_key": "YOUR_ALPACA_API_KEY",
  "secret_key": "YOUR_ALPACA_SECRET_KEY",
  "anthropic_api_key": "YOUR_ANTHROPIC_API_KEY"
}
```

### 3. Review config

Edit `ozymandias/config/config.json` if you want to change risk limits, loop timing, or the Claude model. The defaults are conservative and suitable for paper trading.

### 4. Validate your setup

```bash
PYTHONPATH=. python scripts/validate_config.py
```

This checks that config loads, credentials are present, prompt templates exist, and both APIs respond. Fix any failures before proceeding.

## Starting paper trading

```bash
PYTHONPATH=. python -m ozymandias.main
```

The bot will connect to Alpaca, reconcile any existing state with broker positions, and start the three async loops.

## Dry-run mode

Run the full bot logic without placing any orders:

```bash
PYTHONPATH=. python -m ozymandias.main --dry-run
```

In dry-run mode every order decision is logged at INFO level with all details (symbol, side, quantity, price, strategy) but nothing is submitted to the broker. Useful for the first few days to build confidence in the system's decision-making.

## Reading the logs

Logs are written to `ozymandias/logs/current.log` and to stdout. On startup the bot rotates the previous log to `previous.log`.

Key log prefixes to watch:

| Prefix / content | Meaning |
|---|---|
| `=== Startup reconciliation ===` | Position/order sync with broker at boot |
| `[DRY RUN] Would place order` | Dry-run order decision |
| `Entry order placed` | Real order submitted |
| `Override exit` | Quant system forced an exit (VWAP cross, ATR stop, etc.) |
| `Claude reasoning cycle` | AI reasoning loop ran |
| `time_ceiling trigger` | 60+ minutes since last Claude call → forced reasoning |
| `WARN conservative mode` | Reconciliation errors detected — new entries paused |
| `ERROR` | Something needs your attention |

## Architecture overview

```
External Data (yfinance) → Orchestrator (3 async loops) → Intelligence (Claude + TA + Ranker) → Execution (Risk + Broker)
                                  ↕
                          Persistent State (JSON files)
```

Three concurrent loops:

- **Fast loop (10s):** Order fill polling, stale order cancellation, quant override exits (VWAP, ATR trailing stop, RSI divergence), PDT guard, position sync with broker.
- **Medium loop (2min):** Fetches bars for all watchlist symbols, runs technical analysis, ranks opportunities, validates and places entry orders, evaluates open positions for exit.
- **Slow loop (5min check):** Calls Claude only when a trigger fires — 60-minute time ceiling, >2% price move on a watched symbol, watchlist critically small, session transition, or multiple quant override exits.

See `ozymandias_v3_spec_revised.md` for the full design specification.

## Known limitations (v3 MVP)

- **Single data source:** Only yfinance. No real-time data; bars have ~15-minute delay.
- **No websockets:** Order fills are polled every 10 seconds, not pushed.
- **No backtesting harness:** The system is designed for live/paper operation, not historical simulation.
- **Prompt templates are static:** Claude is given a fixed JSON schema; the prompts in `config/prompts/` are the only way to tune AI behavior.
- **Alpaca paper only:** Live trading requires changing `broker.environment` to `"live"` in config and accepting the associated risks.

## Post-MVP roadmap

1. Additional data adapters (Alpha Vantage, Finnhub) for real-time quotes
2. Alpaca websocket streaming for instant order fill notifications
3. Backtesting harness with `SimulatedBroker`
4. Notification system (Slack/email) for critical events
5. Performance analytics dashboard
