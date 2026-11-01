"""Process-local event numbering; restart reads session Store completed history only."""

import asyncio
from dataclasses import replace

from pycode.kernel.domain.events import Event


class MemoryEventJournal:
    def __init__(self):
        self.sequences: dict[tuple[str, str], int] = {}
        self.lock = asyncio.Lock()

    async def append_event(self, event: Event) -> Event:
        """Number the event within its (session, run); several streams may share one run."""
        async with self.lock:
            key = (event.session_id, event.run_id)
            sequence = self.sequences.get(key, 0) + 1
            self.sequences[key] = sequence
            return replace(event, sequence=sequence)

    async def aclose(self):
        self.sequences.clear()
