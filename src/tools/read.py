from langchain.tools import ToolRuntime, tool

from ..context import Context
from .files import MAX_BYTES, MAX_LINES, ToolError, head


@tool(parse_docstring=True)
def read(
    path: str, runtime: ToolRuntime[Context], offset: int = 1, limit: int | None = None
) -> str:
    """Read a text file. Output stops at 2000 lines or 50KB; continue with offset for more.

    Args:
        path: File path, relative to the workspace.
        offset: Line number to start from, counting from 1.
        limit: Maximum number of lines to return.
    """
    lines = runtime.context.files.read(path).split("\n")
    if offset < 1 or offset > len(lines):
        raise ToolError(f"Offset {offset} is outside the file ({len(lines)} lines)")
    end = len(lines) if limit is None else min(len(lines), offset - 1 + limit)
    shown, cut = head("\n".join(lines[offset - 1 : end]))
    if not shown and cut:
        return (
            f"[Line {offset} is longer than {MAX_BYTES // 1024}KB. "
            f"Use bash: sed -n '{offset}p' {path} | head -c {MAX_BYTES}]"
        )
    last = offset + shown.count("\n")
    if cut or end < len(lines):
        limited = f" ({MAX_LINES} lines / {MAX_BYTES // 1024}KB limit)" if cut else ""
        shown += (
            f"\n\n[Showing lines {offset}-{last} of {len(lines)}{limited}. "
            f"Use offset={last + 1} to continue.]"
        )
    return shown
