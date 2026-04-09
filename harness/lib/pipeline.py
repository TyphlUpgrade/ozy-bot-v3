"""Pipeline state machine, agent definitions, and project configuration."""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass, field, asdict
from datetime import datetime, UTC
from pathlib import Path

logger = logging.getLogger("harness.pipeline")

# Valid caveman compression levels
VALID_CAVEMAN_LEVELS = frozenset({
    "off", "lite", "full", "ultra",
    "wenyan-lite", "wenyan-full", "wenyan-ultra",
})


# ---------- Agent Definition ----------


@dataclass
class AgentDef:
    name: str
    model: str                          # "opus" | "sonnet" | "haiku"
    mode: str                           # "read-only" | "full"
    lifecycle: str                      # "persistent" | "per-task"
    stale_minutes: int = 15
    keywords: str = "ERROR,CRITICAL,BLOCKED"
    discord_channel: str = "dev-agents"
    auto_start: bool = True
    deny_flags: list[str] = field(default_factory=list)
    role_file: Path | None = None       # path to agent role markdown
    cwd: Path | None = None             # working directory (worktree for per-task sessions)

    @property
    def deny_flags_str(self) -> str:
        if not self.deny_flags:
            return ""
        tools = ",".join(self.deny_flags)
        return f"--disallowedTools {tools}"

    @property
    def role_content(self) -> str:
        if self.role_file and self.role_file.exists():
            return self.role_file.read_text()
        return ""

    def with_cwd(self, worktree: Path) -> AgentDef:
        """Return a copy with worktree-specific adjustments."""
        return AgentDef(
            name=self.name, model=self.model, mode=self.mode,
            lifecycle="per-task", stale_minutes=self.stale_minutes,
            keywords=self.keywords, discord_channel=self.discord_channel,
            auto_start=False, deny_flags=self.deny_flags,
            role_file=self.role_file, cwd=worktree,
        )


# Read-only deny list — comprehensive, covers built-in and MCP filesystem tools
READ_ONLY_DENY = [
    "Edit", "Write", "NotebookEdit",
    "mcp__filesystem__write_file", "mcp__filesystem__edit_file",
    "mcp__filesystem__create_directory", "mcp__filesystem__move_file",
]


# ---------- Phase 1 Hardcoded Agents ----------


def _default_agents(agents_dir: Path) -> dict[str, AgentDef]:
    return {
        "architect": AgentDef(
            name="architect", model="opus", mode="read-only",
            lifecycle="persistent", stale_minutes=15,
            deny_flags=READ_ONLY_DENY,
            role_file=agents_dir / "architect.md",
        ),
        "executor": AgentDef(
            name="executor", model="sonnet", mode="full",
            lifecycle="per-task", stale_minutes=30,
            auto_start=False,
            role_file=agents_dir / "executor.md",
        ),
        "reviewer": AgentDef(
            name="reviewer", model="sonnet", mode="read-only",
            lifecycle="persistent", stale_minutes=15,
            deny_flags=READ_ONLY_DENY,
            role_file=agents_dir / "reviewer.md",
        ),
    }


# ---------- Caveman Configuration ----------


@dataclass
class CavemanConfig:
    default_level: str = "full"
    agents: dict[str, str] = field(default_factory=dict)
    orchestrator: dict[str, str] = field(default_factory=dict)
    wenyan_enabled: bool = False
    wenyan_default: str = "lite"
    skill_path: str = "~/.claude/plugins/marketplaces/caveman/caveman/SKILL.md"
    compress_script: str = "~/.claude/plugins/marketplaces/caveman/caveman-compress"
    compress_targets: list[str] = field(default_factory=list)
    skills_commit: bool = True
    skills_review: bool = True
    skills_compress: bool = True
    directives: dict[str, str] = field(default_factory=dict)  # loaded at runtime by SessionManager
    _runtime_overrides: dict[str, str] = field(default_factory=dict, repr=False)

    def level_for(self, agent_name: str) -> str:
        """Get caveman level for an agent, respecting runtime overrides."""
        if agent_name in self._runtime_overrides:
            return self._runtime_overrides[agent_name]
        if "__all__" in self._runtime_overrides:
            return self._runtime_overrides["__all__"]
        return self.agents.get(agent_name, self.default_level)

    def set_agent(self, agent_name: str, level: str) -> None:
        if level not in VALID_CAVEMAN_LEVELS:
            raise ValueError(f"Invalid caveman level: {level!r}")
        self._runtime_overrides[agent_name] = level

    def set_all(self, level: str) -> None:
        self._runtime_overrides["__all__"] = level
        # Also override per-agent so level_for() returns it
        for name in self.agents:
            self._runtime_overrides[name] = level

    def reset_to_defaults(self) -> None:
        self._runtime_overrides.clear()

    @classmethod
    def from_toml(cls, data: dict) -> CavemanConfig:
        caveman = data.get("caveman", {})
        skills = caveman.get("skills", {})
        wenyan = caveman.get("wenyan", {})
        agents = caveman.get("agents", {})
        orch = caveman.get("orchestrator", {})
        # Validate levels
        dl = caveman.get("default_level", "full")
        for name, level in {**{"default": dl}, **agents, **orch}.items():
            if level not in VALID_CAVEMAN_LEVELS:
                raise ValueError(
                    f"Invalid caveman level '{level}' for {name}. "
                    f"Valid: {', '.join(sorted(VALID_CAVEMAN_LEVELS))}"
                )
        return cls(
            default_level=caveman.get("default_level", "full"),
            agents=agents,
            orchestrator=orch,
            wenyan_enabled=wenyan.get("enabled", False),
            wenyan_default=wenyan.get("default_level", "lite"),
            skill_path=skills.get("skill_path",
                "~/.claude/plugins/marketplaces/caveman/caveman/SKILL.md"),
            compress_script=skills.get("compress_script",
                "~/.claude/plugins/marketplaces/caveman/caveman-compress"),
            compress_targets=skills.get("compress_targets", []),
            skills_commit=skills.get("commit", True),
            skills_review=skills.get("review", True),
            skills_compress=skills.get("compress", True),
        )


# ---------- Project Configuration ----------


@dataclass
class ProjectConfig:
    project_root: Path
    signal_dir: Path
    task_dir: Path
    state_file: Path
    session_dir: Path
    worktree_base: Path
    poll_interval: float
    max_retries: int
    escalation_timeout: int
    tier1_timeout: int
    agents: dict[str, AgentDef]
    caveman: CavemanConfig
    timeouts: dict[str, int] = field(default_factory=dict)
    max_stage_minutes: dict[str, int] = field(default_factory=dict)
    claude_binary: str = "claude"       # configurable LLM CLI binary
    commands_module: str | None = None
    test_command: str = "python3 -m pytest tests/ -x"  # command run after merge; override for non-Python projects
    token_rotation_threshold: int = 100_000  # cumulative tokens before session rotation

    @classmethod
    def load(cls, config_path: Path) -> ProjectConfig:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        project = data.get("project", {})
        pipeline = data.get("pipeline", {})
        root = Path(project.get("root", ".")).resolve()
        agents_dir = config_path.parent / "agents"
        return cls(
            project_root=root,
            signal_dir=root / project.get("signal_dir", "signals"),
            task_dir=root / project.get("task_dir", "agent_tasks"),
            state_file=root / project.get("state_file", "pipeline_state.json"),
            session_dir=Path(project.get("session_dir", "/tmp/harness-sessions")),
            worktree_base=Path(project.get("worktree_base", "/tmp/harness-worktrees")),
            poll_interval=pipeline.get("poll_interval", 5.0),
            max_retries=pipeline.get("max_retries", 3),
            escalation_timeout=pipeline.get("escalation_timeout", 14400),
            tier1_timeout=pipeline.get("tier1_timeout", 1800),
            agents=_default_agents(agents_dir),
            caveman=CavemanConfig.from_toml(data),
            claude_binary=pipeline.get("claude_binary", "claude"),
            test_command=pipeline.get("test_command", "python3 -m pytest tests/ -x"),
            token_rotation_threshold=pipeline.get("token_rotation_threshold", 100_000),
            timeouts={
                "classify": pipeline.get("classify_timeout", 120),
                "summarize": pipeline.get("summarize_timeout", 120),
                "reformulate": pipeline.get("reformulate_timeout", 120),
                "wiki": pipeline.get("wiki_timeout", 300),
                "classify_target": pipeline.get("classify_target_timeout", 10),
            },
            max_stage_minutes={
                stage: pipeline.get(f"{stage}_max_minutes", default)
                for stage, default in [
                    ("classify", 10),
                    ("architect", 60),
                    ("executor", 120),
                    ("reviewer", 60),
                    ("merge", 15),
                    ("wiki", 15),
                ]
            },
            commands_module=data.get("commands", {}).get("module"),
        )


# Valid pipeline stages — advance() validates against this set
VALID_STAGES = frozenset({
    "classify", "architect", "executor", "reviewer", "merge", "wiki",
    "escalation_wait", "escalation_tier1",
})


# ---------- Pipeline State ----------


@dataclass
class PipelineState:
    active_task: str | None = None
    task_description: str | None = None  # stored for classify and wiki
    stage: str | None = None            # classify|architect|executor|reviewer|merge|wiki|escalation_*
    stage_agent: str | None = None
    worktree: Path | None = None
    retry_count: int = 0
    heartbeat_ts: str | None = None
    shutdown_ts: str | None = None
    escalation_started_ts: str | None = None  # when current escalation began (for tier promotion timing)
    pre_escalation_stage: str | None = None  # stage before escalation, for resume routing
    pre_escalation_agent: str | None = None  # agent before escalation, for reply injection
    last_renotify_ts: str | None = None  # when blocking escalation last re-notified operator
    stage_started_ts: str | None = None  # wall-clock time current stage began (for max timeout)
    shelved_tasks: list[dict] = field(default_factory=list)  # tasks shelved during escalation
    plan_summary: str | None = None        # architect plan output, collected in check_stage
    diff_stat: str | None = None           # git diff --stat, collected in do_merge
    review_verdict: str | None = None      # reviewer verdict, collected in check_reviewer

    def activate(self, task: "TaskSignal") -> None:
        from .signals import TaskSignal  # avoid circular at module level
        self.active_task = task.task_id
        self.task_description = task.description
        self.stage = "classify"
        self.stage_agent = None
        self.worktree = None
        self.retry_count = 0
        self.escalation_started_ts = None
        self.pre_escalation_stage = None
        self.pre_escalation_agent = None
        self.last_renotify_ts = None
        self.stage_started_ts = datetime.now(UTC).isoformat()
        self.plan_summary = None
        self.diff_stat = None
        self.review_verdict = None

    def advance(self, next_stage: str, agent: str | None = None) -> None:
        if next_stage not in VALID_STAGES:
            raise ValueError(f"Invalid stage: {next_stage!r}. Valid: {', '.join(sorted(VALID_STAGES))}")
        self.stage = next_stage
        self.stage_agent = agent
        self.stage_started_ts = datetime.now(UTC).isoformat()
        # Track escalation timing for tier promotion
        if next_stage.startswith("escalation_"):
            if self.escalation_started_ts is None:
                self.escalation_started_ts = datetime.now(UTC).isoformat()
        else:
            self.escalation_started_ts = None

    def resume_from_escalation(self) -> None:
        """Restore pre-escalation stage/agent and clear escalation context."""
        original_stage = self.pre_escalation_stage or "executor"
        original_agent = self.pre_escalation_agent
        self.advance(original_stage, original_agent)
        self.pre_escalation_stage = None
        self.pre_escalation_agent = None

    def clear_active(self) -> None:
        self.active_task = None
        self.task_description = None
        self.stage = None
        self.stage_agent = None
        self.worktree = None
        self.retry_count = 0
        self.escalation_started_ts = None
        self.pre_escalation_stage = None
        self.pre_escalation_agent = None
        self.last_renotify_ts = None
        self.stage_started_ts = None
        self.plan_summary = None
        self.diff_stat = None
        self.review_verdict = None

    def shelve(self) -> None:
        """Save current active task to shelf and clear pipeline for next task."""
        if not self.active_task:
            return
        self.shelved_tasks.append({
            "task_id": self.active_task,
            "description": self.task_description,
            "stage": self.stage,
            "stage_agent": self.stage_agent,
            "worktree": str(self.worktree) if self.worktree else None,
            "retry_count": self.retry_count,
            "shelved_at": datetime.now(UTC).isoformat(),
            "escalation_started_ts": self.escalation_started_ts,
            "pre_escalation_stage": self.pre_escalation_stage,
            "pre_escalation_agent": self.pre_escalation_agent,
            "last_renotify_ts": self.last_renotify_ts,
            "plan_summary": self.plan_summary,
            "diff_stat": self.diff_stat,
            "review_verdict": self.review_verdict,
        })
        self.clear_active()

    def unshelve(self) -> dict | None:
        """Pop newest shelved task and restore it as active. Returns task dict or None."""
        if not self.shelved_tasks:
            return None
        task = self.shelved_tasks.pop()
        self.active_task = task["task_id"]
        self.task_description = task.get("description")
        self.stage = task.get("stage")
        self.stage_agent = task.get("stage_agent")
        wt = task.get("worktree")
        self.worktree = Path(wt) if wt else None
        self.retry_count = task.get("retry_count", 0)
        self.escalation_started_ts = task.get("escalation_started_ts")
        self.pre_escalation_stage = task.get("pre_escalation_stage")
        self.pre_escalation_agent = task.get("pre_escalation_agent")
        self.last_renotify_ts = task.get("last_renotify_ts")
        self.plan_summary = task.get("plan_summary")
        self.diff_stat = task.get("diff_stat")
        self.review_verdict = task.get("review_verdict")
        self.stage_started_ts = datetime.now(UTC).isoformat()
        # Reset escalation clock so shelved duration doesn't count toward timeout
        if self.stage and self.stage.startswith("escalation_"):
            self.escalation_started_ts = datetime.now(UTC).isoformat()
        return task

    def heartbeat(self) -> None:
        self.heartbeat_ts = datetime.now(UTC).isoformat()

    def save(self, path: Path | None = None) -> None:
        if path is None:
            return
        data = asdict(self)
        # Convert Path to str for JSON
        if data.get("worktree"):
            data["worktree"] = str(data["worktree"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)

    @classmethod
    def load(cls, path: Path) -> PipelineState:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            wt = data.pop("worktree", None)
            # Pop unknown keys to avoid TypeError on version mismatch (BUG-002 mitigation)
            known = {f.name for f in cls.__dataclass_fields__.values()}
            for key in list(data):
                if key not in known:
                    logger.debug("Dropping unknown state key: %s", key)
                    data.pop(key)
            state = cls(**data)
            if wt:
                state.worktree = Path(wt)
            return state
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Bad pipeline state, starting fresh: %s", e)
            return cls()
