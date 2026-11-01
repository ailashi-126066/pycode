"""Output bytes can survive a crash; JSONL alone confirms a completed tool fact."""

from dataclasses import replace

import pytest

from pycode.kernel.domain.errors import ToolError
from pycode.kernel.domain.messages import Message, ToolResult
from pycode.kernel.domain.values import plain_data
from pycode.session.adapters.storage.header_reader import FileSessionHeaderReader
from pycode.tools.adapters.output_store import FileToolOutputStore
from tests.tools.adapters.test_output_store import save, setup


@pytest.mark.parametrize("streaming", [False, True])
async def test_before_jsonl_commit_only_bytes_survive_never_a_confirmed_tool_result(
    tmp_path, streaming
):
    journal, output, context = await setup(tmp_path)
    original = ToolResult(
        "call-1",
        "Bash",
        "x" * 60000,
        is_error=True,
        error_code="known_error",
        exit_code=3,
        details={"cause": "known"},
    )
    if streaming:
        writer = await output.begin(context, "tail")
        await writer.append(original.text.encode())
        prepared = await writer.finish(original)
    else:
        prepared = await output.prepare(context, original, "tail")
    restored = FileToolOutputStore(
        tmp_path / "sessions",
        "s1",
        journal,
        header_reader=FileSessionHeaderReader(tmp_path / "sessions"),
    )
    reference = await restored.interrupted_reference(context)
    assert reference == prepared.output_id
    metadata = plain_data(output.metadata(reference))
    assert metadata["source_entry_id"] is None and metadata["preview"] == ""
    assert "settled_result" not in metadata and "settlement_sha256" not in metadata
    assert not any(
        entry.message and entry.message.tool_result for entry in (await journal.snapshot()).entries
    )
    interrupted = ToolResult(
        "call-1",
        "Bash",
        "Execution effects are unknown",
        is_error=True,
        error_code="tool_interrupted",
        output_id=reference,
        session_id="s1",
        operation_id=context.operation_id,
        details={"output_recovered": True},
    )
    entry_id = await journal.append_message(
        Message("interrupted", "tool_result", tool_result=interrupted)
    )
    await restored.bind(reference, entry_id)
    assert (await restored.read(reference, 1, 1, 4096)).text == "x" * 4096
    assert interrupted.error_code == "tool_interrupted"
    await journal.aclose()


async def test_interrupted_output_is_only_a_reference_and_cannot_confirm_completion(tmp_path):
    journal, output, context = await setup(tmp_path)
    writer = await output.begin(context, "tail")
    await writer.append(b"partial")
    await writer.abort()
    reference = await output.interrupted_reference(context)
    assert reference is not None and output.metadata(reference).status == "interrupted"
    assert not any(
        entry.message and entry.message.tool_result for entry in (await journal.snapshot()).entries
    )
    await journal.aclose()


async def test_interrupted_reference_checks_body_hash_and_session_scope(tmp_path):
    journal, output, context = await setup(tmp_path)
    result = await output.prepare(context, ToolResult("call-1", "Bash", "x" * 60000), "tail")
    full = tmp_path / "sessions/s1/tool-results" / (result.output_id + ".txt")
    full.write_bytes(b"changed")
    with pytest.raises(ToolError) as error:
        await output.interrupted_reference(context)
    assert error.value.code == "output_corrupt"
    with pytest.raises(ToolError):
        await output.interrupted_reference(replace(context, session_id="other"))
    await journal.aclose()


async def test_structured_artifact_checksum_verified_and_no_duplicate_result_metadata(tmp_path):
    journal, output, context = await setup(tmp_path)
    result = await output.prepare(
        context,
        ToolResult(
            "call-1",
            "Bash",
            "small preview",
            structured_content={"value": "x" * 100000},
        ),
        "tail",
    )
    await save(journal, output, result)
    assert (await output.read(result.output_id, 1, 1, 4096)).text == "small preview"
    artifact = tmp_path / "sessions/s1/tool-results" / (result.output_id + ".artifact.json")
    original = artifact.read_bytes()
    artifact.write_bytes(b"{}")
    with pytest.raises(ToolError) as error:
        await output.read(result.output_id, 1, 1, 4096)
    assert error.value.code == "output_corrupt"
    artifact.write_bytes(original)
    output.publish_metadata(replace(output.metadata(result.output_id), sha256="0" * 64))
    with pytest.raises(ToolError) as error:
        await output.read(result.output_id, 1, 1, 4096)
    assert error.value.code == "output_corrupt"
    await journal.aclose()
