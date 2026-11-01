import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict

import pytest

from pycode.kernel import SnapshotError
from pycode.snapshot.adapters.git_tree import GitSnapshotStore
from tests.snapshot.adapters.test_git_tree import git, repository


@pytest.mark.parametrize("compensate", [False, True], ids=["continue", "compensate"])
async def test_restore_process_death_reconciles_paths_and_keeps_recovery_snapshot(
    tmp_path, compensate
):
    root = repository(tmp_path)
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("original")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    storage = tmp_path / "snapshots"
    store = GitSnapshotStore(root, storage)
    before = await store.capture()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("changed")
    after = await store.capture()
    plan = await store.preview(
        {"a.txt": before.tree_id, "b.txt": before.tree_id}, expected_tree_id=after.tree_id
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(asdict(plan)), encoding="utf-8")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.support.restore_crash_worker",
        str(root),
        str(storage),
        str(plan_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
        assert child.returncode == 75, (stdout.decode(), stderr.decode())
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert (root / "a.txt").read_text() == "original"
    assert (root / "b.txt").read_text() == "changed"
    restarted = GitSnapshotStore(root, storage)
    assert (await restarted.recover(plan.restore_id, compensate=compensate)).status == "completed"
    expected = "changed" if compensate else "original"
    assert all((root / name).read_text() == expected for name in ("a.txt", "b.txt"))
    assert (await restarted.get(plan.before_tree_id)).tree_id == after.tree_id
    assert (await restarted.recover(plan.restore_id)).status == (
        "compensated" if compensate else "completed"
    )
    assert not list(root.glob("*.pycode-restore-*.partial"))


async def test_compensation_crash_reuses_one_durable_reverse_transaction(tmp_path):
    root = repository(tmp_path)
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("original", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    storage = tmp_path / "snapshots"
    store = GitSnapshotStore(root, storage)
    before = await store.capture()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("changed", encoding="utf-8")
    after = await store.capture()
    plan = await store.preview(
        {"a.txt": before.tree_id, "b.txt": before.tree_id}, expected_tree_id=after.tree_id
    )
    await store.restore(plan)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(asdict(plan)), encoding="utf-8")
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.support.restore_crash_worker",
        str(root),
        str(storage),
        str(plan_path),
        "compensate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
        assert child.returncode == 75, (stdout.decode(), stderr.decode())
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert (root / "a.txt").read_text(encoding="utf-8") == "changed"
    assert (root / "b.txt").read_text(encoding="utf-8") == "original"
    original_path = storage / "restores" / f"{plan.restore_id}.json"
    journal = json.loads(original_path.read_text(encoding="utf-8"))["value"]
    assert journal["status"] == "compensating"
    compensation_id = journal["compensation_id"]
    assert compensation_id is not None
    restarted = GitSnapshotStore(root, storage)
    with pytest.raises(SnapshotError) as error:
        await restarted.recover(plan.restore_id)
    assert error.value.code == "restore_compensation_pending"
    restored = await restarted.recover(plan.restore_id, compensate=True)
    assert restored.plan.restore_id == compensation_id and restored.status == "completed"
    assert all(
        (root / name).read_text(encoding="utf-8") == "changed" for name in ("a.txt", "b.txt")
    )
    assert len(list((storage / "restores").glob("*.json"))) == 2
    assert not list(root.glob(".*.pycode-restore-*.partial"))
    assert (await restarted.recover(plan.restore_id, compensate=True)).status == "compensated"
