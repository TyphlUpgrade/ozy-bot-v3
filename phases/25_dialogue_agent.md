# Phase 25: Strategy Dialogue Agent

Read `plans/2026-04-07-agentic-workflow-v4-omc-only.md` § Phase B.5 (lines ~1146-1190)
and § Pressure-Testing Protocol (lines ~789-827).

**Implementation dependency:** Phase 23 (Discord Companion), Phase 24 (Conductor — for
`state/signals/dialogue/` directory).

**Context:** The Dialogue agent is a Claude Code instance bridged to Discord `#strategy`,
giving the operator a collaborative thinking partner. It communicates via signal files —
writes responses to `state/signals/dialogue/response.json`, which the companion polls and
posts to Discord.

---

## What to Build

### 1. Dialogue role definition (`config/agent_roles/dialogue.md`)

New file: `config/agent_roles/dialogue.md`

The role prompt for the Strategy Dialogue agent. Includes:
- Role definition (collaborative thinking partner, not autonomous)
- Pressure-testing protocol with all three personas (Contrarian, Simplifier, Ontologist)
- Ambiguity scoring framework (6 dimensions, brownfield weights, threshold 0.20)
- Mandatory readiness gates (non-goals, decision boundaries)
- Output actions (plan files, task directives, NOTES.md updates)
- Signal file output convention (write response to `state/signals/dialogue/response.json`)

### 2. Discord companion dialogue bridge

Extend `tools/discord_companion.py` with dialogue bridging:
- Messages in the configured strategy channel are written to
  `state/signals/dialogue/inbound.json` (for the Dialogue agent to read)
- Companion polls `state/signals/dialogue/response.json`:
  - If found, read content, post to Discord (chunked for 2000-char limit), delete file
  - If not found within 120s, log a timeout warning
- New command: `!dialogue-status` — check if dialogue agent tmux session is alive

### 3. Dialogue session management

Document the tmux session setup for the dialogue agent:
```bash
# Launch dialogue agent in dedicated tmux pane:
tmux new-window -t ozymandias -n dialogue
tmux send-keys -t ozymandias:dialogue \
  "cd $PROJECT_ROOT && claude --profile dialogue" Enter
```

Session recovery: companion detects dead process, restarts. Conversation continuity is
best-effort — filesystem is the persistent memory.

---

## Tests to Write

Create `ozymandias/tests/test_dialogue_agent.py`:

### Role definition tests
- `test_dialogue_role_file_exists` — verify `config/agent_roles/dialogue.md` exists
- `test_dialogue_role_has_frontmatter` — verify YAML frontmatter with name, model, tier
- `test_dialogue_role_has_personas` — verify all three personas mentioned
- `test_dialogue_role_has_readiness_gates` — verify non-goals and decision boundaries

### Signal file convention tests
- `test_dialogue_response_schema` — verify response.json has expected fields (text, ts)
- `test_dialogue_inbound_schema` — verify inbound.json has expected fields (text, author, ts)

### Companion bridge tests
- `test_dialogue_status_command` — verify `!dialogue-status` returns status message
- `test_message_chunking` — verify messages >2000 chars are split correctly

---

## Done When

1. `config/agent_roles/dialogue.md` exists with complete role definition
2. Role includes pressure-testing protocol with all three personas
3. Role includes ambiguity scoring (6 dimensions, 0.20 threshold)
4. Role includes readiness gates (non-goals, decision boundaries)
5. Signal file convention documented (response.json, inbound.json)
6. Discord companion extended with dialogue bridge and `!dialogue-status`
7. Tests pass: `pytest ozymandias/tests/test_dialogue_agent.py`
