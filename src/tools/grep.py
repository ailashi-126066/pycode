import shutil
import subprocess

from langchain.tools import ToolRuntime, tool

from ..context import Context
from .files import ToolError, head


@tool(parse_docstring=True)
def grep(
    pattern: str,
    runtime: ToolRuntime[Context],
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    literal: bool = False,
    limit: int = 100,
) -> str:
    """Search file contents with ripgrep. Respects .gitignore and skips hidden files.

    Returns matching lines as path:line:text. Long lines are cut to 500 characters.

    Args:
        pattern: Regular expression, or plain text when literal is true.
        path: Directory or file to search, relative to the workspace.
        glob: Only search files matching this glob, for example "*.py".
        ignore_case: Match case-insensitively.
        literal: Treat pattern as plain text instead of a regular expression.
        limit: Maximum number of matching lines to return.
    """
    files = runtime.context.files
    base = files.search_root(path)
    if shutil.which("rg") is None:
        raise ToolError("ripgrep (rg) is not installed")
    command = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns=500"]
    command += ["--max-columns-preview", *(["-i"] if ignore_case else [])]
    command += [*(["-F"] if literal else []), *(["-g", glob] if glob else [])]
    try:
        found = subprocess.run(
            [*command, "--", pattern, str(base)],
            cwd=files.workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise ToolError("Search took longer than 15 seconds; narrow the path or pattern") from error
    if found.returncode == 1:
        return "No matches found"
    if found.returncode != 0:
        raise ToolError(found.stderr.strip() or "ripgrep failed")
    lines = found.stdout.splitlines()
    text, cut = head("\n".join(lines[:limit]))
    if len(lines) > limit or cut:
        text += f"\n\n[{len(lines)} matching lines; output was cut. Refine the search.]"
    return text
