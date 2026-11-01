"""Behavior of the actual Deep Agents BackendProtocol implementation."""

import asyncio

import pytest
from deepagents.backends.protocol import BackendProtocol

from pycode.kernel import ToolError
from pycode.plans.store import FilePlanStore
from pycode.tools.backend import PycodeBackend


async def test_backend_reads_writes_edits_and_rejects_external_changes(tmp_path):
    backend = PycodeBackend(tmp_path)
    assert isinstance(backend, BackendProtocol)
    assert (await backend.awrite("/main.py", "one\ntwo\n")).error is None
    result = await backend.aread("/main.py", 1, 1)
    assert result.file_data["content"] == "two"
    assert result.start_line == 2
    assert (await backend.aedit("/main.py", "two", "three")).error is None
    assert (tmp_path / "main.py").read_text() == "one\nthree\n"
    (tmp_path / "main.py").write_text("external change")
    assert "file_changed" in (await backend.awrite("/main.py", "lost")).error
    assert (tmp_path / "main.py").read_text() == "external change"


async def test_existing_files_require_read_receipt_and_unique_edit(tmp_path):
    (tmp_path / "code.txt").write_text("a a")
    backend = PycodeBackend(tmp_path)
    assert "file_not_read" in (await backend.awrite("/code.txt", "overwrite")).error
    assert "file_not_read" in (await backend.aedit("/code.txt", "a", "b")).error
    await backend.aread("/code.txt")
    assert "edit_not_unique" in (await backend.aedit("/code.txt", "a", "b")).error
    assert (await backend.aedit("/code.txt", "a", "b", True)).occurrences == 2


async def test_workspace_paths_and_readonly_backend(tmp_path):
    backend = PycodeBackend(tmp_path)
    for path in ("/../escape", "/.git/config", "/.pycode/secret", "relative", "/a\\b"):
        assert "outside_workspace" in (await backend.awrite(path, "secret")).error
        assert "outside_workspace" in (await backend.aread(path)).error
    readonly = PycodeBackend(tmp_path, read_only=True)
    assert "permission_denied" in (await readonly.awrite("/new", "data")).error
    assert not (tmp_path / "new").exists()


@pytest.mark.requires_rg
async def test_rg_honors_gitignore_without_git_and_excludes_internal_files(tmp_path):
    (tmp_path / ".gitignore").write_text("generated/\n")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "ignored.py").write_text("needle")
    (tmp_path / ".pycode").mkdir()
    (tmp_path / ".pycode" / "hidden.py").write_text("needle")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "main.py").write_text("needle 123\nnext")
    (tmp_path / "root.py").write_text("needle 456")
    backend = PycodeBackend(tmp_path)
    glob = await backend.aglob("*.py")
    assert glob.error is None
    assert [item["path"] for item in glob.matches] == ["/nested/main.py", "/root.py"]
    grep = await backend.agrep("needle [0-9]+")
    assert grep.error is None
    assert [item["path"] for item in grep.matches] == ["/nested/main.py", "/root.py"]
    assert (await backend.agrep("not present")).matches == []
    invalid = await backend.agrep("[")
    assert invalid.error and "search_incomplete" in invalid.error


@pytest.mark.requires_rg
async def test_rg_limits_are_explicit_and_missing_rg_is_an_error(tmp_path):
    (tmp_path / "main.py").write_text("hit\nhit\nhit\n")
    backend = PycodeBackend(tmp_path)
    result = await backend.agrep("hit", max_count=2)
    assert len(result.matches) == 2
    assert result.truncated
    backend.rg = None
    assert "rg_unavailable" in (await backend.agrep("hit")).error


@pytest.mark.requires_rg
async def test_uncreated_plan_namespace_is_empty_without_masking_missing_workspace(tmp_path):
    root = tmp_path / "plans"
    plans = FilePlanStore(root, backend_factory=PycodeBackend)
    backend = plans.backend
    assert not root.exists()
    assert backend.ls("/").entries == [] and backend.ls("/").error is None
    for result in (
        backend.glob("*.md"),
        backend.grep("needle"),
        await backend.aglob("*.md"),
        await backend.agrep("needle"),
    ):
        assert result.matches == [] and result.error is None and not result.truncated
    assert not root.exists()
    with pytest.raises(ToolError, match="current Plan"):
        backend.read("/other.md")
    assert "file_not_found" in PycodeBackend(root).ls("/").error
    assert "file_not_found" in (await PycodeBackend(root).aglob("*.md")).error
    plans.configure("a" * 32)
    assert backend.write("/" + "a" * 32 + ".md", "needle").error is None
    assert [item["path"] for item in (await backend.aglob("*.md")).matches] == [
        "/" + "a" * 32 + ".md"
    ]


async def test_concurrent_edits_preserve_path_lock_and_version(tmp_path):
    backend = PycodeBackend(tmp_path)
    await backend.awrite("/main.txt", "old")
    results = await asyncio.gather(
        backend.aedit("/main.txt", "old", "first"),
        backend.aedit("/main.txt", "old", "second"),
    )
    assert sum(result.error is None for result in results) == 1
    assert (tmp_path / "main.txt").read_text() in {"first", "second"}


@pytest.mark.requires_rg
async def test_plan_delegate_scopes_rg_before_opening_other_plan_content(tmp_path):
    root = tmp_path / "plans"
    root.mkdir()
    active = "a" * 32 + ".md"
    inactive = "b" * 32 + ".md"
    (root / active).write_text("active needle\n")
    (root / inactive).write_text("private inactive needle\n")
    plans = FilePlanStore(root, backend_factory=PycodeBackend)
    backend = plans.backend
    plans.configure("a" * 32)
    command, base = backend.inner.search_command("needle", "/", None)
    assert base == root and command[-1] == active
    assert "." not in command and inactive not in command
    result = await backend.agrep("needle")
    assert [match["path"] for match in result.matches] == ["/" + active]
    with pytest.raises(ToolError, match="current Plan"):
        backend.read("/" + inactive)
    with pytest.raises(ToolError, match="current Plan"):
        backend.download_files(["/" + inactive])
    plans.deactivate()
    assert backend.inner.search_command("needle", "/", None)[0] is None
    assert (await backend.agrep("needle")).matches == []
