# Phase 27: Strategy Analyst Agent

Read `plans/2026-04-07-agentic-workflow-v4-omc-only.md` § Phase D (lines ~1243-1280).

**Implementation dependency:** Phase 22 (Signal File API), Phase 24 (Conductor — spawns
the Analyst session).

**Context:** The Strategy Analyst runs post-market, reads the trade journal, categorizes
outcomes, and writes structured findings. The Conductor spawns it after session close.
Findings feed into the development backlog via `state/agent_tasks/`.

---

## What to Build

### 1. Strategy Analyst role definition (`config/agent_roles/strategy_analyst.md`)

Includes:
- Role definition (post-market analyst, not a developer)
- Four-category outcome classification (signal present/ignored, signal present/filtered,
  signal ambiguous, truly unforeseeable)
- Missed opportunity analysis (watchlist symbols that moved but weren't entered)
- Ontologist pressure-test (cross-reference NOTES.md and findings log before reporting)
- Hindsight bias prevention (must cite specific signal/indicator values at decision time)
- Output convention: write findings to `state/signals/analyst/<date>/findings.json`
- Findings log awareness (`state/analyst_findings_log.json`)

### 2. Findings log schema (`state/analyst_findings_log.json`)

Conductor appends each processed finding with status and date. Analyst reads this to
avoid re-discovering known issues.

```json
[
  {
    "date": "2026-04-07",
    "finding_id": "2026-04-07-nke-oversold-bounce",
    "category": "signal_present_bot_filtered",
    "status": "queued|completed|dismissed",
    "summary": "NKE oversold bounce — BB squeeze + RSI 22 at entry time"
  }
]
```

### 3. Findings output schema

```json
{
  "date": "2026-04-07",
  "trades_analyzed": 12,
  "findings": [
    {
      "finding_id": "<date>-<symbol>-<short-desc>",
      "category": "signal_present_bot_ignored|signal_present_bot_filtered|signal_ambiguous|truly_unforeseeable",
      "symbol": "NKE",
      "signal_citation": "BB squeeze firing, RSI 22 at 10:15 ET",
      "recommendation": "Lower BB squeeze threshold from 0.03 to 0.02",
      "severity": "low|medium|high"
    }
  ]
}
```

---

## Tests to Write

Create `ozymandias/tests/test_strategy_analyst.py`:

- `test_analyst_role_file_exists` — verify file exists
- `test_analyst_role_has_frontmatter` — verify YAML frontmatter
- `test_analyst_role_has_categories` — verify 4 outcome categories mentioned
- `test_analyst_role_has_hindsight_gate` — verify hindsight bias prevention
- `test_analyst_role_has_ontologist` — verify Ontologist pressure-test
- `test_findings_output_schema` — verify expected fields
- `test_findings_log_schema` — verify log entry fields

---

## Done When

1. `config/agent_roles/strategy_analyst.md` exists with complete role definition
2. Four-category outcome classification defined
3. Hindsight bias prevention gate specified
4. Ontologist pressure-test documented
5. Findings output and log schemas defined
6. Tests pass
