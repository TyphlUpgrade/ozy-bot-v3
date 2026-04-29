# Wiki Organization Policy — v5 Harness Project

**Revision 2** — Incorporates Architect and Critic feedback from iteration 1.

## RALPLAN-DR Summary

### Principles (5)

1. **LLM-First Consumption** — The wiki's primary readers are agents via MCP tools (keyword+tag search, relevance scoring). Human readability is secondary but still valued.
2. **Bounded Growth** — No page grows without limit. Every append-only section has a byte-size ceiling and a defined overflow strategy.
3. **Find by Query, Not by Browse** — Agents find pages via `wiki_query` (tags weight 3, title weight 5, content weight 1). Organization must optimize for tag/title hit rates, not hierarchical navigation. **All pages must live in the wiki root directory** — `listPages()` uses flat `readdirSync` and does not recurse into subdirectories. Subdirectory pages are invisible to search, lint, and auto-indexing.
4. **One Concern per Page** — Each page owns one topic. When a page accretes multiple concerns, it splits. Cross-references (`[[page-name]]`) replace inline duplication.
5. **Decay-Aware** — Resolved bugs, completed phases, and stale session logs are archived or pruned on a defined schedule. The wiki reflects current state, not full history.

### Decision Drivers (Top 3)

1. **Page size vs. search precision** — Large pages dilute keyword relevance (content weight 1 competes with tag weight 3). Smaller, focused pages score higher for specific queries. But too many tiny pages create index bloat (SessionStart injects first 30 lines of index).
2. **Append-only growth risk** — `v5-harness-known-bugs.md` (164 lines, 13.1KB — over the 10KB default lint ceiling) and `v5-harness-reviewer-findings.md` grow monotonically. Without ceilings, they become noise-heavy for agents searching current issues.
3. **Index as context budget** — SessionStart injects only the first 30 lines of the index. The current index is 47 lines — agents already see only the first ~60% at session start. The index must prioritize the most-queried categories in its first 30 lines. Full browsing uses `wiki_list`; targeted lookup uses `wiki_query`.

### Viable Options

#### Option A: Soft Limits + Manual Splits (Lightweight)

**Approach:** Document recommended page size limits in the wiki guide. Rely on wiki_lint to flag oversized pages. Authors (agents or humans) split pages when lint warns.

| Pros | Cons |
|------|------|
| Zero tooling changes needed | Relies on discipline — lint warnings can be ignored |
| Simple mental model | No archive automation — resolved bugs stay in-page forever |
| Works today with existing wiki_lint | Doesn't address index growth |

#### Option B: Tiered Lifecycle with Flat Archive Rotation (Structured)

**Approach:** Define hard page byte-size ceilings (8KB for tracking pages, 12KB for architecture pages). Resolved items rotate to flat-named archive pages (`*-archive-YYYY.md`) in the wiki root. Archive pages use `category: reference` and are excluded from index.md but remain searchable via `wiki_query`. Session logs auto-prune after 30 days. Existing subdirectory pages (proposals/, logs/) are flattened to restore searchability.

| Pros | Cons |
|------|------|
| Bounded growth — pages never exceed byte ceiling | Requires archive naming convention and rotation workflow |
| Index stays lean — only active pages listed | More wiki root files (mitigated by clear naming prefixes) |
| Resolved bugs/findings move to archive, reducing noise | More complex wiki guide |
| wiki_lint already enforces byte-based ceilings | Initial migration to flatten existing subdirectory pages |
| Session log pruning prevents unbounded log growth | Archive pages visible in file listing (less "clean" than subfolder) |
| **All pages searchable via wiki_query** — no invisible pages | Flat naming requires discipline in prefixes |

#### Option C: Tag-Based Virtual Sections (Semantic)

**Approach:** All pages in wiki root, organized purely via tags and `category` field. Navigation entirely through `wiki_query` with tag filters. Index becomes a tag cloud.

| Pros | Cons |
|------|------|
| Maximizes search-first philosophy | Humans lose browsability entirely |
| No subdirectory management overhead | Index as tag cloud is novel — may confuse agents |
| Scales to hundreds of pages | Doesn't address page size growth |

**Invalidation of Option C:** Does not address the core user concerns (page size, findability). The tag cloud index format would require MCP tool changes and break SessionStart injection which expects a page list. While Option B adopts flat-root storage (which Option C also requires), it adds the size ceilings and archive lifecycle that Option C lacks.

---

## Requirements Summary

Design a wiki organization policy that prevents unbounded page growth, keeps the index concise, ensures agents can efficiently find current information via keyword+tag search, and fixes existing subdirectory invisibility.

### Context

- **Current state:** 8 pages in wiki root, 6 pages in subdirectories (invisible to search), ~1600 total lines
- **Largest page:** `v5-harness-known-bugs.md` — 164 lines, 13.1KB (over 10KB default lint ceiling)
- **Consumption:** 7 MCP tools; `wiki_query` uses relevance scoring (tags=3, title=5, content=1)
- **Context injection:** SessionStart injects first 30 lines of index (current index is 47 lines — already truncated)
- **Existing tooling:** `wiki_lint` detects orphans, stale content, broken refs, oversized pages (byte-based, default 10KB), contradictions
- **Tooling constraint:** `listPages()` uses flat `readdirSync(wikiDir)` — does NOT recurse into subdirectories. `safeWikiPath()` rejects filenames containing `/`. All pages must live in the wiki root to be searchable.
- **Category enum:** `WikiCategory` type allows: `architecture`, `decision`, `pattern`, `debugging`, `environment`, `session-log`, `reference`, `convention`
- **Growth vectors:** Bug tracker (append-only), reviewer findings (append-only), session logs (append-only), future Ozymandias trading bot section
- **Pre-existing problem:** 5 pages in `proposals/` and `logs/` subdirectories are invisible to `wiki_query`, `wiki_lint`, and auto-indexing. The current `index.md` is manually maintained and references these paths, but `updateIndexUnsafe()` would overwrite them.

## Acceptance Criteria

1. **Page byte-size ceiling defined** — tracking/quality pages (bugs, findings) have an 8KB hard ceiling; architecture/design pages have a 12KB ceiling. Documented in `wiki-guide.md` with byte values matching `wiki_lint` measurement.
2. **Archive rotation documented** — clear procedure for moving resolved/completed items to flat-named archive pages, with defined triggers (page exceeds byte ceiling, OR resolved items exceed 50% of page content).
3. **Index budget documented** — active index kept under 30 lines so SessionStart injection captures the full index. Archive and session-log pages excluded from index but queryable via `wiki_query`.
4. **Session log pruning** — 30-day retention period. Pruning procedure documented.
5. **Category usage corrected** — wiki-guide.md documents the actual `WikiCategory` enum values. Existing pages using phantom categories are corrected. Tags (free-form) and categories (enum-constrained) are clearly distinguished.
6. **Split criteria defined** — when a page should split (byte size, topic drift, section count), and naming convention for split pages.
7. **Subdirectory pages flattened** — All pages in `proposals/` and `logs/` moved to wiki root with flat naming. No future pages created in subdirectories.
8. **wiki_lint alignment** — document that `wiki_lint` already enforces byte-based ceilings (default 10KB). Per-category overrides documented as a future enhancement.

## Implementation Steps

### Step 0 (Prerequisite): Flatten existing subdirectory pages

**Problem:** `proposals/` and `logs/` subdirectories contain 5+ pages invisible to `wiki_query`, `wiki_lint`, and auto-indexing. This is the same broken pattern that the original plan proposed with `archive/`.

**Action:**
- Move `proposals/v5-omc-agent-integration.md` → `v5-omc-agent-integration.md` (wiki root)
- Move `proposals/v5-conversational-discord-operator.md` → `v5-conversational-discord-operator.md` (wiki root)
- Move session logs from `logs/session-log-*.md` → `session-log-*.md` (wiki root)
- Move `logs/log.md` → `wiki-log.md` (wiki root — `log.md` is a RESERVED_FILE in the wiki plugin and cannot be used as a page name)
- Remove empty `proposals/` and `logs/` directories
- Update `index.md` to reference flat paths
- Update `wiki-guide.md` directory structure section — remove subdirectories, document flat-root policy
- Update all `[[cross-references]]` that used subdirectory paths
- **Files:** `.omc/wiki/proposals/*`, `.omc/wiki/logs/*`, `.omc/wiki/index.md`, `.omc/wiki/wiki-guide.md`

**Verification:** `wiki_lint` can see all pages; `wiki_query("agent integration")` returns the proposals page.

### Step 1: Define byte-size ceilings and document in wiki-guide.md

- Tracking/quality pages (bugs, findings): **8KB** hard ceiling (wiki_lint default is 10KB — our ceiling is stricter)
- Architecture/design pages: **12KB** soft ceiling (lint warning at 10KB default, guide warns at 12KB)
- Session logs: **6KB** per log
- Index: **30 lines maximum** (matches SessionStart injection window)
- Document that `wiki_lint` measures size in bytes, not lines. Reference `config.maxPageSize` (default 10,240 bytes).
- **File:** `.omc/wiki/wiki-guide.md` — add "Page Size Policy" section

### Step 2: Define archive rotation workflow (flat naming)

- **No subdirectories.** Archive pages live in wiki root with flat naming: `{original-page-name}-archive-YYYY.md`
- Example: `v5-harness-known-bugs-archive-2026.md`
- Archive pages use `category: reference` in frontmatter (valid `WikiCategory` enum value)
- Archive pages tagged with original page's tags plus `archive` tag
- Archive pages are **excluded from index.md** but **searchable via wiki_query** (because they live in wiki root)
- Rotation triggers:
  - Page exceeds its byte-size ceiling, OR
  - Resolved/completed items exceed 50% of page content (detected by counting `~~strikethrough~~ RESOLVED` headers or `## Resolved` section size)
- wiki_lint `archive-candidate` signal: when a `debugging` or `quality` category page has >50% resolved items, lint emits a warning recommending rotation. (Lint code change tracked as separate implementation task.)
- **File:** `.omc/wiki/wiki-guide.md` — add "Archive Rotation" section

### Step 3: Correct category documentation and define tag conventions

**Categories** (enum-constrained by `WikiCategory` type — must use one of these):
- `architecture` — System structure, module design, data flow
- `decision` — Trade-offs, decisions, rationale
- `pattern` — Meta docs (guides, conventions)
- `debugging` — Bugs, incident tracking
- `environment` — Environment setup, tooling config
- `session-log` — Per-session discoveries
- `reference` — Reference material, archived content
- `convention` — Coding standards, process rules

**Tags** (free-form, used for search relevance scoring):
- Core tags: `harness`, `bugs`, `architecture`, `design`, `roadmap`, `quality`, `progress`, `escalation`, `discord`, `agent`, `pipeline`, `archive`
- Rule: every page should have 2-5 tags. Tags are free-form but should prefer canonical tags for search consistency.
- New canonical tags can be added to the list in wiki-guide.md.

**Category corrections needed on existing pages:**
- Audit all pages for phantom categories (`design`, `roadmap`, `quality`, `progress`) and replace with valid enum values
- `v5-harness-known-bugs.md`: already uses `debugging` — correct
- `wiki-guide.md`: uses `pattern` — correct
- Check all other pages against enum

**File:** `.omc/wiki/wiki-guide.md` — replace existing category section with corrected list; add "Tag Conventions" section

### Step 4: Define split criteria

- **Byte size trigger:** page exceeds its ceiling → must split or archive resolved items
- **Topic drift:** if a page has 3+ H2 sections on unrelated topics → split by topic
- **Naming:** split pages use `{parent-page}-{subtopic}.md`
- **Cross-reference:** parent page gets a "See also" section linking to splits
- **File:** `.omc/wiki/wiki-guide.md` — add "When to Split a Page" section

### Step 5: Compact the index to fit 30-line SessionStart window

- Current index is 47 lines; only first 30 injected at session start
- Redesign index format: one line per page, minimal section overhead
- Prioritize most-queried categories in first 30 lines (Architecture, Quality, Roadmap)
- Session logs and archive pages excluded from index
- Add note: "For full page listing, use `wiki_list`. For targeted lookup, use `wiki_query`."
- Target: ≤28 lines (leaves 2-line buffer)
- **Note:** `updateIndexUnsafe()` auto-generates the index on every `writePage()` call, grouped by `WikiCategory`. The auto-generated format is already compact (one line per page). After initial manual compaction, subsequent auto-generated indices should stay within budget as long as archive/session-log pages are excluded from the auto-index. If auto-generation overwrites manual formatting, that is acceptable — the budget constraint is on line count, not aesthetic layout.
- **File:** `.omc/wiki/index.md`

### Step 6: Apply policy to current oversized pages

- `v5-harness-known-bugs.md` (164 lines, 13.1KB — over 10KB): Move 9 resolved bugs to `v5-harness-known-bugs-archive-2026.md`. Expected: ~80 lines, ~6KB remaining.
- `v5-harness-reviewer-findings.md`: audit byte size, archive completed review rounds if over 8KB ceiling.
- Session logs: verify retention (30 days from creation date), prune any older than 2026-03-10.
- **Files:** Multiple wiki pages

### Step 7: Document wiki_lint alignment

- Document that wiki_lint already enforces byte-based page size ceiling (default 10KB via `config.maxPageSize`)
- Note that per-category byte overrides (8KB for quality, 12KB for architecture) and `archive-candidate` detection are tracked as future `wiki_lint` code enhancements
- Note that missing canonical tags can be flagged by lint as a future enhancement
- **File:** `.omc/wiki/wiki-guide.md` — add "Lint Alignment" section in Quick Rules

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Archived bugs hard to find when debugging a regression | Archive pages live in wiki root — fully searchable via `wiki_query`. Cross-ref `[[archive-page]]` from active page's "See also" section. |
| Index exceeds 30-line SessionStart injection window | Compact index format (Step 5). Agents use `wiki_list` for full browse, `wiki_query` for targeted lookup. |
| Tag usage becomes inconsistent | Canonical tag list in wiki-guide.md. Future wiki_lint enhancement to validate tags. |
| Split pages create orphans that nobody links to | wiki_lint orphan detection catches unlinked pages. Split workflow requires cross-references. |
| Archive rotation gets skipped | wiki_lint `archive-candidate` signal detects pages with >50% resolved items. Lint is the enforcement mechanism, not manual discipline. |
| Flattening subdirectories increases wiki root clutter | Consistent naming prefixes (`v5-`, `session-log-`, `*-archive-*`) keep files visually grouped. Filename prefix is the only human-browsing affordance; agents use tags. |
| `archive` not a valid WikiCategory | Archive pages use `category: reference`. The `archive` tag (free-form) provides search differentiation. |

## Verification Steps

1. After flattening (Step 0): `wiki_lint` detects all previously-invisible pages (proposals, session logs). `wiki_query("agent integration")` returns the flattened proposal page.
2. After size policy (Step 1): wiki-guide.md documents byte-based ceilings matching wiki_lint's measurement unit.
3. After archive rotation (Step 6): `v5-harness-known-bugs.md` is under 8KB. `v5-harness-known-bugs-archive-2026.md` exists in wiki root with `category: reference` and is returned by `wiki_query("resolved bugs")`.
4. After index compaction (Step 5): `index.md` is ≤30 lines. SessionStart injection captures the full index.
5. After category correction (Step 3): every page's `category` field uses a valid `WikiCategory` enum value. No phantom categories remain.
6. wiki-guide.md contains all new sections: Page Size Policy, Archive Rotation, Tag Conventions, When to Split, Lint Alignment.

---

## ADR (Architecture Decision Record)

**Decision:** Adopt Option B — Tiered Lifecycle with Flat Archive Rotation

**Drivers:** Page byte-size growth in append-only tracking pages; index context budget constraint (30-line SessionStart window); need for agents to find current issues without noise from resolved items; existing subdirectory pages invisible to search.

**Alternatives considered:**
- Option A (Soft Limits + Manual Splits): Too reliant on discipline; no archive mechanism; doesn't fix existing subdirectory invisibility
- Option C (Tag-Based Virtual Sections): Requires MCP tool changes for tag-cloud index; doesn't address page size growth

**Why chosen:** Option B directly addresses all user concerns (page size, findability, navigation) with bounded growth guarantees. The critical revision from v1: flat archive naming in wiki root instead of `archive/` subfolder, because `listPages()` does not recurse into subdirectories. This also surfaced the pre-existing problem with proposals/ and logs/ pages being invisible to search — Step 0 fixes this. Byte-based ceilings align with wiki_lint's existing measurement. Lint-driven archive signals replace unenforceable manual checklists.

**Consequences:**
- Existing subdirectory pages must be flattened (one-time migration)
- Wiki root will have more files (mitigated by naming prefixes)
- wiki_lint code enhancements tracked separately (archive-candidate signal, per-category byte overrides)
- wiki-guide.md directory structure section must be updated to reflect flat-root policy
- Index must be compacted to fit 30-line window

**Follow-ups:**
- Implement archive rotation for v5-harness-known-bugs.md (Step 6)
- wiki_lint code: add `archive-candidate` detection, per-category byte overrides
- Review tag taxonomy after 3 months of usage
- Monitor index line count as pages are added

---

## Changelog

### v2 (Architect + Critic revision)

| Issue | Source | Fix applied |
|-------|--------|-------------|
| `archive/` subfolder invisible to `wiki_query` | Architect finding #1 (BLOCKING) | Flat `*-archive-YYYY.md` naming in wiki root |
| Size ceilings in lines; lint uses bytes | Architect finding #2 | Byte-based ceilings (8KB/12KB/6KB) |
| No automated rotation trigger | Architect finding #3 | wiki_lint `archive-candidate` signal |
| Index exceeds 30-line SessionStart window | Architect finding #4 | Step 5: compact index to ≤30 lines |
| Tag taxonomy vs WikiCategory enum mismatch | Architect finding #5 | Step 3: separate categories (enum) from tags (free-form) |
| known-bugs.md is 164 lines, not 258 | Architect finding #6 | Corrected metrics throughout |
| Existing proposals/ and logs/ pages invisible | Critic CRITICAL finding | Step 0: flatten all subdirectory pages |
| wiki-guide.md documents phantom categories | Critic MAJOR finding | Step 3: correct category documentation |
| `archive` not in WikiCategory enum | Critic ambiguity flag | Archive pages use `category: reference` |
| Risk mitigations reference non-functional capabilities | Critic FAIL on risk assessment | Rewritten: all mitigations reference working mechanisms |
| Verification step 5 would fail | Critic FAIL on verification | Rewritten: verifies flat archive page searchability |

### v2 post-approval improvements (Architect + Critic iteration 2 observations)

| Observation | Source | Applied |
|-------------|--------|---------|
| `log.md` is a RESERVED_FILE — rename rationale clarified | Critic minor #2 | Updated Step 0 rename rationale |
| Page count inaccuracy (8 root + 6 subdirectory, not 10+5) | Critic minor #1 | Corrected in Context section |
| `updateIndexUnsafe()` overwrites manual index on every `writePage()` | Architect observation B | Added note to Step 5 acknowledging auto-generation |
