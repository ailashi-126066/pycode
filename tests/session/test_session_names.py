"""Unicode names, duplicates, restart and fork always retain opaque identities."""

import pytest

from pycode.kernel import Message, StorageError
from pycode.session.sessions import (
    SessionService,
    assign_initial_title,
    name_session,
    session_title,
    validate_title,
)
from pycode.session.store import SqliteSessionRepository
from tests.support.requests import TestTiming


async def test_initial_title_after_user_manual_override_restart_and_fork(tmp_path):
    repository = SqliteSessionRepository(tmp_path / "sessions", str(tmp_path), TestTiming())
    store = await repository.open("opaque", create=True)
    message = Message("u1", "user", "  中文   task\nwith whitespace  ")
    await store.append_message(message)
    await assign_initial_title(store, message)
    assert session_title(await store.snapshot()) == "中文 task with whitespace"
    before = await store.snapshot()
    await name_session(store, "手动名称")
    await assign_initial_title(store, Message("u2", "user", "new task"))
    assert session_title(await store.snapshot()) == "手动名称"
    assert (await store.snapshot()).header == before.header
    assert (await store.snapshot()).entries[: len(before.entries)] == before.entries
    leaf = (await store.snapshot()).leaf_id
    await store.aclose()
    manager = SessionService(repository)
    assert await manager.resolve("opaque") == await manager.resolve("手动名称") == "opaque"
    listed = await manager.list_sessions()
    assert listed[0]["title"] == "手动名称" and listed[0]["short_id"] == "opaque"
    assert listed[0]["updated_at"]
    forked = await manager.fork("opaque", leaf, "child")
    assert forked["header"]["parent_session_id"] == "opaque"
    assert forked["header"]["session_id"] == "child"
    fork_listing = next(
        item for item in await manager.list_sessions() if item["session_id"] == "child"
    )
    assert fork_listing["title"] == "手动名称 (fork)"


async def test_duplicate_title_requires_id_and_never_merges_writers(tmp_path):
    repository = SqliteSessionRepository(tmp_path / "sessions", str(tmp_path), TestTiming())
    for identifier in ("first", "second"):
        store = await repository.open(identifier, create=True)
        await store.append_message(Message("u", "user", "body"))
        await name_session(store, "same name")
        await store.aclose()
    manager = SessionService(repository)
    with pytest.raises(StorageError) as error:
        await manager.resolve("same name")
    assert error.value.code == "ambiguous_session_name"
    assert "first" in str(error.value) and "second" in str(error.value)
    assert await manager.resolve("first") == "first"
    assert (await manager.rename("second", "独立名字"))["session_id"] == "second"
    assert await manager.resolve("same name") == "first"


@pytest.mark.parametrize("title", ["", "   ", "x" * 81, "bad\x00name", "bad\ud800"])
def test_name_validation_rejects_empty_oversized_control_or_invalid_utf8(title):
    with pytest.raises(StorageError) as error:
        validate_title(title)
    assert error.value.code == "invalid_session_name"
