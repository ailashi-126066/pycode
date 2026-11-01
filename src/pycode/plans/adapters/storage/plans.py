"""Exactly one writable Plan Markdown path under project state."""

import hashlib
import re
import stat
from pathlib import Path
from typing import Protocol

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from pycode.kernel import ToolError, owned_io
from pycode.plans.domain.plans import PlanDocument


class BackendOperations(Protocol):
    def resolve(self, value: str) -> Path: ...
    def error(self, error: OSError | ToolError | UnicodeError) -> str: ...
    def ls(self, path: str) -> LsResult: ...
    def read(self, file_path: str, offset=0, limit=2000) -> ReadResult: ...
    def grep(self, pattern, path=None, glob=None, *, max_count=None) -> GrepResult: ...
    def glob(self, pattern, path=None) -> GlobResult: ...
    async def agrep(self, pattern, path=None, glob=None, *, max_count=None) -> GrepResult: ...
    async def aglob(self, pattern, path=None) -> GlobResult: ...
    def write(self, file_path: str, content: str) -> WriteResult: ...
    def edit(self, file_path, old_string, new_string, replace_all=False) -> EditResult: ...
    def download_files(self, paths) -> list[FileDownloadResponse]: ...
    def upload_files(self, files) -> list[FileUploadResponse]: ...


class PlanBackend(BackendProtocol):
    def __init__(self, root, *, backend_factory):
        self.active_name = None
        self.backend: BackendOperations = backend_factory(
            root,
            path_guard=self.check_path,
            search_scope=self.search_scope,
            empty_missing_root=True,
        )

    def configure(self, plan_id):
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise ToolError("invalid_plan_id", "Plan identity must be a UUID hex string")
        self.active_name = "/" + plan_id + ".md"

    def check_path(self, value):
        if value != "/" and value != self.active_name:
            raise ToolError(
                "invalid_plan_path", "Only the current Plan Markdown path is accessible"
            )

    def search_scope(self, value):
        self.check_path(value)
        return (self.active_name,) if self.active_name is not None else ()

    def resolve(self, value):
        self.check_path(value)
        return self.backend.resolve(value)

    def write(self, file_path, content):
        if file_path != self.active_name:
            return WriteResult(error="invalid_plan_path: Only the active Plan file is writable")
        return self.backend.write(file_path, content)

    def read(self, file_path, offset=0, limit=2000):
        return self.backend.read(file_path, offset, limit)

    async def aread(self, file_path, offset=0, limit=2000):
        return await owned_io(self.read, file_path, offset, limit)

    def ls(self, path):
        return self.backend.ls(path)

    async def als(self, path):
        return await owned_io(self.ls, path)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return self.backend.grep(pattern, path, glob, max_count=max_count)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return await self.backend.agrep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern, path=None):
        return self.backend.glob(pattern, path)

    async def aglob(self, pattern, path=None):
        return await self.backend.aglob(pattern, path)

    async def awrite(self, file_path, content):
        return await owned_io(self.write, file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return self.backend.edit(file_path, old_string, new_string, replace_all)

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        return await owned_io(self.edit, file_path, old_string, new_string, replace_all)

    def download_files(self, paths):
        return self.backend.download_files(paths)

    async def adownload_files(self, paths):
        return await owned_io(self.download_files, paths)

    def upload_files(self, files):
        return self.backend.upload_files(files)

    async def aupload_files(self, files):
        return await owned_io(self.upload_files, files)


class FilePlanStore:
    def __init__(self, root: Path, *, backend_factory):
        self.root = root.resolve()
        self.backend = PlanBackend(self.root, backend_factory=backend_factory)

    def configure(self, plan_id):
        self.backend.configure(plan_id)

    def deactivate(self):
        self.backend.active_name = None

    def virtual_path(self, plan_id):
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise ToolError("invalid_plan_id", "Plan identity must be a UUID hex string")
        return "/plans/" + plan_id + ".md"

    def storage_path(self, plan_id):
        self.virtual_path(plan_id)
        return str(self.root / (plan_id + ".md"))

    async def read(self, plan_id):
        def read():
            path = self.backend.resolve("/" + plan_id + ".md")
            try:
                with path.open("rb") as source:
                    raw = source.read(65537)
                if len(raw) > 65536:
                    raise ToolError("plan_too_large", "Plan Markdown exceeds 64 KiB")
                content = raw.decode("utf-8")
                if not content.strip():
                    raise ToolError("empty_plan", "Plan Markdown must contain a nonempty plan")
                return PlanDocument(content, hashlib.sha256(raw).hexdigest(), str(path))
            except FileNotFoundError as error:
                raise ToolError(
                    "plan_missing", "Write the active Plan Markdown before requesting approval"
                ) from error
            except (OSError, UnicodeError) as error:
                raise ToolError("plan_unreadable", "Cannot read UTF-8 Plan Markdown") from error

        return await owned_io(read)

    async def exists(self, plan_id):
        def check():
            path = self.backend.resolve("/" + plan_id + ".md")
            try:
                return stat.S_ISREG(path.stat().st_mode)
            except FileNotFoundError:
                return False
            except OSError as error:
                raise ToolError("plan_unreadable", "Cannot inspect active Plan Markdown") from error

        return await owned_io(check)
