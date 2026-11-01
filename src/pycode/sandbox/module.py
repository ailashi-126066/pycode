"""Assemble platform isolation and command execution within the sandbox feature."""

import os
import sys
from copy import copy
from pathlib import Path

from pycode.kernel import AgentTool, ToolSpec
from pycode.sandbox.adapters.seatbelt import RequiredSandbox, SeatbeltSandbox, UnavailableSandbox
from pycode.sandbox.adapters.tools.bash import Bash, find_bash
from pycode.sandbox.adapters.tools.process_runner import run_process
from pycode.sandbox.domain.sandbox import SandboxSettings
from pycode.sandbox.domain.subprocess_env import validate_passthrough
from pycode.sandbox.ports.output import OutputSink
from pycode.sandbox.ports.sandbox import SandboxBackend


def skill_read_grant(sandbox: SandboxBackend, directory: Path) -> SandboxBackend:
    return (
        sandbox.for_skill(directory)
        if isinstance(sandbox, (RequiredSandbox, SeatbeltSandbox))
        else sandbox
    )


class SandboxModule:
    """Configuration lifetime owner; each tool receives the same explicit policy."""

    process_runner = staticmethod(run_process)

    def scoped(self, settings, *, isolated_worker=False, passthrough=(), path_prefix=()):
        scoped = copy(self)
        scoped.settings, scoped.isolated_worker = settings, isolated_worker
        scoped.passthrough, scoped.path_prefix = validate_passthrough(passthrough), path_prefix
        return scoped

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        isolated_worker: bool = False,
        passthrough: tuple[str, ...] = (),
        path_prefix: tuple[str, ...] = (),
    ) -> None:
        self.settings = settings
        self.isolated_worker = isolated_worker
        self.passthrough = validate_passthrough(passthrough)
        self.path_prefix = path_prefix

    def backend(self) -> SandboxBackend:
        return SeatbeltSandbox() if sys.platform == "darwin" else UnavailableSandbox()

    def worker(self) -> SandboxBackend:
        backend = self.backend()
        return RequiredSandbox(backend) if self.isolated_worker else backend

    @staticmethod
    def shell() -> str:
        return find_bash()

    @staticmethod
    def bash_spec() -> ToolSpec:
        """History inspection needs a declaration without probing the host shell."""
        return Bash.spec

    def bash(
        self, outputs: OutputSink, *, executable: str | None = None, skill_roots=()
    ) -> AgentTool:
        environment = None
        if self.path_prefix:
            environment = dict(os.environ)
            environment["PATH"] = os.pathsep.join((*self.path_prefix, environment.get("PATH", "")))
        sandbox = self.worker()
        for root in skill_roots:
            sandbox = skill_read_grant(sandbox, root)
        return Bash(
            executable or self.shell(),
            outputs,
            sandbox,
            self.settings,
            environment=environment,
            passthrough=self.passthrough,
        )

    skill_read_grant = staticmethod(skill_read_grant)
