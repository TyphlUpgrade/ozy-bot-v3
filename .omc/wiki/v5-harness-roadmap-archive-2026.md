---
title: v5 Harness Roadmap — Archive 2026
tags: [harness, roadmap, archive]
category: reference
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Roadmap — Archive 2026

Archived sections from [[v5-harness-roadmap]]. These phases are COMPLETE.

---

## Phase 2.5: Stall Triad (Prerequisite for Phase 3)

Three bugs that compound into permanent pipeline hangs. Fixed before Phase 3 entry.

| Bug | File | Severity | Fix | Effort |
|-----|------|----------|-----|--------|
| BUG-015 | `orchestrator.py:258-260` | High | Deleted escalation signal → silent stall. When `read_escalation()` returns `None`, check `escalation_started_ts` age. If exceeds `2 * escalation_timeout`, force-resume with warning. | ~10 lines |
| BUG-016 | `lifecycle.py:64-86` | Medium | Crash during `escalation_tier1` → no recovery. Add `escalation_tier1` case to `reconcile()`, re-send escalation question or promote to Tier 2. | ~15 lines |
| BUG-017 | `orchestrator.py:208-247` | Medium | No timeout on Tier 1 wait. `handle_escalation_tier1` polls indefinitely. Add `tier1_timeout` check against `escalation_started_ts`, auto-promote to Tier 2 if exceeded. | ~20 lines |

**Test Gaps (P0 Blocking — now resolved):**

| Gap | Tests Needed | Coverage |
|-----|--------------|----------|
| DiscordCompanion.handle_message() | Valid/malformed commands, permission checks, dispatch | Zero → 40 lines |
| _handle_caveman() dispatcher | Caveman parsing + command routing | Zero → 40 lines |

**Completion Notes:**
- All stall triad bugs fixed (BUG-015, BUG-016, BUG-017)
- P0 Discord tests added (DiscordCompanion.handle_message, _handle_caveman dispatcher)
- Phase 2 fix batch validated (handle_escalation_wait, _apply_reply tests passing)

---

## Cross-References

- [[v5-harness-roadmap]] — Active roadmap (Phases 3–5 and unscheduled proposals)
- [[v5-harness-known-bugs]] — Bug tracking
