"""ScopedBackend forwards to the real backend, adding a path guard and read-only mode."""

import pytest
from deepagents.backends.protocol import BackendProtocol

from pycode.kernel import ToolError
from pycode.tools import ScopedBackend
from pycode.tools.adapters.backend import PycodeBackend


def scoped(tmp_path, **options):
    return ScopedBackend(PycodeBackend(tmp_path), tmp_path, **options)


async def test_forwards_reads_writes_and_edits(tmp_path):
    backend = scoped(tmp_path, read_only=False)
    assert isinstance(backend, BackendProtocol)
    assert (await backend.awrite("/a.txt", "one\n")).error is None
    assert (await backend.aread("/a.txt")).file_data["content"] == "one\n"
    assert (await backend.aedit("/a.txt", "one", "two")).error is None
    assert (tmp_path / "a.txt").read_text() == "two\n"
    assert [entry["path"] for entry in (await backend.als("/")).entries] == ["/a.txt"]
    assert [match["path"] for match in (await backend.aglob("*.txt")).matches] == ["/a.txt"]


async def test_async_results_match_sync(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    backend = scoped(tmp_path, read_only=False)
    assert await backend.aread("/a.txt", 1, 1) == backend.read("/a.txt", 1, 1)
    assert (await backend.agrep("two")).matches == backend.grep("two").matches


async def test_read_only_rejects_writes(tmp_path):
    (tmp_path / "a.txt").write_text("keep")
    backend = scoped(tmp_path, read_only=True)
    backend.read("/a.txt")
    assert "read_only" in (await backend.awrite("/b.txt", "x")).error
    assert "read_only" in (await backend.aedit("/a.txt", "keep", "lost")).error
    assert backend.upload_files([("/c.txt", b"x")])[0].error == "permission_denied"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.txt"]
    assert (await backend.aread("/a.txt")).file_data["content"] == "keep"


async def test_path_guard_errors_propagate(tmp_path):
    def guard(path):
        if path != "/allowed.md":
            raise ToolError("invalid_path", path)

    backend = scoped(tmp_path, read_only=False, path_guard=guard)
    assert (await backend.awrite("/allowed.md", "ok")).error is None
    for call in (
        lambda: backend.write("/other.md", "x"),
        lambda: backend.read("/other.md"),
        lambda: backend.ls("/"),
        lambda: backend.resolve("/other.md"),
    ):
        with pytest.raises(ToolError, match="/"):
            call()


def test_resolve_stays_under_root(tmp_path):
    backend = scoped(tmp_path, read_only=False)
    assert backend.resolve("/dir/a.txt") == tmp_path.resolve() / "dir" / "a.txt"
    with pytest.raises(ToolError) as error:
        backend.resolve("/../escape")
    assert error.value.code == "path_outside_scope"
