from langchain.tools import ToolRuntime, tool

from ..context import Context


@tool(parse_docstring=True)
def write(path: str, content: str, runtime: ToolRuntime[Context]) -> str:
    """Create a file or replace its whole content. Read an existing file before replacing it.

    Args:
        path: File path, relative to the workspace. Missing directories are created.
        content: The complete new file content.
    """
    runtime.context.files.write(path, content)
    return f"Wrote {len(content)} characters to {path}."
