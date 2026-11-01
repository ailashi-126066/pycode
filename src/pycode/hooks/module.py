"""Own Hook configuration loading, action implementations and lifecycle engines."""

from collections.abc import Mapping
from copy import copy
from pathlib import Path

from pycode.hooks.actions import ConfiguredHookActions
from pycode.hooks.api import Hooks
from pycode.hooks.config import load_hooks
from pycode.hooks.contracts import HookActions
from pycode.hooks.types import Hook
from pycode.kernel import EventSink, HistoryJournal, data_hash, plain_data
from pycode.kernel.adapters.path_io import owned_io
from pycode.sandbox import OutputSink, ProcessRunner, SandboxBackend, SandboxSettings


class HooksModule:
    def scoped(self, workspace, user_data, *, trusted=False, worker=False):
        scoped = copy(self)
        scoped.workspace, scoped.user_data = workspace, user_data
        scoped.trusted, scoped.worker = trusted, worker
        return scoped

    def __init__(
        self,
        workspace: Path,
        user_data: Path,
        *,
        trusted: bool = False,
        worker: bool = False,
        enabled: bool = True,
    ) -> None:
        self.workspace = workspace
        self.user_data = user_data
        self.trusted = trusted
        self.worker = worker
        self.enabled = enabled

    async def configuration(self) -> tuple[Hook, ...]:
        if not self.enabled:
            return ()
        roots = [("user", self.user_data / "hooks.json")]
        if self.trusted:
            roots.append(("project", self.workspace / ".pycode" / "hooks.json"))
        hooks = await owned_io(load_hooks, tuple(roots))
        if self.worker:
            return tuple(hook for hook in hooks if hook.action.type == "prompt")
        return hooks

    @staticmethod
    def config_hash(hooks: tuple[Hook, ...]) -> str:
        return data_hash([plain_data(hook) for hook in hooks]) if hooks else ""

    @staticmethod
    def actions(
        outputs: OutputSink,
        shell: str,
        sandbox: SandboxBackend,
        settings: SandboxSettings,
        environment: Mapping[str, str],
        *,
        process_runner: ProcessRunner,
        passthrough: tuple[str, ...] = (),
    ) -> HookActions:
        return ConfiguredHookActions(
            outputs,
            shell,
            sandbox,
            settings,
            environment,
            process_runner=process_runner,
            passthrough=passthrough,
        )

    @staticmethod
    def engine(
        hooks: tuple[Hook, ...],
        history: HistoryJournal,
        actions: HookActions,
        events: EventSink,
    ) -> Hooks:
        return Hooks(hooks, history, actions, events)
