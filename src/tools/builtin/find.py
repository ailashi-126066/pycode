# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/find.ts
"""Find files by glob pattern through the backend mounts (ripgrep --files, not fd)."""

from deepagents.backends import CompositeBackend

from ...kernel import (
    MAX_BYTES,
    ToolContext,
    ToolResult,
    ToolSpec,
    format_size,
    plain_data,
    truncate,
)
from .paths import backend_error, relative_to, resolve_to_cwd

DEFAULT_LIMIT = 1000


class Find:
    spec = ToolSpec(
        name="find",
        description=(
            "Search for files by glob pattern. Returns matching file paths relative to the "
            "search directory. Respects .gitignore. Output is truncated to "
            f"{DEFAULT_LIMIT} results or {MAX_BYTES // 1024}KB (whichever is hit first)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern to match files, e.g. '*.ts', '**/*.json', or "
                        "'src/**/*.spec.ts'"
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": f"Maximum number of results (default: {DEFAULT_LIMIT})",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        category="read",
        exposure="direct",
        prompt_snippet="Find files by glob pattern (respects .gitignore)",
    )

    def __init__(self, backend: CompositeBackend) -> None:
        self.backend = backend

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        base = resolve_to_cwd(self.backend, arguments.get("path", "."))
        limit = arguments.get("limit", DEFAULT_LIMIT)
        found = await self.backend.aglob(arguments["pattern"], base)
        if found.error:
            raise backend_error(found.error)
        paths = [relative_to(match["path"], base) for match in found.matches or []]
        if not paths:
            return ToolResult(context.call_id, self.spec.name, "No files found matching pattern")
        shown = paths[:limit]
        truncation = truncate("\n".join(shown), "head", max_lines=len(shown), max_bytes=MAX_BYTES)
        details = {}
        notices = []
        if len(paths) > limit:
            notices.append(
                f"{limit} results limit reached. Use limit={limit * 2} for more, or refine pattern"
            )
            details["result_limit_reached"] = limit
        if truncation.truncated:
            notices.append(f"{format_size(MAX_BYTES)} limit reached")
            details["truncation"] = {
                key: value for key, value in plain_data(truncation).items() if key != "text"
            }
        text = truncation.text
        if notices:
            text += "\n\n[" + ". ".join(notices) + "]"
        return ToolResult(context.call_id, self.spec.name, text, details=details)
