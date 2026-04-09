---
title: v5 Conversational Discord Operator
tags: [harness, discord, operator, communication]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# v5 Conversational Discord Operator

**Status**: Approved design, not yet scheduled  
**Depends on**: Phase 2 (escalation) complete  
**Reference**: [Conversational Discord Operator Plan](../../plans/2026-04-09-conversational-discord-operator.md)

## Overview

Transforms the v5 harness Discord interface from command-based (`!tell`, `!reply`) to conversational natural language, while preserving the star topology (agents have no direct Discord read access). Based on Claw Code's architecture: operators send NL messages → companion interprets → orchestrator routes → agents execute.

## Architecture: Three Independent Pieces

### 1. Agent Outbound (Status Posts)

Agents post status updates directly to Discord via `clawhip send` (no code changes).

| Aspect | Details |
|--------|---------|
| **How** | `clawhip send --channel "$AGENT_CHANNEL" --message "status text"` |
| **Access** | Agents already have Bash tool access; clawhip token available |
| **Rate limit** | `clawhip send --rate-limit 1/60s` flag or hook-based throttle |
| **Structured variant** | `StatusUpdate` signal type in `harness/lib/signals.py` for richer formatting |
| **Code location** | Agent role prompts only; update `config/agent_roles/*.md` |
| **Implementation cost** | Zero harness changes; prompt-only |

**Key constraint**: Write-only. Agents never read Discord responses; all inbound goes through FIFO mutation queue.

### 2. Natural Language Inbound Routing

Operator sends messages without `!` prefix; companion interprets and routes to correct agent.

| Aspect | Details |
|--------|---------|
| **Location** | `harness/discord_companion.py:_route_natural_language()` |
| **Size** | ~50 lines |
| **Single agent** | Route directly (no LLM call) |
| **Multiple agents** | Lightweight Claude classify to determine target |
| **Ambiguous** | Return "Who do you mean? Active agents: architect, executor" |
| **Scope** | Maps to existing commands only (`!tell`, `!reply`, `!status` equivalents) |
| **No scope creep** | Does NOT compose multi-step mutations; says "I don't understand" on ambiguity |
| **Output** | Routes as `[OPERATOR] message` via FIFO; appears in EventLog audit trail |

**Algorithm**:
```
if no command prefix AND message not recognized:
  if len(active_agents) == 1:
    target = active_agents[0]
  else:
    target = classify(message, active_agents)  # lightweight LLM call
  
  append_mutation(send(target, f"[OPERATOR] {message}"))
```

**Agent targeting rules**:
- Single active agent: direct route
- Multiple agents: lightweight classify (not full conversation)
- Ambiguous: echo clarification prompt

### 3. Escalation Dialogue (Future Sub-Phase)

Structured multi-turn conversation during Tier 2 escalation instead of single `!reply`.

| Stage | Flow |
|-------|------|
| **Escalation triggered** | Operator notified via Discord (existing) |
| **Operator responds** | Natural language message (no `!` prefix) |
| **Routing** | Piece 2 routes to blocked agent via FIFO: `[OPERATOR] message` |
| **Agent responds** | Posts reply to Discord via `clawhip send` (Piece 1) |
| **Operator continues** | Sends next message (repeats 3-5 times) |
| **Resolution** | Operator signals "okay, go with approach B" or reacts with checkmark |
| **Pipeline resumes** | Existing `_apply_reply` path; `clear_escalation` → resume |

**PipelineState changes**:
- New stage: `escalation_dialogue` (between `escalation_wait` and resume)
- New field: `dialogue_last_message_ts: str | None` (refreshed per exchange, suppresses re-notify logic)

**Resolution detection** (hybrid):
1. LLM interprets each operator message: is this mid-conversation or resolution?
2. If resolution detected: "Confirm resolution with checkmark or 'yes'"
3. Operator confirms → apply reply + clear escalation + resume
4. No confirm within 30s → stay in dialogue

**Key insight**: Two independent channels eliminate SessionOutputReader complexity:
- **Inbound**: FIFO mutation queue (orchestrator → agent)
- **Outbound**: `clawhip send` (agent → Discord directly)

## Implementation Order

```
1. Piece 1 (agent outbound)     ← no code changes, do anytime
   ↓
2. Piece 2 (NL inbound routing) ← ~50 lines in discord_companion
   ↓
3. Piece 3 (escalation dialogue) ← uses Pieces 1+2, PipelineState changes
```

Pieces 1 and 2 are independently useful; Piece 3 depends on both.

## Design Constraints (Load-Bearing)

| Constraint | Rationale |
|-----------|-----------|
| **Star topology** | Preserves central audit trail, escalation routing, pipeline state consistency, FIFO concurrency model |
| **Agent outbound is write-only** | Agents post status but never read Discord responses |
| **Harness remains project-agnostic** | No Claw Code-specific code in `harness/`; uses signal types + clawhip |
| **No agent Discord read access** | Security boundary; all inbound mediated by orchestrator |
| **Orchestrator is mediator** | All messages routed through FIFO, never agent-to-agent or agent-to-Discord direct |

## Risk Mitigation

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Agent spams Discord status posts | Low | Rate-limit flag on `clawhip send`, hook-based throttle |
| NL router misinterprets operator intent | Medium | Constrain to single-action output, "I don't understand" on ambiguity |
| Scope creep (conversation memory, multi-step) | Medium | Hard scope: stateless, one message → one action, no memory |
| Operator walks away during escalation dialogue | Low | `dialogue_last_message_ts` timeout, fall back to re-notify behavior |
| Two write paths to Discord (orchestrator + agent) | Low | Both use `clawhip send`; clawhip orders messages |
| Sensitive context leaked to Discord | Medium | Agent role prompts specify what to post; never raw state/credentials |

## What We Are NOT Building

- Agents reading from Discord (breaks star topology)
- Conversation memory in the NL router (stateless, each message interpreted independently)
- Multi-step compound mutations from single NL message
- Discord thread management (messages stay in channel)
- General-purpose chatbot (router maps to existing pipeline actions only)

## Prerequisites

- [ ] Phase 2 (escalation) committed and stable
- [ ] `clawhip send` supports `--channel` flag for targeted posting
- [ ] Verify agents can call `clawhip send` from Bash tool in their tmux sessions

## Acceptance Criteria

### Piece 1 (Agent Outbound)
- [ ] Agent posts status update → appears in Discord within 5s
- [ ] Agent cannot post more than 1 message per 60s (rate limit enforced)

### Piece 2 (NL Inbound Routing)
- [ ] `"tell the executor to focus on error handling"` → routed to executor
- [ ] Ambiguous message with multiple agents → gets clarification prompt
- [ ] Gibberish message → gets "I don't understand" (not a guess)
- [ ] All NL-routed messages appear in EventLog audit trail

### Piece 3 (Escalation Dialogue)
- [ ] Operator has 3+ turn conversation with blocked agent
- [ ] Pipeline stays paused throughout dialogue
- [ ] Timeout logic suppressed during active dialogue
- [ ] Operator confirms resolution → pipeline resumes correctly
- [ ] Operator walks away → timeout fires, falls back to re-notify

## Related Pages

- [[v5-harness-architecture]] — Orchestrator, FIFO sessions, stage pipeline
- [[v5-omc-agent-integration]] — Agent roles and integration patterns
- [[v5-harness-design-decisions]] — O_NONBLOCK, concurrency patterns
