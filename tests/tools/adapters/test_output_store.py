import hashlib

import pytest

from pycode.kernel import plain_data
from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.history import EntryDraft, SessionHeader
from pycode.kernel.domain.messages import Message, ToolResult
from pycode.kernel.domain.tools import ToolContext
from pycode.kernel.domain.truncation import MAX_BYTES, MAX_LINES
from pycode.session.adapters.storage.header_reader import FileSessionHeaderReader
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.session.adapters.storage.session_repository import SqliteSessionRepository
from pycode.tools.adapters.output_store import FileToolOutputStore
from tests.support.requests import TestTiming


async def setup(tmp_path):
    sessions = tmp_path / "sessions"
    journal = SqliteSessionStore(
        sessions / "s1", SessionHeader("s1", "/project", "created"), TestTiming()
    )
    await journal.append_message(Message("u1", "user", "request"))
    output = FileToolOutputStore(
        sessions, "s1", journal, header_reader=FileSessionHeaderReader(sessions)
    )
    context = ToolContext("s1", "run", "operation", "/project", "call-1", "Bash")
    return journal, output, context


async def save(journal, output, result):
    entry_id = await journal.append_message(
        Message("tool-" + result.output_id, "tool_result", tool_result=result)
    )
    await output.bind(result.output_id, entry_id)
    return entry_id


async def test_large_result_full_text_preview_and_scoped_read_survive_reopen(tmp_path):
    journal, output, context = await setup(tmp_path)
    text = "".join(f"line-{i:04d}\n" for i in range(1, 8001))
    prepared = await output.prepare(
        context, ToolResult("call-1", "Bash", text, session_id="s1"), "tail"
    )
    assert len(prepared.text.encode()) <= MAX_BYTES
    assert len(prepared.text.splitlines()) <= MAX_LINES
    assert "line-8000" in prepared.text
    await save(journal, output, prepared)
    full = tmp_path / "sessions/s1/tool-results" / (prepared.output_id + ".txt")
    assert full.read_text() == text
    assert (
        hashlib.sha256(full.read_bytes()).hexdigest() == output.metadata(prepared.output_id).sha256
    )
    await journal.aclose()
    restored = SqliteSessionStore(
        tmp_path / "sessions/s1", SessionHeader("s1", "/project", "created"), TestTiming()
    )
    output = FileToolOutputStore(
        tmp_path / "sessions",
        "s1",
        restored,
        header_reader=FileSessionHeaderReader(tmp_path / "sessions"),
    )
    selected = await output.read(prepared.output_id, 500, 100, MAX_BYTES)
    assert selected.text.splitlines()[0] == "line-0500"
    assert selected.text.splitlines()[-1] == "line-0599"
    assert selected.next_offset == 600
    assert selected.has_more
    await restored.aclose()


async def test_small_results_read_original_history_without_full_file_copy(tmp_path):
    journal, output, context = await setup(tmp_path)
    prepared = await output.prepare(
        context, ToolResult("call-1", "Read", "one\ntwo\n", session_id="s1"), "head"
    )
    await save(journal, output, prepared)
    assert not list((tmp_path / "sessions/s1/tool-results").glob("*.txt"))
    assert (await output.read(prepared.output_id, 2, 1, MAX_BYTES)).text == "two\n"
    await journal.aclose()


async def test_giant_single_line_readback_progresses_at_utf8_boundaries(tmp_path):
    journal, output, context = await setup(tmp_path)
    raw = "€" * 30000
    prepared = await output.prepare(
        context, ToolResult("call-1", "Bash", raw, session_id="s1"), "tail"
    )
    await save(journal, output, prepared)
    first = await output.read(prepared.output_id, 1, 1, 4096)
    assert first.has_more and first.next_offset == 1 and first.next_byte_offset > 0
    assert "�" not in first.text
    second = await output.read(
        prepared.output_id, first.next_offset, 1, 4096, first.next_byte_offset
    )
    assert second.next_byte_offset > first.next_byte_offset
    assert len(second.text.encode()) <= 4096
    await journal.aclose()


async def test_missing_corrupt_and_unreferenced_outputs_are_errors_not_reexecution(tmp_path):
    journal, output, context = await setup(tmp_path)
    prepared = await output.prepare(
        context, ToolResult("call-1", "Bash", "x" * 60000, session_id="s1"), "tail"
    )
    with pytest.raises(ToolError) as error:
        await output.read(prepared.output_id, 1, 1, MAX_BYTES)
    assert error.value.code == "output_not_accessible"
    await save(journal, output, prepared)
    full = tmp_path / "sessions/s1/tool-results" / (prepared.output_id + ".txt")
    full.write_text("corrupt")
    with pytest.raises(ToolError) as error:
        await output.read(prepared.output_id, 1, 1, MAX_BYTES)
    assert error.value.code == "output_corrupt"
    full.unlink()
    with pytest.raises(ToolError) as error:
        await output.read(prepared.output_id, 1, 1, MAX_BYTES)
    assert error.value.code == "output_missing"
    with pytest.raises(ToolError):
        await output.read("../../escape", 1, 1, MAX_BYTES)
    await journal.aclose()


async def test_branch_edit_keeps_output_and_fork_can_read_only_copied_references(tmp_path):
    journal, output, context = await setup(tmp_path)
    result = await output.prepare(
        context, ToolResult("call-1", "Read", "original", session_id="s1"), "head"
    )
    entry_id = await save(journal, output, result)
    await journal.append_entry(
        EntryDraft("context_edit", data={"target_id": entry_id, "replacement": "omitted"})
    )
    assert (await output.read(result.output_id, 1, 1, MAX_BYTES)).text == "original"
    await journal.aclose()
    repository = SqliteSessionRepository(tmp_path / "sessions", "/project", TestTiming())
    fork = await repository.fork("s1", entry_id, "forked")
    inherited = FileToolOutputStore(
        tmp_path / "sessions",
        "forked",
        fork,
        header_reader=FileSessionHeaderReader(tmp_path / "sessions"),
    )
    assert (await inherited.read(result.output_id, 1, 1, MAX_BYTES)).text == "original"
    await fork.branch(None)
    with pytest.raises(ToolError):
        await inherited.read(result.output_id, 1, 1, MAX_BYTES)
    await fork.aclose()


async def test_streaming_bytes_split_unicode_and_line_only_limits_are_preserved(tmp_path):
    journal, output, context = await setup(tmp_path)
    writer = await output.begin(context, "tail")
    raw = ("€\n" * 4000).encode()
    for index in range(0, len(raw), 7):
        await writer.append(raw[index : index + 7])
    result = await writer.finish(ToolResult("call-1", "Bash", "", session_id="s1"))
    await save(journal, output, result)
    full = tmp_path / "sessions/s1/tool-results" / (result.output_id + ".txt")
    assert full.read_bytes() == raw
    assert "�" not in result.text
    assert output.metadata(result.output_id).line_count == 4000
    await journal.aclose()


async def test_large_structured_payload_is_preserved_without_bloating_history(tmp_path):
    import json

    journal, output, context = await setup(tmp_path)
    structured = {"value": "x" * 200000}
    result = await output.prepare(
        context,
        ToolResult(
            "call-1", "MCP", "small preview", structured_content=structured, session_id="s1"
        ),
        "head",
    )
    await save(journal, output, result)
    assert result.structured_content is None
    assert len(json.dumps([plain_data(e) for e in (await journal.snapshot()).entries])) < 10000
    artifact = tmp_path / "sessions/s1/tool-results" / (result.output_id + ".artifact.json")
    assert json.loads(artifact.read_text())["structured_content"] == structured
    await journal.aclose()


async def test_large_output_keeps_control_details(tmp_path):
    journal, output, context = await setup(tmp_path)
    text = "x" * 60000
    result = await output.prepare(
        context,
        ToolResult(
            "call-1", "ToolSearch", text, details={"loaded_tools": {"echo": "h"}}, session_id="s1"
        ),
        "head",
    )
    assert result.details["loaded_tools"] == {"echo": "h"}
    assert result.details["output"]["bytes"] == 60000
    await journal.aclose()


async def test_readback_rejects_forged_call_identity_even_if_entry_binding_matches(tmp_path):
    from dataclasses import replace

    journal, output, context = await setup(tmp_path)
    prepared = await output.prepare(
        context, ToolResult("call-1", "Bash", "x" * 60000, session_id="s1"), "tail"
    )
    await save(journal, output, prepared)
    output.publish_metadata(replace(output.metadata(prepared.output_id), call_id="other-call"))
    with pytest.raises(ToolError) as error:
        await output.read(prepared.output_id, 1, 1, MAX_BYTES)
    assert error.value.code == "output_origin_conflict"
    await journal.aclose()


async def test_published_stream_reference_cannot_change_its_error_state(tmp_path):
    from dataclasses import replace

    journal, output, context = await setup(tmp_path)
    writer = await output.begin(context, "tail")
    await writer.append(b"known output")
    prepared = await writer.finish(ToolResult("call-1", "Bash", "", exit_code=1, is_error=True))
    with pytest.raises(ToolError) as error:
        await output.prepare(context, replace(prepared, exit_code=0, is_error=False), "tail")
    assert error.value.code == "output_identity_conflict"
    await journal.aclose()


async def test_repeated_compaction_keeps_full_output_readable_from_original_branch_history(
    tmp_path,
):
    journal, output, context = await setup(tmp_path)
    prepared = await output.prepare(
        context, ToolResult("call-1", "Bash", "x" * 60000, session_id="s1"), "tail"
    )
    await save(journal, output, prepared)
    for summary in ("first summary", "second summary"):
        await journal.append_entry(
            EntryDraft("compaction", data={"summary": summary, "first_kept_entry_id": None})
        )
    assert (await output.read(prepared.output_id, 1, 1, 4096)).text == "x" * 4096
    assert (tmp_path / "sessions/s1/tool-results" / (prepared.output_id + ".txt")).exists()
    await journal.aclose()
