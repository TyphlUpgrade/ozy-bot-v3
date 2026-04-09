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

## Page Naming Convention

**Harness pages:** `v5-{section}-{topic}.md`

Sections:
- **harness-** : Architecture, design, bugs, findings (core system)
- **[feature-name]-** : Roadmap feature (e.g., `v5-omc-agent-integration.md`)
- **phase[N]-readiness.md** : Progress checkpoints

**Trading bot pages** (future): `trading-bot-{topic}.md`

## Frontmatter Template

```yaml
---
title: Page Title (human-readable)
tags: [category, topic, subtopic]
category: [architecture|design|roadmap|quality|progress|pattern]
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

Categories:
- `architecture` : System structure, module design, data flow
- `design` : Trade-offs, decisions, rationale
- `roadmap` : Proposals, integration plans, features
- `quality` : Bugs, findings, audit results
- `progress` : Phase completions, readiness assessments
- `pattern` : Meta docs (like this guide)

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

## Wiki Sections at a Glance

### Harness (Development Pipeline)
Current state of the v5 agent orchestration system. Read before modifying signal routing, conductor logic, or agent roles.

### Roadmap
Upcoming features and integration proposals. Read before planning new capabilities.

### Quality
Bug tracker and review findings. Consult before touching a module with known issues.

### Progress
Phase readiness and sign-off. Answers "what's done and what's next?"

### Session Logs
Per-session ephemeral notes. Auto-generated; kept for 30 days.

## Quick Rules

1. **One concern per page** — Keep pages focused. Link liberally.
2. **Frontmatter required** — Use the template above. Category is mandatory.
3. **Link as you write** — When you mention another wiki topic, add `[[page-name]]`.
4. **Reference files, not snippets** — Use `file:line`, not copy-pasted code.
5. **Update the timestamp** — Change `updated:` field when you edit.
6. **Index is auto-built** — Don't manually edit `index.md`. Structure it via frontmatter.
