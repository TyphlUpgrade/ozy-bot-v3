---
title: Wiki Contribution Guide
tags: [wiki, meta, guide]
category: pattern
created: 2026-04-09
updated: 2026-04-09
---

# Wiki Contribution Guide

## Decision Tree: Where Does Your Information Go?

| I have... | Put it in... | Example |
|-----------|--------------|---------|
| How a module/system works | [[v5-harness-architecture]] | Signal reader design, 7-module flow, FIFO queue |
| Design rationale or trade-off | [[v5-harness-design-decisions]] | Why O_NONBLOCK, why caveman config |
| Known bug with repro + severity | [[v5-harness-known-bugs]] | Race condition with ID, impact level |
| Implementation that differs from v5 plan | [[v5-harness-drift-log]] | Plan said clawhip launches sessions, impl uses Python orchestrator |
| Open engineering concern (not a bug) | [[v5-harness-open-concerns]] | Performance issue, unbounded cache, unvalidated shape |
| Code review finding or audit issue | [[v5-harness-reviewer-findings]] | Refactoring opportunity, safety concern |
| Feature proposal or integration plan | [[v5-omc-agent-integration]], [[v5-conversational-discord-operator]] | New capability, architecture change |
| Phase completion checklist | [[v5-phase3-readiness]] | Sign-off criteria, due diligence |
| Per-session discovery or experiment | Session Logs (auto-named) | Dead-end investigation, prototype notes |
| Trading bot spec deviation | [[ozy-drift-log]] | Spec says X, impl does Y |
| Trading bot engineering concern | [[ozy-open-concerns]] | Entry conditions bypass, prompt inefficiency |
| Completed Ozy phase narrative | [[ozy-completed-phases]] | Phase 18 summary |
| Ozy doc navigation | [[ozy-doc-index]] | Where is X documented? |

**Disambiguation — two drift logs coexist:**
- `DRIFT_LOG.md` (project root) → **deprecated**, see [[ozy-drift-log]] and frozen archives
- [[ozy-drift-log]] → **trading bot** spec deviations (Spec/Impl/Why)
- [[v5-harness-drift-log]] → **v5 harness** plan deviations (Plan/Impl/Why)
- Bugs with repro steps → [[v5-harness-known-bugs]], not the drift log
- Engineering concerns without repro → [[v5-harness-open-concerns]] (harness) or [[ozy-open-concerns]] (trading bot)

## Directory Structure

All wiki pages live in the wiki root — no subdirectories (pages in subfolders are invisible to wiki tooling).

```text
.omc/wiki/
├── index.md                               # Catalog — auto-maintained
├── wiki-guide.md                          # This file
├── wiki-log.md                            # Append-only operation chronicle
├── v5-harness-*.md                        # Architecture, quality, progress pages
├── v5-omc-agent-integration.md            # Feature proposals (flat, not in proposals/)
├── v5-conversational-discord-operator.md  # Feature proposals (flat, not in proposals/)
├── session-log-*.md                       # Per-session logs (flat, not in logs/)
└── *-archive-YYYY.md                      # Archived pages (flat, not in archive/)
```

**Naming conventions:**
- Ozymandias pages: `ozy-{topic}.md`
- Harness core pages: `v5-harness-{topic}.md`
- Feature proposals: `v5-{feature-name}.md`
- Phase readiness: `v5-phase{N}-readiness.md`
- Session logs: `session-log-{YYYY-MM-DD}-{slug}.md`
- Archive pages: `{original-page-name}-archive-YYYY.md`
- Operation log: `wiki-log.md` (not `log.md` — that name is reserved by the wiki plugin)

## Page Size Policy

Per-category byte ceilings (enforced via `wiki_lint` or manual `wc -c`):

| WikiCategory | Ceiling | Rationale |
|---|---|---|
| `debugging` | 8KB | High churn — archive resolved items aggressively |
| `decision` | 8KB | Tracking pages — archive when resolved |
| `architecture` | 10KB | Reference material, lower churn |
| `pattern` | 10KB | Meta/guide pages |
| `reference` | 12KB | Archive material, rarely read in full |
| `session-log` | 6KB | Auto-pruned after 30 days |
| `environment` | 10KB | Infrequent |
| `convention` | 10KB | Infrequent |

**Special entries:** `index.md`: 30 lines maximum (matches SessionStart injection window).

When a page exceeds its ceiling, archive resolved/completed items first (cheapest intervention). If still over, split by topic coherence. See [[wiki-operations]] for archive rotation and split procedures.

## Freshness Policy

- **`updated:` field is mandatory.** Every edit must bump the date.
- **30-day staleness flag:** Pages >30 days old flagged for review (confirm or revise).
- **Enforcement:** `wiki_lint` staleness check. Manual audit until automated.
- **Exemptions:** Archive pages and session logs (historical records).

## Archive Rotation

See [[wiki-operations]] for full archive rotation mechanics (naming, frontmatter template, triggers, examples). Key points:

- **Naming:** `{original-page-name}-archive-YYYY.md`
- **Triggers:** page exceeds byte ceiling, OR resolved items exceed 50% of content
- **Archive pages are excluded from index.md** but searchable via `wiki_query`

## Frontmatter Template

```yaml
---
title: Page Title (human-readable)
tags: [tag1, tag2, tag3]
category: [architecture|decision|pattern|debugging|environment|session-log|reference|convention]
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

## Categories

Categories are enum-constrained by the `WikiCategory` type. You must use exactly one of these values:

| Category | Use for |
|----------|---------|
| `architecture` | System structure, module design, data flow |
| `decision` | Trade-offs, decisions, rationale |
| `pattern` | Meta docs (guides, conventions) |
| `debugging` | Bugs, incident tracking |
| `environment` | Environment setup, tooling config |
| `session-log` | Per-session discoveries |
| `reference` | Reference material, archived content |
| `convention` | Coding standards, process rules |

Do not use phantom categories (`design`, `roadmap`, `quality`, `progress`) — these are not valid `WikiCategory` enum values and will fail validation.

## Tag Conventions

Tags are free-form strings (not enum-constrained like categories). They drive `wiki_query` relevance scoring (tags weight 3 vs content weight 1), so consistent tags significantly improve search precision.

**Core canonical tags:**
`harness`, `bugs`, `architecture`, `design`, `roadmap`, `quality`, `progress`, `escalation`, `discord`, `agent`, `pipeline`, `archive`

**Rules:**
- Every page should have 2-5 tags
- Prefer canonical tags for search consistency
- New canonical tags can be added to this list when a recurring topic warrants it
- **Minimum enforced:** Pages with <2 tags should be flagged during review. Manual audit until `wiki_lint` tag-count check ships (Phase 3+).
- **No single-use tags.** If a tag appears on only one page, it adds no search value. Either add it to more pages or remove it.
- **Tag audit:** During archive rotation or page splits, verify tags on both the active and archive pages.

## Cross-References: When to Use Links

**Use `[[page-name]]` when** mentioning another wiki page, establishing a relationship, or referencing a documented concept.

**Do NOT link:** file paths (use `file:line`), commit hashes (use `git log`), or code snippets.

## When to Split a Page

See [[wiki-operations]] for full split procedures and naming conventions. Key points:

- **Archive first** — resolved/completed items are cheapest to remove
- **Split by topic** only if still over ceiling after archiving
- **Split naming:** `{parent-page}-{subtopic}.md`

## What Does NOT Belong in the Wiki

- **Code documentation** → Use inline comments and docstrings
- **Git history** → Use `git log` and commit messages
- **Ephemeral debugging notes** → Use session logs (auto-captured)
- **Exact code snippets** → Reference `file:line` instead (stays fresh)
- **Individual method signatures** → Use LSP hover or code comments

## Relationship to Other Docs

| Document | Purpose | When to Write |
|----------|---------|---------------|
| `CLAUDE.md` | Active conventions (rules) | When a rule changes or new constraint established |
| `COMPLETED_PHASES.md` | Phase narratives (history) | After phase completion, before implementation |
| `DRIFT_LOG.md` | Spec deviations (signatures, edge cases) | When implementation differs from spec in non-obvious ways |
| `plans/` | Approved designs (before building) | For non-trivial architectural work, before coding |
| `.omc/wiki/` | Living knowledge base (now+future) | For ongoing architecture, roadmap, quality tracking, progress |
| `.omc/wiki/v5-harness-drift-log.md` | V5 plan deviations (Plan/Impl/Why) | When harness implementation differs from `plans/2026-04-08-v5-harness-architecture.md` |
| `.omc/wiki/v5-harness-open-concerns.md` | Engineering concerns (not bugs) | Performance issues, unvalidated assumptions, unbounded growth |
| `.omc/wiki/ozy-drift-log.md` | Ozy spec deviations (Spec/Impl/Why) | When trading bot implementation differs from spec |
| `.omc/wiki/ozy-open-concerns.md` | Ozy trading concerns | Entry condition gaps, prompt inefficiency, sizing issues |

## Quick Rules

1. **One concern per page** — focused pages, link liberally.
2. **Frontmatter required** — valid `WikiCategory` enum, template above.
3. **Link as you write** — mention a wiki topic → add `[[page-name]]`.
4. **Reference files, not snippets** — `file:line`, not copy-pasted code.
5. **Update `updated:` on every edit.**
6. **Index is auto-built** — don't manually edit `index.md`.
7. **Flat root only** — no subdirectories in `.omc/wiki/`.
8. **Link, don't duplicate** — one canonical source, others cross-reference via `[[page-name]]`.
9. **Agents: prefer `wiki_query`** — use `wiki_read` only for full page context.
10. **Check drift log** after implementation — file deviations from v5 plan in [[v5-harness-drift-log]].
