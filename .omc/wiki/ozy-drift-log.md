---
title: Ozymandias Drift Log
tags: [ozymandias, drift-log, spec-deviation, trading-bot]
category: decision
created: 2026-04-09
updated: 2026-04-09
---

# Ozymandias Drift Log

Deviations from `ozymandias_v3_spec_revised.md` introduced during implementation. This file takes precedence over the spec on any listed item.

For the full routing map of source files to archive pages, see [[ozy-doc-index]].

---

## Entry Format

```
**`identifier`** . spec SX.Y . `path/to/file.py`
- **Spec:** what the spec says, or *(not defined)* for pure additions
- **Impl:** what was actually implemented
- **Why:** reason for the deviation
```

## Filing Heuristic

Add an entry here when a change **would surprise a developer** reading only the spec. If the code and commit message together explain everything, skip the entry. Entries go in this active page; frozen archives are not edited.

**Scope:** Ozymandias trading bot spec deviations only. For v5 harness plan deviations, see [[v5-harness-drift-log]].

**8KB ceiling** (decision category). When this page approaches the ceiling, archive the oldest entries to a new frozen archive page.

---

## Active Entries

*New entries go here. Entries are migrated to frozen archives when this page approaches the 8KB ceiling.*

*(No new entries yet — migration just completed. All historical entries are in the archives below.)*

---

## Archives

| Archive | Covers |
|---------|--------|
| [[ozy-drift-log-eras-02-10]] | Phases 02-10 + Post-MVP anti-bias (foundational) |
| [[ozy-drift-log-eras-11-14]] | Phases 11-14 + Context Blindness + Post-14 Debug (execution fidelity) |
| [[ozy-drift-log-eras-15-17]] | Phases 15-17 + Post-16 + Ops Hardening + Quant Overrides (entry conditions) |
| [[ozy-drift-log-eras-18]] | Post-Phase-17 Bugs + Phase 18 sessions March 23-27 (watchlist intelligence) |
| [[ozy-drift-log-eras-19-21]] | Phases 19-21 March 27 (Sonnet/Haiku/Durability) |
| [[ozy-drift-log-eras-22-23]] | Phases 22-23 + post-phase + agentic workflow March 28-April (split-call & workflow) |

---

## Cross-References

- [[ozy-doc-index]] — Full file-to-page routing table
- [[ozy-open-concerns]] — Open engineering concerns
- [[v5-harness-drift-log]] — Harness plan deviations (separate namespace)
