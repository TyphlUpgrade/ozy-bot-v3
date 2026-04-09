# Conversational Discord Operator Interaction

**Date**: 2026-04-09
**Status**: Approved design, not yet scheduled
**Depends on**: Phase 2 (escalation) complete
**Reference**: Claw Code (rusty-claude-code) Discord interaction model

## Problem

The current Discord interface is command-based (`!tell`, `!reply`, `!caveman`, `!status`).
Operators must memorize command syntax and can't interact naturally with agents. The Claw Code
project demonstrates that a conversational Discord experience is achievable without breaking
orchestrator-as-mediator topology — their agents emit structured events that get formatted into
conversational messages, and operator natural language gets interpreted into structured commands
by a separate layer.

## Key Architectural Insight

Claw Code agents do NOT have direct Discord access. Their three-part system:
```
Human (Discord) → OmX (NL interpreter) → Claw Code (executes) → LaneEvents (JSON) → Clawhip (formats) → Discord
```
The "conversational feel" is a UI layer on top of the same star topology we already have.
Our architecture needs two additions, not a redesign.

## Design Constraints

- **Star topology is load-bearing.** All inbound operator messages route through the orchestrator.
  Agents NEVER read from Discord. This preserves: central audit trail, escalation routing,
  pipeline state consistency, mutation queue concurrency model.
- **Agent outbound is write-only.** Agents can post to Discord via `clawhip send` (Bash tool).
  They never receive responses through this channel — all inbound goes through FIFO.
- **Harness remains project-agnostic.** No Claw Code-specific code in `harness/`.

## Implementation: Three Independent Pieces

### Piece 1: Agent Outbound Status (trivial, no code changes)

**What**: Agents post status updates directly to Discord via `clawhip send`.

**How**: Update agent role prompts in `config/agent_roles/*.md` to include:
```
Post status updates to Discord: clawhip send --channel "$AGENT_CHANNEL" --message "your status"
```

Agents already have Bash access. Clawhip already has the Discord token. Zero harness code changes.

**Rate limiting**: Add a `clawhip send --rate-limit 1/60s` flag or a hook-based throttle to
prevent agent spam.

**Structured variant**: Define a `status_update` signal type in `harness/lib/signals.py`:
```python
@dataclass
class StatusUpdate:
    agent: str
    stage: str
    summary: str
    progress: str | None = None  # e.g. "3/5 units complete"
    detail: str | None = None
```
Add a clawhip route that watches for these and formats them nicely for Discord. This gives
richer formatting than raw `clawhip send` messages.

### Piece 2: Natural Language Inbound Routing (small, ~50 lines)

**What**: Operator sends natural language in Discord (no `!` prefix). The companion interprets
it and routes to the correct agent via the existing mutation queue.

**Where**: Fallback path in `harness/discord_companion.py:handle_message()` when no command
prefix is detected.

**How**:
```python
async def handle_message(self, cmd: str, args: str) -> str | None:
    # ... existing !tell, !reply, !caveman, !status handlers ...

    # Check project-specific commands
    if self._project_handler and cmd in self._project_commands:
        ...

    # No command prefix — natural language routing
    if not cmd.startswith("!"):
        return await self._route_natural_language(f"{cmd} {args}".strip())

    return None

async def _route_natural_language(self, message: str) -> str:
    """Interpret operator natural language as a pipeline action."""
    # Determine target agent
    if len(active_agents) == 1:
        target = active_agents[0]
    else:
        target = await classify_target(message, active_agents)

    # Route as operator feedback (same as !tell)
    self.pending_mutations.append(
        lambda s, sm, a=target, m=message: sm.send(a, f"[OPERATOR] {m}")
    )
    return f"Routed to {target}."
```

**Agent targeting rules**:
- Single active agent: route to it (no LLM call needed)
- Multiple active agents: lightweight Claude classify call to determine target
- Ambiguous: echo back "Who do you mean? Active agents: architect, executor"

**Scope guard**: The NL router maps to existing commands only (`!tell`, `!reply`, `!status`
equivalents). It does NOT compose multi-step mutations. If the LLM can't map to one existing
action, it says "I don't understand" rather than guessing. This is the boundary that prevents
the 50-line feature from becoming 500.

### Piece 3: Escalation Dialogue (medium, separate sub-phase)

**What**: During Tier 2 escalation, operator has a multi-turn conversation with the blocked
agent through Discord instead of a single `!reply`.

**How it works with existing architecture**:
1. Escalation hits Discord as before (Tier 2 notification via `notify()`)
2. Operator replies naturally (no `!reply` prefix needed — Piece 2 handles routing)
3. Companion routes message to blocked agent via FIFO: `[OPERATOR] message`
4. Agent responds and posts its reply to Discord via `clawhip send` (Piece 1)
5. Operator sees response in Discord, continues conversation
6. Operator signals resolution: "okay, go with approach B" or reacts with checkmark
7. Companion detects resolution intent (hybrid: LLM suggests, operator confirms)
8. Pipeline resumes via existing `_apply_reply` path

**Key insight**: No SessionOutputReader needed. The agent handles its own outbound via
`clawhip send`. The relay problem disappears because we're using two independent channels:
FIFO for inbound (orchestrator → agent), `clawhip send` for outbound (agent → Discord).

**PipelineState changes**:
- New stage: `escalation_dialogue` (between `escalation_wait` and resume)
- `dialogue_last_message_ts: str | None` — refreshed on each exchange, suppresses
  timeout/re-notify logic in `handle_escalation_wait`
- Transition: `escalation_wait` → operator sends first conversational message →
  `escalation_dialogue` → operator confirms resolution → resume original stage

**Resolution detection**: Hybrid approach.
- LLM interprets each operator message: is this mid-conversation or resolution?
- If resolution detected: "I'll resume the pipeline with instruction: 'go with approach B'. Confirm? (react with checkmark or say 'yes')"
- Operator confirms → `_apply_reply` + `clear_escalation` + resume
- No confirm within 30s → stay in dialogue

## Implementation Order

```
Piece 1 (agent outbound)     ← no code changes, prompt-only, do anytime
    ↓
Piece 2 (NL inbound routing) ← ~50 lines in discord_companion, standalone
    ↓
Piece 3 (escalation dialogue) ← uses Pieces 1+2, needs PipelineState changes
```

Pieces 1 and 2 are independently useful. Piece 3 depends on both.

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Agent spams Discord with status posts | Low | Rate-limit flag on `clawhip send`, hook throttle |
| NL router misinterprets operator intent | Medium | Constrain to single-mutation output, "I don't understand" on ambiguity |
| NL router scope creep (conversation memory, multi-step) | Medium | Hard scope: stateless, one message → one action, no memory |
| Escalation dialogue — operator walks away mid-conversation | Low | `dialogue_last_message_ts` timeout, fall back to `escalation_wait` behavior |
| Two write paths to Discord (orchestrator + agent) | Low | Both use `clawhip send` — ordering is clawhip's responsibility |
| Sensitive context leaking to Discord via agent posts | Medium | Agent role prompts specify what to post; never post raw state/credentials |

## What We Are NOT Building

- Agents reading from Discord (breaks star topology)
- Conversation memory in the NL router (each message interpreted independently)
- Multi-step compound mutations from a single NL message
- Discord thread management (messages stay in the channel, not threaded)
- A general-purpose chatbot (the router maps to existing pipeline actions only)

## Prerequisites

- [ ] Phase 2 (escalation) committed and stable
- [ ] `clawhip send` supports `--channel` flag for targeted posting
- [ ] Verify agents can call `clawhip send` from Bash tool in their tmux sessions

## Acceptance Criteria

**Piece 1**:
- [ ] Agent posts a status update, it appears in Discord within 5s
- [ ] Agent cannot post more than 1 message per 60s (rate limit)

**Piece 2**:
- [ ] Operator sends "tell the executor to focus on error handling" → routed to executor
- [ ] Operator sends ambiguous message with multiple agents active → gets clarification prompt
- [ ] Operator sends gibberish → gets "I don't understand" (not a guess)
- [ ] All NL-routed messages appear in EventLog audit trail

**Piece 3**:
- [ ] Operator has 3+ turn conversation with blocked agent during escalation
- [ ] Pipeline stays paused throughout dialogue
- [ ] Timeout logic suppressed during active dialogue
- [ ] Operator confirms resolution → pipeline resumes correctly
- [ ] Operator walks away → timeout eventually fires, falls back to re-notify behavior
