---
title: v5 Conversational Discord Operator
tags: [harness, discord, operator, communication]
category: architecture
created: 2026-04-09
updated: 2026-04-09
---

# v5 Conversational Discord Operator

**Status**: All 3 pieces implemented (2026-04-09). Discord Integration Revisions (three-way classify, pipeline pause, NL tasks) also implemented.  
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
| **Entry point** | `harness/discord_companion.py:handle_raw_message(text)` — new method, replaces direct `handle_message` calls from Discord |
| **NL router** | `harness/discord_companion.py:_route_natural_language()` ~50 lines |
| **LLM classify** | `harness/lib/claude.py:classify_target()` — new function |
| **Single agent** | Route directly (no LLM call) |
| **Multiple agents** | Lightweight Claude classify to determine target |
| **Ambiguous** | Return "Who do you mean? Active agents: architect, executor" |
| **Scope** | Maps to existing commands only (`!tell`, `!reply`, `!status` equivalents) |
| **No scope creep** | Does NOT compose multi-step mutations; says "I don't understand" on ambiguity |
| **Output** | Routes as `[OPERATOR] message` via FIFO; mutation logged for EventLog audit trail |

#### Caller Contract: `handle_raw_message`

The Discord `on_message` handler (implemented in `start()` coroutine, `harness/discord_companion.py`) sends the **full raw message text** to:

```python
async def handle_raw_message(self, text: str) -> str | None:
    """Entry point for all Discord messages. Splits prefix commands vs NL."""
    text = text.strip()
    if text.startswith("!"):
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        args = parts[1] if len(parts) > 1 else ""
        return await self.handle_message(cmd, args)
    return await self._route_natural_language(text)
```

Existing `handle_message(cmd, args)` is unchanged — `handle_raw_message` is the new top-level dispatcher. All tests for `!`-prefix commands continue to call `handle_message` directly.

#### Active Agents Data Access

The companion needs to know which agents have live sessions. Pass a callable at construction:

```python
class DiscordCompanion:
    def __init__(self, config, pending_mutations, signal_reader,
                 active_agents_fn: Callable[[], list[str]] | None = None):
        self._active_agents_fn = active_agents_fn or (lambda: list(config.agents.keys()))
```

The orchestrator passes `lambda: list(session_mgr.sessions.keys())` when constructing the companion. This provides minimal coupling — the companion never holds a reference to `SessionManager`.

#### NL Routing Algorithm

```python
async def _route_natural_language(self, text: str) -> str | None:
    agents = self._active_agents_fn()
    if not agents:
        return "No active agents. Submit a task first."
    if len(agents) == 1:
        target = agents[0]
    else:
        target = await classify_target(text, agents, self.config)
        if target is None:
            return f"Who do you mean? Active agents: {', '.join(agents)}"
    self.pending_mutations.append(
        lambda s, sm, a=target, m=text: sm.send(a, f"[OPERATOR] {m}")
    )
    return f"Message routed to {target}."
```

#### `classify_target()` Specification

New function in `harness/lib/claude.py` using existing `_run_claude` infrastructure.

**Prerequisite**: Add optional `model: str | None = None` parameter to `_run_claude()`. When provided, append `["--model", model]` to the subprocess command. This enables haiku routing for cheap classify calls without affecting existing callers (classify, summarize, reformulate, document_task all continue using the default model).

| Aspect | Details |
|--------|---------|
| **Signature** | `async def classify_target(message: str, agents: list[str], config: ProjectConfig) -> str \| None` |
| **Model** | Haiku via `_run_claude(..., model="haiku")` |
| **System prompt** | `"You are a message router. Given the operator's message and the list of active agents, reply with exactly one agent name. If the intent is ambiguous, reply 'ambiguous'."` |
| **User prompt** | `f"Active agents: {', '.join(agents)}\n\nOperator message: {message}"` |
| **Timeout** | 10s — add `"classify_target": pipeline.get("classify_target_timeout", 10)` to `ProjectConfig.load()` timeouts dict at `pipeline.py:217-222` |
| **On failure/timeout** | Return `None` (triggers ambiguity prompt) |
| **On 'ambiguous' response** | Return `None` |
| **On unrecognized agent name** | Return `None` |
| **Gibberish handling** | Single agent: routes to that agent (agent handles gibberish). Multiple agents: classify returns `None` → ambiguity prompt. To add explicit "I don't understand", check if classify response is neither a valid agent name nor 'ambiguous' |

#### EventLog Audit Trail

NL-routed messages are logged via the mutation mechanism. The mutation lambda queued by `_route_natural_language` calls `sm.send()`, which is already visible in the orchestrator's event loop. Additionally, the orchestrator records an `"nl_routed"` event when applying NL mutations:

```python
await event_log.record("nl_routed", {"target": agent, "source": "discord_nl"})
```

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

#### Files Modified (Piece 3)

| File | Change |
|------|--------|
| `harness/lib/pipeline.py:239-242` | Add `"escalation_dialogue"` to `VALID_STAGES` frozenset |
| `harness/lib/pipeline.py:249+` | Add `dialogue_last_message_ts: str \| None = None` field to `PipelineState` |
| `harness/lib/pipeline.py:268-283` | Reset `dialogue_last_message_ts` in `activate()` |
| `harness/lib/pipeline.py:306-320` | Reset `dialogue_last_message_ts` in `clear_active()` |
| `harness/lib/pipeline.py:285-296` | Update `advance()` escalation tracking — `escalation_dialogue` preserves `escalation_started_ts` |
| `harness/lib/pipeline.py:322-341` | Include `dialogue_last_message_ts` in `shelve()` dict |
| `harness/lib/pipeline.py:344-367` | Restore `dialogue_last_message_ts` in `unshelve()` |
| `harness/orchestrator.py:524-540` | Add `case "escalation_dialogue"` to `match state.stage` dispatch |
| `harness/orchestrator.py:543` | Add `"escalation_dialogue"` to the shelving guard (currently `== "escalation_wait"` → `in ("escalation_wait", "escalation_dialogue")`) |
| `harness/lib/lifecycle.py:64-91` | Add `escalation_dialogue` handling to `reconcile()` |
| `harness/discord_companion.py:181,199` | Add `"escalation_dialogue"` to stage guard tuples in `_apply_reply` — both active-task branch (`line 181`) and shelved-task branch (`line 199`). Without this, operator resolution replies during dialogue are silently rejected |
| `harness/discord_companion.py` | NL router detects escalation context via `active_agents_fn` or a new `pipeline_stage_fn: Callable[[], str \| None]` callback. If stage is `escalation_dialogue`, route directly to `pre_escalation_agent` without classify |
| `harness/orchestrator.py` | New `handle_escalation_dialogue()` function for the `match` block. Checks `dialogue_last_message_ts` timeout — if exceeded, `advance("escalation_wait")` to fall back to re-notify behavior. Otherwise, no-op (dialogue is active, operator is engaged) |

#### Resolution detection (hybrid)

1. LLM interprets each operator message: is this mid-conversation or resolution?
2. If resolution detected: "Confirm resolution with checkmark or 'yes'"
3. Operator confirms → apply reply + clear escalation + resume
4. No confirm within 30s (wall-clock from last agent response) → stay in dialogue

**Resolution classify**: New `classify_resolution()` function in `harness/lib/claude.py`. Same `_run_claude` pattern as `classify_target`, haiku model via `model="haiku"`, 10s timeout (add `"classify_resolution"` key to `config.timeouts` with default 10). System prompt: "Is this operator message a resolution (decision made, go-ahead) or continuation (question, clarification, discussion)? Reply exactly: 'resolution' or 'continuation'." On failure/timeout: treat as continuation (safe default — keeps dialogue open).

**Integration point**: Resolution classification runs in the orchestrator's `handle_escalation_dialogue()` handler — NOT in the companion. The companion routes all messages during dialogue to the blocked agent via FIFO as `[OPERATOR] message`. The orchestrator intercepts the mutation result, runs `classify_resolution()`, and either (a) prompts for confirmation + applies reply on confirm, or (b) refreshes `dialogue_last_message_ts` and continues dialogue.

#### Shelving during dialogue

New tasks arriving during `escalation_dialogue` are handled identically to `escalation_wait` — the current task is shelved. On unshelve, `dialogue_last_message_ts` is restored but the escalation clock is reset (same pattern as existing unshelve).

**Key insight**: Two independent channels eliminate SessionOutputReader complexity:
- **Inbound**: FIFO mutation queue (orchestrator → agent)
- **Outbound**: `clawhip send` (agent → Discord directly)

## Implementation Order

```text
1. Piece 1 (agent outbound)     ✅ implemented (prompt-only, zero harness changes)
   ↓
2. Piece 2 (NL inbound routing) ✅ implemented (classify_target, handle_raw_message)
   ↓
3. Piece 3 (escalation dialogue) ✅ implemented (classify_resolution, dialogue state, circuit breaker)
   ↓
4. Discord Integration Revisions ✅ implemented (three-way classify, pipeline pause, NL tasks)
```

All pieces complete. Discord Integration Revisions added: control pre-filter, classify_intent, pipeline pause/resume, NL-initiated tasks via TaskSignal.

## Agent Presence Model

Agents and conductor have **presence** in Discord — not direct access. Clawhip manages all Discord I/O, but presents it as if agents are there:

- **Outbound**: Agents call `clawhip send` to post status updates. Clawhip formats and delivers. From the operator's view, "the architect said X."
- **Inbound**: Operator pings an agent by name (NL or `!tell`). Clawhip/companion routes to FIFO. From the operator's view, "I told the executor to do Y."
- **Conversational responsiveness**: Agents appear responsive and present. Operator gets feedback, can direct work, sees progress — without agents having any Discord awareness.

This is a **mediated presence** pattern: clawhip is the proxy that creates the illusion of agents being in the channel. Star topology preserved — agents never see Discord directly.

## Design Constraints (Load-Bearing)

| Constraint | Rationale |
|-----------|-----------:|
| **Star topology** | Preserves central audit trail, escalation routing, pipeline state consistency, FIFO concurrency model |
| **Agent outbound is write-only** | Agents post status but never read Discord responses |
| **Mediated presence** | Clawhip proxies agent/conductor presence in Discord for operator clarity — agents unaware |
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

- [x] Phase 2 (escalation) committed and stable
- [ ] `clawhip send` supports `--channel` flag for targeted posting — verify with `clawhip send --help` before Piece 1
- [ ] Verify agents can call `clawhip send` from Bash tool in their tmux sessions
- [x] Discord `on_message` handler exists and calls `handle_raw_message(text)` — `start()` coroutine added to `harness/discord_companion.py`, wired into orchestrator via `asyncio.create_task()`

**Channel routing**: Agents post to the channel matching their role as defined in `AgentDef.discord_channel` (default: `"dev-agents"`). Override per-agent in `config/agent_roles/*.md` prompt or future `sessions.toml`.

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
