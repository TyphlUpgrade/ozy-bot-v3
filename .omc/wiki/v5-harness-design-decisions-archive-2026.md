---
title: v5 Harness Design Decisions — Archive 2026
tags: [harness, design, archive]
category: reference
created: 2026-04-09
updated: 2026-04-09
---

# v5 Harness Design Decisions — Archive 2026

Resolved decisions archived from [[v5-harness-design-decisions]]. These concerns are settled — implementation is in place and no longer constrains active development.

## Per-level directive construction (append, not template)

**Decision**: Build per-level directives by appending `**Active level: {level}.**` to the full SKILL.md, rather than using `template.replace("CAVEMAN_LEVEL", level)`.

**Why**: SKILL.md contains all levels inline — there's no single `CAVEMAN_LEVEL` placeholder. Appending an activation line is simpler and works regardless of SKILL.md internal structure. The plan's template approach assumed a placeholder that doesn't exist in the actual file.

**Status**: Implemented. Approach is settled; no ongoing decision.

## FIFO permissions 0o600

**Decision**: `os.mkfifo(path, mode=0o600)` — owner-only read/write.

**Why**: FIFOs live in `/tmp/harness-sessions`, a shared directory. Default umask (0o022) would create 0o644 FIFOs, allowing any local user to write to them — injecting arbitrary messages into Claude sessions. 0o600 restricts to the owning user.

**Status**: Implemented and closed. Permission is set in `harness/sessions.py`; no further action needed.

## Phase 2/3 types pulled forward

**Decision**: Implemented `EscalationRequest`, `ArchitectResolution`, `EscalationReply` dataclasses and `reformulate()`/`document_task()` functions even though they were Phase 2/3 scope.

**Why**: The dataclass schemas cost nothing to define early and establishing them now means Phase 2 doesn't need to modify `signals.py`. Similarly, `claude.py` functions are self-contained — implementing them early exercises the subprocess wrapper pattern without adding risk.

**Status**: Phase 2 is complete. The types exist and are in active use. This was a correct early decision; no further action.

## Future-proofing: what to build when (architect + critic consensus)

Reviewed by architect and critic agents prior to Phase 2. Captured here for historical reference; most items are resolved or incorporated.

### Before Phase 2 (genuine Phase 2 prerequisites) — all completed

1. **EventLog (JSONL append-only)** — implemented.
2. **Configurable CLI binary** — `claude_binary` config field added to `ProjectConfig`.
3. **`escalation_started_ts` on PipelineState** — added.

### Recommended with Phase 2 (cheap, no urgency) — resolved or deferred

4. **Notifier protocol** — bare function sufficient; no open action.
5. **`role` field on Session** — added.
6. **Document message prefix conventions** — documented.
7. **Comment on TaskSignal.priority sort gap** — added.

### Before Phase 3 (don't build yet) — design deferred, requirements still not concrete

8. **Extract TaskState from PipelineState** — still deferred to Phase 3/5. Not resolved; see [[v5-harness-design-decisions]] Latent Issues for the serialization note.
9. **Stage transition graph as constant** — deferred to Phase 5 configurable pipelines.
10. **`tokens_in`/`tokens_out` on Session** — deferred to Phase 3 session rotation.

## Cross-References

- [[v5-harness-design-decisions]] — active load-bearing decisions
- [[v5-harness-architecture]] — module overview and pipeline flow
- [[v5-harness-known-bugs]] — deferred bugs
