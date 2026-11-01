# Pi 没有，pycode 新增
"""One tool call from issue to settlement; Pi keeps this state implicit in executeToolCalls."""

import time
from dataclasses import dataclass, field
from typing import Literal

from ..kernel import StateMachine, ToolCall, ToolResult

ToolCallStatus = Literal["issued", "validating", "awaiting_approval", "running", "settled"]
ToolCallTrigger = Literal["validate", "request_approval", "approve", "run", "settle"]
ToolCallOutcome = Literal["completed", "error", "denied", "abandoned", "interrupted", "aborted"]

TOOL_CALL_TRANSITIONS: StateMachine[ToolCallStatus, ToolCallTrigger] = StateMachine(
    {
        ("issued", "validate"): ("validating", None),
        ("validating", "request_approval"): ("awaiting_approval", None),
        ("validating", "run"): ("running", None),
        ("awaiting_approval", "approve"): ("running", None),
        ("validating", "settle"): ("settled", None),
        ("awaiting_approval", "settle"): ("settled", None),
        ("running", "settle"): ("settled", None),
    }
)


@dataclass
class ToolCallState:
    status: ToolCallStatus = "issued"
    outcome: ToolCallOutcome | None = None
    began: float = field(default_factory=time.monotonic)

    def apply(self, trigger: ToolCallTrigger) -> None:
        self.status = TOOL_CALL_TRANSITIONS.apply(self.status, trigger)

    def settle(self, result: ToolResult) -> None:
        self.apply("settle")
        self.outcome = tool_call_outcome(result)


def tool_call_outcome(result: ToolResult) -> ToolCallOutcome:
    if result.error_code == "permission_denied":
        return "denied"
    if result.error_code == "tool_abandoned":
        return "abandoned"
    if result.error_code == "tool_interrupted":
        return "interrupted"
    return "error" if result.is_error else "completed"


def abandoned_result(call: ToolCall) -> ToolResult:
    """The run ended before this call started, so it has no effects."""
    return ToolResult(
        call.id,
        call.name,
        "tool_abandoned: the run ended before this call started; it was not executed.",
        is_error=True,
        error_code="tool_abandoned",
        details={"executed": False},
    )


def interrupted_result(call: ToolCall, cause: str) -> ToolResult:
    """The call started (tool_started was written) and may have partial effects."""
    return ToolResult(
        call.id,
        call.name,
        f"tool_interrupted: {cause}. "
        "Effects may have occurred; inspect current state before a new call.",
        is_error=True,
        error_code="tool_interrupted",
    )
