"""Settings shared by every entry point (the TUI, and the evals later)."""

import os
from dataclasses import dataclass
from pathlib import Path

# Skills shipped with pycode (commit, review, test).
BUILTIN_SKILLS = Path(__file__).parent / "skills"


@dataclass(frozen=True)
class Settings:
    workspace: Path
    providers_file: Path
    provider_name: str | None = None
    max_turns: int = 100
    sandbox: bool = True  # --no-sandbox turns Seatbelt off
    # Sessions of every workspace live in one SQLite file here; PYCODE_HOME overrides it.
    data_dir: Path = Path(os.environ.get("PYCODE_HOME", Path.home() / ".pycode"))

    @property
    def database(self) -> Path:
        return self.data_dir / "sessions.sqlite"

    @property
    def settings_file(self) -> Path:
        # "allow_commands": bash commands that run without approval (see sandbox.py).
        return self.data_dir / "settings.json"

    @property
    def worktree_dir(self) -> Path:
        # Subagent worktrees: outside the workspace, so grep and find do not see the copies,
        # and under ~/.cache, which the sandbox lets commands write.
        return Path.home() / ".cache" / "pycode" / "worktrees"

    @property
    def hooks_file(self) -> Path:
        # Commands run at fixed moments (middleware/hooks.py); only the user's own file.
        return self.data_dir / "hooks.json"

    @property
    def mcp_file(self) -> Path:
        # Only the user's own file: a project file would start its commands on opening the repo.
        return self.data_dir / "mcp.json"

    @property
    def skill_dirs(self) -> dict[str, Path]:
        """Where skills are found, by label; a later directory wins on equal skill names."""
        project = self.workspace.resolve()
        return {
            "Built-in": BUILTIN_SKILLS,
            "User": self.data_dir / "skills",
            "Project .agents": project / ".agents" / "skills",
            "Project .pycode": project / ".pycode" / "skills",
        }

    @property
    def instruction_files(self) -> list[Path]:
        """AGENTS.md files put into the system prompt: the user's, then the project's."""
        return [self.data_dir / "AGENTS.md", self.workspace.resolve() / "AGENTS.md"]
