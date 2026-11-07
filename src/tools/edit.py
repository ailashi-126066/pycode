from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from ..context import Context
from .files import ToolError


class Replacement(BaseModel):
    old_text: str = Field(description="Exact text to replace; must be unique in the original file.")
    new_text: str = Field(description="Text to put in its place.")


@tool(parse_docstring=True)
def edit(path: str, edits: list[Replacement], runtime: ToolRuntime[Context]) -> str:
    """Change a file by exact text replacement. Read the file first.

    Every old_text is matched against the original file, must be unique and must not overlap
    another one. Merge nearby changes into one replacement instead of overlapping them.

    Args:
        path: File path, relative to the workspace.
        edits: One or more replacements applied together.
    """
    files = runtime.context.files
    with files.lock(path):
        raw = files.read_for_change(path)
        # Match on "\n" text; restore the file's byte-order mark and line endings afterwards.
        bom = "\ufeff" if raw.startswith("\ufeff") else ""
        crlf = "\r\n" in raw
        original = raw.removeprefix(bom).replace("\r\n", "\n")

        spans = []
        for number, item in enumerate(edits, 1):
            old = item.old_text.replace("\r\n", "\n")
            count = original.count(old) if old else 0
            if count != 1:
                found = "is empty" if not old else f"was found {count} times"
                raise ToolError(f"Edit {number}: old_text {found} in {path}; it must match once")
            start = original.index(old)
            spans.append((start, start + len(old), item.new_text.replace("\r\n", "\n"), number))
        spans.sort()
        for before, after in zip(spans, spans[1:], strict=False):
            if before[1] > after[0]:
                raise ToolError(f"Edits {before[3]} and {after[3]} overlap; merge them into one")

        changed = original
        for start, end, new, _ in reversed(spans):
            changed = changed[:start] + new + changed[end:]
        if changed == original:
            raise ToolError(f"The edits leave {path} unchanged")
        files.write(path, bom + (changed.replace("\n", "\r\n") if crlf else changed))
    return f"Applied {len(edits)} edit(s) to {path}."
