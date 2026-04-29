---
title: "Reference Screenshot Analysis — Conversational Discord Operator Style"
tags: ["reference", "discord", "operator-ux", "phase-e", "design-target", "clawhip", "gaebal-gajae"]
created: 2026-04-27T05:16:13.516Z
updated: 2026-04-27T05:16:13.516Z
sources: []
links: ["phase-e-agent-perspective-discord-rendering-intended-features.md", "ralplan-procedure-failure-modes-and-recommended-mitigations.md", "session-log-wave-e-commit-1-mechanical-extraction-2026-04-26.md", "v5-conversational-discord-operator.md"]
category: reference
confidence: medium
schemaVersion: 1
---

# Reference Screenshot Analysis — Conversational Discord Operator Style

# Reference Screenshot Analysis — Conversational Discord Operator Style

**Captured:** 2026-04-27 from operator-supplied reference screenshots (clawhip + gaebal-gajae bot dialogue from external project).

**Purpose:** distill operator's target Discord rendering style for harness-ts Phase E. Each design element below maps to one or more Phase E sub-phases (α/β/γ/δ). Future implementers reference this doc when extending notifier/templates/LLM prompts.

## Source Material

Three reference screenshots provided by operator (project: clawhip dev/rust pipeline):

1. **"Rust porting parity check" thread** — clawhip pings @gaebal-gajae for status; gaebal-gajae responds with structured nudge check; operator (Bellman/허예찬) replies with ❤️ reaction + brief follow-up + nudge to act now
2. **"Nudge Check" detail** — clawhip request + gaebal-gajae multi-paragraph response with bulleted Status, decision-stating closing prose
3. **"Recovery successful" detail** — gaebal-gajae reports recovery results with bullets, fenced command list with ✅ status badges, Immediate follow-up section, bot-to-bot reply quote card at bottom

## Distilled Design Elements

### Visible at first glance (screenshot 1+2)

1. **Distinct bot identities with avatars** — clawhip (red claw circle), gaebal-gajae (red eyeballs), operator (Bellman with CLAW badge). Each has its own webhook username + avatar. Operator instantly distinguishes who said what.

2. **Bot-to-bot dialogue** — clawhip @-mentions @gaebal-gajae explicitly; gaebal-gajae responds in next message. Forms a conversation thread. NOT just status spam from one bot.

3. **Multi-paragraph epistle structure**:
   ```
   {opener prose paragraph — 1-3 sentences setting context}

   {🦀 emoji prefix} **Section Header** — {UTC timestamp}

   {body prose paragraph leading into bullet list}
   - **Bold tag:** value
   - **Bold tag:** value

   {closing forward-looking prose paragraph}
   ```

4. **First-person declarative voice** — verbatim from reference:
   - "I'm checking the recovery session results right away."
   - "Okay. Recovery was successful and we have 1 more new commit. Merging it to dev/rust immediately."
   - "I'll proceed with re-injecting the next slice immediately."
   - "If there are no new commits at the next check, I will treat it as stale and clean up/replace the session."

   Pattern: bot speaks AS the agent role, not third-person status report. Decision-stating ("I'll proceed", "I will treat", "Merging it to dev/rust immediately"). Forward-looking commitments common.

5. **Section header pattern** — emoji + bold label + em-dash + UTC timestamp. Examples:
   - `🦀 **Nudge Check** — 20:40 UTC`
   - `🦀 **Nudge check** — 21:50 UTC`
   - `🦀 **Rust porting parity check!**`

   Acts as semantic anchor. Operator can scan archive for `🦀 Nudge` to find all nudge checks.

6. **Bullet list with bold inline tags**:
   ```
   - **tools**: AgentTool semantics reinforcement + .clawd-agents/ noise cleaning slice.
   - **cli**: /status + /config presentation/layout polish, OR actual /clear UX polish (whichever is faster).
   - **runtime**: Entering first transport/client scaffolding as the next step of MCP helper.
   ```

   Tag is bold short label (1-2 words); colon; description prose. Tags are dimensions of work being reported (tools/cli/runtime are subsystems).

### Visible in detail (screenshot 3)

7. **Multi-section single post** — gaebal-gajae's response combines:
   - Two opener sentences ("I'm checking the recovery session results right away. Okay. Recovery was successful and we have 1 more new commit. Merging it to dev/rust immediately.")
   - Section header ("🦀 Nudge check — 21:50 UTC")
   - Subhead with bullets ("Runtime recovery successful:" + bulleted commit + scope + Results sub-bullets)
   - Code-fence section with status badges (each command on own line with ✅ green checkbox)
   - Subhead with bullets ("Immediate follow-up:" + 2 bullets)
   - Closing prose paragraph ("The main active branch now involves...")

   ALL within ONE Discord message. Multi-paragraph + multi-section + nested bullets.

8. **Inline code highlighting** — backtick-wrapped tokens for technical identifiers:
   - SHAs (`5eeb7be`, `647b407`, `daf98cc`)
   - Branch names (`dev/rust`, `main`)
   - File paths (`rust/crates/runtime/src/mcp_stdio.rs`, `.clawd-agents/`)
   - Commands (`/status`, `/config`, `/clear`)
   - Module names (`tools`, `cli`, `runtime` — when in tag form)

   Operator scans for technical identifiers visually because they're code-styled.

9. **Fenced command list with ✅ inline badges**:
   ```
   o `cargo fmt --all` ✅
   o `cargo clippy -p runtime --all-targets -- -D warnings` ✅
   o `cargo test -p runtime` ✅
   ```

   Each command on own line, code-styled, with green checkmark badge after. Scannable verification result.

10. **Bot-to-bot reply quote card** — Bellman's "Yeah, but I'm still curious if a session is currently running full-out." replies to gaebal-gajae's prior nudge-check message. Discord renders quote-preview card at top:

    ```
    ↩  @gaebal-gajae  🦀 Nudge Check — 21:50 UTC  Runtime recover  MCP stdio runtime tests after the in-flight JSON-RPC slice • Repair scope: Limited...
    ```

    Quote includes source author + first line of replied-to content + truncated preview. Provides conversational context without re-quoting full message.

11. **Operator participates with reactions + replies** — `❤️ 1`, `👍 1`, `👀 1` reactions visible on bot messages. Operator reactions act as approval/dismissal signals. Operator also writes brief follow-ups ("Check PR every 10 minutes", "Damn it, do it right now. Why the hell are you waiting until the next check?").

12. **Bilingual rendering** — second screenshot shows "🦀 Nudge Check — 21:50 UTC" English bot output AND a Korean-language version "🦀 넋지 체크 — 21:50 UTC" (same content, Korean). Bot can render in operator's preferred language. **OUT OF SCOPE for harness-ts (English-only)** but documented for reference.

13. **NEW unread marker** — Discord renders red "NEW" indicator on right side of unread message boundary. Not bot-controlled; standard Discord UI.

## Mapping to Phase E sub-phases

| Reference element | Wave covering |
|---|---|
| 1. Distinct bot identities with webhooks | **Wave E-α** ✅ LANDED — identity diversification + per-event mapping |
| 2. Bot-to-bot dialogue (separate posts) | **Wave E-α** ✅ LANDED (identity attribution) + **Wave E-β** (reply chain card) |
| 3. Multi-paragraph epistle structure | **Wave E-α** partial (escalation/review_mandatory/task_done structured); **Wave E-γ (LLM voice)** for prose-rich body |
| 4. First-person declarative voice | **Wave E-γ (LLM voice)** — per-role system prompts enforce |
| 5. Section header (emoji + bold + UTC) | **Wave E-α** ✅ LANDED in epistle templates (header line per renderer) |
| 6. Bullet list with bold inline tags | **Wave E-α** partial (escalation Options/Context, review_mandatory findings); **Wave E-γ** extends to all narrative events |
| 7. Multi-section single post | **Wave E-γ (LLM voice)** — multi-section narrative requires LLM composition |
| 8. Inline code highlighting | Already supported via existing sanitize() backtick handling; templates use `${id}` style |
| 9. Fenced command list with ✅ badges | **Wave E-γ** OR **deferred** — requires structured command-result data on event payload (not currently captured) |
| 10. Bot-to-bot reply quote card | **Wave E-β** — Discord message_reference + WebhookSender plumbing |
| 11. Operator reactions as control surface | **DROPPED PER OPERATOR** (Wave E-α scope decision; bot mostly automated; reply-routing CW-3 + @-mention CW-4.5/E.8 cover interruption) |
| 12. Bilingual rendering | **OUT OF SCOPE** (English only) |
| 13. Periodic nudge_check pattern | **Wave E-δ** — scheduled introspection emitter |
| 14. Per-role @-mention routing | **Wave E-δ** — extends CW-4.5 mention routing per agent role |

## Gap Analysis vs Wave E-α (Current State)

What Wave E-α actually delivers (per session-log-wave-e-commit-1 + commit-2):
- ✅ Distinct identities (architect / reviewer / executor / orchestrator routing per event)
- ✅ Section headers with emoji + bold label + UTC timestamp (in epistle templates)
- ✅ Multi-line escalation_needed (Options + Context structure)
- ✅ Multi-line review_mandatory (Findings bullets via formatFindingForOps)
- ✅ Multi-line task_done structured (Summary + Files changed) WHEN summary/filesChanged populated by markPhaseSuccess
- ✅ Phase A pin :309 byte-equality preserved
- ✅ Architecture invariant enforced (no-discord-leak test)

What Wave E-α does NOT yet deliver vs reference:
- ❌ First-person prose voice (Wave E-γ — LLM)
- ❌ Multi-section narrative within single post (Wave E-γ — LLM)
- ❌ Forward-looking commitment paragraphs (Wave E-γ — LLM)
- ❌ Bot-to-bot reply quote cards (Wave E-β — message_reference)
- ❌ Periodic nudge_check pattern (Wave E-δ)
- ❌ Per-role @-mention routing (Wave E-δ)
- ❌ Avatar URLs per identity (deferred from Wave E-α; operator chose webhook-default avatars)
- ❌ Fenced command list with status badges (requires structured command-result events; not in current scope)

## Architectural Note: LLM is Necessary for Reference Voice

Looking at reference messages literally:
> "I'm checking the recovery session results right away. Okay. Recovery was successful and we have 1 more new commit. Merging it to dev/rust immediately."

This is NOT a template-rendered status. It's narrative prose generated FROM the structured data (recovery succeeded; commit count = 1; merge initiated). To generate this kind of prose deterministically would require dozens of branching templates per event. Reference style is achievable ONLY with LLM augmentation (Wave E-γ).

Wave E-α intentionally DOES NOT attempt this — deterministic templates only. Operator should expect Wave E-γ to deliver the conversational prose layer, with Wave E-α templates as the always-available deterministic fallback.

## Implementation Sequencing Recommendation

Per Wave E intended-features doc, recommended order: α → γ → β → δ. Rationale:
- **α first**: visible identity win, addresses primary operator complaint (single-bot collapse)
- **γ second**: LLM voice per role makes each bot speak in role-perspective (this is what makes reference target reachable)
- **β third**: reply chains tie role-attributed posts into conversation threads (after voice exists)
- **δ fourth**: nudge + per-role @-mention routing (UX polish on top of working pipeline)

Wave E-α currently shipped ✅. Reference target ~30% achieved (identity + skeleton + structured task_done). Wave E-γ is the next biggest jump in reference fidelity.

## Cross-refs

- [[phase-e-agent-perspective-discord-rendering-intended-features]] — full Phase E design intent
- [[ralplan-procedure-failure-modes-and-recommended-mitigations]] — RALPLAN consensus loop postmortem
- [[session-log-wave-e-commit-1-mechanical-extraction-2026-04-26]] — Wave E-α commit 1 session log
- [[v5-conversational-discord-operator]] — original conversational design doc
- `.omc/plans/2026-04-26-discord-wave-e-alpha.md` — Wave E-α plan
- `.omc/plans/2026-04-26-discord-conversational-output.md` — Phase A+B plan (LANDED)

