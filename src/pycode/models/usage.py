# Pi 没有，pycode 新增
"""Request accounting per run, session, purpose and agent, independent of projected context."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pycode.kernel import SessionTree, plain_data
from pycode.models.requests import SQLiteRequestStore
from pycode.models.types import RequestRecord

if TYPE_CHECKING:
    from pycode.session import SqliteSessionStore

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_miss_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)


@dataclass(frozen=True)
class UsageGroup:
    request_count: int
    settled_requests: int
    pending_requests: int
    tokens: dict[str, int | None]
    known_tokens: dict[str, int | None]
    reported: dict[str, int]


def aggregate_requests(records: tuple[RequestRecord, ...]) -> UsageGroup:
    settled = tuple(record for record in records if record.status != "started")
    tokens, known, reported = {}, {}, {}
    for field in TOKEN_FIELDS:
        values = []
        for record in settled:
            value = getattr(record.usage, field) if record.usage is not None else None
            if value is not None:
                values.append(value)
        reported[field] = len(values)
        known[field] = sum(values) if values else None
        tokens[field] = known[field] if len(values) == len(settled) else None
    return UsageGroup(
        len(records), len(settled), len(records) - len(settled), tokens, known, reported
    )


def origin_purpose(record: RequestRecord) -> str:
    if record.purpose != "retry":
        return record.purpose
    if record.usage is not None:
        return record.usage.raw.get("response_metadata", {}).get("pycode_origin_purpose", "unknown")
    return "unknown"


def owned(snapshot, entry) -> bool:
    """The run_start was written by this session; entries copied from another name it."""
    return entry.data["execution_session_id"] == snapshot.header.session_id


class UsageService:
    async def request_records(self):
        snapshot = await self._history.snapshot()
        return await self._records.list_requests(snapshot.header.session_id)

    def __init__(self, history: "SqliteSessionStore", records: SQLiteRequestStore):
        self._history = history
        self._records = records

    async def report(self, run_id=None):
        snapshot = await self._history.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        if run_id is None:
            for entry in reversed(path):
                if entry.data.get("kind") == "run_start" and owned(snapshot, entry):
                    run_id = entry.run_id
                    break
        records = await self._records.list_requests(snapshot.header.session_id)
        child_sessions = {}
        for entry in snapshot.entries:
            if entry.data.get("kind") == "subagent_start":
                child_sessions[entry.data["session_id"]] = entry.run_id
        subagent_records = []
        for session_id in child_sessions:
            subagent_records.extend(await self._records.list_requests(session_id))
        linked_runs = {run_id} if run_id is not None else set()
        tree = SessionTree(snapshot.index)
        for entry in snapshot.entries:
            if entry.data.get("kind") != "summary_job":
                continue
            parent_run = None
            for parent in tree.path(entry.id):
                if parent.data.get("kind") == "run_start" and owned(snapshot, parent):
                    parent_run = parent.run_id
                elif parent.data.get("kind") == "run_end" and parent.run_id == parent_run:
                    parent_run = None
            if run_id is not None and parent_run == run_id:
                linked_runs.add(entry.data["job_id"])
        turn = tuple(record for record in records if record.run_id in linked_runs)
        subagent_turn = []
        for record in subagent_records:
            if child_sessions[record.session_id] in linked_runs:
                subagent_turn.append(record)
        main = tuple(
            record
            for record in turn
            if record.agent_id == "lead" and origin_purpose(record) == "agent"
        )
        return {
            "session_id": snapshot.header.session_id,
            "run_id": run_id,
            "last_request": plain_data(records[-1]) if records else None,
            "main_turn": plain_data(aggregate_requests(main)),
            "turn": plain_data(aggregate_requests(turn)),
            "session": plain_data(aggregate_requests(records)),
            "by_purpose": self.groups(records, lambda record: record.purpose),
            "by_origin_purpose": self.groups(records, origin_purpose),
            "turn_by_purpose": self.groups(turn, lambda record: record.purpose),
            "by_agent": self.groups(records, lambda record: record.agent_id),
            "subagents": plain_data(aggregate_requests(tuple(subagent_records))),
            "subagents_turn": plain_data(aggregate_requests(tuple(subagent_turn))),
            "subagents_by_agent": self.groups(subagent_records, lambda record: record.agent_id),
            "total_with_subagents": plain_data(aggregate_requests((*records, *subagent_records))),
        }

    def groups(self, records, key):
        groups = {}
        for record in records:
            groups.setdefault(key(record), []).append(record)
        return {
            name: plain_data(aggregate_requests(tuple(items))) for name, items in groups.items()
        }

    async def aclose(self):
        await self._records.aclose()
