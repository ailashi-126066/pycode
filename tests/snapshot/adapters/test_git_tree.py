import json
import os
import subprocess
from pathlib import Path

import pytest

from pycode.kernel import SnapshotError
from pycode.snapshot.adapters.git_tree import GitSnapshotStore

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/snapshot-baselines.json").read_text(encoding="utf-8")
)


def git(workspace, *arguments):
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(workspace), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return result.stdout


def repository(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    git(workspace, "init", "-q")
    git(workspace, "config", "user.name", "Snapshot Test")
    git(workspace, "config", "user.email", "snapshot@example.invalid")
    git(workspace, "config", "commit.gpgsign", "false")
    return workspace


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
async def test_source_capture_restore_vectors_in_temporary_git_only(tmp_path, case):
    if case.get("git") is False:
        workspace = tmp_path / "plain"
        workspace.mkdir()
        store = GitSnapshotStore(workspace, tmp_path / "storage")
        assert await store.capture() is None
        assert not (workspace / ".git").exists() and not (tmp_path / "storage").exists()
        return
    workspace = repository(tmp_path)
    for name, content in case["before"].items():
        (workspace / name).write_text(content, encoding="utf-8")
    git(workspace, "add", ".")
    git(workspace, "commit", "--allow-empty", "-qm", "initial")
    index = (workspace / ".git/index").read_bytes() if (workspace / ".git/index").exists() else None
    head = git(workspace, "rev-parse", "HEAD")
    store = GitSnapshotStore(workspace, tmp_path / "storage")
    before = await store.capture()
    for name in case["before"]:
        if name not in case["after"]:
            (workspace / name).unlink()
    for name, content in case["after"].items():
        (workspace / name).write_text(content, encoding="utf-8")
    after = await store.capture()
    plan = await store.preview(
        {name: before.tree_id for name in case["restore_paths"]}, expected_tree_id=after.tree_id
    )
    assert not plan.conflicts
    assert (await store.restore(plan)).status == "completed"
    for name in set(case["before"]) | set(case["after"]):
        path = workspace / name
        if name in case["expected"]:
            assert path.read_text(encoding="utf-8") == case["expected"][name]
        else:
            assert not path.exists()
    assert git(workspace, "rev-parse", "HEAD") == head
    if index is not None:
        assert (workspace / ".git/index").read_bytes() == index


async def test_external_conflict_stops_entire_restore_and_keeps_unrelated_paths(tmp_path):
    workspace = repository(tmp_path)
    for name in ("one", "two", "unrelated"):
        (workspace / name).write_text("old")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "initial")
    store = GitSnapshotStore(workspace, tmp_path / "storage")
    before = await store.capture()
    (workspace / "one").write_text("new")
    (workspace / "two").write_text("new")
    after = await store.capture()
    plan = await store.preview(
        {"one": before.tree_id, "two": before.tree_id}, expected_tree_id=after.tree_id
    )
    (workspace / "two").write_text("external edit")
    with pytest.raises(SnapshotError) as error:
        await store.restore(plan)
    assert error.value.code == "restore_conflict"
    assert (workspace / "one").read_text() == "new"
    assert (workspace / "two").read_text() == "external edit"
    assert (workspace / "unrelated").read_text() == "old"
