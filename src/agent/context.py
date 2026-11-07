# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
"""Graph-owned execution state, phase validation and per-run services."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from deepagents.graph import DeepAgentState

from ..context import ContextProjector, SummaryService
from ..kernel import (
    EventStream,
    Message,
    ModelSelection,
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

# Interrupts live in framework checkpoints; phase describes the executing graph stage.
RunStatus = Literal["starting", "model_turn", "tool_turn", "verifying", "finished"]
RunTrigger = Literal["model", "tools", "verify", "finish"]
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
        # The model answered without tool calls: check queued input before ending.
        ("model_turn", "verify"): ("verifying", None),
        ("verifying", "model"): ("model_turn", None),
        **{
            (state, "finish"): ("finished", None)
            for state in ("starting", "model_turn", "tool_turn", "verifying")
        },
    }
)


def transition_phase(state, trigger: RunTrigger) -> RunStatus:
    """Validate a graph-state transition without keeping a second mutable state object."""
    phase = state.get("phase")
    if phase is None:
        # Checkpoints written before phase existed still have a turn and messages.
        phase = "starting" if not state.get("turn", 0) else "model_turn"
        if trigger == "model" and state.get("turn", 0):
            phase = "tool_turn"
    return RUN_TRANSITIONS.apply(phase, trigger)


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
    turn: int
    phase: RunStatus
    reason: RunEndReason | None
    # Last turn whose end event was published; prevents duplicate events during finish.
    ended_turn: int
    final_text: str
    model_tools: list[dict]


@dataclass
class RunContext:
    session_id: str
    run_id: str
    workspace: str
    selection: ModelSelection
    events: EventStream
    history: "SqliteSessionStore"
    executor: ToolExecutor
    registry: ToolRegistry
    inputs: "PendingInputs"
    requests: "ModelRequestRunner"
    max_turns: int
    # Shared immutable identity and event data for callbacks.
    scope: RunScope
    projector: ContextProjector
    durable_inputs: "DurableInputs"
    initial_inputs: tuple[Message, ...] = ()
    initial_entry_ids: tuple[str, ...] = ()
    one_at_a_time: bool = False
    agent_id: str = "lead"
    summary: SummaryService | None = None
    model_source_leaf_id: str | None = None
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
    ready_results: dict[str, tuple[ToolCall, ToolResult]] = field(default_factory=dict)
    settled_calls: set[str] = field(default_factory=set)
    settlement: asyncio.Condition = field(default_factory=asyncio.Condition)
    # Pi's pendingMessages: steering polled at a turn end, delivered before the next request.
    steering: tuple[Message, ...] = ()
    tool_tasks: set[asyncio.Task] = field(default_factory=set)
    # Visible text of the streaming reply; None until its first chunk sent message_start.
    streamed_text: str | None = None

    async def run_messages(self) -> tuple[Message, ...]:
        """Read this run's original messages from its durable branch, without another list."""
        from ..kernel import SessionTree

        snapshot = await self.history.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        start = next(
            (
                i
                for i, entry in enumerate(path)
                if entry.run_id == self.run_id and entry.data.get("kind") == "run_start"
            ),
            None,
        )
        if start is None:
            initial_ids = {message.id for message in self.initial_inputs}
            start = next(
                (
                    i - 1
                    for i, entry in enumerate(path)
                    if entry.message is not None and entry.message.id in initial_ids
                ),
                -1,
            )
        messages = {message.id: message for message in self.initial_inputs}
        for entry in path[start + 1 :]:
            if entry.type == "message" and entry.message is not None:
                messages[entry.message.id] = entry.message
        return tuple(messages.values())

    async def last_assistant(self) -> Message | None:
        return next(
            (
                message
                for message in reversed(await self.run_messages())
                if message.role == "assistant"
            ),
            None,
        )
