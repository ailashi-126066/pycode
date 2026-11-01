"""Explicit projection functions survive compaction and the incremental cache stays immutable."""

import pytest

from pycode.context import ContextProjector
from pycode.kernel import (
    Message,
    ProjectedEntry,
    ProjectionContribution,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    ToolResult,
)


def snapshot(*entries):
    return SessionSnapshot(
        SessionHeader("s", "/project", "t"),
        entries,
        entries[-1].id if entries else None,
        len(entries),
        len(entries),
    )


class Recording:
    """A test projection: pins one system message and records the checkpoint it was shown."""

    def __init__(self, name, key, calls):
        self.name = name
        self.checkpoint_keys = frozenset({key})
        self.key = key
        self.calls = calls

    def contribute(self, snapshot, path, checkpoint):
        self.calls.append((self.name, dict(checkpoint)))
        return ProjectionContribution(
            (ProjectedEntry(path[-1].id, (Message(self.name, "system", self.name),)),),
            {self.key: len(path)},
        )


class Silent:
    name = "silent"

    def contribute(self, snapshot, path, checkpoint):
        return None


PROMPT = SessionEntry(
    "p", None, "system_prompt", "t", data={"text": "SYSTEM", "sha256": "h", "source": None}
)


def test_functions_contribute_in_list_order_after_the_system_prompt():
    calls = []
    first, second = Recording("first", "one", calls), Recording("second", "two", calls)
    user = SessionEntry("u", "p", "message", "t", message=Message("u", "user", "hi"))
    projected = ContextProjector(
        (first.contribute, Silent().contribute, second.contribute),
        checkpoint_keys=frozenset({"one", "two"}),
    ).transform_context(snapshot(PROMPT, user))
    assert [name for name, _ in calls] == ["first", "second"]
    assert calls[1][1]["one"] == 2 and "two" not in calls[1][1]
    assert [message.text for message in projected.messages] == ["SYSTEM", "first", "second", "hi"]
    assert projected.checkpoint["one"] == projected.checkpoint["two"] == 2


def test_removal_clears_a_key_carried_by_an_older_compaction():
    class Clearing:
        name = "plans"
        checkpoint_keys = frozenset({"plan"})

        def contribute(self, snapshot, path, checkpoint):
            return ProjectionContribution(checkpoint_removals=self.checkpoint_keys)

    compacted = SessionEntry(
        "c",
        None,
        "compaction",
        "t",
        data={
            "first_kept_entry_id": "c",
            "summary": "old context",
            "checkpoint": {"plan": {"mode": "planning"}},
        },
    )
    projected = ContextProjector(
        (Clearing().contribute,), checkpoint_keys=frozenset({"plan"})
    ).transform_context(snapshot(compacted))
    assert "plan" not in projected.checkpoint


def test_projected_state_in_a_tool_result_recomputes_but_ordinary_messages_reuse():
    calls = []
    feature = Recording("tasks", "task_board", calls)
    projector = ContextProjector((feature.contribute,), checkpoint_keys=feature.checkpoint_keys)
    user = SessionEntry("u", "p", "message", "t", message=Message("u", "user", "hi"))
    projector.transform_context(snapshot(PROMPT, user))
    reply = SessionEntry("a", "u", "message", "t", message=Message("a", "assistant", "ok"))
    projector.transform_context(snapshot(PROMPT, user, reply))
    assert len(calls) == 1
    board = SessionEntry(
        "r",
        "a",
        "message",
        "t",
        message=Message(
            "r",
            "tool_result",
            tool_result=ToolResult("c", "TaskWrite", "saved", details={"task_board": {}}),
        ),
    )
    projected = projector.transform_context(snapshot(PROMPT, user, reply, board))
    assert len(calls) == 2 and projected.checkpoint["task_board"] == 4


def test_incremental_missing_tool_result_is_deeply_immutable():
    from pycode.kernel import ToolCall

    user = SessionEntry("u", None, "message", "t", message=Message("u", "user", "task"))
    assistant = SessionEntry(
        "a",
        "u",
        "message",
        "t",
        message=Message("a", "assistant", content=(ToolCall("call", "probe", {}),)),
    )
    projector = ContextProjector()
    projector.transform_context(snapshot(user))
    current = projector.transform_context(snapshot(user, assistant))
    result = current.messages[-1].require_tool_result()
    assert result.error_code == "execution_unknown"
    with pytest.raises(TypeError):
        result.details["status"] = "success"
    assert (
        projector.transform_context(snapshot(user, assistant))
        .messages[-1]
        .require_tool_result()
        .details["status"]
        == "unknown"
    )
