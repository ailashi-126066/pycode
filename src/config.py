"""Settings shared by every entry point (the CLI now, the TUI later)."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    providers_file: Path
    provider_name: str | None = None
    max_turns: int = 100
    # Sessions of every workspace live in one SQLite file here; PYCODE_HOME overrides it.
    data_dir: Path = Path(os.environ.get("PYCODE_HOME", Path.home() / ".pycode"))

    @property
    def database(self) -> Path:
        return self.data_dir / "sessions.sqlite"
