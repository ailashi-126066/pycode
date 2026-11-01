# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/ls.ts
"""List one directory through the backend mounts."""

import posixpath

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
from .paths import backend_error, resolve_to_cwd

DEFAULT_LIMIT = 500


class Ls:
    spec = ToolSpec(
        name="ls",
        description=(
            "List directory contents. Returns entries sorted alphabetically, with '/' suffix for "
            f"directories. Includes dotfiles. Output is truncated to {DEFAULT_LIMIT} entries or "
            f"{MAX_BYTES // 1024}KB (whichever is hit first)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list (default: current directory)",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        f"Maximum number of entries to return (default: {DEFAULT_LIMIT})"
                    ),
                },
            },
            "additionalProperties": False,
        },
        category="read",
        exposure="direct",
        prompt_snippet="List directory contents",
    )

    def __init__(self, backend: CompositeBackend) -> None:
        self.backend = backend

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        limit = arguments.get("limit", DEFAULT_LIMIT)
        listing = await self.backend.als(resolve_to_cwd(self.backend, arguments.get("path", ".")))
        if listing.error:
            raise backend_error(listing.error)
        names = sorted(
            (
                posixpath.basename(entry["path"].rstrip("/")) + ("/" if entry.get("is_dir") else "")
                for entry in listing.entries or []
            ),
            key=str.lower,
        )
        if not names:
            return ToolResult(context.call_id, self.spec.name, "(empty directory)")
        shown = names[:limit]
        truncation = truncate("\n".join(shown), "head", max_lines=len(shown), max_bytes=MAX_BYTES)
        details = {}
        notices = []
        if len(names) > limit:
            notices.append(f"{limit} entries limit reached. Use limit={limit * 2} for more")
            details["entry_limit_reached"] = limit
        if truncation.truncated:
            notices.append(f"{format_size(MAX_BYTES)} limit reached")
            details["truncation"] = {
                key: value for key, value in plain_data(truncation).items() if key != "text"
            }
        text = truncation.text
        if notices:
            text += "\n\n[" + ". ".join(notices) + "]"
        return ToolResult(context.call_id, self.spec.name, text, details=details)
