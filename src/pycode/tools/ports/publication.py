"""Native publication and lifecycle capabilities consumed by tool settlement."""

from typing import Protocol, TypeVar

from pycode.kernel import Message


class MessagePublisher(Protocol):
    async def publish(self, message: Message) -> str: ...


ContextT = TypeVar("ContextT", contravariant=True)


class ToolLifecycle(Protocol[ContextT]):
    async def __call__(
        self, context: ContextT, event: str, *, message: str = "", error: str = ""
    ) -> None: ...
