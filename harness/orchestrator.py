"""v5 Harness Orchestrator — async main loop with stage dispatch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shlex
import signal
import sys
from datetime import datetime, UTC
from pathlib import Path

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
                      stage: str, event_log: EventLog) -> None:
    """Poll for stage completion signal."""
    result = await signal_reader.check_stage_complete(stage, state.active_task or "")
    if result is not None:
        next_stages = {"architect": "executor", "executor": "reviewer", "reviewer": "merge"}
        next_stage = next_stages.get(stage, "merge")
        state.advance(next_stage, next_stage if next_stage != "merge" else None)
        await event_log.record("stage_advanced", {"task": state.active_task, "from": stage, "to": next_stage})
        logger.info("Stage %s complete for %s → %s", stage, state.active_task, next_stage)


async def check_reviewer(state: PipelineState, signal_reader: SignalReader,
                         session_mgr: SessionManager, config: ProjectConfig,
                         event_log: EventLog) -> None:
    """Check reviewer verdict — approve or trigger retry."""
    result = await signal_reader.check_stage_complete("reviewer", state.active_task or "")
    if result is None:
        return
    verdict = result.get("verdict", "").lower()
    if verdict == "approve" or verdict == "approved":
        state.advance("merge")
        await event_log.record("stage_advanced", {"task": state.active_task, "from": "reviewer", "to": "merge", "verdict": "approved"})
        logger.info("Reviewer approved %s", state.active_task)
    else:
        if state.retry_count >= config.max_retries:
            logger.error("Task %s failed after %d retries", state.active_task, state.retry_count)
            _escalation_cache.pop(state.active_task or "", None)
            state.clear_active()
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
    state.advance("wiki")
    await event_log.record("stage_advanced", {"task": state.active_task, "from": "merge", "to": "wiki"})
    logger.info("Merge + tests passed for %s", state.active_task)


async def do_wiki(state: PipelineState, config: ProjectConfig, event_log: EventLog) -> None:
    """Document task via claude -p + /wiki."""
    task_id = state.active_task or ""
    description = state.task_description or task_id
    success = await claude.document_task(
        task_id=task_id,
        description=description,
        plan_summary="(auto-generated)",
        diff_stat="(see git log)",
        review_verdict="approved",
        config=config,
    )
    if not success:
        logger.warning("Wiki documentation failed for %s — continuing", task_id)
    await event_log.record("task_completed", {"task": task_id})
    _escalation_cache.pop(task_id, None)
    state.clear_active()
    logger.info("Task %s complete", task_id)


# ---------- Escalation Handlers ----------


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

    tier = escalation.route_escalation(esc)
    # Store pre-escalation context for resume routing
    state.pre_escalation_stage = state.stage
    state.pre_escalation_agent = state.stage_agent

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

    # Discord companion placeholder — Phase 1 runs without Discord
    # In Phase 2, discord_companion.start() runs as asyncio.create_task() here.
    pending_mutations: list = []

    await notify("started", "orchestrator", "Harness started")

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def _on_signal(sig, _frame):
        logger.info("Received %s — shutting down", signal.Signals(sig).name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    while not shutdown_event.is_set():
        # 0. Apply pending mutations from Discord (concurrency-safe)
        for mutation in pending_mutations:
            await mutation(state, session_mgr)
        pending_mutations.clear()

        # 1. Check pipeline progress
        if state.active_task:
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
                    await check_stage(state, signal_reader, "architect", event_log)
                case "executor":
                    await check_stage(state, signal_reader, "executor", event_log)
                case "reviewer":
                    await check_reviewer(state, signal_reader, session_mgr, config, event_log)
                case "merge":
                    await do_merge(state, config, event_log)
                case "wiki":
                    await do_wiki(state, config, event_log)
                case "escalation_tier1":
                    await handle_escalation_tier1(state, signal_reader, session_mgr, config, event_log)
                case "escalation_wait":
                    await handle_escalation_wait(state, signal_reader, config, event_log)
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

        # 3. Heartbeat
        state.heartbeat()
        state.save(config.state_file)

        await asyncio.sleep(config.poll_interval)

    # Graceful shutdown
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
