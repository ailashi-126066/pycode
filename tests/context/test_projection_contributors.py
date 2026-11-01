"""Feature contributions survive compaction and the incremental cache stays immutable."""

import pytest

from pycode.context import ContextProjector
from pycode.kernel import (
    Message,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
)


def snapshot(*entries):
    return SessionSnapshot(
        SessionHeader("s", "/project", "t"),
        entries,
        entries[-1].id if entries else None,
        len(entries),
        len(entries),
    )


def test_completed_plan_clears_active_plan_in_an_older_compaction():
    from pycode.plans.services.projection import PlanProjection

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
    completed = SessionEntry(
        "done", "c", "plan_state", "t", data={"session_id": "s", "mode": "completed"}
    )
    projected = ContextProjector((PlanProjection(),)).transform_context(
        snapshot(compacted, completed)
    )
    assert "plan" not in projected.checkpoint


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
