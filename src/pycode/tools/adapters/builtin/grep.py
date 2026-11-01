# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/grep.ts
"""Search file contents with ripgrep through the backend mounts."""

import re

from deepagents.backends import CompositeBackend

from pycode.kernel import (
    MAX_BYTES,
    ToolContext,
    ToolResult,
    ToolSpec,
    format_size,
    plain_data,
    truncate,
)
from pycode.tools.adapters.builtin.path_utils import backend_error, relative_to, resolve_to_cwd

DEFAULT_LIMIT = 100
GREP_MAX_LINE_LENGTH = 500


# Pi: truncateLine.
def truncate_line(line: str) -> tuple[str, bool]:
    if len(line) <= GREP_MAX_LINE_LENGTH:
        return line, False
    return line[:GREP_MAX_LINE_LENGTH] + "... [truncated]", True


class Grep:
    spec = ToolSpec(
        name="grep",
        description=(
            "Search file contents for a pattern. Returns matching lines with file paths and line "
            "numbers. Respects .gitignore. Output is truncated to "
            f"{DEFAULT_LIMIT} matches or {MAX_BYTES // 1024}KB (whichever is hit first). Long "
            f"lines are truncated to {GREP_MAX_LINE_LENGTH} chars."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (regex or literal string)",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search (default: current directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "Filter files by glob pattern, e.g. '*.ts' or '**/*.spec.ts'",
                },
                "ignoreCase": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false)",
                },
                "literal": {
                    "type": "boolean",
                    "description": (
                        "Treat pattern as literal string instead of regex (default: false)"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        f"Maximum number of matches to return (default: {DEFAULT_LIMIT})"
                    ),
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        category="read",
        exposure="direct",
        prompt_snippet="Search file contents for patterns (respects .gitignore)",
    )

    def __init__(self, backend: CompositeBackend) -> None:
        self.backend = backend

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        base = resolve_to_cwd(self.backend, arguments.get("path", "."))
        limit = arguments.get("limit", DEFAULT_LIMIT)
        pattern = arguments["pattern"]
        if arguments.get("literal"):
            # ripgrep's regex syntax: escape only its metacharacters.
            pattern = re.sub(r"([\\.+*?()|\[\]{}^$])", r"\\\1", pattern)
        if arguments.get("ignoreCase"):
            pattern = "(?i)" + pattern
        found = await self.backend.agrep(pattern, base, arguments.get("glob"), max_count=limit + 1)
        if found.error:
            raise backend_error(found.error)
        matches = found.matches or []
        if not matches:
            return ToolResult(context.call_id, self.spec.name, "No matches found")
        lines = []
        lines_truncated = False
        for match in matches[:limit]:
            text, clipped = truncate_line(match["text"])
            lines_truncated = lines_truncated or clipped
            lines.append(f"{relative_to(match['path'], base)}:{match['line']}: {text}")
        truncation = truncate(
            "\n".join(lines), "head", max_lines=len(lines), max_bytes=MAX_BYTES - 512
        )
        details = {}
        notices = []
        if len(matches) > limit:
            notices.append(
                f"{limit} matches limit reached. Use limit={limit * 2} for more, or refine pattern"
            )
            details["match_limit_reached"] = limit
        if truncation.truncated:
            notices.append(f"{format_size(MAX_BYTES)} limit reached")
            details["truncation"] = {
                key: value for key, value in plain_data(truncation).items() if key != "text"
            }
        if lines_truncated:
            notices.append(
                f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. "
                "Use read tool to see full lines"
            )
            details["lines_truncated"] = True
        text = truncation.text
        if notices:
            text += "\n\n[" + ". ".join(notices) + "]"
        return ToolResult(context.call_id, self.spec.name, text, details=details)
