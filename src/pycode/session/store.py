# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import get_args
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import PutOp
from langgraph.store.sqlite.base import SqliteStore
from pydantic import TypeAdapter, ValidationError

from pycode.kernel import (
    CustomKind,
    EntryDraft,
    FileLease,
    Message,
    ModelCapabilities,
    ModelSelection,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    SessionTree,
    StorageError,
    SystemTiming,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    UsageRecord,
    freeze_value,
    json_dumps,
    owned_io,
    plain_data,
)
from pycode.session.inputs import queue_from_snapshot
from pycode.session.restart import run_belongs_to_session


class StorageLayout:
    def __init__(self, workspace: Path, user_data: Path) -> None:
        normalized = os.path.normcase(str(workspace.resolve()))
        key = hashlib.sha256(normalized.encode()).hexdigest()[:24]
        self.project = user_data / "projects" / key
        self.sessions = self.project / "sessions"
        self.runtime_database = self.project / "runtime.sqlite"


def encode_header(header: SessionHeader) -> bytes:
    return encode({"record_type": "header", **plain_data(header)})


def encode_entry(entry: SessionEntry) -> bytes:
    return encode(plain_data(entry))


def encode(data: dict) -> bytes:
    return (
        json_dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def reject_unknown_fields(payload, record_type):
    if not isinstance(payload, dict) or set(payload) - {
        field.name for field in fields(record_type)
    }:
        raise ValueError("Unsupported record fields")


def decode_header(data: bytes) -> SessionHeader:
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("Session header must be an object")
        if payload.get("record_type") != "header" or payload.get("application") != "pycode":
            raise ValueError("Not a native session header")
        version = payload.get("schema_version")
        if type(version) is not int or version != 1:
            raise ValueError("Unsupported native session schema")
        payload.pop("record_type")
        reject_unknown_fields(payload, SessionHeader)
        return TypeAdapter(SessionHeader).validate_json(json_dumps(payload), strict=True)
    except (ValueError, TypeError, ValidationError) as error:
        raise StorageError(
            "invalid_session_header", "Invalid or unsupported native session header"
        ) from error


CONTENT_BLOCKS = {"text": TextBlock, "thinking": ThinkingBlock, "tool_call": ToolCall}


def upgrade_entry(payload: dict) -> dict:
    """One-way read upgrade of records written before 11.1; writes use only the new format."""
    message = payload.get("message")
    if isinstance(message, dict) and "text" in message:
        thinking = [{**block, "type": "thinking"} for block in message.pop("thinking")]
        text = message.pop("text")
        calls = [{**call, "type": "tool_call"} for call in message.pop("tool_calls")]
        message["content"] = (
            [*thinking, *([{"type": "text", "text": text}] if text else []), *calls]
            if thinking or calls
            else text
        )
        if message["role"] == "tool":
            message["role"] = "tool_result"
        if message["stop_reason"] == "tool_calls":
            message["stop_reason"] = "tool_use"
        if message["stop_reason"] == "content_filter":
            message.update(stop_reason="error", error="provider_content_filter")
        if message["tool_result"] is not None:
            message["tool_result"].pop("terminate")
    # Records written before ToolSpec.replay existed only came from non-read tools.
    data = payload.get("data")
    if isinstance(data, dict) and data.get("kind") == "tool_started":
        data.setdefault("replay", "never")
    # Compactions written before file tracking (11.1 section 8.3) have no details.
    if payload.get("type") == "compaction" and isinstance(data, dict):
        data.setdefault("details", {"read_files": [], "modified_files": []})
    # PlanState.approved_hash was dropped; the approved sha256 is in the plan_decision record.
    if payload.get("type") == "plan_state" and isinstance(data, dict):
        data.pop("approved_hash", None)
    # 11.1 section 31: output_id gave way to details["full_output_path"].
    if isinstance(message, dict) and message["tool_result"] is not None:
        message["tool_result"].pop("output_id", None)
    return payload


REMOVED_ENTRY_TYPES = frozenset(
    {
        "team_agent",
        "team_parent",
        "team_transition",
        "hook_call",
        "hook_result",
        "memory_load",
        "memory_recall_error",
        "plan_review",
        "plan_decision",
        "skill_install",
        "skill_load",
        "session_deleted",
        "session_restored",
    }
)


REMOVED_CUSTOM_KINDS = frozenset(
    {"revert_started", "revert_completed", "revert_recovery", "file_snapshot"}
)


def decode_entries(records) -> tuple[tuple[SessionEntry, ...], dict[str, str | None]]:
    """Decode stored records in order; children of a skipped record move to its parent."""
    skipped: dict[str, str | None] = {}
    entries = []
    for record in records:
        try:
            parent = skipped.get(record["parent_id"], record["parent_id"])
            removed = record["type"] in REMOVED_ENTRY_TYPES or (
                record["type"] == "custom" and record["data"].get("kind") in REMOVED_CUSTOM_KINDS
            )
        except (KeyError, TypeError) as error:
            raise StorageError("invalid_session_entry", "Invalid native session entry") from error
        if removed:
            skipped[record["id"]] = parent
            continue
        entries.append(decode_entry(encode({**record, "parent_id": parent})))
    return tuple(entries), skipped


def decode_entry(data: bytes) -> SessionEntry:
    try:
        payload = json.loads(data)
        reject_unknown_fields(payload, SessionEntry)
        payload = upgrade_entry(payload)
        if payload.get("selection") is not None:
            validate_selection_fields(payload["selection"])
        message = payload.get("message")
        if message is not None:
            reject_unknown_fields(message, Message)
            if isinstance(message["content"], list):
                for block in message["content"]:
                    if not isinstance(block, dict) or block.get("type") not in CONTENT_BLOCKS:
                        raise ValueError("Unsupported content block")
                    reject_unknown_fields(block, CONTENT_BLOCKS[block["type"]])
            for attachment in message.get("attachments", []):
                if set(attachment) - {"media_type", "reference", "metadata"}:
                    raise ValueError("Unsupported attachment fields")
            if message.get("tool_result") is not None:
                reject_unknown_fields(message["tool_result"], ToolResult)
            if message.get("usage") is not None:
                reject_unknown_fields(message["usage"], UsageRecord)
            if message.get("model_selection") is not None:
                validate_selection_fields(message["model_selection"])
        entry = TypeAdapter(SessionEntry).validate_json(json_dumps(payload), strict=True)
        validate_entry(entry)
        return entry
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as error:
        raise StorageError("invalid_session_entry", "Invalid native session entry") from error


def validate_selection_fields(selection):
    reject_unknown_fields(selection, ModelSelection)
    if selection.get("capabilities") is not None:
        reject_unknown_fields(selection["capabilities"], ModelCapabilities)


def validate_entry(entry: SessionEntry) -> None:
    if not entry.id or not entry.timestamp:
        raise ValueError("Entry identity and timestamp are required")
    # Unknown entry types are rejected by the EntryType Literal during validation.
    if entry.type == "custom" and entry.data.get("kind") not in get_args(CustomKind):
        raise ValueError("Unknown custom entry kind")
    if entry.type == "custom" and entry.data.get("kind") == "run_start":
        owner = entry.data.get("execution_session_id")
        if not isinstance(owner, str) or not owner:
            raise ValueError("Run start requires its explicit execution session")
    if entry.type == "message":
        message = entry.message
        if message is None or not message.id:
            raise ValueError("Completed message requires a stable ID")
        if message.role == "tool_result" and message.tool_result is None:
            raise ValueError("Tool message requires its paired result")
        if message.role != "tool_result" and message.tool_result is not None:
            raise ValueError("Only tool messages can contain a tool result")
        if message.role != "assistant" and (message.tool_calls or message.thinking):
            raise ValueError("Only assistant messages can contain thinking or tool calls")
    elif entry.type == "input_queued":
        if (
            entry.message is None
            or entry.message.role != "user"
            or not entry.message.id
            or not entry.run_id
            or entry.data.get("queue_kind") not in ("steer", "follow_up")
            or entry.message.tool_result is not None
            or entry.message.tool_calls
            or entry.message.thinking
        ):
            raise ValueError("Queued input requires identified user, run and queue kind")
    elif entry.type == "model_change":
        if entry.selection is None:
            raise ValueError("Model change requires its selection snapshot")
    elif entry.type == "thinking_level_change":
        if not isinstance(entry.data.get("thinking"), str):
            raise ValueError("Thinking change requires a level")
    elif entry.type == "context_edit":
        if not isinstance(entry.data.get("target_id"), str) or "replacement" not in entry.data:
            raise ValueError("Context edit requires a target and explicit replacement")
        if entry.data["replacement"] is not None and not isinstance(entry.data["replacement"], str):
            raise ValueError("Context replacement must be text or an omission")
    elif entry.type == "compaction":
        if not isinstance(entry.data.get("summary"), str) or not isinstance(
            entry.data.get("first_kept_entry_id"), str
        ):
            raise ValueError("Compaction requires summary and retained boundary")
    elif entry.type == "custom_message":
        if not isinstance(entry.data.get("text"), str):
            raise ValueError("Custom message requires model-visible text")


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
        self._closed_input_runs: set[str] = set()
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

    def snapshot_locked(self):
        self.load()
        return SessionSnapshot(
            self._header,
            tuple(self._entries_by_id.values()),
            self._leaf,
            self._revision,
            self._record_count,
            (),
            MappingProxyType(dict(self._entries_by_id)),
            tuple(self._active_path_ids),
        )

    async def snapshot(self):
        return await self.run_io(self.snapshot_locked)

    async def finish_input_boundary(self, run_id):
        def finish():
            pending = queue_from_snapshot(self.snapshot_locked(), run_id)
            if not pending.has("steer") and not pending.has("follow_up"):
                self._closed_input_runs.add(run_id)
            return pending

        return await self.run_io(finish)

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
            validate_entry(entry)
            entry = freeze_value(entry)
            if entry.id in self._entries_by_id:
                raise StorageError("entry_id_conflict", "Entry ID already exists")
            if entry.type == "input_queued":
                if entry.run_id in self._closed_input_runs:
                    raise StorageError(
                        "input_admission_closed", "Task is ending; submit a new task"
                    )
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
