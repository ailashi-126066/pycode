# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/read.ts
"""Read a text file through the backend mounts, keeping the head like Pi."""

from deepagents.backends import CompositeBackend

from pycode.kernel import (
    MAX_BYTES,
    MAX_LINES,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    format_size,
    plain_data,
    truncate,
)
from pycode.tools.builtin.paths import backend_error, resolve_to_cwd


class Read:
    spec = ToolSpec(
        name="read",
        description=(
            "Read the contents of a file. For text files, output is truncated to "
            f"{MAX_LINES} lines or {MAX_BYTES // 1024}KB (whichever is hit first). Use "
            "offset/limit for large files. When you need the full file, continue with offset "
            "until complete."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative or absolute)",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Line number to start reading from (1-indexed)",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum number of lines to read",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        category="read",
        exposure="direct",
        prompt_snippet="Read file contents",
        prompt_guidelines=("Use read to examine files instead of cat or sed.",),
    )

    def __init__(self, backend: CompositeBackend) -> None:
        self.backend = backend

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        path = arguments["path"]
        start = arguments.get("offset", 1)
        limit = arguments.get("limit")
        page = await self.backend.aread(
            # One line past the limit lets truncation see that more lines follow.
            resolve_to_cwd(self.backend, path),
            start - 1,
            limit or MAX_LINES + 1,
        )
        if page.error:
            raise backend_error(page.error)
        if page.file_data is None:
            raise ToolError("not_a_file", f"{path} has no file content")
        content = page.file_data["content"]
        total = f" of {page.total_lines}" if page.total_lines is not None else ""
        truncation = truncate(content, "head")
        details = {}
        if truncation.truncated:
            details["truncation"] = {
                key: value for key, value in plain_data(truncation).items() if key != "text"
            }
        if truncation.first_line_exceeds_limit:
            size = format_size(len(content.split("\n", 1)[0].encode()))
            text = (
                f"[Line {start} is {size}, exceeds {format_size(MAX_BYTES)} limit. "
                f"Use bash: sed -n '{start}p' {path} | head -c {MAX_BYTES}]"
            )
        elif truncation.truncated:
            end = start + truncation.output_lines - 1
            limited = (
                f" ({format_size(MAX_BYTES)} limit)" if truncation.truncated_by == "bytes" else ""
            )
            text = (
                f"{truncation.text}\n\n[Showing lines {start}-{end}{total}{limited}. "
                f"Use offset={end + 1} to continue.]"
            )
        elif page.next_offset is not None:
            remaining = (
                f"{page.total_lines - page.next_offset} more lines"
                if page.total_lines is not None
                else "More lines"
            )
            offset = page.next_offset + 1
            text = f"{content}\n\n[{remaining} in file. Use offset={offset} to continue.]"
        else:
            text = content
        return ToolResult(context.call_id, self.spec.name, text, details=details)
