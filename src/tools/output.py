# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/output-accumulator.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/bash.ts
"""Large tool outputs: a truncated preview in the result, the full text in a temp file."""

import os
import re
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

from ..kernel import (
    MAX_BYTES,
    MAX_LINES,
    PreviewMode,
    StorageError,
    ToolContext,
    ToolResult,
    format_size,
    line_count,
    owned_io,
    plain_data,
    truncate,
)


def output_directory(session_id: str) -> Path:
    """$TMPDIR/pycode/<session_id>; the Seatbelt profile already allows reading $TMPDIR."""
    return Path(tempfile.gettempdir()) / "pycode" / session_id


def make_private_directory(directory: Path) -> None:
    """Create $TMPDIR/pycode and the session directory as 0700; refuse one another user made.

    On Linux $TMPDIR is often the shared /tmp, and full outputs skip the credential filter.
    """
    for path in (directory.parent, directory):
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and info.st_uid != os.getuid()):
            raise StorageError("output_directory_not_owned", f"{path} is not this user's directory")


def full_output_path(context: ToolContext) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", context.call_id)
    return output_directory(context.session_id) / (name + ".log")


# Pi: formatOutput. Head previews start at line 1, tail previews end at the last line.
def truncated_result(result, text, mode, path, *, total_lines=None, total_bytes=None):
    truncation = truncate(text, mode)
    total_lines = truncation.total_lines if total_lines is None else total_lines
    total_bytes = truncation.total_bytes if total_bytes is None else total_bytes
    truncation = replace(truncation, total_lines=total_lines, total_bytes=total_bytes)
    if truncation.first_line_exceeds_limit:
        notice = (
            f"[Line 1 exceeds {format_size(MAX_BYTES)} limit. Use read with offset or bash "
            f"head -c on the full output: {path}]"
        )
    elif truncation.partial_line:
        notice = (
            f"[Showing last {format_size(truncation.output_bytes)} of line {total_lines}. "
            f"Full output: {path}]"
        )
    else:
        start = 1 if mode == "head" else total_lines - truncation.output_lines + 1
        end = start + truncation.output_lines - 1
        limited = "" if truncation.truncated_by == "lines" else f" ({format_size(MAX_BYTES)} limit)"
        notice = f"[Showing lines {start}-{end} of {total_lines}{limited}. Full output: {path}]"
    summary = {key: value for key, value in plain_data(truncation).items() if key != "text"}
    return replace(
        result,
        text=(truncation.text + "\n\n" if truncation.text else "") + notice,
        # Structured content describes the complete output, which is now in the file.
        structured_content=None,
        details={**result.details, "truncation": summary, "full_output_path": str(path)},
    )


def is_small(text: str) -> bool:
    return len(text.encode()) <= MAX_BYTES and line_count(text) <= MAX_LINES


class TempOutputStore:
    def __init__(self, session_id: str) -> None:
        self.directory = output_directory(session_id)

    async def prepare(
        self, context: ToolContext, result: ToolResult, mode: PreviewMode
    ) -> ToolResult:
        # Tools that truncate themselves (Pi's read, grep, bash) already report it.
        if "truncation" in result.details or is_small(result.text):
            return result
        path = full_output_path(context)

        def write():
            make_private_directory(path.parent)
            path.write_text(result.text, encoding="utf-8")

        await owned_io(write)
        return truncated_result(result, result.text, mode, path)

    async def begin(self, context: ToolContext, mode: PreviewMode) -> "StreamingOutputWriter":
        writer = StreamingOutputWriter(full_output_path(context), mode)
        await owned_io(writer.open)
        return writer


# Pi: OutputAccumulator. The whole stream goes to the file; memory keeps a head and a tail.
class StreamingOutputWriter:
    def __init__(self, path: Path, mode: PreviewMode) -> None:
        self.path = path
        self.mode = mode
        self.stream: BinaryIO | None = None
        self.size = 0
        self.newlines = 0
        self.last_byte: int | None = None
        self.head = bytearray()
        self.tail = b""

    def open(self) -> None:
        make_private_directory(self.path.parent)
        self.stream = self.path.open("wb")

    async def append(self, data: bytes) -> None:
        if self.stream is None:
            raise StorageError("output_not_open", "Open the output file before appending")
        await owned_io(self.stream.write, data)
        self.size += len(data)
        self.newlines += data.count(b"\n")
        if data:
            self.last_byte = data[-1]
        keep = MAX_BYTES * 2
        self.head.extend(data[: max(0, keep - len(self.head))])
        self.tail = (self.tail + data)[-keep:]

    async def finish(self, result: ToolResult) -> ToolResult:
        await self.abort()
        total_lines = self.newlines + (1 if self.size and self.last_byte != 10 else 0)
        if self.size <= MAX_BYTES and total_lines <= MAX_LINES:
            await owned_io(self.path.unlink)
            return replace(result, text=bytes(self.head).decode("utf-8", "replace"))
        raw = bytes(self.head) if self.mode == "head" else self.tail
        if self.mode == "tail":
            # The rolling tail may start inside a UTF-8 sequence.
            start = 0
            while start < len(raw) and raw[start] & 0xC0 == 0x80:
                start += 1
            raw = raw[start:]
        text = raw.decode("utf-8", "replace")
        return truncated_result(
            result, text, self.mode, self.path, total_lines=total_lines, total_bytes=self.size
        )

    async def abort(self) -> None:
        """Close the file; an interrupted command keeps its partial output on disk."""
        if self.stream is not None:
            await owned_io(self.stream.close)
            self.stream = None
