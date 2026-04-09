"""FIFO-based session management for persistent Claude Code sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline import AgentDef, ProjectConfig

logger = logging.getLogger("harness.sessions")

from .pipeline import VALID_CAVEMAN_LEVELS as VALID_LEVELS  # single source of truth


# Message prefix conventions (used across orchestrator, lifecycle, discord_companion):
#   [TASK]     — new task assignment (orchestrator → executor/architect)
#   [RETRY]    — reformulated retry after reviewer rejection (orchestrator → executor)
#   [OPERATOR] — operator feedback from Discord (discord_companion → agent)
#   [OPERATOR REPLY] — operator reply to escalation (discord_companion → agent)
#   [SYSTEM]   — caveman level change or system directive (session_mgr → agent)
#   [REINIT]   — session reinitialization after crash recovery (lifecycle → agent)


@dataclass
class Session:
    name: str
    role: str                           # agent archetype (e.g. "executor"); name is unique ID
    fd: int                             # write-end file descriptor for FIFO
    fifo: Path
    log: Path
    pid: int | None = None              # tmux pane PID (for health checks)


def _load_caveman_directives(skill_path: Path) -> dict[str, str]:
    """Load caveman SKILL.md and build per-level directive dict.

    The SKILL.md contains all levels inline. Each per-level directive
    is the full SKILL.md + an activation line specifying the active level.
    """
    if not skill_path.exists():
        logger.warning("Caveman SKILL.md not found at %s", skill_path)
        return {}
    template = skill_path.read_text()
    # Strip YAML frontmatter
    if template.startswith("---"):
        parts = template.split("---", 2)
        if len(parts) >= 3:
            template = parts[2]
    base = template.strip()
    return {
        level: f"{base}\n\n**Active level: {level}.** Use this level for all output."
        for level in ("lite", "full", "ultra",
                      "wenyan-lite", "wenyan-full", "wenyan-ultra")
    }


async def compress_startup_files(config: "ProjectConfig") -> None:
    """LLM-based startup compression of CLAUDE.md and agent role files.

    Idempotent: skips any file whose .original.md backup already exists.
    Falls back gracefully on compress failure (restores original, logs warning).
    """
    if not config.caveman.skills_compress:
        return

    compress_script = Path(config.caveman.compress_script).expanduser() / "compress.py"
    if not compress_script.exists():
        logger.warning("caveman-compress script not found at %s — skipping startup compression",
                       compress_script)
        return

    targets: list[Path] = [config.project_root / "CLAUDE.md"]
    for agent_def in config.agents.values():
        if agent_def.role_file and agent_def.role_file.exists():
            targets.append(agent_def.role_file)
    # Also include any explicit compress_targets from config
    for t in config.caveman.compress_targets:
        p = Path(t).expanduser()
        if not p.is_absolute():
            p = config.project_root / p
        if p.exists():
            targets.append(p)

    for target in targets:
        backup = target.with_name(target.name + ".original.md")
        if backup.exists():
            logger.debug("Skipping %s — backup already exists (already compressed)", target)
            continue
        if not target.exists():
            continue

        # Back up original before compressing
        backup.write_text(target.read_text())
        logger.info("Compressing %s via caveman-compress", target)

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", str(compress_script), str(target),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode()[:200])
            logger.info("Compressed %s", target)
        except Exception as exc:
            logger.warning("caveman-compress failed for %s (%s) — restoring original", target, exc)
            target.write_text(backup.read_text())
            backup.unlink(missing_ok=True)


class SessionManager:
    """Manages persistent OMC sessions via FIFO + stream-json."""

    def __init__(self, session_dir: Path, config: ProjectConfig):
        self.sessions: dict[str, Session] = {}
        self.session_dir = session_dir
        self.config = config
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # Load caveman directives once at startup, store on config for shared access
        skill_path = Path(config.caveman.skill_path).expanduser()
        config.caveman.directives = _load_caveman_directives(skill_path)
        if config.caveman.directives:
            logger.info("Loaded caveman directives for %d levels", len(config.caveman.directives))

    async def launch(self, name: str, agent_def: AgentDef) -> None:
        """Launch a persistent session via clawhip tmux new."""
        fifo_path = self.session_dir / f"{name}.fifo"
        log_path = self.session_dir / f"{name}.log"

        # Clean up stale FIFO (crash recovery)
        fifo_path.unlink(missing_ok=True)
        os.mkfifo(fifo_path, mode=0o600)

        # Launch via clawhip tmux new. The tmux shell's `< fifo` redirection
        # blocks in the tmux pane's process, NOT in our event loop. This avoids
        # the FIFO open deadlock (POSIX: read-end open blocks until writer exists).
        binary = shlex.quote(self.config.claude_binary)
        cwd_prefix = f"cd {shlex.quote(str(agent_def.cwd))} && " if agent_def.cwd else ""
        cmd = (
            f"{cwd_prefix}{binary} -p --verbose"
            f" --input-format stream-json --output-format stream-json"
            f" --permission-mode dontAsk {agent_def.deny_flags_str}"
            f" --model {shlex.quote(agent_def.model)}"
            f" --include-hook-events"
            f" < {shlex.quote(str(fifo_path))} > {shlex.quote(str(log_path))} 2>&1"
        )
        await asyncio.create_subprocess_exec(
            "clawhip", "tmux", "new",
            "--session", f"agent-{name}",
            "-n", name,
            "--stale-minutes", str(agent_def.stale_minutes),
            "--keywords", agent_def.keywords,
            "--channel", agent_def.discord_channel,
            "--", "bash", "-c", cmd,
        )

        # clawhip tmux new returns immediately (async tmux launch).
        # Brief wait for the tmux pane to open the read end of the FIFO.
        await asyncio.sleep(0.5)

        # Now safe to open write end — reader exists in the tmux pane.
        # O_NONBLOCK: avoids stalling the event loop if buffer is full.
        fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)

        # Capture tmux pane PID for health checks
        pid = None
        try:
            pid_proc = await asyncio.create_subprocess_exec(
                "tmux", "list-panes", "-t", f"agent-{name}", "-F", "#{pane_pid}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await pid_proc.communicate()
            if pid_proc.returncode == 0 and stdout.strip():
                pid = int(stdout.strip().split(b"\n")[0])
        except (ValueError, OSError):
            logger.debug("Could not capture PID for session %s", name)

        self.sessions[name] = Session(
            name=name, role=agent_def.name, fd=fd, fifo=fifo_path, log=log_path, pid=pid,
        )

        # Build init message with caveman directive if configured
        init_parts = [agent_def.role_content]
        caveman_level = self.config.caveman.level_for(name)
        directives = self.config.caveman.directives
        if caveman_level != "off" and caveman_level in directives:
            init_parts.insert(0, directives[caveman_level])

        if any(init_parts):
            await self.send(name, "\n\n".join(p for p in init_parts if p))
        logger.info("Launched session %s (model=%s, caveman=%s)",
                     name, agent_def.model, caveman_level)

    async def send(self, name: str, content: str) -> None:
        """Send a message to a session via its FIFO."""
        session = self.sessions.get(name)
        if session is None:
            logger.warning("send() called for unknown session %s", name)
            return
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": content}
        })
        payload = (msg + "\n").encode()
        try:
            os.write(session.fd, payload)
        except OSError as e:
            logger.warning("FIFO write error for %s (%s), restarting session", name, e)
            await self.restart(name)
            try:
                os.write(self.sessions[name].fd, payload)
            except OSError as e2:
                logger.error("FIFO write failed after restart for %s: %s", name, e2)

    async def restart(self, name: str) -> None:
        """Restart a dead session. Cleans up old FIFO, launches fresh."""
        session = self.sessions.pop(name, None)
        if session:
            try:
                os.close(session.fd)
            except OSError:
                pass
            await asyncio.create_subprocess_exec(
                "tmux", "kill-session", "-t", f"agent-{name}",
            )
            await asyncio.sleep(1)
            session.fifo.unlink(missing_ok=True)
        await self.launch(name, self.config.agents[name])

    async def inject_caveman_update(self, name: str, level: str) -> None:
        """Send updated caveman directive to a running session."""
        if name not in self.sessions:
            logger.warning("Cannot inject caveman update: session %s not found", name)
            return
        directives = self.config.caveman.directives
        if level == "off":
            await self.send(name, "[SYSTEM] Caveman mode disabled. Resume normal output.")
        elif level in directives:
            directive = directives[level]
            await self.send(name, f"[SYSTEM] Update compression level to {level}.\n\n{directive}")
        else:
            logger.warning("Unknown caveman level '%s' for inject_caveman_update", level)

    async def shutdown(self) -> None:
        """Graceful shutdown — close all FIFOs (sends EOF to sessions)."""
        for session in list(self.sessions.values()):
            try:
                os.close(session.fd)
            except OSError:
                pass
        await asyncio.sleep(5)
        # Force-kill any sessions that ignored EOF
        for session in list(self.sessions.values()):
            try:
                await asyncio.create_subprocess_exec(
                    "tmux", "kill-session", "-t", f"agent-{session.name}",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                pass
            session.fifo.unlink(missing_ok=True)
        self.sessions.clear()
        logger.info("All sessions shut down")
