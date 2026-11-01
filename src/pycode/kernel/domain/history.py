# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
# Adaptation: native Python schema and immutable data; no framework message classes.
"""Completed session history, active position and projection contracts."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pycode.kernel.domain.messages import Message
from pycode.kernel.domain.model_identity import ModelSelection

# Every SessionEntry.type that is written today; team_* goes away with the team package.
EntryType = Literal[
    "message",
    "custom",
    "custom_message",
    "compaction",
    "context_edit",
    "fork_context",
    # hook_call/hook_result: read-only, written before 11.1 section 33.1 #3.
    "hook_call",
    "hook_result",
    "hook_context",
    "input_queued",
    "input_expired",
    "mcp_catalog",
    # memory_load/memory_recall_error: read-only, written before 11.1 section 29.
    "memory_load",
    "memory_recall_error",
    "model_change",
    "thinking_level_change",
    "tool_set_change",
    "plan_state",
    # plan_review: read-only, written before 11.1 section 33.1 #11.
    "plan_review",
    "plan_decision",
    "session_metadata",
    "session_deleted",
    "session_restored",
    "skill_catalog",
    "skill_load",
    "skill_install",
    "system_prompt",
    "team_agent",
    "team_parent",
    "team_transition",
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
