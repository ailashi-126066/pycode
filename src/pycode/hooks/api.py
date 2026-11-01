"""Hooks api: component-owned implementation."""

# Source: MewCode local hooks/engine.py; fixed local inventory in docs/source-map.md.
# SHA-256: 91e9d3162556ea54e9c1d211feb3ecf4126a302f50f7eb3e5f584194be9e9b29
# Adaptation: Hooks await their action directly, like Pi extension handlers; no ledger.

from typing import TYPE_CHECKING
from uuid import uuid4

from pycode.hooks.contracts import HookActions, LifecycleContext
from pycode.hooks.types import Hook, HookContext
from pycode.kernel import (
    DomainError,
    EntryDraft,
    EventStream,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSpec,
    clone_value,
)

if TYPE_CHECKING:
    from pycode.session import SqliteSessionStore


class Hooks:
    def __init__(
        self,
        hooks: tuple[Hook, ...],
        history: "SqliteSessionStore",
        actions: HookActions,
        events: EventStream,
    ) -> None:
        self._hooks = hooks
        self._history = history
        self._actions = actions
        self._events = events

    async def fire(self, context: HookContext) -> str | None:
        """Run matching Hooks in order; a pre_tool_use rejection is returned as text."""
        for hook in self._hooks:
            if hook.event != context.event_name:
                continue
            if hook.condition is not None and not hook.condition.matches(context.fields()):
                continue
            if hook.once and await self.fired_before(hook):
                continue
            result = await self.run(hook, context)
            if context.event_name != "pre_tool_use":
                continue
            # 11.1 section 26 #3: fail-closed; only a successful run lets the tool proceed.
            if result.is_error:
                return "Hook check did not complete successfully: " + result.text
            if hook.reject:
                return result.text or "Configured Hook rejected this tool"
        return None

    async def fired_before(self, hook: Hook) -> bool:
        snapshot = await self._history.snapshot()
        return any(
            entry.type == "hook_context" and entry.data["hook_id"] == hook.id
            for entry in snapshot.entries
        )

    async def run(self, hook: Hook, context: HookContext) -> ToolResult:
        call_id = f"hook_{hook.id}_{uuid4().hex[:8]}"
        tool = ToolContext(
            context.session_id,
            context.run_id,
            call_id,
            context.workspace,
            call_id=call_id,
            tool_name="hook__" + hook.id,
            agent_id="hook",
        )
        try:
            result = await self._actions.execute(hook.action, context, tool)
        except Exception as error:
            # A Hook that cannot run is a failed Hook; pre_tool_use then blocks the tool.
            result = ToolResult(
                call_id,
                tool.tool_name,
                str(error) or type(error).__name__,
                is_error=True,
                error_code=error.code if isinstance(error, DomainError) else "hook_failed",
            )
        # Only output that enters the model context is recorded and announced.
        if hook.action.type == "command" or not result.is_error:
            await self._history.append_entry(
                EntryDraft(
                    "hook_context",
                    data={
                        "text": result.text
                        if hook.action.type == "prompt"
                        else (
                            f"Hook [{hook.id}] {context.event_name}: "
                            + ("failed" if result.is_error else "completed")
                            + "\n"
                            + result.text
                        ),
                        "hook_id": hook.id,
                    },
                    run_id=context.run_id,
                )
            )
            await self._events.emit(
                "notification",
                {
                    "kind": "hook",
                    "hook_id": hook.id,
                    "event": context.event_name,
                    "success": not result.is_error,
                    "message": result.text,
                    "full_output_path": result.details.get("full_output_path"),
                },
            )
        return result


async def run_hook(
    context: LifecycleContext, event: str, *, message: str = "", error: str = ""
) -> None:
    hooks = context.hooks
    if hooks is not None:
        await hooks.fire(
            HookContext(
                context.session_id,
                context.run_id,
                context.workspace,
                event,
                message=message,
                error=error,
            )
        )


class ToolHookBridge:
    def __init__(self, engine: Hooks):
        self.engine = engine

    async def fire(
        self,
        event_name: str,
        context: ToolContext,
        call: ToolCall,
        spec: ToolSpec | None = None,
        result: ToolResult | None = None,
    ) -> str | None:
        return await self.engine.fire(
            HookContext(
                context.session_id,
                context.run_id,
                context.workspace,
                event_name,
                call.name,
                clone_value(call.arguments),
                str(call.arguments.get("path", "")),
                result.text if result is not None else "",
                result.error_code or "" if result is not None else "",
            )
        )
