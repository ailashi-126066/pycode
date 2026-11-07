"""Old tool previews persist unless an existing explicit context edit changes their view."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from pycode.context import ContextProjector
from pycode.kernel import EntryDraft, Message, SessionHeader, ToolCall, ToolResult
from pycode.session import SqliteSessionStore
from tests.agent.test_runtime import assemble
from tests.support.model import ScriptedChatModel
from tests.support.requests import TestTiming
from tests.support.runtime import build_agent_runtime


@pytest.mark.parametrize("legacy_edit", [False, True])
async def test_old_outputs_survive_model_rounds_and_existing_edits_still_load(
    tmp_path, legacy_edit
):
    _, context, _, _, _, _ = assemble([])
    store = SqliteSessionStore(
        tmp_path / context.session_id,
        SessionHeader(context.session_id, "/workspace", "t"),
        TestTiming(),
    )
    try:
        await store.append_message(Message("initial", "user", "initial"))
        results = []
        for index in range(6):
            call_id = f"old-{index}"
            await store.append_message(
                Message(f"a{index}", "assistant", content=(ToolCall(call_id, "read", {}),))
            )
            results.append(
                await store.append_message(
                    Message(
                        f"r{index}",
                        "tool_result",
                        tool_result=ToolResult(call_id, "read", "x" * 50000),
                    )
                )
            )
        await store.append_message(Message("recent1", "user", "recent one"))
        await store.append_message(Message("recent2", "user", "recent two"))
        if legacy_edit:
            await store.append_entry(
                EntryDraft(
                    "context_edit",
                    data={
                        "kind": "tool_eviction",
                        "target_id": results[0],
                        "replacement": "Old tool output omitted.",
                    },
                )
            )
        context.history = store
        context.projector = ContextProjector(cache=False)
        context.inputs.submit(Message("follow-up", "user", "continue"), "follow_up")
        model = ScriptedChatModel(responses=[AIMessage(content="first"), AIMessage(content="done")])
        runtime = build_agent_runtime(model)
        history = context.projector.transform_context(await store.snapshot()).messages
        await runtime.run((Message("current", "user", "current"),), context, history=history)
        assert len(model.requests) == 2
        for messages in model.requests:
            texts = [message.content for message in messages if isinstance(message, ToolMessage)]
            assert sum(len(text) == 50000 for text in texts) == 6 - int(legacy_edit)
            assert sum("Old tool output omitted" in text for text in texts) == int(legacy_edit)
        snapshot = await store.snapshot()
        assert sum(entry.type == "context_edit" for entry in snapshot.entries) == int(legacy_edit)
        assert not any(
            entry.data.get("kind") == "tool_eviction_audit" for entry in snapshot.entries
        )
        originals = [
            entry.message.tool_result.text
            for entry in snapshot.entries
            if entry.message is not None and entry.message.tool_result is not None
        ]
        assert len(originals) == 6 and all(len(text) == 50000 for text in originals)
        assert not hasattr(context, "eviction")
        assert context.projector._cached is None
    finally:
        await store.aclose()


pytestmark = pytest.mark.usefixtures("disclosed_tools")
