# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/tools/write.ts
"""Create or overwrite a file through the backend mounts."""

from deepagents.backends import CompositeBackend

from ...kernel import ToolContext, ToolResult, ToolSpec
from .paths import (
    backend_error,
    real_path,
    resolve_to_cwd,
    with_file_mutation_queue,
)


class Write:
    spec = ToolSpec(
        name="write",
        description=(
            "Write content to a file. Creates the file if it doesn't exist, overwrites if it "
            "does. Automatically creates parent directories."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (relative or absolute)",
                },
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        category="write",
        exposure="direct",
        prompt_snippet="Create or overwrite files",
        prompt_guidelines=("Use write only for new files or complete rewrites.",),
    )

    def __init__(self, backend: CompositeBackend) -> None:
        self.backend = backend

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        path = arguments["path"]
        target = resolve_to_cwd(self.backend, path)

        async def write():
            # PycodeBackend still requires a fresh read before overwriting an existing file.
            result = await self.backend.awrite(target, arguments["content"])
            if result.error:
                raise backend_error(result.error)
            return ToolResult(context.call_id, self.spec.name, f"Successfully wrote to {path}")

        return await with_file_mutation_queue(real_path(self.backend, target), write)
