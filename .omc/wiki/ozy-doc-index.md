---
title: Ozymandias Documentation Index
tags: [ozymandias, navigation, index, trading-bot]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# Ozymandias Documentation Index

Navigation hub for all Ozymandias (trading bot) documentation in the wiki. Maps original root files to their wiki equivalents and provides quick-reference routing.

---

## Source File Routing

| Original file | Wiki equivalent(s) | Notes |
|---------------|-------------------|-------|
| `DRIFT_LOG.md` | [[ozy-drift-log]] (active) + 6 frozen archives | Spec deviations (Spec/Impl/Why). New entries go in active page only. |
| `NOTES.md` (Ozy concerns) | [[ozy-open-concerns]] | CONCERN-2 through CONCERN-5. Harness concerns stayed in [[v5-harness-open-concerns]]. |
| `NOTES.md` (analyses) | [[ozy-analyses]] | Ozy-specific engineering analyses (frozen archive). |
| `COMPLETED_PHASES.md` | [[ozy-completed-phases]] + [[ozy-completed-phases-postmvp]] | Phase narratives (frozen). |
| `docs/operator-guide.md` | [[ozy-operator-guide]] | Operational reference. |
| `docs/agentic-workflow.md` | Not migrated | Harness dev infrastructure, not Ozy trading doc. |
| `docs/BUGS_2026-03-*.md` | Superseded by [[v5-harness-known-bugs]] | Deprecated. |
| `docs/claw-code-analysis.md` | Not migrated | One-time historical analysis. Deprecated. |

## Drift Log Archive Map

| Wiki page | Covers | Era |
|-----------|--------|-----|
| [[ozy-drift-log]] | New entries + format spec + filing rules | Active |
| [[ozy-drift-log-eras-02-10]] | Phases 02-10 + Post-MVP anti-bias | Foundational |
| [[ozy-drift-log-eras-11-14]] | Phases 11-14 + Context Blindness Fix + Post-14 Debug | Execution fidelity |
| [[ozy-drift-log-eras-15-17]] | Phases 15-17 + Post-16 fixes + Ops Hardening + Quant Overrides | Entry conditions & enrichment |
| [[ozy-drift-log-eras-18]] | Post-Phase-17 Bugs + Phase 18 sessions (March 23-27) | Watchlist intelligence |
| [[ozy-drift-log-eras-19-21]] | Phases 19-21 (March 27) | Sonnet/Haiku/Durability |
| [[ozy-drift-log-eras-22-23]] | Phases 22-23 + post-phase + agentic workflow (March 28-April) | Split-call & workflow |

## Quick Reference

| I need to... | Go to... |
|-------------|----------|
| Check how a module deviates from spec | [[ozy-drift-log]] (active) or grep archives below |
| Log a new spec deviation | [[ozy-drift-log]] — add entry in Spec/Impl/Why format |
| Find an open trading concern | [[ozy-open-concerns]] |
| Understand what a completed phase built | [[ozy-completed-phases]] or [[ozy-completed-phases-postmvp]] |
| Look up operational procedures | [[ozy-operator-guide]] |
| Find harness (v5 pipeline) docs | [[v5-harness-architecture]], [[v5-harness-dev-reference]] |
| Understand the original system design | `ozymandias_v3_spec_revised.md` (DRIFT_LOG takes precedence) |

## Searching Archives

`wiki_query` uses keyword + tag matching and works for most lookups (function names, file paths, phase numbers).

For regex pattern searches across frozen archives, use grep directly:
```bash
grep -r 'pattern' .omc/wiki/ozy-drift-log-eras-*.md
grep -r 'pattern' .omc/wiki/ozy-*.md
```

## Non-Wiki Sources

Documentation that remains outside the wiki. Listed here so the routing hub covers the full Ozy documentation landscape.

| Source | Location | Mutable? | Notes |
|--------|----------|----------|-------|
| System spec | `ozymandias_v3_spec_revised.md` | Frozen | Foundational design. DRIFT_LOG takes precedence. |
| Phase files | `phases/` (29 files) | Frozen | Immutable build specs. Never modify. |
| Approved plans | `plans/YYYY-MM-DD-*.md` | Active | Pre-implementation design rationale. |
| Active conventions | `CLAUDE.md` | Active | Session-start rules. Updated each session. |
| Agentic workflow | `docs/agentic-workflow.md` | Active | Dev infrastructure, not trading bot. |
| Operator guide (original) | `docs/operator-guide.md` | Frozen | Migrated to wiki as [[ozy-operator-guide]]. |

---
