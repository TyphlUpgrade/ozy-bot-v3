# Phase 15: Claude Context Enrichment — Feedback Loops & Execution Visibility

Read the Phase 14 and Phase 16 sections of DRIFT_LOG.md before starting. This phase assumes
Phase 16 is complete (which itself assumes Phase 14 is complete).

**Implementation order note:** Phase 16 must be implemented before this phase. Phase 16 adds
five new signals (`roc_negative_deceleration`, `rsi_slope_5`, `macd_histogram_expanding`,
`bb_squeeze`, `volume_trend_bars`) to `generate_signal_summary`. Because `ta_readiness`
(Section 4 below) is a direct pass-through of `indicators[symbol]["signals"]`, all Phase 16
signals appear in Claude's context automatically when this phase is implemented — no additional
work is required here to expose them.

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

Because `ta_readiness` is a direct pass-through of `indicators[symbol]["signals"]`, all Phase 16
pattern signals (`rsi_slope_5`, `macd_histogram_expanding`, `bb_squeeze`, `volume_trend_bars`)
appear in Claude's context here automatically — no additional mapping required.

```json
"ta_readiness": {
  "above_vwap": true,
  "rsi": 58.2,
  "rsi_slope_5": 6.4,
  "macd": "bullish_cross",
  "macd_histogram_expanding": true,
  "volume_ratio": 1.85,
  "volume_trend_bars": 3,
  "trend": "bullish_aligned",
  "bb_squeeze": false,
  "composite_score": 0.72
}
```

This comes directly from `indicators[symbol]["signals"]` — no new computation needed. The existing string `_make_technical_summary()` result can be retained as `technical_summary_text` alongside `ta_readiness` for Claude's narrative reference, or dropped to save tokens (prefer dropping to stay within token budget).

## 5. Execution Statistics Digest

In `assemble_reasoning_context`, add an `execution_stats` section alongside `recent_executions`. Where `recent_executions` shows individual trades, `execution_stats` shows aggregated calibration data:

```json
"execution_stats": {
  "total_trades": 12,
  "win_rate_pct": 50,
  "short_win_rate_pct": 33,
  "avg_hold_min": 43,
  "avg_pnl_pct": 0.28,
  "high_conviction_win_rate_pct": 75
}
```

Add `TradeJournal.compute_session_stats() -> dict` that reads the last `max(N, 20)` completed trades and computes:
- `total_trades` — count
- `win_rate_pct` — integer, trades where `pnl_pct > 0`
- `short_win_rate_pct` — same filter, direction == `"short"` only; omit key if no short trades
- `avg_hold_min` — mean of `duration_min` across all trades
- `avg_pnl_pct` — mean `pnl_pct` rounded to 2dp
- `high_conviction_win_rate_pct` — win rate for trades where `claude_conviction >= 0.75`; omit key if fewer than 3 high-conviction trades

Returns `{}` if fewer than `execution_stats_min_trades` (default 3) completed trades are available — not enough data to calibrate from.

New config key (add to `ClaudeConfig` and `config.json`):
- `execution_stats_min_trades: int = 3`

## 6. Watchlist Direction Tagging

Add `expected_direction: str = "either"` to `WatchlistEntry` in `core/state_manager.py`. Valid values: `"long"`, `"short"`, `"either"` (default). This is Claude's declared directional thesis for the symbol and is stored persistently with the watchlist.

Update Claude's `watchlist_changes.add` schema in the prompt template to include the optional field:

```json
"add": [
  {
    "symbol": "TSLA",
    "reason": "Bearish breakdown below key support",
    "priority_tier": 1,
    "strategy": "momentum",
    "expected_direction": "short"
  }
]
```

`expected_direction` is optional — `_process_watchlist_changes` defaults to `"either"` when absent for backward compatibility with Claude outputs that omit the field.

In `ta_readiness` (Section 4 above), compute `composite_score` using the direction-adjusted scorer:

```python
from ozymandias.intelligence.technical_analysis import compute_composite_score
from ozymandias.core.direction import ACTION_TO_DIRECTION

direction = entry.expected_direction if entry.expected_direction != "either" else "long"
ta_readiness["composite_score"] = compute_composite_score(raw_signals, direction=direction)
```

This ensures Claude sees a bearish-quality score for short-tagged watchlist candidates rather than the long-biased default.

Also include `expected_direction` in the watchlist entry's context block so Claude can see what direction it previously tagged and confirm or revise it.

## 7. Update Prompt Template

In `config/prompts/v3.4.0/reasoning.txt` (or create v3.5.0 if Phase 12 changes require it — prefer v3.4.0 if both phases produce a single clean prompt version):

Add instructions for the new context fields:

**For `pending_entries`**: "If `pending_entries` shows limit orders that have been pending >30 minutes with significant drift from target, treat them as failed entries. In your next opportunity recommendations, either adjust the entry price, reduce conviction, or omit the symbol if the entry window has closed."

**For `recent_executions`**: "Use `recent_executions` to calibrate. If recent momentum trades stopped out at high frequency, consider raising conviction thresholds or tightening entry conditions in new opportunities."

**For `ta_readiness`**: "The `ta_readiness` dict in each watchlist entry shows current indicator values. Use these values to calibrate `entry_conditions` in your opportunities — e.g., if current RSI is 58 and the stock's typical momentum range is 50–72, specify `rsi_min: 50, rsi_max: 72`."

**For `execution_stats`**: "Use `execution_stats` to calibrate conviction and position sizing. If `short_win_rate_pct` is below 40%, reduce short opportunity conviction or tighten entry conditions. If `high_conviction_win_rate_pct` is below 50%, your conviction scores are not predictive — consider using the full conviction range rather than clustering near 0.8+."

**For `expected_direction` in watchlist entries**: "The `expected_direction` field shows the directional thesis you assigned when adding this symbol. Confirm or revise it in your position reviews. When adding new symbols via `watchlist_changes.add`, always specify `expected_direction` — it affects how TA scores and entry conditions are evaluated in subsequent cycles."

## 8. Tests to Write

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
- **`ta_readiness` uses direction-adjusted score**: watchlist entry with `expected_direction="short"` → `composite_score` is bearish-adjusted, not long-default
- **`execution_stats` returns empty when < min trades**: fewer than `execution_stats_min_trades` journal entries → `execution_stats: {}`
- **`execution_stats` win rate correct**: mock journal with 4 wins / 6 losses → `win_rate_pct: 40`
- **`execution_stats` short win rate omitted when no shorts**: journal has no short trades → `short_win_rate_pct` key absent
- **`execution_stats` high conviction win rate omitted when < 3 samples**: fewer than 3 trades with conviction >= 0.75 → `high_conviction_win_rate_pct` key absent
- **`WatchlistEntry.expected_direction` persists**: watchlist entry written with `expected_direction="short"` → reloaded state has same value
- **`expected_direction` defaults to `"either"` when absent**: Claude output missing the field → entry saved with `"either"`
- **Token budget**: assembled context with all new sections stays within 8K token estimate; if not, verify truncation fires correctly
- **Backward compat**: `assemble_reasoning_context` called with `indicators={}` (no data) → `pending_entries: []`, `recent_executions: []`, `ta_readiness: {}`, `execution_stats: {}` — no crash

## Done When

- All existing tests pass; all `test_context_enrichment.py` tests pass
- Claude's assembled context JSON includes `pending_entries`, `recent_executions`, `ta_readiness`, and `execution_stats` sections
- `TradeJournal.load_recent(n)` and `TradeJournal.compute_session_stats()` implemented and tested
- `WatchlistEntry.expected_direction` field present, persisted, and defaulting correctly
- `ta_readiness.composite_score` uses direction-adjusted scoring when `expected_direction != "either"`
- Token budget guard still enforces 8K limit after new sections added
- In a paper session run, Claude's market_assessment or position_reviews referencing pending entry drift is observable in reasoning cache files
- DRIFT_LOG.md has a Phase 15 entry covering context schema additions, `TradeJournal` new methods, and `WatchlistEntry.expected_direction`
