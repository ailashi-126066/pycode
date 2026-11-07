"""Rules all file tools follow: stay in the workspace, read before changing, keep output small."""

import hashlib
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

# Tool output beyond these limits is cut so one result cannot fill the model's context.
MAX_LINES = 2000
MAX_BYTES = 50 * 1024

PROTECTED_DIRS = {".git", ".pycode"}


class ToolError(Exception):
    """A tool failure the model should see as its tool result and can react to."""


class Files:
    """One per agent run: the workspace root plus what the agent has read."""

    def __init__(self, workspace: Path, read_only: list[Path] = ()):
        self.workspace = workspace.resolve()
        # Directories outside these rules that may be read but never changed (skills).
        self.read_only = [root.resolve() for root in read_only]
        # sha256 of each file as the agent last read or wrote it.
        self.seen: dict[Path, str] = {}
        self.locks: dict[Path, threading.RLock] = {}
        self.locks_guard = threading.Lock()

    def resolve(self, path: str, *, change: bool = False) -> Path:
        """The real file a model-supplied path names; refuse anything outside the workspace.
        Reading may also reach the read-only directories."""
        target = (self.workspace / path).resolve()  # symlinks are followed before checking
        if not change and any(target.is_relative_to(root) for root in self.read_only):
            return target
        if not target.is_relative_to(self.workspace):
            raise ToolError(f"{path} is outside the workspace")
        parts = target.relative_to(self.workspace).parts
        if any(part.casefold() in PROTECTED_DIRS for part in parts):
            raise ToolError(f"{path} is internal project state and cannot be accessed")
        name = target.name.casefold()
        if name.startswith(".env") and name != ".env.example":
            raise ToolError(f"{path} may contain credentials and cannot be accessed")
        return target

    def read(self, path: str) -> str:
        target = self.resolve(path)
        data = target.read_bytes()
        self.seen[target] = hashlib.sha256(data).hexdigest()
        return data.decode("utf-8", "replace")

    def read_for_change(self, path: str) -> str:
        """Current text of a file the agent is about to change; it must be unchanged since read."""
        self.check_unchanged(self.resolve(path, change=True), path)
        return self.read(path)

    def write(self, path: str, text: str) -> None:
        target = self.resolve(path, change=True)
        with self.lock(path):
            self.check_unchanged(target, path)
            data = text.encode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write a sibling file, then rename it over the target: readers never see half a file.
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(data)
                if target.exists():
                    shutil.copymode(target, temporary)
                os.replace(temporary, target)
            except BaseException:
                temporary.unlink(missing_ok=True)  # e.g. the disk filled up: leave no stray file
                raise
            self.seen[target] = hashlib.sha256(data).hexdigest()

    def check_unchanged(self, target: Path, path: str) -> None:
        expected = self.seen.get(target)
        if not target.exists():
            if expected is not None:
                raise ToolError(f"{path} was deleted after you read it; check again")
            return
        if expected is None:
            raise ToolError(f"Read {path} before changing it")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ToolError(f"{path} changed after you read it; read it again")

    def search_root(self, path: str) -> str:
        """Where find and grep start: relative inside the workspace, so results stay short."""
        target = self.resolve(path)
        inside = target.is_relative_to(self.workspace)
        return str(target.relative_to(self.workspace) if inside else target)

    @contextmanager
    def lock(self, path: str):
        """Serialize changes to one file; different files still change in parallel."""
        target = self.resolve(path, change=True)
        with self.locks_guard:
            lock = self.locks.setdefault(target, threading.RLock())
        with lock:
            yield


def head(text: str) -> tuple[str, bool]:
    """The start of text within MAX_LINES and MAX_BYTES, and whether anything was cut."""
    kept, size = [], 0
    for line in text.split("\n")[:MAX_LINES]:
        size += len(line.encode("utf-8")) + 1
        if size > MAX_BYTES:
            break
        kept.append(line)
    result = "\n".join(kept)
    return result, result != text


def tail(text: str) -> tuple[str, bool]:
    """The end of text within MAX_LINES and MAX_BYTES, and whether anything was cut."""
    kept, size = [], 0
    for line in reversed(text.split("\n")[-MAX_LINES:]):
        size += len(line.encode("utf-8")) + 1
        if size > MAX_BYTES:
            break
        kept.append(line)
    result = "\n".join(reversed(kept))
    return result, result != text


def output_file() -> Path:
    """A private file that keeps a command's complete output when the tool result is cut."""
    directory = Path(tempfile.gettempdir()) / "pycode-output"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / f"{uuid4().hex}.log"
