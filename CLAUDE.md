# Ozymandias v3 — Automated Trading Bot

## What This Is
An automated stock trading bot using Claude API for strategic reasoning + quantitative technical analysis for execution. Targets aggressive momentum and swing trading on high-volatility, high-liquidity equities. Alpaca paper trading initially.

## Full Spec
The complete specification is in `ozymandias_v3_spec_revised.md` at the project root. Consult relevant spec sections to resolve ambiguities. The Spec Drift Log (see below) takes precedence where it contradicts the spec.

## Architecture (Mental Model)
```
External Data (yfinance) → Orchestrator (3 async loops) → Intelligence (Claude + TA + Ranker) → Execution (Risk + Broker)
                                  ↕
                          Persistent State (JSON files)
```

- **Fast loop (5-15s):** Order fills, fill protection, quant overrides, PDT guard, position sync
- **Medium loop (1-5min):** Technical scans, signal detection, opportunity ranking, position re-eval
- **Slow loop (event-driven, checked every 5min):** Claude reasoning, watchlist management, news digest, thesis review. Only calls Claude when a trigger fires (price move, time ceiling, session transition, etc.)

## Hard Technical Constraints
- **Python 3.12+**, asyncio throughout (no threading/multiprocessing)
- **No third-party TA libraries** (no pandas-ta, no ta-lib). All indicators hand-rolled with pandas + numpy in `intelligence/technical_analysis.py`
- **Timezone:** All internal timestamps UTC. Market hours logic uses `zoneinfo.ZoneInfo("America/New_York")`. Never rely on local system clock timezone.
- **Claude JSON parsing:** Expect ~5% malformed. 4-step defensive pipeline: strip fences → json.loads → regex extract → skip cycle. Never crash on bad Claude output.
- **State files:** JSON with atomic writes (write temp file, then rename). Validate schemas on startup.
- **Broker abstraction:** All broker-specific code behind `BrokerInterface` ABC. No broker imports outside `execution/alpaca_broker.py`.

## Key Design Rules
- Modules communicate via interfaces and JSON, never direct coupling
- Only the orchestrator knows about all other modules
- Prompt templates are versioned files in `config/prompts/`, never hardcoded
- Risk manager has override authority over everything — can cancel orders, force exits, block entries
- Fill protection: never place a new order for a symbol that has a PENDING or PARTIALLY_FILLED order
- PDT buffer: default reserve 1 of 3 allowed day trades for emergency exits
- Never hardcode tunable parameters (thresholds, multipliers, intervals, weights, limits). Put them in config.json with a comment in the code explaining what the config value controls.
- Entry limit orders use current market price (from `_latest_indicators`), never Claude's cached
  `suggested_entry`. Claude's suggested price is a reference for drift checks only.
- Claude specifies per-trade `entry_conditions` in `new_opportunities` output. The medium loop
  evaluates these against current indicator values before entering. Absent conditions = no gate.
  This is the bridge between Claude's per-ticker knowledge and TA's real-time confirmation.
- Never modify files in `phases/` or `ozymandias_v3_spec_revised.md` unless explicitly instructed.
  Phase files are historical records; spec deviations go in DRIFT_LOG.md and CLAUDE.md only.

## Testing Standards
- Every module gets unit tests. TA functions tested against hand-calculated values.
- Fill protection tested for all edge cases: partial fills, cancel-during-fill race, unexpected fills.
- Use pytest + pytest-asyncio. Mock broker and external APIs — never hit real APIs in unit tests.
- **When tests fail:** First categorize every failure — is the code wrong or is the test wrong? Fix all code bugs first, then fix broken tests in a single batch. If a test asserts implementation details (exact mock call counts, internal state shapes, specific log strings) rather than behavioral outcomes, rewrite it to test behavior. Do not fix tests one at a time to make them green.

## Workflow Rules
- **Before starting work:** Read this file. Read the current phase prompt only — not previous ones.
  Consult the spec only to resolve a specific ambiguity; don't re-read it in full.
- **Bug handling during implementation:**
  - Bug in code you're currently building → fix immediately before continuing
  - Bug in a previously built module that blocks you → fix it
  - Bug in a previously built module that doesn't block you → add a `# BUG: description` comment and continue
  - Never add broad `try/except` or unnecessary `None` checks to work around bugs in other modules
- **Updating this file:** Update CLAUDE.md (especially the Spec Drift Log) when: a module's interface
  changes from what the phase file specified, a dependency or assumption is discovered that affects
  future phases, or a meaningful architectural decision is made. Do not modify phase files or the
  spec file — document deviations here instead.
- **Never modify phase documents or the spec file.** `phases/` files and `ozymandias_v3_spec_revised.md`
  are immutable historical records. All deviations, decisions, and post-MVP additions belong in
  DRIFT_LOG.md and CLAUDE.md only. Only create new phase files when explicitly instructed to do so.

## Adding Features Beyond the Spec
Features not described in the spec may be requested. When implementing them, follow existing patterns:
- New strategies → implement the `Strategy` ABC in `strategies/`
- New Claude calls → new method in `claude_reasoning.py` + new prompt template in `config/prompts/`
- New persistent state → extend existing JSON schemas in `state/`, don't create new files
- New loop logic → integrate into the orchestrator's existing loop structure
- All new code uses `async`, `get_logger()`, and `StateManager`
- New Claude output fields (like `entry_conditions`) → add to prompt schema in `config/prompts/vN.N.N/reasoning.txt`, add to `ReasoningResult` or carry through `ScoredOpportunity`, add evaluator function in `intelligence/opportunity_ranker.py`

## Post-MVP Status

All 10 spec phases complete per `ozymandias_v3_spec_revised.md`. Post-MVP phases (11–14) are addressing architectural gaps discovered during paper trading.

Last spec phase completed: Phase 10 (March 15)
Last post-MVP phase completed: *(none yet — see roadmap below)*

### Completed post-MVP work
- **Anti-bias hardening** (March 15): conviction sanity floor, `rejected_opportunities` logging,
  adversarial `updated_reasoning` prompts, thesis challenge for large positions
- **Paper session bug fixes** (March 16): 9 bugs fixed — re-adoption runaway, Claude exits ignored,
  short direction inference, fill dispatch routing, ghost cleanup race, test isolation,
  cash sync, exit price fallback, PDT broker floor (see BUGS_2026-03-16.md)
- **Index ticker blacklist + dead field cleanup** (March 16): Anomalies 10 + 12 fixed

### Post-MVP Roadmap: Phases 11–14

- **Phase 11 — Execution Fidelity**: see `phases/11_execution_fidelity.md`
- **Phase 12 — Claude-Directed Entry Conditions**: see `phases/12_claude_directed_entry_conditions.md`
- **Phase 13 — Context Enrichment**: see `phases/13_context_enrichment.md`
- **Phase 14 — Short Position Protection**: see `phases/14_short_protection.md`

## Spec Drift Log
See `DRIFT_LOG.md`. Read the relevant phase section of DRIFT_LOG.md before modifying or debugging any module built in a previous phase.
