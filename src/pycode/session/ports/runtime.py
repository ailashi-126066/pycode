"""A common execution boundary for main, CLI and later worker sessions."""

from typing import Protocol

from pycode.context import SummaryReport
from pycode.kernel import Message, RunResult
from pycode.models import ProviderConfig
from pycode.session.ports.history import SessionStore


class RuntimeLauncher(Protocol):
    def has_live_task(self) -> bool: ...
    def prepare_handoff(self) -> None: ...
    def reassign_team(self, member, agent_id, prompt_factory, consumer) -> None: ...
    async def continue_handoff(self, provider, store, run_id) -> RunResult: ...

    async def cancel_wait(self, run_id: str) -> None: ...

    async def run(
        self,
        provider: ProviderConfig,
        store: SessionStore,
        inputs: tuple[Message, ...],
        history: tuple[Message, ...],
    ) -> RunResult: ...

    async def compact(self, provider: ProviderConfig, store: SessionStore) -> SummaryReport: ...

    async def aclose(self) -> None: ...

    async def invalidate_summary(self, reason: str) -> None: ...
    async def resume(
        self,
        provider: ProviderConfig,
        store: SessionStore,
        run_id: str,
        answers: dict | None = None,
    ) -> RunResult: ...
