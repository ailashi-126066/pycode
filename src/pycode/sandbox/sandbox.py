# Pi 没有，pycode 新增；参照 MewCode local sandbox/seatbelt.py
# SHA-256: b16d640a21d057388a858dd047302916167e49e08bafbc23c4b2f058750267af
# Adaptation: explicit argv, owned async probe, protected state and no launch fallback.
"""SandboxFeature: the bash tool, command gates and sandboxed commands for hooks.

Seatbelt is enabled only after an actual executable probe succeeds.
"""

import asyncio
import os
import site
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from pycode.kernel import (
    Feature,
    RunScope,
    SessionScope,
    ToolContext,
    ToolError,
    ToolResult,
    json_dumps,
)
from pycode.sandbox.policy import auto_approvable, denied_command
from pycode.sandbox.process import (
    run_process,
    stop_process,
    subprocess_environment,
    validate_passthrough,
)
from pycode.sandbox.tools import Bash, find_bash
from pycode.sandbox.types import SandboxSettings, SandboxStatus
from pycode.tools import TempOutputStore

UNAVAILABLE = "OS 沙箱不可用；使用权限审批"

# System locations bash, git, rg and Python need to start and run. Seatbelt matches
# resolved paths, so /etc, /tmp and /var appear under /private.
SYSTEM_READ_SUBPATHS = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/Applications/Xcode.app",
    "/opt/homebrew",
    "/private/etc",
    "/private/var/db/timezone",
    "/private/var/db/dyld",
    "/private/var/select",
    "/private/tmp",
    "/dev/fd",
)
SYSTEM_READ_LITERALS = (
    "/",
    "/dev",
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
    "/dev/dtracehelper",
    "/private/var/db/xcode_select_link",
)
# Credential and private state directories under HOME; denied after every allow.
PRIVATE_HOME_DIRECTORIES = (".ssh", ".aws", ".gnupg", ".config", ".pycode")
# Git aborts when an existing global config file is unreadable, so only these
# configuration files are readable again. ~/.config/git/credentials stays denied.
GIT_HOME_FILES = (
    ".gitconfig",
    ".config/git/config",
    ".config/git/ignore",
    ".config/git/attributes",
)
DOTENV = '(regex #"/\\.env(\\.[^/]*)?$")'
REGEX_SPECIAL = frozenset("\\.^$|?*+()[]{}")


def python_read_paths():
    """Current interpreter, its standard library, site-packages and this package."""
    paths = {
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
        str(Path(sys.executable).resolve().parent),
        # Import root of pycode itself, used by the Python Skill worker.
        str(Path(__file__).resolve().parents[3]),
        *site.getsitepackages(),
    }
    if site.ENABLE_USER_SITE:
        paths.add(site.getusersitepackages())
    # Absolute import roots added by .pth files or PYTHONPATH (empty/relative entries skipped).
    paths.update(entry for entry in sys.path if entry and Path(entry).is_absolute())
    return paths


def build_profile(workspace, network_enabled, home=None, *, skill_read_roots=(), write_roots=None):
    root = Path(workspace).resolve()
    if write_roots is not None and any(
        not Path(path).resolve().is_relative_to(root) for path in write_roots
    ):
        raise ToolError("outside_task_scope", "Task write roots must stay inside the workspace")
    home = Path(home if home is not None else Path.home()).resolve()

    def quote(path):
        return json_dumps(str(path), ensure_ascii=False)

    readable = {Path(path) for path in SYSTEM_READ_SUBPATHS}
    readable.add(Path(tempfile.gettempdir()).resolve())
    # An import root that is HOME or one of its ancestors would expose all of HOME.
    readable.update(
        path
        for path in (Path(entry).resolve() for entry in python_read_paths())
        if not home.is_relative_to(path)
    )
    subpaths = " ".join(f"(subpath {quote(path)})" for path in sorted(readable))
    literals = " ".join(f"(literal {quote(path)})" for path in SYSTEM_READ_LITERALS)
    private = [home / name for name in PRIVATE_HOME_DIRECTORIES]
    # A workspace may itself live under private state (subagent worktrees under
    # ~/.pycode); private directories inside the workspace are denied again.
    nested = [path for path in private if path.is_relative_to(root)]

    def deny_private(paths):
        return "(deny file-read* " + " ".join(f"(subpath {quote(p)})" for p in paths) + ")"

    # .env.example is re-allowed only inside the workspace. A path that cannot be
    # written as a Seatbelt regex literal keeps .env.example denied as well.
    example = None
    if '"' not in str(root):
        prefix = "".join("\\" + c if c in REGEX_SPECIAL else c for c in str(root).rstrip("/"))
        example = f'(regex #"^{prefix}/(.*/)?\\.env\\.example$")'

    rules = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        # Path lookup needs metadata of parent directories; file contents and
        # directory listings stay limited to the allow lists below.
        "(allow file-read-metadata)",
        f"(allow file-read* {literals} {subpaths})",
        # Later Seatbelt rules take precedence over earlier ones.
        deny_private(private),
        f"(allow file-read* (subpath {quote(root)}))",
        *[
            f"(allow file-read* (subpath {quote(Path(path).resolve())}))"
            for path in skill_read_roots
        ],
        # Dotenv files stay unreadable inside and outside the workspace.
        f"(deny file-read-data {DOTENV})",
        *([f"(allow file-read-data {example})"] if example else []),
        # Placed after the .env.example allow so it cannot reopen private state.
        *([deny_private(nested)] if nested else []),
        "(allow file-read* "
        + " ".join(f"(literal {quote(home / name)})" for name in GIT_HOME_FILES)
        + ")",
        # Standard logging and redirected commands use the null device. This
        # grants no write access to an ordinary file outside the workspace.
        '(allow file-write-data (literal "/dev/null"))',
        *(
            [f"(allow file-write* (subpath {quote(root)}))"]
            if write_roots is None
            else [
                f"(allow file-write* (subpath {quote(Path(path).resolve())}) "
                f"(literal {quote(Path(path).resolve())}))"
                for path in write_roots
            ]
        ),
        f"(deny file-write-unlink (literal {quote(root)}))",
        # Renaming or linking a dotenv file would escape the name-based read deny.
        f"(deny file-write* {DOTENV})",
        *([f"(allow file-write* {example})"] if example and write_roots is None else []),
    ]
    for name in (".git", ".pycode", ".env"):
        path = quote(root / name)
        rules.append(f"(deny file-write* (subpath {path}) (literal {path}))")
    rules.append("(allow network*)" if network_enabled else "(deny network*)")
    return "\n".join(rules)


class Sandbox:
    """Seatbelt on macOS; elsewhere unavailable, and commands run behind ordinary approval.

    required: worker processes never fall back to executing outside an OS sandbox.
    """

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        required: bool = False,
        executable: str = "/usr/bin/sandbox-exec",
        skill_read_roots: tuple[Path, ...] = (),
    ):
        self.settings = settings
        self.required = required
        self._executable = executable
        self._skill_read_roots = skill_read_roots

    def for_skill(self, directory: Path) -> "Sandbox":
        if self.required and sys.platform != "darwin":
            raise ToolError(
                "worker_sandbox_unavailable", "Skill execution requires a strict sandbox backend"
            )
        return Sandbox(
            self.settings,
            required=self.required,
            executable=self._executable,
            skill_read_roots=(*self._skill_read_roots, Path(directory).resolve()),
        )

    async def usable(self) -> bool:
        if sys.platform != "darwin":
            return False
        process = None
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                self._executable,
                "-p",
                "(version 1)(allow default)",
                "/usr/bin/true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(spawning)
            async with asyncio.timeout(2):
                return await process.wait() == 0
        except asyncio.CancelledError:
            try:
                process = await spawning
            except OSError:
                pass
            if process is not None:
                await stop_process(process)
            raise
        except (OSError, TimeoutError):
            if process is not None:
                await stop_process(process)
            return False

    async def status(self) -> SandboxStatus:
        mode = self.settings.mode
        if not await self.usable():
            return SandboxStatus(False, False, False, UNAVAILABLE)
        enabled = mode != "off"
        return SandboxStatus(
            True, enabled, mode == "on-auto", "Seatbelt " + (mode if enabled else "off")
        )

    async def launch(self, argv: tuple[str, ...], workspace: str) -> tuple[str, ...]:
        if self.required:
            if not (await self.status()).enabled:
                raise ToolError(
                    "worker_sandbox_unavailable", "Worker commands require an available OS sandbox"
                )
        elif self.settings.mode == "off" or sys.platform != "darwin":
            return tuple(argv)
        elif not await self.usable():
            raise ToolError(
                "sandbox_unavailable", "Seatbelt became unavailable; command was not run"
            )
        profile = await asyncio.to_thread(
            build_profile,
            workspace,
            self.settings.network_enabled,
            skill_read_roots=self._skill_read_roots,
            write_roots=self.settings.write_roots,
        )
        return (self._executable, "-p", profile, *argv)


class CommandGate:
    """Source deny rules first; on-auto approves only allow-listed read-only commands."""

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox

    async def authorize(self, run, spec, call):
        if spec.name != "bash" or spec.source != "builtin":
            return None
        command = call.arguments.get("command", "")
        reason = denied_command(command)
        if reason is not None:
            raise ToolError("command_denied", reason)
        if (
            self.sandbox.settings.mode == "on-auto"
            and auto_approvable(command)
            and (await self.sandbox.status()).auto_approve
        ):
            return "allow"
        return None


class SandboxFeature(Feature):
    name = "sandbox"

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        required: bool = False,
        passthrough: tuple[str, ...] = (),
        path_prefix: tuple[str, ...] = (),
        read_roots: Callable[[RunScope], tuple[Path, ...]] = lambda run: (),
    ):
        self.sandbox = Sandbox(settings, required=required)
        self.passthrough = validate_passthrough(passthrough)
        self.path_prefix = path_prefix
        # Extra read-only roots for bash (Python Skill script directories).
        self.read_roots = read_roots
        self.session_id = ""

    async def open(self, session: SessionScope) -> None:
        self.session_id = session.session_id

    async def status(self) -> SandboxStatus:
        return await self.sandbox.status()

    def tools(self, run: RunScope) -> tuple[Bash, ...]:
        environment = None
        if self.path_prefix:
            environment = dict(os.environ)
            environment["PATH"] = os.pathsep.join((*self.path_prefix, environment.get("PATH", "")))
        sandbox = self.sandbox
        for root in self.read_roots(run):
            sandbox = sandbox.for_skill(root)
        bash = Bash(
            find_bash(),
            TempOutputStore(run.session.session_id),
            sandbox,
            environment=environment,
            passthrough=self.passthrough,
        )
        return (bash,)

    def gates(self, run: RunScope) -> tuple[CommandGate, ...]:
        return (CommandGate(self.sandbox),)

    async def run_command(
        self, argv: tuple[str, ...], cwd: str, env: Mapping[str, str] | None, seconds: float
    ) -> ToolResult:
        """kernel CommandRunner for hooks; env adds variables to the allowlisted environment.

        Hook commands keep the session's sandbox mode but not worker-required isolation.
        """
        sandbox = Sandbox(self.sandbox.settings)
        call_id = "hook-" + uuid.uuid4().hex
        context = ToolContext(self.session_id, "", call_id, cwd, call_id)
        writer = await TempOutputStore(self.session_id).begin(context, "head")
        environment = subprocess_environment(os.environ, passthrough=self.passthrough)
        return await run_process(
            await sandbox.launch(argv, cwd),
            cwd,
            writer,
            ToolResult(call_id, "hook", ""),
            limit_seconds=seconds,
            prefix="hook_command",
            environment={**environment, **(env or {})},
        )
