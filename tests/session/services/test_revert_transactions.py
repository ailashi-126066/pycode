import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace

import pytest

from pycode.kernel import EntryDraft, Message, SessionHeader, SessionTree, StorageError
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.session.domain.revert_state import pending_revert
from pycode.session.services.revert import RevertService
from pycode.snapshot.adapters.git_tree import GitSnapshotStore
from tests.snapshot.adapters.test_git_tree import git, repository
from tests.support.requests import TestTiming


async def exit_at(tmp_path, root, plan_path, stage):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.support.revert_crash_worker",
        str(root),
        str(tmp_path / "snapshots"),
        str(tmp_path / "sessions/s1"),
        str(plan_path),
        stage,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
        assert child.returncode == 76, (stdout.decode(), stderr.decode())
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def scenario(tmp_path, mode):
    root = repository(tmp_path)
    (root / "file").write_text("old", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    files = GitSnapshotStore(root, tmp_path / "snapshots")
    before = await files.capture()
    (root / "file").write_text("new", encoding="utf-8")
    after = await files.capture()
    history = SqliteSessionStore(
        tmp_path / "sessions/s1", SessionHeader("s1", str(root), "t"), TestTiming()
    )
    target = await history.append_message(Message("u1", "user", "before"))
    changed = await history.append_message(Message("a1", "assistant", "changed"))
    await history.append_entry(
        EntryDraft(
            "custom",
            data={
                "kind": "file_snapshot",
                "before": before.tree_id,
                "after": after.tree_id,
                "paths": ["file"],
                "message_entry_id": changed,
            },
        )
    )
    service = RevertService(history, files)
    plan = await service.preview(target, mode)
    return root, files, history, service, plan


@pytest.mark.parametrize("mode", ["dialogue", "code", "both"])
async def test_completed_revert_retry_preserves_later_messages_and_external_edits(tmp_path, mode):
    root, _, history, service, plan = await scenario(tmp_path, mode)
    await service.apply(plan)
    await history.aclose()
    history = SqliteSessionStore(
        tmp_path / "sessions/s1", SessionHeader("s1", str(root), "t"), TestTiming()
    )
    service = RevertService(history, GitSnapshotStore(root, tmp_path / "snapshots"))
    try:
        await history.append_message(Message("u2", "user", "later"))
        (root / "file").write_text("external", encoding="utf-8")
        before_retry = await history.snapshot()
        await service.apply(plan)
        await service.recover(plan.transaction_id)
        assert await history.snapshot() == before_retry
        assert (root / "file").read_text(encoding="utf-8") == "external"
        with pytest.raises(StorageError) as error:
            await service.apply(replace(plan, target_leaf_id=None))
        assert error.value.code == "revert_plan_changed"
        with pytest.raises(StorageError) as error:
            await service.recover(plan.transaction_id, compensate=True)
        assert error.value.code == "revert_already_completed"
    finally:
        await history.aclose()


async def test_new_code_preview_uses_prior_restore_version_but_rejects_external_changes(tmp_path):
    root, _, history, service, plan = await scenario(tmp_path, "code")
    try:
        await service.apply(plan)
        repeated = await service.preview(plan.target_leaf_id, "code")
        assert not repeated.files.conflicts
        assert all(not item.patch for item in repeated.files.files)
        await service.apply(repeated)
        (root / "file").write_text("external", encoding="utf-8")
        conflicting = await service.preview(plan.target_leaf_id, "code")
        assert conflicting.files.conflicts == ("file",)
    finally:
        await history.aclose()


@pytest.mark.parametrize(
    "mode,stage",
    [
        ("dialogue", "after-start"),
        ("dialogue", "after-branch"),
        ("both", "after-start"),
        ("both", "before-branch"),
        ("both", "after-branch"),
        ("both", "after-reminder"),
        ("code", "after-start"),
        ("code", "after-reminder"),
    ],
)
@pytest.mark.parametrize("compensate", [False, True], ids=["continue", "compensate"])
async def test_process_exit_recovers_revert_even_after_start_leaves_active_branch(
    tmp_path,
    mode,
    stage,
    compensate,
):
    root, files, history, _, plan = await scenario(tmp_path, mode)
    await history.aclose()
    plan_path = tmp_path / "revert.json"
    plan_path.write_text(json.dumps(asdict(plan)), encoding="utf-8")
    await exit_at(tmp_path, root, plan_path, stage)
    history = SqliteSessionStore(
        tmp_path / "sessions/s1", SessionHeader("s1", str(root), "t"), TestTiming()
    )
    service = RevertService(history, files)
    try:
        assert pending_revert(await history.snapshot()) is not None
        await service.recover(plan.transaction_id, compensate=compensate)
        snapshot = await history.snapshot()
        assert pending_revert(snapshot) is None
        path = SessionTree(snapshot.entries).path(snapshot.leaf_id)
        assert any(entry.message and entry.message.id == "a1" for entry in path) == (
            mode == "code" or compensate
        )
        assert (root / "file").read_text(encoding="utf-8") == (
            "new" if mode == "dialogue" or compensate else "old"
        )
        await service.recover(plan.transaction_id, compensate=compensate)
        assert await history.snapshot() == snapshot
        assert sum(entry.type == "custom_message" for entry in path) == (
            0 if mode == "dialogue" else 1
        )
    finally:
        await history.aclose()


@pytest.mark.parametrize("stage", ["before-branch", "after-branch", "after-reminder"])
async def test_compensation_process_exit_preserves_action_and_recovers_original_branch(
    tmp_path,
    stage,
):
    root, files, history, _, plan = await scenario(tmp_path, "both")
    await history.aclose()
    plan_path = tmp_path / "revert.json"
    plan_path.write_text(json.dumps(asdict(plan)), encoding="utf-8")
    await exit_at(tmp_path, root, plan_path, "after-reminder")
    await exit_at(tmp_path, root, plan_path, "compensate-" + stage)
    history = SqliteSessionStore(
        tmp_path / "sessions/s1", SessionHeader("s1", str(root), "t"), TestTiming()
    )
    service = RevertService(history, files)
    try:
        with pytest.raises(StorageError) as error:
            await service.recover(plan.transaction_id)
        assert error.value.code == "revert_recovery_conflict"
        await service.recover(plan.transaction_id, compensate=True)
        snapshot = await history.snapshot()
        assert pending_revert(snapshot) is None
        path = SessionTree(snapshot.entries).path(snapshot.leaf_id)
        assert any(entry.message and entry.message.id == "a1" for entry in path)
        assert (root / "file").read_text(encoding="utf-8") == "new"
        await service.recover(plan.transaction_id, compensate=True)
        assert await history.snapshot() == snapshot
    finally:
        await history.aclose()
