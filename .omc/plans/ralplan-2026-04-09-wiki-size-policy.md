# Wiki Health Policy — Consensus Plan

## Requirements Summary

The v5 harness wiki (`.omc/wiki/`) is consumed by LLM agents whose context token cost is invisible at read time. File size is the only observable proxy for ingestion cost. 6 of 12 substantive pages exceed the documented 8KB soft ceiling. The ceiling is unenforced — `wiki_lint` warns at 10KB but warnings are ignored.

Beyond size, three additional wiki health concerns must be addressed: (a) **staleness** — pages with outdated information waste context on wrong answers, which is worse than wasting context on large correct answers; (b) **tag hygiene** — `wiki_query` weights tags 3x over content, so inconsistent/sparse tags degrade the primary retrieval path; (c) **content duplication** — the same information in multiple pages means multiple maintenance surfaces and conflicting versions.

The policy must: (1) make ingestion cost bounded and observable, (2) be enforceable without relying on agent behavioral discipline, (3) require minimal ongoing maintenance, (4) work with current tooling now and allow tool-level enhancement later, (5) keep content fresh, discoverable, and non-duplicated.

## RALPLAN-DR Summary

### Principles (5)

1. **Observable cost proxy** — File size is the only visible proxy for context ingestion cost. Policy must operate on the observable property, not the hidden one.
2. **Tool-level over behavioral enforcement** — Agents are LLMs; multi-step behavioral protocols erode. Enforcement must be structural (lint, tooling) not behavioral (role file instructions).
3. **Query-first discovery** — `wiki_query` (keyword + tag matching, snippet return) is the primary retrieval path. Policy must not create competing navigation mechanisms. Tags are the highest-leverage input to query quality.
4. **Archive for relevance, split for coherence** — Resolved content moves to archive because it's no longer actionable. Large pages split when they cover multiple distinct topics, not when they hit a byte count.
5. **Fresh over large** — A small stale page is worse than a large current page. Staleness wastes context on wrong answers; size only wastes context on extra right answers. Freshness enforcement complements size enforcement.

### Decision Drivers (top 3)

1. **Ingestion cost visibility** — The user's core insight: "ingestion size hides, file size doesn't." Must keep the cost observable.
2. **Maintenance burden** — Prior SECTIONS-block proposal failed review because manual keyword-to-heading mappings rot within days. Any policy must have near-zero ongoing maintenance cost.
3. **Current tooling constraints** — `wiki_read` returns full pages (no section/offset parameter). `wiki_query` returns snippets. `wiki_lint` supports `maxPageSize` but not per-category enforcement. These constrain short-term options.

### Viable Options

#### Option A: Enforce Ceilings + Split/Archive (Recommended)

**Approach:** Enforce per-category byte ceilings via `wiki_lint`. Split or archive the 6 oversized pages now. Add a one-line wiki hint to agent role files. Defer tool-level enhancement to Phase 3+.

| Pros | Cons |
|------|------|
| Zero tooling investment — works today | Ceilings will be re-exceeded as features are added |
| Observable enforcement via lint | Splitting creates more files to maintain |
| One-time migration cost | Splitting decisions require judgment calls on topic boundaries |
| Proven pattern (bugs archive rotation already works) | Per-category ceilings add cognitive load to wiki-guide |

#### Option B: Tool-Level Enforcement (wiki_read section parameter)

**Approach:** Add `section` parameter to `wiki_read` tool. Auto-generate section indexes at page write time. Drop all byte ceilings — pages grow without limit because the tool itself returns bounded chunks.

| Pros | Cons |
|------|------|
| Structurally enforced — no behavioral dependence | Requires OMC plugin code changes (not in our control) |
| Zero maintenance (auto-generated indexes) | Deferred to Phase 3+ at earliest |
| Pages stay unified — one source of truth per topic | Doesn't solve the problem NOW — 6 pages still oversized |
| Makes ingestion cost truly bounded regardless of file size | Unknown timeline for plugin enhancement |

**Invalidation rationale for Option B as sole strategy:** The wiki plugin is third-party (oh-my-claudecode). We cannot ship plugin code changes on the timeline needed to resolve the 6 oversized pages. Option B is the correct medium-term investment but cannot be the short-term policy.

### Selected: Option A now, Option B as Phase 3+ enhancement

---

## Acceptance Criteria

1. **AC-1:** No wiki page exceeds its category ceiling after implementation. Verified by `wiki_lint` (or manual `wc -c` until per-category lint is available).
2. **AC-2:** `wiki-guide.md` documents the revised policy with per-category ceilings and enforcement rules. The old SECTIONS-block and behavioral protocol text is absent.
3. **AC-3:** Each of the 6 oversized pages is either split or archived below its ceiling. Verified by `wc -c` on each resulting file.
4. **AC-4:** Agent role files (`config/harness/agents/*.md`) contain a one-line wiki usage hint. Verified by grep.
5. **AC-5:** `index.md` remains ≤30 lines. Verified by `wc -l`.
6. **AC-6:** All cross-references (`[[page-name]]`) resolve after splits/archives. Verified by `wiki_lint` broken-ref check or manual grep.
7. **AC-7:** No new wiki tooling or plugin changes required for the short-term implementation.
8. **AC-8:** `wiki-guide.md` documents a staleness policy: pages with `updated:` >30 days old are flagged by `wiki_lint` (or manual audit until lint support ships). Verified by reading the guide.
9. **AC-9:** `wiki-guide.md` documents tag hygiene rules: minimum 2 tags per page, canonical tag list maintained, `wiki_lint` flags violations. Verified by reading the guide.
10. **AC-10:** `wiki-guide.md` documents a de-duplication rule: link to the canonical source instead of copying content. Verified by reading the guide.
11. **AC-11:** All existing wiki pages have ≥2 tags. Verified by grep on frontmatter. Exemptions: `index.md` and `log.md` (structural pages without standard frontmatter).

## Implementation Steps

**Phase ordering:** A → A2 → B (B5 depends on A+A2 byte totals) → C → D. Phases B1-B4 are independent of each other and can run in parallel.

### Phase A: Policy Update (wiki-guide.md)

**File:** `.omc/wiki/wiki-guide.md`

1. Replace the "Page Size Policy" section (`wiki-guide.md:49-60`) with revised ceilings:

**WikiCategory-to-ceiling mapping** (machine-readable, single source of truth for future `wiki_lint` per-category enforcement):

| WikiCategory | Ceiling | Rationale |
|---|---|---|
| `debugging` | 8KB | High churn, frequent reads — archive resolved items aggressively |
| `decision` | 8KB | Tracking pages with findings — archive when resolved |
| `architecture` | 10KB | Reference material, lower churn (tightened from prior 12KB soft ceiling) |
| `pattern` | 10KB | Meta/guide pages |
| `reference` | 12KB | Archive/reference material, rarely read in full |
| `session-log` | 6KB | Auto-pruned after 30 days |
| `environment` | 10KB | Infrequent |
| `convention` | 10KB | Infrequent |

**Special entries:**
- `index.md`: 30 lines maximum (matches SessionStart injection window)

**Note:** The architecture ceiling is tightened from 12KB soft to 10KB. Two pages currently between 10-12KB (`v5-harness-design-decisions.md`, `v5-phase3-readiness.md`) will be split/archived as part of this plan.

2. Update the "When to Split a Page" section to emphasize:
   - Archive resolved/completed content FIRST (cheapest intervention)
   - Split by topic coherence only if still over ceiling after archiving
   - Split naming convention: `{parent-page}-{subtopic}.md`

3. Remove any references to SECTIONS blocks or behavioral read protocols (none exist yet — this is a preventive measure).

4. Add a "Future Enhancement" note documenting the Phase 3+ plan for `wiki_read` section parameter.

5. Add to "Quick Rules": *"Agents: prefer `wiki_query` for targeted lookup. Use `wiki_read` only when you need full page context."*

6. **Update Quick Rules #8** (`wiki-guide.md:203`) to reflect the new architecture ceiling (10KB, not 12KB). The current text says `Per-category byte overrides (8KB for debugging, 12KB for architecture)` — this must be updated to match the new WikiCategory mapping table to avoid internal contradiction.

### Phase B: Split/Archive Oversized Pages

Each page analyzed individually based on content and growth pressure:

**B1: `v5-harness-reviewer-findings.md` (10,003B → ~5KB active + ~5KB archive)**
- Archive Rounds 1-2 findings (all resolved/fixed) to `v5-harness-reviewer-findings-archive-2026.md`
- Active page keeps Round 3 findings only + summary counts
- Cross-reference: active → archive via `[[v5-harness-reviewer-findings-archive-2026]]`

**B2: `v5-phase3-readiness.md` (10,635B → ~6KB active)**
- The stall triad (BUG-015/016/017) is resolved. Trim duplicated stall triad analysis (already in `v5-harness-known-bugs-archive-2026.md`).
- Also trim resolved items: P0 test gaps marked DONE (lines ~217-218), resolved code review findings, and completed blocker items.
- Keep: Phase 3 scope definition, remaining open gaps, sign-off status, and any unresolved action items.
- Rule: if an item has a DONE/RESOLVED marker, it leaves the active page.

**B3: `v5-harness-design-decisions.md` (10,554B → ~6KB active + ~5KB archive)**
- Split criterion: **resolved-vs-load-bearing**, not phase-origin. Several Phase 1 decisions (pending_mutations, task_id validation, sessions-never-talk) still govern active code.
- Archive decisions whose motivating concern is fully resolved and no longer constrains development to `v5-harness-design-decisions-archive-2026.md`
- Keep decisions that still govern active code patterns (regardless of when they were made)
- Cross-reference between active and archive

**B4: `v5-harness-roadmap.md` (9,635B → ~5KB active)**
- At 9,635B this is already under the 10KB architecture ceiling. Archiving is for information hygiene, not size compliance.
- Archive completed phase descriptions (Phase 1, 2, 2.5) with resolved items to `v5-harness-roadmap-archive-2026.md`
- Active page keeps Phase 3+ scope and timeline
- Cross-reference to archive for historical context
- **Note:** New archive pages are NOT added to index.md (per wiki-guide convention, line 83)

**B5: `wiki-guide.md` (9,314B + ~800B additions → requires split)**
- Phase A adds the WikiCategory mapping table (+300B net), Phase A2 adds staleness/tag/dedup content (+500B net). With only 686B of headroom, the guide will exceed its 10KB `pattern` ceiling.
- **Expected outcome (not contingency):** Extract the "Archive Rotation" and "When to Split a Page" detailed sections to a `wiki-operations.md` reference page (`category: reference`). This frees ~1.5KB, keeping the guide under ceiling with room for the new additions.
- The guide retains the decision tree, directory structure, page size table, frontmatter template, categories, tag conventions (with enforcement additions), cross-reference rules, quick rules, and relationship table.
- `wiki-operations.md` gets: archive rotation mechanics, split triggers and naming, and the de-duplication rule (Quick Rule #9). Cross-reference from guide → operations page.

**B6: `v5-harness-known-bugs-archive-2026.md` (9,410B — exempt)**
- This is already an archive page. Archive pages have a 12KB ceiling.
- 9,410B < 12KB. No action needed.

### Phase A2: Wiki Health Additions (wiki-guide.md)

**File:** `.omc/wiki/wiki-guide.md` — add three new subsections.

**A2.1: Staleness Policy** (add after "Page Size Policy" section)

```markdown
## Freshness Policy

Stale pages waste agent context on outdated information — worse than large pages with correct information.

- **`updated:` field is mandatory.** Every edit must bump the `updated:` date in frontmatter.
- **30-day staleness flag:** Pages with `updated:` >30 days old are flagged for review. The author (or any contributor) either confirms the content is still current (bump `updated:`) or revises it.
- **Enforcement:** `wiki_lint` staleness check (existing `stale` lint code). Manual audit until per-page staleness is automated.
- **Exemptions:** Archive pages (`reference` category with `archive` tag) and session logs are exempt from staleness flags — they are historical records.
```

**A2.2: Tag Hygiene** (merge into existing "Tag Conventions" section at `wiki-guide.md:121-131`, not a separate subsection — avoids internal duplication since the existing section already says "2-5 tags" and "prefer canonical")

Add these enforcement bullets to the existing Tag Conventions rules list:

```markdown
- **Minimum enforced:** Pages with <2 tags should be flagged during review. Manual audit until `wiki_lint` tag-count check ships (Phase 3+).
- **No single-use tags.** If a tag appears on only one page, it adds no search value. Either add it to more pages or remove it.
- **Tag audit:** During archive rotation or page splits, verify tags on both the active and archive pages.
```

**A2.3: De-duplication Rule** (add to "Quick Rules" or as a standalone subsection)

Add to Quick Rules:

```markdown
9. **Link, don't duplicate.** When the same information exists in multiple pages, designate one as the canonical source and have the others link to it via `[[page-name]]`. If you find duplicated content, remove the copy and add a cross-reference. The canonical source is the page whose primary topic matches the information (e.g., bug details belong in the bugs tracker, not in the readiness page).
```

### Phase C: Agent Role File Update

**Files:** `config/harness/agents/{architect,executor,reviewer}.md`

Add one line to each agent's existing context section:
- `config/harness/agents/architect.md` → append to `## Context Access`
- `config/harness/agents/executor.md` → append to `## Worktree Scope`
- `config/harness/agents/reviewer.md` → append to `## Context Access`

```
**Wiki:** Use `wiki_query` for targeted lookup. Only read full wiki pages when you need complete context of one topic.
```

This is a hint, not a protocol. One line, no multi-step procedure, no competing discovery paths. Do NOT create new sections in role files.

### Phase D: Verification

1. Run `wc -c .omc/wiki/*.md | sort -n` — verify all pages under their category ceiling
2. Run `wc -l .omc/wiki/index.md` — verify ≤30 lines
3. Grep for `[[` cross-references in all modified pages — verify all targets exist
4. Grep agent role files for wiki hint line — verify present in all 3

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Split pages drift out of sync with each other | Medium | Medium | Cross-references + wiki_lint orphan detection catch missing links. Active pages link to archives. |
| Archive pages grow beyond 12KB | Low | Low | Archive pages are rarely read in full. Raise ceiling or split by year if needed. |
| Agent role hint gets ignored by LLMs | Medium | Low | The hint is supplementary. The real enforcement is file size ceilings keeping pages cheap to read whole. |
| wiki-guide.md still exceeds 10KB after revision | Low | Medium | Extract archive/split operations to wiki-operations.md if needed. |
| New pages created without considering ceilings | Medium | Low | wiki_lint catches oversized pages. The policy is documented in the guide. |
| Phase 3+ tool enhancement never ships | Medium | Medium | Short-term policy is self-sufficient. Tool enhancement is a nice-to-have, not a dependency. |
| Staleness flags create noise on stable pages | Medium | Low | Exemptions for archive + session-log categories. Bumping `updated:` on confirmation is a 2-second edit. |
| Tag hygiene enforcement is manual until wiki_lint ships | Medium | Low | One-time audit during Phase B splits covers existing pages. New pages follow the guide. |
| De-duplication rule adds judgment calls | Low | Low | The canonical-source heuristic (primary topic match) resolves most ambiguity. Cross-references are cheap. |

## Verification Steps

1. **Size verification:** `wc -c .omc/wiki/*.md` — all pages under category ceiling
2. **Index verification:** `wc -l .omc/wiki/index.md` — ≤30 lines
3. **Cross-reference verification:** Grep for `[[` targets, verify all exist as files
4. **Role file verification:** Grep agent roles for wiki hint
5. **Content verification:** Read each split/archived page — confirm no information lost, just reorganized
6. **Lint verification:** Run `wiki_lint` — no new warnings introduced
7. **Tag verification:** Grep frontmatter `tags:` lines in all wiki pages — verify ≥2 tags each, prefer canonical
8. **Staleness verification:** Grep `updated:` dates — verify all active pages have today's date or a recent date
9. **Duplication spot-check:** Grep for stall triad content (BUG-015/016/017) — verify it exists in exactly one canonical location (bugs-archive), with cross-references elsewhere

## ADR

**Decision:** Enforce per-category byte ceilings via lint + archive/split for the 6 oversized pages. Add one-line wiki hint to agent roles. Defer wiki_read section parameter to Phase 3+.

**Drivers:** Observable cost proxy (file size), zero-maintenance enforcement (lint not behavioral), current tooling constraints (wiki_read has no section param).

**Alternatives considered:**
1. *SECTIONS blocks + 3-step behavioral protocol* — REJECTED by both architect and critic. Manual maintenance rot, physically impossible with current wiki_read API, competing navigation mechanism with wiki_query.
2. *No ceilings, rely on TOC + targeted reads* — REJECTED. Optimizes for best-case agent behavior. Agents will read whole files because wiki_read returns whole files. Ingestion cost remains hidden.
3. *Tool-level only (wiki_read section param)* — DEFERRED. Correct long-term but requires OMC plugin changes we can't ship now. Doesn't solve the 6 oversized pages today.
4. *Universal 12KB ceiling* — REJECTED. Too permissive for tracking pages (bugs, findings) that should stay lean for frequent reads. Per-category is more precise.

**Why chosen:** Option A is the only approach that (a) solves the 6 oversized pages now, (b) requires zero tooling investment, (c) uses proven patterns (archive rotation), and (d) keeps the observable cost proxy (file size) as the enforcement surface. It's also compatible with Option B as a future enhancement.

**Consequences:** More wiki files after splitting (~4 new archive pages). Cross-reference maintenance increases. Three new policy subsections in wiki-guide.md (staleness, tag hygiene, de-duplication). These are low-overhead conventions — staleness is a date bump, tags are 2-5 words, de-duplication is a link instead of a copy. More files with correct information is better than fewer files with stale information.

**Follow-ups:**
- Phase 3+: Evaluate wiki_read section parameter enhancement
- Phase 3+: Evaluate wiki_lint per-category maxPageSize support
- Phase 3+: Evaluate auto-generated section indexes at write time
- Phase 3+: Evaluate wiki_lint tag-count check (<2 tags flagged)
- Phase 3+: Evaluate wiki_lint single-use-tag detection

---

## Changelog

- **v1.0** — Planner draft (size-only policy)
- **v1.1** — Architect improvements: WikiCategory mapping, B2/B3 split criteria, ceiling tightening noted, archive index exclusion
- **v1.2** — Critic fixes: agent role file section names corrected per-file, Quick Rules #8 update
- **v2.0** — Expanded scope: added Principle 5 (fresh over large), staleness policy (A2.1), tag hygiene (A2.2), de-duplication rule (A2.3), AC-8 through AC-11, additional risks and verification steps.
- **v2.1** — Architect v2 advisory items: merged A2.2 into existing Tag Conventions (avoids internal duplication), reclassified B5 wiki-guide split as expected outcome (byte math confirms guide will exceed 10KB).
- **v2.2** — Critic v2 fixes: A2.2 tag enforcement reworded to "manual audit until automated" (was present-tense as if wiki_lint support exists), duplicated Consequences paragraph merged, AC-11 exemptions for index.md/log.md, phase ordering note added. **Consensus reached.**
