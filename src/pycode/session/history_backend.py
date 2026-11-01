# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
from __future__ import annotations

import asyncio
import fnmatch
import re
from pathlib import PurePosixPath

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from pycode.kernel import MAX_BYTES, MAX_LINES, SessionTree, json_dumps

SEARCH_BYTES = 2 * 1024 * 1024


def matches_filename(filename, pattern):
    normalized = pattern.lstrip("/")
    while normalized.startswith("**/"):
        normalized = normalized[3:]
    return fnmatch.fnmatchcase(filename.lstrip("/"), normalized)


def invalid_glob(pattern):
    return "\\" in pattern or ".." in PurePosixPath(pattern).parts


class ConversationHistoryBackend(BackendProtocol):
    def __init__(self, history):
        self.history = history

    def read(self, file_path, offset=0, limit=2000):
        return ReadResult(error="async_backend_required: use the async Agent entrypoint")

    async def aread(self, file_path, offset=0, limit=2000):
        match = re.fullmatch(r"/([A-Za-z0-9:_-]+)\.json", file_path)
        if match is None:
            return ReadResult(error="history_not_accessible: use a listed entry ID")
        snapshot = await self.history.snapshot()
        selected = None
        for entry in SessionTree(snapshot.index).path(snapshot.leaf_id):
            if entry.id == match.group(1):
                selected = entry
                break
        if selected is None:
            return ReadResult(error="history_not_accessible: entry is outside the current branch")
        text = json_dumps(selected, ensure_ascii=False, indent=2)
        lines = text.split("\n")
        offset = max(0, offset)
        limit = min(2000, max(0, limit))
        pieces = []
        size = 0
        for line in lines[offset : offset + limit]:
            encoded = (line + "\n").encode()
            if size + len(encoded) > 49500:
                pieces.append("[History range limited; inspect the original session.]\n")
                break
            size += len(encoded)
            pieces.append(line + "\n")
        return ReadResult(file_data={"content": "".join(pieces), "encoding": "utf-8"})

    async def als(self, path):
        if path != "/":
            return LsResult(error="history_not_accessible: list the history root")
        snapshot = await self.history.snapshot()
        entries = []
        for entry in SessionTree(snapshot.index).path(snapshot.leaf_id):
            entries.append({"path": "/" + entry.id + ".json", "is_dir": False})
        return LsResult(entries=entries)

    def ls(self, path):
        return LsResult(error="async_backend_required: use the async Agent entrypoint")

    def glob(self, pattern, path=None):
        return GlobResult(error="async_backend_required: use the async Agent entrypoint")

    async def aglob(self, pattern, path=None):
        if invalid_glob(pattern):
            return GlobResult(error="invalid_glob: glob must stay inside the history root")
        listing = await self.als(path or "/")
        if listing.error:
            return GlobResult(error=listing.error)
        selected = [
            entry for entry in listing.entries or () if matches_filename(entry["path"], pattern)
        ]
        return GlobResult(matches=selected[:MAX_LINES], truncated=len(selected) > MAX_LINES)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        return GrepResult(error="async_backend_required: use the async Agent entrypoint")

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        try:
            expression = re.compile(pattern)
        except re.error as error:
            return GrepResult(error="invalid_regex: " + str(error))
        if glob and invalid_glob(glob):
            return GrepResult(error="invalid_glob: glob must stay inside the history root")
        snapshot = await self.history.snapshot()
        entries = SessionTree(snapshot.index).path(snapshot.leaf_id)
        if path not in {None, "/"} and not any(
            "/" + entry.id + ".json" == path for entry in entries
        ):
            return GrepResult(error="history_not_accessible: entry is outside the current branch")
        ceiling = min(MAX_LINES, max(0, max_count)) if max_count is not None else MAX_LINES
        if ceiling == 0:
            return GrepResult(matches=[], truncated=bool(entries))
        matches = []
        scanned = rendered = 0
        for entry in entries:
            filename = "/" + entry.id + ".json"
            if path not in {None, "/"} and filename != path:
                continue
            if glob and not matches_filename(filename, glob):
                continue
            await asyncio.sleep(0)
            for number, line in enumerate(
                json_dumps(entry, ensure_ascii=False, indent=2).splitlines(), 1
            ):
                data = line.encode("utf-8")
                remaining = max(0, SEARCH_BYTES - scanned)
                selected = data[:remaining].decode("utf-8", "ignore")
                scanned += min(len(data), remaining)
                if expression.search(selected):
                    match = {"path": filename, "line": number, "text": selected[:2048]}
                    size = len(json_dumps(match, ensure_ascii=False).encode("utf-8"))
                    if rendered + size > MAX_BYTES:
                        return GrepResult(matches=matches, truncated=True)
                    matches.append(match)
                    rendered += size
                    if len(matches) >= ceiling:
                        return GrepResult(matches=matches, truncated=True)
                if len(data) > remaining or scanned >= SEARCH_BYTES:
                    return GrepResult(matches=matches, truncated=True)
        return GrepResult(matches=matches)

    def write(self, file_path, content):
        return WriteResult(error="permission_denied: history is append-only")

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return EditResult(error="permission_denied: history is append-only")
