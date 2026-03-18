# Phase 18: Watchlist Intelligence — Live Market Discovery

Read the Phase 13 section of DRIFT_LOG.md and the Phase 15 section (expected_direction, ta_readiness) before starting. This phase assumes Phases 12, 13, and 15 are complete.

`run_watchlist_build` currently asks Claude to invent tickers from training memory with only SPY/sector trend as input. Claude has no knowledge of what is actually moving today, what earnings are due this week, or what news catalysts are live. This phase closes that gap in two ways: a live RVOL-ranked candidate pipeline that gives Claude real market data to evaluate, and web search tool use that lets Claude actively research current catalysts — matching the discovery capability of a search-grounded AI.

The design intention is full automation: no static files to maintain, no human curation step, no symbols hardcoded anywhere.

---

## 1. Dynamic Universe Fetcher (`intelligence/universe_fetcher.py`)

New module. `UniverseFetcher` builds a live candidate universe from two automated sources:

**Source A — Yahoo Finance screener (today's activity):**

Hit the Yahoo Finance screener JSON endpoint that yfinance uses internally:
```
https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved
  ?formatted=true&scrIds=most_actives&count=50
```
Run once for `most_actives` and once for `day_gainers` (count=25 each). Parse the `quotes` array from the JSON response. Extract `symbol` strings. Dedup and return up to 75 symbols.

This is a GET request with no API key. Wrap in `asyncio.to_thread` (consistent with adapter pattern). On any failure, return `[]` — this source is best-effort.

**Source B — S&P 500 + Nasdaq 100 from Wikipedia (structural universe):**

```python
import pandas as pd
sp500 = pd.read_html(
    'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
    attrs={'id': 'constituents'}
)[0]['Symbol'].tolist()
ndx100 = pd.read_html(
    'https://en.wikipedia.org/wiki/Nasdaq-100',
    attrs={'id': 'constituents'}
)[0]['Ticker'].tolist()
```

Wrap in `asyncio.to_thread`. Cache result with a 24-hour TTL — index constituents change quarterly, not intraday. On failure, return `[]`.

**`get_universe() -> list[str]`:**

Runs both sources concurrently via `asyncio.gather`. Merges results (Source A first — today's active names take precedence). Dedup preserving order. Filter: remove symbols from `config.ranker.no_entry_symbols`. Clean any symbols with non-alphabetic characters (`.`, `-`, spaces — ETF classes and foreign listings that Alpaca won't trade). Return the merged list.

**Why both sources:** Source A surfaces high-volatility names actually moving today (includes mid-caps, momentum names, sector leaders). Source B provides a deep structural bench of liquid names for when the market is quiet and the active list is thin. Together they cover the full universe of what a momentum/swing strategy would ever trade.

---

## 2. Universe Scanner (`intelligence/universe_scanner.py`)

New module. `UniverseScanner` takes the universe list and produces a ranked candidate list for Claude.

### `UniverseScanner.__init__`

```python
def __init__(self, data_adapter: DataAdapter, config: "UniverseScannerConfig") -> None:
```

### `get_top_candidates(n, exclude, blacklist) -> list[dict]`

1. Call `UniverseFetcher.get_universe()` to get the live symbol list.
2. Remove symbols in `exclude` (already on watchlist) and `blacklist` (no-entry list).
3. Fetch `5m/5d` bars for all remaining symbols using `asyncio.gather` with `asyncio.Semaphore(config.scan_concurrency)`. This reuses `data_adapter.fetch_bars` — the 5-min cache means symbols already fetched by the medium loop are free hits.
4. Run `generate_signal_summary` on each successful DataFrame.
5. Filter: drop symbols where `bars_available < 5` or `volume_ratio < config.min_rvol_for_candidate`.
6. Sort by `volume_ratio` descending — RVOL is direction-neutral and surfaces what is genuinely active today regardless of direction.
7. For the top `min(n * 2, 60)` symbols after RVOL sort: fetch news via `data_adapter.fetch_news(symbol, max_items=2)` and earnings calendar via `_fetch_earnings_calendar(symbol)` (see below). Run both concurrently per symbol.
8. Return the top `n` as a list of candidate dicts (schema below).

**Candidate dict schema:**
```python
{
    "symbol": "NVDA",
    "rvol": 2.8,
    "technical_summary": "VWAP above, RSI 62, MACD bullish cross, trend bullish_aligned, ROC +1.8%",
    "composite_score": 0.74,    # long-biased; Claude should weigh direction independently
    "price": 487.20,
    "recent_news": [            # up to 2 items; [] if none
        {"title": "NVIDIA announces ...", "publisher": "Reuters", "age_hours": 2.1}
    ],
    "earnings_within_days": 3,  # None if no earnings found in next 10 days
}
```

**`_fetch_earnings_calendar(symbol) -> int | None`:**

```python
ticker = yf.Ticker(symbol)
cal = ticker.calendar
```

yfinance returns a dict with an `"Earnings Date"` key containing a list of datetimes. Extract the next earnings date, compute `days_until = (earnings_date.date() - date.today()).days`. Return `days_until` if `0 <= days_until <= 10`, else `None`. Wrap in `asyncio.to_thread`. Return `None` on any exception — earnings data is best-effort.

**Session cache:** store the result of `get_top_candidates` on the orchestrator as `_last_universe_scan: list[dict]` with a timestamp. If `watchlist_small` fires again within `config.universe_scanner.cache_ttl_min` minutes, return the cached result without re-scanning.

---

## 3. Search Adapter (`data/adapters/search_adapter.py`)

New module. `SearchAdapter` wraps the Brave Search API.

```python
class SearchAdapter:
    def __init__(self, api_key: str | None) -> None:
        self._api_key = api_key
        self._enabled = bool(api_key)

    async def search(self, query: str, n_results: int = 5) -> list[dict]:
        """
        Returns list of {title, url, description} dicts.
        Returns [] if disabled (no API key) or on any exception.
        """
```

Endpoint: `https://api.search.brave.com/res/v1/web/search?q={query}&count={n_results}`
Header: `X-Subscription-Token: {api_key}`

Wrap HTTP call in `asyncio.to_thread` using `urllib.request` (no new dependencies). Parse JSON response: extract `web.results` array, map each to `{title, url, description}`. Cap at `n_results`.

On any exception (network error, bad API key, rate limit): log WARNING and return `[]`. The adapter never raises — a failed search degrades gracefully to Claude reasoning without search results.

**API key:** read from `credentials.enc` (existing credentials system) under key `brave_search_api_key`. If absent, `SearchAdapter` is instantiated with `api_key=None` and silently disabled. Log a one-time INFO at startup: "Brave Search not configured — watchlist build will use screener data only."

---

## 4. Tool Use in `call_claude` (`intelligence/claude_reasoning.py`)

### New method: `call_claude_with_tools`

```python
async def call_claude_with_tools(
    self,
    prompt_template: str,
    context: dict,
    tools: list[dict],
    tool_executor: Callable[[str, dict], Awaitable[str]],
    max_tokens_override: int | None = None,
    max_tool_rounds: int = 3,
) -> str:
```

Multi-turn conversation loop:

1. Fill the prompt template (same identifier-only substitution as `call_claude`).
2. Build `messages = [{"role": "user", "content": filled_prompt}]`.
3. Loop up to `max_tool_rounds`:
   - Call `self._client.messages.create(model=..., messages=messages, tools=tools, max_tokens=...)`.
   - If `response.stop_reason != "tool_use"`: extract text from the last content block and return it.
   - For each `tool_use` block in `response.content`: call `await tool_executor(block.name, block.input)`, collect results.
   - Append assistant message and tool results to `messages`.
4. If rounds exhausted: force a final call with `tools=[]` and `tool_choice={"type": "none"}` to get a text response. Log WARNING "Tool use rounds exhausted — forcing final response."

All retry/fallback logic from `call_claude` applies: same circuit breaker, same error handling. The method is self-contained — the rest of the system doesn't know tool use happened.

### Tool definition: `web_search`

```python
_WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web for current financial news, earnings calendars, analyst "
        "actions, and market catalysts. Use 2–3 targeted queries per watchlist "
        "build. Focus on near-term catalysts: earnings this week, analyst upgrades, "
        "sector rotation, breakout setups with news backing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query string."}
        },
        "required": ["query"],
    },
}
```

### Tool executor in `run_watchlist_build`

```python
async def _execute_search_tool(name: str, inputs: dict) -> str:
    if name != "web_search":
        return "Unknown tool."
    results = await search_adapter.search(inputs.get("query", ""), n_results=5)
    if not results:
        return "No results returned."
    return "\n\n".join(
        f"{r['title']}\n{r['description']}" for r in results
    )
```

---

## 5. Updated `run_watchlist_build` (`intelligence/claude_reasoning.py`)

```python
async def run_watchlist_build(
    self,
    market_context: dict,
    current_watchlist: WatchlistState,
    target_count: int = 20,
    candidates: list[dict] | None = None,
    search_adapter: "SearchAdapter | None" = None,
) -> Optional[WatchlistResult]:
```

Build context dict:
```python
ctx = {
    "current_date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    "market_context":     json.dumps(market_context, default=str, indent=2),
    "current_watchlist":  watchlist_str,
    "target_count":       target_count,
    "candidates":         json.dumps(candidates, indent=2) if candidates else "none",
}
```

If `search_adapter` is provided and `search_adapter._enabled`:
- Use `call_claude_with_tools(template, ctx, tools=[_WEB_SEARCH_TOOL], tool_executor=...)`
Else:
- Use `call_claude(template, ctx)` (existing behavior — no tools offered)

This means: if no API key is configured, the behavior is identical to today. Graceful degradation is complete.

---

## 6. New Prompt Template (`config/prompts/v3.4.0/watchlist.txt`)

Create `config/prompts/v3.4.0/` by copying all files from `v3.3.0/`. Update `watchlist.txt`:

```
You are the watchlist manager for the Ozymandias trading system.

CURRENT DATE: {current_date}
TARGET WATCHLIST SIZE: {target_count} tickers

MARKET CONTEXT:
{market_context}

CURRENT WATCHLIST (may be empty or small):
{current_watchlist}

LIVE SCREENER CANDIDATES — SYMBOLS ACTIVE TODAY:
The following symbols were pre-screened from a live universe (S&P 500, Nasdaq 100, today's most-active list) for elevated relative volume right now. Each entry shows: symbol, RVOL, current technical summary, composite score (long-biased — evaluate direction independently), recent news, and days until earnings (if within 10 days).

{candidates}

INSTRUCTIONS:
- Use the candidates above as your PRIMARY source for new watchlist additions. These are what is actually moving today.
- If you have a web_search tool available, use 2–3 targeted queries to find additional catalyst-driven setups not captured above (e.g., "stocks with earnings this week momentum", "analyst upgrades today technology sector"). Prefer queries that surface near-term catalysts.
- You may add symbols not in the candidate list if you find a specific current catalyst through search or know of a confirmed near-term catalyst (earnings, FDA decision, known event). Do not add symbols purely from memory.
- Pay particular attention to candidates with earnings_within_days <= 5 — these are pre-earnings setups.
- For symbols with recent_news present, use that context to inform your reasoning field.

[rest of existing tier assignment rules and format unchanged]
```

Update `config.json`: `claude.prompt_version` → `"v3.4.0"`.

---

## 7. Config (`core/config.py` + `config/config.json`)

**`UniverseScannerConfig` dataclass:**

```python
@dataclass
class UniverseScannerConfig:
    enabled: bool = True
    scan_concurrency: int = 20          # parallel yfinance fetches
    max_candidates: int = 50            # top N by RVOL sent to Claude
    min_rvol_for_candidate: float = 0.8 # drop stale/inactive symbols
    cache_ttl_min: int = 60             # reuse scan result within same session
```

**`SearchConfig` dataclass:**

```python
@dataclass
class SearchConfig:
    enabled: bool = True
    max_searches_per_build: int = 3     # cap on tool_use rounds
    result_count_per_query: int = 5
```

Add both to the top-level `Config` dataclass. Add corresponding `universe_scanner` and `search` sections to `config.json`.

---

## 8. Orchestrator Wiring (`core/orchestrator.py`)

In `__init__`:
```python
self._universe_scanner = UniverseScanner(self._data_adapter, self._config.universe_scanner)
self._search_adapter = SearchAdapter(api_key=self._credentials.get("brave_search_api_key"))
self._last_universe_scan: list[dict] = []
self._last_universe_scan_time: float = 0.0
```

In the slow loop, before calling `run_watchlist_build` (triggered by `watchlist_small`):

```python
if self._config.universe_scanner.enabled:
    cache_age_min = (time.monotonic() - self._last_universe_scan_time) / 60
    if cache_age_min > self._config.universe_scanner.cache_ttl_min or not self._last_universe_scan:
        watchlist = await self._state_manager.load_watchlist()
        existing = {e.symbol for e in watchlist.entries}
        blacklist = set(self._config.ranker.no_entry_symbols)
        self._last_universe_scan = await self._universe_scanner.get_top_candidates(
            n=self._config.universe_scanner.max_candidates,
            exclude=existing,
            blacklist=blacklist,
        )
        self._last_universe_scan_time = time.monotonic()
        log.info(
            "Universe scan complete: %d candidates (top RVOL: %s)",
            len(self._last_universe_scan),
            [c["symbol"] for c in self._last_universe_scan[:5]],
        )

result = await self._claude.run_watchlist_build(
    market_context=market_data,
    current_watchlist=watchlist,
    candidates=self._last_universe_scan or None,
    search_adapter=self._search_adapter,
)
```

---

## 9. Tests to Write

**`tests/test_universe_fetcher.py`:**
- `get_universe()` merges Source A and Source B, deduplicates, and returns a list of strings
- Source A failure returns `[]` without raising
- Source B failure returns `[]` without raising
- `no_entry_symbols` are filtered from the result
- Symbols with non-alphabetic characters are filtered (e.g. `"BRK.B"`)

**`tests/test_universe_scanner.py`:**
- `get_top_candidates` returns candidates sorted by `volume_ratio` descending
- Symbols in `exclude` set are not returned
- Symbols with `bars_available < 5` are filtered
- Symbols with `volume_ratio < min_rvol_for_candidate` are filtered
- `earnings_within_days` is `None` when calendar raises an exception
- `earnings_within_days` is populated when calendar returns a date within 10 days
- `earnings_within_days` is `None` when next earnings date is > 10 days away
- `recent_news` is `[]` when `fetch_news` returns empty list
- `recent_news` is capped at 2 items
- Candidate count capped at `n`
- Empty universe (all fetches fail) returns `[]` without raising

**`tests/test_search_adapter.py`:**
- `search()` returns `[]` when `api_key=None` (disabled)
- `search()` returns `[]` on network exception without raising
- `search()` returns parsed result list on successful HTTP response (mock)
- Result count capped at `n_results`

**`tests/test_watchlist_build_tool_use.py`:**
- When `search_adapter._enabled=True`: `call_claude_with_tools` is used, not `call_claude`
- When `search_adapter` is `None`: `call_claude` is used (existing behavior)
- Tool executor returns search results as a string
- Tool executor returns "No results returned." when `search()` returns `[]`
- `call_claude_with_tools`: tool use round → results returned → final response extracted
- `call_claude_with_tools`: rounds exhausted → final call made without tools → text returned
- Existing `run_watchlist_build` parse + return behavior unchanged

---

## 10. What Is NOT in This Phase

- **General market news RSS feed**: The screener candidates + yfinance news + web search cover this use case. An RSS fetch adds maintenance surface for marginal gain.
- **Storing universe scan in persistent state**: The scan is in-memory only. Loss on restart is acceptable — the scan re-runs at the next `watchlist_small` trigger, which fires on startup if the watchlist is thin.
- **Returning search result URLs to Claude in context**: Only title + description are forwarded. URLs add token overhead with no analytical value for Claude.

---

## Done When

- All existing tests pass; all new tests pass
- `UniverseFetcher.get_universe()` returns a non-empty merged list during a live session (integration check)
- `UniverseScanner.get_top_candidates()` returns candidates with `rvol`, `technical_summary`, `recent_news`, and `earnings_within_days` fields
- `run_watchlist_build` with a configured Brave API key calls `call_claude_with_tools` and the search tool is exercised (visible in reasoning cache as a multi-turn exchange)
- `run_watchlist_build` with no Brave API key behaves identically to pre-Phase-18 (no regression)
- Claude's watchlist build output in a paper session names at least some symbols sourced from the candidates list (visible in watchlist state `reason` fields referencing RVOL or news)
- `config.json` updated: `prompt_version = "v3.4.0"`, `universe_scanner` and `search` sections present
- DRIFT_LOG.md has a Phase 18 entry covering: `UniverseFetcher`, `UniverseScanner`, `SearchAdapter`, `call_claude_with_tools`, `run_watchlist_build` signature change, prompt version bump to v3.4.0
