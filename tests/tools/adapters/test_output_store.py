"""Pi's large-output contract: a bounded preview, the full text in $TMPDIR/pycode/<session>/."""

import asyncio
from pathlib import Path

from pycode.kernel import MAX_BYTES, ToolContext, ToolResult
from pycode.tools.adapters.output_store import TempOutputStore


def context(call_id="call-1"):
    return ToolContext("s1", "run", "operation", "/project", call_id, "bash")


async def test_small_result_is_unchanged_and_writes_no_file():
    store = TempOutputStore("s1")
    result = ToolResult("call-1", "grep", "small", details={"loaded_tools": {"x": "h"}})
    assert await store.prepare(context(), result, "head") == result
    assert not store.directory.exists()


async def test_large_result_keeps_details_and_points_at_the_full_output():
    store = TempOutputStore("s1")
    text = "".join(f"line-{i:04d}\n" for i in range(1, 3001))
    result = ToolResult(
        "call-1",
        "ToolSearch",
        text,
        structured_content={"tools": []},
        details={"loaded_tools": {"echo": "hash"}, "executed": True},
    )
    prepared = await store.prepare(context(), result, "head")
    path = prepared.details["full_output_path"]
    assert path == str(store.directory / "call-1.log")
    # Only this user can read full outputs, also when $TMPDIR is a shared /tmp.
    for directory in (store.directory.parent, store.directory):
        assert directory.stat().st_mode & 0o777 == 0o700
    assert await asyncio.to_thread(Path(path).read_text) == text
    # Original details survive; Pi's truncation and fullOutputPath are added beside them.
    assert prepared.details["loaded_tools"] == {"echo": "hash"}
    assert prepared.details["executed"] is True
    assert prepared.details["truncation"]["total_lines"] == 3000
    assert prepared.structured_content is None
    assert prepared.text.startswith("line-0001\n")
    assert prepared.text.endswith(f"[Showing lines 1-2000 of 3000. Full output: {path}]")


async def test_streamed_tail_preview_reports_the_last_lines_and_keeps_every_byte():
    store = TempOutputStore("s1")
    writer = await store.begin(context(), "tail")
    for index in range(1, 3001):
        await writer.append(f"中文{index}\n".encode())
    finished = await writer.finish(ToolResult("call-1", "bash", "", exit_code=0))
    path = finished.details["full_output_path"]
    assert (await asyncio.to_thread(Path(path).read_text)).count("\n") == 3000
    assert "中文3000" in finished.text and "中文1\n" not in finished.text
    assert finished.text.endswith(f"[Showing lines 1001-3000 of 3000. Full output: {path}]")


async def test_small_stream_leaves_no_file_and_byte_limit_is_reported():
    store = TempOutputStore("s1")
    writer = await store.begin(context("small"), "tail")
    await writer.append(b"done\n")
    small = await writer.finish(ToolResult("small", "bash", ""))
    assert small.text == "done\n" and "full_output_path" not in small.details
    assert not (store.directory / "small.log").exists()
    wide = await store.prepare(
        context("wide"), ToolResult("wide", "mcp", ("y" * 1000 + "\n") * 100), "head"
    )
    assert wide.text.endswith(
        " (50.0KB limit). Full output: " + wide.details["full_output_path"] + "]"
    )
    assert len(wide.text.encode()) <= MAX_BYTES + 200
