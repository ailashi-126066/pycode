"""Consumer-owned execution capabilities; no session or framework implementation types."""

from typing import Literal, Protocol, TypeVar

from pycode.kernel import Message, ModelSelection, ProjectedContext, RunResult, SessionSnapshot

InputKind = Literal["steer", "follow_up"]


class InputQueue(Protocol):
    def has(self, kind: InputKind) -> bool: ...
    def take(self, kind: InputKind, *, one_at_a_time: bool = False) -> tuple[Message, ...]: ...


class DurableInputDelivery(Protocol):
    async def submit(self, message: Message, kind: InputKind, run_id: str) -> bool: ...
    async def pending(self, run_id: str) -> InputQueue: ...
    async def finish_boundary(self, run_id: str) -> InputQueue: ...


class HistoryProjector(Protocol):
    def transform_context(self, snapshot: SessionSnapshot) -> ProjectedContext: ...


class RequestTracking(Protocol):
    active_request_id: str | None
    active_attempt: int

    @property
    def selection(self) -> ModelSelection: ...


class TitleAssignment(Protocol):
    async def assign(self, message: Message) -> None: ...


class WorkspaceReadback(Protocol):
    receipts: dict[str, str]


RuntimeContextT = TypeVar("RuntimeContextT", contravariant=True)


class Runtime(Protocol[RuntimeContextT]):
    @property
    def checkpointer(self) -> object: ...

    async def restore_task(self, context: RuntimeContextT) -> None: ...

    async def continue_task(self, context: RuntimeContextT) -> RunResult: ...

    async def run(
        self,
        inputs: tuple[Message, ...],
        context: RuntimeContextT,
        *,
        history: tuple[Message, ...] = (),
    ) -> RunResult: ...
    async def resume(self, context: RuntimeContextT, answers: dict | None = None) -> RunResult: ...
    def discard(self) -> None: ...
