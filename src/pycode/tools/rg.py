# Pi 没有，pycode 新增
"""Bounded parsing of ripgrep's NUL filename framing, without JSON submatch arrays."""

from pathlib import Path

from deepagents.backends.protocol import GlobResult, GrepResult

from pycode.kernel import MAX_BYTES, MAX_LINES, ToolError


class RipgrepMatches:
    def __init__(self, base, workspace, resolve, *, files=False, max_count=None):
        self.base = base
        self.workspace = workspace
        self.resolve = resolve
        self.files = files
        self.limit = min(max_count, MAX_LINES) if max_count is not None else MAX_LINES
        self.matches = []
        self.count = 0
        self.bytes = 0
        self.truncated = False
        self.phase = "path"
        self.header = bytearray()
        self.path = None
        self.line = None
        self.text = bytearray()
        self.trailing_cr = 0

    def filename(self):
        try:
            name = self.header.decode("utf-8")
        except UnicodeError as error:
            raise ToolError("invalid_path_encoding", "rg returned a non-UTF-8 filename") from error
        path = self.base / Path(name)
        virtual = "/" + path.relative_to(self.workspace).as_posix()
        self.resolve(virtual)
        self.path = virtual
        self.header.clear()

    def require_path(self) -> str:
        if self.path is None:
            raise ToolError("search_incomplete", "rg match has no filename")
        return self.path

    def finish_match(self):
        path = self.require_path()
        self.count += 1
        if len(self.matches) < self.limit and self.bytes < MAX_BYTES - 512:
            item = (
                {"path": path, "is_dir": False}
                if self.files
                else {
                    "path": path,
                    "line": self.line,
                    "text": self.text.decode("utf-8", "replace"),
                }
            )
            self.matches.append(item)
            self.bytes += len(path.encode()) + len(self.text) + 32
        else:
            self.truncated = True
        self.text.clear()
        self.trailing_cr = 0
        self.path = self.line = None
        self.phase = "path"

    def content(self, piece, ending):
        body = piece.rstrip(b"\r")
        if body:
            self.remember(b"\r" * self.trailing_cr)
            self.remember(body)
            self.trailing_cr = len(piece) - len(body)
        else:
            self.trailing_cr += len(piece)
        if ending:
            self.trailing_cr = 0

    def remember(self, piece):
        remaining = max(0, MAX_BYTES - 512 - self.bytes - len(self.text))
        if len(piece) > remaining:
            self.truncated = True
        if len(self.matches) < self.limit:
            self.text.extend(piece[:remaining])

    def feed(self, data):
        offset = 0
        while offset < len(data):
            if self.phase == "text":
                end = data.find(b"\n", offset)
                stop = len(data) if end < 0 else end
                self.content(data[offset:stop], end >= 0)
                offset = stop + 1
                if end >= 0:
                    self.finish_match()
                continue
            separator = b"\0" if self.phase == "path" else b":"
            end = data.find(separator, offset)
            stop = len(data) if end < 0 else end
            self.header.extend(data[offset:stop])
            if len(self.header) > 16384:
                raise ToolError("search_incomplete", "Malformed or oversized rg path/line header")
            offset = stop + 1
            if end < 0:
                continue
            if self.phase == "path":
                self.filename()
                if self.files:
                    self.finish_match()
                else:
                    self.phase = "line"
            else:
                try:
                    self.line = int(self.header)
                except ValueError as error:
                    raise ToolError("search_incomplete", "Invalid rg line number") from error
                if self.line < 1:
                    raise ToolError("search_incomplete", "Invalid rg line number")
                self.header.clear()
                self.phase = "text"

    def finish(self):
        if self.phase == "text":
            self.finish_match()
        elif self.header or self.phase != "path":
            raise ToolError("search_incomplete", "rg output ended inside a result header")

    def result(self, error=None):
        truncated = self.truncated or error is not None
        if self.files:
            return GlobResult(
                error=error,
                matches=self.matches,
                truncated=truncated,
                truncation_reason="unreadable" if error else "budget" if truncated else None,
            )
        return GrepResult(error=error, matches=self.matches, truncated=truncated)
