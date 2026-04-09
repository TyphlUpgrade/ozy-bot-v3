"""v5 Harness Orchestrator — async main loop with stage dispatch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime, UTC
from pathlib import Path

from lib import claude, lifecycle
from lib.pipeline import PipelineState, ProjectConfig
from lib.sessions import SessionManager, compress_startup_files
from lib.signals import SignalReader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("harness.orchestrator")


# ---------- Stage Handlers ----------


async def classify_task(state: PipelineState, session_mgr: SessionManager,
                        config: ProjectConfig) -> None:
    """Classify task complexity, route to architect or executor."""
    result = await claude.classify(state.task_description or state.active_task or "", config)
    if result == "complex":
        state.advance("architect", "architect")
        # Send task to architect for planning
        await session_mgr.send("architect", f"[TASK] Plan this task: {state.active_task}")
    else:
        state.advance("executor", "executor")


async def check_stage(state: PipelineState, signal_reader: SignalReader,
                      stage: str) -> None:
    """Poll for stage completion signal."""
    result = await signal_reader.check_stage_complete(stage, state.active_task or "")
    if result is not None:
        next_stages = {"architect": "executor", "executor": "reviewer", "reviewer": "merge"}
        next_stage = next_stages.get(stage, "merge")
        state.advance(next_stage, next_stage if next_stage != "merge" else None)
        logger.info("Stage %s complete for %s → %s", stage, state.active_task, next_stage)


async def check_reviewer(state: PipelineState, signal_reader: SignalReader,
                         session_mgr: SessionManager, config: ProjectConfig) -> None:
    """Check reviewer verdict — approve or trigger retry."""
    result = await signal_reader.check_stage_complete("reviewer", state.active_task or "")
    if result is None:
        return
    verdict = result.get("verdict", "").lower()
    if verdict == "approve" or verdict == "approved":
        state.advance("merge")
        logger.info("Reviewer approved %s", state.active_task)
    else:
        if state.retry_count >= config.max_retries:
            logger.error("Task %s failed after %d retries", state.active_task, state.retry_count)
            state.clear_active()
            return
        # Reformulate and retry
        feedback = result.get("feedback", "Reviewer rejected — no specific feedback.")
        reformulated = await claude.reformulate(feedback, state.active_task or "", config)
        if reformulated:
            state.retry_count += 1
            state.advance("executor", "executor")
            await session_mgr.send("executor", reformulated)
            logger.info("Retry %d for %s", state.retry_count, state.active_task)
        else:
            # Reformulate failed — send raw feedback as fallback
            state.retry_count += 1
            state.advance("executor", "executor")
            await session_mgr.send("executor", f"[RETRY] {feedback}")


async def do_merge(state: PipelineState, config: ProjectConfig) -> None:
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
        await asyncio.create_subprocess_exec("git", "merge", "--abort", cwd=cwd)
        state.clear_active()
        return
    # Run tests
    proc = await asyncio.create_subprocess_exec(
        "python3", "-m", "pytest", "tests/", "-x", "--timeout=120",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        logger.error("Tests timed out for %s — killing and reverting", state.active_task)
        proc.kill()
        await proc.wait()
        await asyncio.create_subprocess_exec(
            "git", "revert", "--no-edit", "-m", "1", "HEAD", cwd=cwd,
        )
        state.clear_active()
        return
    if proc.returncode != 0:
        logger.error("Tests failed after merge for %s — reverting", state.active_task)
        await asyncio.create_subprocess_exec(
            "git", "revert", "--no-edit", "-m", "1", "HEAD", cwd=cwd,
        )
        state.clear_active()
        return
    state.advance("wiki")
    logger.info("Merge + tests passed for %s", state.active_task)


async def do_wiki(state: PipelineState, config: ProjectConfig) -> None:
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
    state.clear_active()
    logger.info("Task %s complete", task_id)


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


# ---------- Main Loop ----------


async def main_loop(config: ProjectConfig) -> None:
    state = PipelineState.load(config.state_file)
    session_mgr = SessionManager(config.session_dir, config)
    signal_reader = SignalReader(config.signal_dir)

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
            match state.stage:
                case "classify":
                    await classify_task(state, session_mgr, config)
                case "architect":
                    await check_stage(state, signal_reader, "architect")
                case "executor":
                    await check_stage(state, signal_reader, "executor")
                case "reviewer":
                    await check_reviewer(state, signal_reader, session_mgr, config)
                case "merge":
                    await do_merge(state, config)
                case "wiki":
                    await do_wiki(state, config)
                case "escalation_wait" | "escalation_tier1":
                    pass  # Phase 2 — checked by lifecycle.reconcile on recovery
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
