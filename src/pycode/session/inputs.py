# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pycode.kernel import (
    EntryDraft,
    Message,
    SessionEntry,
    SessionSnapshot,
    SessionTree,
    StorageError,
)

if TYPE_CHECKING:
    from pycode.session.store import SqliteSessionStore


InputKind = Literal["steer", "follow_up"]


def pending_input_entries(snapshot: SessionSnapshot, run_id: str) -> tuple[SessionEntry, ...]:
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    positions = {entry.id: index for index, entry in enumerate(path)}
    responded_through = -1
    for entry in path:
        if entry.type != "message":
            continue
        message = entry.require_message()
        if message.role != "assistant":
            continue
        if message.provider_metadata.get("pycode_run_id") != run_id:
            continue
        source_id = message.provider_metadata.get("pycode_source_leaf_id")
        if not isinstance(source_id, str):
            continue
        responded_through = max(responded_through, positions.get(source_id, -1))
    consumed = {
        entry.require_message().id
        for entry in path[: responded_through + 1]
        if entry.type == "message" and entry.require_message().role == "user"
    }
    return tuple(
        entry
        for entry in path
        if entry.type == "input_queued"
        and entry.run_id == run_id
        and entry.require_message().id not in consumed
    )


class PendingInputs:
    def __init__(self) -> None:
        self._steer: deque[Message] = deque()
        self._follow_up: deque[Message] = deque()

    def submit(self, message: Message, kind: InputKind) -> bool:
        if message.role != "user" or not message.id:
            raise ValueError("Queued input must be a user message with a stable ID")
        if kind not in {"steer", "follow_up"}:
            raise ValueError("Unknown input queue kind")
        queue = self._steer if kind == "steer" else self._follow_up
        queue.append(message)
        return True

    def has(self, kind: InputKind) -> bool:
        queue = self._steer if kind == "steer" else self._follow_up
        return bool(queue)

    def take(self, kind: InputKind, *, one_at_a_time: bool = False) -> tuple[Message, ...]:
        queue = self._steer if kind == "steer" else self._follow_up
        if not queue:
            return ()
        if one_at_a_time:
            return (queue.popleft(),)
        messages = tuple(queue)
        queue.clear()
        return messages


class DurableInputs:
    def __init__(self, store: SqliteSessionStore):
        self._store = store

    async def submit(self, message: Message, kind: InputKind, run_id: str) -> bool:
        if message.role != "user" or not message.id or kind not in ("steer", "follow_up"):
            raise StorageError(
                "invalid_queued_input", "Queued input requires user ID and queue kind"
            )
        identifier = uuid4().hex
        entry = await self._store.append_entry(
            EntryDraft(
                "input_queued",
                id=identifier,
                message=message,
                data={"queue_kind": kind},
                run_id=run_id,
            )
        )
        return entry.id == identifier

    async def pending(self, run_id: str) -> PendingInputs:
        return queue_from_snapshot(await self._store.snapshot(), run_id)

    async def finish_boundary(self, run_id):
        return await self._store.finish_input_boundary(run_id)


def queue_from_snapshot(snapshot, run_id):
    queue = PendingInputs()
    for entry in pending_input_entries(snapshot, run_id):
        queue.submit(entry.require_message(), entry.data["queue_kind"])
    return queue
