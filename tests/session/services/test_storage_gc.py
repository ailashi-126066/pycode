"""Real object files, JSONL references, quarantine retention and restoration."""

import pytest

from pycode.kernel.domain.errors import StorageError
from pycode.kernel.domain.messages import Message, ToolCall, ToolResult
from pycode.kernel.domain.tools import ToolContext
from pycode.session.adapters.storage.header_reader import FileSessionHeaderReader
from pycode.session.adapters.storage.layout import StorageLayout
from pycode.session.adapters.storage.objects import FileStorageObjects
from pycode.session.adapters.storage.session_repository import SqliteSessionRepository
from pycode.session.domain.storage import RETENTION_SECONDS
from pycode.session.services.storage_gc import StorageGC
from pycode.tools.adapters.output_store import FileToolOutputStore
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


async def write_output(repository, layout, workspace, session_id="s1"):
    store = await repository.open(session_id, create=True)
    outputs = FileToolOutputStore(
        layout.sessions, session_id, store, header_reader=FileSessionHeaderReader(layout.sessions)
    )
    try:
        await store.append_message(Message("u1", "user", "task"))
        await store.append_message(
            Message("a1", "assistant", content=(ToolCall("c1", "Bash", {}),))
        )
        context = ToolContext(session_id, "r1", "op1", str(workspace), "c1", "Bash")
        result = await outputs.prepare(context, ToolResult("c1", "Bash", "line\n" * 3000), "tail")
        entry_id = await store.append_message(Message("t1", "tool_result", tool_result=result))
        await outputs.bind(result.output_id, entry_id)
        return result.output_id, entry_id
    finally:
        await store.aclose()


async def test_dry_run_and_30_day_quarantine_do_not_delete_active_references(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    output_id, _ = await write_output(repository, layout, workspace)
    gc = StorageGC(objects, repository, timing)
    retained = await gc.collect(dry_run=True)
    assert any(row["action"] == "keep_referenced" for row in retained["decisions"])
    output_decision = next(row for row in retained["decisions"] if row["type"] == "output")
    assert output_decision["last_reference"]["session_id"] == "s1"
    assert output_decision["last_reference"]["timestamp"]
    body = layout.sessions / "s1" / "tool-results" / (output_id + ".txt")
    assert body.exists()
    await gc.delete_session("s1")
    dry = await gc.collect(dry_run=True)
    assert any(
        row["type"] == "output" and row["action"] == "quarantine" for row in dry["decisions"]
    )
    assert body.exists()
    first = await gc.collect()
    assert any(row["action"] == "quarantine" for row in first["decisions"])
    assert not body.exists()
    timing.current += RETENTION_SECONDS - 1
    held = await gc.collect()
    assert all(row["action"] == "retain_quarantine" for row in held["decisions"])
    timing.current += 1
    purged = await gc.collect()
    assert all(row["action"] == "purge" for row in purged["decisions"])
    assert (objects.root / "storage-audit.jsonl").is_file()
    with pytest.raises(StorageError):
        await gc.restore_session("s1")


async def test_restore_session_restores_referenced_output_and_original_identity(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    output_id, _ = await write_output(repository, layout, workspace)
    gc = StorageGC(objects, repository, timing)
    await gc.delete_session("s1")
    await gc.collect()
    with pytest.raises(StorageError) as error:
        await repository.open("s1", create=True)
    assert error.value.code == "session_quarantined"
    await gc.restore_session("s1")
    store = await repository.open("s1")
    try:
        snapshot = await store.snapshot()
        assert snapshot.header.session_id == "s1"
        output = await FileToolOutputStore(
            layout.sessions, "s1", store, header_reader=FileSessionHeaderReader(layout.sessions)
        ).read(output_id, 2500, 10, 1024)
        assert len(output.text.splitlines()) == 10
        assert (layout.sessions / "s1" / "tool-results" / (output_id + ".txt")).exists()
    finally:
        await store.aclose()


async def test_fork_reference_protects_output_when_original_session_is_deleted(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    output_id, _ = await write_output(repository, layout, workspace)
    source = await repository.open("s1")
    leaf = (await source.snapshot()).leaf_id
    await source.aclose()
    forked = await repository.fork("s1", leaf, "s2")
    await forked.aclose()
    gc = StorageGC(objects, repository, timing)
    await gc.delete_session("s1")
    collected = await gc.collect()
    output_rows = [row for row in collected["decisions"] if row["type"] == "output"]
    assert output_rows[0]["action"] == "keep_referenced"
    assert (layout.sessions / "s1" / "tool-results" / (output_id + ".txt")).exists()


async def test_live_reference_and_partial_files_stay_protected(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    output_id, _ = await write_output(repository, layout, workspace)
    gc = StorageGC(objects, repository, timing)
    await gc.delete_session("s1")
    identifier = f"output:{layout.project.name}:s1:{output_id}"
    gc.active_references = lambda: (identifier,)
    kept = await gc.collect()
    assert any(
        row["object_id"] == identifier and row["action"] == "keep_referenced"
        for row in kept["decisions"]
    )
    results = layout.sessions / "s1" / "tool-results"
    (results / "owned.partial").write_bytes(b"unfinished")
    protected = await gc.collect(dry_run=True)
    assert any(
        row["type"] == "partial" and row["action"] == "keep_protected"
        for row in protected["decisions"]
    )


async def test_invalid_reference_log_stops_gc_without_deleting_objects(tmp_path):
    workspace, layout, timing, repository, objects = await setup_objects(tmp_path)
    output_id, _ = await write_output(repository, layout, workspace)
    from pycode.session.adapters.storage.session import open_database

    database = open_database(layout.sessions / "s1" / "session.sqlite")
    database.put(("entries",), "invalid", {"sequence": 99, "entry": {"invalid": "record"}})
    database.conn.close()
    with pytest.raises(StorageError):
        await StorageGC(objects, repository, timing).collect()
    assert (layout.sessions / "s1" / "tool-results" / (output_id + ".txt")).exists()


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
    await write_output(repository, layout, workspace)
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
