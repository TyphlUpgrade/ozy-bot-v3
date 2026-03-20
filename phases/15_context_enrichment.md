# Phase 15: Claude Context Enrichment — Feedback Loops & Execution Visibility

Read the Phase 14 and Phase 16 sections of DRIFT_LOG.md before starting. This phase assumes
Phase 16 is complete (which itself assumes Phase 14 is complete).

**Implementation order note:** Phase 16 must be implemented before this phase. Phase 16 adds
five new signals (`roc_negative_deceleration`, `rsi_slope_5`, `macd_histogram_expanding`,
`bb_squeeze`, `volume_trend_bars`) to `generate_signal_summary`. Because `ta_readiness`
(Section 3 below) is a direct pass-through of `indicators[symbol]["signals"]`, all Phase 16
signals appear in Claude's context automatically when this phase is implemented — no additional
work is required here to expose them.

**Implementation dependency order within this phase:** Section 5 (`WatchlistEntry.expected_direction`)
must be implemented before Section 3 (`ta_readiness`) because `ta_readiness` reads
`entry.expected_direction`. Implement in order: 5 → 1 → 2 → 3 → 4 → 6.

Claude currently makes decisions without knowing whether its previous recommendations were
executed, blocked by quant filters, or sitting unfilled. A symbol rejected four times by RVOL
is invisible to Claude's next reasoning cycle — it will recommend the same symbol again with
no awareness of why it never entered. This phase closes that loop with a single unified
recommendation outcome tracker and adds session execution history and structured TA context.

---

## 1. Unified Recommendation Outcome Tracker

### 1a. Data structure

Add to `Orchestrator.__init__`:

```python
# symbol → {
#   "claude_entry_target": float,    # top.suggested_entry at recommendation time
#   "attempt_time_utc": str,         # ISO UTC when Claude recommended it
#   "stage": str,                    # current pipeline stage (see table below)
#   "stage_detail": str,             # human-readable reason for current stage
#   "rejection_count": int,          # incremented each time stage is set to "ranker_rejected"
#   "order_id": str | None,          # set when order is placed
# }
self._recommendation_outcomes: dict[str, dict] = {}
```

Valid `stage` values. A recommendation generally advances forward but can regress (e.g.
`order_pending` → cancel → `ranker_rejected` on the next cycle):

| stage | meaning |
|---|---|
| `"ranker_rejected"` | Hard-filtered by `rank_opportunities` (RVOL, composite score, etc.) |
| `"conditions_waiting"` | Passed ranker; blocked by `entry_conditions` gate |
| `"gate_expired"` | `entry_conditions` gate cleared by `max_entry_defer_cycles` expiry |
| `"order_pending"` | Order placed, not yet filled |
| `"filled"` | Opening fill confirmed |
| `"cancelled"` | Order was cancelled |

**Session-veto symbols** (blocked before the ranker hard-filter, e.g. already-open position,
cycle-consumed, PDT blocked) are **not** recorded in `_recommendation_outcomes`. Only symbols
that reach `rank_opportunities` and are hard-filtered are recorded as `"ranker_rejected"`.

**Purge**: At the start of each slow loop cycle, remove entries where `attempt_time_utc`
date (UTC) != today (UTC). In-memory only — no state file persistence. After a bot restart,
all entries are gone; this is acceptable.

### 1b. `rank_opportunities` return type change

Change `rank_opportunities` in `intelligence/opportunity_ranker.py` to return a named result
that includes both accepted candidates and hard-filter rejections:

```python
from dataclasses import dataclass

@dataclass
class RankResult:
    candidates: list[ScoredOpportunity]
    rejections: list[tuple[str, str]]   # (symbol, reason_string)
```

The `rejections` list is populated inside `apply_hard_filters` (or wherever hard-filter
rejections are currently logged). Every log line of the form
`"Hard filter rejected %s: %s"` maps to one `(symbol, reason)` tuple.

Update the call site in the medium loop (`_run_medium_cycle` or equivalent) from:
```python
ranked = self._ranker.rank_opportunities(...)
```
to:
```python
rank_result = self._ranker.rank_opportunities(...)
ranked = rank_result.candidates
```

Then iterate `rank_result.rejections` to update `_recommendation_outcomes`.

Update all tests that call `rank_opportunities` or mock its return value to use `RankResult`.

### 1c. Update points in orchestrator

**After `rank_opportunities` call** — for each `(symbol, reason)` in `rank_result.rejections`:
```python
existing = self._recommendation_outcomes.get(symbol, {})
self._recommendation_outcomes[symbol] = {
    "claude_entry_target": existing.get("claude_entry_target", 0.0),
    "attempt_time_utc": existing.get("attempt_time_utc") or datetime.now(timezone.utc).isoformat(),
    "stage": "ranker_rejected",
    "stage_detail": reason,
    "rejection_count": existing.get("rejection_count", 0) + 1,
    "order_id": None,
}
```

**In `_medium_try_entry`** when `entry_conditions` defer fires — write/update:
```python
self._recommendation_outcomes[symbol] = {
    **self._recommendation_outcomes.get(symbol, {}),
    "stage": "conditions_waiting",
    "stage_detail": f"defer_count={self._entry_defer_counts.get(symbol, 0)}, conditions={top.entry_conditions}",
    "attempt_time_utc": self._recommendation_outcomes.get(symbol, {}).get("attempt_time_utc")
                        or datetime.now(timezone.utc).isoformat(),
}
```

**In `_medium_try_entry`** when `max_entry_defer_cycles` expiry fires (gate cleared):
```python
self._recommendation_outcomes[symbol] = {
    **self._recommendation_outcomes.get(symbol, {}),
    "stage": "gate_expired",
    "stage_detail": f"gate cleared after {self._config.scheduler.max_entry_defer_cycles} misses",
}
```

**In `_medium_try_entry`** after successful `broker.place_order()`:
```python
self._recommendation_outcomes[symbol] = {
    **self._recommendation_outcomes.get(symbol, {}),
    "stage": "order_pending",
    "order_id": result.order_id,
    "claude_entry_target": top.suggested_entry,
    "attempt_time_utc": self._recommendation_outcomes.get(symbol, {}).get("attempt_time_utc")
                        or datetime.now(timezone.utc).isoformat(),
}
```

**In `_dispatch_confirmed_fill`** when an opening fill is confirmed:
```python
if symbol in self._recommendation_outcomes:
    self._recommendation_outcomes[symbol]["stage"] = "filled"
```

**In `_fast_step_poll_and_reconcile`** when a cancel is detected:
```python
if symbol in self._recommendation_outcomes:
    self._recommendation_outcomes[symbol]["stage"] = "cancelled"
```

### 1d. `recommendation_outcomes` in Claude's context

`_recommendation_outcomes` lives on `Orchestrator`. `assemble_reasoning_context` lives on
`ClaudeReasoningEngine`. Cross this boundary by passing the dict as a new parameter:

```python
# In ClaudeReasoningEngine:
def assemble_reasoning_context(
    self, portfolio, watchlist, market_data, indicators,
    recommendation_outcomes: dict,      # ← new
    entry_defer_counts: dict,           # ← new (for defer_count in conditions_waiting)
) -> dict:
```

The orchestrator's slow-loop call site already passes arguments to `assemble_reasoning_context`;
add `recommendation_outcomes=self._recommendation_outcomes` and
`entry_defer_counts=self._entry_defer_counts` there.

In `assemble_reasoning_context`, build the `recommendation_outcomes` context list:

```json
"recommendation_outcomes": [
  {
    "symbol": "NVDA",
    "stage": "ranker_rejected",
    "stage_detail": "RVOL 0.10 below floor 1.00",
    "rejection_count": 4,
    "claude_entry_target": 875.00,
    "age_min": 8
  },
  {
    "symbol": "AMD",
    "stage": "conditions_waiting",
    "stage_detail": "defer_count=3, conditions={rsi_min: 65}",
    "claude_entry_target": 204.00,
    "age_min": 22
  },
  {
    "symbol": "BAC",
    "stage": "order_pending",
    "claude_entry_target": 47.00,
    "current_price": 47.31,
    "drift_pct": 0.66,
    "age_min": 12
  },
  {
    "symbol": "XOM",
    "stage": "filled",
    "age_min": 45
  }
]
```

Assembly rules:
- `age_min` = minutes since `attempt_time_utc`
- For `order_pending`: `drift_pct = (current_price - claude_entry_target) / claude_entry_target × 100`;
  `current_price` from the `indicators` parameter of `assemble_reasoning_context` (same dict
  used for `ta_readiness` — the flat signals dict, i.e. `indicators[symbol].get("price")`)
- `filled` and `cancelled` entries with `age_min > recommendation_outcome_max_age_min` (default 60)
  are omitted
- Sort by `age_min` ascending (most recent first)
- Cap total list at 15 entries

**These new sections are embedded in the existing `context` dict that is serialised to
`{context_json}` in the prompt template. Do NOT add new `{placeholder}` variables to the
template — everything goes inside the existing JSON context block.**

New config key (add to `ClaudeConfig` and `config.json`):
- `recommendation_outcome_max_age_min: int = 60`

---

## 2. Add `recent_executions` and `execution_stats` to Claude's Context

### 2a. `TradeJournal.load_recent`

Add an async method to `TradeJournal`:

```python
async def load_recent(self, n: int) -> list[dict]:
```

Reads the last N records from `trade_journal.jsonl` where `record_type == "close"` OR
`record_type` is absent (for pre-lifecycle close records that predate this field). Include
only records where `entry_price > 0` (exclude ghost/phantom trades). Returns at most `n`
records in reverse-chronological order (newest first).

Must acquire `self._lock` for the read to avoid races with concurrent `append()` calls.

Reads from the end of the file without parsing the entire file. Standard approach: read all
lines (the file is small — ~1 MB/year) and take the last N matching records.

New config key:
- `recent_executions_count: int = 5`

### 2b. `recent_executions` context section

```json
"recent_executions": [
  {
    "symbol": "AMD",
    "direction": "long",
    "entry_price": 204.83,
    "exit_price": 202.31,
    "pnl_pct": -1.23,
    "strategy": "swing",
    "claude_conviction": 0.45,
    "duration_min": 1117
  }
]
```

Field names must match what `_journal_closed_trade` writes: `pnl_pct`, `hold_duration_min`
(rename to `duration_min` in the context payload — truncate to int). Verify against the
actual `_journal_closed_trade` call before finalising the field mapping.

Limit to `config.claude.recent_executions_count` entries.

### 2c. `TradeJournal.compute_session_stats`

Add an async method:

```python
async def compute_session_stats(self, min_trades: int = 3) -> dict:
```

Reads the last 20 completed trades (same filter as `load_recent`: `record_type == "close"`
or absent, `entry_price > 0`). Returns `{}` if fewer than `min_trades` are available.

Computes:
- `total_trades` — count
- `win_rate_pct` — integer percentage, trades where `pnl_pct > 0`
- `short_win_rate_pct` — same filter, `direction == "short"` only; **omit key** if no short trades
- `avg_hold_min` — mean of `hold_duration_min` rounded to int
- `avg_pnl_pct` — mean `pnl_pct` rounded to 2dp
- `high_conviction_win_rate_pct` — win rate for trades where `claude_conviction >= 0.75`;
  **omit key** if fewer than 3 such trades

### 2d. `execution_stats` context section

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

Call `await self._trade_journal.compute_session_stats(min_trades=config.execution_stats_min_trades)`
from `assemble_reasoning_context`. Since `assemble_reasoning_context` is not currently async,
this call requires it to become `async def`, or the stats must be computed upstream and passed
in as a parameter (like `recommendation_outcomes`). **Prefer computing in the orchestrator
before the `assemble_reasoning_context` call and passing as a parameter** — keeps
`assemble_reasoning_context` sync and consistent with its current design.

New config key:
- `execution_stats_min_trades: int = 3`

---

## 3. Add `ta_readiness` to Watchlist Tier 1 Context

Replace `technical_summary` (string) in each tier-1 watchlist entry with a structured
`ta_readiness` dict. Drop `technical_summary` to save tokens. **Do not** remove the
`_make_technical_summary` helper — it is still used by `run_position_review`.

`ta_readiness` is a direct pass-through of `indicators[symbol]["signals"]` — no new
computation needed. The existing `composite_score` key on the watchlist entry context block is
**replaced** by `composite_score` inside `ta_readiness` (remove the standalone key to avoid
Claude seeing two scores for the same symbol):

```json
"ta_readiness": {
  "above_vwap": true,
  "rsi": 58.2,
  "rsi_slope_5": 6.4,
  "macd_signal": "bullish",
  "macd_histogram_expanding": true,
  "roc_negative_deceleration": false,
  "volume_ratio": 1.85,
  "volume_trend_bars": 3,
  "trend": "bullish_aligned",
  "bb_squeeze": false,
  "composite_score": 0.72
}
```

Note: the key is `macd_signal` (not `macd`) — this matches the actual signals dict key.
`roc_negative_deceleration` is included because `ta_readiness` is a pass-through; omitting
it from the example was an oversight in the original spec.

**Direction-adjusted `composite_score`** (requires Section 5 first):
```python
direction = entry.expected_direction if entry.expected_direction != "either" else "long"
ta_readiness["composite_score"] = compute_composite_score(raw_signals, direction=direction)
```

Include `expected_direction` in the watchlist entry context block so Claude can see and revise it.

---

## 4. Watchlist Direction Tagging

### 4a. `WatchlistEntry` field

Add to `WatchlistEntry` in `core/state_manager.py`:
```python
expected_direction: str = "either"   # "long" | "short" | "either"
```

### 4b. `_from_dict_watchlist_entry`

Update the deserialiser to load the field with backward-compatible default:
```python
expected_direction=d.get("expected_direction", "either"),
```

### 4c. `_apply_watchlist_changes`

Extract `expected_direction` from Claude's add-item dict:
```python
expected_direction = item.get("expected_direction", "either")
```
Pass it to the new `WatchlistEntry` constructor call. Default `"either"` for backward
compatibility when Claude omits the field.

### 4d. Watchlist pruning

The pruning logic in `_apply_watchlist_changes` currently uses `max(long_score, short_score)`
as the prune score. After Phase 15, the direction-adjusted score is available via
`compute_composite_score(signals, direction=direction)`. Update the prune score to use the
direction-adjusted score when `expected_direction != "either"`. This ensures a short-thesis
symbol is not evicted because it has a weak long score.

### 4e. Prompt schema update

Update `watchlist_changes.add` schema in the prompt template to include `expected_direction`:
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
Field is optional — existing plain-string add format is already handled by `_apply_watchlist_changes`.

---

## 5. Update Prompt Template

Bump to `v3.5.0`. Create `config/prompts/v3.5.0/` directory, copy `reasoning.txt` from
`v3.4.0/`, update `ClaudeConfig.prompt_version` default and `config.json` value to `"v3.5.0"`.

**The new context sections are embedded inside `{context_json}` — no new template
placeholders are needed.**

Add instructions for the new context fields:

**For `recommendation_outcomes`**: "The `recommendation_outcomes` list shows what happened to
each symbol you recommended this session. `ranker_rejected` means the quant system blocked the
entry — check `stage_detail` for the reason (low RVOL, weak composite score, etc.) and
`rejection_count` for how many times. Do not re-recommend symbols with repeated
`ranker_rejected` status unless the underlying condition has changed — the quant system will
block them again. `conditions_waiting` means your `entry_conditions` gate has not triggered
yet. `gate_expired` means your gate was cleared after too many misses — reconsider the
conditions before recommending again. `order_pending` shows live orders with price drift from
your target."

**For `recent_executions`**: "Use `recent_executions` to calibrate. If recent momentum trades
stopped out at high frequency, consider raising conviction thresholds or tightening entry
conditions in new opportunities."

**For `ta_readiness`**: "The `ta_readiness` dict in each watchlist entry shows current
indicator values. Use these to calibrate `entry_conditions` in your opportunities — e.g., if
current `rsi` is 58 and you want upward momentum confirmed, specify `rsi_min: 60`. All values
are live at the time of this reasoning call."

**For `execution_stats`**: "Use `execution_stats` to calibrate conviction and position sizing.
If `short_win_rate_pct` is below 40%, reduce short opportunity conviction or tighten entry
conditions. If `high_conviction_win_rate_pct` is below 50%, your conviction scores are not
predictive — consider using the full conviction range rather than clustering near 0.8+."

**For `expected_direction` in watchlist entries**: "The `expected_direction` field shows the
directional thesis you assigned when adding this symbol. Confirm or revise it in your position
reviews. When adding symbols via `watchlist_changes.add`, always specify `expected_direction`
— it affects how TA scores and entry conditions are evaluated."

---

## 6. Tests to Write

Create `tests/test_context_enrichment.py`.

**`RankResult` and ranker interface:**
- `rank_opportunities` returns `RankResult` with `.candidates` and `.rejections`
- Hard-filtered symbol appears in `.rejections` with correct reason string
- Accepted symbol not in `.rejections`

**`_recommendation_outcomes` lifecycle:**
- Populated with `stage="ranker_rejected"` and `rejection_count=1` on first ranker rejection
- `rejection_count` increments to 2 on second rejection of same symbol
- Updated to `stage="conditions_waiting"` when entry_conditions defer fires
- Updated to `stage="gate_expired"` when max_entry_defer_cycles expiry fires
- Updated to `stage="order_pending"` with `order_id` after successful order placement
- Updated to `stage="filled"` on opening fill confirmation
- Updated to `stage="cancelled"` on cancel detection
- Stale entries (date != today UTC) purged at slow loop start; same-day entries retained
- Session-veto symbols do not appear in `_recommendation_outcomes`

**`recommendation_outcomes` context assembly:**
- `ranker_rejected` entry includes `rejection_count`
- `order_pending` entry includes correct `drift_pct = (current - target) / target * 100`
- `filled`/`cancelled` entries older than `recommendation_outcome_max_age_min` omitted
- Recent `filled`/`cancelled` entries (within max age) included
- List sorted by `age_min` ascending
- Capped at 15 entries (oldest dropped)

**`TradeJournal.load_recent`:**
- Returns last N `close` records in reverse-chronological order
- Includes records with absent `record_type` (pre-lifecycle)
- Excludes records with `entry_price == 0`
- Capped at `recent_executions_count`
- Returns mixed `record_type` journal correctly (open/snapshot/review/close interleaved)

**`TradeJournal.compute_session_stats`:**
- Returns `{}` when fewer than `execution_stats_min_trades` trades available
- `win_rate_pct` correct: 4 wins out of 10 total → 40
- `short_win_rate_pct` present when short trades exist; 0 when all shorts lost
- `short_win_rate_pct` key absent when no short trades in sample
- `high_conviction_win_rate_pct` omitted when fewer than 3 trades with `claude_conviction >= 0.75`

**`ta_readiness`:**
- Contains expected keys including `macd_signal` (not `macd`) and `roc_negative_deceleration`
- Values match `indicators[symbol]["signals"]`
- `composite_score` uses direction-adjusted scoring when `expected_direction="short"`
- `composite_score` uses long-default when `expected_direction="either"`
- Empty dict when symbol absent from `indicators`
- Old standalone `composite_score` key not present alongside `ta_readiness` in watchlist entry

**`WatchlistEntry.expected_direction`:**
- Persists through save/load cycle with value `"short"`
- Defaults to `"either"` when key absent from JSON (backward compat)
- `_apply_watchlist_changes` extracts `expected_direction` from Claude add-item and sets it on new entry
- Defaults to `"either"` when Claude add-item omits the field

**Backward compat / error handling:**
- `assemble_reasoning_context` with `indicators={}` → all new sections empty/`{}`, no crash
- `assemble_reasoning_context` with `recommendation_outcomes={}` → `recommendation_outcomes: []` in context

---

## Done When

- All existing tests pass; all `test_context_enrichment.py` tests pass
- `rank_opportunities` returns `RankResult`; all existing ranker tests updated
- Claude's assembled context JSON includes `recommendation_outcomes`, `recent_executions`,
  `ta_readiness`, `execution_stats`, and `expected_direction` in watchlist entries
- `TradeJournal.load_recent(n)` and `TradeJournal.compute_session_stats()` implemented,
  async, and lock-safe
- `WatchlistEntry.expected_direction` field present, persisted, defaulting correctly, and
  used for direction-adjusted scoring and pruning
- Prompt bumped to `v3.5.0`
- DRIFT_LOG.md has a Phase 15 entry covering all additions
