# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
# Adaptation: native Python schema and immutable data; no framework message classes.
# Adaptation: preserve parent traversal; reject corrupt parent chains and unknown selected leaves.
"""Completed session history, tree traversal and projection contributions."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pycode.kernel.messages import Message
from pycode.kernel.values import ModelSelection, freeze_value

# Every SessionEntry.type that is written today.
EntryType = Literal[
    "message",
    "custom",
    "custom_message",
    "compaction",
    "context_edit",
    "fork_context",
    "hook_context",
    "input_queued",
    "input_expired",
    "mcp_catalog",
    "model_change",
    "thinking_level_change",
    "tool_set_change",
    "plan_state",
    "session_metadata",
    "skill_catalog",
    "system_prompt",
]

# data["kind"] of a "custom" entry.
CustomKind = Literal[
    "run_start",
    "run_resume",
    "run_wait",
    "run_end",
    "tool_started",
    "tool_approval",
    "file_snapshot",
    "tool_eviction_audit",
    "summary_job",
    "compact_request",
    "revert_started",
    "revert_completed",
    "revert_recovery",
    "skill_created",
    "skill_updated",
    "subagent_start",
    "task_board",
    "plan_decision",
    "skill_install",
]


@dataclass(frozen=True)
class SessionHeader:
    session_id: str
    workspace: str
    created_at: str
    application: Literal["pycode"] = "pycode"
    schema_version: Literal[1] = 1
    parent_session_id: str | None = None
    forked_from_entry_id: str | None = None


@dataclass(frozen=True)
class SessionEntry:
    id: str
    parent_id: str | None
    type: EntryType
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)
    message: Message | None = None
    selection: ModelSelection | None = None
    run_id: str | None = None
    operation_id: str | None = None

    def require_message(self) -> Message:
        """Read a message from an entry whose schema requires a message payload."""
        message = self.message
        if message is None:
            raise ValueError("Session entry requires a message payload: " + self.id)
        return message


@dataclass(frozen=True)
class EntryDraft:
    type: EntryType
    data: dict[str, Any] = field(default_factory=dict)
    message: Message | None = None
    selection: ModelSelection | None = None
    id: str | None = None
    run_id: str | None = None
    operation_id: str | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    header: SessionHeader
    entries: tuple[SessionEntry, ...]
    leaf_id: str | None
    revision: int
    record_count: int
    issues: tuple[str, ...] = ()
    entries_by_id: Mapping[str, SessionEntry] | None = field(
        default=None, compare=False, repr=False, metadata={"serialize": False}
    )
    active_path_ids: tuple[str, ...] = ()

    @property
    def index(self):
        if self.entries_by_id is not None:
            if not self.entries or self.entries_by_id.get(self.entries[-1].id) is self.entries[-1]:
                return self.entries_by_id
        return self.entries


@dataclass(frozen=True)
class ProjectedEntry:
    entry_id: str
    messages: tuple[Message, ...]


@dataclass(frozen=True)
class ProjectedContext:
    leaf_id: str | None
    entries: tuple[ProjectedEntry, ...]
    messages: tuple[Message, ...]
    selection: ModelSelection | None
    thinking: str
    checkpoint: dict[str, Any] = field(default_factory=dict)


class SessionTree:
    def __init__(self, entries):
        if isinstance(entries, Mapping):
            # SessionStore already validated this insertion-ordered index.
            self._index = entries
            return
        self._index = {}
        for entry in entries:
            if not entry.id or entry.id in self._index:
                raise ValueError("Session entry ID is empty or duplicated")
            if entry.parent_id is not None and entry.parent_id not in self._index:
                raise ValueError(f"Session parent must precede its child: {entry.id}")
            self._index[entry.id] = freeze_value(entry)

    def path(self, leaf_id: str | None) -> tuple[SessionEntry, ...]:
        if leaf_id is None:
            return ()
        if leaf_id not in self._index:
            raise ValueError(f"Unknown session leaf: {leaf_id}")
        path = []
        current = self._index[leaf_id]
        while True:
            path.append(current)
            if current.parent_id is None:
                break
            current = self._index[current.parent_id]
        path.reverse()
        return tuple(path)

    def get(self, entry_id: str) -> SessionEntry:
        entry = self._index.get(entry_id)
        if entry is None:
            raise ValueError(f"Unknown session entry: {entry_id}")
        return entry

    def children(self, parent_id):
        children = []
        for entry in self._index.values():
            if entry.parent_id == parent_id:
                children.append(entry)
        return tuple(children)


@dataclass(frozen=True)
class ProjectionContribution:
    entries: tuple[ProjectedEntry, ...] = ()
    checkpoint_updates: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_removals: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "entries", freeze_value(self.entries))
        object.__setattr__(self, "checkpoint_updates", freeze_value(self.checkpoint_updates))
        object.__setattr__(self, "checkpoint_removals", frozenset(self.checkpoint_removals))
