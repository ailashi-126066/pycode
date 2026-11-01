import json
import os
import subprocess
from pathlib import Path

import pytest

from pycode.snapshot.store import GitSnapshotStore

VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures/snapshot-baselines.json").read_text(encoding="utf-8")
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
    await store.capture()
    for name in case["restore_paths"]:
        await store.write_path(name, before.tree_id)
    for name in set(case["before"]) | set(case["after"]):
        path = workspace / name
        if name in case["expected"]:
            assert path.read_text(encoding="utf-8") == case["expected"][name]
        else:
            assert not path.exists()
    assert git(workspace, "rev-parse", "HEAD") == head
    if index is not None:
        assert (workspace / ".git/index").read_bytes() == index
