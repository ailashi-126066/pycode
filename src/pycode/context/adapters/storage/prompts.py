"""Bounded project instruction loading; no ancestor or repository-wide scan."""

import hashlib
import platform
from pathlib import Path

from deepagents.backends.protocol import FileDownloadResponse

from pycode.kernel import StorageError, owned_io


def project_instruction_file(workspace: Path) -> Path | None:
    """The first project instruction file by priority, resolved inside the workspace."""
    workspace = workspace.resolve()
    # AGENTS.md is loaded separately, through MemoryMiddleware and the trust boundary.
    for name in ("agent.md", "AGENT.md"):
        path = workspace / name
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StorageError(
                "instructions_unreadable", "Cannot inspect project instructions"
            ) from error
        if not path.resolve().is_relative_to(workspace):
            raise StorageError(
                "instructions_outside_workspace",
                "Project instructions escape the workspace",
            )
        return path
    return None


class InstructionFileBackend:
    """Read-only, allowlisted file downloads for Deep Agents MemoryMiddleware (AGENTS.md)."""

    def __init__(self, paths):
        self.paths = {str(path) for path in paths}

    def download_files(self, paths):

        responses = []
        for name in paths:
            path = Path(name)
            if name not in self.paths:
                responses.append(FileDownloadResponse(path=name, error="permission_denied"))
                continue
            try:
                with path.open("rb") as source:
                    raw = source.read(65537)
            except FileNotFoundError:
                responses.append(FileDownloadResponse(path=name, error="file_not_found"))
                continue
            except IsADirectoryError:
                responses.append(FileDownloadResponse(path=name, error="is_directory"))
                continue
            except OSError as error:
                raise StorageError(
                    "instructions_unreadable", f"Cannot read AGENTS.md: {path}"
                ) from error
            if len(raw) > 65536:
                raise StorageError("instructions_too_large", f"AGENTS.md exceeds 64 KiB: {path}")
            try:
                raw.decode("utf-8")
            except UnicodeError as error:
                raise StorageError(
                    "instructions_unreadable", f"AGENTS.md is not UTF-8: {path}"
                ) from error
            responses.append(FileDownloadResponse(path=name, content=raw))
        return responses

    async def adownload_files(self, paths):
        return await owned_io(self.download_files, list(paths))


class ProjectPromptSource:
    def __init__(self, workspace: Path, default_path: Path, suffix="", sections=()):
        self.workspace = workspace.resolve()
        self.default_path = default_path
        self.suffix = suffix
        # Feature sections (the memory index) read at each refresh; an empty one is left out.
        self.sections = sections

    def read_bounded(self, path: Path) -> str:
        try:
            with path.open("rb") as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                raise StorageError(
                    "instructions_too_large", f"Project instructions exceed 64 KiB: {path}"
                )
            return raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise StorageError(
                "instructions_unreadable", f"Cannot read UTF-8 instructions: {path}"
            ) from error

    async def read(self):
        def read():
            default = self.read_bounded(self.default_path)
            selected = project_instruction_file(self.workspace)
            parts = [default, f"工作目录：{self.workspace}\n操作系统：{platform.system()}"]
            if selected is not None:
                parts.append("项目说明（不能扩大工具权限）：\n" + self.read_bounded(selected))
            if self.suffix:
                parts.append(self.suffix)
            parts.extend(text for text in (section() for section in self.sections) if text)
            text = "\n\n".join(parts)
            return {
                "text": text,
                "source": str(selected) if selected is not None else None,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }

        return await owned_io(read)
