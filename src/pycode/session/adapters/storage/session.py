"""Complete record trees in LangGraph Store; execution positions in its checkpointer."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import PutOp
from langgraph.store.sqlite.base import SqliteStore

from pycode.kernel import (
    EntryDraft,
    FileLease,
    Message,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    SessionTree,
    StorageError,
    SystemTiming,
    freeze_value,
    owned_io,
    plain_data,
)
from pycode.session.adapters.storage.session_codec import (
    decode_entries,
    decode_entry,
    decode_header,
    encode_entry,
    encode_header,
)
from pycode.session.domain.execution_recovery import run_belongs_to_session


def open_database(path):
    connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    store = SqliteStore(connection)
    store.setup()
    return store


def read_records(path):
    store = open_database(path)
    try:
        saved = store.get(("session",), "position")
        if saved is None:
            raise StorageError("invalid_session_position", "Session has no stored position")
        metadata = saved.value
        entries = []
        offset = 0
        while True:
            page = store.search(("entries",), limit=1000, offset=offset)
            entries.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
        entries.sort(key=lambda item: item.value["sequence"])
        decoded, skipped = decode_entries(item.value["entry"] for item in entries)
        leaf = metadata["leaf_id"]
        return {**metadata, "leaf_id": skipped.get(leaf, leaf)}, decoded, len(entries)
    finally:
        store.conn.close()


def read_header(path):
    store = open_database(path)
    try:
        saved = store.get(("session",), "position")
        if saved is None:
            raise StorageError("invalid_session_position", "Session has no stored position")
        return decode_header(encode_header(saved.value["header"]))
    finally:
        store.conn.close()


class SqliteSessionStore:
    def __init__(self, directory: Path, header: SessionHeader, timing: SystemTiming):
        self._directory = directory
        self._log = directory / "session.sqlite"
        self._header, self._timing = header, timing
        self._entries_by_id = {}
        self._entries_by_message_id = {}
        self._active_path_ids = []
        self._leaf = None
        self._revision = self._record_count = 0
        self._loaded = self._persisted = self._closed = False
        self._lease = FileLease(directory / "writer.lock")
        self._lock = asyncio.Lock()
        self._database = None
        self._checkpoint_scope = None
        self._checkpointer = None

    async def run_io(self, operation):
        async with self._lock:
            if self._closed:
                raise StorageError("session_closed", "Session store is closed")
            return await owned_io(operation)

    def load(self):
        if self._loaded:
            return
        if self._log.exists():
            metadata, entries, stored = read_records(self._log)
            header = decode_header(encode_header(metadata["header"]))
            if (header.session_id, header.workspace) != (
                self._header.session_id,
                self._header.workspace,
            ):
                raise StorageError("session_identity_conflict", "Session identity differs")
            self._header = header
            self._entries_by_id = {entry.id: freeze_value(entry) for entry in entries}
            self._leaf = metadata["leaf_id"]
            self._revision = metadata["revision"]
            # Skipped records keep their sequence numbers, so new ones follow all stored records.
            self._record_count = stored
            self._active_path_ids = [
                entry.id for entry in SessionTree(self._entries_by_id).path(self._leaf)
            ]
            self._rebuild_message_index()
            self._persisted = True
        self._loaded = True

    async def snapshot(self):
        def read():
            self.load()
            return SessionSnapshot(
                self._header,
                tuple(self._entries_by_id.values()),
                self._leaf,
                self._revision,
                self._record_count,
                (),
                MappingProxyType(self._entries_by_id),
                tuple(self._active_path_ids),
            )

        return await self.run_io(read)

    def acquire_writer(self):
        if self._lease.acquire() and self._log.exists():
            self._loaded = False
        self.load()

    async def claim_writer(self):
        await self.run_io(self.acquire_writer)

    async def checkpoint(self):
        if self._checkpointer is None:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._checkpoint_scope = AsyncSqliteSaver.from_conn_string(
                str(self._directory / "execution.sqlite")
            )
            self._checkpointer = await self._checkpoint_scope.__aenter__()
        return self._checkpointer

    async def append_message(self, message: Message) -> str:
        run_id = message.usage.run_id if message.usage is not None else None
        operation_id = None
        if message.tool_result is not None:
            run_id = message.tool_result.run_id
            operation_id = message.tool_result.operation_id
        entry = await self.append_entry(
            EntryDraft("message", message=message, run_id=run_id, operation_id=operation_id)
        )
        return entry.id

    def _index_message_entry(self, entry) -> None:
        if entry.type not in {"message", "input_queued"} or entry.message is None:
            return
        self._entries_by_message_id.setdefault(entry.message.id, []).append(entry.id)

    def _rebuild_message_index(self) -> None:
        self._entries_by_message_id = {}
        for entry in self._entries_by_id.values():
            self._index_message_entry(entry)

    def _previous_message_entries(self, message_id):
        for entry_id in self._entries_by_message_id.get(message_id, ()):
            yield self._entries_by_id[entry_id]

    async def append_entry(self, draft: EntryDraft) -> SessionEntry:
        def append():
            self.acquire_writer()
            if draft.type == "input_queued":
                queued_message = draft.message
                if queued_message is None:
                    raise StorageError("invalid_session_entry", "Queued input requires its message")
                for previous in self._previous_message_entries(queued_message.id):
                    if previous.type not in {"input_queued", "message"}:
                        continue
                    if previous.message != draft.message or (
                        previous.type == "input_queued"
                        and (previous.run_id != draft.run_id or previous.data != draft.data)
                    ):
                        raise StorageError("input_id_conflict", "Queued input identity changed")
                    active = SessionTree(self._entries_by_id).path(self._leaf)
                    if previous.id not in {entry.id for entry in active}:
                        raise StorageError(
                            "input_branch_conflict", "Input belongs to another branch"
                        )
                    return previous
            if draft.type == "message":
                if draft.message is None:
                    raise StorageError(
                        "invalid_session_entry", "Message entry requires its completed message"
                    )
                for previous in self._previous_message_entries(draft.message.id):
                    if previous.type != "message":
                        continue
                    if previous.message != draft.message:
                        raise StorageError(
                            "input_id_conflict", "Message ID already has different content"
                        )
                    if previous.id not in {
                        entry.id for entry in SessionTree(self._entries_by_id).path(self._leaf)
                    }:
                        raise StorageError(
                            "input_branch_conflict", "Message ID belongs to another branch"
                        )
                    return previous
            identifier = draft.id or uuid4().hex
            entry = SessionEntry(
                identifier,
                self._leaf,
                draft.type,
                datetime.fromtimestamp(self._timing.now(), timezone.utc).isoformat(),
                draft.data,
                draft.message,
                draft.selection,
                draft.run_id,
                draft.operation_id,
            )
            entry = freeze_value(decode_entry(encode_entry(entry)))
            if entry.id in self._entries_by_id:
                raise StorageError("entry_id_conflict", "Entry ID already exists")
            if entry.type == "input_queued":
                self.require_active_run(entry)
            will_persist = self._persisted or (
                entry.type == "message" and entry.require_message().role == "user"
            )
            if will_persist:
                self.write_entry(entry)
            self._entries_by_id[entry.id] = entry
            self._index_message_entry(entry)
            self._active_path_ids.append(entry.id)
            self._leaf = entry.id
            self._revision += 1
            return entry

        return await self.run_io(append)

    def require_active_run(self, entry: SessionEntry) -> None:
        """Queued input races the run's end; the run must still be open on this branch."""
        path = SessionTree(self._entries_by_id).path(self._leaf)
        started = any(
            item.type == "custom"
            and item.data.get("kind") == "run_start"
            and item.run_id == entry.run_id
            and run_belongs_to_session(self._header, item)
            for item in path
        )
        ended = any(
            item.type == "custom"
            and item.data.get("kind") == "run_end"
            and item.run_id == entry.run_id
            for item in self._entries_by_id.values()
        )
        if not started or ended:
            raise StorageError("run_settled", "Queued input has no active owning run")

    async def has_checkpoint(self, run_id):
        if not (self._directory / "execution.sqlite").exists():
            return False
        saver = await self.checkpoint()
        saved = await saver.aget_tuple(
            {"configurable": {"thread_id": f"{self._header.session_id}:{run_id}"}}
        )
        return saved is not None

    def database(self):
        if self._database is None:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._database = open_database(self._log)
        return self._database

    def position(self, leaf, revision):
        return PutOp(
            ("session",),
            "position",
            {
                "header": plain_data(self._header),
                "leaf_id": leaf,
                "revision": revision,
            },
        )

    def write_entry(self, entry):
        pending = [*self._entries_by_id.values(), entry] if not self._persisted else [entry]
        start = self._record_count if self._persisted else 0
        writes = [
            PutOp(("entries",), row.id, {"sequence": start + i, "entry": plain_data(row)})
            for i, row in enumerate(pending)
        ]
        self.database().batch([*writes, self.position(entry.id, self._revision + 1)])
        self._persisted = True
        self._record_count = start + len(pending)

    async def branch(self, leaf_id):
        def change():
            self.acquire_writer()
            path = SessionTree(self._entries_by_id).path(leaf_id)
            if self._persisted:
                self.database().batch([self.position(leaf_id, self._revision + 1)])
            self._active_path_ids = [entry.id for entry in path]
            self._leaf = leaf_id
            self._revision += 1

        await self.run_io(change)

    async def aclose(self):
        if self._closed:
            return
        if self._checkpoint_scope is not None:
            await self._checkpoint_scope.__aexit__(None, None, None)

        def close():
            if self._database is not None:
                self._database.conn.close()
            self._lease.close()

        await self.run_io(close)
        self._closed = True

    async def import_path(self, entries, leaf_id):
        def publish():
            self.acquire_writer()
            if self._persisted or self._entries_by_id:
                raise StorageError("fork_target_exists", "Fork target already contains history")
            path = SessionTree(entries).path(leaf_id)
            writes = [
                PutOp(("entries",), entry.id, {"sequence": i, "entry": plain_data(entry)})
                for i, entry in enumerate(entries)
            ]
            self.database().batch([*writes, self.position(leaf_id, len(entries))])
            self._entries_by_id = {entry.id: freeze_value(entry) for entry in entries}
            self._active_path_ids = [entry.id for entry in path]
            self._leaf, self._revision, self._record_count = leaf_id, len(entries), len(entries)
            self._rebuild_message_index()
            self._persisted = True

        await self.run_io(publish)
