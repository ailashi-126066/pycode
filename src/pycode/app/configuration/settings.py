"""Application settings and local path defaults, independent of Provider JSON parsing."""

from dataclasses import dataclass, field
from pathlib import Path

from pycode.sandbox import SandboxSettings, validate_passthrough
from pycode.tools import ToolGate


@dataclass(frozen=True)
class AppConfig:
    workspace: Path
    user_data: Path
    summary_keep_recent_tokens: int = 20000
    # The only run budget: a run ends with turn_limit after this many turns.
    max_turns: int = 100
    # 11.1 section 40: the same call with the same result (back to back for read-only
    # tools), or a tree seen this often, is a loop. Only set here; there is no CLI flag.
    loop_repeat_threshold: int = 3
    # A second detection asks the user; a subagent of a non-interactive run ends instead.
    loop_asks_user: bool = True
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    project_trusted: bool = False
    skill_index_budget: int = 2000
    memory_mode: str | None = None
    # Subagents running or waiting for approval at the same time.
    max_agents: int = 4
    read_only: bool = False
    allow_delegation: bool = True
    agent_id: str = "lead"
    request_database: Path | None = None
    isolated_worker: bool = False
    # Embedding hooks: a gate checked before the default tool authorization chain and
    # directories prepended to the Bash tool's PATH.
    tool_gate: ToolGate | None = None
    bash_path_prefix: tuple[str, ...] = ()
    credential_workspace: Path | None = None
    subprocess_passthrough: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.max_agents <= 4:
            raise ValueError("Subagent concurrency must be 1–4")
        validate_passthrough(self.subprocess_passthrough)
        if self.memory_mode not in {None, "auto", "off"}:
            raise ValueError("Memory mode must be auto or off")
        if (
            not isinstance(self.project_trusted, bool)
            or not isinstance(self.skill_index_budget, int)
            or isinstance(self.skill_index_budget, bool)
            or self.skill_index_budget < 1
        ):
            raise ValueError("Skill trust and index budget are invalid")
        if not isinstance(self.sandbox, SandboxSettings):
            raise ValueError("Sandbox settings must be validated")
        if not self.workspace.is_absolute() or not self.user_data.is_absolute():
            raise ValueError("Application paths must be absolute")
        if (
            not isinstance(self.summary_keep_recent_tokens, int)
            or isinstance(self.summary_keep_recent_tokens, bool)
            or self.summary_keep_recent_tokens < 1
        ):
            raise ValueError("Summary configuration is invalid")
        if self.max_turns < 1:
            raise ValueError("Maximum turns must be positive")
        if self.loop_repeat_threshold < 1:
            raise ValueError("Loop repeat threshold must be positive")


def load_config(workspace: str | None = None, user_data: str | None = None) -> AppConfig:
    project = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
    data = Path(user_data).expanduser().resolve() if user_data else Path.home() / ".pycode"
    return AppConfig(workspace=project, user_data=data)
