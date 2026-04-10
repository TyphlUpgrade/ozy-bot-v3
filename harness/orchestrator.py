"""v5 Harness Orchestrator — async main loop with stage dispatch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import signal
import sys
from datetime import datetime, UTC
from pathlib import Path

import discord_companion as dc
from lib import claude, escalation, lifecycle
from lib.events import EventLog
from lib.pipeline import PipelineState, ProjectConfig
from lib.sessions import SessionManager, compress_startup_files
from lib.signals import SignalReader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("harness.orchestrator")

# Cache escalation requests across poll cycles to avoid TOCTOU re-reads.
# check_for_escalation stashes here; handle_escalation_tier1 pops.
_escalation_cache: dict[str, "EscalationRequest"] = {}


# ---------- Stage Handlers ----------


async def classify_task(state: PipelineState, session_mgr: SessionManager,
                        config: ProjectConfig, event_log: EventLog) -> None:
    """Classify task complexity, route to architect or executor."""
    result = await claude.classify(state.task_description or state.active_task or "", config)
    if result == "complex":
        state.advance("architect", "architect")
        await event_log.record("stage_advanced", {"task": state.active_task, "from": "classify", "to": "architect"})
        # Send task to architect for planning
        await session_mgr.send("architect", f"[TASK] Plan this task: {state.active_task}")
    else:
        state.advance("executor", "executor")
        await event_log.record("stage_advanced", {"task": state.active_task, "from": "classify", "to": "executor"})


async def check_stage(state: PipelineState, signal_reader: SignalReader,
                      session_mgr: SessionManager, config: ProjectConfig,
                      stage: str, event_log: EventLog) -> None:
    """Poll for stage completion signal."""
    result = await signal_reader.check_stage_complete(stage, state.active_task or "")
    if result is not None:
        # Collect architect plan for wiki documentation
        if stage == "architect" and isinstance(result, dict):
            state.plan_summary = result.get("output", "") or result.get("plan", "")
        next_stages = {"architect": "executor", "executor": "reviewer", "reviewer": "merge"}
        next_stage = next_stages.get(stage, "merge")
        state.advance(next_stage, next_stage if next_stage != "merge" else None)
        await event_log.record("stage_advanced", {"task": state.active_task, "from": stage, "to": next_stage})
        logger.info("Stage %s complete for %s → %s", stage, state.active_task, next_stage)
        # Send summarized context to next agent on architect→executor and executor→reviewer transitions
        if stage in ("architect", "executor") and next_stage in ("executor", "reviewer"):
            output = result.get("output", "") if isinstance(result, dict) else ""
            if output:
                summary = await claude.summarize(output, config)
                if summary:
                    next_agent = next_stage
                    await session_mgr.send(
                        next_agent,
                        f"[CONTEXT] Previous stage ({stage}) summary:\n{summary}\n\n"
                        f"[TASK] Continue work on: {state.active_task}",
                    )
                else:
                    logger.debug("summarize returned None for stage %s — proceeding without context", stage)
            else:
                logger.debug("No output in stage %s signal — proceeding without context", stage)


async def check_reviewer(state: PipelineState, signal_reader: SignalReader,
                         session_mgr: SessionManager, config: ProjectConfig,
                         event_log: EventLog) -> None:
    """Check reviewer verdict — approve or trigger retry."""
    result = await signal_reader.check_stage_complete("reviewer", state.active_task or "")
    if result is None:
        return
    verdict = result.get("verdict", "").lower()
    if verdict == "approve" or verdict == "approved":
        state.review_verdict = result.get("feedback", "") or "approved"
        state.advance("merge")
        await event_log.record("stage_advanced", {"task": state.active_task, "from": "reviewer", "to": "merge", "verdict": "approved"})
        logger.info("Reviewer approved %s", state.active_task)
    else:
        if state.retry_count >= config.max_retries:
            if not config.auto_escalate_on_max_retries:
                logger.error("Task %s failed after %d retries", state.active_task, state.retry_count)
                _escalation_cache.pop(state.active_task or "", None)
                state.clear_active()
                return

            # Guard against overwriting existing escalation
            existing = await signal_reader.read_escalation(state.active_task or "")
            if existing:
                logger.info("Task %s already has escalation — skipping auto-escalate",
                            state.active_task)
                _escalation_cache.pop(state.active_task or "", None)
                state.clear_active()
                return

            feedback = result.get("feedback", "Reviewer rejected — no specific feedback.")
            logger.warning("Task %s failed after %d retries — auto-escalating",
                           state.active_task, state.retry_count)

            from lib.signals import EscalationRequest, write_signal
            esc = EscalationRequest(
                task_id=state.active_task or "",
                agent=state.stage_agent or "executor",
                stage=state.stage or "reviewer",
                severity="blocking",
                category="persistent_failure",
                question=f"Task failed {state.retry_count} reviewer rounds. Last rejection: {feedback[:200]}",
                options=["replan_approach", "simplify_scope", "escalate_to_operator"],
                context=f"retry_count={state.retry_count}, last_feedback={feedback[:500]}",
                retry_count=0,  # BUG-023 fix: use 0 so circuit breaker can route Tier 1 first
            )
            write_signal(config.signal_dir / "escalation", f"{state.active_task}.json", esc)

            # Route through circuit breaker — resume at executor, not reviewer
            # BUG-024 fix: reviewer rejected, so resuming at reviewer loops forever
            state.pre_escalation_stage = "executor"
            state.pre_escalation_agent = "executor"
            tier = _route_with_circuit_breaker(state, esc, config)
            if tier == "tier1":
                _escalation_cache[esc.task_id] = esc
                msg = escalation.format_escalation_for_architect(esc)
                await session_mgr.send("architect", msg)
                state.advance("escalation_tier1", "architect")
            else:
                summary = escalation.format_tier2_notification(esc)
                await notify("blocked", esc.agent, summary)
                state.advance("escalation_wait")

            await event_log.record("auto_escalated", {
                "task": state.active_task,
                "retry_count": state.retry_count,
                "tier": 1 if tier == "tier1" else 2,
                "reason": "max_retries_exhausted",
                "circuit_breaker_count": state.tier1_escalation_count,
            })
            return
        # Reformulate and retry
        feedback = result.get("feedback", "Reviewer rejected — no specific feedback.")
        reformulated = await claude.reformulate(feedback, state.active_task or "", config)
        if reformulated:
            state.retry_count += 1
            state.advance("executor", "executor")
            await session_mgr.send("executor", reformulated)
            await event_log.record("stage_advanced", {"task": state.active_task, "from": "reviewer", "to": "executor", "retry": state.retry_count})
            logger.info("Retry %d for %s", state.retry_count, state.active_task)
        else:
            # Reformulate failed — send raw feedback as fallback
            state.retry_count += 1
            state.advance("executor", "executor")
            await session_mgr.send("executor", f"[RETRY] {feedback}")
            await event_log.record("stage_advanced", {"task": state.active_task, "from": "reviewer", "to": "executor", "retry": state.retry_count})


async def do_merge(state: PipelineState, config: ProjectConfig, event_log: EventLog) -> None:
    """Merge worktree, run tests, revert on failure."""
    if state.worktree is None:
        state.advance("wiki")
        return
    cwd = str(config.project_root)
    branch = f"task/{state.active_task}"
    proc = await asyncio.create_subprocess_exec(
        "git", "merge", "--no-ff", branch,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("Merge failed for %s: %s", state.active_task, stderr.decode()[:200])
        abort = await asyncio.create_subprocess_exec(
            "git", "merge", "--abort", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await abort.communicate()
        _escalation_cache.pop(state.active_task or "", None)
        state.clear_active()
        return
    # Run tests
    if not config.test_command.strip():
        logger.warning("Empty test_command — skipping tests for %s", state.active_task)
        state.advance("wiki")
        await event_log.record("stage_advanced", {"task": state.active_task, "from": "merge", "to": "wiki"})
        return
    proc = await asyncio.create_subprocess_exec(
        *shlex.split(config.test_command),
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        logger.error("Tests timed out for %s — killing and reverting", state.active_task)
        proc.kill()
        await proc.wait()
        revert = await asyncio.create_subprocess_exec(
            "git", "revert", "--no-edit", "-m", "1", "HEAD", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await revert.communicate()
        _escalation_cache.pop(state.active_task or "", None)
        state.clear_active()
        return
    if proc.returncode != 0:
        logger.error("Tests failed after merge for %s — reverting", state.active_task)
        revert = await asyncio.create_subprocess_exec(
            "git", "revert", "--no-edit", "-m", "1", "HEAD", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await revert.communicate()
        _escalation_cache.pop(state.active_task or "", None)
        state.clear_active()
        return
    # Capture diff stat for wiki documentation
    diff_proc = await asyncio.create_subprocess_exec(
        "git", "diff", "--stat", "HEAD~1",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    diff_out, _ = await diff_proc.communicate()
    state.diff_stat = diff_out.decode().strip() if diff_proc.returncode == 0 else None
    state.advance("wiki")
    await event_log.record("stage_advanced", {"task": state.active_task, "from": "merge", "to": "wiki"})
    logger.info("Merge + tests passed for %s", state.active_task)


async def do_wiki(state: PipelineState, config: ProjectConfig, event_log: EventLog,
                  session_mgr: SessionManager | None = None) -> None:
    """Document task via claude -p + /wiki."""
    task_id = state.active_task or ""
    description = state.task_description or task_id
    success = await claude.document_task(
        task_id=task_id,
        description=description,
        plan_summary=state.plan_summary or "(no architect plan)",
        diff_stat=state.diff_stat or "(no file changes)",
        review_verdict=state.review_verdict or "(no review)",
        config=config,
    )
    if not success:
        logger.warning("Wiki documentation failed for %s — continuing", task_id)
        await event_log.record("wiki_failed", {"task": task_id})
    await event_log.record("task_completed", {"task": task_id})
    await notify("task_completed", "orchestrator", f"Task {task_id} completed: {description}")
    _escalation_cache.pop(task_id, None)
    state.clear_active()
    # Unshelve next task if any are waiting
    unshelved = state.unshelve()
    if unshelved:
        logger.info("Unshelved task %s (was in %s)", unshelved["task_id"], unshelved.get("stage"))
        await event_log.record("task_unshelved", {"task_id": unshelved["task_id"], "stage": unshelved.get("stage")})
        # Inject stored operator reply if escalation was resolved while shelved.
        # NOTE: The executor session at this point may belong to the just-completed
        # task, not the unshelved one. If the session is dead or wrong, the reply is
        # silently skipped — the main loop will need to relaunch an executor for the
        # unshelved task. The operator reply content is still on the unshelved dict
        # for future re-injection if needed.
        pending_reply = unshelved.get("pending_operator_reply")
        if pending_reply and session_mgr and state.stage_agent:
            if state.stage_agent in session_mgr.sessions:
                await session_mgr.send(state.stage_agent, pending_reply)
                logger.info("Injected pending operator reply for unshelved task %s", state.active_task)
            else:
                logger.warning("Executor session not available for unshelved task %s — "
                               "pending reply will need re-injection after session launch",
                               state.active_task)
    logger.info("Task %s complete", task_id)


# ---------- Escalation Handlers ----------


def _route_with_circuit_breaker(state: PipelineState, esc: "EscalationRequest",
                                 config: ProjectConfig) -> str:
    """Route escalation through standard tiers, with circuit breaker override.

    After max_tier1_escalations architect attempts for the same task,
    skip architect and go straight to operator (Tier 2).
    """
    tier = escalation.route_escalation(esc)
    if tier == "tier1":
        if state.tier1_escalation_count >= config.max_tier1_escalations:
            logger.warning("Circuit breaker: Tier 1 exhausted for %s (%d attempts) — forcing Tier 2",
                           state.active_task, state.tier1_escalation_count)
            return "tier2"
        state.tier1_escalation_count += 1
    return tier


async def check_for_escalation(state: PipelineState, signal_reader: SignalReader,
                               session_mgr: SessionManager, config: ProjectConfig,
                               event_log: EventLog) -> bool:
    """Check for new escalation signal during an active agent stage.

    Returns True if an escalation was found and state transitioned, False otherwise.
    Only called when state.stage is an agent stage (architect, executor, reviewer).
    """
    esc = await signal_reader.read_escalation(state.active_task or "")
    if esc is None:
        return False

    # Informational escalations: FYI to operator, no pipeline pause (spec line 301)
    if esc.severity == "informational":
        summary = escalation.format_tier2_notification(esc)
        await notify("info", esc.agent, summary)
        signal_reader.clear_escalation(state.active_task or "")
        await event_log.record("escalation_informational", {
            "task": state.active_task, "category": esc.category, "agent": esc.agent,
        })
        logger.info("Informational escalation from %s — FYI sent, no pause", esc.agent)
        return False

    tier = _route_with_circuit_breaker(state, esc, config)
    # Store pre-escalation context for resume routing
    state.pre_escalation_stage = state.stage
    state.pre_escalation_agent = state.stage_agent
    # Clear any pending stage completion signal to prevent spurious advancement on resume
    if state.stage:
        signal_reader.clear_stage_signal(state.stage, state.active_task or "")

    if tier == "tier1":
        _escalation_cache[esc.task_id] = esc  # cache for handle_escalation_tier1
        msg = escalation.format_escalation_for_architect(esc)
        await session_mgr.send("architect", msg)
        state.advance("escalation_tier1", "architect")
        await event_log.record("escalation_routed", {
            "task": state.active_task, "category": esc.category,
            "tier": 1, "agent": esc.agent,
        })
        logger.info("Escalation from %s routed to architect (Tier 1)", esc.agent)
    else:
        summary = escalation.format_tier2_notification(esc)
        await notify("blocked", esc.agent, summary)
        state.advance("escalation_wait")
        await event_log.record("escalation_routed", {
            "task": state.active_task, "category": esc.category,
            "tier": 2, "agent": esc.agent,
        })
        logger.info("Escalation from %s routed to operator (Tier 2)", esc.agent)
    return True


async def handle_escalation_tier1(state: PipelineState, signal_reader: SignalReader,
                                  session_mgr: SessionManager, config: ProjectConfig,
                                  event_log: EventLog) -> None:
    """Poll for architect resolution of a Tier 1 escalation."""
    resolution = await signal_reader.read_architect_resolution(state.active_task or "")
    if resolution is None:
        started_ts = state.escalation_started_ts
        tier1_timeout = config.tier1_timeout
        if started_ts:
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(started_ts)).total_seconds()
            if elapsed > tier1_timeout:
                logger.warning("Tier 1 timeout for %s after %.0fs — promoting to Tier 2",
                               state.active_task, elapsed)
                task_id = state.active_task or ""
                esc = _escalation_cache.pop(task_id, None) or await signal_reader.read_escalation(task_id)
                summary = f"TIMEOUT: Architect did not resolve within {tier1_timeout}s"
                if esc:
                    summary = escalation.format_tier2_notification(esc, summary)
                await notify("blocked", state.pre_escalation_agent or "unknown", summary)
                state.advance("escalation_wait")
                await event_log.record("escalation_promoted", {
                    "task": state.active_task, "from_tier": 1, "to_tier": 2,
                    "reason": "tier1_timeout", "elapsed_seconds": elapsed,
                })
        else:
            logger.warning("Tier 1 escalation for %s has no started_ts — cannot check timeout",
                           state.active_task)
        return

    if escalation.should_promote(resolution):
        # Promote to Tier 2 — architect couldn't resolve or has low confidence
        task_id = state.active_task or ""
        esc = _escalation_cache.pop(task_id, None) or await signal_reader.read_escalation(task_id)
        architect_assessment = f"{resolution.resolution} (confidence: {resolution.confidence}): {resolution.reasoning}"
        summary = escalation.format_tier2_notification(esc, architect_assessment) if esc else architect_assessment
        await notify("blocked", state.pre_escalation_agent or "unknown", summary)
        state.advance("escalation_wait")
        await event_log.record("escalation_promoted", {
            "task": state.active_task, "from_tier": 1, "to_tier": 2,
            "reason": resolution.resolution, "confidence": resolution.confidence,
        })
        logger.info("Escalation for %s promoted to Tier 2 (confidence: %s)",
                     state.active_task, resolution.confidence)
    else:
        # Resolved — inject resolution into the original agent and resume
        _escalation_cache.pop(state.active_task or "", None)
        original_agent = state.pre_escalation_agent
        if original_agent and original_agent in session_mgr.sessions:
            await session_mgr.send(
                original_agent,
                f"[ESCALATION RESOLVED] {resolution.resolution}\nReasoning: {resolution.reasoning}",
            )
        state.resume_from_escalation()
        signal_reader.clear_escalation(state.active_task or "")
        await event_log.record("escalation_resolved", {
            "task": state.active_task, "resolver": "architect",
            "confidence": resolution.confidence,
        })
        logger.info("Escalation for %s resolved by architect (confidence: %s)",
                     state.active_task, resolution.confidence)


async def handle_escalation_wait(state: PipelineState, signal_reader: SignalReader,
                                 config: ProjectConfig, event_log: EventLog) -> None:
    """Handle Tier 2 escalation wait — timeout and re-notify logic.

    Operator replies are handled via discord_companion !reply → _apply_reply mutation.
    This function only handles timeout behaviors: re-notify for blocking, auto-proceed
    for advisory.
    """
    esc = await signal_reader.read_escalation(state.active_task or "")
    if esc is None:
        started_ts = state.escalation_started_ts
        if started_ts:
            elapsed = (datetime.now(UTC) - datetime.fromisoformat(started_ts)).total_seconds()
            if elapsed > 2 * config.escalation_timeout:
                logger.warning("Escalation signal missing for %s after %.0fs — force-resuming",
                               state.active_task, elapsed)
                state.resume_from_escalation()
                await event_log.record("escalation_force_resumed", {
                    "task": state.active_task, "reason": "signal_missing", "elapsed_seconds": elapsed,
                })
        else:
            logger.warning("Escalation wait for %s has no started_ts — cannot check timeout",
                           state.active_task)
        return

    started_ts = state.escalation_started_ts

    # Advisory escalations auto-proceed after timeout
    if escalation.should_auto_proceed(esc, started_ts, config.escalation_timeout):
        state.resume_from_escalation()
        signal_reader.clear_escalation(state.active_task or "")
        await event_log.record("escalation_auto_proceeded", {
            "task": state.active_task, "severity": esc.severity,
        })
        logger.info("Advisory escalation for %s auto-proceeded after timeout", state.active_task)
        return

    # Blocking escalations re-notify at interval
    if esc.severity == "blocking" and escalation.should_renotify(
        started_ts, config.escalation_timeout, state.last_renotify_ts
    ):
        summary = escalation.format_tier2_notification(esc)
        await notify("blocked", esc.agent, f"REMINDER: {summary}")
        state.last_renotify_ts = datetime.now(UTC).isoformat()
        logger.info("Re-notified operator for blocking escalation on %s", state.active_task)


async def handle_escalation_dialogue(state: PipelineState, config: ProjectConfig,
                                      event_log: EventLog) -> None:
    """Handle active escalation dialogue — classify messages, detect resolution.

    Dialogue timeout is separate from max_stage_minutes — do not add
    'escalation_dialogue' to that dict. This handler manages its own timeout
    via dialogue_last_message_ts.
    """
    # Timeout: no operator message in dialogue_timeout seconds -> fall back to wait
    ts = state.dialogue_last_message_ts or state.escalation_started_ts or state.stage_started_ts
    if ts:
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds()
        if elapsed > config.dialogue_timeout:
            logger.warning("Escalation dialogue timed out for %s after %.0fs",
                           state.active_task, elapsed)
            state.advance("escalation_wait")
            state.dialogue_last_message_ts = None
            state.dialogue_last_message = None
            state.dialogue_pending_confirmation = False
            await event_log.record("dialogue_timeout", {"task": state.active_task})
            return

    # Classify new operator message (if any, and no pending confirmation)
    if state.dialogue_last_message and not state.dialogue_pending_confirmation:
        intent = await claude.classify_resolution(state.dialogue_last_message, config)
        if intent == "resolution":
            state.dialogue_pending_confirmation = True
            msg = state.dialogue_last_message
            await notify("dialogue_confirm", state.pre_escalation_agent or "unknown",
                        f'Resolution detected: "{msg[:100]}". '
                        f'Confirm: say "yes" or `!reply {state.active_task} <instruction>`')
            await event_log.record("dialogue_resolution_detected", {
                "task": state.active_task, "message_preview": msg[:200],
            })
        state.dialogue_last_message = None  # consumed — classify once per message


# ---------- Worktree Management ----------


async def create_worktree(task_id: str, config: ProjectConfig) -> Path | None:
    """Create a git worktree for a task. Returns the worktree path or None on failure."""
    worktree = config.worktree_base / task_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = f"task/{task_id}"
    proc = await asyncio.create_subprocess_exec(
        "git", "worktree", "add", "-b", branch, str(worktree),
        cwd=str(config.project_root),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("Failed to create worktree for %s: %s", task_id, stderr.decode()[:200])
        return None
    return worktree


# ---------- Notify ----------


async def notify(event: str, agent: str, summary: str) -> None:
    """Post lifecycle event to Discord via clawhip or signal file fallback."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "clawhip", "agent", event,
            "--name", agent, "--summary", summary,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            logger.warning("clawhip agent failed: %s", stderr.decode()[:100])
    except (asyncio.TimeoutError, FileNotFoundError):
        logger.debug("clawhip notify unavailable for %s/%s", event, agent)


# ---------- Stage Timeout ----------


def _check_stage_timeout(state: PipelineState, config: ProjectConfig) -> bool:
    """Return True if current stage has exceeded its wall-clock timeout."""
    if not state.stage_started_ts or state.stage not in config.max_stage_minutes:
        return False
    elapsed = (datetime.now(UTC) - datetime.fromisoformat(state.stage_started_ts)).total_seconds()
    max_seconds = config.max_stage_minutes[state.stage] * 60
    return elapsed > max_seconds


# ---------- Main Loop ----------


async def main_loop(config: ProjectConfig) -> None:
    state = PipelineState.load(config.state_file)
    session_mgr = SessionManager(config.session_dir, config)
    signal_reader = SignalReader(config.signal_dir)
    event_log = EventLog(config.project_root / "harness_events.jsonl")

    # Compress CLAUDE.md and role files at startup (idempotent, graceful fallback)
    await compress_startup_files(config)

    # Launch persistent sessions (architect, reviewer)
    for name, agent_def in config.agents.items():
        if agent_def.auto_start and agent_def.lifecycle == "persistent":
            await session_mgr.launch(name, agent_def)

    # Reconcile after crash
    async def notify_escalation(esc):
        await notify("blocked", esc.agent, f"ESCALATION (re-sent): {esc.question}")

    await lifecycle.reconcile(state, session_mgr, signal_reader, notify_escalation)

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def _on_signal(sig, _frame):
        logger.info("Received %s — shutting down", signal.Signals(sig).name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Discord companion — routes inbound messages + NL to agents
    pending_mutations: list = []
    companion = dc.DiscordCompanion(
        config=config,
        pending_mutations=pending_mutations,
        signal_reader=signal_reader,
        active_agents_fn=lambda: list(session_mgr.sessions.keys()),
        pipeline_stage_fn=lambda: (state.stage, state.pre_escalation_agent),
        pipeline_paused_fn=lambda: state.paused,
        shutdown_event=shutdown_event,
    )
    channel_ids = [int(ch) for ch in [
        os.environ.get("AGENT_CHANNEL", ""),
        os.environ.get("DEV_CHANNEL", ""),
    ] if ch]
    discord_task = asyncio.create_task(dc.start(companion, channel_ids))

    def _log_discord_error(task):
        if not task.cancelled() and task.exception():
            logger.error("Discord companion crashed: %s", task.exception())

    discord_task.add_done_callback(_log_discord_error)
    logger.info("Discord companion task started")

    await notify("started", "orchestrator", "Harness started")

    while not shutdown_event.is_set():
        # 0. Apply pending mutations from Discord (concurrency-safe)
        for mutation in pending_mutations:
            await mutation(state, session_mgr)
        pending_mutations.clear()

        # 1. Check pipeline progress (skip when paused — health checks still run)
        if state.paused:
            pass  # mutations applied above; health checks + heartbeat below
        elif state.active_task:
            # Check for new escalation from active agent stages
            if state.stage in ("architect", "executor", "reviewer"):
                if await check_for_escalation(state, signal_reader, session_mgr, config, event_log):
                    state.heartbeat()
                    state.save(config.state_file)
                    await asyncio.sleep(config.poll_interval)
                    continue

            # Check wall-clock stage timeout
            if _check_stage_timeout(state, config):
                elapsed = (
                    datetime.now(UTC) - datetime.fromisoformat(state.stage_started_ts)
                ).total_seconds()
                logger.warning(
                    "Stage timeout: stage=%s task=%s elapsed=%.0fs",
                    state.stage, state.active_task, elapsed,
                )
                agent_name = state.stage_agent
                if agent_name and agent_name in session_mgr.sessions:
                    await session_mgr.kill(agent_name)
                await event_log.record("stage_timeout", {
                    "task": state.active_task,
                    "stage": state.stage,
                    "elapsed_seconds": elapsed,
                })
                _escalation_cache.pop(state.active_task or "", None)
                state.clear_active()
                state.heartbeat()
                state.save(config.state_file)
                await asyncio.sleep(config.poll_interval)
                continue

            match state.stage:
                case "classify":
                    await classify_task(state, session_mgr, config, event_log)
                case "architect":
                    await check_stage(state, signal_reader, session_mgr, config, "architect", event_log)
                case "executor":
                    await check_stage(state, signal_reader, session_mgr, config, "executor", event_log)
                case "reviewer":
                    await check_reviewer(state, signal_reader, session_mgr, config, event_log)
                case "merge":
                    await do_merge(state, config, event_log)
                case "wiki":
                    await do_wiki(state, config, event_log, session_mgr)
                case "escalation_tier1":
                    await handle_escalation_tier1(state, signal_reader, session_mgr, config, event_log)
                case "escalation_wait":
                    await handle_escalation_wait(state, signal_reader, config, event_log)
                case "escalation_dialogue":
                    await handle_escalation_dialogue(state, config, event_log)

            # F3: Check for new tasks while current is blocked in escalation_wait
            if state.stage in ("escalation_wait", "escalation_dialogue"):
                new_task = await signal_reader.next_task(config.task_dir)
                if new_task:
                    worktree = await create_worktree(new_task.task_id, config)
                    if worktree:
                        shelved_id = state.active_task
                        logger.info("Shelving escalation-blocked task %s, activating %s",
                                    shelved_id, new_task.task_id)
                        state.shelve()
                        state.activate(new_task)
                        state.worktree = worktree
                        executor_def = config.agents["executor"].with_cwd(worktree)
                        await session_mgr.launch("executor", executor_def)
                        await event_log.record("task_shelved_and_new_activated", {
                            "shelved": shelved_id, "new_task": new_task.task_id,
                        })
                    else:
                        logger.error("Worktree failed for %s — keeping current task in escalation",
                                     new_task.task_id)
        else:
            # Check for new tasks
            new_task = await signal_reader.next_task(config.task_dir)
            if new_task:
                worktree = await create_worktree(new_task.task_id, config)
                if worktree is None:
                    logger.error("Skipping task %s — worktree creation failed", new_task.task_id)
                else:
                    state.activate(new_task)
                    state.worktree = worktree
                    executor_def = config.agents["executor"].with_cwd(worktree)
                    await session_mgr.launch("executor", executor_def)
                    await event_log.record("task_activated", {"task": new_task.task_id, "source": new_task.source})
                    await notify("task_started", "orchestrator", f"New task: {new_task.task_id}")

        # 2. Health checks (persistent sessions only)
        await lifecycle.check_sessions(session_mgr, state)

        # 2.5 Session rotation check
        if state.stage_agent and state.stage_agent in session_mgr.sessions:
            if session_mgr.needs_rotation(state.stage_agent, config.token_rotation_threshold):
                logger.info("Rotating session %s (token threshold exceeded)", state.stage_agent)
                session = session_mgr.sessions[state.stage_agent]
                summary = None
                if session.log.exists():
                    try:
                        content = session.log.read_text()[-4000:]
                        summary = await claude.summarize(content, config)
                    except Exception as exc:
                        logger.warning("Failed to read session log for %s: %s", state.stage_agent, exc)
                await session_mgr.restart(state.stage_agent)
                if summary:
                    await session_mgr.send(
                        state.stage_agent,
                        f"[SYSTEM] Session rotated due to token limit. Context summary:\n\n{summary}",
                    )

        # 3. Heartbeat
        state.heartbeat()
        state.save(config.state_file)

        await asyncio.sleep(config.poll_interval)

    # Graceful shutdown
    discord_task.cancel()
    try:
        await discord_task
    except asyncio.CancelledError:
        pass
    await session_mgr.shutdown()
    await notify("finished", "orchestrator", "Harness shut down gracefully")
    state.shutdown_ts = datetime.now(UTC).isoformat()
    state.save(config.state_file)


async def main():
    parser = argparse.ArgumentParser(description="v5 Harness Orchestrator")
    parser.add_argument("--config", required=True, help="Path to project.toml")
    args = parser.parse_args()
    config = ProjectConfig.load(Path(args.config))
    await main_loop(config)


if __name__ == "__main__":
    asyncio.run(main())
