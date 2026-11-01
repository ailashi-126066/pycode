"""Hooks contracts: component-owned implementation."""

from typing import Protocol

from pycode.hooks.types import Hook, HookContext
from pycode.kernel import ExecutableTool, StopReason, ToolCall, ToolContext, ToolResult, ToolSpec


class HookActions(Protocol):
    async def matches(self, hook: Hook, context: HookContext) -> bool: ...
    async def execute(
        self, action: dict, context: HookContext, tool: ToolContext
    ) -> ToolResult: ...


class HookExecution(Protocol):
    async def begin(self, context: ToolContext, call: ToolCall) -> object: ...
    async def execute(
        self, context: ToolContext, call: ToolCall, stop_reason: StopReason
    ) -> ToolResult: ...
    async def emit_tool_execution_end(
        self, context: ToolContext, call: ToolCall, result: ToolResult
    ) -> None: ...


class HookPlanGuard(Protocol):
    async def guard(self, context: ToolContext, spec: ToolSpec, call: ToolCall) -> bool: ...


class HookExecutorFactory(Protocol):
    def __call__(
        self, tools: tuple[ExecutableTool, ...], *, plans: HookPlanGuard | None, read_only: bool
    ) -> HookExecution: ...


class LifecycleHooks(Protocol):
    async def fire(self, context: HookContext) -> str | None: ...


class LifecycleContext(Protocol):
    @property
    def session_id(self) -> str: ...
    @property
    def run_id(self) -> str: ...
    @property
    def workspace(self) -> str: ...
    @property
    def hooks(self) -> LifecycleHooks | None: ...
