"""Large tool outputs: truncated previews with the full text in a readable file."""

from pathlib import Path
from typing import Protocol

from pycode.kernel import PreviewMode, ToolContext, ToolResult


class OutputWriter(Protocol):
    async def append(self, data: bytes) -> None: ...
    async def finish(self, result: ToolResult) -> ToolResult: ...
    async def abort(self) -> None: ...


class ToolOutputStore(Protocol):
    # Mounted read-only at its own absolute path, so read accepts full_output_path as is.
    directory: Path

    async def prepare(
        self, context: ToolContext, result: ToolResult, mode: PreviewMode
    ) -> ToolResult: ...
    async def begin(self, context: ToolContext, mode: PreviewMode) -> OutputWriter: ...
