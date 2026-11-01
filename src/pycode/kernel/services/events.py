# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent.ts
"""Single ordered event publisher; it owns no alternative message history."""

import asyncio
from typing import Any

from pycode.kernel.domain.errors import OutputClosed
from pycode.kernel.domain.events import Event, EventKind
from pycode.kernel.domain.values import clone_value
from pycode.kernel.ports.events import EventConsumer, EventJournal


class EventStream:
    def __init__(
        self,
        session_id: str,
        run_id: str,
        consumers: tuple[EventConsumer, ...],
        initial_sequence: int = 0,
        *,
        journal: EventJournal | None = None,
        agent_id: str = "lead",
        parent_call_id: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._run_id = run_id
        self._consumers = consumers
        self._sequence = initial_sequence
        self._lock = asyncio.Lock()
        self._journal = journal
        self._agent_id = agent_id
        self._parent_call_id = parent_call_id
        self._turn = 0
        self._closed: set[int] = set()

    # Pi: Agent.processEvents awaits every listener in order; a listener error fails the run.
    async def emit(
        self, kind: EventKind, data: dict[str, Any], *, operation_id: str | None = None
    ) -> Event:
        async with self._lock:
            if kind == "turn_start":
                self._turn = data["turn"]
            event = Event(
                sequence=self._sequence + 1,
                session_id=self._session_id,
                run_id=self._run_id,
                kind=kind,
                data=clone_value(data),
                operation_id=operation_id,
                turn=self._turn,
                agent_id=self._agent_id,
                parent_call_id=self._parent_call_id,
            )
            if self._journal is not None:
                event = await self._journal.append_event(event)
            self._sequence = event.sequence
            for index, consumer in enumerate(self._consumers):
                if index in self._closed:
                    continue
                try:
                    await consumer.consume(clone_value(event))
                except BrokenPipeError:
                    # stdout is gone: stop writing to it and cancel the run.
                    self._closed.add(index)
                    raise OutputClosed("Console output closed") from None
            return event
