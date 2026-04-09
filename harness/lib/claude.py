"""On-demand claude -p subprocess calls for harness judgment decisions."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.lib.pipeline import ProjectConfig

logger = logging.getLogger("harness.claude")

async def _run_claude(
    system_prompt: str,
    user_prompt: str,
    timeout: int,
    call_type: str,
    config: "ProjectConfig",
    tools: list[str] | None = None,
    model: str | None = None,
) -> str | None:
    """Core subprocess wrapper for ephemeral claude -p calls.

    Returns stdout string on success, None on timeout or non-zero exit.
    """
    level = config.caveman.orchestrator.get(call_type, "ultra")
    directives = config.caveman.directives
    if level != "off" and level in directives:
        system_prompt = directives[level] + "\n\n" + system_prompt

    cmd: list[str] = [config.claude_binary, "-p", "--permission-mode", "dontAsk"]
    if model:
        cmd += ["--model", model]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    cmd += ["--system-prompt", system_prompt, user_prompt]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            logger.warning(
                "claude -p [%s] exited %d: %s",
                call_type, proc.returncode, stderr.decode().strip()[:200],
            )
            return None
        return stdout.decode().strip()
    except asyncio.TimeoutError:
        logger.warning("claude -p [%s] timed out after %ds", call_type, timeout)
        try:
            proc.kill()
            await proc.wait()  # reap zombie
        except Exception:
            pass
        return None
    except Exception as exc:
        logger.error("claude -p [%s] failed: %s", call_type, exc)
        return None


async def classify(task_description: str, config: "ProjectConfig") -> str:
    """Classify a task as 'complex' or 'simple'. Defaults to 'complex' on failure."""
    system = (
        "You are a task classifier. Reply with exactly one word: 'complex' or 'simple'.\n"
        "complex = multi-file changes, unclear scope, architecture decisions, or anything risky.\n"
        "simple  = single-file fix, straightforward addition, obvious scope."
    )
    timeout = config.timeouts.get("classify", 120)
    result = await _run_claude(system, task_description, timeout, "classify", config)
    if result is None:
        logger.warning("classify failed — defaulting to 'complex'")
        return "complex"
    normalized = result.strip().lower()
    if normalized not in ("complex", "simple"):
        logger.warning("classify returned unexpected value %r — defaulting to 'complex'", result)
        return "complex"
    return normalized


async def summarize(content: str, config: "ProjectConfig") -> str | None:
    """Compress stage output for context transfer. Returns summary or None on failure."""
    system = (
        "You are a context compressor. Summarize the following agent output into a concise "
        "paragraph that captures the key decisions, changes made, and any open issues. "
        "Preserve technical specifics. Drop pleasantries and formatting."
    )
    timeout = config.timeouts.get("summarize", 120)
    return await _run_claude(system, content, timeout, "summarize", config)


async def reformulate(
    rejection_feedback: str,
    original_task: str,
    config: "ProjectConfig",
) -> str | None:
    """Rewrite reviewer rejection into an actionable executor prompt. Returns prompt or None."""
    system = (
        "You are a task reformulator. A reviewer has rejected an executor's work. "
        "Rewrite the feedback as a clear, actionable prompt for the executor to retry. "
        "Include: what was wrong, what must change, and what must NOT change. "
        "Be specific and direct. No preamble."
    )
    user = f"Original task:\n{original_task}\n\nReviewer rejection:\n{rejection_feedback}"
    timeout = config.timeouts.get("reformulate", 120)
    return await _run_claude(system, user, timeout, "reformulate", config)


async def document_task(
    task_id: str,
    description: str,
    plan_summary: str,
    diff_stat: str,
    review_verdict: str,
    config: "ProjectConfig",
) -> bool:
    """Write a wiki entry for a completed task via claude -p with /wiki skill.

    Returns True on success, False on failure.
    """
    system = "You are a technical documentation writer. Write a concise wiki entry."
    user = (
        f"/wiki\n\n"
        f"Task ID: {task_id}\n"
        f"Description: {description}\n\n"
        f"Plan summary:\n{plan_summary}\n\n"
        f"Changes (diff stat):\n{diff_stat}\n\n"
        f"Review verdict:\n{review_verdict}"
    )
    timeout = config.timeouts.get("wiki", 300)
    # /wiki in user prompt triggers the skill via magic keyword — no --allowedTools needed.
    # Passing a skill name as --allowedTools blocks all real tools (Write, Read, etc.),
    # leaving the model unable to write wiki files. BUG: Umbra catch, 2026-04-09.
    result = await _run_claude(system, user, timeout, "wiki", config)
    if result is None:
        logger.warning("document_task failed for task_id=%s", task_id)
        return False
    logger.info("document_task succeeded for task_id=%s", task_id)
    return True


async def classify_target(
    message: str,
    agents: list[str],
    config: "ProjectConfig",
) -> str | None:
    """Route an operator's NL message to the correct agent. Returns agent name or None."""
    system = (
        "You are a message router. Given the operator's message and the list of "
        "active agents, reply with exactly one agent name. "
        "If the intent is ambiguous, reply 'ambiguous'."
    )
    user = f"Active agents: {', '.join(agents)}\n\nOperator message: {message}"
    timeout = config.timeouts.get("classify_target", 10)
    result = await _run_claude(system, user, timeout, "classify_target", config, model="haiku")
    if result is None:
        return None
    normalized = result.strip().lower()
    if normalized == "ambiguous":
        return None
    # Validate the response is a known agent name
    for agent in agents:
        if normalized == agent.lower():
            return agent
    logger.warning("classify_target returned unknown agent %r — treating as ambiguous", result)
    return None
