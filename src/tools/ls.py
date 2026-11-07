from langchain.tools import ToolRuntime, tool

from ..context import Context
from .files import ToolError, head


@tool(parse_docstring=True)
def ls(runtime: ToolRuntime[Context], path: str = ".", limit: int = 500) -> str:
    """List a directory, sorted by name, with "/" after directories. Includes dotfiles.

    Args:
        path: Directory, relative to the workspace.
        limit: Maximum number of entries to return.
    """
    directory = runtime.context.files.resolve(path)
    if not directory.is_dir():
        raise ToolError(f"{path} is not a directory")
    names = sorted(
        (entry.name + "/" if entry.is_dir() else entry.name for entry in directory.iterdir()),
        key=str.lower,
    )
    if not names:
        return "(empty directory)"
    text, cut = head("\n".join(names[:limit]))
    if len(names) > limit or cut:
        text += f"\n\n[{len(names)} entries; output was cut. Use a larger limit or a subdirectory.]"
    return text
