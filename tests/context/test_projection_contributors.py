"""Optional context contributions cannot mutate facts or disappear behind incremental caches."""

import pytest

from pycode.context import ContextProjector
from pycode.kernel import (
    Message,
    ProjectedEntry,
    ProjectionContribution,
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


class MessageCount:
    name = "message-count"
    incremental_safe = False
    checkpoint_keys = frozenset({"message_count"})

    def contribute(self, state, path, entries, checkpoint):
        with pytest.raises(TypeError):
            checkpoint["other"] = "mutation"
        return ProjectionContribution(
            (ProjectedEntry(path[-1].id, (Message("count", "system", str(len(path))),)),),
            {"message_count": len(path)},
        )


def test_custom_contributor_observes_new_ordinary_messages():
    first = SessionEntry("u1", None, "message", "t", message=Message("u1", "user", "one"))
    second = SessionEntry("u2", "u1", "message", "t", message=Message("u2", "user", "two"))
    projector = ContextProjector((MessageCount(),))
    assert projector.transform_context(snapshot(first)).checkpoint["message_count"] == 1
    current = projector.transform_context(snapshot(first, second))
    assert current.checkpoint["message_count"] == 2
    assert current.messages[0].text == "2"
    assert projector.transform_context(snapshot(first, second)) is current


def test_contributor_cannot_claim_another_feature_checkpoint():
    class Invalid(MessageCount):
        def contribute(self, *args):
            return ProjectionContribution(checkpoint_updates={"somebody_else": 1})

    entry = SessionEntry("u", None, "message", "t", message=Message("u", "user", "x"))
    with pytest.raises(ValueError, match="another feature"):
        ContextProjector((Invalid(),)).transform_context(snapshot(entry))
    with pytest.raises(ValueError, match="unique"):
        ContextProjector((MessageCount(), MessageCount()))


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
