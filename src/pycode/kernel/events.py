# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/types.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent.ts
# Adaptation: an envelope with sequence, turn and agent identity around Pi's event payloads.
"""Event envelope, ordered publisher and process-local event numbering."""

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from pycode.kernel.errors import OutputClosed
from pycode.kernel.messages import Message
from pycode.kernel.values import freeze_value

# 11.1 section 3.1: Pi's 10 AgentEvent types, then 8 pycode additions.
EventKind = Literal[
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "needs_input",
    "result",
    "retry",
    "compaction",
    "model_change",
    "subagent",
    "task",
    "notification",
]


@dataclass(frozen=True)
class Event:
    sequence: int
    session_id: str
    run_id: str
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)
    operation_id: str | None = None
    turn: int = 0
    agent_id: str = "lead"
    parent_call_id: str | None = None


# Payloads of the 18 events in 11.1 section 3.1; Event.kind carries Pi's `type`.
# Pi AgentEvent: the first 10 payloads.
class AgentStartEvent(TypedDict):
    pass


class AgentEndEvent(TypedDict):
    reason: str
    error_code: NotRequired[str]
    error: NotRequired[str]


class TurnStartEvent(TypedDict):
    turn: int


class TurnEndEvent(TypedDict):
    turn: int
    # None when the turn failed before its reply was saved.
    message_id: str | None
    tool_result_ids: list[str]


class MessageStartEvent(TypedDict):
    message: dict[str, Any]
    # A streamed reply starts with its first chunk, once per retry attempt.
    request_id: NotRequired[str]
    attempt: NotRequired[int]


class MessageUpdateEvent(TypedDict):
    request_id: str
    kind: Literal["text", "thinking"]
    delta: str


class MessageEndEvent(TypedDict):
    """An assistant message carries its usage, stop_reason, error and duration_ms."""

    message: dict[str, Any]
    entry_id: NotRequired[str]


class ToolExecutionStartEvent(TypedDict):
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


class ToolExecutionUpdateEvent(TypedDict):
    tool_call_id: str
    tool_name: str
    # "running" when the tool body starts; replaces tool_action_start.
    state: NotRequired[Literal["running"]]
    # Pi sends the accumulated partialResult; pycode streams each new output chunk.
    delta: NotRequired[str]


class ToolExecutionEndEvent(TypedDict):
    tool_call_id: str
    tool_name: str
    result: dict[str, Any]
    is_error: bool
    status: str
    outcome: str
    duration_ms: int


# pycode additions.
class NeedsInputEvent(TypedDict):
    interrupt_id: str
    checkpoint_id: str
    kind: str


class ResultEvent(TypedDict):
    """Pi's agent_settled: the run is finished and persisted."""

    outcome: str
    final_text: str
    pending: list[dict[str, Any]]
    checkpoint_id: str | None
    leaf_id: str | None
    usage: dict[str, Any]


class RetryEvent(TypedDict):
    attempt: int
    max_retries: int
    delay_seconds: float
    error_code: str
    request_id: str | None


class CompactionEvent(TypedDict):
    job_id: str
    status: str
    error_code: NotRequired[str | None]
    context_tokens: NotRequired[int]


class ModelChangeEvent(TypedDict):
    entry_id: str
    selection: dict[str, Any]


class SubagentEvent(TypedDict):
    """A subagent's status, and one of its own events when forwarded; the envelope's
    agent_id and parent_call_id name the subagent and the Agent call that started it."""

    name: str
    status: str
    description: str
    event: NotRequired[dict[str, Any]]


class TaskEvent(TypedDict):
    """Task board snapshot, consumed by stream-json only."""

    board: dict[str, Any]


class NotificationEvent(TypedDict):
    """kind is hook, input_delivered or tool_eviction; the other keys belong to that kind."""

    kind: str
    message: NotRequired[str]
    hook_id: NotRequired[str]
    event: NotRequired[str]
    success: NotRequired[bool]
    full_output_path: NotRequired[str | None]
    input_id: NotRequired[str]
    queue_kind: NotRequired[str]
    entry_id: NotRequired[str]
    targets: NotRequired[list[Any]]
    reclaimed_tokens: NotRequired[int]


AgentEvent = (
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
    | NeedsInputEvent
    | ResultEvent
    | RetryEvent
    | CompactionEvent
    | ModelChangeEvent
    | SubagentEvent
    | TaskEvent
    | NotificationEvent
)


@dataclass(frozen=True)
class RunResult:
    outcome: str
    final_text: str
    messages: tuple[Message, ...]
    pending: tuple[dict[str, Any], ...] = ()
    checkpoint_id: str | None = None
    run_id: str | None = None


class EventConsumer(Protocol):
    async def on_event(self, event: Event) -> None: ...


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


class EventStream:
    def __init__(
        self,
        session_id: str,
        run_id: str,
        consumers: tuple[EventConsumer, ...],
        initial_sequence: int = 0,
        *,
        journal: MemoryEventJournal | None = None,
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
                # Frozen once here; every consumer receives the same object.
                data=freeze_value(data),
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
                    await consumer.on_event(event)
                except BrokenPipeError:
                    # stdout is gone: stop writing to it and cancel the run.
                    self._closed.add(index)
                    raise OutputClosed("Console output closed") from None
            return event
