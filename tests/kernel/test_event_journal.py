"""Events are process-local; completed facts belong to the session Store."""

import asyncio

from pycode.kernel import EventStream, MemoryEventJournal


async def test_shared_publishers_are_ordered_but_restart_starts_empty(tmp_path):
    journal = MemoryEventJournal()
    first = EventStream("s", "r", (), journal=journal)
    second = EventStream("s", "r", (), journal=journal)
    events = await asyncio.gather(
        *(
            first.emit("notification", {"i": i}) if i % 2 else second.emit("notification", {"i": i})
            for i in range(20)
        )
    )
    assert sorted(event.sequence for event in events) == list(range(1, 21))
    event = await EventStream("s", "r", (), journal=MemoryEventJournal()).emit(
        "result", {"outcome": "completed"}
    )
    assert event.sequence == 1
    assert list(tmp_path.iterdir()) == []
