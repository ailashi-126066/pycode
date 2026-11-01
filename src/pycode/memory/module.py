"""LangMem consolidation and Markdown memory files assembled without application globals."""

from functools import partial
from pathlib import Path

from pycode.memory.adapters.background import ConfiguredMemoryUpdater
from pycode.memory.adapters.memory_tools import memory_tools
from pycode.memory.adapters.storage.markdown_memory import MarkdownMemoryStore
from pycode.memory.domain.memory import NAMESPACES, Scope
from pycode.memory.services.memory import MemoryService
from pycode.models import RetryPolicy


class MemoryModule:
    def scoped(self, workspace, user_data, *, trusted=False, mode=None):
        return type(self)(workspace, user_data, trusted=trusted, mode=mode)

    def __init__(self, workspace, user_data, *, trusted=False, mode: str | None = "auto"):
        self.mode = mode
        # User memory always; project memory only in a trusted project.
        self.roots: dict[Scope, Path] = {"user": user_data / "memory"}
        if trusted:
            self.roots["project"] = workspace / ".pycode/memory"

    def service(self, history, *, updater=None):
        store = MarkdownMemoryStore({NAMESPACES[scope]: root for scope, root in self.roots.items()})
        scopes = {NAMESPACES[scope]: scope for scope in self.roots}
        return MemoryService(history, store, scopes, updater=updater, mode=self.mode)

    def mounts(self):
        """Read-only virtual prefixes; the model reads /memory/<scope>/<id>.md with read."""
        return {f"/memory/{scope}/": root for scope, root in self.roots.items()}

    def updater(self, provider, model_factory, facade_factory):
        return ConfiguredMemoryUpdater(
            provider,
            partial(model_factory, retry_policy=RetryPolicy(max_retries=0)),
            facade_factory,
        )

    def tools(self, memory, *, read_only=False):
        return memory_tools(memory, read_only=read_only)
