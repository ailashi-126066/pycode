import json
from pathlib import Path

import pytest

from pycode.context.domain.compaction import find_cut_point
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import (
    Message,
    SessionEntry,
    SessionHeader,
    SessionSnapshot,
    TextBlock,
    ToolCall,
    ToolResult,
)

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures/compaction-baselines.json").read_text())


@pytest.mark.parametrize("case", VECTORS["cuts"], ids=lambda case: case["name"])
def test_pi_cut_vectors_use_effective_native_projection(case):
    entries = []
    parent = None
    for index, record in enumerate(case["messages"]):
        role, text = record[:2]
        text *= record[2] if len(record) > 2 else 1
        identifier = f"entry-{index}"
        if role == "custom":
            entry = SessionEntry(identifier, parent, "custom_message", "t", data={"text": text})
        else:
            message = Message(identifier, role, text)
            if role == "tool":
                message = Message(
                    identifier, "tool_result", tool_result=ToolResult(record[3], "Read", text)
                )
            elif len(record) > 3:
                message = Message(
                    identifier,
                    "assistant",
                    (TextBlock(text), ToolCall(record[3], "Read", {"path": "big.txt"})),
                )
            entry = SessionEntry(identifier, parent, "message", "t", message=message)
        entries.append(entry)
        parent = identifier
    snapshot = SessionSnapshot(SessionHeader("s1", "/project", "t"), tuple(entries), parent, 0, 0)
    result = find_cut_point(
        tuple(entries), ContextProjector().transform_context(snapshot), 0, case["budget"]
    )
    assert result.first_kept_index == case["expected"]
    assert result.split_turn == case["split"]
    assert result.turn_start_index == case["turn_start"]
