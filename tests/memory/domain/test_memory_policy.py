"""Pure memory file format and write-filter policy."""

import pytest

from pycode.kernel.domain.errors import StorageError, ToolError
from pycode.memory.domain.memory import (
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    check_memory_write,
    check_slug,
    format_note,
    note_fields,
    parse_note,
)


def test_note_round_trips_through_frontmatter():
    text = format_note("Testing: pytest", "2026-01-01T00:00:00+00:00", "Run pytest -x.")
    assert parse_note(text, "m.md") == (
        "Testing: pytest",
        "2026-01-01T00:00:00+00:00",
        "Run pytest -x.",
    )
    # A hand-written file may use an unquoted date and omit "updated".
    assert parse_note("---\ndescription: d\nupdated: 2026-01-02\n---\nbody\n", "m.md")[1] == (
        "2026-01-02"
    )
    assert parse_note("---\ndescription: d\n---\nbody\n", "m.md")[1] == ""
    with pytest.raises(StorageError):
        parse_note("no frontmatter", "m.md")
    with pytest.raises(StorageError):
        parse_note("---\nupdated: x\n---\nbody", "m.md")


def test_tool_and_store_manager_values_need_description_and_content():
    value = {"description": "d", "content": "c"}
    assert note_fields({"content": value}) == ("d", "c")
    assert note_fields({"kind": "MemoryNote", "content": value}) == ("d", "c")
    with pytest.raises(ToolError):
        note_fields({"content": "plain text"})


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ABCDEFGHIJKLMNOP1234",
        '"api_key": "hunter2"',
        "my api-key: something",
        "github token:abc",
        "password: hunter2",
        "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key is set",
    ],
)
def test_secret_like_content_is_rejected(secret):
    with pytest.raises(ToolError) as error:
        check_memory_write("note", secret, 1)
    assert error.value.code == "memory_secret_rejected"


def test_length_count_and_slug_limits():
    check_memory_write("d", "x" * (MAX_ENTRY_CHARS - 1), MAX_ENTRIES)
    with pytest.raises(ToolError) as error:
        check_memory_write("d", "x" * MAX_ENTRY_CHARS, 1)
    assert error.value.code == "memory_entry_too_long"
    with pytest.raises(ToolError) as error:
        check_memory_write("d", "ok", MAX_ENTRIES + 1)
    assert error.value.code == "memory_limit_reached"
    check_slug("01234567-89ab-cdef-0123-456789abcdef")
    for slug in ("Upper", "a--b", "-a", "a" * 65, "../x"):
        with pytest.raises(ToolError):
            check_slug(slug)
