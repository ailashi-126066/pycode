# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pycode.kernel import EntryDraft, StorageError, plain_data
from pycode.session.store import SqliteSessionRepository


def validate_title(value):
    if not isinstance(value, str):
        raise StorageError("invalid_session_name", "Session name must be text")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise StorageError("invalid_session_name", "Session name must be valid UTF-8") from error
    title = " ".join(value.split())
    if not title or len(title) > 80:
        raise StorageError(
            "invalid_session_name", "Session name must contain one to eighty characters"
        )
    for character in title:
        if ord(character) < 32 or ord(character) == 127:
            raise StorageError(
                "invalid_session_name", "Session name cannot contain control characters"
            )
    return title


def session_title(snapshot):
    title = None
    for entry in snapshot.entries:
        if entry.type == "session_metadata" and "title" in entry.data:
            title = entry.data["title"]
    return title


def initial_title(text):
    title = " ".join(text.split())[:80]
    if not title:
        return None
    return validate_title(title)


@dataclass(frozen=True)
class WorkspaceStatus:
    path: str
    exists: bool
    is_git: bool
    state_path: str
    snapshot_available: bool


@dataclass(frozen=True)
class ApplicationStatus:
    name: str
    version: str
    workspace: WorkspaceStatus


class FileWorkspaceInspector:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def inspect(self) -> WorkspaceStatus:
        exists = self._workspace.is_dir()
        is_git = False
        if exists:
            for directory in (self._workspace, *self._workspace.parents):
                git_marker = directory / ".git"
                if git_marker.is_dir() or git_marker.is_file():
                    is_git = True
                    break
        return WorkspaceStatus(
            path=str(self._workspace),
            exists=exists,
            is_git=is_git,
            state_path=str(self._workspace / ".pycode"),
            snapshot_available=is_git,
        )


class ApplicationService:
    def __init__(self, workspace: FileWorkspaceInspector, version: str) -> None:
        self._workspace = workspace
        self._version = version

    def status(self) -> ApplicationStatus:
        return ApplicationStatus(
            name="pycode", version=self._version, workspace=self._workspace.inspect()
        )


class SessionService:
    def __init__(self, repository: SqliteSessionRepository):
        self._repository = repository

    async def list_sessions(self):
        sessions = []
        for header in await self._repository.list_headers():
            if header.session_id.startswith("team-"):
                continue
            store = await self._repository.open(header.session_id)
            try:
                snapshot = await store.snapshot()
                title = session_title(snapshot) or "Untitled session"
                updated = snapshot.entries[-1].timestamp if snapshot.entries else header.created_at
                selection = None
                for entry in snapshot.entries:
                    if entry.selection is not None:
                        selection = entry.selection
                    elif entry.message is not None and entry.message.model_selection is not None:
                        selection = entry.message.model_selection
                sessions.append(
                    {
                        **plain_data(header),
                        "title": title,
                        "short_id": header.session_id[:8],
                        "updated_at": updated,
                        "model": plain_data(selection) if selection else None,
                    }
                )
            finally:
                await store.aclose()
        sessions.sort(key=lambda item: (item["updated_at"], item["session_id"]), reverse=True)
        return tuple(sessions)

    async def resolve(self, value):
        headers = await self._repository.list_headers()
        for header in headers:
            if header.session_id == value:
                return value
        matches = []
        for item in await self.list_sessions():
            if item["title"] == value:
                matches.append(item)
        if len(matches) == 1:
            return matches[0]["session_id"]
        if len(matches) > 1:
            identifiers = []
            for item in matches:
                identifiers.append(item["session_id"] + " (" + item["updated_at"] + ")")
            raise StorageError(
                "ambiguous_session_name", "Choose a session ID: " + ", ".join(identifiers)
            )
        raise StorageError("session_missing", "No session has this ID or exact name")

    async def rename(self, value, title):
        identifier = await self.resolve(value)
        store = await self._repository.open(identifier)
        try:
            await name_session(store, title)
            return {"session_id": identifier, "title": validate_title(title)}
        finally:
            await store.aclose()

    async def fork(self, source_id, leaf_id, target_id):
        store = await self._repository.fork(source_id, leaf_id, target_id)
        try:
            snapshot = await store.snapshot()
            title = session_title(snapshot) or source_id
            await name_session(store, title[:73] + " (fork)", automatic=False)
            return plain_data(await store.snapshot())
        finally:
            await store.aclose()


async def name_session(store, title, *, automatic=False):
    title = validate_title(title)
    snapshot = await store.snapshot()
    previous = session_title(snapshot)
    if automatic and previous is not None:
        return previous
    if previous == title:
        return title
    await store.append_entry(
        EntryDraft("session_metadata", data={"title": title, "automatic": automatic})
    )
    return title


async def assign_initial_title(store, message):
    if message.role != "user":
        return
    title = initial_title(message.text)
    if title is not None:
        await name_session(store, title, automatic=True)
