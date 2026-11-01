"""The LangGraph BaseStore over one Markdown file per memory."""

import pytest

from pycode.kernel import StorageError, ToolError
from pycode.memory.adapters.storage.markdown_memory import MarkdownMemoryStore
from pycode.memory.domain.memory import NAMESPACES

USER, PROJECT = NAMESPACES["user"], NAMESPACES["project"]


def note(description, content):
    return {"kind": "MemoryNote", "content": {"description": description, "content": content}}


async def test_put_get_search_delete_one_file_per_memory(tmp_path):
    store = MarkdownMemoryStore({USER: tmp_path / "user", PROJECT: tmp_path / "project"})
    await store.aput(USER, "older", note("Older", "first"))
    await store.aput(USER, "newer", note("Newer", "second"))
    await store.aput(PROJECT, "p", note("Project", "third"))
    assert sorted(path.name for path in (tmp_path / "user").glob("*.md")) == [
        "newer.md",
        "older.md",
    ]
    assert (await store.aget(USER, "older")).value == note("Older", "first")
    assert await store.aget(USER, "missing") is None
    found = await store.asearch(USER, query="anything")
    assert [item.key for item in found] == ["newer", "older"]
    assert {item.key for item in await store.asearch(("memories",))} == {"newer", "older", "p"}
    await store.adelete(USER, "older")
    assert not (tmp_path / "user" / "older.md").exists()
    with pytest.raises(ToolError):
        await store.aput(("memories", "other"), "x", note("d", "c"))
    with pytest.raises(ToolError):
        await store.aput(USER, "../escape", note("d", "c"))


async def test_lenient_view_skips_filtered_entries(tmp_path):
    store = MarkdownMemoryStore({USER: tmp_path})
    with pytest.raises(ToolError):
        await store.aput(USER, "leak", note("d", "password: hunter2"))
    view = store.lenient()
    await view.aput(USER, "leak", note("d", "password: hunter2"))
    assert view.rejected == [{"key": "leak", "reason": "memory_secret_rejected"}]
    assert not list(tmp_path.glob("*.md"))


async def test_mode_settings_and_unreadable_files(tmp_path):
    store = MarkdownMemoryStore({USER: tmp_path})
    assert await store.get_mode(USER) == "auto"
    await store.set_mode(USER, "off")
    assert await store.get_mode(USER) == "off"
    # The old memory.json is not migrated and not read.
    (tmp_path / "memory.json").write_text('{"records": {}}')
    assert await store.asearch(USER) == []
    (tmp_path / "broken.md").write_text("no frontmatter")
    with pytest.raises(StorageError) as error:
        await store.asearch(USER)
    assert error.value.code == "memory_corrupt"
