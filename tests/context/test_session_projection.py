import json
from pathlib import Path

import pytest

from pycode.context.projector import ContextProjector
from pycode.kernel import (
    Message,
    ModelSelection,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    SessionTree,
    ToolCall,
    ToolResult,
    UsageRecord,
)

VECTORS = json.loads((Path(__file__).parents[1] / "fixtures/pi-session.json").read_text())


def entry(row):
    identifier, parent, kind, text, *extra = row
    if kind in {"user", "assistant", "system"}:
        return SessionEntry(
            identifier,
            parent,
            "message",
            "2026-01-01T00:00:00Z",
            message=Message(identifier, kind, text),
        )
    field = "first_kept_entry_id" if kind == "compaction" else "from_id"
    return SessionEntry(
        identifier, parent, kind, "2026-01-01T00:00:00Z", data={"summary": text, field: extra[0]}
    )


def project(entries, leaf):
    snapshot = SessionSnapshot(
        SessionHeader("session-1", "/project", "created"), tuple(entries), leaf, 0, 0
    )
    return ContextProjector().transform_context(snapshot)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_upstream_projection_vectors(case):
    projection = project([entry(row) for row in case["entries"]], case["leaf"])
    assert [message.text for message in projection.messages] == case["expected_texts"]


def test_unknown_leaf_and_orphan_are_explicit_native_format_errors():
    with pytest.raises(ValueError, match="leaf"):
        SessionTree((entry(["1", None, "user", "hello"]),)).path("missing")
    with pytest.raises(ValueError, match="parent"):
        SessionTree((entry(["1", "missing", "user", "orphan"]),))


def test_latest_edit_wins_without_rewriting_raw_message_or_usage():
    usage = UsageRecord("provider", "model", "request", total_tokens=11)
    message = Message("m1", "assistant", "original", usage=usage)
    original = SessionEntry("1", None, "message", "t", message=message)
    edits = [
        SessionEntry(
            "2", "1", "context_edit", "t", data={"target_id": "1", "replacement": "first"}
        ),
        SessionEntry("3", "2", "context_edit", "t", data={"target_id": "1", "replacement": None}),
        SessionEntry(
            "4", "3", "context_edit", "t", data={"target_id": "1", "replacement": "restored"}
        ),
    ]
    projected = project([original, *edits], "4")
    assert projected.messages[0].text == "restored"
    assert projected.messages[0].usage.total_tokens == 11
    assert original.message.text == "original"


def test_tool_replacement_preserves_pairing_error_and_output_reference():
    call = Message("m1", "assistant", content=(ToolCall("call-1", "Read", {"path": "file"}),))
    result = ToolResult("call-1", "Read", "raw output", is_error=True)
    entries = [
        SessionEntry("1", None, "message", "t", message=call),
        SessionEntry(
            "2", "1", "message", "t", message=Message("m2", "tool_result", tool_result=result)
        ),
        SessionEntry(
            "3", "2", "context_edit", "t", data={"target_id": "2", "replacement": "omitted"}
        ),
    ]
    projected = project(entries, "3")
    assert projected.messages[1].tool_result.text == "omitted"
    assert projected.messages[1].tool_result.call_id == "call-1"
    assert projected.messages[1].tool_result.is_error
    assert entries[1].message.tool_result.text == "raw output"


def test_settings_come_from_entire_branch_even_after_compaction():
    selection = ModelSelection("provider", "model", "openai")
    entries = [
        SessionEntry("1", None, "model_change", "t", selection=selection),
        SessionEntry("2", "1", "thinking_level_change", "t", data={"thinking": "high"}),
        entry(["3", "2", "user", "kept"]),
        entry(["4", "3", "compaction", "summary", "3"]),
    ]
    projection = project(entries, "4")
    assert projection.selection == selection
    assert projection.thinking == "high"


def test_custom_state_is_not_sent_but_custom_message_is():
    entries = [
        SessionEntry(
            "1",
            None,
            "custom",
            "t",
            data={"kind": "subagent_start", "secret_state": "not a model message"},
        ),
        SessionEntry("2", "1", "custom_message", "t", data={"text": "visible", "display": False}),
    ]
    assert [message.text for message in project(entries, "2").messages] == ["visible"]


def test_returning_before_a_branch_edit_restores_original_projection():
    entries = [
        entry(["1", None, "user", "original"]),
        SessionEntry(
            "2", "1", "context_edit", "t", data={"target_id": "1", "replacement": "edited"}
        ),
    ]
    assert project(entries, "2").messages[0].text == "edited"
    assert project(entries, "1").messages[0].text == "original"


def test_state_only_audit_does_not_change_model_request_prefix_hash():
    entries = [entry(["1", None, "user", "request"])]
    before = project(entries, "1")
    entries.append(SessionEntry("2", "1", "custom", "t", data={"kind": "audit"}))
    assert project(entries, "2").messages == before.messages


def test_tool_set_changes_are_declared_with_a_system_message():
    def tools(identifier, parent, *names):
        data = {"source": "effective_model", "tools": [{"name": name} for name in names]}
        return SessionEntry(identifier, parent, "tool_set_change", "t", data=data)

    entries = [
        entry(("u1", None, "user", "hi")),
        tools("t1", "u1", "read", "bash"),
        entry(("a1", "t1", "assistant", "ok")),
        tools("t2", "a1", "read", "mcp__srv__search"),
    ]
    projection = project(entries, "t2")
    declared = projection.messages[-1]
    assert [message.role for message in projection.messages] == ["user", "assistant", "system"]
    assert "Added: mcp__srv__search" in declared.text and "Removed: bash" in declared.text
