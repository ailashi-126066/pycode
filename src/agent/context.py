# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
"""Per-run state: the loop's state machine, the continuation policy and the run context."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from deepagents.graph import DeepAgentState

from ..context import ContextProjector, SummaryService, ToolEvictionService
from ..kernel import (
    EventStream,
    Message,
    ModelSelection,
    ProjectedContext,
    RunScope,
    StateMachine,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from ..tools import FailureGuard, ToolExecutor, ToolRegistry, empty_failure_guard

if TYPE_CHECKING:
    from ..models import ModelRequestRunner
    from ..session import DurableInputs, PendingInputs, SqliteSessionStore
    from ..tools import PycodeBackend

# The Agent loop as an explicit state machine; Pi keeps it implicit in runLoop.
RunStatus = Literal[
    "starting", "model_turn", "tool_turn", "awaiting_input", "verifying", "finished"
]
RunTrigger = Literal["model", "tools", "wait", "resume", "verify", "finish"]
RunEndReason = Literal[
    "completed",
    "aborted",
    "model_error",
    "error",
    "interrupted",
    "turn_limit",
    "loop_detected",
    # A blocked task board ends the run with its question for the user.
    "needs_input",
]

RUN_TRANSITIONS: StateMachine[RunStatus, RunTrigger] = StateMachine(
    {
        ("starting", "model"): ("model_turn", None),
        ("model_turn", "tools"): ("tool_turn", None),
        ("tool_turn", "model"): ("model_turn", None),
        ("tool_turn", "wait"): ("awaiting_input", None),
        ("awaiting_input", "resume"): ("tool_turn", None),
        # The model answered without tool calls: check queued input before ending.
        ("model_turn", "verify"): ("verifying", None),
        ("verifying", "model"): ("model_turn", None),
        **{
            (state, "finish"): ("finished", None)
            for state in ("starting", "model_turn", "tool_turn", "awaiting_input", "verifying")
        },
    }
)


@dataclass
class RunState:
    status: RunStatus = "starting"
    turn: int = 0
    # Set when the loop stops early (turn_limit, needs_input, loop_detected); else it completes.
    reason: RunEndReason | None = None

    def apply(self, trigger: RunTrigger) -> bool:
        """Apply the trigger; True when the transition ends a turn (Pi emits turn_end)."""
        ends_turn = self.status in {"model_turn", "tool_turn"} and trigger in {
            "model",
            "verify",
            "finish",
        }
        self.status = RUN_TRANSITIONS.apply(self.status, trigger)
        return ends_turn


@dataclass(frozen=True)
class NextTurn:
    action: Literal["request", "end"]
    queue: Literal["steer", "follow_up"] | None = None


def next_turn(*, steering_pending: bool = False, follow_up_pending: bool = False) -> NextTurn:
    """Select queued input after the framework has completed the current Agent loop."""
    if steering_pending:
        return NextTurn("request", queue="steer")
    if follow_up_pending:
        return NextTurn("request", queue="follow_up")
    return NextTurn("end")


class PycodeState(DeepAgentState):
    """Private graph state owned by the agent runtime."""

    session_id: str
    run_id: str
    branch_leaf_id: str | None
    turn: int
    final_text: str
    model_tools: list[dict]


@dataclass
class RunContext:
    session_id: str
    run_id: str
    workspace: str
    selection: ModelSelection
    events: EventStream
    journal: "SqliteSessionStore"
    executor: ToolExecutor
    registry: ToolRegistry
    inputs: "PendingInputs"
    requests: "ModelRequestRunner"
    max_turns: int
    # Shared immutable identity and event data for callbacks.
    scope: RunScope
    history_store: "SqliteSessionStore"
    projector: ContextProjector
    eviction: ToolEvictionService
    durable_inputs: "DurableInputs"
    initial_inputs: tuple[Message, ...] = ()
    initial_entry_ids: tuple[str, ...] = ()
    one_at_a_time: bool = False
    completed: list[Message] = field(default_factory=list)
    agent_id: str = "lead"
    summary: SummaryService | None = None
    model_source_leaf_id: str | None = None
    model_projection: ProjectedContext | None = None
    workspace_files: "PycodeBackend | None" = None
    model_tools: tuple[ToolSpec, ...] = ()
    system_hash: str = ""
    disclosed_tool_names: tuple[str, ...] | None = None
    read_only: bool = False
    failure_guard: FailureGuard = field(default_factory=empty_failure_guard)
    interrupted_calls: set[str] = field(default_factory=set)
    duplicate_calls: asyncio.Condition = field(default_factory=asyncio.Condition)
    dispatched_calls: set[str] = field(default_factory=set)
    # Calls that finished preparing (executing or settled); parallel preparation waits on it.
    prepared_calls: set[str] = field(default_factory=set)
    ready_results: dict[str, tuple[ToolCall, ToolResult, dict]] = field(default_factory=dict)
    settled_calls: set[str] = field(default_factory=set)
    settlement: asyncio.Condition = field(default_factory=asyncio.Condition)
    run_state: RunState = field(default_factory=RunState)
    # Pi's pendingMessages: steering polled at a turn end, delivered before the next request.
    steering: tuple[Message, ...] = ()
    tool_tasks: set[asyncio.Task] = field(default_factory=set)
    # Visible text of the streaming reply; None until its first chunk sent message_start.
    streamed_text: str | None = None

    @property
    def assistant(self) -> Message | None:
        return next((m for m in reversed(self.completed) if m.role == "assistant"), None)

    @property
    def call_order(self) -> list[str]:
        message = self.assistant
        return [call.id for call in message.tool_calls] if message is not None else []
