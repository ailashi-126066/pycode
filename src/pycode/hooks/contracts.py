"""Hooks contracts: component-owned implementation."""

from typing import Protocol

from pycode.hooks.types import HookAction, HookContext
from pycode.kernel import ToolContext, ToolResult


class HookActions(Protocol):
    async def execute(
        self, action: HookAction, context: HookContext, tool: ToolContext
    ) -> ToolResult: ...


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
