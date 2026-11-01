import pytest
from langchain_core.messages import AIMessage, ToolMessage

from pycode.context.eviction import EvictionBudget, ToolEvictionService
from pycode.context.projector import ContextProjector
from pycode.kernel import Message, SessionHeader, ToolCall, ToolResult
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.agent.adapters.test_agent import assemble
from tests.support.model import ScriptedChatModel
from tests.support.requests import TestTiming
from tests.support.runtime import build_agent_runtime


async def test_next_framework_request_uses_persistent_evictions_not_original_graph_messages(
    tmp_path,
):
    _, context, _, _, _, _ = assemble([])
    store = SqliteSessionStore(
        tmp_path / context.session_id,
        SessionHeader(context.session_id, "/workspace", "t"),
        TestTiming(),
    )
    await store.append_message(Message("initial", "user", "initial"))
    for index in range(6):
        call_id = f"old-{index}"
        await store.append_message(
            Message(f"old-assistant-{index}", "assistant", content=(ToolCall(call_id, "Bash", {}),))
        )
        result = ToolResult(
            call_id, "Bash", "x" * 50000, session_id=context.session_id, operation_id=call_id
        )
        await store.append_message(
            Message(f"old-result-{index}", "tool_result", tool_result=result)
        )
    await store.append_message(Message("recent-1", "user", "recent one"))
    await store.append_message(Message("recent-2", "user", "recent two"))
    context.journal = store
    context.history_store = store
    context.projector = ContextProjector()
    context.eviction = ToolEvictionService(store, context.projector, EvictionBudget(200000))
    context.inputs.submit(Message("follow-up", "user", "continue"), "follow_up")
    model = ScriptedChatModel(
        responses=[AIMessage(id="a1", content="first complete"), AIMessage(id="a2", content="done")]
    )
    runtime = build_agent_runtime(model)
    history = context.projector.transform_context(await store.snapshot()).messages
    await runtime.run((Message("current", "user", "current"),), context, history=history)
    first = [message.content for message in model.requests[0] if isinstance(message, ToolMessage)]
    second = [message.content for message in model.requests[1] if isinstance(message, ToolMessage)]
    assert sum("Old tool output omitted" in text for text in first) == 3
    assert sum(len(text) == 50000 for text in first) == 3
    assert sum("Old tool output omitted" in text for text in second) == 3
    assert (
        len([entry for entry in (await store.snapshot()).entries if entry.type == "context_edit"])
        == 3
    )
    original = [
        entry.message.tool_result.text
        for entry in (await store.snapshot()).entries
        if entry.message and entry.message.tool_result
    ]
    assert len(original) == 6 and all(len(text) == 50000 for text in original)
    await store.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
