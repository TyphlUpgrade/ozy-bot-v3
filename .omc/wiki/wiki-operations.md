---
title: Wiki Operations Reference
tags: [wiki, meta, guide, operations]
category: reference
created: 2026-04-09
updated: 2026-04-09
---

# Wiki Operations Reference

Detailed procedures for archive rotation, page splitting, and de-duplication. Extracted from [[wiki-guide]] to keep the guide under its `pattern` category ceiling.

---

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

---

## When to Split a Page

Split a page when either trigger fires:

1. **Byte size trigger** — page exceeds its ceiling. Archive resolved items first; if still over, split by topic.
2. **Topic drift** — 3+ H2 sections cover unrelated topics. Split by topic.

**Split naming:** `{parent-page}-{subtopic}.md`

Example: `v5-harness-known-bugs.md` → `v5-harness-known-bugs-agent-roles.md`

**After splitting:** add a "See also" section to the parent page linking to the split pages. `wiki_lint` orphan detection will catch any split pages that lose their cross-reference.

---

## De-duplication

When the same information exists in multiple pages, designate one as the canonical source and have the others link to it via `[[page-name]]`.

**Canonical source rule:** The canonical source is the page whose primary topic matches the information. Examples:
- Bug details belong in [[v5-harness-known-bugs]], not in readiness pages
- Design rationale belongs in [[v5-harness-design-decisions]], not in review findings
- Phase completion status belongs in [[v5-harness-roadmap]], not duplicated across pages

If you find duplicated content, remove the copy and add a cross-reference to the canonical page.

---

## Cross-References

- [[wiki-guide]] — Parent guide (decision tree, categories, frontmatter, quick rules)
