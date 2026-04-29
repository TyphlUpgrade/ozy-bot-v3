# RALPLAN-DR: CLAUDE.md Wiki Integration & Documentation Coverage

**Date:** 2026-04-09
**Mode:** Short (RALPLAN-DR)
**Status:** APPROVED (iteration 2) — Architect APPROVE + Critic APPROVE

---

## Context

The Ozy documentation migration to `.omc/wiki/` is complete. 13 `ozy-*.md` wiki pages exist, plus a routing hub (`ozy-doc-index.md`). However, CLAUDE.md — the primary session-start document — does not mention the wiki at all. Several documentation sources (`phases/`, the spec, `docs/operator-guide.md`, the wiki itself) are absent from the Reference Documents section. There are no maintenance standards governing when to use wiki vs CLAUDE.md vs other surfaces post-migration.

## Principles

1. **Single entry point, shallow depth.** A new session reads CLAUDE.md first. Every documentation surface must be reachable within one hop from Reference Documents — either directly or via a named routing hub.
2. **Conciseness over completeness in CLAUDE.md.** CLAUDE.md must stay lean. It should point to surfaces, not duplicate their content. The wiki and `ozy-doc-index.md` handle detailed routing.
3. **Frozen sources get references, not migrations.** `phases/`, `ozymandias_v3_spec_revised.md`, and `docs/Multilayer agentic workflow spec.pdf` are historical/foundational. They need pointers, not wiki copies.
4. **One canonical routing hub per domain.** `ozy-doc-index.md` is the Ozy wiki routing hub. It should cover all Ozy documentation — migrated and unmigrated alike.
5. **Post-cutoff clarity.** After 2026-04-14, the 3 root files (COMPLETED_PHASES.md, DRIFT_LOG.md, NOTES.md) are frozen. The maintenance rules must make the post-cutoff workflow unambiguous.

## Decision Drivers

1. **Session discoverability** — A new Claude session must learn the wiki exists and know how to navigate the full documentation landscape from CLAUDE.md alone. This is the primary gap.
2. **Minimal CLAUDE.md bloat** — CLAUDE.md is already ~215 lines. Adding content must be offset by not duplicating what routing hubs already cover. Net line growth should be small.
3. **Maintenance sustainability** — Rules for when-to-update-what must be simple enough that they're actually followed. Complex flowcharts get ignored.

## Viable Options

### Option A: Add wiki as a top-level Reference Document entry + expand ozy-doc-index.md

Add one new subsection to Reference Documents for `.omc/wiki/` (the wiki as a whole), update the Convention paragraph, add lightweight entries for `phases/` and the spec, and expand `ozy-doc-index.md` to cover unmigrated sources. Add a compact maintenance standards paragraph to the wiki entry.

**Pros:**
- Minimal CLAUDE.md growth (~20-25 net lines for wiki entry + phases + spec entries)
- Keeps detailed routing in ozy-doc-index.md where it belongs
- Single new convention paragraph covers post-cutoff workflow
- All 8 audit gaps addressed

**Cons:**
- Two files to edit (CLAUDE.md + ozy-doc-index.md)
- The wiki entry in Reference Documents must be general enough to cover both Ozy and v5-harness wiki pages, but Reference Documents is currently Ozy-focused

### Option B: Restructure Reference Documents into tiers (Session-Critical / Deep Reference / Historical)

Reorganize all 9+ entries into 3 tiers. Tier 1 (CLAUDE.md, wiki) is always-read. Tier 2 (plans/, docs/agentic-workflow.md) is consult-on-demand. Tier 3 (phases/, spec) is historical/foundational. Each tier gets a brief intro.

**Pros:**
- Clearer hierarchy — sessions know what to read vs what to consult
- Natural home for frozen sources (Tier 3)
- Scales better as documentation surfaces grow

**Cons:**
- Larger rewrite of Reference Documents section (~40-50 lines changed)
- Risk of over-engineering for a section that works today
- Tiering adds cognitive overhead — "which tier is this?" becomes a question
- Higher chance of introducing errors during the rewrite

### Recommendation

**Option A.** It addresses all 8 gaps with minimal structural disruption. The current flat list of Reference Documents works fine — it just has gaps. Option B solves a problem that doesn't exist yet (scaling) at the cost of a larger, riskier rewrite. If the list grows past ~10 entries in the future, tiering can be revisited then.

---

## Work Objectives

Update CLAUDE.md and ozy-doc-index.md to achieve full documentation coverage, so that any new session can discover and navigate all documentation surfaces — wiki, phases, spec, and active docs — from CLAUDE.md within one hop.

## Guardrails

**Must have:**
- Every documentation surface reachable from CLAUDE.md Reference Documents (directly or via routing hub)
- Wiki mentioned explicitly with read-before/update-when guidance
- Post-cutoff 2026-04-14 workflow unambiguous for the 3 frozen root files
- ozy-doc-index.md covers unmigrated sources (phases/, spec, plans/)
- Net CLAUDE.md growth under 30 lines

**Must NOT have:**
- Duplicated routing tables (ozy-doc-index.md content copied into CLAUDE.md)
- Changes to any frozen files (phases/, spec, COMPLETED_PHASES.md, DRIFT_LOG.md, NOTES.md)
- New wiki pages created for this task
- Removal of existing Reference Documents content (only additions and edits to existing entries)

---

## Task Flow

### Step 1: Update CLAUDE.md Convention paragraph (lines 165-168)

**What:** Expand the Convention paragraph to acknowledge `.omc/wiki/` as a documentation surface alongside `docs/`.

**File:** `CLAUDE.md` (lines 165-168)

**Changes:**
- Add one sentence acknowledging that `.omc/wiki/` is the persistent knowledge base for content that outgrows root-level docs or needs structured navigation. Mention `ozy-doc-index.md` as the Ozy routing hub.
- Keep existing `docs/` convention intact.

**Acceptance criteria:**
- The paragraph mentions both `docs/` and `.omc/wiki/` as documentation surfaces
- A reader understands when to use each (docs/ for standalone reference docs, wiki for persistent cross-session knowledge that benefits from tagging/search)

### Step 2: Add `.omc/wiki/` entry to Reference Documents

**What:** Add a new subsection after `plans/` and before `CLAUDE.md (this file)`. Use a compact domain-routing format — NOT the full read-before/update-when/does-not-belong template, since the wiki is a container surface, not an atomic updateable document.

**File:** `CLAUDE.md`

**Changes:** New compact subsection:
```
### `.omc/wiki/` — Persistent knowledge base
```
- One paragraph with domain routing: Ozy trading bot docs → `ozy-doc-index.md`, v5 harness docs → `index.md`. State that the wiki spans both domains.
- Single-line post-cutoff rule: "After 2026-04-14, new Ozy drift log / completed phase / concern entries go to wiki only."
- Do NOT use the read-before/update-when/does-not-belong template — those belong on atomic surfaces. Wiki update guidance lives in `wiki-guide.md`, not here.

**Rationale (Architect feedback):** The wiki is a container, not a single document. The full-format entry template produces guidance that's structurally ill-fitting — "update when" for a container would be too abstract to be actionable. The compact format routes by domain and states the post-cutoff rule, which is all CLAUDE.md needs.

**Acceptance criteria:**
- Wiki entry exists in Reference Documents with compact domain-routing format
- Entry routes Ozy work to `ozy-doc-index.md` and v5-harness work to `index.md`
- Post-cutoff workflow is stated (wiki is primary for new entries after 2026-04-14)
- Entry does NOT use read-before/update-when/does-not-belong bullets

### Step 3: Add lightweight entries for `phases/` and `ozymandias_v3_spec_revised.md`

**What:** Add two compact Reference Documents entries for the spec and the phases directory. These are read-only historical sources that CLAUDE.md already references in rules but doesn't list in Reference Documents.

**File:** `CLAUDE.md`

**Changes:** Two new subsections, compact (3-4 lines each, no bullet lists needed since these are read-only):
```
### `phases/` — Immutable phase specification files
### `ozymandias_v3_spec_revised.md` — Foundational system specification
```
- Each gets a one-line description + "Read before" + "Never modify" reminder
- No update-when needed (they are immutable)

**Acceptance criteria:**
- Both entries exist in Reference Documents
- Each states it is immutable / never modify
- phases/ entry mentions verified file count (run `ls phases/ | wc -l` before writing)
- Spec entry mentions DRIFT_LOG.md takes precedence where it contradicts

### Step 4: Expand ozy-doc-index.md to cover unmigrated sources

**What:** Add a new section to ozy-doc-index.md that lists documentation sources that remain outside the wiki — so the routing hub covers the full Ozy documentation landscape, not just migrated files.

**File:** `.omc/wiki/ozy-doc-index.md`

**Changes:** Add a section (after "Source File Routing" or at the end) titled something like "Non-Wiki Sources" or "Unmigrated Documentation":

| Source | Location | Mutable? | Notes |
|--------|----------|----------|-------|
| System spec | `ozymandias_v3_spec_revised.md` | Frozen | Foundational. DRIFT_LOG takes precedence. |
| Phase files | `phases/01-28 + context_compression_historical` | Frozen | Immutable build specs. Never modify. |
| Approved plans | `plans/YYYY-MM-DD-*.md` | Active | Pre-implementation design rationale. |
| Active conventions | `CLAUDE.md` | Active | Session-start rules. Updated each session. |
| Agentic workflow | `docs/agentic-workflow.md` | Active | Dev infrastructure, not trading bot. |
| Operator guide (original) | `docs/operator-guide.md` | Frozen | Migrated to wiki as ozy-operator-guide. |

Also add a Quick Reference row: "Understand the original system design" -> `ozymandias_v3_spec_revised.md`

**Acceptance criteria:**
- ozy-doc-index.md has a section covering non-wiki Ozy documentation sources
- phases/, spec, plans/, and CLAUDE.md are all listed
- Quick Reference table includes a spec entry
- Existing content unchanged

### Step 5: Update the "Six documents" count and handle the agentic workflow PDF

**What:** Update the introductory line of Reference Documents ("Six documents together form...") to reflect the actual count after Steps 2-3. Add a brief note about `docs/Multilayer agentic workflow spec.pdf` — either as a parenthetical in the `docs/agentic-workflow.md` entry or as a one-line mention.

**File:** `CLAUDE.md`

**Changes:**
- Count the actual `###` subsections in Reference Documents after Steps 2-3 are applied, then update the introductory line count to match (do not hardcode — verify by counting)
- In the `docs/agentic-workflow.md` entry, add a parenthetical noting the PDF exists as a companion/predecessor document

**Acceptance criteria:**
- Document count matches actual number of entries
- PDF is mentioned (even if just as a parenthetical) so it is discoverable
- No orphaned documentation surfaces remain

---

## Success Criteria

1. A new Claude session reading CLAUDE.md can discover the wiki, phases, and spec within Reference Documents
2. Following the wiki pointer leads to ozy-doc-index.md, which routes to all Ozy documentation — both wiki pages and non-wiki sources
3. Post-cutoff 2026-04-14 workflow is unambiguous: new drift log entries go to wiki, new completed phase entries go to wiki, new concerns go to wiki
4. CLAUDE.md net growth is under 30 lines
5. No frozen files modified
6. All audit gaps addressed — verified by checking each is reachable from CLAUDE.md Reference Documents:
   - `.omc/wiki/` (wiki surface)
   - `phases/` (immutable phase specs)
   - `ozymandias_v3_spec_revised.md` (foundational spec)
   - `docs/Multilayer agentic workflow spec.pdf` (mentioned as companion)
   - Post-cutoff 2026-04-14 workflow stated
   - Convention paragraph acknowledges wiki alongside docs/
   - `ozy-doc-index.md` Non-Wiki Sources table covers phases, spec, plans, CLAUDE.md, agentic-workflow.md
   - `docs/operator-guide.md` discoverable via ozy-doc-index.md routing (intentionally not in Reference Documents — migrated to wiki)

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| CLAUDE.md bloat — adding 3 entries makes it too long | Low | Medium | Keep new entries compact (3-5 lines each). Spec and phases entries are read-only stubs, not full subsections. Budget: <30 net lines. |
| Maintenance rules too complex to follow | Medium | High | Keep rules to one sentence per surface. Avoid flowcharts. The post-cutoff rule is binary: "after 2026-04-14, new entries go to wiki." |
| ozy-doc-index.md becomes a second CLAUDE.md | Low | Medium | ozy-doc-index.md lists locations, not rules. CLAUDE.md owns the conventions; ozy-doc-index.md owns the routing. |
| PDF reference adds confusion about which is canonical | Low | Low | State it as "companion/predecessor" — not an alternative to docs/agentic-workflow.md. |

---

## ADR: CLAUDE.md Wiki Integration

**Decision:** Add wiki, phases, and spec as Reference Document entries in CLAUDE.md (Option A — incremental additions). Expand ozy-doc-index.md to cover unmigrated sources.

**Drivers:** Session discoverability (primary), minimal bloat, maintenance sustainability.

**Alternatives considered:**
- Option B (tiered restructure): Rejected — solves a scaling problem that doesn't yet exist, larger rewrite surface, higher error risk.

**Why chosen:** Option A addresses all 8 audit gaps with the smallest change surface. The flat list of Reference Documents works well at 9 entries. Tiering can be revisited if the list grows past ~12.

**Consequences:**
- CLAUDE.md grows by ~20-25 lines (within budget)
- ozy-doc-index.md becomes the complete Ozy documentation router (migrated + unmigrated)
- Future documentation surfaces must be added to both CLAUDE.md Reference Documents and the relevant routing hub

**Follow-ups:**
- After 2026-04-14 cutoff passes, verify the 3 root files are actually frozen and the wiki workflow is being followed
- Consider tiered restructure if Reference Documents grows past 12 entries
- `docs/Multilayer agentic workflow spec.pdf` may warrant its own wiki page if the agentic workflow documentation evolves further
