import pytest

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel.domain.errors import StorageError
from pycode.kernel.domain.messages import Message
from pycode.session.adapters.storage.session_repository import SqliteSessionRepository
from tests.support.requests import TestTiming


async def test_fork_copies_selected_path_preserves_source_and_records_origin(tmp_path):
    repository = SqliteSessionRepository(tmp_path, "/project", TestTiming())
    source = await repository.open("source", create=True)
    root = await source.append_message(Message("u1", "user", "root"))
    abandoned = await source.append_message(Message("a1", "assistant", "abandoned"))
    await source.branch(root)
    kept = await source.append_message(Message("a2", "assistant", "kept"))
    original = await source.snapshot()
    await source.aclose()
    fork = await repository.fork("source", kept, "forked")
    snapshot = await fork.snapshot()
    assert snapshot.header.parent_session_id == "source"
    assert snapshot.header.forked_from_entry_id == kept
    assert abandoned not in {entry.id for entry in snapshot.entries}
    assert [
        message.text for message in ContextProjector().transform_context(snapshot).messages
    ] == [
        "root",
        "kept",
    ]
    await fork.append_message(Message("u2", "user", "continued"))
    await fork.aclose()
    unchanged = await repository.open("source")
    current = await unchanged.snapshot()
    assert current.entries == original.entries and current.leaf_id == original.leaf_id
    assert current.header == original.header
    await unchanged.aclose()
    restored = await repository.open("forked")
    assert len((await restored.snapshot()).entries) == 3
    await restored.aclose()


@pytest.mark.parametrize("session_id", ["../escape", "C:/outside", "a/b", ""])
async def test_session_identifiers_cannot_escape_repository(tmp_path, session_id):
    repository = SqliteSessionRepository(tmp_path, "/project", TestTiming())
    with pytest.raises(StorageError) as error:
        await repository.open(session_id, create=True)
    assert error.value.code == "invalid_session_id"
