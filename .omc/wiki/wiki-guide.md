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
| Code review finding or audit issue | [[v5-harness-reviewer-findings]] | Refactoring opportunity, safety concern |
| Feature proposal or integration plan | [[v5-omc-agent-integration]], [[v5-conversational-discord-operator]] | New capability, architecture change |
| Phase completion checklist | [[v5-phase3-readiness]] | Sign-off criteria, due diligence |
| Per-session discovery or experiment | Session Logs (auto-named) | Dead-end investigation, prototype notes |

## Directory Structure

All wiki pages live directly in the wiki root. No subdirectories. This is required because `listPages()` uses a flat `readdirSync` and does not recurse into subdirectories — pages in subfolders are invisible to `wiki_query`, `wiki_lint`, and auto-indexing.

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
- Harness core pages: `v5-harness-{topic}.md`
- Feature proposals: `v5-{feature-name}.md`
- Phase readiness: `v5-phase{N}-readiness.md`
- Session logs: `session-log-{YYYY-MM-DD}-{slug}.md`
- Archive pages: `{original-page-name}-archive-YYYY.md`
- Operation log: `wiki-log.md` (not `log.md` — that name is reserved by the wiki plugin)

Do NOT create subdirectories. Any page placed in a subfolder will be invisible to all wiki tooling.

## Page Size Policy

Page size is measured in bytes by `wiki_lint` (via `config.maxPageSize`, default 10,240 bytes).

| Page type | Ceiling | Enforcement |
|-----------|---------|-------------|
| Tracking/quality pages (bugs, findings) | 8KB hard ceiling | Split or archive when reached |
| Architecture/design pages | 12KB soft ceiling | wiki_lint warns at 10KB default; guide warns at 12KB |
| Session logs | 6KB per log | Auto-pruned after 30 days |
| index.md | 30 lines maximum | Matches SessionStart injection window |

When a page exceeds its ceiling, either archive resolved/completed items or split the page by topic. See **Archive Rotation** and **When to Split a Page** below.

## Archive Rotation

When a tracking page exceeds its byte ceiling or resolved items exceed 50% of content, move resolved items to an archive page.

**Archive page naming:** `{original-page-name}-archive-YYYY.md` in the wiki root.

Example: `v5-harness-known-bugs-archive-2026.md`

**Archive page frontmatter:**
```yaml
---
title: [Original Title] — Archive 2026
tags: [original-tags, archive]
category: reference
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

- Use `category: reference` (the `archive` tag differentiates it from other reference pages)
- Include original page's tags plus the `archive` tag
- Archive pages are **excluded from index.md** but **searchable via `wiki_query`** (because they live in wiki root)
- Add a "See also" link in the active page pointing to the archive page

**Rotation triggers:**
1. Page exceeds its byte-size ceiling, OR
2. Resolved/completed items exceed 50% of page content — detected by counting `~~strikethrough~~ RESOLVED` headers or measuring `## Resolved` section size

**Future enhancement:** `wiki_lint` will add an `archive-candidate` signal when a `debugging` category page has >50% resolved items. This is tracked as a separate lint code enhancement.

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

## Cross-References: When to Use Links

**Use `[[page-name]]` when:**
- You mention another wiki page by name — readers may want to navigate
- You're establishing a relationship (depends on, conflicts with, extends)
- You reference a concept documented elsewhere

**Example:**
```
This design extends [[v5-harness-architecture]] by adding priority queuing
to the FIFO intake. See [[v5-phase3-readiness]] for sign-off criteria.
```

**Do NOT use links for:**
- File paths (use `file:line` format instead)
- Commit hashes (use `git log` instead)
- Code snippet references (inline code or `file:line`)

## When to Split a Page

Split a page when either trigger fires:

1. **Byte size trigger** — page exceeds its ceiling. Archive resolved items first; if still over, split by topic.
2. **Topic drift** — 3+ H2 sections cover unrelated topics. Split by topic.

**Split naming:** `{parent-page}-{subtopic}.md`

Example: `v5-harness-known-bugs.md` → `v5-harness-known-bugs-agent-roles.md`

**After splitting:** add a "See also" section to the parent page linking to the split pages. `wiki_lint` orphan detection will catch any split pages that lose their cross-reference.

## What Does NOT Belong in the Wiki

- **Code documentation** → Use inline comments and docstrings
- **Git history** → Use `git log` and commit messages
- **Ephemeral debugging notes** → Use session logs (auto-captured)
- **Exact code snippets** → Reference `file:line` instead (stays fresh)
- **Individual method signatures** → Use LSP hover or code comments

Example of "wrong way":
```
The `_process_signal()` method in conductor.py line 42 checks
if signal.status == "READY" and then...
```

Right way:
```
See `conductor.py:42` for signal intake logic. The method validates
status before processing.
```

## Relationship to Other Docs

| Document | Purpose | When to Write |
|----------|---------|---------------|
| `CLAUDE.md` | Active conventions (rules) | When a rule changes or new constraint established |
| `COMPLETED_PHASES.md` | Phase narratives (history) | After phase completion, before implementation |
| `DRIFT_LOG.md` | Spec deviations (signatures, edge cases) | When implementation differs from spec in non-obvious ways |
| `plans/` | Approved designs (before building) | For non-trivial architectural work, before coding |
| `.omc/wiki/` | Living knowledge base (now+future) | For ongoing architecture, roadmap, quality tracking, progress |

## Quick Rules

1. **One concern per page** — Keep pages focused. Link liberally.
2. **Frontmatter required** — Use the template above. Category must be a valid `WikiCategory` enum value.
3. **Link as you write** — When you mention another wiki topic, add `[[page-name]]`.
4. **Reference files, not snippets** — Use `file:line`, not copy-pasted code.
5. **Update the timestamp** — Change `updated:` field when you edit.
6. **Index is auto-built** — Don't manually edit `index.md` for content. Structure it via frontmatter.
7. **Flat root only** — Never create subdirectories. All pages go directly in `.omc/wiki/`.
8. **Lint alignment** — `wiki_lint` already enforces a byte-based page size ceiling (default 10KB via `config.maxPageSize`). Per-category byte overrides (8KB for `debugging`, 12KB for `architecture`) and `archive-candidate` detection are tracked as future `wiki_lint` code enhancements.
