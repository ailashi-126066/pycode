# Source: MewCode local sandbox/seatbelt.py.
# SHA-256: b16d640a21d057388a858dd047302916167e49e08bafbc23c4b2f058750267af
# Adaptation: explicit argv, owned async probe, protected state and no launch fallback.
"""Seatbelt is enabled only after an actual executable probe succeeds."""

import asyncio
import site
import sys
import tempfile
from pathlib import Path

from pycode.kernel import ToolError, json_dumps
from pycode.sandbox.adapters.tools.process_runner import stop_process
from pycode.sandbox.domain.sandbox import SandboxStatus

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


class UnavailableSandbox:
    async def status(self, settings):
        return SandboxStatus(False, False, False, "OS 沙箱不可用；使用权限审批")

    async def launch(self, argv, workspace, settings):
        return tuple(argv)


class SeatbeltSandbox:
    def __init__(
        self, executable="/usr/bin/sandbox-exec", *, skill_read_roots=(), write_roots=None
    ):
        self._executable = executable
        self._skill_read_roots = tuple(skill_read_roots)

    def for_skill(self, directory):
        return SeatbeltSandbox(
            self._executable,
            skill_read_roots=(*self._skill_read_roots, Path(directory).resolve()),
        )

    async def usable(self):
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

    async def status(self, settings):
        available = await self.usable()
        enabled = available and settings.mode != "off"
        return SandboxStatus(
            available,
            enabled,
            enabled and settings.mode == "on-auto",
            "Seatbelt " + (settings.mode if enabled else "off")
            if available
            else "OS 沙箱不可用；使用权限审批",
        )

    async def launch(self, argv, workspace, settings):
        if settings.mode == "off":
            return tuple(argv)
        if not await self.usable():
            raise ToolError(
                "sandbox_unavailable", "Seatbelt became unavailable; command was not run"
            )
        profile = await asyncio.to_thread(
            build_profile,
            workspace,
            settings.network_enabled,
            skill_read_roots=self._skill_read_roots,
            write_roots=settings.write_roots,
        )
        return (self._executable, "-p", profile, *argv)


class RequiredSandbox:
    """Worker processes never fall back to executing outside an OS sandbox."""

    def __init__(self, backend):
        self.backend = backend

    def for_skill(self, directory):
        if not isinstance(self.backend, SeatbeltSandbox):
            raise ToolError(
                "worker_sandbox_unavailable", "Skill execution requires a strict sandbox backend"
            )
        return RequiredSandbox(self.backend.for_skill(directory))

    async def status(self, settings):
        return await self.backend.status(settings)

    async def launch(self, argv, workspace, settings):
        status = await self.backend.status(settings)
        if not status.available or not status.enabled:
            raise ToolError(
                "worker_sandbox_unavailable", "Worker commands require an available OS sandbox"
            )
        return await self.backend.launch(argv, workspace, settings)
