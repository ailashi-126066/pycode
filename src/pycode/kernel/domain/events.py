# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/types.ts
# Adaptation: sequence and stable execution identifiers for common CLI/TUI events.
"""Unified event envelope; deltas are distinct from completed messages."""

from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict

EventKind = Literal[
    "team",
    "model_change",
    "model_request_start",
    "model_request_end",
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_delta",
    "thinking_delta",
    "message_end",
    "tool_execution_start",
    "tool_action_start",
    "tool_action_end",
    "tool_output_ready",
    "tool_execution_update",
    "tool_execution_end",
    "usage",
    "retry",
    "needs_input",
    "summary",
    "notification",
    "error",
    "result",
]


@dataclass(frozen=True)
class Event:
    sequence: int
    session_id: str
    run_id: str
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)
    operation_id: str | None = None


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
    message_id: str
    tool_result_ids: list[str]


class MessageStartEvent(TypedDict):
    message: dict[str, Any]


class MessageUpdateEvent(TypedDict):
    message_id: str
    kind: Literal["text", "thinking"]
    delta: str


class MessageEndEvent(TypedDict):
    message: dict[str, Any]
    entry_id: NotRequired[str]
    usage: NotRequired[dict[str, Any]]
    duration_ms: NotRequired[int]


class ToolExecutionStartEvent(TypedDict):
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


class ToolExecutionUpdateEvent(TypedDict):
    tool_call_id: str
    tool_name: str
    state: NotRequired[str]
    partial_result: NotRequired[Any]


class ToolExecutionEndEvent(TypedDict):
    tool_call_id: str
    tool_name: str
    result: dict[str, Any]
    is_error: bool
    status: str
    outcome: str
    output_id: str | None
    duration_ms: NotRequired[int]


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


class ModelChangeEvent(TypedDict):
    entry_id: str
    selection: dict[str, Any]


class SubagentEvent(TypedDict):
    agent_id: str
    status: str


class TaskEvent(TypedDict):
    """Task board snapshot, consumed by stream-json only."""

    board: dict[str, Any]


class NotificationEvent(TypedDict):
    kind: str
    message: NotRequired[str]


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
