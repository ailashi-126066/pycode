"""Awaited event delivery; consumers own display and persistence."""

from typing import Any, Protocol

from pycode.kernel.domain.events import Event, EventKind


class EventConsumer(Protocol):
    async def consume(self, event: Event) -> None: ...


class EventSink(Protocol):
    async def emit(
        self, kind: EventKind, data: dict[str, Any], *, operation_id: str | None = None
    ) -> Event: ...


class EventJournal(Protocol):
    async def append_event(self, event: Event) -> Event: ...

    async def aclose(self) -> None: ...
