"""Hooks api: component-owned implementation."""

# Source: MewCode local hooks/engine.py; fixed local inventory in docs/source-map.md.
# SHA-256: 91e9d3162556ea54e9c1d211feb3ecf4126a302f50f7eb3e5f584194be9e9b29
# Adaptation: synchronous actions with append-only results and interrupted-effect records.
# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

import asyncio

from pycode.hooks.contracts import HookActions, HookExecutorFactory, LifecycleContext
from pycode.hooks.types import HookContext
from pycode.kernel import (
    EntryDraft,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSpec,
    clone_value,
    data_hash,
    plain_data,
)


class HookActionTool:
    def __init__(self, hook, actions: HookActions):
        # Custom embedders may omit policy metadata; configured subprocess actions bind it.
        policy = getattr(actions, "policy_hash", None)
        if policy is not None and not isinstance(policy, str):
            raise TypeError("Hook action policy hash must be a string")
        self.spec = ToolSpec(
            "hook__" + hook.id,
            "Configured lifecycle action",
            {
                "type": "object",
                "properties": {"context": {"type": "object"}, "action": {"type": "object"}},
                "required": ["context", "action"],
                "additionalProperties": False,
            },
            source="hook:" + hook.source,
            category="read"
            if hook.action.type == "prompt"
            else "command"
            if hook.action.type == "command"
            else "external",
            implementation_hash=data_hash(plain_data(hook)),
            policy_hash=policy,
        )
        self._actions = actions

    async def execute(self, context, arguments):
        return await self._actions.execute(
            arguments["action"], HookContext(**arguments["context"]), context
        )


class Hooks:
    def __init__(
        self,
        hooks,
        history,
        actions,
        outputs,
        events,
        *,
        executor_factory: HookExecutorFactory,
        plans=None,
        read_only=False,
    ):
        self._hooks = hooks
        self._history = history
        self._actions = actions
        self._outputs = outputs
        self._events = events
        self._executor = executor_factory(
            tuple(HookActionTool(hook, actions) for hook in hooks),
            plans=plans,
            read_only=read_only,
        )
        self._locks = {}

    async def fire(self, context):
        for hook in self._hooks:
            if hook.event != context.event_name:
                continue
            key = data_hash(
                {
                    "session": context.session_id,
                    "hook": plain_data(hook),
                    "boundary": "once" if hook.once else context.boundary_id,
                }
            )
            if not await self._actions.matches(hook, context):
                continue
            result = await self.execute_one(hook, context, key)
            if result.is_error and context.event_name == "pre_tool_use":
                return "Hook check did not complete successfully: " + result.text
            if hook.reject:
                return result.text or "Configured Hook rejected this tool"
        return None

    async def execute_one(self, hook, context, key):
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            operation_id = "hook:" + key
            snapshot = await self._history.snapshot()
            previous = None
            started = False
            for entry in snapshot.entries:
                if entry.operation_id != operation_id:
                    continue
                if entry.type == "hook_result":
                    previous = entry.data["result"]
                elif entry.type == "hook_call":
                    started = True
            if previous is not None:
                return ToolResult(**previous)
            tool_context = ToolContext(
                context.session_id,
                context.run_id,
                operation_id,
                context.workspace,
                call_id=operation_id,
                tool_name="hook__" + hook.id,
                agent_id="hook",
            )
            call = ToolCall(
                operation_id,
                tool_context.tool_name,
                {"context": plain_data(context), "action": plain_data(hook.action)},
            )
            await self._executor.begin(tool_context, call)
            if started:
                result = ToolResult(
                    call.id,
                    call.name,
                    "Hook interrupted; previous external effects are unknown",
                    is_error=True,
                    error_code="hook_interrupted",
                    session_id=context.session_id,
                    run_id=context.run_id,
                    operation_id=operation_id,
                )
            else:
                # hook_call is a durable session Store boundary even before first input.
                await self._history.append_entry(
                    EntryDraft(
                        "hook_call",
                        data={"hook": plain_data(hook), "context": plain_data(context)},
                        run_id=context.run_id,
                        operation_id=operation_id,
                    )
                )
                result = await self._executor.execute(tool_context, call, "tool_use")
            entry = await self._history.append_entry(
                EntryDraft(
                    "hook_result",
                    data={
                        "hook_id": hook.id,
                        "event": context.event_name,
                        "boundary_id": context.boundary_id,
                        "result": plain_data(result),
                    },
                    run_id=context.run_id,
                    operation_id=operation_id,
                )
            )
            if result.output_id:
                await self._outputs.bind(result.output_id, entry.id)
            if result.details.get("file_snapshot"):
                await self._history.append_entry(
                    EntryDraft(
                        "custom",
                        data={
                            "kind": "file_snapshot",
                            "message_entry_id": entry.id,
                            **result.details["file_snapshot"],
                        },
                        run_id=context.run_id,
                        operation_id=operation_id,
                    )
                )
            if hook.action.type == "command" or not result.is_error:
                await self._history.append_entry(
                    EntryDraft(
                        "hook_context",
                        data={
                            "text": result.text
                            if hook.action.type == "prompt"
                            else (
                                f"Hook [{hook.id}] {context.event_name}: "
                                + ("failed or interrupted" if result.is_error else "completed")
                                + "\n"
                                + result.text
                            ),
                            "hook_id": hook.id,
                            "operation_id": operation_id,
                        },
                        run_id=context.run_id,
                    )
                )
            await self._executor.emit_tool_execution_end(tool_context, call, result)
            await self._events.emit(
                "notification",
                {
                    "kind": "hook",
                    "hook_id": hook.id,
                    "event": context.event_name,
                    "success": not result.is_error,
                    "message": result.text,
                    "output_id": result.output_id,
                },
            )
            return result


async def run_hook(
    context: LifecycleContext, event: str, boundary: str, *, message: str = "", error: str = ""
) -> None:
    hooks = context.hooks
    if hooks is not None:
        await hooks.fire(
            HookContext(
                context.session_id,
                context.run_id,
                context.workspace,
                boundary,
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
                context.operation_id,
                event_name,
                call.name,
                clone_value(call.arguments),
                str(call.arguments.get("path", "")),
                result.text if result is not None else "",
                result.error_code or "" if result is not None else "",
            )
        )
