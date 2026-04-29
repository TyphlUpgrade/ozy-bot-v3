# Wiki Bridge Pass: AI-Optimized Structure + Ozy Feature Parity

**Date:** 2026-04-09
**Status:** APPROVED (RALPLAN-DR consensus — iteration 1)
**Scope:** Structural wiki improvements — no content migration from Ozy docs yet

## Requirements Summary

Bridge the gap between the Ozy documentation system (DRIFT_LOG, NOTES.md, COMPLETED_PHASES.md)
and the OMC wiki so the wiki is ready for future Ozy doc migration. Simultaneously optimize
existing pages for AI context efficiency.

## Acceptance Criteria

1. `v5-harness-dev-reference.md` is under 5KB (currently 9.2KB) — keeps PipelineState + SignalReader contract tables
2. `v5-harness-drift-log.md` exists with Plan/Impl/Why format, keyed by v5 plan sections; all entries verified against live code
3. `v5-harness-open-concerns.md` exists with status labels, severity, first-observed dates
4. `wiki-guide.md` routing table includes both new page types + disambiguation rule for Ozy vs harness drift logs
5. `wiki-guide.md` is under 9.5KB (currently 10.2KB — slimming partially offset by new routing entries)
6. PERF-1 moved from known-bugs to open-concerns (it's not a bug)
7. `index.md` updated with new pages
8. All `[[page-name]]` links verified via `grep -oP '\[\[([^\]]+)\]\]' .omc/wiki/*.md | sort -u` against existing files
9. `known-bugs.md` count updated after PERF-1 removal
10. `wiki-log.md` updated with operations chronicle entry

## RALPLAN-DR Summary

### Principles
1. **AI context efficiency** — Every wiki byte must justify its context-window cost. Cut content reconstructible from code; keep cross-cutting contracts.
2. **Feature parity before migration** — Bridge Ozy documentation features (drift tracking, open concerns) into the wiki structure now, so future Ozy→wiki migration has a landing zone.
3. **Non-overlapping scopes** — Two drift log systems coexist temporarily. Each must have a clear, non-overlapping domain (Ozy spec vs harness plan).
4. **Staleness prevention** — New pages must have routing rules and freshness triggers, not just initial content.

### Decision Drivers
1. **Context window budget** — dev-reference.md at 9.2KB consumes disproportionate context for partially stale content
2. **Documentation gap** — Harness plan deviations and engineering concerns have no wiki home, creating knowledge loss risk
3. **Migration readiness** — Phase 4+ Ozy doc migration needs wiki-side containers already in place

### Viable Options

**Option A: Bridge pass (chosen)** — Create harness drift log + open concerns pages, slim existing pages, update routing rules
- Pros: Fills documentation gap now, reduces context cost, sets up migration landing zones
- Cons: Adds 2 new pages to maintain, temporary coexistence of two drift log systems

**Option B: Full Ozy migration now** — Move all Ozy docs (DRIFT_LOG, NOTES, COMPLETED_PHASES) into wiki immediately
- Pros: Single source of truth, no coexistence confusion
- Cons: Massive scope (all Ozy docs + cross-references), blocks Phase 3 feature work, premature — wiki structure not yet proven at scale
- **Invalidation rationale**: Phase 4 explicitly scopes this work. Doing it now delays harness feature development and risks migrating into an unproven wiki structure.

**Option C: Defer entirely** — Keep Ozy docs as-is, only slim existing wiki pages
- Pros: Minimal effort, no new maintenance burden
- Cons: Documentation gap persists, Phase 4 migration has no landing zone, engineering concerns continue to be undocumented
- **Invalidation rationale**: The gap is causing active knowledge loss (drift entries not tracked, concerns not filed). Deferring compounds the problem.

## Implementation Steps

### Step 1: Slim dev-reference.md (9.2KB → ~4.5KB)

**What to keep** (AI-valuable content not reconstructible from a single file):
- Extension Points section (lines 17-45) — "where to plug in" checklists
- PipelineState field table (lines 50-71) — cross-cutting "Set by / Cleared by" contracts
  assembled from 12+ call sites across orchestrator.py. Not in any docstring.
- SignalReader methods table (lines 86-98) — maps methods to signal directories, routing
  info spread across 5+ methods in signals.py
- ProjectConfig TOML keys table (lines 73-84) — compact, high signal
- Cross-References section

**What to cut:**
- SessionManager methods table (lines 100-112) — genuinely restates the code's docstrings
- Code Patterns section (lines 117-129) — all 5 patterns already in CLAUDE.md or code comments
- Diagnostic Flows section (lines 133-155) — cross-module debugging paths, but goes stale.
  Move key diagnostic pointers to inline comments in orchestrator.py instead.
- Open Bug Summary section (lines 159-173) — duplicates known-bugs page

**Resulting structure:**
```
# v5 Harness Developer Reference
## Extension Points (4 checklists, ~40 lines)
## Key Interfaces (PipelineState fields + SignalReader methods, ~45 lines)
## Config Quick Reference (TOML table, ~15 lines)
## Cross-References (~5 lines)
```

### Step 2: Create harness drift log

**File:** `.omc/wiki/v5-harness-drift-log.md`
**Category:** `decision`
**Tags:** `[harness, drift, deviations, v5-plan]`

**Format** (matches Ozy DRIFT_LOG Spec/Impl/Why triple):
```
### `identifier` · v5 plan §section · `file.py`
- **Plan:** what the v5 architecture plan specified
- **Impl:** what was actually implemented
- **Why:** reason for the deviation
```

**Seed entries** (known deviations from `plans/2026-04-08-v5-harness-architecture.md`):

1. **Orchestrator is pure Python, not clawhip-integrated** — Plan §Architecture (lines 29-34)
   shows clawhip launching sessions and watching signals. Impl: Python orchestrator does both.
   Clawhip only does tmux launch + Discord routing. Why: simpler to keep signal polling in
   Python where state management lives.

2. **Bot sessions not implemented** — Plan §Session Registry (lines 47-52) shows ops_monitor,
   dialogue, analyst sessions. Impl: only dev sessions (architect, executor, reviewer) exist.
   Why: Phase 5 scope (bot pipeline integration).

3. **Stage timeout clears entire task, not just session** — Plan §Stage Pipeline (line 291)
   implies timeout kills the session and retries. Impl: `handle_stage_timeout` calls `kill()`
   on the session but also clears the active task via `clear_active()`. Why: a timed-out stage
   indicates the task itself is stuck, not just the session — restarting without clearing
   would re-enter the same stuck state.

4. **shelved_tasks is a list[dict], not a dataclass** — Plan §Pipeline-frozen mitigation
   (lines 1254-1305) implies structured task queue. Impl: LIFO list of plain dicts on
   PipelineState. Why: save/load simplicity — dicts serialize to JSON directly without
   custom encoder.

**Filing heuristic:** Only file a drift entry when the deviation would surprise a future
developer reading the v5 plan — not for every minor implementation choice. The drift log
has an **8KB size ceiling** (matching the `decision` category cap).

### Step 3: Create open concerns page

**File:** `.omc/wiki/v5-harness-open-concerns.md`
**Category:** `debugging`
**Tags:** `[harness, concerns, open-issues, engineering]`

**Format** (matches Ozy NOTES.md structure):
```
### CONCERN-N: title
**Status:** open | deferred | resolved | won't-fix
**Severity:** Low | Medium | High
**First observed:** YYYY-MM-DD
**Area:** file or module name
Description of the concern, analysis, and any proposed mitigation.
```

**Initial entries:**

1. **PERF-1: parse_token_usage O(n) re-read** (moved from known-bugs)
   - Status: deferred | Severity: Low | Area: sessions.py
   - Re-reads entire stream-json log every poll cycle

2. **CONCERN-1: shelved_tasks dict shape not validated**
   - Status: open | Severity: Low | Area: pipeline.py
   - `unshelve()` trusts dict keys exist. If a shelved dict is corrupted (e.g., manual
     edit of state.json), KeyError crash. Mitigation: add `.get()` with defaults.

3. **CONCERN-2: Signal file cleanup on task completion**
   - Status: open | Severity: Low | Area: orchestrator.py
   - `archive()` moves signals on completion, but if the process crashes between
     `clear_active()` and `archive()`, orphan signal files accumulate in signal dirs.

4. **CONCERN-3: Escalation cache not bounded**
   - Status: open | Severity: Low | Area: orchestrator.py
   - `_escalation_cache` grows by one entry per escalated task. Popped on `clear_active()`
     but if tasks are abandoned without clearing, cache grows unbounded. Same class as
     BUG-001 (_processed set).

### Step 4: Update wiki-guide.md

**4a. Add routing entries to decision tree:**

| I have... | Put it in... | Example |
|-----------|--------------|---------|
| Implementation that differs from v5 plan | [[v5-harness-drift-log]] | Plan said clawhip launches sessions, impl uses Python orchestrator |
| Open engineering concern (not a bug) | [[v5-harness-open-concerns]] | Performance issue, unbounded cache, unvalidated shape |

**Disambiguation rule — two drift logs:**
- `DRIFT_LOG.md` (project root) → **trading bot** spec deviations (Ozy spec vs implementation)
- `v5-harness-drift-log.md` (wiki) → **v5 harness** plan deviations (architecture plan vs implementation)
- When Ozy docs migrate to the wiki in Phase 4+, a separate `ozy-drift-log.md` wiki page will absorb `DRIFT_LOG.md`. Until then, the two systems coexist with non-overlapping scopes.

**4b. Add to "Relationship to Other Docs" table:**

| Document | Purpose | When to Write |
|----------|---------|---------------|
| `.omc/wiki/v5-harness-drift-log.md` | V5 plan deviations (Plan/Impl/Why) | When harness implementation differs from `plans/2026-04-08-v5-harness-architecture.md` |
| `.omc/wiki/v5-harness-open-concerns.md` | Engineering concerns (not bugs) | Performance issues, unvalidated assumptions, unbounded growth, deferred analyses |

**4c. Slim wiki-guide for AI:**
- Cut the "Example of wrong way / right way" block (lines 165-175) — the rule on line 163
  is sufficient. Saves ~12 lines.
- Compress the Quick Rules section — currently 10 numbered items with verbose descriptions.
  Reduce each to one line. Saves ~10 lines.
- Cut the "lint alignment" rule (line 196) — implementation detail, not contributor guidance.

### Step 5: Move PERF-1 from known-bugs to open-concerns

- Remove PERF-1 entry from `v5-harness-known-bugs.md`
- Verify whether "8 open bugs" count changes (PERF-1 sits in the Phase 3 section, not under "Open (Deferred)" — count may stay at 8)
- Update index.md count only if bug count changed
- PERF-1 already added to open-concerns in Step 3

### Step 6: Update index.md

Add under appropriate sections:
```
## Quality
- [v5 Harness Known Bugs](v5-harness-known-bugs.md) — N open bugs (M resolved)
- [v5 Harness Open Concerns](v5-harness-open-concerns.md) — Engineering concerns, perf issues
- [v5 Harness Drift Log](v5-harness-drift-log.md) — V5 plan deviations (Plan/Impl/Why)
- [v5 Harness Reviewer Findings](v5-harness-reviewer-findings.md) — 4 review rounds
```

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Cutting too much from dev-reference loses useful info | AI agents miss extension points | Keep all 4 extension checklists intact; only cut what's in code/CLAUDE.md |
| Drift log goes stale (entries not added as code diverges) | False trust in plan accuracy | Add "check drift log" to wiki-guide as step in post-implementation review |
| Open concerns page duplicates known-bugs | Confusion about where to file | Clear rule: bugs have repro steps, concerns have analysis. Wiki-guide routing table disambiguates. |
| wiki-guide slimming removes useful context for human readers | Humans struggle with terse rules | Keep the decision tree table (high value per byte); cut only examples and verbose descriptions |
| Parallel drift log systems cause filing confusion | Entries land in the wrong log | Disambiguation rule in wiki-guide routing table (Ozy spec → DRIFT_LOG.md, harness plan → wiki drift log). Non-overlapping scopes until Phase 4+ migration. |

## Verification Steps

1. `wc -c` on dev-reference.md < 5500 bytes
2. `wc -c` on wiki-guide.md < 9500 bytes
3. All `[[page-name]]` links resolve to existing files: `grep -oP '\[\[([^\]]+)\]\]' .omc/wiki/*.md | sort -u` vs `ls .omc/wiki/*.md`
4. Drift log has at least 3 seed entries with Plan/Impl/Why format
5. Open concerns has PERF-1 + at least 2 new concerns
6. known-bugs no longer contains PERF-1
7. index.md lists both new pages
8. wiki-guide decision tree has entries for both new page types
9. wiki-guide disambiguation rule distinguishes DRIFT_LOG.md (Ozy spec) from v5-harness-drift-log.md (harness plan)
10. wiki-log.md updated with operations chronicle entry

## ADR: Wiki Bridge Pass

**Decision:** Create harness-specific drift log and open concerns pages in the OMC wiki, slim dev-reference.md for AI context efficiency, and add routing/disambiguation rules to wiki-guide.md.

**Drivers:**
1. Context window budget — dev-reference.md at 9.2KB is disproportionate for partially stale content
2. Documentation gap — harness plan deviations and engineering concerns have no wiki home
3. Migration readiness — Phase 4+ Ozy doc migration needs wiki-side containers already in place

**Alternatives considered:**
- *Option B (Full Ozy migration now)*: Move all Ozy docs into wiki immediately. Rejected: massive scope, blocks Phase 3, premature before wiki structure is proven at scale.
- *Option C (Defer entirely)*: Only slim existing pages. Rejected: documentation gap compounds, Phase 4 has no landing zone, engineering concerns remain undocumented.

**Why chosen:** Option A fills the active documentation gap with bounded scope (2 new pages + 2 page edits), creates landing zones for Phase 4 migration, and reduces AI context cost. The 8KB ceiling and filing heuristic address the staleness risk identified by the Architect.

**Consequences:**
- Two drift log systems coexist temporarily (DRIFT_LOG.md for Ozy spec, wiki drift log for harness plan) — disambiguation rule mitigates confusion
- Net wiki page count increases by 2 — bounded by size ceilings
- dev-reference.md loses SessionManager table, Code Patterns, Diagnostic Flows, and Open Bug Summary sections

**Follow-ups:**
- Phase 4: Ozy doc migration absorbs DRIFT_LOG.md into a separate `ozy-drift-log.md` wiki page, eliminating the parallel system
- Monitor drift log usage in Phase 3 — if entries stale, reassess filing heuristic

## Consensus Changelog

1. Removed fabricated seed entry #2 (EventLog module — does not exist in v5 plan). Renumbered remaining entries.
2. Added plan line references to all drift log seed entries for traceability.
3. Added 8KB size ceiling and filing heuristic to drift log (Architect synthesis).
4. Fixed Step 4a routing example to reference clawhip deviation instead of removed EventLog entry.
5. Clarified Step 5: PERF-1 may not change the "8 open bugs" count since it sits outside the Open section (Critic finding).
