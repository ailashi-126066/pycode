"""Deep Agents file backend with workspace scope, version receipts and ripgrep.

Source: the existing pycode file adapters and their fixed upstream source notices
in docs/source-map.md. Framework schemas belong to FilesystemMiddleware.
"""

import asyncio
import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import IO, Literal, cast, overload

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
from deepagents.backends.utils import InvalidGlobPatternError, compile_grep_include_glob

from pycode.kernel import MAX_BYTES, ToolError, is_internal_component
from pycode.kernel.adapters.path_io import atomic_write, file_sha256, owned_io
from pycode.tools.adapters.rg_stream import RipgrepMatches


class PycodeBackend(BackendProtocol):
    def __init__(
        self,
        workspace: Path,
        *,
        read_only=False,
        write_roots=None,
        excluded_roots=(),
        rg=None,
        search_timeout=15,
        empty_missing_root=False,
        path_guard=None,
        search_scope=None,
    ):
        self.workspace = workspace.resolve()
        self.path_guard = path_guard
        self.search_scope = search_scope
        self.read_only = read_only
        self.write_roots = (
            None if write_roots is None else tuple(Path(path).resolve() for path in write_roots)
        )
        self.empty_missing_root = empty_missing_root
        excluded = []
        for value in excluded_roots:
            path = Path(value).resolve()
            if path.is_relative_to(self.workspace):
                excluded.append(path)
        self.excluded_roots = tuple(excluded)
        self.rg = rg if rg is not None else shutil.which("rg")
        self.receipts: dict[str, str] = {}
        self.locks: dict[str, threading.RLock] = {}
        self.lock_guard = threading.Lock()
        self.search_timeout = search_timeout

    def resolve(self, value: str) -> Path:
        if self.path_guard is not None:
            self.path_guard(value)
        virtual = PurePosixPath(value)
        if not value.startswith("/") or ".." in virtual.parts or "\\" in value:
            raise ToolError("outside_workspace", "Use an absolute virtual workspace path")
        relative = value.removeprefix("/")
        components = virtual.parts[1:]
        if os.name == "nt":
            components = tuple(part.casefold().rstrip(" .") for part in components)
            if any(":" in part for part in components):
                raise ToolError("outside_workspace", "Windows drive and stream paths are forbidden")
        if any(is_internal_component(part) for part in components):
            raise ToolError("outside_workspace", "Internal project state is not accessible")
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace):
            raise ToolError("outside_workspace", "Path escapes the permitted workspace")
        components = path.relative_to(self.workspace).parts
        if os.name == "nt":
            components = tuple(part.casefold().rstrip(" .") for part in components)
        if any(is_internal_component(part) for part in components):
            raise ToolError("outside_workspace", "Internal project state is not accessible")
        for excluded in self.excluded_roots:
            if path.is_relative_to(excluded):
                raise ToolError("outside_workspace", "Application storage is not accessible")
        return path

    def lock(self, path: Path):
        with self.lock_guard:
            key = str(path)
            if key not in self.locks:
                self.locks[key] = threading.RLock()
            return self.locks[key]

    def check_version(self, path: Path) -> None:
        expected = self.receipts.get(str(path))
        if not path.exists():
            if expected is not None:
                raise ToolError("file_changed", "Previously read file was removed; inspect again")
            return
        if expected is None:
            raise ToolError("file_not_read", "Read the existing file before changing it")
        if file_sha256(path) != expected:
            raise ToolError("file_changed", "File changed since it was read; read it again")

    def error(self, error: OSError | ToolError | UnicodeError) -> str:
        code = "tool_io_error"
        if isinstance(error, ToolError):
            code = error.code
        elif isinstance(error, UnicodeError):
            code = "binary_file"
        elif isinstance(error, FileNotFoundError):
            code = "file_not_found"
        elif isinstance(error, PermissionError):
            code = "permission_denied"
        elif isinstance(error, IsADirectoryError):
            code = "is_a_directory"
        return f"{code}: {error}"

    def read(self, file_path: str, offset=0, limit=2000) -> ReadResult:
        """Lines split on "\\n" like Pi's read; the read tool applies truncation."""
        if limit <= 0:
            return ReadResult(
                file_data={"content": "", "encoding": "utf-8"}, no_lines_requested=True
            )
        try:
            path = self.resolve(file_path)
            with self.lock(path):
                data = path.read_bytes()
            self.receipts[str(path)] = hashlib.sha256(data).hexdigest()
            lines = data.decode("utf-8", "replace").split("\n")
            if offset >= len(lines):
                raise ToolError(
                    "invalid_offset",
                    f"Offset {offset + 1} is beyond end of file ({len(lines)} lines total)",
                )
            selected = lines[offset : offset + limit]
            end = offset + len(selected)
            return ReadResult(
                file_data={"content": "\n".join(selected), "encoding": "utf-8"},
                start_line=offset + 1,
                end_line=end,
                total_lines=len(lines),
                next_offset=end if end < len(lines) else None,
            )
        except (OSError, ToolError) as error:
            return ReadResult(error=self.error(error))

    async def aread(self, file_path: str, offset=0, limit=2000):
        return await owned_io(self.read, file_path, offset, limit)

    def check_write_scope(self, path):
        if self.write_roots is not None and not any(
            path.is_relative_to(root) for root in self.write_roots
        ):
            raise ToolError("outside_task_scope", "File is outside the task write scope")

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            if self.read_only:
                raise ToolError("permission_denied", "This backend is read only")
            path = self.resolve(file_path)
            self.check_write_scope(path)
            with self.lock(path):
                self.check_version(path)
                encoded = content.encode("utf-8")
                atomic_write(path, encoded)
                self.receipts[str(path)] = hashlib.sha256(encoded).hexdigest()
            return WriteResult(path=file_path)
        except (OSError, ToolError, UnicodeError) as error:
            return WriteResult(error=self.error(error))

    async def awrite(self, file_path: str, content: str):
        return await owned_io(self.write, file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False) -> EditResult:
        try:
            if self.read_only:
                raise ToolError("permission_denied", "This backend is read only")
            if not old_string or old_string == new_string:
                raise ToolError("invalid_edit", "Provide a nonempty, changed replacement")
            path = self.resolve(file_path)
            self.check_write_scope(path)
            with self.lock(path):
                self.check_version(path)
                text = path.read_bytes().decode("utf-8")
                count = text.count(old_string)
                if not count or (not replace_all and count != 1):
                    raise ToolError("edit_not_unique", f"Expected an exact match; found {count}")
                replaced = text.replace(old_string, new_string, -1 if replace_all else 1)
                encoded = replaced.encode("utf-8")
                atomic_write(path, encoded)
                self.receipts[str(path)] = hashlib.sha256(encoded).hexdigest()
            return EditResult(path=file_path, occurrences=count)
        except (OSError, ToolError, UnicodeError) as error:
            return EditResult(error=self.error(error))

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        return await owned_io(self.edit, file_path, old_string, new_string, replace_all)

    def ls(self, path: str) -> LsResult:
        try:
            directory = self.resolve(path)
            if self.empty_missing_root and directory == self.workspace and not directory.exists():
                return LsResult(entries=[])
            entries = []
            for child in sorted(directory.iterdir()):
                try:
                    resolved = self.resolve("/" + child.relative_to(self.workspace).as_posix())
                except ToolError:
                    continue
                entries.append(
                    {
                        "path": "/" + resolved.relative_to(self.workspace).as_posix(),
                        "is_dir": child.is_dir(),
                        "size": child.stat().st_size,
                    }
                )
            return LsResult(entries=entries)
        except (OSError, ToolError) as error:
            return LsResult(error=self.error(error))

    async def als(self, path):
        return await owned_io(self.ls, path)

    def search_command(self, pattern, path, include, *, files=False):
        if self.rg is None:
            raise ToolError("rg_unavailable", "Install ripgrep and put rg on PATH")
        if include and (".." in PurePosixPath(include).parts or "\\" in include):
            raise ToolError("invalid_glob", "Glob must stay inside the search root")
        base = self.resolve(path or "/")
        targets = ["."]
        if self.search_scope is not None:
            # Restricted backends constrain the files before rg opens any content.
            # Result filtering alone cannot enforce an exact active Plan path.
            base = self.workspace
            targets = [
                self.resolve(value).relative_to(base).as_posix()
                for value in self.search_scope(path or "/")
            ]
            if include:
                try:
                    excluded = include.startswith("!")
                    matches = compile_grep_include_glob(include[1:] if excluded else include)
                    targets = [target for target in targets if matches(target) != excluded]
                except InvalidGlobPatternError as error:
                    raise ToolError("invalid_glob", str(error)) from error
            if not targets:
                return None, base
        command = [self.rg, "--no-require-git", "--sort", "path", "--null", "--color", "never"]
        state_glob = "--iglob" if os.name == "nt" else "--glob"
        command.extend([state_glob, "!.git/**", state_glob, "!.pycode/**"])
        for excluded in self.excluded_roots:
            if excluded.is_relative_to(base):
                command.extend(["--glob", "!" + excluded.relative_to(base).as_posix() + "/**"])
        if include:
            if ".." in PurePosixPath(include).parts or "\\" in include:
                raise ToolError("invalid_glob", "Glob must stay inside the search root")
            command.extend(["--glob", include])
        if include and not any(part.startswith(".") for part in PurePosixPath(include).parts):
            command.extend(["--glob", "!.*", "--glob", "!**/.*", "--glob", "!**/.*/**"])
        if files:
            command.extend(["--files", *targets])
        else:
            if "(?<=" in pattern or "(?<!" in pattern:
                command.append("--pcre2")
            command.extend(
                ["--line-number", "--with-filename", "--no-heading", "--", pattern, *targets]
            )
        return command, base

    @overload
    def search(
        self, pattern, path, include, *, files: Literal[False] = False, max_count=None
    ) -> GrepResult: ...

    @overload
    def search(
        self, pattern, path, include, *, files: Literal[True], max_count=None
    ) -> GlobResult: ...

    def search(
        self, pattern, path, include, *, files: bool = False, max_count=None
    ) -> GrepResult | GlobResult:
        result_type = GlobResult if files else GrepResult
        try:
            command, base = self.search_command(pattern, path, include, files=files)
            if command is None:
                return result_type(matches=[])
            if self.empty_missing_root and base == self.workspace and not base.exists():
                return result_type(matches=[])
            parser = RipgrepMatches(
                base, self.workspace, self.resolve, files=files, max_count=max_count
            )
            process = subprocess.Popen(
                command,
                cwd=base,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, ToolError, UnicodeError) as error:
            return result_type(error=self.error(error), truncated=True)
        # Both were requested as PIPE.
        stdout, stderr = cast(IO[bytes], process.stdout), cast(IO[bytes], process.stderr)
        diagnostics = bytearray()
        timed_out = threading.Event()

        def errors():
            while True:
                chunk = stderr.read(8192)
                if not chunk:
                    return
                diagnostics.extend(chunk[: max(0, MAX_BYTES - len(diagnostics))])

        def stop():
            timed_out.set()
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        reader = threading.Thread(target=errors)
        timer = threading.Timer(self.search_timeout, stop)
        reader.start()
        timer.start()
        failure = None
        try:
            while True:
                data = stdout.read(8192)
                if not data:
                    break
                parser.feed(data)
            parser.finish()
        except (ToolError, UnicodeError, OSError) as error:
            failure = "search_incomplete: " + str(error)
            if process.poll() is None:
                process.kill()
        finally:
            process.wait()
            reader.join()
            timer.cancel()
            timer.join()
            stdout.close()
            stderr.close()
        if timed_out.is_set():
            failure = "search_incomplete: rg timed out"
        elif process.returncode not in {0, 1}:
            failure = "search_incomplete: " + diagnostics.decode("utf-8", "replace").strip()
        return parser.result(failure)

    @overload
    async def asearch(
        self, pattern, path, include, *, files: Literal[False] = False, max_count=None
    ) -> GrepResult: ...

    @overload
    async def asearch(
        self, pattern, path, include, *, files: Literal[True], max_count=None
    ) -> GlobResult: ...

    async def asearch(
        self, pattern, path, include, *, files: bool = False, max_count=None
    ) -> GrepResult | GlobResult:
        result_type = GlobResult if files else GrepResult
        try:
            command, base = self.search_command(pattern, path, include, files=files)
            if command is None:
                return result_type(matches=[])
            if self.empty_missing_root and base == self.workspace and not base.exists():
                return result_type(matches=[])
            process = await asyncio.create_subprocess_exec(
                *command, cwd=base, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except (OSError, ToolError) as error:
            return result_type(error=self.error(error), truncated=True)
        parser = RipgrepMatches(
            base, self.workspace, self.resolve, files=files, max_count=max_count
        )
        # Both were requested as PIPE.
        stdout = cast(asyncio.StreamReader, process.stdout)
        stderr = cast(asyncio.StreamReader, process.stderr)
        diagnostics = bytearray()

        async def errors():
            while True:
                chunk = await stderr.read(8192)
                if not chunk:
                    return
                diagnostics.extend(chunk[: max(0, MAX_BYTES - len(diagnostics))])

        reader = asyncio.create_task(errors())
        failure = None
        try:
            async with asyncio.timeout(self.search_timeout):
                while True:
                    data = await stdout.read(8192)
                    if not data:
                        break
                    parser.feed(data)
                parser.finish()
                await process.wait()
                await asyncio.shield(reader)
            if process.returncode not in {0, 1}:
                failure = "search_incomplete: " + diagnostics.decode("utf-8", "replace").strip()
        except TimeoutError:
            failure = "search_incomplete: rg timed out"
        except (ToolError, OSError, UnicodeError) as error:
            failure = "search_incomplete: " + str(error)
        finally:
            if process.returncode is None:
                process.kill()
            while await stdout.read(8192):
                pass
            await process.wait()
            await reader
        return parser.result(failure)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return self.search(pattern, path, glob, max_count=max_count)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        return await self.asearch(pattern, path, glob, max_count=max_count)

    def glob(self, pattern, path=None):
        return self.search("", path, pattern, files=True)

    async def aglob(self, pattern, path=None):
        return await self.asearch("", path, pattern, files=True)

    def download_files(self, paths):
        results = []
        for value in paths:
            try:
                path = self.resolve(value)
                data = path.read_bytes()
                self.receipts[str(path)] = hashlib.sha256(data).hexdigest()
                results.append(FileDownloadResponse(path=value, content=data))
            except (OSError, ToolError):
                results.append(FileDownloadResponse(path=value, error="permission_denied"))
        return results

    def upload_files(self, files):
        results = []
        for path, content in files:
            result = self.write(path, content.decode("utf-8"))
            results.append(
                FileUploadResponse(path=path, error="permission_denied" if result.error else None)
            )
        return results
