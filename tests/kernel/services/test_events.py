import asyncio

import pytest

from pycode.kernel.services.events import EventStream


class Recorder:
    def __init__(self):
        self.events = []

    async def consume(self, event):
        self.events.append(event)


async def test_concurrent_producers_have_one_ordered_event_sequence():
    recorder = Recorder()
    stream = EventStream("s1", "r1", (recorder,), initial_sequence=10)
    await asyncio.gather(
        *(stream.emit("message_update", {"kind": "text", "delta": str(i)}) for i in range(20))
    )
    assert [event.sequence for event in recorder.events] == list(range(11, 31))
    assert all(event.session_id == "s1" and event.run_id == "r1" for event in recorder.events)


async def test_payload_is_frozen_once_and_shared_by_every_consumer():
    first, second = Recorder(), Recorder()
    stream = EventStream("s1", "r1", (first, second))
    data = {"nested": {"text": "original"}}
    published = await stream.emit("message_end", data)
    data["nested"]["text"] = "later"
    assert first.events[0] is second.events[0] is published
    assert published.data["nested"]["text"] == "original"
    with pytest.raises(TypeError):
        published.data["nested"]["text"] = "changed"


async def test_consumer_error_fails_the_emit_and_the_consumer_keeps_receiving_events():
    class FailingConsumer:
        calls = 0

        async def consume(self, event):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("broken display")

    failing = FailingConsumer()
    stream = EventStream("s1", "r1", (failing,))
    with pytest.raises(ValueError, match="broken display"):
        await stream.emit("tool_execution_start", {"tool_call_id": "c1"})
    await stream.emit("agent_end", {"reason": "error"})
    assert failing.calls == 2


async def test_envelope_carries_turn_and_agent_identity():
    recorder = Recorder()
    stream = EventStream("s1", "r1", (recorder,), agent_id="child", parent_call_id="call-1")
    await stream.emit("agent_start", {})
    await stream.emit("turn_start", {"turn": 2})
    await stream.emit("message_end", {"message": {}})
    assert [event.turn for event in recorder.events] == [0, 2, 2]
    assert {(event.agent_id, event.parent_call_id) for event in recorder.events} == {
        ("child", "call-1")
    }


async def test_broken_pipe_requests_controlled_cancellation():
    class ClosedConsole:
        async def consume(self, event):
            raise BrokenPipeError("closed")

    stream = EventStream("s1", "r1", (ClosedConsole(),))
    with pytest.raises(asyncio.CancelledError, match="Console output closed"):
        await stream.emit("message_update", {"kind": "text", "delta": "cancel"})
