"""The task board is the last board snapshot on the current branch."""

from pycode.context import ContextProjector, board_from_data
from pycode.kernel import (
    Board,
    EntryDraft,
    Message,
    SessionHeader,
    Step,
    ToolCall,
    ToolResult,
    plain_data,
)
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.support.requests import TestTiming


def board(revision):
    return Board("b1", "fix login", ("tests pass",), (Step("s1", "locate", ()),), revision=revision)


async def save_board(store, revision):
    call = f"call-{revision}"
    await store.append_message(
        Message(f"a-{revision}", "assistant", (ToolCall(call, "TaskUpdate", {}),))
    )
    details = {"task_board": plain_data(board(revision)), "source_assistant_id": f"a-{revision}"}
    return await store.append_message(
        Message(
            f"r-{revision}",
            "tool_result",
            tool_result=ToolResult(call, "TaskUpdate", "saved", details=details),
        )
    )


async def test_board_follows_the_branch_and_survives_compaction(tmp_path):
    store = SqliteSessionStore(
        tmp_path / "session", SessionHeader("s1", str(tmp_path), "created"), TestTiming()
    )
    projector = ContextProjector()
    try:
        root = await store.append_message(Message("u1", "user", "fix login"))
        assert projector.transform_context(await store.snapshot()).checkpoint["task_board"] is None
        first = await save_board(store, 1)
        current = projector.transform_context(await store.snapshot()).checkpoint["task_board"]
        assert board_from_data(current) == board(1)
        await save_board(store, 2)
        # The cached projection must not reuse the old board for an appended snapshot.
        current = projector.transform_context(await store.snapshot()).checkpoint["task_board"]
        assert current["revision"] == 2
        await store.branch(first)
        current = projector.transform_context(await store.snapshot()).checkpoint["task_board"]
        assert current["revision"] == 1
        await store.append_entry(
            EntryDraft(
                "compaction",
                data={
                    "summary": "located",
                    "first_kept_entry_id": root,
                    "checkpoint": {"task_board": current},
                },
            )
        )
        compacted = ContextProjector().transform_context(await store.snapshot())
        assert compacted.checkpoint["task_board"]["revision"] == 1
        await store.branch(root)
        assert projector.transform_context(await store.snapshot()).checkpoint["task_board"] is None
    finally:
        await store.aclose()
