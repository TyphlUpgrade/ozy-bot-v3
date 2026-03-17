# Phase 13: Claude Context Enrichment — Feedback Loops & Execution Visibility

Read the Phase 12 section of DRIFT_LOG.md before starting. This phase assumes Phase 12 is complete.

Claude currently makes decisions without knowing whether its previous recommendations were executed, filled, or failed. A limit order sitting unfilled for 45 minutes while the stock has moved 2% away is invisible to Claude's next reasoning cycle — it will likely recommend the same symbol again at a price that's no longer achievable. This phase adds two feedback mechanisms that close the loop between execution reality and Claude's strategic reasoning: pending entry visibility, and session execution history.

## 1. Track Pending Entry Attempts on the Orchestrator

Add `_pending_entry_log: dict[str, dict]` to `Orchestrator.__init__`:

```python
# symbol → {claude_entry_target, order_id, attempt_time_utc, order_status}
self._pending_entry_log: dict[str, dict] = {}
```

In `_medium_try_entry`, after a successful `broker.place_order()`:
- Write an entry to `_pending_entry_log[symbol]` with: `claude_entry_target` (the original `top.suggested_entry`), `order_id`, `attempt_time_utc` (UTC now), `order_status = "PENDING"`

In `_dispatch_confirmed_fill` (fast loop fill handler) when an opening fill is confirmed:
- Set `_pending_entry_log[symbol]["order_status"] = "FILLED"`

In `_fast_step_poll_and_reconcile` when a cancel is detected:
- Set `_pending_entry_log[symbol]["order_status"] = "CANCELLED"`

Entries older than 1 trading session (e.g., from_date != today) are purged at the start of each slow loop cycle. The dict is in-memory only (does not need state file persistence).

## 2. Add `pending_entries` Section to Claude's Context

In `assemble_reasoning_context` (`intelligence/claude_reasoning.py`), add a `pending_entries` section to the assembled context dict:

```json
"pending_entries": [
  {
    "symbol": "NVDA",
    "claude_entry_target": 480.00,
    "current_price": 487.20,
    "drift_pct": 1.5,
    "pending_since_min": 42,
    "order_status": "PENDING"
  }
]
```

Rules:
- Only include entries where `order_status == "PENDING"` and age > `config.claude.pending_entry_min_age_min` (default 5, to filter very recent placements that just haven't filled yet)
- Cap at 10 entries (respect token budget)
- Include filled/cancelled entries from the last 30 min as a short historical signal (show `order_status`)
- `drift_pct` = `(current_price - claude_entry_target) / claude_entry_target × 100` (sign indicates direction)
- `current_price` comes from `indicators` dict passed to `assemble_reasoning_context`

New config key (add to `ClaudeConfig` and `config.json`):
- `pending_entry_min_age_min: int = 5`

## 3. Add `recent_executions` Section to Claude's Context

In `assemble_reasoning_context`, read the last N completed trades from the trade journal:

```json
"recent_executions": [
  {
    "symbol": "AMD",
    "direction": "short",
    "entry_price": 198.50,
    "exit_price": 196.20,
    "pnl_pct": 1.16,
    "strategy": "momentum",
    "claude_conviction": 0.72,
    "duration_min": 28
  }
]
```

- Call a new method `TradeJournal.load_recent(n: int) -> list[dict]` that reads the last N lines from `trade_journal.jsonl`
- Limit to `config.claude.recent_executions_count` entries (default 5)
- Include only entries where `entry_price > 0` (exclude ghost/phantom trades)

New config key:
- `recent_executions_count: int = 5`

`TradeJournal.load_recent(n)` reads the file from the end using standard file iteration — no need for a full parse.

## 4. Add `ta_readiness` to Watchlist Tier 1 Context

In `assemble_reasoning_context`, currently each tier-1 watchlist entry includes `technical_summary` (a string) and `composite_score` (a float). Replace `technical_summary` with a structured `ta_readiness` dict that gives Claude the actual values to use when writing `entry_conditions`:

```json
"ta_readiness": {
  "above_vwap": true,
  "rsi": 58.2,
  "macd": "bullish_cross",
  "volume_ratio": 1.85,
  "trend": "bullish_aligned",
  "composite_score": 0.72
}
```

This comes directly from `indicators[symbol]["signals"]` — no new computation needed. The existing string `_make_technical_summary()` result can be retained as `technical_summary_text` alongside `ta_readiness` for Claude's narrative reference, or dropped to save tokens (prefer dropping to stay within token budget).

## 5. Update Prompt Template

In `config/prompts/v3.4.0/reasoning.txt` (or create v3.5.0 if Phase 12 changes require it — prefer v3.4.0 if both phases produce a single clean prompt version):

Add instructions for the new context fields:

**For `pending_entries`**: "If `pending_entries` shows limit orders that have been pending >30 minutes with significant drift from target, treat them as failed entries. In your next opportunity recommendations, either adjust the entry price, reduce conviction, or omit the symbol if the entry window has closed."

**For `recent_executions`**: "Use `recent_executions` to calibrate. If recent momentum trades stopped out at high frequency, consider raising conviction thresholds or tightening entry conditions in new opportunities."

**For `ta_readiness`**: "The `ta_readiness` dict in each watchlist entry shows current indicator values. Use these values to calibrate `entry_conditions` in your opportunities — e.g., if current RSI is 58 and the stock's typical momentum range is 50–72, specify `rsi_min: 50, rsi_max: 72`."

## 6. Tests to Write

Create `tests/test_context_enrichment.py`:

- **`pending_entry_log` populated on order placement**: after `_medium_try_entry` places an order, `_pending_entry_log` has an entry for that symbol
- **`pending_entry_log` updated on fill**: fill detection sets status to "FILLED"
- **`pending_entries` in context: age filter**: entry age < 5min → not included; age > 5min → included
- **`pending_entries` capped**: more than 10 entries → only 10 in context
- **`pending_entries` drift calculated correctly**: verify drift_pct formula
- **`recent_executions`**: `TradeJournal.load_recent(5)` returns last 5 entries from journal
- **`recent_executions` count cap**: limited to `recent_executions_count` config value
- **`recent_executions` phantom filter**: entries with `entry_price=0` excluded
- **`ta_readiness` dict structure**: contains expected keys (above_vwap, rsi, macd, volume_ratio, trend, composite_score)
- **`ta_readiness` populated from indicators**: values match `indicators[symbol]["signals"]`
- **Token budget**: assembled context with all new sections stays within 8K token estimate; if not, verify truncation fires correctly
- **Backward compat**: `assemble_reasoning_context` called with `indicators={}` (no data) → `pending_entries: []`, `recent_executions: []`, `ta_readiness: {}` — no crash

## Done When

- All existing tests pass; all `test_context_enrichment.py` tests pass
- Claude's assembled context JSON includes `pending_entries`, `recent_executions`, and `ta_readiness` sections
- `TradeJournal.load_recent(n)` method implemented and tested
- Token budget guard still enforces 8K limit after new sections added
- In a paper session run, Claude's market_assessment or position_reviews referencing pending entry drift is observable in reasoning cache files
- DRIFT_LOG.md has a Phase 13 entry covering context schema additions and `TradeJournal.load_recent`
