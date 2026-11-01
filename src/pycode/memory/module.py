"""LangMem storage, recall and consolidation assembled without application globals."""

from copy import copy
from functools import partial

from pycode.kernel import Timing
from pycode.memory.adapters.background import ConfiguredMemoryUpdater
from pycode.memory.adapters.memory_tools import memory_tools
from pycode.memory.adapters.storage.json_memory import JsonMemoryStore, MemoryChangeLog
from pycode.memory.domain.namespaces import automatic_namespaces, scoped_memory_namespaces
from pycode.memory.services.memory import MemoryService
from pycode.models import RetryPolicy


class MemoryModule:
    def scoped(self, workspace, user_data, project_id, *, trusted=False, mode=None):
        scoped = copy(self)
        scoped.workspace, scoped.user_data, scoped.mode = workspace, user_data, mode
        scoped.namespaces = scoped_memory_namespaces(str(user_data.resolve()), project_id)
        scoped.automatic = automatic_namespaces(*scoped.namespaces, trusted=trusted)
        return scoped

    def __init__(
        self,
        workspace,
        user_data,
        project_id,
        timing: Timing,
        *,
        trusted=False,
        mode: str | None = "auto",
    ):
        self.workspace = workspace
        self.user_data = user_data
        self.timing = timing
        self.mode = mode
        self.namespaces = scoped_memory_namespaces(str(user_data.resolve()), project_id)
        self.automatic = automatic_namespaces(*self.namespaces, trusted=trusted)

    def service(self, history, *, updater=None):
        user, project = self.namespaces
        return MemoryService(
            history,
            JsonMemoryStore(
                {user: self.user_data / "memory", project: self.workspace / ".pycode/memory"}
            ),
            {user: "user", project: "project"},
            self.timing,
            MemoryChangeLog(self.user_data / "memory/changes.jsonl"),
            updater=updater,
            mode=self.mode,
            auto_namespaces=self.automatic,
        )

    def updater(self, provider, model_factory, facade_factory):
        return ConfiguredMemoryUpdater(
            provider,
            partial(model_factory, retry_policy=RetryPolicy(max_retries=0)),
            facade_factory,
        )

    def tools(self, memory, *, read_only=False):
        return memory_tools(memory, read_only=read_only)
