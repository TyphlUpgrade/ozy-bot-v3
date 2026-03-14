# Phase 07: Opportunity Ranker

Read section 4.5 (Opportunity Ranker) of `ozymandias_v3_spec_revised.md`.

## Context
Phases 01-06 gave us everything the ranker needs as inputs: Claude reasoning output (`ReasoningResult` with conviction scores and trade suggestions), technical analysis signals (composite technical scores), market data (for liquidity calculation), and risk management (for hard filters). The ranker is the bridge between intelligence and execution.

## What to Build

### 1. Opportunity ranker (`intelligence/opportunity_ranker.py`)

Implement an `OpportunityRanker` class:

**Composite scoring:**
- `score_opportunity(opportunity, technical_signals, account_info, portfolio) -> ScoredOpportunity`: Apply the ranking formula from section 4.5:
  ```
  composite = (ai_conviction * W_ai) + (technical_score * W_tech) + (risk_adjusted_return * W_risk) + (liquidity_score * W_liq)
  ```
  Default weights from config: W_ai=0.35, W_tech=0.30, W_risk=0.20, W_liq=0.15.

- **Risk-adjusted return:** `(exit - entry) / (entry - stop)`, capped at 5:1 and normalized to 0-1. Handle edge cases: if stop >= entry (invalid setup), return score of 0.

- **Liquidity score:** `min(avg_daily_volume / 1_000_000, 1.0)`.

**Hard filters (applied before scoring — any failure removes the opportunity):**
- `apply_hard_filters(opportunity, account_info, portfolio, pdt_guard, market_hours) -> tuple[bool, str]`: Return (passes, rejection_reason). Checks:
  - Sufficient buying power for intended position size
  - Would not exceed max concurrent positions (8)
  - Would not violate PDT limits (delegate to PDT guard)
  - Market is in regular hours (or explicitly flagged for extended hours)
  - Stock has minimum 100,000 avg daily volume

**Ranking pipeline:**
- `rank_opportunities(reasoning_result, technical_signals, account_info, portfolio, pdt_guard, market_hours) -> list[ScoredOpportunity]`: The main entry point.
  1. Extract new opportunities from Claude's reasoning output
  2. Apply hard filters — remove any that fail
  3. Score remaining opportunities
  4. Sort by composite score descending
  5. Return the ranked list

Also handle position reviews from Claude:
- `rank_exit_actions(reasoning_result, technical_signals) -> list[ExitAction]`: Extract Claude's position review recommendations (hold/exit/adjust) and combine with technical signals to produce prioritized exit/adjustment actions.

**Data types:**
- `ScoredOpportunity`: symbol, action, strategy, composite_score, ai_conviction, technical_score, risk_adjusted_return, liquidity_score, suggested_entry, suggested_exit, suggested_stop, position_size_pct, reasoning
- `ExitAction`: symbol, action (hold/exit/adjust), urgency (float 0-1), reasoning, adjusted_targets (optional)

### 2. Integration with existing modules

The ranker should import and use:
- `ReasoningResult` from claude_reasoning
- Technical signal output from technical_analysis
- `RiskManager` for position sizing (if the ranker decides to refine Claude's suggested size)
- `PDTGuard` for day trade filtering
- Market hours utility for session checking

Don't duplicate validation logic — delegate to the existing modules.

## Tests to Write

Create `tests/test_opportunity_ranker.py`:
- Test composite score calculation with known inputs (verify math)
- Test risk-adjusted return calculation, including edge cases (stop >= entry, very high ratio capped at 5:1)
- Test liquidity score scaling (100k volume, 500k volume, 2M volume)
- Test hard filter: insufficient buying power rejects opportunity
- Test hard filter: max positions reached rejects
- Test hard filter: PDT violation rejects
- Test hard filter: outside market hours rejects
- Test hard filter: low volume (<100k) rejects
- Test full ranking pipeline: give it 5 opportunities with different scores, verify correct sort order
- Test that opportunities failing hard filters are excluded from the ranked output
- Test exit action ranking with mixed hold/exit/adjust recommendations

## Done When
- All tests pass
- You can feed a mock `ReasoningResult` and mock technical signals through the full pipeline and get a correctly ranked list of `ScoredOpportunity` objects
- Hard filters correctly block invalid opportunities before they're ever scored
