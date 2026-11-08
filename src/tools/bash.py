import asyncio
import os
import re
import shutil
import signal
import subprocess

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage

from ..context import Context
from ..sandbox import denied_command, sandboxed
from .files import MAX_BYTES, ToolError, output_file, tail

# Variables a shell, git, Python or a virtualenv needs; everything else is withheld.
ALLOWED_VARIABLES = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM"}
ALLOWED_VARIABLES |= {"TMPDIR", "TZ", "VIRTUAL_ENV"}
CREDENTIAL = re.compile(
    r"(?:.*_)?API_?KEY|(?:.*_)?TOKEN|.*SECRET.*|.*PASSWORD.*|.*PASSWD.*|(?:.*_)?CREDENTIALS?|AWS_.*",
    re.IGNORECASE,
)


def command_environment() -> dict[str, str]:
    """The parent environment without provider keys or other credentials."""
    return {
        name: value
        for name, value in os.environ.items()
        if (name in ALLOWED_VARIABLES or name.startswith("LC_")) and not CREDENTIAL.fullmatch(name)
    }


# `timeout` is an argument the model fills in, not an asyncio timeout parameter.
@tool(parse_docstring=True)
async def bash(command: str, runtime: ToolRuntime[Context], timeout: float = 120) -> ToolMessage:  # noqa: ASYNC109
    """Run a bash command in the workspace and return stdout and stderr together.

    Output keeps the last 2000 lines or 50KB; the complete output is then saved to a file.
    To wait for a service or build, loop inside one command with a timeout instead of
    running the same command again and again.

    Args:
        command: The shell command to run.
        timeout: Seconds before the command is stopped, at most 600.
    """
    if reason := denied_command(command):
        raise ToolError(f"Command refused: {reason}. Do not try to work around this.")
    shell = shutil.which("bash")
    if shell is None:
        raise ToolError("bash is not installed")
    workspace = runtime.context.files.workspace
    argv = [shell, "-c", command]
    if runtime.context.sandboxed:
        argv = sandboxed(argv, workspace)
    log = output_file()
    timed_out = False
    with log.open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=command_environment(),
            start_new_session=True,  # its own process group, so we can stop all its children
        )
        try:
            await asyncio.wait_for(process.wait(), min(timeout, 600))
        except TimeoutError:
            timed_out = True
        finally:
            # Also runs when the user cancels the run.
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    size = log.stat().st_size
    with log.open("rb") as saved:
        saved.seek(max(0, size - 2 * MAX_BYTES))
        text, cut = tail(saved.read().decode("utf-8", "replace"))
    if cut or size > 2 * MAX_BYTES:
        text = f"[Output cut; showing the end. Complete output: {log}]\n" + text
    else:
        log.unlink()
    if timed_out:
        text += f"\n[Command stopped after {min(timeout, 600):g} seconds]"
    elif process.returncode != 0:
        text += f"\n[Exit code {process.returncode}]"
    # A command that failed or timed out gives a failed result (task acceptance relies on it).
    failed = timed_out or process.returncode != 0
    return ToolMessage(
        text or "(no output)",
        tool_call_id=runtime.tool_call_id,
        name="bash",
        status="error" if failed else "success",
    )
