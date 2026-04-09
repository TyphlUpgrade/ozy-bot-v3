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
    claude_binary: str = "claude"       # configurable LLM CLI binary
    commands_module: str | None = None

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
            timeouts={
                "classify": pipeline.get("classify_timeout", 120),
                "summarize": pipeline.get("summarize_timeout", 120),
                "reformulate": pipeline.get("reformulate_timeout", 120),
                "wiki": pipeline.get("wiki_timeout", 300),
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

    def advance(self, next_stage: str, agent: str | None = None) -> None:
        if next_stage not in VALID_STAGES:
            raise ValueError(f"Invalid stage: {next_stage!r}. Valid: {', '.join(sorted(VALID_STAGES))}")
        self.stage = next_stage
        self.stage_agent = agent
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
            state = cls(**data)
            if wt:
                state.worktree = Path(wt)
            return state
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Bad pipeline state, starting fresh: %s", e)
            return cls()
