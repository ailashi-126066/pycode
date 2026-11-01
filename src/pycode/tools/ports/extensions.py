"""Consumer-owned native boundaries for optional tool capabilities."""

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, TypedDict

from pycode.kernel import ToolCall, ToolContext, ToolResult


# Pi: AfterToolCallResult. Present fields replace the executed result's fields.
class AfterToolCallResult(TypedDict, total=False):
    text: str
    details: dict[str, Any]
    is_error: bool


class ToolExtension(Protocol):
    async def validate_call(self, context: ToolContext, call: ToolCall) -> None:
        """Read-only rejection checks; may run for undisclosed calls before authorization.

        This capability must never request approval, execute a tool, or perform
        external effects. Accepting here grants no authority to execute.
        """
        ...

    async def before_call(self, context: ToolContext, call: ToolCall) -> None:
        """Execution preparation, called only after the executor authorizes."""
        ...

    # Pi: AgentLoopConfig.afterToolCall; only executed calls reach it, before persistence.
    def after_tool_call(self, call: ToolCall, result: ToolResult) -> AfterToolCallResult | None: ...
    async def after_persist(
        self, context: ToolContext, call: ToolCall, result: ToolResult, entry_id: str
    ) -> None: ...


class NativeToolBinding(Protocol):
    # The SDK object is opaque here; the framework adapter checks its native type.
    def framework_tool(self, name: str) -> object | None: ...
    def call_scope(self, name: str) -> AbstractAsyncContextManager[None]: ...
    def normalize(
        self, name: str, content: object, status: str, artifact: object, call_id: str
    ) -> ToolResult: ...
