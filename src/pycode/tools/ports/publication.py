"""Native publication capability consumed by tool settlement."""

from typing import Protocol

from pycode.kernel import Message


class MessagePublisher(Protocol):
    async def publish(self, message: Message) -> str: ...
