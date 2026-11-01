from pycode.context.domain.history_boundaries import valid_kept_boundary
from pycode.context.domain.summary_source import capture_source
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import Message, ModelSelection, SessionHeader, SessionTree, ToolCall, ToolResult
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.support.requests import TestTiming


async def test_cut_moves_before_a_call_whose_result_follows_an_intervening_reminder(tmp_path):
    store = SqliteSessionStore(tmp_path / "s1", SessionHeader("s1", "/project", "t"), TestTiming())
    try:
        await store.append_message(Message("u1", "user", "old history " * 1000))
        await store.append_message(Message("a1", "assistant", "old answer " * 1000))
        call = await store.append_message(
            Message("call", "assistant", content=(ToolCall("c1", "Read", {}),))
        )
        await store.append_message(Message("reminder", "custom", "reminder"))
        await store.append_message(
            Message("t1", "tool_result", tool_result=ToolResult("c1", "Read", "x" * 8000))
        )
        await store.append_message(Message("a2", "assistant", "recent answer"))
        snapshot = await store.snapshot()
        source = capture_source(
            snapshot,
            ContextProjector().transform_context(snapshot),
            job_id="a" * 32,
            selection=ModelSelection("test", "model", "openai"),
            config_hash="config",
            keep_recent_tokens=2005,
            tokens_before=0,
        )
        assert source.first_kept_entry_id == call
        path = SessionTree(snapshot.entries).path(snapshot.leaf_id)
        assert valid_kept_boundary(path, source.first_kept_entry_id)
        assert not any(message.id in {"call", "t1"} for message in source.messages)
    finally:
        await store.aclose()
