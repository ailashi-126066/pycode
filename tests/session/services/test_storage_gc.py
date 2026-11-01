"""Real object files, JSONL references, quarantine retention and restoration."""

import pytest

from pycode.kernel.domain.errors import StorageError
from pycode.kernel.domain.messages import Message
from pycode.session.adapters.storage.layout import StorageLayout
from pycode.session.adapters.storage.objects import FileStorageObjects
from pycode.session.adapters.storage.session_repository import SqliteSessionRepository
from pycode.session.domain.storage import RETENTION_SECONDS
from pycode.session.services.storage_gc import StorageGC
from tests.support.requests import TestTiming


async def setup_objects(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_data = tmp_path / "data"
    layout = StorageLayout(workspace, user_data)
    timing = TestTiming()
    repository = SqliteSessionRepository(layout.sessions, str(workspace), timing)
    objects = FileStorageObjects(user_data, workspace)
    return workspace, layout, timing, repository, objects


async def write_session(repository, session_id="s1"):
    store = await repository.open(session_id, create=True)
    try:
        await store.append_message(Message("u1", "user", "task"))
    finally:
        await store.aclose()


async def test_invalid_reference_log_stops_gc_without_deleting_objects(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    await write_session(repository)
    from pycode.session.adapters.storage.session import open_database

    database = open_database(layout.sessions / "s1" / "session.sqlite")
    database.put(("entries",), "invalid", {"sequence": 99, "entry": {"invalid": "record"}})
    database.conn.close()
    with pytest.raises(StorageError):
        await StorageGC(objects, repository, timing).collect()


async def test_path_escape_is_rejected_before_any_move(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("user data")
    from pycode.session.domain.storage import StoredObject

    item = StoredObject("untrusted", "output", (str(outside),), 9)
    with pytest.raises(StorageError) as error:
        await objects.apply(item, "quarantine", timing.now())
    assert error.value.code == "gc_path_escape" and outside.read_text() == "user data"


async def test_memory_directories_are_never_managed_objects(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    await write_session(repository)
    gc = StorageGC(objects, repository, timing)
    await gc.delete_session("s1")
    files = []
    for root in (objects.root / "memory", workspace / ".pycode" / "memory"):
        root.mkdir(parents=True)
        for name in ("memory.json", "settings.json", "changes.jsonl"):
            (root / name).write_text("{}\n")
            files.append(root / name)
    await gc.collect()
    timing.current += RETENTION_SECONDS
    collected = await gc.collect()
    assert all(path.read_text() == "{}\n" for path in files)
    assert not any("memory" in row["object_id"] for row in collected["decisions"])
    from pycode.session.domain.storage import StoredObject

    item = StoredObject("forged", "output", (str(files[0]),), 3)
    with pytest.raises(StorageError) as error:
        await objects.apply(item, "quarantine", timing.now())
    assert error.value.code == "gc_memory_excluded" and files[0].exists()
