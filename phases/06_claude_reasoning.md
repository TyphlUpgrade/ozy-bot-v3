# Phase 06: Claude AI Reasoning Integration

Read section 4.3 (Claude AI Reasoning Module) and section 5.5 (Reasoning Cache) of `ozymandias_v3_spec_revised.md`.

## Context
Phases 01-05 gave us: state management, broker, fill protection, market data, technical analysis, and risk management. Now we add the strategic brain. Claude provides high-level trade reasoning — what to buy, when to exit, watchlist management. The quantitative systems from previous phases handle execution safety.

## What to Build

### 1. Prompt templates (`config/prompts/v3.3.0/`)

Create three prompt template files. These are plain text with `{placeholder}` variables that get filled at runtime:

**`reasoning.txt`** — The main reasoning prompt. This is the most important prompt in the system.
- System prompt that establishes Claude as a professional equities trader focused on aggressive momentum and swing trading.
- Instructs Claude to return ONLY a JSON object (no markdown, no explanation outside the JSON).
- Defines the exact JSON output schema (from section 4.3's "Expected output" example).
- Emphasizes: short-term opportunities (days) are primary focus, medium-term (weeks) is secondary, long-term is almost never.
- Includes `{portfolio_context}`, `{watchlist_tier1}`, `{market_context}` placeholders.
- Remind Claude of position limits, PDT constraints, and current buying power in the prompt.

**`watchlist.txt`** — Used when the watchlist needs significant changes (startup, or when fewer than 10 tickers).
- Asks Claude to build/replenish a watchlist of momentum and swing candidates.
- Includes `{market_context}`, `{current_watchlist}`, `{target_count}` placeholders.
- Output schema: list of symbols with reasoning and strategy classification.

**`review.txt`** — Used for focused position review when a specific position needs attention.
- Provides context about a single position and asks: thesis intact? adjust targets? exit now?
- Includes `{position_detail}`, `{market_context}`, `{indicators}` placeholders.
- Output schema: action (hold/exit/adjust), reasoning, adjusted targets if any.

### 2. Context assembler (`intelligence/claude_reasoning.py`)

Implement a `ClaudeReasoningEngine` class:

**Context assembly:**
- `assemble_reasoning_context(portfolio, watchlist, market_data, indicators) -> dict`: Build the input context JSON from section 4.3. This pulls together:
  - Portfolio state with current prices and unrealized P&L
  - Tier 1 watchlist symbols with technical summaries
  - Market context (SPY trend, VIX, sector rotation, macro events, session info, PDT trades remaining)
- Tier 1 = current positions + top watchlist candidates (up to `tier1_max_symbols` from config, default 12). Full context for each.
- Tier 2 = remaining watchlist (minimal context — just symbol and latest price). Tier 2 is NOT sent to Claude, only used locally for technical scanning.
- Target 4,000-8,000 input tokens per cycle. If context exceeds this, truncate Tier 1 down.

**Claude API integration:**
- `async call_claude(prompt_template: str, context: dict) -> str`: Fill the template with context, call the Anthropic API, return the raw response text.
- Use the `anthropic` Python SDK with async support.
- Model and max_tokens come from config.
- Implement retry with exponential backoff (base 30s, max 10min) for rate limits and 5xx errors.
- Timeout: 120 seconds per call. Log a WARNING if a call takes >60s.

**Defensive JSON parsing:**
- `parse_claude_response(raw_text: str) -> dict | None`: Implement the exact 4-step pipeline from section 4.3:
  1. Strip markdown code fences (`\`\`\`json` and `\`\`\``)
  2. Attempt `json.loads`
  3. On failure, regex extract JSON object `\{.*\}` with `re.DOTALL`
  4. On second failure, log the raw response and return None
- **Never crash on bad Claude output.** A None return means "skip this cycle."

**High-level reasoning methods:**
- `async run_reasoning_cycle(portfolio, watchlist, market_data, indicators) -> ReasoningResult | None`: Full reasoning cycle — assemble context, call Claude, parse response, cache result. Returns None if parsing fails.
- `async run_watchlist_build(market_context, current_watchlist) -> WatchlistResult | None`: Dedicated watchlist population cycle.
- `async run_position_review(position, market_context, indicators) -> ReviewResult | None`: Focused single-position review.

Define `ReasoningResult`, `WatchlistResult`, `ReviewResult` dataclasses that mirror the expected output schemas from section 4.3.

### 3. Reasoning cache integration

Wire the cache from Phase 01's reasoning cache manager:
- After every successful Claude call, save to cache.
- On startup, check for a reusable cached response (same trading day, <60 minutes old).
- Track token usage (input + output) per call for cost monitoring.

## Tests to Write

Create `tests/test_claude_json_parsing.py` — this is critical:
- Test clean JSON parses correctly
- Test JSON wrapped in ```json code fences parses correctly
- Test JSON with trailing text after the closing brace parses correctly
- Test JSON with leading text before the opening brace parses correctly
- Test completely malformed text returns None (doesn't crash)
- Test empty string returns None
- Test JSON with single-line comments (Claude sometimes adds these) — your regex approach should handle this
- Test a realistic Claude response string (copy the example from section 4.3)

Create `tests/test_claude_reasoning.py`:
- Mock the Anthropic API client
- Test context assembly produces valid JSON under the token target
- Test that tier 1 symbol count respects config limit
- Test retry logic on simulated API failures
- Test that a cached response is loaded on startup when valid
- Test that an expired cache is ignored

## Done When
- All tests pass
- You can run a manual test that calls Claude with real context from yfinance data and gets a parseable response
- The defensive JSON parser handles all the edge cases without crashing
- Prompt templates are clean, versioned files — not hardcoded strings
