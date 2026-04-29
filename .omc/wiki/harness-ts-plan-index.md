---
title: Harness-TS Plan Index
description: Index of approved/historical plan files at .omc/plans/. One-liner per plan + status. Read this before opening a plan file.
category: reference
tags: ["harness-ts", "plans", "index", "navigation"]
created: 2026-04-27
updated: 2026-04-27
---

# Harness-TS Plan Index

Cross-reference for `.omc/plans/*.md` files. Plans are NOT auto-indexed by wiki tooling because they live outside `.omc/wiki/`. This page maintains a curated index keyed by topic + status.

**Status legend:**
- ✅ **LANDED** — implementation merged, plan superseded by code
- 🟡 **PARTIAL** — partially implemented; consult for context on remaining waves
- 🟠 **APPROVED-NOT-STARTED** — consensus reached, no code yet
- 🔵 **REFERENCE** — design rationale, not a delivery plan
- ⚫ **SUPERSEDED** — replaced by a newer plan

For raw file list: `ls .omc/plans/*.md`. For wiki navigation, prefer this page.

---

## Active / recent (read these first)

| Plan | Status | One-liner |
|---|---|---|
| `2026-04-26-discord-wave-e-alpha.md` | ✅ LANDED (2026-04-27) | Wave E-α: deterministic identity + epistle templates + markPhaseSuccess collapse. 33 accumulated requireds integrated post-RALPLAN halt at iter 4. See commits `66801b0` + `5bec3dc`. |
| `2026-04-26-discord-conversational-output.md` | ✅ LANDED (2026-04-26) | Phase A+B: multi-line markdown bodies, truncateBody 1900-cap, 16-fixture smoke matrix. Commits `e585c3c` / `3fd81a8` / `32ce0ea`. |
| `ralplan-harness-ts-three-tier-architect.md` | 🟡 PARTIAL | Master plan for Architect/Executor/Reviewer three-tier. Waves 1-3 + A + B landed; Wave B.5/4/C/6/D pending. SUPERSEDES original Phase 2B layout. |
| `ralplan-harness-ts-propose-then-commit.md` | ⚫ SUPERSEDED | Pre-three-tier plan for proposal-then-commit pattern. Replaced by three-tier-architect plan. |

## Historical pipeline (LANDED — kept for context)

| Plan | Status | One-liner |
|---|---|---|
| `ralplan-harness-ts-rewrite.md` | ✅ LANDED (Phase 0+1) | Original TypeScript rewrite plan. 6 modules + 9-state machine. Commit `2298ad1`. |
| `ralplan-harness-ts-rewrite-v2.md` | ⚫ SUPERSEDED | Iteration on rewrite plan. v3 supersedes. |
| `ralplan-harness-ts-rewrite-v3.md` | ✅ LANDED (Phase 0+1) | Final rewrite consensus. Implemented as Phase 0+1. |
| `ralplan-harness-ts-phase2a.md` | ✅ LANDED (Phase 2A) | Pipeline hardening: completion enrichment, escalation, retry, circuit breaker, graduated routing. 273 tests passing. |
| `ralplan-harness-ts-phase2b-3.md` | ⚫ SUPERSEDED | Phase 2B+3 combined plan. Replaced by three-tier-architect. |

## Domain-specific plans

| Plan | Status | One-liner |
|---|---|---|
| `ralplan-conversational-discord.md` | 🟡 PARTIAL | Discord conversational layer master plan. Phase A+B + Wave E-α landed; E-β/γ/δ pending. |
| `ralplan-llm-intent-classifier.md` | ✅ LANDED | LLM intent classifier for Discord NL routing. Wave 3 deliverable. |
| `ralplan-2026-04-09-escalation-dialogue.md` | 🔵 REFERENCE | Pre-TS escalation dialogue design. Informs Phase 3 dialogue agent + escalation Discord channel. |

## Wiki / process plans

| Plan | Status | One-liner |
|---|---|---|
| `ralplan-2026-04-09-wiki-organization-policy.md` | ✅ LANDED | Wiki structure policy: flat root, naming conventions, frontmatter requirements. Implemented in [[wiki-guide]]. |
| `ralplan-2026-04-09-wiki-size-policy.md` | ✅ LANDED | Per-category byte ceilings + archive rotation triggers. Implemented in [[wiki-guide]]. |
| `2026-04-09-claudemd-wiki-integration.md` | ✅ LANDED | CLAUDE.md ↔ wiki cross-reference policy. Migration cutoff 2026-04-14. |
| `2026-04-09-wiki-bridge-pass.md` | ✅ LANDED | Wiki migration bridge pass: root-doc → wiki content moves. |
| `2026-04-09-ozy-doc-migration.md` | ✅ LANDED | Ozymandias doc routing migration to wiki. |
| `2026-04-09-discord-integration-revisions.md` | 🔵 REFERENCE | Pre-TS Discord integration revisions (Python harness era). |
| `2026-04-08-unlock-omc-tools-for-agents.md` | 🔵 REFERENCE | Pre-TS plan for unlocking OMC tools to agent sessions. Locked under Architecture Invariant I-1. |

## Planning utilities

| File | Status | One-liner |
|---|---|---|
| `autopilot-impl.md` | 🔵 REFERENCE | Autopilot skill implementation notes. Cross-cuts harness; not a harness plan per se. |
| `open-questions.md` | 🟠 APPROVED-NOT-STARTED | Live register of unresolved design questions across waves. Updated as questions resolve. |

---

## Maintenance rule

When a new plan lands at `.omc/plans/`, add a row here within the same session. When a plan is superseded, mark ⚫ SUPERSEDED + name the replacement.

For full plan content, open the file directly. For status of waves *within* a plan, see [[harness-ts-phase-roadmap]] (delivery history) and [[harness-ts-wave-c-backlog]] (deferred work).

## Cross-refs

- [[harness-ts-phase-roadmap]] — phase-by-phase delivery history
- [[harness-ts-architecture]] — core architecture concepts
- [[harness-ts-wave-c-backlog]] — deferred items + P1/P2 follow-ups
- [[ralplan-procedure-failure-modes-and-recommended-mitigations]] — RALPLAN consensus failure modes
