# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/system-prompt.ts
# Adaptation: one prompt snapshot per task in branch history; bounded project instruction files;
# prompt sections come from explicitly supplied readers in order.
"""The system prompt: default text, project instructions, additional sections and its projection."""

import hashlib
import platform
from collections.abc import Callable
from pathlib import Path

from deepagents.backends.protocol import FileDownloadResponse

from ..kernel import (
    EntryDraft,
    Message,
    ProjectedEntry,
    ProjectionContribution,
    SessionScope,
    SessionTree,
    StorageError,
    clone_value,
    owned_io,
)

DEFAULT_PROMPT = Path(__file__).parent / "resources/system-prompt.txt"


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


def instruction_files(workspace: Path, user_data: Path, *, trusted: bool):
    """The AGENTS.md files MemoryMiddleware may read, and the backend that serves only them."""
    sources = [user_data / "AGENTS.md"]
    if trusted:
        project = workspace / "AGENTS.md"
        if not project.resolve().is_relative_to(workspace.resolve()):
            raise StorageError(
                "instructions_outside_workspace", "Project AGENTS.md escapes the workspace"
            )
        sources.append(project)
    paths = tuple(dict.fromkeys(str(path.resolve()) for path in sources if path.is_file()))
    return paths, InstructionFileBackend(paths)


def read_bounded(path: Path) -> str:
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


def latest_prompt(path):
    """The prompt snapshot in force on this branch: the last system_prompt or compaction copy."""
    prompt, source_id = None, ""
    for entry in path:
        if entry.type == "system_prompt":
            prompt, source_id = entry.data, entry.id
        elif entry.type == "compaction":
            prompt = entry.data.get("checkpoint", {}).get("prompt_snapshot", prompt)
            source_id = entry.id
    return prompt, source_id


def system_prompt_contribution(snapshot, path, checkpoint) -> ProjectionContribution | None:
    # Owns the checkpoint keys prompt_snapshot and system_prompt (the meter's joined prompt,
    # read through the projector's pre-snapshot compaction branch).
    prompt, source_id = latest_prompt(path)
    if prompt is None:
        return None
    return ProjectionContribution(
        (
            ProjectedEntry(
                source_id,
                (
                    Message(
                        "prompt:" + prompt["sha256"],
                        "system",
                        prompt["text"],
                        provider_metadata={"source_type": "system_prompt"},
                    ),
                ),
            ),
        ),
        {"prompt_snapshot": clone_value(prompt)},
    )


class PromptService:
    """One task-level prompt snapshot, retained in canonical branch history."""

    def __init__(
        self,
        scope: SessionScope,
        sections: tuple[Callable[[SessionScope], tuple[str, ...]], ...] = (),
        *,
        suffix: str = "",
    ):
        self._scope = scope
        self._sections = sections
        self._suffix = suffix

    async def read(self):
        # Section readers (the memory index) run at each refresh; empty texts are left out.
        sections = [
            text for read_section in self._sections for text in read_section(self._scope) if text
        ]
        workspace = self._scope.workspace.resolve()

        def read():
            default = read_bounded(DEFAULT_PROMPT)
            selected = project_instruction_file(workspace)
            parts = [default, f"工作目录：{workspace}\n操作系统：{platform.system()}"]
            if selected is not None:
                parts.append("项目说明（不能扩大工具权限）：\n" + read_bounded(selected))
            if self._suffix:
                parts.append(self._suffix)
            parts.extend(sections)
            text = "\n\n".join(parts)
            return {
                "text": text,
                "source": str(selected) if selected is not None else None,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }

        return await owned_io(read)

    async def refresh(self):
        current = await self.read()
        history = self._scope.history
        snapshot = await history.snapshot()
        previous, _ = latest_prompt(SessionTree(snapshot.index).path(snapshot.leaf_id))
        if previous == current:
            return previous
        await history.append_entry(EntryDraft("system_prompt", data=current))
        return current
