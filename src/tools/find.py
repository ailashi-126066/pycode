import shutil
import subprocess

from langchain.tools import ToolRuntime, tool

from ..context import Context
from .files import ToolError, head


@tool(parse_docstring=True)
def find(pattern: str, runtime: ToolRuntime[Context], path: str = ".", limit: int = 1000) -> str:
    """Find files by glob pattern. Respects .gitignore and skips hidden files.

    Args:
        pattern: Glob pattern, for example "*.py" or "src/**/*.ts".
        path: Directory to search, relative to the workspace.
        limit: Maximum number of paths to return.
    """
    files = runtime.context.files
    base = files.search_root(path)
    if shutil.which("rg") is None:
        raise ToolError("ripgrep (rg) is not installed")
    try:
        found = subprocess.run(
            ["rg", "--files", "--color=never", "-g", pattern, "--", str(base)],
            cwd=files.workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolError("Search took longer than 15 seconds; narrow the path or pattern") from error
    if found.returncode not in (0, 1):
        raise ToolError(found.stderr.strip() or "ripgrep failed")
    paths = sorted(found.stdout.splitlines())
    if not paths:
        return "No files found"
    text, cut = head("\n".join(paths[:limit]))
    if len(paths) > limit or cut:
        text += f"\n\n[{len(paths)} files; output was cut. Refine the pattern.]"
    return text
