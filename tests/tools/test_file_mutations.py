"""Native Backend locking, exact edits and optimistic version protection."""

import asyncio

from pycode.tools.backend import PycodeBackend


async def test_read_write_edit_require_current_hash_and_preserve_unrelated_text(tmp_path):
    backend = PycodeBackend(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("before target after", encoding="utf-8")
    assert (await backend.aedit("/file.txt", "target", "changed")).error.startswith("file_not_read")
    await backend.aread("/file.txt")
    assert (await backend.aedit("/file.txt", "target", "changed")).error is None
    assert path.read_text() == "before changed after"
    path.write_text("external", encoding="utf-8")
    assert (await backend.awrite("/file.txt", "bad")).error.startswith("file_changed")
    assert path.read_text() == "external"
    assert (await backend.awrite("/../outside", "bad")).error.startswith("outside_workspace")


async def test_parallel_same_path_edits_hold_the_entire_read_replace_write_lock(tmp_path):
    backend = PycodeBackend(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("first second", encoding="utf-8")
    await backend.aread("/file.txt")
    results = await asyncio.gather(
        backend.aedit("/file.txt", "first", "ONE"), backend.aedit("/file.txt", "second", "TWO")
    )
    assert all(result.error is None for result in results)
    assert path.read_text() == "ONE TWO"
    assert not list(tmp_path.glob("*.partial"))


async def test_exact_edit_reports_missing_and_repeated_matches_without_writing(tmp_path):
    backend = PycodeBackend(tmp_path)
    path = tmp_path / "file.txt"
    path.write_text("same same", encoding="utf-8")
    await backend.aread("/file.txt")
    for old in ("same", "missing"):
        assert (await backend.aedit("/file.txt", old, "new")).error.startswith("edit_not_unique")
    assert path.read_text() == "same same"
