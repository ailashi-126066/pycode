"""Framework-free per-run execution state bound to consumer-owned capabilities."""

import asyncio
from dataclasses import dataclass, field

from pycode.agent.ports import (
    DurableInputDelivery,
    HistoryProjector,
    InputQueue,
    RequestTracking,
    TeamMailbox,
    WorkspaceReadback,
)
from pycode.context import ProjectedContext, SummaryService, ToolEvictionService
from pycode.hooks import Hooks
from pycode.kernel import (
    EventSink,
    HistoryJournal,
    Message,
    MessageJournal,
    ModelSelection,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from pycode.plans import PlanService
from pycode.tools import ToolExecutor, ToolOutputStore, ToolRegistry


@dataclass
class RunContext:
    session_id: str
    run_id: str
    workspace: str
    selection: ModelSelection
    events: EventSink
    journal: MessageJournal
    executor: ToolExecutor
    registry: ToolRegistry
    inputs: InputQueue
    requests: RequestTracking
    initial_inputs: tuple[Message, ...] = ()
    handoff_pending: bool = False
    one_at_a_time: bool = False
    completed: list[Message] = field(default_factory=list)
    history_store: HistoryJournal | None = None
    projector: HistoryProjector | None = None
    outputs: ToolOutputStore | None = None
    eviction: ToolEvictionService | None = None
    durable_inputs: DurableInputDelivery | None = None
    agent_id: str = "lead"
    summary: SummaryService | None = None
    model_source_leaf_id: str | None = None
    model_projection: ProjectedContext | None = None
    plans: PlanService | None = None
    workspace_files: WorkspaceReadback | None = None
    model_tools: tuple[ToolSpec, ...] = ()
    system_hash: str = ""
    task_context_tokens: int = 0
    disclosed_tool_names: tuple[str, ...] | None = None
    read_only: bool = False
    failure_guard: dict = field(default_factory=dict)
    interrupted_calls: set[str] = field(default_factory=set)
    duplicate_calls: asyncio.Condition = field(default_factory=asyncio.Condition)
    dispatched_calls: set[str] = field(default_factory=set)
    team: TeamMailbox | None = None
    hooks: Hooks | None = None
    mcp_config_hash: str = ""
    ready_results: dict[str, tuple[ToolCall, ToolResult, dict]] = field(default_factory=dict)
    settled_calls: set[str] = field(default_factory=set)
    settlement: asyncio.Condition = field(default_factory=asyncio.Condition)
    turn_end_sent: bool = False
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
