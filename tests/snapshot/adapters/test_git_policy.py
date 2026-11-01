import os

import pytest

from pycode.kernel import SnapshotError
from pycode.snapshot.adapters.git_tree import MAX_UNTRACKED_BYTES, GitSnapshotStore
from tests.snapshot.adapters.test_git_tree import git, repository


async def test_ignored_state_large_untracked_and_tracked_binary_policy(tmp_path):
    workspace = repository(tmp_path)
    (workspace / ".gitignore").write_text("*.ignored\n")
    (workspace / "ignored.ignored").write_text("private")
    (workspace / "large-new.bin").write_bytes(b"x" * (MAX_UNTRACKED_BYTES + 1))
    (workspace / "tracked.bin").write_bytes(b"\x00x" * (MAX_UNTRACKED_BYTES // 2 + 1))
    (workspace / ".pycode").mkdir()
    (workspace / ".pycode/state.json").write_text("{}")
    git(workspace, "add", ".gitignore", "tracked.bin")
    git(workspace, "commit", "-qm", "initial")
    store = GitSnapshotStore(workspace, tmp_path / "snapshots")
    before = await store.capture()
    assert "tracked.bin" in before.files
    assert "large-new.bin" not in before.files and "ignored.ignored" not in before.files
    assert ".pycode/state.json" not in before.files
    (workspace / "ignored.ignored").unlink()
    assert (await store.capture()).tree_id == before.tree_id
    (workspace / "tracked.bin").write_bytes(b"\x00changed")
    after = await store.capture()
    changes = await store.diff(before.tree_id, after.tree_id)
    assert len(changes) == 1 and changes[0].path == "tracked.bin" and changes[0].binary
    plan = await store.preview({"tracked.bin": before.tree_id}, expected_tree_id=after.tree_id)
    await store.restore(plan)
    assert (workspace / "tracked.bin").read_bytes() == b"\x00x" * (MAX_UNTRACKED_BYTES // 2 + 1)


async def test_snapshot_scope_is_location_and_outside_change_is_left_untouched(tmp_path):
    root = repository(tmp_path)
    scope = root / "scope"
    scope.mkdir()
    (scope / "one").write_text("old")
    (root / "outside").write_text("outside")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    store = GitSnapshotStore(scope, tmp_path / "snapshots")
    before = await store.capture()
    assert set(before.files) == {"scope/one"}
    (scope / "one").write_text("new")
    (root / "outside").write_text("external outside")
    after = await store.capture()
    assert [change.path for change in await store.diff(before.tree_id, after.tree_id)] == [
        "scope/one"
    ]
    await store.restore(
        await store.preview({"scope/one": before.tree_id}, expected_tree_id=after.tree_id)
    )
    assert (scope / "one").read_text() == "old"
    assert (root / "outside").read_text() == "external outside"
    for name in ("../outside", "outside", ".git/config"):
        with pytest.raises(SnapshotError):
            await store.preview({name: before.tree_id})


async def test_staged_index_and_stash_are_preserved_through_capture_and_restore(tmp_path):
    root = repository(tmp_path)
    (root / "file").write_text("committed")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    (root / "file").write_text("stash content")
    git(root, "stash", "push", "-qm", "preserve stash")
    (root / "file").write_text("staged")
    git(root, "add", "file")
    (root / "file").write_text("working")
    index = (root / ".git/index").read_bytes()
    staged, head, stash = (
        git(root, "show", ":file"),
        git(root, "rev-parse", "HEAD"),
        git(root, "rev-parse", "refs/stash"),
    )
    store = GitSnapshotStore(root, tmp_path / "storage")
    before = await store.capture()
    (root / "file").write_text("later")
    after = await store.capture()
    await store.restore(
        await store.preview({"file": before.tree_id}, expected_tree_id=after.tree_id)
    )
    assert (root / "file").read_text() == "working"
    assert (root / ".git/index").read_bytes() == index
    assert git(root, "show", ":file") == staged
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "rev-parse", "refs/stash") == stash


async def test_file_symlink_is_saved_without_following_its_target(tmp_path):
    root = repository(tmp_path)
    (root / "target").write_text("one")
    try:
        os.symlink("target", root / "link")
    except OSError as error:
        pytest.skip("Host cannot create file symlinks: " + str(error))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    store = GitSnapshotStore(root, tmp_path / "storage")
    before = await store.capture()
    assert before.files["link"].mode == "120000"
    (root / "link").unlink()
    os.symlink("missing-target", root / "link")
    after = await store.capture()
    await store.restore(
        await store.preview({"link": before.tree_id}, expected_tree_id=after.tree_id)
    )
    assert os.readlink(root / "link") == "target" and (root / "target").read_text() == "one"
