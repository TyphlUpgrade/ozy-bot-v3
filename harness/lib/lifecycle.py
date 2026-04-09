"""Startup recovery and session health monitoring for the v5 harness."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .pipeline import PipelineState, ProjectConfig
from .signals import EscalationRequest, SignalReader

if TYPE_CHECKING:
    from .sessions import SessionManager

logger = logging.getLogger("harness.lifecycle")


# ---------- Process Check ----------


def is_alive(pid: int) -> bool:
    """Return True if the process with the given PID is alive.

    Uses signal 0 (existence check) which does not kill the process.
    Returns False on any OSError (no such process, permission denied, etc.).
    """
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------- Context Re-injection ----------


def build_reinit_prompt(state: PipelineState) -> str:
    """Build a context re-injection message after a session restart."""
    return (
        f"[REINIT] Resuming task {state.active_task} at stage {state.stage}. "
        "Previous context lost due to session restart."
    )


# ---------- Startup Recovery ----------


async def reconcile(
    state: PipelineState,
    session_mgr: SessionManager,
    signal_reader: SignalReader,
    notify_fn: Callable[[EscalationRequest], Awaitable[None]],
) -> None:
    """Reconcile pipeline state after a crash or restart.

    Called once at startup before the main poll loop begins. Handles three
    cases for an in-progress task: escalation_wait (re-notify), missing
    worktree (clear state), and all other stages (log and resume). After
    reconciling task state, verifies all persistent sessions are alive and
    restarts any that are dead.
    """
    if state.active_task:
        if state.stage == "escalation_wait":
            esc = await signal_reader.read_escalation(state.active_task)
            if esc is not None:
                logger.info(
                    "Re-notifying escalation for task %s", state.active_task
                )
                await notify_fn(esc)
            else:
                logger.warning(
                    "escalation_wait for task %s but no escalation signal found",
                    state.active_task,
                )
        elif state.stage == "escalation_tier1":
            # BUG-016: crash during tier1 — re-send escalation to architect
            esc = await signal_reader.read_escalation(state.active_task)
            if esc is not None:
                logger.info(
                    "Re-sending escalation to architect for task %s after crash",
                    state.active_task,
                )
                await notify_fn(esc)
            else:
                # No signal found — promote to Tier 2 so operator can handle
                logger.warning(
                    "escalation_tier1 for task %s but no escalation signal — promoting to Tier 2",
                    state.active_task,
                )
                state.advance("escalation_wait")
        elif state.worktree is not None and not state.worktree.exists():
            logger.warning(
                "Worktree %s missing for task %s — clearing active state",
                state.worktree,
                state.active_task,
            )
            state.clear_active()
        else:
            logger.info(
                "Resuming %s at stage %s", state.active_task, state.stage
            )

    # Reconcile shelved tasks — re-notify any stuck in escalation stages.
    # Both escalation_wait (Tier 2 / operator) and escalation_tier1 (architect)
    # are checked. For tier1, we re-send to architect; if signal is missing,
    # promote to escalation_wait so operator can handle it on next unshelve.
    for shelved in state.shelved_tasks:
        stask_id = shelved.get("task_id", "")
        sstage = shelved.get("stage", "")
        if sstage in ("escalation_wait", "escalation_tier1"):
            esc = await signal_reader.read_escalation(stask_id)
            if esc is not None:
                logger.info("Re-notifying escalation for shelved task %s (stage=%s)", stask_id, sstage)
                await notify_fn(esc)
            else:
                if sstage == "escalation_tier1":
                    logger.warning(
                        "Shelved task %s in escalation_tier1 but no signal — promoting to escalation_wait",
                        stask_id,
                    )
                    shelved["stage"] = "escalation_wait"
                else:
                    logger.warning(
                        "Shelved task %s in escalation_wait but no escalation signal found",
                        stask_id,
                    )

    # Verify all persistent sessions alive; restart dead ones.
    for name, session in list(session_mgr.sessions.items()):
        agent_def = session_mgr.config.agents.get(name)
        if agent_def is None or agent_def.lifecycle != "persistent":
            continue
        if session.pid is not None and not is_alive(session.pid):
            logger.warning("Session %s (pid %d) is dead — restarting", name, session.pid)
            await session_mgr.restart(name)
            if name == state.stage_agent and state.active_task:
                prompt = build_reinit_prompt(state)
                await session_mgr.send(name, prompt)


# ---------- Health Monitoring ----------


async def check_sessions(
    session_mgr: SessionManager,
    state: PipelineState,
) -> None:
    """Check all persistent sessions for liveness; restart dead ones.

    Called each poll cycle. Sends a reinit prompt to any restarted session
    that was the active stage_agent for the current task.
    """
    for name, session in list(session_mgr.sessions.items()):
        agent_def = session_mgr.config.agents.get(name)
        if agent_def is None or agent_def.lifecycle != "persistent":
            continue
        if session.pid is None:
            continue
        if not is_alive(session.pid):
            logger.warning(
                "Session %s (pid %d) died during poll cycle — restarting",
                name,
                session.pid,
            )
            await session_mgr.restart(name)
            if name == state.stage_agent and state.active_task:
                prompt = build_reinit_prompt(state)
                await session_mgr.send(name, prompt)
