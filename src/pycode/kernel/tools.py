# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/types.ts
# Adaptation: explicit tool classification and workspace execution identifiers.
"""Tool declarations and the executable tool interface."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pycode.kernel.messages import ToolResult

ExecutionMode = Literal["parallel", "sequential"]
ToolCategory = Literal["read", "write", "command", "external"]
# Borrowed from pi-durable: "safe" calls rerun after a restart instead of settling as interrupted.
Replay = Literal["never", "safe"]
# Pi: ToolDefinition.exposure. Direct tools are declared on every request; deferred ones
# become callable after ToolSearch loads them.
Exposure = Literal["direct", "deferred"]


# Pi: AgentTool (label, promptSnippet, promptGuidelines, executionMode default "parallel").
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    category: ToolCategory = "external"
    execution_mode: ExecutionMode = "parallel"
    source: str = "builtin"
    waits_for_user: bool = False
    model_visible: bool = True
    approval_details: dict[str, Any] = field(default_factory=dict)
    # None resolves to "safe" for read tools and "never" for everything else.
    replay: Replay | None = None
    # None resolves to the tool name.
    label: str | None = None
    exposure: Exposure = "deferred"
    # One line in the system prompt's tool list; tools without a snippet are not listed.
    prompt_snippet: str | None = None
    prompt_guidelines: tuple[str, ...] = ()
    # Pi: AgentTool.prepareArguments, applied by prepare_tool_call before schema validation.
    prepare_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = field(
        default=None, compare=False, repr=False, metadata={"serialize": False}
    )

    def __post_init__(self) -> None:
        if self.replay is None:
            object.__setattr__(self, "replay", "safe" if self.category == "read" else "never")
        if self.label is None:
            object.__setattr__(self, "label", self.name)
        # Specs restored from checkpoints carry lists.
        object.__setattr__(self, "prompt_guidelines", tuple(self.prompt_guidelines))


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    run_id: str
    operation_id: str
    workspace: str
    call_id: str = ""
    tool_name: str = ""
    attempt: int = 0
    agent_id: str = "lead"
    progress: Callable[[str], Awaitable[None]] | None = field(
        default=None, compare=False, repr=False, metadata={"serialize": False}
    )


class AgentTool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...
    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult: ...
