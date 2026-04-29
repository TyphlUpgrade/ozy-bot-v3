---
title: Wiki Curation Policy
description: What gets auto-captured vs curated. Rule for handling generic session-log stubs. autoCapture config knob.
category: pattern
tags: ["wiki", "policy", "curation", "auto-capture", "process"]
created: 2026-04-27
updated: 2026-04-27
---

# Wiki Curation Policy

**Why this page exists:** During Tier 1 wiki cleanup (2026-04-27), 206 generic auto-captured `session-log-*.md` stubs were deleted as noise. They contained zero curated content — just `Auto-captured session metadata. Session ID: <id>`. This policy prevents the noise from regrowing.

For the related architectural-mistakes prevention pages, see [[harness-ts-common-mistakes]] and [[harness-ts-core-invariants]].

---

## Auto-capture default behavior

The OMC SessionEnd hook (`session-hooks.ts:106`) writes a stub session-log page for every session unless `autoCapture: false` is set in `.omc-config.json`.

Default body content:
```
Auto-captured session metadata.
Session ID: <session-id>

Review and promote significant findings to curated wiki pages via `wiki_ingest`.
```

This default is fine in greenfield projects but creates noise in long-running ones — most sessions don't generate insights worth a wiki page.

## Project-level config (this repo)

`/home/typhlupgrade/.local/share/ozy-bot-v3/.omc-config.json` sets `wiki.autoCapture: false`. Generic session stubs are NOT written. Curation responsibility shifts to the operator (or curated session logs written manually via `wiki_add`).

To re-enable for a specific session, edit the config or pass `autoCapture: true` in OMC config override.

## When to write a session log manually

Write a curated session log via `wiki_add` (or `Write` tool directly) when ALL of these hold:

1. **Non-trivial work landed** — multi-commit feature, architectural decision, debugging postmortem
2. **Future-self utility** — the session uncovered a non-obvious pattern, tradeoff, or institutional knowledge
3. **Not already documented** — content doesn't duplicate [[harness-ts-architecture]], [[harness-ts-core-invariants]], or DRIFT_LOG entries

If only #1 holds (work landed but no insight), the commit message + git history are the canonical record. No wiki entry needed.

## Curated session log template

```yaml
---
title: Session Log: {Topic} ({YYYY-MM-DD})
description: One-line hook for index visibility
category: session-log
tags: [{wave-name}, {component}, ...]
updated: YYYY-MM-DD
---

# {Topic}

## Context
{Why this session, what triggered it}

## Decisions
{Non-obvious choices + WHY — links to commits}

## Mistakes / corrections
{What didn't work and why; cross-link to [[harness-ts-common-mistakes]] M-N if pattern repeats}

## Forward-looking
{Open questions, deferred items, follow-up tasks}

## Cross-refs
- [[related-page]]
- commit `abc1234`
```

## Anti-patterns to avoid

- **Stub session logs.** A page that only says "session happened" wastes index space.
- **Duplicating commit messages.** If the commit body has the rationale, link to it; don't paraphrase.
- **Speculative future-tense.** If the work hasn't shipped, it belongs in `.omc/plans/` (or [[harness-ts-plan-index]]), not wiki.
- **Restating types/architecture.** [[harness-ts-types-reference-source-of-truth]] is authoritative — link don't copy.

## Pruning rule

Before adding a new session log: scan `index.md` for similar topics. If a curated log already covers the area, EXTEND it rather than create a new one. Wiki pages decay fast when too many small ones accumulate.

Quarterly audit: any session-log page >30 days old without explicit cross-references from another active page is a candidate for deletion (unless it captures a load-bearing lesson).

## Archive subdirectory (NOT used)

Earlier proposals suggested an `archive/auto-capture/` subdirectory for stubs. Wiki tooling treats subdirectories as invisible (per [[wiki-guide]]: "all wiki pages live in the wiki root — no subdirectories"). Disabling autoCapture entirely is the simpler answer; if curated archives are ever needed, they live as flat files with `archive-{topic}-{YYYY}.md` naming.

## Cross-refs

- [[wiki-guide]] — overall wiki contribution conventions
- [[wiki-operations]] — archive rotation + split mechanics
- [[harness-ts-common-mistakes]] — session learnings repository (curated)
- [[harness-ts-core-invariants]] — load-bearing rules
