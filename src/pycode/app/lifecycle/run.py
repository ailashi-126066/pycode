"""Own successfully assembled runs and roll back failed assembly lifetimes."""

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Protocol

from pycode.agent import RunContext, Runtime
from pycode.kernel import MemoryEventJournal
from pycode.models import RequestStore


class AsyncResource(Protocol):
    async def aclose(self) -> None: ...


@dataclass
class RunAssembly:
    """Rollback only resources created by a run that failed before ownership transfer."""

    events: Callable[[], Awaitable[None]] | None = None
    model: Callable[[], Awaitable[None]] | None = None
    summary: Callable[[], Awaitable[None]] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, error_type, error, traceback):
        if error_type is not None:
            async with AsyncExitStack() as pending:
                for close in (self.events, self.model, self.summary):
                    if close is not None:
                        pending.push_async_callback(close)


@dataclass
class RuntimeContainer:
    runtime: Runtime[RunContext]
    context: RunContext
    request_store: RequestStore
    model_owner: AsyncResource
    owns_summary: bool = True
    event_store: MemoryEventJournal | None = None
    preparations: tuple[Callable[[], Awaitable[None]], ...] = ()

    async def prepare_tools(self):
        for prepare in self.preparations:
            await prepare()

    async def aclose(self):
        try:
            if self.owns_summary and self.context.summary is not None:
                await self.context.summary.aclose()
        finally:
            try:
                await self.model_owner.aclose()
            finally:
                if self.event_store is not None:
                    await self.event_store.aclose()
