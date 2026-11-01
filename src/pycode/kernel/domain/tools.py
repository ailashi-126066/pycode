# Source: https://github.com/earendil-works/pi/blob/42a3497d03ad17e308a2299fa824727894f2c0ec/packages/agent/src/types.ts
# Adaptation: explicit tool classification and workspace execution identifiers.
"""Tool declarations and pure execution barrier selection."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

ExecutionMode = Literal["parallel", "sequential"]
ToolCategory = Literal["read", "write", "command", "external"]
# Borrowed from pi-durable: "safe" calls rerun after a restart instead of settling as interrupted.
Replay = Literal["never", "safe"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    category: ToolCategory = "external"
    execution_mode: ExecutionMode = "sequential"
    source: str = "builtin"
    implementation_hash: str | None = None
    waits_for_user: bool = False
    policy_hash: str | None = None
    model_visible: bool = True
    approval_details: dict[str, Any] = field(default_factory=dict)
    # None resolves to "safe" for read tools and "never" for everything else.
    replay: Replay | None = None

    def __post_init__(self) -> None:
        if self.replay is None:
            object.__setattr__(self, "replay", "safe" if self.category == "read" else "never")


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
