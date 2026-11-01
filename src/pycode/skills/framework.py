"""Skills framework: component-owned implementation."""

import hashlib
import json
from pathlib import Path
from typing import Protocol, cast

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
from deepagents.middleware.skills import SkillsMiddleware, SkillsState

from pycode.kernel import SessionTree, ToolError
from pycode.kernel.adapters.path_io import file_sha256, owned_io
from pycode.skills.types import skill_scope


class BackendOperations(Protocol):
    receipts: dict[str, str]

    def resolve(self, value: str) -> Path: ...
    def error(self, error: OSError | ToolError | UnicodeError) -> str: ...
    def ls(self, path: str) -> LsResult: ...
    def read(self, file_path: str, offset=0, limit=2000) -> ReadResult: ...
    def grep(self, pattern, path=None, glob=None, *, max_count=None) -> GrepResult: ...
    def glob(self, pattern, path=None) -> GlobResult: ...
    async def agrep(self, pattern, path=None, glob=None, *, max_count=None) -> GrepResult: ...
    async def aglob(self, pattern, path=None) -> GlobResult: ...


class SkillBackend(BackendProtocol):
    def __init__(self, backend: BackendOperations, *, selection=None, prefix=""):
        self.backend = backend
        self.receipts = backend.receipts
        self.read_only = True
        self.selection = selection
        self.prefix = prefix

    def ls(self, path):
        result = self.backend.ls(path)
        if self.selection is None or result.error:
            return result
        filtered = []
        for entry in result.entries or ():
            virtual = self.prefix.rstrip("/") + entry["path"]
            if self.selection.visible(virtual):
                filtered.append(entry)
        result.entries = filtered
        return result

    def verify_version(self, value):
        path = self.resolve(value)
        expected = self.receipts.get(str(path))
        if expected is not None and file_sha256(path) != expected:
            raise ToolError(
                "skill_changed",
                "Skill changed during this run; start a new run to use the new version",
            )
        return path

    def read(self, file_path, offset=0, limit=2000):
        try:
            self.verify_version(file_path)
        except (OSError, ToolError) as error:
            return ReadResult(error=self.error(error))
        result = self.backend.read(file_path, offset, limit)
        if not result.error and result.file_data is not None and Path(file_path).name == "SKILL.md":
            result.file_data["content"] += self.script_instructions(file_path)
        return result

    def script_instructions(self, file_path):
        return (
            "\n\nSkill source directory: "
            + str(self.resolve(file_path).parent)
            + "\nRead supporting files through the Skill mount. Run scripts with Bash using "
            "their physical paths in this directory; normal command permissions apply."
        )

    def download_files(self, paths):
        results = []
        for value in paths:
            try:
                path = self.verify_version(value)
                with path.open("rb") as source:
                    data = source.read(256 * 1024 + 1)
                if len(data) > 256 * 1024:
                    raise ToolError("skill_too_large", "Skill file exceeds 256 KiB")
                self.receipts[str(path)] = hashlib.sha256(data).hexdigest()
                if path.name == "SKILL.md":
                    data += self.script_instructions(value).encode("utf-8")
                results.append(FileDownloadResponse(path=value, content=data))
            except (OSError, ToolError, UnicodeError, ValueError, KeyError):
                results.append(FileDownloadResponse(path=value, error="permission_denied"))
        return results

    def resolve(self, value):
        return self.backend.resolve(value)

    def error(self, error):
        return self.backend.error(error)

    async def als(self, path):
        return await owned_io(self.ls, path)

    async def aread(self, file_path, offset=0, limit=2000):
        return await owned_io(self.read, file_path, offset, limit)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return self.backend.grep(pattern, path, glob, max_count=max_count)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return await self.backend.agrep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern, path=None):
        return self.backend.glob(pattern, path)

    async def aglob(self, pattern, path=None):
        return await self.backend.aglob(pattern, path)

    async def adownload_files(self, paths):
        return await owned_io(self.download_files, paths)

    def write(self, file_path, content):
        return WriteResult(error="permission_denied: This backend is read only")

    async def awrite(self, file_path, content):
        return self.write(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return EditResult(error="permission_denied: This backend is read only")

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        return self.edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files):
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]

    async def aupload_files(self, files):
        return self.upload_files(files)


class PycodeSkillsMiddleware(SkillsMiddleware):
    def __init__(self, *, selection=None, model_boundary=None, **options):
        super().__init__(**options)
        self.selection = selection
        self.loaded_revision = selection.revision if selection is not None else 0
        self.model_boundary = model_boundary

    @property
    def name(self):
        # Replace the harness's SkillsMiddleware slot; there is one loader.
        return "SkillsMiddleware"

    async def abefore_model(self, state, runtime):
        updates = {}
        if self.selection is not None and self.selection.revision != self.loaded_revision:
            loaded = await super().abefore_agent({**state, "skills_metadata": None}, runtime, {})
            updates.update(loaded or {})
            self.loaded_revision = self.selection.revision
        pinned = await super().abefore_model(cast(SkillsState, {**state, **updates}), runtime)
        updates.update(pinned or {})
        return updates or None

    async def awrap_model_call(self, request, handler):
        async def disclosed_request(current):
            if self.model_boundary is not None:
                current = await self.model_boundary(current)
            return await handler(current)

        return await super().awrap_model_call(request, disclosed_request)


# Claude-style allowed-tools names mapped to Pi's builtin tools.
ALIASES = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Grep": "grep",
    "Glob": "find",
    "Bash": "bash",
}


class SkillsScopeConstraint:
    def __init__(self, history):
        self.history = history

    async def allowed(self):
        snapshot = await self.history.snapshot()
        scope = skill_scope(SessionTree(snapshot.index).path(snapshot.leaf_id))
        if scope is None:
            return None
        if any(not isinstance(name, str) for name in scope):
            raise ToolError("invalid_skill_scope", "Skill tool names must be strings")
        return {ALIASES.get(name, name) for name in scope} | {"SkillSearch", "ToolSearch"}

    async def authorize(self, context, spec, call):
        await self.check_call(call)

    async def check_call(self, call):
        allowed = await self.allowed()
        instruction_read = call.name == "read" and call.arguments["path"].startswith("/skills/")
        if allowed is not None and call.name not in allowed and not instruction_read:
            raise ToolError("skill_tool_not_allowed", "Active Skill scope excludes this tool")

    async def filter_request(self, request):
        allowed = await self.allowed()
        if allowed is None:
            return request
        tools = []
        for tool in request.tools:
            name = (
                tool.name if not isinstance(tool, dict) else tool.get("function", tool).get("name")
            )
            if name in allowed or name == "read":
                # Loading/reloading instructions remains possible even when the
                # active Skill excludes ordinary workspace reads. authorize()
                # still permits only the exact Skill-path exception.
                tools.append(tool)
        return request.override(tools=tools)


class SkillsFrameworkExtension:
    prepares_model_boundary = True

    def __init__(self, selection, sources, constraint):
        self.selection = selection
        self.sources, self.constraint = sources, constraint

    def middleware(self, context, backend, model_boundary, tool_converter):
        return PycodeSkillsMiddleware(
            selection=self.selection,
            model_boundary=model_boundary,
            backend=backend,
            sources=list(self.sources),
        )

    async def filter_request(self, request):
        return (
            await self.constraint.filter_request(request)
            if self.constraint is not None
            else request
        )


class SkillsToolExtension:
    def __init__(self, catalog, selection, constraint=None):
        self.catalog, self.selection = catalog, selection
        self.constraint = constraint
        # SKILL.md entries of read calls, from before_call to after_tool_call.
        self.reading: dict[str, dict] = {}

    async def validate_call(self, context, call):
        if self.constraint is not None:
            await self.constraint.check_call(call)

    async def before_call(self, context, call):
        await self.validate_call(context, call)
        if call.name == "read" and call.arguments["path"].startswith("/skills/"):
            entry = await self.catalog.entry_at_virtual_path(call.arguments["path"])
            if entry is not None:
                self.reading[call.id] = {
                    "name": entry.name,
                    "path": call.arguments["path"],
                    "content_hash": entry.content_hash,
                    "allowed_tools": list(entry.allowed_tools),
                }

    def after_tool_call(self, call, result):
        if call.name == "read" and call.arguments["path"].startswith("/skills/"):
            skill = self.reading.pop(call.id, None)
            if not result.is_error:
                details = {**result.details, "protected_content": True}
                if skill is not None:
                    details["skill"] = skill
                return {"details": details}
        return None

    async def after_persist(self, context, call, result, entry_id):
        if result.is_error:
            return
        if call.name == "SkillSearch" and self.selection.dispatch:
            selected = result.structured_content
            if selected is None:
                selected = json.loads(result.text)
            if not isinstance(selected, list):
                raise TypeError("SkillSearch must return a list of skill records")
            self.selection.select([item["path"] for item in selected])
