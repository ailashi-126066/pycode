"""What a bash command may do: refused patterns, commands that skip approval, and Seatbelt.

A bash call passes three checks, in this order:
1. A dangerous command (`denied_command`) is refused by the bash tool; it never asks the user.
2. A command from ~/.pycode/settings.json "allow_commands" (`allowed_command`) runs without
   approval; every other command waits for the user (see `needs_approval`, given to the
   framework's HumanInTheLoopMiddleware).
3. On macOS the command runs under Seatbelt (`sandboxed`): it can read almost anything except
   credentials, and write only inside the workspace and the temporary directory.

The patterns in 1 are an extra guard only: a shell can spell a command in ways no regular
expression recognises. Seatbelt and approval are what actually limit a command.
"""

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path

# From the baseline sandbox/policy.py.
_SHELL = r"(?:sudo\s+)?(?:ba|da|k|z)?sh\b"
_TARGET_END = r"(?:\s|$|[;&|])"
DENIED = (
    (
        r"\brm\s+(?:-\S*\s+)*(?:/\*?|~/?\*?|\$HOME/?\*?|\$\{HOME\}/?\*?)" + _TARGET_END,
        "deleting the root or home directory",
    ),
    (r"mkfs\.", "formatting a disk"),
    (r"dd\s+if=.*of=/dev/", "writing to a disk device"),
    (r">\s*/dev/sd", "overwriting a disk device"),
    (r"chmod\s+-R\s+777\s+/", "changing permissions of the whole system"),
    (r":\(\)\{\s*:\|:&\s*\};:", "a fork bomb"),
    (r"(?:curl|wget)\b[^\n]*\|\s*" + _SHELL, "running a downloaded script"),
    (
        r"(?:\b(?:ba|da|k|z)?sh|\bsource|(?:^|[;&|]\s*)\.)\s+<\(\s*(?:curl|wget)\b",
        "running a downloaded script",
    ),
    (r"\b(?:ba|da|k|z)?sh\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b", "running a downloaded script"),
)

DEFAULT_ALLOWED = ["git status", "git diff", "git log"]
# Shell syntax that chains, redirects or substitutes: `git status; rm -rf src` must still ask.
SHELL_SYNTAX = set(";&|<>`$\\\n\r")

# Under HOME, never readable or writable from a command: keys, and pycode's own sessions
# and configuration (mcp.json may hold tokens).
PRIVATE_HOME = (".ssh", ".aws", ".gnupg", ".pycode")


def denied_command(command: str) -> str | None:
    """Why this command is refused, or None."""
    return next((reason for pattern, reason in DENIED if re.search(pattern, command)), None)


def load_allowed(settings_file: Path) -> list[str]:
    """Commands that skip approval; the defaults when the file does not set them."""
    if not settings_file.exists():
        return DEFAULT_ALLOWED
    return json.loads(settings_file.read_text()).get("allow_commands", DEFAULT_ALLOWED)


def allowed_command(command: str, allowed: list[str]) -> bool:
    """The command is an allowed one, possibly with arguments, and nothing more."""
    command = command.strip()
    if SHELL_SYNTAX & set(command):
        return False
    return any(command == item or command.startswith(item + " ") for item in allowed)


def approval_check(allowed: list[str]):
    """The `when` function for bash in HumanInTheLoopMiddleware: True means ask the user."""

    def needs_approval(request) -> bool:
        command = request.tool_call["args"].get("command", "")
        if denied_command(command):
            return False  # the bash tool refuses it; asking first would be pointless
        return not allowed_command(command, allowed)

    return needs_approval


def profile(workspace: Path) -> str:
    """Seatbelt rules for one workspace. A later rule overrides an earlier one."""
    home = Path.home().resolve()

    def path(value) -> str:
        return json.dumps(str(Path(value).resolve()))  # Seatbelt matches resolved paths

    private = " ".join(f"(subpath {path(home / name)})" for name in PRIVATE_HOME)
    temporary = path(tempfile.gettempdir())
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            # Writing: only the workspace, temporary files and the terminal devices.
            "(deny file-write*)",
            f"(allow file-write* (subpath {path(workspace)}) (subpath {temporary})"
            ' (subpath "/private/tmp") (literal "/dev/null") (regex #"^/dev/tty"))',
            # The project's own pycode directory stays as protected as for the file tools.
            f"(deny file-write* (subpath {path(workspace / '.pycode')}))",
            f"(deny file-read* file-write* {private})",
            # .env files hold keys; .env.example is meant to be read.
            '(deny file-read* file-write* (regex #"/\\.env[^/]*$"))',
            '(allow file-read* (regex #"/\\.env\\.example$"))',
        ]
    )


def sandboxed(argv: list[str], workspace: Path) -> list[str]:
    return ["sandbox-exec", "-p", profile(workspace), *argv]


async def available(workspace: Path) -> bool:
    """Seatbelt actually runs here (macOS with sandbox-exec), tried with the real rules."""
    if shutil.which("sandbox-exec") is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            *sandboxed(["/usr/bin/true"], workspace),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(process.wait(), 10) == 0
    except (OSError, TimeoutError):
        return False
