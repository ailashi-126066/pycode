"""Native background memory capabilities; model and LangGraph implementations are injected."""

from collections.abc import Sequence
from typing import Protocol

from pycode.kernel import Message


class MemoryUpdater(Protocol):
    async def consolidate(
        self,
        store: object,
        namespace: tuple[str, ...],
        summary: str,
        messages: Sequence[Message],
        session_id: str,
        run_id: str,
    ) -> object: ...
