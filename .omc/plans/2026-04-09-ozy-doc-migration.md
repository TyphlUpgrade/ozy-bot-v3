# Ozymandias Documentation Migration to Wiki

**Date:** 2026-04-09
**Status:** APPROVED (RALPLAN-DR consensus reached — iteration 2)
**Scope:** Migrate Ozy root docs (DRIFT_LOG.md, NOTES.md, COMPLETED_PHASES.md) and docs/ to OMC wiki with clean harness/Ozy separation

## Requirements Summary

Safely migrate Ozymandias documentation from root-level markdown files to the OMC wiki. Must maintain clean separation between harness (`v5-harness-*`) and Ozymandias (`ozy-*`) wiki pages. Normalize all migrated content to wiki format (YAML frontmatter, category enum, tags, size ceilings, cross-references). Preserve the original files during a transition period, then deprecate.

## Current State

| Source | Size | Entries/Sections | Active? |
|--------|------|-----------------|---------|
| `DRIFT_LOG.md` | 179KB | 3 H2 sections (File Index 97KB, Post-Phase-17 Bugs 2.5KB, Phase 18+ 79KB), 47 H3 subsections (1-7KB each) | Yes — referenced in CLAUDE.md |
| `NOTES.md` | 39KB | 18 sections (10 open, 2 deferred, 1 resolved) | Yes — open concerns active |
| `COMPLETED_PHASES.md` | 25KB | 6 sections | Low churn — historical |
| `docs/agentic-workflow.md` | 23KB | Active reference | Yes — referenced in CLAUDE.md |
| `docs/BUGS_2026-03-*.md` | 36KB | 3 historical files | No — superseded by wiki bugs page |
| `docs/claw-code-analysis.md` | 36KB | Analysis doc | No — historical |
| `docs/operator-guide.md` | 7.5KB | Ops reference | Low churn |

**Wiki:** 22 pages, 110KB total. All harness-focused. Category ceilings: `decision` 8KB, `debugging` 8KB, `architecture` 10KB, `reference` 12KB.

## RALPLAN-DR Summary

### Principles
1. **Clean namespace separation** — Ozy pages use `ozy-*` prefix, harness pages use `v5-harness-*`. Never mix domains in one page.
2. **Archive bulk, surface active** — Historical entries go in `reference`-category archive pages (12KB ceiling). Only active/recent content lives in primary pages.
3. **Format normalization** — Every migrated page gets YAML frontmatter, valid category, tags, cross-references. No exceptions.
4. **Gradual deprecation** — Original files stay during transition. CLAUDE.md references updated only after wiki equivalents are verified. No big-bang cutover.
5. **Navigation parity** — Wiki routing (wiki-guide decision tree + index) must make any Ozy doc findable as fast as the current grep-based workflow.

### Decision Drivers
1. **DRIFT_LOG.md is 179KB** — 3 H2 sections containing 47 H3 subsections. The `## File Index` H2 (97KB) holds all phases 2-17 as H3 subsections (1-7KB each). `## Phase 18` H2 (79KB) holds 22 date-based session H3s. No single subsection exceeds 7.2KB, but the H2 sections are far too large for wiki pages.
2. **Mixed concerns in NOTES.md** — Contains both Ozy trading concerns (CONCERN-2/3/4/5) and harness/workflow concerns (CONCERN-6/7/8/9, PERF-1). Must be separated during migration.
3. **CLAUDE.md reference chain** — Multiple CLAUDE.md sections reference root docs. Migration must update these references atomically with content migration.
4. **Dual-source window** — During migration, content exists in both original files and wiki. Without a hard cutoff date, both diverge indefinitely.

### Viable Options

**Option A: Era-based archive pages with ceiling overrides + routing index (chosen)**
- Split DRIFT_LOG by era into 6 frozen archive pages (ceiling overrides, no 12KB limit for immutable content)
- Split NOTES.md by domain (Ozy concerns vs analyses vs resolved)
- Split COMPLETED_PHASES.md into 1-2 pages
- Create `ozy-doc-index.md` routing page that replaces DRIFT_LOG's File Index section
- Create `ozy-drift-log.md` active page for new entries (strict 8KB ceiling)
- Pros: Full wiki integration, searchable via `wiki_query`, frozen archives preserve cross-entry context within eras, manageable page count (~13)
- Cons: Frozen archives exceed normal ceilings (20-45KB), wiki_query keyword matching less powerful than grep for regex

**Option B: Thin wiki indexes + original files stay authoritative**
- Create wiki index pages that point into the original root files (by section anchor)
- Don't migrate content — just add navigation layer
- Pros: Minimal effort, no content duplication risk, originals unchanged
- Cons: Two systems permanently, no format normalization, DRIFT_LOG keeps growing unbounded, wiki_query can't search original files
- **Invalidation rationale**: Violates principles 2 and 3 (no archiving, no normalization). The original files will keep growing unchecked. This defers the problem rather than solving it.

**Option C: Selective migration (active content only)**
- Migrate only NOTES.md (open concerns) and docs/operator-guide.md
- Keep DRIFT_LOG.md and COMPLETED_PHASES.md as-is (historical, low churn)
- Pros: Smallest scope, lowest risk, addresses the most painful gap (scattered concerns)
- Cons: DRIFT_LOG.md stays at 179KB and growing, no navigation improvement, incomplete migration
- **Invalidation rationale**: DRIFT_LOG is the biggest documentation pain point (179KB, no wiki search). Skipping it makes the migration mostly cosmetic.

## Acceptance Criteria

1. All Ozy wiki pages use `ozy-*` prefix — zero `v5-harness-*` pages contain Ozy trading content
2. DRIFT_LOG.md content fully represented in wiki (active page + 6 era-based frozen archive pages)
3. NOTES.md Ozy concerns in `ozy-open-concerns.md`; harness concerns remain in `v5-harness-open-concerns.md`
4. COMPLETED_PHASES.md content in wiki phase narrative pages
5. `ozy-doc-index.md` routing page maps files → wiki pages (replaces DRIFT_LOG File Index)
6. `wiki-guide.md` decision tree updated with Ozy page routing
7. `index.md` lists all new Ozy pages in a dedicated Ozymandias section
8. Every new page has valid frontmatter (title, tags, category, dates) and ≥2 tags. Active pages within category ceilings; frozen archives have `ceiling_override: frozen-archive`
9. CLAUDE.md "Reference Documents" section updated to point to wiki equivalents
10. Original files NOT deleted — marked with deprecation header pointing to wiki
11. All `[[page-name]]` links resolve
12. `wiki-log.md` updated with migration chronicle

## Implementation Steps

### Step 1: Create Ozy routing index

**File:** `.omc/wiki/ozy-doc-index.md`
**Category:** `architecture` (10KB ceiling)
**Tags:** `[ozymandias, navigation, index, trading-bot]`

Purpose: Replace DRIFT_LOG's File Index with a wiki-native routing page. Maps source files to the wiki pages that contain relevant drift entries, concerns, and phase narratives.

Contents:
- File → Wiki Page routing table (from DRIFT_LOG File Index, expanded)
- Quick reference: "I need X → go to Y" decision tree for Ozy docs
- Cross-references to all `ozy-*` pages

### Step 2: Migrate DRIFT_LOG.md — era-based archives with ceiling overrides

**Actual DRIFT_LOG structure** (verified):
- `## File Index` H2 (97KB): 25 H3 subsections covering Phases 02-17 + post-phase fixes (each 1-7KB)
- `## Post-Phase-17 Bug Fixes` H2 (2.5KB): standalone section
- `## Phase 18` H2 (79KB): 22 H3 subsections covering dated sessions through Phase 23 + agentic workflow (each 1-7KB)

**Ceiling override policy:** Frozen archive pages use `ceiling_override: frozen-archive` in YAML frontmatter. No hard byte limit — they are immutable historical content, only read on specific queries. Active pages use strict category ceilings.

Create 6 frozen archive pages + 1 active page:

| Wiki page | Content | Source bytes | Category |
|-----------|---------|-------------|----------|
| `ozy-drift-log.md` | Format spec, filing heuristic, latest entries, links to archives | ~3KB | `decision` (8KB) |
| `ozy-drift-log-eras-02-10.md` | Phases 02-10 + Post-MVP anti-bias (11 H3s incl. Entry format) | ~24KB | `reference` (frozen) |
| `ozy-drift-log-eras-11-14.md` | Phases 11-14 + Context Blindness Fix + Post-14 Debug (6 H3s) | ~29KB | `reference` (frozen) |
| `ozy-drift-log-eras-15-17.md` | Phases 15-17 + Post-16 + Ops Hardening + Quant Overrides (9 H3s) | ~44KB | `reference` (frozen) |
| `ozy-drift-log-eras-18.md` | Phase 18 sessions + Post-Phase-17 Bugs (5 H3s + 1 H2 block, March 23-27) | ~18KB | `reference` (frozen) |
| `ozy-drift-log-eras-19-21.md` | Phases 19-21 (3 H3s, March 27) | ~19KB | `reference` (frozen) |
| `ozy-drift-log-eras-22-23.md` | Phases 22-23 + post-phase + agentic workflow (14 H3s, March 28 - April) | ~40KB | `reference` (frozen) |

**Total drift log pages: 7** (1 active + 6 frozen archives).

**Frozen archive frontmatter:**
```yaml
---
title: "Ozy Drift Log — Eras 02-10"
tags: [ozymandias, drift-log, archive, phases-02-10]
category: reference
ceiling_override: frozen-archive
created: 2026-04-09
updated: 2026-04-09
frozen: true
---
```

**Active page (`ozy-drift-log.md`):**
- Contains format spec (Spec/Impl/Why triple)
- Filing heuristic (same as harness drift log: "only when it would surprise a developer")
- Latest entries (post-migration new entries go here only)
- Links to all 6 archive pages
- **This is the landing page** — `wiki_query("drift log ozymandias")` hits this first

**Search parity note:** `wiki_query` uses keyword + tag matching, not regex. For archive content, this means searching for a specific function name or file path will find matching pages. For complex regex patterns (e.g., `broker.*alpaca.*fill`), users should fall back to `grep .omc/wiki/ozy-drift-log-*.md`. This is documented in `ozy-doc-index.md`.

### Step 3: Migrate NOTES.md — split by domain

| Wiki page | Content | Category |
|-----------|---------|----------|
| `ozy-open-concerns.md` | CONCERN-2, 3, 4, 5 (trading-specific) + any new Ozy concerns | `debugging` (8KB) |
| `ozy-analyses.md` | Trade journal audit, session log analysis, orchestrator analysis, and other Ozy-specific analyses (~13.5KB) | `reference` (frozen, `ceiling_override: frozen-archive`) |

**Note on analyses ceiling:** The 7 Ozy-specific analyses total ~13.5KB, exceeding both `decision` (8KB) and `reference` (12KB) ceilings. Since all analyses are completed historical work (not actively updated), the page uses `ceiling_override: frozen-archive` — the same policy applied to drift log archives. The "Agentic Development Workflow Design" analysis (7.3KB) is harness infrastructure, not Ozy trading — exclude it from `ozy-analyses.md` (it stays in NOTES.md or migrates to a harness page separately).

**Already handled:**
- CONCERN-6/7/8/9 (harness/workflow) → already captured in `v5-harness-open-concerns.md` or superseded by v5 harness architecture. **Renumber** if migrating to avoid collision with existing CONCERN-1/2/3 in `v5-harness-open-concerns.md`.
- PERF-1 → already in `v5-harness-open-concerns.md`
- DEFERRED-1 (DRIFT_LOG File Index) → already resolved (File Index exists at 97KB); not resolved by this migration

**Resolved/won't-fix entries:** Drop during migration (no value in wiki). Record the resolution in `ozy-open-concerns.md` header if relevant.

### Step 4: Migrate COMPLETED_PHASES.md

| Wiki page | Content | Category |
|-----------|---------|----------|
| `ozy-completed-phases.md` | Phases 11-18 + Paper Session Fixes (~15KB) | `reference` (frozen, `ceiling_override: frozen-archive`) |
| `ozy-completed-phases-postmvp.md` | Orchestrator extraction, agentic workflow (~9KB) | `reference` (12KB — fits without override) |

25KB total → 2 pages. Split after "Paper Session Fixes post-18" (section 2 of 6). Page 1 (~15KB) uses `ceiling_override: frozen-archive` since all content is immutable phase history. Page 2 (~9KB) fits within the standard 12KB `reference` ceiling.

### Step 5: Migrate active docs/ files

| Source | Wiki page | Category | Notes |
|--------|-----------|----------|-------|
| `docs/operator-guide.md` | `ozy-operator-guide.md` | `pattern` (10KB) | 7.5KB — fits in one page |
| `docs/agentic-workflow.md` | Already referenced in CLAUDE.md as harness dev infra | Leave as-is | Not Ozy trading doc — it's harness infrastructure |

**Not migrated (historical artifacts):**
- `docs/BUGS_2026-03-*.md` — superseded by wiki bugs page. Add deprecation header.
- `docs/claw-code-analysis.md` — one-time analysis. Add deprecation header.
- `docs/Multilayer agentic workflow spec.pdf` — binary, can't wiki-ify. Reference from `ozy-doc-index.md`.

### Step 6: Update wiki-guide.md routing

Add Ozy routing entries to decision tree:

| I have... | Put it in... | Example |
|-----------|--------------|---------|
| Trading bot spec deviation | [[ozy-drift-log]] | Spec says X, impl does Y |
| Trading bot engineering concern | [[ozy-open-concerns]] | Entry conditions bypass, prompt inefficiency |
| Completed Ozy phase narrative | [[ozy-completed-phases]] | Phase 18 summary |

Update disambiguation rule to include Ozy drift log:
- `DRIFT_LOG.md` (project root) → **deprecated**, see [[ozy-drift-log]] and archives
- `ozy-drift-log.md` (wiki) → Ozy spec deviations (Spec/Impl/Why)
- `v5-harness-drift-log.md` (wiki) → harness plan deviations (Plan/Impl/Why)

### Step 7: Update index.md

Add Ozymandias section:
```
## Ozymandias (Trading Bot)
- [Ozy Documentation Index](ozy-doc-index.md) — File→page routing, navigation
- [Ozy Drift Log](ozy-drift-log.md) — Active spec deviations (Spec/Impl/Why)
- [Ozy Open Concerns](ozy-open-concerns.md) — Trading-specific engineering concerns
- [Ozy Completed Phases](ozy-completed-phases.md) — Phase 11-18 narratives
- [Ozy Operator Guide](ozy-operator-guide.md) — Operational reference
```

### Step 8: Update CLAUDE.md references

Update "Reference Documents" section:
- `DRIFT_LOG.md` → add note: "Being migrated to wiki. See [[ozy-drift-log]] for latest entries."
- `NOTES.md` → add note: "Ozy concerns migrated to [[ozy-open-concerns]]. Harness concerns in [[v5-harness-open-concerns]]."
- `COMPLETED_PHASES.md` → add note: "Migrated to [[ozy-completed-phases]] and [[ozy-completed-phases-postmvp]]."

**Do NOT delete the original CLAUDE.md descriptions** — the originals stay as canonical references until full migration is verified.

### Step 9: Deprecation headers on original files

Add to top of DRIFT_LOG.md, NOTES.md, COMPLETED_PHASES.md:
```
> **⚠️ Migration in progress:** This file's content is being migrated to the OMC wiki.
> See `.omc/wiki/ozy-doc-index.md` for the wiki routing table.
> **Cutoff: 2026-04-14.** After this date, new entries go in wiki pages only. This file is frozen.
```

### Step 10: Verification pass

1. All new pages within category ceilings (`wc -c`)
2. All `[[page-name]]` links resolve
3. `wiki_query("drift log")` returns both `ozy-drift-log.md` and `v5-harness-drift-log.md`
4. No Ozy trading content in `v5-harness-*` pages
5. No harness content in `ozy-*` pages
6. CLAUDE.md references updated
7. wiki-guide routing table includes Ozy entries
8. index.md has Ozymandias section
9. wiki-log.md updated

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Frozen archive pages exceed normal ceilings (18-44KB) | Higher token cost when read into AI context | `ceiling_override: frozen-archive` frontmatter signals intent; frozen archives are only opened on specific keyword queries (not browsed casually); grep fallback avoids needing to read full pages into context |
| DRIFT_LOG splitting loses cross-entry context | Developer can't see related entries across phases | Era-based grouping preserves context within logical eras; ozy-doc-index.md routing table + cross-references between archives |
| Migration introduces content drift from originals | Two sources disagree | Hard cutoff date (2026-04-14): no new entries in originals after cutoff. Deprecation headers added immediately. |
| NOTES.md Ozy/harness split misclassifies a concern | Concern lands in wrong page | Classification rule: if it mentions trading, Ozy ticker, broker, or market data → Ozy. If it mentions orchestrator, sessions, pipeline, signals → harness. |
| CLAUDE.md reference update breaks agent workflow | Agents can't find docs | Gradual update: add wiki pointers alongside existing references, don't remove originals until verified |
| wiki_query keyword matching weaker than grep for regex | Complex pattern searches miss results | Document grep fallback in ozy-doc-index.md: `grep -r pattern .omc/wiki/ozy-drift-log-*.md` for regex needs |

## Verification Steps

1. `find .omc/wiki/ozy-*.md | wc -l` — shows 13 pages
2. `wc -c .omc/wiki/ozy-drift-log.md` — active page under 8KB (8192 bytes)
3. `wc -c .omc/wiki/ozy-open-concerns.md .omc/wiki/ozy-analyses.md` — under category ceilings
4. `grep 'ceiling_override: frozen-archive' .omc/wiki/ozy-drift-log-eras-*.md | wc -l` — shows 6 (all frozen archives marked)
5. `grep -oP '\[\[([^\]]+)\]\]' .omc/wiki/ozy-*.md` — all links resolve to existing files
6. `grep "ozy-" .omc/wiki/index.md` — Ozymandias section present
7. `grep "ozy-" .omc/wiki/wiki-guide.md` — routing entries present
8. `head -5 DRIFT_LOG.md NOTES.md COMPLETED_PHASES.md` — deprecation headers with cutoff date present
9. `grep "wiki" CLAUDE.md` — wiki migration pointers present
10. No `v5-harness-*` page contains trading-specific terms: `grep -l "broker\|alpaca\|ticker\|position_size" .omc/wiki/v5-harness-*.md` returns empty
11. `grep -r "grep.*ozy-drift-log" .omc/wiki/ozy-doc-index.md` — grep fallback documented

## Estimated Page Count

| Category | Pages | Notes |
|----------|-------|-------|
| Ozy routing index | 1 | `ozy-doc-index.md` |
| Ozy drift log (active) | 1 | `ozy-drift-log.md` (8KB ceiling) |
| Ozy drift log (frozen archives) | 6 | Era-based, ceiling override (18-44KB each) |
| Ozy open concerns | 1 | `ozy-open-concerns.md` |
| Ozy analyses | 1 | `ozy-analyses.md` |
| Ozy completed phases | 2 | Split by era |
| Ozy operator guide | 1 | `ozy-operator-guide.md` |
| **Total new pages** | **13** | Wiki grows from 22 → 35 pages |

## ADR: Ozymandias Documentation Migration

**Decision:** Migrate all Ozy root docs to wiki using era-based frozen archive pages (with ceiling overrides) and a routing index, maintaining clean `ozy-*` / `v5-harness-*` namespace separation.

**Drivers:**
1. DRIFT_LOG.md at 179KB is unsearchable and growing unbounded
2. NOTES.md mixes Ozy and harness concerns with no separation
3. Wiki provides search, size ceilings, staleness tracking, and cross-references that root files lack

**Alternatives considered:**
- *Option B (Thin indexes only)*: Rejected — defers the problem, no normalization, wiki_query can't search originals
- *Option C (Active content only)*: Rejected — skips DRIFT_LOG which is the biggest pain point

**Why chosen:** Option A (with ceiling overrides) is the only approach that fully normalizes Ozy docs into the wiki system, enables wiki_query search across all project documentation, and prevents unbounded growth via active-page ceilings + frozen-archive markers. The ceiling override synthesis avoids the fragmentation problem of the original Option A (which would have needed 18+ drift log pages at 12KB each) while preserving era-level cross-entry context.

**Consequences:**
- Wiki grows from 22 to ~35 pages (13 new Ozy pages)
- 6 frozen archive pages for DRIFT_LOG use ceiling overrides (18-44KB each, not subject to 12KB limit)
- Original files stay during transition with deprecation headers; hard cutoff 2026-04-14
- CLAUDE.md needs careful reference updates
- wiki_query keyword matching covers most lookups; grep fallback documented for regex patterns

**Follow-ups:**
- Monitor wiki_query performance with 60% more pages
- After cutoff date (2026-04-14), freeze originals (make read-only or move to `docs/deprecated/`)
- Consider pruning obviously obsolete entries from frozen archives in a future pass (not a blocker)
