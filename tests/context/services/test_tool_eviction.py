from pycode.context.domain.token_estimate import estimate_messages
from pycode.context.domain.tool_eviction import EvictionBudget
from pycode.context.services.context_projector import ContextProjector
from pycode.context.services.tool_eviction import ToolEvictionService
from pycode.kernel.domain.history import SessionHeader
from pycode.kernel.domain.messages import Message, ToolCall, ToolResult
from pycode.kernel.domain.tools import ToolContext
from pycode.session.adapters.storage.header_reader import FileSessionHeaderReader
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.tools.adapters.output_store import FileToolOutputStore
from tests.support.requests import TestTiming


async def test_eviction_is_append_only_idempotent_and_readback_survives_branch_restore(tmp_path):
    directory = tmp_path / "sessions"
    store = SqliteSessionStore(directory / "s1", SessionHeader("s1", "/project", "t"), TestTiming())
    outputs = FileToolOutputStore(
        directory, "s1", store, header_reader=FileSessionHeaderReader(directory)
    )
    await store.append_message(Message("u1", "user", "initial request"))
    entry_ids = []
    output_ids = []
    for index in range(6):
        context = ToolContext("s1", "r1", f"op-{index}", "/project", f"call-{index}", "Bash")
        await store.append_message(
            Message(
                f"assistant-{index}",
                "assistant",
                content=(ToolCall(f"call-{index}", "Bash", {}),),
            )
        )
        result = await outputs.prepare(
            context,
            ToolResult(
                f"call-{index}",
                "Bash",
                "x" * 50000,
                session_id="s1",
                exit_code=1 if index == 0 else 0,
                is_error=index == 0,
            ),
            "tail",
        )
        entry = await store.append_message(
            Message(f"result-{index}", "tool_result", tool_result=result)
        )
        await outputs.bind(result.output_id, entry)
        entry_ids.append(entry)
        output_ids.append(result.output_id)
    await store.append_message(Message("u2", "user", "recent second"))
    prior_leaf = await store.append_message(Message("u3", "user", "recent third"))
    original = (await store.snapshot()).entries
    projector = ContextProjector()
    service = ToolEvictionService(store, projector, EvictionBudget(200000))
    report = await service.apply()
    assert set(report.entry_ids) == set(entry_ids[:3])
    assert report.reclaimed_tokens > 20000
    assert (await store.snapshot()).entries[: len(original)] == original
    assert report.context_tokens == estimate_messages(
        projector.transform_context(await store.snapshot()).messages
    )
    before_repeat = (await store.snapshot()).revision
    assert (await service.apply()).entry_ids == ()
    assert (await store.snapshot()).revision == before_repeat
    projected = projector.transform_context(await store.snapshot())
    first = next(
        message.tool_result
        for message in projected.messages
        if message.tool_result and message.tool_result.call_id == "call-0"
    )
    assert "exit code 1" in first.text and output_ids[0] in first.text
    assert (await outputs.read(output_ids[0], 1, 1, 4096)).text == "x" * 4096
    await store.aclose()
    restored = SqliteSessionStore(
        directory / "s1", SessionHeader("s1", "/project", "t"), TestTiming()
    )
    assert projector.transform_context(await restored.snapshot()).messages == projected.messages
    await restored.branch(prior_leaf)
    raw = projector.transform_context(await restored.snapshot())
    assert all(
        len(message.tool_result.text) == 50000 for message in raw.messages if message.tool_result
    )
    await restored.aclose()
