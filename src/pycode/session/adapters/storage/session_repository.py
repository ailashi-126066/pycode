"""Resolve safe session identities; no reference project data is consulted."""

import asyncio
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from pycode.kernel import SessionHeader, SessionTree, StorageError, SystemTiming
from pycode.session.adapters.storage.session import SqliteSessionStore, read_header


class SqliteSessionRepository:
    def __init__(self, directory: Path, workspace: str, timing: SystemTiming) -> None:
        self._directory = directory
        self._workspace = workspace
        self._timing = timing

    async def list_headers(self):
        def read():
            if not self._directory.exists():
                return ()
            headers = []
            for directory in sorted(self._directory.iterdir()):
                path = directory / "session.sqlite"
                if directory.is_symlink() or not directory.is_dir() or not path.is_file():
                    continue
                header = read_header(path)
                if header.workspace != self._workspace or header.session_id != directory.name:
                    raise StorageError(
                        "session_identity_conflict", "Stored session identity differs"
                    )
                headers.append(header)
            return tuple(headers)

        try:
            return await asyncio.to_thread(read)
        except OSError as error:
            raise StorageError("session_list_error", "Cannot read session directory") from error

    async def open(self, session_id: str, *, create: bool = False) -> SqliteSessionStore:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
            raise StorageError(
                "invalid_session_id", "Session ID must be an opaque name, not a path"
            )
        directory = self._directory / session_id
        path = directory / "session.sqlite"

        def header_for():
            if path.exists():
                return read_header(path)
            if not create:
                raise StorageError("session_not_found", f"Session not found: {session_id}")
            created_at = datetime.fromtimestamp(self._timing.now(), timezone.utc).isoformat()
            return SessionHeader(session_id, self._workspace, created_at)

        header = await asyncio.to_thread(header_for)
        if header.session_id != session_id or header.workspace != self._workspace:
            raise StorageError(
                "session_identity_conflict", "Session identity or workspace does not match"
            )
        store = SqliteSessionStore(directory, header, self._timing)
        await store.snapshot()
        return store

    async def fork(self, source_id: str, leaf_id: str | None, target_id: str) -> SqliteSessionStore:
        if source_id == target_id:
            raise StorageError("fork_target_exists", "Fork requires a distinct session identity")
        source = await self.open(source_id)
        try:
            snapshot = await source.snapshot()
            selected = SessionTree(snapshot.index).path(leaf_id)
        finally:
            await source.aclose()
        target = await self.open(target_id, create=True)
        header = replace(
            (await target.snapshot()).header,
            parent_session_id=source_id,
            forked_from_entry_id=leaf_id,
        )
        await target.aclose()
        target = SqliteSessionStore(self._directory / target_id, header, self._timing)
        try:
            await target.import_path(selected, leaf_id)
        except BaseException:
            await target.aclose()
            raise
        return target
