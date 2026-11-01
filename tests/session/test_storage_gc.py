"""Real object files and references; unreferenced objects and deleted sessions go directly."""

import pytest

from pycode.kernel import Message, StorageError
from pycode.session.storage_gc import FileStorageObjects, StorageGC
from pycode.session.store import SqliteSessionRepository, StorageLayout
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
    from pycode.session.store import open_database

    database = open_database(layout.sessions / "s1" / "session.sqlite")
    database.put(("entries",), "invalid", {"sequence": 99, "entry": {"invalid": "record"}})
    database.conn.close()
    with pytest.raises(StorageError):
        await StorageGC(objects).collect()


async def test_unreferenced_plan_is_deleted_and_dry_run_only_reports(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    await write_session(repository)
    plan = workspace / ".pycode" / "plans" / "s1" / ("a" * 32 + ".md")
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan")
    gc = StorageGC(objects)
    preview = await gc.collect(dry_run=True)
    assert [row["action"] for row in preview["decisions"]] == ["delete"] and plan.exists()
    await gc.collect()
    assert not plan.exists()


async def test_session_delete_removes_it_unless_it_is_open(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    await write_session(repository)
    store = await repository.open("s1")
    await store.append_message(Message("u2", "user", "still writing"))
    with pytest.raises(StorageError) as error:
        await StorageGC(objects).delete_session("s1")
    assert error.value.code == "session_busy"
    await store.aclose()
    await StorageGC(objects).delete_session("s1")
    assert not (layout.sessions / "s1").exists()
    with pytest.raises(StorageError) as error:
        await repository.open("s1")
    assert error.value.code == "session_not_found"


async def test_path_escape_is_rejected_before_any_delete(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("user data")
    from pycode.session.storage_gc import StoredObject

    item = StoredObject("untrusted", "output", (str(outside),), 9)
    with pytest.raises(StorageError) as error:
        await objects.delete(item)
    assert error.value.code == "gc_path_escape" and outside.read_text() == "user data"


async def test_memory_directories_are_never_managed_objects(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    await write_session(repository)
    gc = StorageGC(objects)
    files = []
    for root in (objects.root / "memory", workspace / ".pycode" / "memory"):
        root.mkdir(parents=True)
        for name in ("memory.json", "settings.json", "changes.jsonl"):
            (root / name).write_text("{}\n")
            files.append(root / name)
    collected = await gc.collect()
    assert all(path.read_text() == "{}\n" for path in files)
    assert not any("memory" in row["object_id"] for row in collected["decisions"])
    from pycode.session.storage_gc import StoredObject

    item = StoredObject("forged", "output", (str(files[0]),), 3)
    with pytest.raises(StorageError) as error:
        await objects.delete(item)
    assert error.value.code == "gc_memory_excluded" and files[0].exists()
