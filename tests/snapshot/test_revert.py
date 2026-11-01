"""Single-step revert boundaries, reversible records and partial-write failures."""

import asyncio
import subprocess

import pytest

from pycode.kernel import Message, SessionHeader, SnapshotError, StorageError, ToolResult
from pycode.session import SqliteSessionStore
from pycode.snapshot.snapshot import SnapshotService
from pycode.snapshot.store import GitSnapshotStore
from tests.support.requests import TestTiming


async def setup(tmp_path, *, git=True):
    workspace = tmp_path / "work"
    workspace.mkdir()
    if git:
        await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(workspace)], check=True)
    history = SqliteSessionStore(
        tmp_path / "session", SessionHeader("s", str(workspace), "t"), TestTiming()
    )
    snapshots = GitSnapshotStore(workspace, tmp_path / "snapshots")
    service = SnapshotService(history, snapshots)
    root = await history.append_message(Message("u", "user", "change files"))
    return workspace, history, snapshots, service, root


async def change(workspace, history, snapshots, values, identifier="t"):
    before = await snapshots.capture()
    for name, value in values.items():
        (workspace / name).write_text(value)
    after = await snapshots.capture()
    result = ToolResult(
        identifier,
        "write",
        "ok",
        details={
            "file_snapshot": {
                "before": before.tree_id,
                "after": after.tree_id,
                "paths": list(values),
            }
        },
    )
    return await history.append_message(Message(identifier, "tool_result", tool_result=result))


@pytest.mark.parametrize("mode", ["dialogue", "code", "both"])
async def test_revert_and_undo_restore_selected_parts(tmp_path, mode):
    workspace, history, store, service, root = await setup(tmp_path)
    try:
        (workspace / "a").write_text("before")
        last = await change(workspace, history, store, {"a": "after"})
        record = await service.revert(root, mode)
        assert (workspace / "a").read_text() == ("after" if mode == "dialogue" else "before")
        assert record.parent_id == (last if mode == "code" else root)
        undo = await service.revert(record.id, mode)
        assert (workspace / "a").read_text() == "after"
        assert undo.parent_id == (record.id if mode == "code" else last)
        await service.revert(undo.id, mode)
        assert (workspace / "a").read_text() == ("after" if mode == "dialogue" else "before")
    finally:
        await history.aclose()


async def test_ordinary_target_excludes_itself_and_root_includes_all(tmp_path):
    workspace, history, store, service, root = await setup(tmp_path)
    try:
        target = await change(workspace, history, store, {"a": "one"})
        await change(workspace, history, store, {"a": "two"}, "t2")
        await service.revert(target, "code")
        assert (workspace / "a").read_text() == "one"
        await service.revert("root", "code")
        assert not (workspace / "a").exists()
    finally:
        await history.aclose()


async def test_conflict_changes_no_files(tmp_path):
    workspace, history, store, service, root = await setup(tmp_path)
    try:
        await change(workspace, history, store, {"a": "one", "b": "one"})
        (workspace / "b").write_text("external")
        with pytest.raises(SnapshotError) as caught:
            await service.revert(root, "both")
        assert caught.value.code == "revert_conflict"
        assert (workspace / "a").read_text() == "one"
        assert (workspace / "b").read_text() == "external"
    finally:
        await history.aclose()


async def test_partial_write_records_actual_changes_and_can_undo(tmp_path, monkeypatch):
    workspace, history, store, service, root = await setup(tmp_path)
    try:
        (workspace / "a").write_text("before")
        (workspace / "b").write_text("before")
        await change(workspace, history, store, {"a": "after", "b": "after"})
        write = store.write_path

        async def failing(name, tree):
            if name == "b":
                raise OSError("disk failure")
            await write(name, tree)

        monkeypatch.setattr(store, "write_path", failing)
        with pytest.raises(SnapshotError) as caught:
            await service.revert(root, "both")
        assert caught.value.code == "revert_partial"
        state = await history.snapshot()
        record = state.index[state.leaf_id]
        assert tuple(record.data["paths"]) == ("a",)
        assert (workspace / "b").read_text() == "after"
        monkeypatch.setattr(store, "write_path", write)
        await service.revert(record.id, "both")
        assert (workspace / "a").read_text() == "after"
    finally:
        await history.aclose()


async def test_non_git_dialogue_and_non_leaf_revert(tmp_path):
    _, history, _, service, root = await setup(tmp_path, git=False)
    try:
        record = await service.revert(root, "dialogue")
        await history.append_message(Message("later", "user", "continue"))
        with pytest.raises(StorageError, match="latest revert"):
            await service.revert(record.id, "dialogue")
        with pytest.raises(SnapshotError) as caught:
            await service.revert(root, "code")
        assert caught.value.code == "snapshot_unavailable"
    finally:
        await history.aclose()


@pytest.mark.parametrize("failure", ["snapshot", "record"])
async def test_partial_failure_never_promises_missing_recovery_record(
    tmp_path, monkeypatch, failure
):
    workspace, history, store, service, root = await setup(tmp_path)
    try:
        await change(workspace, history, store, {"a": "after"})
        capture = store.capture
        count = 0

        async def capture_failure():
            nonlocal count
            count += 1
            if count > 1:
                raise OSError("snapshot unavailable")
            return await capture()

        async def write_failure(*args):
            raise OSError("write unavailable")

        async def record_failure(*args):
            raise OSError("storage unavailable")

        monkeypatch.setattr(store, "write_path", write_failure)
        if failure == "snapshot":
            monkeypatch.setattr(store, "capture", capture_failure)
        else:
            monkeypatch.setattr(service, "record", record_failure)
        with pytest.raises(SnapshotError, match="recovery record could not be saved") as caught:
            await service.revert(root, "both")
        assert caught.value.code == "revert_partial"
        assert "undo revert" not in str(caught.value)
    finally:
        await history.aclose()


async def test_other_branch_target_is_rejected(tmp_path):
    _, history, _, service, root = await setup(tmp_path, git=False)
    try:
        other = await history.append_message(Message("other", "user", "other branch"))
        await history.branch(root)
        with pytest.raises(StorageError) as caught:
            await service.revert(other, "dialogue")
        assert caught.value.code == "revert_branch_conflict"
    finally:
        await history.aclose()
