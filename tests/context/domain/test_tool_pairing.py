# Source: local tests/test_migration_basics.py; source baseline in docs/source-map.md.
from copy import deepcopy

import pytest

from pycode.context.domain.tool_pairing import repair_tool_pairing
from pycode.kernel.domain.messages import Message, ToolCall, ToolResult


def test_source_pairing_repairs_send_copy_preserving_real_results_and_history():
    original = (
        Message("u1", "user", "task"),
        Message(
            "a1",
            "assistant",
            content=(ToolCall("one", "Read", {}), ToolCall("two", "Write", {})),
        ),
        Message("u2", "user", "interrupted"),
        Message("t1", "tool_result", tool_result=ToolResult("one", "Read", "real")),
        Message("t2", "tool_result", tool_result=ToolResult("orphan", "Read", "bogus")),
    )
    before = deepcopy(original)
    repaired = repair_tool_pairing(original)
    assert original == before
    assert [message.tool_result.call_id for message in repaired if message.tool_result] == [
        "one",
        "two",
    ]
    assert repaired[2].tool_result.text == "real"
    assert "result is unknown" in repaired[3].tool_result.text
    assert repaired[3].tool_result.is_error
    with pytest.raises(TypeError):
        repaired[1].tool_calls[0].arguments["changed"] = True
    assert original == before


def test_source_pairing_binds_reused_ids_to_correct_occurrence():
    messages = (
        Message("a1", "assistant", content=(ToolCall("same", "Read", {}),)),
        Message("t1", "tool_result", tool_result=ToolResult("same", "Read", "first")),
        Message("a2", "assistant", content=(ToolCall("same", "Read", {}),)),
        Message("t2", "tool_result", tool_result=ToolResult("same", "Read", "second")),
    )
    repaired = repair_tool_pairing(messages)
    assert repaired[1].tool_result.text == "first"
    assert repaired[3].tool_result.text == "second"
