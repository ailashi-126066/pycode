"""Task state uses the same durable record and branch as the assistant message."""

from dataclasses import replace

from pycode.context import ContextProjector, empty_tasks
from pycode.kernel import EntryDraft, Message, SessionHeader
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.agent.adapters.test_task_context import new_task, validate
from tests.support.requests import TestTiming


async def test_task_state_reopens_branches_and_survives_compaction(tmp_path):
    header = SessionHeader("s1", str(tmp_path), "created")

    def open_store():
        return SqliteSessionStore(tmp_path / "session", header, TestTiming())

    store = open_store()
    root = await store.append_message(Message("u1", "user", "修复登录超时"))
    first = validate(empty_tasks(), new_task())
    first_entry = await store.append_message(
        Message("a1", "assistant", "开始定位", provider_metadata={"pycode_task_change": first})
    )
    projector = ContextProjector()
    projection = projector.transform_context(await store.snapshot())
    state = projection.checkpoint["task_state"]
    second = validate(state, new_task("整理部署脚本"))
    await store.append_message(
        Message("a2", "assistant", "先处理脚本", provider_metadata={"pycode_task_change": second})
    )
    await store.aclose()
    store = open_store()
    try:
        snapshot = await store.snapshot()
        state = projector.transform_context(snapshot).checkpoint["task_state"]
        assert state["active_task_id"] == second["task_id"]
        assert state["tasks"][first["task_id"]]["status"] == "paused"
        await store.branch(first_entry)
        state = projector.transform_context(await store.snapshot()).checkpoint["task_state"]
        assert state["active_task_id"] == first["task_id"]
        assert second["task_id"] not in state["tasks"]
        compact = await store.append_entry(
            EntryDraft(
                "compaction",
                data={
                    "summary": "已开始定位",
                    "first_kept_entry_id": root,
                    "checkpoint": {"task_state": state},
                },
            )
        )
        snapshot = await store.snapshot()
        after = ContextProjector().transform_context(snapshot)
        assert after.checkpoint["task_state"] == state
        # A compacted export may contain only its state snapshot and retained messages.
        compact_entry = snapshot.entries_by_id[compact.id]
        exported = replace(
            snapshot,
            entries=(replace(compact_entry, parent_id=None),),
            entries_by_id=None,
            active_path_ids=(),
        )
        assert ContextProjector().transform_context(exported).checkpoint["task_state"] == state
        await store.branch(root)
        assert (
            projector.transform_context(await store.snapshot()).checkpoint["task_state"]
            == empty_tasks()
        )
    finally:
        await store.aclose()
