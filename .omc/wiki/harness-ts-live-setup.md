---
title: Harness-TS Live Setup Recipes
description: How to run live-bot-listen, live-project, live-discord-smoke against scratch repos. Includes minimal project.toml template.
category: pattern
tags: ["harness-ts", "setup", "live-test", "operational", "recipe"]
updated: 2026-04-27
---

# Harness-TS Live Setup Recipes

**Read this when running ANY `scripts/live-*.ts` script.** Each script has different config requirements; this page documents the minimum setup for each.

---

## Prerequisites (all live scripts)

`.env` at repo root (`/home/typhlupgrade/.local/share/ozy-bot-v3/.env`) must define:
```
DISCORD_BOT_TOKEN=...
DEV_CHANNEL=<snowflake>
AGENT_CHANNEL=<snowflake>           # optional; defaults to DEV_CHANNEL
ALERTS_CHANNEL=<snowflake>          # optional; defaults to DEV_CHANNEL
DISCORD_WEBHOOK_DEV=https://...     # required for webhook avatars; live-bot-listen MUST have all 3 webhook URLs
DISCORD_WEBHOOK_OPS=https://...
DISCORD_WEBHOOK_ESCALATION=https://...
ANTHROPIC_API_KEY=sk-ant-...        # required for Claude SDK
```

Sourcing pattern:
```bash
set -a && source ../.env && set +a   # if not auto-loaded
```

---

## Script Catalog (when to use what)

| Script | What it does | Requires | Cost |
|---|---|---|---|
| `live-discord-smoke.ts` | fires SMOKE_FIXTURES via DiscordNotifier; verifies rendering | `.env` only (DEV_CHANNEL + DISCORD_BOT_TOKEN) | Free (no LLM) |
| `live-bot-listen.ts` | full conversational pipeline: bot ingests `!task` / `!project` / NL from Discord; routes through orchestrator + sessions | `.env` + `config/harness/project.toml` (PRODUCTION mode) | $$$ — real Claude calls |
| `live-project.ts` | one-shot project run vs scratch repo; uses INLINE config; prints to stdout + Discord | `.env` only | $$ — real Claude calls |
| `live-project-3phase.ts` | 3-phase project stress test | `.env` only | $$$ |
| `live-project-arbitration.ts` | live arbitration roundtrip with stub Reviewer | `.env` only | $$$ |
| `live-project-architect-crash.ts` | architect crash-recovery roundtrip | `.env` only | $$$ |
| `live-project-mass-phase.ts` | 7-10 phase stress test | `.env` only | $$$$ |

---

## Recipe A — `live-discord-smoke.ts` (rendering smoke; cheapest)

```bash
cd harness-ts/
npx tsx scripts/live-discord-smoke.ts
```

Fires 16 Phase B fixtures + 2 Wave E-α audit fixtures = 18 messages across `#dev`/`#agents`/`#alerts`. Total drain ~40s.

Verifies: identity diversification (Wave E-α), epistle templates (E.3), Phase A pin :309, all event-route mappings.

NO LLM calls; pure transport-layer test.

---

## Recipe B — `live-project.ts` (one-shot project; inline config)

```bash
cd harness-ts/
npx tsx scripts/live-project.ts
```

Inline-builds config via `buildBaseConfig({...})` from `scripts/lib/scratch-repo.ts`. No `project.toml` needed.

Spawns ephemeral scratch repo at `/tmp/harness-live-proj-XXXXXX`, runs single project from declare → decompose → executor → review → merge. Tears down on exit.

Used for Wave C P2 (~$0.30/run typical, ~$0.75 for 3-phase stress).

---

## Recipe C — `live-bot-listen.ts` (full conversational pipeline)

**Requires `config/harness/project.toml` — production mode, no inline default.** Setup procedure:

### Step 1: Init scratch repo
```bash
SCRATCH=$(mktemp -d -t "harness-live-bot-XXXXXX")
cd "$SCRATCH"
git init -b main -q
git config user.email "live@harness.test"
git config user.name "live"
mkdir -p tasks worktrees sessions config/harness
cp /home/typhlupgrade/.local/share/ozy-bot-v3/harness-ts/config/harness/{architect,review,executor,intent-classifier,response-generator}-prompt.md config/harness/
echo "# scratch live-bot-listen" > README.md
cat > .gitignore <<'EOF'
tasks/
worktrees/
sessions/
state.json
projects.json
state.log.jsonl
.harness/
.omc/
EOF
git add -A && git commit -q -m "init"
echo "SCRATCH=$SCRATCH"
```

### Step 2: Write `project.toml` at `$SCRATCH/config/harness/project.toml`

Minimum viable TOML:
```toml
[project]
name = "live-bot-test"
root = "/tmp/harness-live-bot-XXXXXX"               # SET to $SCRATCH
task_dir = "/tmp/harness-live-bot-XXXXXX/tasks"
state_file = "/tmp/harness-live-bot-XXXXXX/state.json"
worktree_base = "/tmp/harness-live-bot-XXXXXX/worktrees"
session_dir = "/tmp/harness-live-bot-XXXXXX/sessions"

[pipeline]
poll_interval = 3
test_command = "true"                                # no-op for smoke; real project would use "npm test" etc
max_retries = 3
test_timeout = 180
escalation_timeout = 14400
retry_delay_ms = 5000
max_session_retries = 2
max_budget_usd = 2.0

[discord]
bot_token_env = "DISCORD_BOT_TOKEN"
dev_channel = "<from .env DEV_CHANNEL>"
ops_channel = "<from .env AGENT_CHANNEL or DEV_CHANNEL>"
escalation_channel = "<from .env ALERTS_CHANNEL or DEV_CHANNEL>"

[discord.agents.orchestrator]
name = "Harness"
avatar_url = ""

[discord.agents.architect]
name = "Architect"
avatar_url = ""

[discord.agents.reviewer]
name = "Reviewer"
avatar_url = ""

[discord.agents.executor]
name = "Executor"
avatar_url = ""

[reviewer]
max_budget_usd = 1.0
timeout_ms = 180000

[architect]
max_budget_usd = 4.0
prompt_path = "/tmp/harness-live-bot-XXXXXX/config/harness/architect-prompt.md"
```

**Important:** TOML cannot reference env vars. Plug actual values from `.env` into the file (use bash heredoc with `$DEV_CHANNEL` substitution).

### Step 3: Run daemon
```bash
cd /home/typhlupgrade/.local/share/ozy-bot-v3/harness-ts
set -a && source ../.env && set +a
HARNESS_CONFIG_PATH=$SCRATCH/config/harness/project.toml npx tsx scripts/live-bot-listen.ts
```

### Step 4: Operator interaction in Discord

In `#dev` channel (or whichever you configured as `dev_channel`):
- `!task <prompt>` — submits a standalone task
- `start a project to <description>` — natural language project declaration
- Reply to a bot's "Task picked up" message — relayOperatorInput fires, distills to plain text, forwards to architect/dialogue session
- `@<agent-name> <message>` — mention routing (CW-4.5)

### Step 5: Inspect results

State + logs at `$SCRATCH/`:
```bash
cat $SCRATCH/state.json | jq        # task records
cat $SCRATCH/state.log.jsonl        # event log
git -C $SCRATCH log --oneline       # merged commits
ls $SCRATCH/worktrees/              # active worktrees (cleaned up post-merge)
```

---

## Common Setup Pitfalls

### "config not found at .../config/harness/project.toml"
- `live-bot-listen.ts` requires production-mode TOML config. See Recipe C step 2.
- `live-project.ts` and friends inline-build config; do NOT need TOML.

### "DEV_CHANNEL missing"
- `.env` not loaded into shell. Use `set -a && source ../.env && set +a` before invoking.

### Bot posts but no avatar diversity
- Webhook URLs not set. Check `DISCORD_WEBHOOK_DEV/_OPS/_ESCALATION`. Without webhooks, fallback is BotSender (single bot identity). Provision via `scripts/provision-webhooks.ts`.
- DISCORD_AGENT_DEFAULTS at `src/lib/config.ts:209` has empty `avatar_url`. Wave E-β will populate dicebear placeholders; for now, webhook-default avatar is the rendering.

### Operator reply doesn't reach session
- MessageContext is in-memory; restart wipes the message-id → projectId map. Reply to a bot message before daemon restart.
- `relayOperatorInput` requires an ACTIVE Architect session for that projectId. Standalone `!task` (no projectId) doesn't have an architect session — operator interjection on standalone tasks falls through to fresh-task NL classifier.

### Tasks complete too fast for mid-flow interjection
- Simple `!task` tasks finish in ~20s. Operator reply arrives after task done.
- For interjection-mid-flow tests, use `start a project ...` natural language → architect decomposes → multi-phase execution = 60-180s window.

### "no live Architect session for {projectId}"
- Architect session ended (project completed/failed) before operator reply hit dispatcher. Re-issue request via `!project <name>` to spawn fresh architect.

---

## Cross-refs

- [[harness-ts-types-reference-source-of-truth]] — types referenced by config
- [[harness-ts-core-invariants]] — Discord-opaque-to-agents (relayOperatorInput contract)
- [[harness-ts-architecture]] — orchestrator + dispatcher + session pipeline
- `scripts/lib/scratch-repo.ts` — `initScratchRepo` + `buildBaseConfig` helper used by inline-config scripts
